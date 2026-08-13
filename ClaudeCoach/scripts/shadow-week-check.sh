#!/usr/bin/env bash
# SHADOW WEEK — validate the agreed-week code against a REAL pinned week before the
# Sunday 18:00 cron ever sees it. Read this before running it.
#
#   bash ClaudeCoach/scripts/shadow-week-check.sh jamie 2026-08-17
#
# WHY THIS EXISTS. Increment 2 changes what the Sunday build pushes, for all three
# athletes, on the one job whose failure is invisible until an athlete asks where their week
# went. The offline suite (scripts/test_agreed_week.py) proves the mechanism against stubs;
# this proves it against the real store, the real brief and a real LLM proposal, WITHOUT
# writing to anyone's calendar. Run it once per athlete, on a week with a real pin, and read
# the output before you let the cron have the code.
#
# WHAT IT DOES NOT DO. It never passes --push, so nothing reaches Intervals.icu and no
# --notify message reaches an athlete. It does not create or release pins: it reads what is
# there. If you want a pin to test against, make one deliberately (step 0) and release it
# afterwards (step 4).
#
# COST. One full stage1-plan generation: up to 3 LLM attempts, ~10-45 minutes, and it holds
# no lock (dry runs deliberately take none), so it can be run while other work continues.
set -u
cd "$(dirname "$0")/../.." || exit 1

SLUG="${1:?usage: shadow-week-check.sh <slug> <week-start Monday YYYY-MM-DD>}"
WS="${2:?usage: shadow-week-check.sh <slug> <week-start Monday YYYY-MM-DD>}"
OUT="/tmp/shadow-week-${SLUG}-${WS}.json"

echo "== 0. THE PINS AS THEY STAND for ${SLUG}, w/c ${WS}"
echo "   If this is empty, the run below proves only that nothing broke — it does NOT"
echo "   exercise the splice. Make a pin first, e.g.:"
echo "     python3 ClaudeCoach/lib/agreed_week.py --slug ${SLUG} \\"
echo "       --pin ${WS} --why 'shadow-week test' --by operator"
echo "   (that pins an EMPTY day, which is the cheapest honest test: the build must leave"
echo "    it alone and must not push anything onto it. For the full path, pin a date that"
echo "    already carries a session and pass its --segments through icu_fetch.)"
python3 ClaudeCoach/lib/agreed_week.py --slug "$SLUG" --week-start "$WS" || exit 1

echo
echo "== 1. DRY BUILD (no --push, no --notify). This is the slow part."
python3 ClaudeCoach/scripts/stage1-plan.py --athlete "$SLUG" --week-start "$WS" > "$OUT"
rc=$?
echo "   stage1-plan exited ${rc}; summary written to ${OUT}"
[ -s "$OUT" ] || { echo "   FAIL: no output — nothing to check"; exit 1; }

echo
echo "== 2. DIFF THE BUILT WEEK AGAINST THE PINS"
python3 - "$SLUG" "$WS" "$OUT" <<'PY'
import json, sys
sys.path.insert(0, "ClaudeCoach/lib")
import agreed_week as aw

slug, ws, out = sys.argv[1], sys.argv[2], sys.argv[3]
s = json.loads(open(out).read())
pins = aw.pins_for_week(slug, ws)
protected = aw.protected_dates(slug, ws)
built = {x["date"]: x for x in s.get("sessions", [])}
bad = []

def want(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        bad.append(msg)

want(s.get("week_start") == ws,
     f"the build planned the week you asked for ({s.get('week_start')} vs {ws})")
want(set(s.get("pinned_days") or []) == set(pins),
     "the build saw exactly the pins in the store")

for d, p in sorted(pins.items()):
    sess = (p or {}).get("session") or {}
    if not sess:
        # A REST-DAY pin: nothing may be planned on it at all.
        want(d not in built, f"{d} is an agreed EMPTY day and the build put nothing on it")
        continue
    b = built.get(d)
    want(bool(b), f"{d} (agreed {sess.get('sport')}) is present in the built week")
    if not b:
        continue
    want(b.get("pinned") is True, f"{d} is flagged pinned, so push() will skip it")
    want(b.get("sport") == sess.get("sport"),
         f"{d} kept the agreed sport ({b.get('sport')} vs {sess.get('sport')})")
    want(b.get("name") == (sess.get("name") or ""),
         f"{d} kept the agreed name ({b.get('name')!r})")
    if sess.get("load_target"):
        want(int(b.get("load") or 0) == int(sess["load_target"]),
             f"{d} carries the AGREED load {sess['load_target']}, not a re-derivation "
             f"(got {b.get('load')})")
    if sess.get("minutes"):
        want(int(b.get("min") or 0) == int(sess["minutes"]),
             f"{d} kept its agreed {sess['minutes']} min (got {b.get('min')})")
    if sess.get("coarse"):
        print(f"  note {d} is a COARSE pin — its zone accounting is approximate, so the "
              f"distribution advisories for that sport are indicative only")

for d in sorted(set(protected) - set(pins)):
    want(d not in built,
         f"{d} was declared unavailable and the build planned nothing on it")

tgt, ptgt, pl = s.get("target_tss"), s.get("proposer_target_tss"), s.get("pinned_load")
if tgt and pl is not None:
    want(ptgt == max(0, tgt - pl),
         f"the proposer was aimed at {tgt} - {pl} = {max(0, tgt - pl)} (got {ptgt})")
    print(f"  note the WHOLE-week gate judged {s.get('built_total_tss')} against {tgt} "
          f"({s.get('load_pct_off_target')}% off, on_target={s.get('load_on_target')}) — "
          f"that number must include the agreed days")
want(not any("dropped" in a for a in s.get("attempts", [])) or True,
     "attempts log read (see below for any 'agreed-days: proposer planned on agreed days')")
for a in s.get("attempts", []):
    if "agreed-days" in a or "quality-injection" in a:
        print("  log  " + a)

print()
if bad:
    print(f"SHADOW WEEK: {len(bad)} PROBLEM(S) — do NOT let the Sunday cron have this code:")
    for b in bad:
        print("  -", b)
    sys.exit(1)
print("SHADOW WEEK: the built week honours every pin. Nothing was pushed.")
PY
rc2=$?

echo
echo "== 3. WHAT TO READ BY EYE (the checks above cannot judge these)"
echo "   * ready_to_push in ${OUT}: false with a load blocker on a heavily pinned week is"
echo "     CORRECT behaviour, not a failure — the week genuinely may not fit. false with a"
echo "     SAFETY blocker (run cap, ankle quality, CTL ramp) is worth understanding before"
echo "     the cron runs."
echo "   * blocking_issues / advisories: an agreed day's own breach (e.g. a pinned run over"
echo "     the weekly run cap) SHOULD still be reported. Silence there means the pinned"
echo "     session is not being counted, which is the fiction the pin record exists to stop."
echo "   * the ops-alerts log: one 'agreed-week' line per pin/release, and any COARSE PIN"
echo "     warning:  grep agreed-week ClaudeCoach/logs/ops-alerts.log | tail -20"
echo
echo "== 4. TIDY UP (only if you created a pin in step 0)"
echo "   python3 ClaudeCoach/lib/agreed_week.py --slug ${SLUG} --week-start ${WS} \\"
echo "     --release --by operator"
echo
echo "== 5. ONLY THEN"
echo "   Re-run with --push by hand for ONE athlete and check the calendar before the"
echo "   Sunday cron plans all three. weekly-plan.sh is unchanged by this increment."
exit $rc2
