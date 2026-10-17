#!/usr/bin/env python3
"""Night-before brief — runs via VM crontab at 20:30 daily. Loops over all active athletes."""
import json, subprocess, sys
from datetime import date, datetime, timedelta
from pathlib import Path

BASE            = Path(__file__).parent.parent  # ClaudeCoach/
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"
NOTIFY          = BASE / "telegram/notify.py"
ATHLETES_CONFIG = BASE / "config/athletes.json"
LOG_DIR         = Path.home() / "Library/Logs/ClaudeCoach"
LOG_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BASE / "lib"))
import coaching_prefs  # noqa: E402  heat / fuelling switches
from coaching_levels import level_block as _level_block  # noqa: E402
import shadow          # noqa: E402  shadow week (Haiku twin, never sent)
sys.path.insert(0, str(BASE / "ironman-analysis"))
import claude_call
import ops_log
import planning_pause    # tracking-only athletes: no prescribing, no adherence
from progression import long_run_cap_km as _lr_cap
from primitives.nutrition import (recent_avg_g_hr, recent_run_avg_g_hr, fuel_target,  # noqa: E402
                                  run_fuel_target, RIDE_SPORTS, RUN_SPORTS)
import fuel_basis      # noqa: E402
import races as races_lib

TOOLS = "Read,Bash"


def _race_focus(slug, profile, athlete_cfg):
    """Focus points for the pre-race message: a curated profile.json race_focus list if
    there is one, otherwise derived from the athlete's own standing rules and logged
    fuelling (lib/races.py derive_focus). Every failure mode degrades to NO focus points
    rather than to a guess — an empty list simply drops that sentence from the message."""
    try:
        rules_f = BASE / f"athletes/{slug}/persistent-rules.md"
        rules = rules_f.read_text() if rules_f.exists() else ""
        avg = None
        try:
            log_f = BASE / f"athletes/{slug}/session-log.json"
            if log_f.exists():
                avg = recent_avg_g_hr(json.loads(log_f.read_text()))
        except Exception:
            pass
        return races_lib.focus_for(profile, rules, avg,
                                   athlete_cfg.get("nutrition_target_g_hr"))
    except Exception as exc:
        print(f"[{slug}] race focus derivation failed: {exc}", file=sys.stderr)
        return []


def _block_fact(athlete_cfg, today):
    """One true fact about the work behind the race (§8.6 "name the work behind it").
    Mirrors morning-checkin._block_fact; weeks since plan_start is the only such fact
    available for every athlete without inventing one."""
    ps = athlete_cfg.get("plan_start")
    if not ps:
        return ""
    try:
        weeks = (today - date.fromisoformat(ps)).days // 7
    except Exception:
        return ""
    return f"You have {weeks} weeks of work behind you for this one" if weeks >= 2 else ""


SWIM_SPORTS = ("Swim", "OpenWaterSwim")


def _sport_family(sport) -> str:
    """"ride" / "run" / "swim" / "strength" / "other" for an ICU event type."""
    sport = str(sport or "")
    if sport in RIDE_SPORTS or sport.endswith("Ride"):
        return "ride"
    if sport in RUN_SPORTS:
        return "run"
    if sport in SWIM_SPORTS:
        return "swim"
    if sport == "WeightTraining":
        return "strength"
    return "other"


def _pace_s(value):
    """Seconds from the first "M:SS" (or "M:SS.s") in a field that may carry prose
    (Kathryn's run threshold is a whole note). None if there is no pace in it."""
    import re
    m = re.search(r"(?<![\d.])(\d{1,2}):([0-5]\d(?:\.\d+)?)", str(value or ""))
    return int(m.group(1)) * 60 + float(m.group(2)) if m else None


def _mmss(sec) -> str:
    sec = int(sec + 0.5)            # half up: 122.5 s is 2:03, not round()'s 2:02
    return f"{sec // 60}:{sec % 60:02d}"


def _workouts(events):
    return [e for e in events or [] if (e.get("category") or "WORKOUT").upper() == "WORKOUT"]


def _pace_bands(events, run_threshold, css=None) -> list[str]:
    """Each "NN-MM% Pace" in tomorrow's steps as a pace band, worked out here so the model
    quotes it rather than doing the sum (9 Oct 2026: two models gave 4:35-5:10 and
    4:36-5:12 for the same 78-88% of 4:02). Pace = threshold / fraction.
    By sport (Jamie, 16 Oct 2026: shadow review): a swim's "% Pace" is a share of CSS
    and comes out /100m; it used to be worked against the RUN threshold, so Kathryn's
    technique swim was given "58-66% Pace = 7:41-8:44/km". Rides get no pace bands.
    CSS is profile.json swim_css_per_100m, the source this file already quotes."""
    import re
    out = []
    for e in _workouts(events):
        fam = _sport_family(e.get("type"))
        if fam == "run":
            thr, unit = _pace_s(run_threshold), "/km"
        elif fam == "swim":
            thr, unit = _pace_s(css), "/100m"
        else:
            continue
        if not thr:
            continue
        for lo, hi in re.findall(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)%\s*Pace",
                                 str(e.get("description") or "")):
            band = (f"{'Run' if fam == 'run' else 'Swim'} {lo}-{hi}% Pace = "
                    f"{_mmss(thr / (float(hi) / 100))}-{_mmss(thr / (float(lo) / 100))}{unit}")
            if band not in out:
                out.append(band)
    return out


def _power_bands(events, ftp) -> list[str]:
    """Each %FTP step in tomorrow's rides ("50-58%", "95%", "88-94% FTP") as watts, worked
    out here (Jamie, 16 Oct 2026: shadow review: left to the model, Sonnet derived
    "187-199 W" from FTP against "never re-derive" and Haiku left the watts out).
    W = FTP x fraction, same FTP as the thresholds line (profile.json ftp_watts).
    % HR / % LTHR / % Pace / % MMP steps are not power and are skipped."""
    import re
    try:
        ftp = float(ftp)
    except (TypeError, ValueError):
        return []
    if ftp <= 0:
        return []
    out = []
    for e in _workouts(events):
        if _sport_family(e.get("type")) != "ride":
            continue
        for lo, hi in re.findall(_PCT, str(e.get("description") or "")):
            if hi:
                band = f"Ride {lo}-{hi}% FTP = {round(ftp * float(lo) / 100)}-{round(ftp * float(hi) / 100)} W"
            else:
                band = f"Ride {lo}% FTP = {round(ftp * float(lo) / 100)} W"
            if band not in out:
                out.append(band)
    return out


_PCT = r"(?<![\d.])(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?\s*%(?!\s*(?:Pace|HR|LTHR|MMP))"


def _main_band(event, pct_re):
    """(lo, hi): the one ride band the "Main set power" bullet quotes, picked here
    (Jamie, 17 Oct 2026: shadow review: Jamie's ride had 15m 50-58% at each end and 57m
    60-70% between, both bands were listed, and Haiku printed the 50-58% one as an extra
    "Easy blocks" bullet while Sonnet did not). Minutes per %FTP band, a "4x" block's
    steps counted 4 times; the main set is the hardest band with 10+ min in it (so a 5-min
    opener in an endurance ride is not it), else the hardest band. "" if none."""
    import re
    mins, mult = {}, 1
    for raw in str(event.get("description") or "").splitlines():
        line = raw.strip()
        if not line:
            mult = 1
            continue
        if not line.startswith("-"):
            m = re.search(r"(\d+)\s*x\s*$", line, re.I)
            mult = int(m.group(1)) if m else 1
            continue
        step = line.lstrip("-").strip()
        tok = step.split()[0] if step.split() else ""
        d = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", tok)
        if not tok or not d:
            continue
        t = int(d.group(1) or 0) * 60 + int(d.group(2) or 0) + int(d.group(3) or 0) / 60
        p = re.search(pct_re, step)
        if not p:
            continue
        lo, hi = float(p.group(1)), float(p.group(2) or p.group(1))
        mins[(lo, hi)] = mins.get((lo, hi), 0) + t * mult
    if not mins:
        return None
    long_enough = [b for b, t in mins.items() if t >= 10] or list(mins)
    return max(long_enough, key=lambda b: (b[1], b[0]))


_PACE_PCT = r"(?<![\d.])(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?\s*%\s*Pace"


def _main_set_pace(event, threshold, unit) -> str:
    """"4:35-5:10/km": the one pace band a run (or swim) "Target pace" bullet quotes - the
    main set, picked like _main_set_watts (Jamie, 17 Oct 2026: the brief quoted his run's
    15-min warm-up band, 6:18-7:12, as the target for the whole run). "" if none."""
    thr = _pace_s(threshold)
    b = _main_band(event, _PACE_PCT)
    if not thr or not b:
        return ""
    lo, hi = b
    return f"{_mmss(thr / (hi / 100))}-{_mmss(thr / (lo / 100))}{unit}"


def _main_set_watts(event, ftp) -> str:
    """Watts for the main-set %FTP band (_main_band)."""
    try:
        ftp = float(ftp)
    except (TypeError, ValueError):
        return ""
    if ftp <= 0:
        return ""
    b = _main_band(event, _PCT)
    if not b:
        return ""
    lo, hi = b
    w_lo, w_hi = round(ftp * lo / 100), round(ftp * hi / 100)
    return f"{w_lo}-{w_hi} W" if w_lo != w_hi else f"{w_lo} W"


def _np_if(event, ftp) -> str:
    """"NP target 188 W (IF 0.62)" for a planned ride, from Intervals.icu's planned
    intensity (icu_intensity, IF x 100) and the same FTP as the power bands. Worked out
    here, to 2 dp (Jamie, 16 Oct 2026: shadow review: the template said "(IF [X.XX])"
    and the rules said "never round", so Haiku printed IF 0.648). "" if not planned."""
    try:
        i = float(event.get("icu_intensity")) / 100
        ftp = float(ftp)
    except (TypeError, ValueError):
        return ""
    if i <= 0 or ftp <= 0:
        return ""
    return f"NP target {round(ftp * i)} W (IF {i:.2f})"


def _title_line(events) -> str:
    """The brief's title, built here so the model copies it rather than reshaping the
    sessions list (Jamie, 16 Oct 2026: shadow review: Step 3's "WeightTraining ·
    Strength: quads · 40 min" was fed where the template wanted "[session name]").
    Event names in calendar order, joined with " + "."""
    names = [str(e.get("name") or "").strip() or "Session"
             for e in sorted(events or [], key=lambda e: e.get("start_date_local") or "")]
    return f"*Tomorrow — {' + '.join(names)}*" if names else ""


def _target_lines(events, css, ftp, run_threshold=None, run_note="", level="mid") -> list[str]:
    """One line of target bullets per sport tomorrow, with no "Ride:" / "Strength:" label
    (Jamie, 16 Oct 2026: shadow review: the four sport templates read like printable text,
    so Haiku printed the labels). Only tomorrow's sports, in calendar order, once each;
    a ride's NP/IF is filled in here (_np_if).
    By coaching level (Jamie, 16 Oct 2026: "minimal jargon not 0 jargon ... depend on the
    level of detail the athlete asked for"): NP and IF for pro only; watts for mid and pro;
    a beginner's ride is effort and heart rate (lib/coaching_levels.py).
    Shadow review, 17 Oct 2026 (Jamie): ONE bullet per line (the template's "• A • B" on
    one line was printed on one line); a "• 87 min · Load 55" bullet per session written
    here (with "2-4 bullets" and no duration bullet, Sonnet added "Duration 100 min,
    Load 70" for two athletes and nothing for the third); the ride's main-set watts filled
    in (_main_set_watts); and a strength session with no exercises in the calendar names
    none (Sonnet invented "swings, goblet squats, deadlifts and carries" for Kathryn's
    empty Kettlebells event, Haiku wrote "Key movements: not set in the calendar yet")."""
    evs = sorted(events or [], key=lambda e: e.get("start_date_local") or "")
    out = []
    for e in evs:
        fam = _sport_family(e.get("type"))
        bullets = []
        bits = []
        if e.get("moving_time"):
            bits.append(f"{round(e['moving_time'] / 60)} min")
        if e.get("icu_training_load") and level != "beginner":      # beginner: no Load / TSS
            bits.append(f"Load {e['icu_training_load']}")
        if bits:
            name = str(e.get("name") or "").strip()
            bullets.append("• " + (f"{name}: " if len(evs) > 1 and name else "") + " · ".join(bits))
        if fam == "ride":
            np_if = _np_if(e, ftp) if level == "pro" else ""
            if np_if:
                bullets.append(f"• {np_if}")
            if level != "beginner":
                w = _main_set_watts(e, ftp)
                if w:
                    bullets.append(f"• Main set power {w}")
            else:
                bullets.append("• How it should feel [easy / steady / hard, from the planned event]")
            bullets.append("• HR cap [bpm] only if the planned event sets one; otherwise leave that"
                           " bullet out, never write that none is set")
        elif fam == "run":
            pace = _main_set_pace(e, run_threshold, "/km")
            bullets += [f"• Target pace {pace}" if pace else
                        "• Target pace [/km, as the planned event gives it, the pace only]",
                        f"• HR cap [bpm]{run_note}"]
        elif fam == "swim":
            pace = _main_set_pace(e, css, "/100m")
            bullets.append((f"• Main set pace {pace}" if pace else
                            "• Target pace [/100m, as the planned event gives it, the pace only]")
                           + (f" vs CSS {css or '?'}" if level == "pro" else ""))
            bullets.append("• Main set structure")
        elif fam == "strength":
            if str(e.get("description") or "").strip():
                bullets += ["• Main focus", "• Key movements [only the exercises the planned event lists]"]
            else:
                bullets.append("• Main focus [one line from the session name; the planned event lists no"
                               " exercises, so name none and write no key-movements bullet]")
        else:
            bullets += ["• What the session is", "• How it should feel"]
        for b in bullets:
            if b not in out:
                out.append(b)
    return out


def _form_and_cap(client, today: date) -> tuple[str, str, int | None]:
    """("+20.9 (Fresh)", "146", 297) from Intervals.icu: today's Form (CTL - ATL) with the
    morning card's bands (> +10 Fresh, < -20 Heavy, else In training), the top of the
    run Z2 HR zone, and the Ride FTP. Worked out here so the brief quotes them: left to the
    model, two runs of the same prompt gave Form +21 and +14.5, and one gave no HR cap at all.
    Ride FTP is the sport-settings number the watch workout's %FTP resolves against, so
    the brief's watts match the device (Jamie, 16 Oct 2026: shadow review: profile.json
    said 305 W, Intervals.icu 297 W); read in the same sport-settings call as the HR cap."""
    form = cap = ""
    ride_ftp = None
    rows = [r for r in client.get_wellness(days=2) or [] if r.get("id") == today.isoformat()]
    if rows and rows[0].get("ctl") is not None and rows[0].get("atl") is not None:
        v = float(rows[0]["ctl"]) - float(rows[0]["atl"])
        form = f"{v:+.1f} ({'Fresh' if v > 10 else 'Heavy' if v < -20 else 'In training'})"
    for ss in client.get_sport_settings() or []:
        types = ss.get("types") or []
        if not cap and "Run" in types and len(ss.get("hr_zones") or []) > 1:
            cap = str(ss["hr_zones"][1])
        if ride_ftp is None and "Ride" in types and ss.get("ftp"):
            ride_ftp = int(ss["ftp"])
    return form, cap, ride_ftp


def _fuel_line(events, athlete_cfg, session_log) -> str:
    """Tomorrow's fuelling, worked out here, not by the model (shadow week, 13 Oct 2026:
    left to "calibrate to session length and intensity", Sonnet gave Fred 40 g/hr and
    Haiku 30 g/hr for the same 75-min sweetspot ride). Driven by the longest ride or run:
      under 60 min, or 60-90 min and easy   no carbs, water to thirst
      60-90 min with work in it             30 g/hr (Load per hour >= 55)
      90 min and over                       the athlete's own ramp (fuel_target /
                                            run_fuel_target toward fuel_basis's ceiling)
    Swims and strength need nothing during the session."""
    best = None
    for ev in events or []:
        sport = str(ev.get("type") or "")
        if sport not in RIDE_SPORTS + RUN_SPORTS:
            continue
        mins = (ev.get("moving_time") or 0) / 60
        if best is None or mins > best[0]:
            best = (mins, sport, ev.get("icu_training_load") or 0)
    if not best or best[0] < 60:
        return "0 g/hr carbs, water to thirst"
    mins, sport, load = best
    if mins < 90:
        return ("30 g/hr carbs + 500 ml/hr fluid" if load / (mins / 60) >= 55
                else "0 g/hr carbs, water to thirst")
    if sport in RUN_SPORTS:
        g = run_fuel_target(recent_run_avg_g_hr(session_log), fuel_basis.run_ceiling(athlete_cfg))
    else:
        g = fuel_target(recent_avg_g_hr(session_log), fuel_basis.ceiling(athlete_cfg))
    return f"{g} g/hr carbs + 500-750 ml/hr fluid"


def _to_brief(events) -> list[dict]:
    """Tomorrow's sessions worth a brief: planned workouts with Load >= 30 or 40+ min.
    Decided here, not by the model (shadow week, 13 Oct 2026: with only a strength
    session of no set length on the calendar, Sonnet stayed silent and Haiku briefed it)."""
    out = []
    for e in events or []:
        if (e.get("category") or "WORKOUT") != "WORKOUT":
            continue
        if (e.get("icu_training_load") or 0) >= 30 or (e.get("moving_time") or 0) >= 40 * 60:
            out.append(e)
    return out


def _sessions_line(events) -> str:
    """The sessions the brief must cover, in calendar order."""
    parts = []
    for e in sorted(events, key=lambda e: e.get("start_date_local") or ""):
        bits = [str(e.get("type") or ""), str(e.get("name") or "session")]
        if e.get("moving_time"):
            bits.append(f"{round(e['moving_time'] / 60)} min")
        if e.get("icu_training_load"):
            bits.append(f"Load {e['icu_training_load']}")
        parts.append(" · ".join(b for b in bits if b))
    return "; ".join(parts)


def _build_prompt(slug, first_name, ftp, css, run_threshold, race_name, injuries, long_run_cap_km=None,
                  pace_bands=None, form="", hr_cap="", fuel="", sessions="",
                  power_bands=None, title="", targets=None, level="mid", fuelling=True):
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    long_run_cap_block = ""
    if long_run_cap_km is not None:
        long_run_cap_block = (
            f"\n## Long-run cap (pre-computed — authoritative)\n"
            f"Ceiling: {long_run_cap_km:.1f} km — do not reference or target a distance above this for tomorrow's long run.\n"
        )

    # Build injury flag line
    injury_flag = ""
    if injuries:
        injury_flag = (
            "\n[If any injury pain score >=3 in current-state.json AND a run is planned: "
            "add \"⚠️ Check injury before starting — note score after.\"]"
        )

    # Build athlete thresholds line
    thresholds = []
    if ftp:
        thresholds.append(f"FTP {ftp} W")
    if run_threshold:
        # The pace only: Kathryn's field is a whole coaching note (Jamie, 16 Oct 2026: shadow review)
        _rt = _pace_s(run_threshold)
        thresholds.append(f"run threshold {_mmss(_rt)}/km" if _rt else f"run threshold {run_threshold}")
    if css:
        thresholds.append(f"swim CSS {css}/100m")
    threshold_line = f"{first_name}: " + ", ".join(thresholds) + "." if thresholds else ""

    # Step 2 only for an ACTIVE injury: run unconditionally it pulled stale notes from
    # current-state.json into the brief (an old Achilles line for Jamie, cleared ankle).
    step2 = (f"Step 2 — Read ClaudeCoach/athletes/{slug}/current-state.json for the active "
             "injury's last pain score only." if injuries else
             "Step 2 — No active injury: say nothing about injuries.")
    bands_line = ("Pace bands, already worked out (quote exactly): " + "; ".join(pace_bands)
                  if pace_bands else "")
    power_line = ("Power bands, already worked out (quote the W exactly): " + "; ".join(power_bands)
                  if power_bands else "")
    form_line = f"Form for the brief, already worked out (quote exactly): {form}" if form else ""
    cap_line = (f"Run HR cap for easy/Z2 running: {hr_cap} bpm (top of Z2 in Intervals.icu)"
                if hr_cap else "")

    # Run-specific notes for injury athletes
    run_note = ""
    if injuries:
        run_note = " Note any active injury protocols from current-state.json."

    # Title and target bullets are built in code from tomorrow's events when the calendar
    # was read (_title_line / _target_lines; Jamie, 16 Oct 2026: shadow review). Only if
    # that read failed does the model get the generic, per-sport guide instead.
    if title:
        title_block = f"{title}\n(the title line: copy it exactly as written here)"
    else:
        title_block = "*Tomorrow — [the session name(s) from the calendar, joined with ' + ']*"
    if targets:
        targets_block = ("[Targets — exactly these bullets, one per line, in this order, filled in "
                         "where marked [ ]; add none. Write only the bullets: no sport name or label "
                         "before them.]\n" + "\n".join(targets))
    else:
        targets_block = (
            "[Targets — 2-4 bullets, only for the sport(s) on tomorrow's calendar. The words "
            "\"for a ride\" etc. are guidance, never printed: write only the bullets.]\n"
            + ("for a ride: • NP target [W] (IF [X.XX], 2 decimal places) • HR cap" if level == "pro" else
               "for a ride: • Main set power [W] • HR cap") + " [bpm] only if the planned event sets one; otherwise leave that bullet out, never write that none is set\n"
            f"for a run: • Target pace [/km] • HR cap [bpm]{run_note}\n"
            f"for a swim: • Target pace [/100m] vs CSS {css or '?'} • Main set structure\n"
            "for strength: • Main focus • Key movements")

    # The level paragraph is shared with chat and debriefs, where PRO means "lead with a
    # narrative verdict" and "NP/IF only when asked". Here the layout is fixed and the pro
    # NP/IF bullet is written in code (Jamie, 17 Oct 2026: shadow review: both models put a
    # narrative paragraph above Jamie's bullets, Haiku dropped his NP bullet and relabelled
    # "Form" as "Form (TSB)"). So the level sets the wording; Step 4 sets the layout.
    # One verdict line back, in its own slot under the title (Jamie, 17 Oct 2026: "A bring
    # back"): fixed position, so Haiku and Sonnet still write the same card.
    layout_rule = ("For this brief the level above sets the WORDING only. The layout is Step 4's, at "
                   "every level: the ONE verdict line in its slot is the only sentence of narrative, "
                   "every bullet Step 4 gives is kept, and the bold labels are copied exactly as written.")
    # Fuelling off: no Nutrition line in the template at all, rather than a template line
    # and a note further down saying to leave it out (Jamie, 17 Oct 2026: shadow review).
    nutrition_line = (f"*Nutrition:* {fuel or '[g/hr carbs + ml/hr fluid — calibrated to session length and intensity. Zero if easy/recovery.]'}\n"
                      if fuelling else "")

    return f"""\
You are generating the night-before session brief for {first_name}.

Apply the GLOBAL coaching rules in ClaudeCoach/athletes/_shared/persistent-rules.md (read them first).

{_level_block(level)}
{layout_rule}

Step 1 — Fetch data via Bash:
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint profile
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint events --start {tomorrow} --end {tomorrow}
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint wellness --days 3

{step2}

Reference for working out targets (NEVER print these lines; they are not part of the brief):
  {threshold_line or "no thresholds on file"}
  {bands_line}
  {power_line}
  {form_line}
  {cap_line}

{"Step 3 — Tomorrow's sessions to brief, already decided (cover EVERY one of these, and no others; this list is for reference, the title is given below): " + sessions if sessions else "Step 3 — If no events tomorrow, or only events with planned Load < 30 AND duration < 40 min: output nothing. Stop."}
{long_run_cap_block}
Step 4 — Output the night-before brief in Telegram Markdown (no preamble, no sign-off):

{title_block}
[Verdict — ONE short sentence: what tomorrow's session is for and how it should feel, in the level's words. No numbers.]

{targets_block}
Numbers (pace bands, power bands, NP, IF, HR caps, durations, Load) are copied EXACTLY as
this prompt, the planned event or the data gives them: never round, re-derive or tidy a figure.{" The Nutrition line is already worked out: copy it exactly." if fuel and fuelling else ""}

{nutrition_line}*Sleep:* ≥8h tonight
*Form:* {"[Fresh / In training / Heavy] only, no number" if level == "beginner" else "[value] ([Fresh / In training / Heavy])"}{injury_flag}

The brief ends at the Form line. Keep the entire brief under 120 words. Never ask questions.
Wrap your entire output in <telegram> and </telegram> tags. If Step 3 says output nothing, output empty tags: <telegram></telegram>. Output nothing outside those tags."""


def _strip_questions(text):
    """Backstop for "Never ask questions" in the prompt above.

    This brief stays a SEPARATE 20:30 push from the 21:00 check-in — it carries tomorrow's
    targets, which is a different job from capturing today, and it has a deterministic
    race-eve path that a check-in prompt must not swallow. The price of keeping it separate
    is that the evening must still contain at most ONE question, and that has to be the
    check-in's. So any question line the model emits here is dropped rather than trusted.
    """
    out, dropped = [], []
    for line in (text or "").split("\n"):
        # Targets bullets and the bold headers can carry a legitimate '?' — the prompt's
        # own swim line is "vs CSS {css or '?'}" — so only prose lines are candidates.
        if "?" in line and not line.lstrip().startswith(("•", "*", "_", "-")):
            dropped.append(line.strip())
            continue
        out.append(line)
    if dropped:
        print(f"dropped question line(s) from brief: {dropped}", file=sys.stderr)
    return "\n".join(out).strip()


def notify(msg, chat_id):
    try:
        subprocess.run(
            ["python3", str(NOTIFY), "--chat-id", str(chat_id), msg],
            cwd=PROJECT_DIR, timeout=15,
        )
    except Exception:
        pass


def run_athlete(slug, athlete_cfg):
    adir = BASE / f"athletes/{slug}"
    chat_id = athlete_cfg.get("chat_id", "")
    log_file = LOG_DIR / "night-before-brief.log"

    profile = {}
    if (adir / "profile.json").exists():
        try:
            profile = json.loads((adir / "profile.json").read_text())
        except Exception:
            pass

    first_name = profile.get("name", slug).split()[0]
    level = profile.get("coaching_level", "mid")
    ftp = profile.get("ftp_watts")
    css = profile.get("swim_css_per_100m")
    run_threshold = profile.get("run_threshold_pace_per_km")
    race_name = profile.get("race_name") or athlete_cfg.get("race_name", "your race")
    injuries = [_i for _i in profile.get("injuries", []) if str(_i.get("status") or "").lower() not in ("cleared", "resolved")]   # cleared = not current

    # Pre-compute tomorrow's long-run distance cap the same way morning-checkin
    # does, so a long run can't be quoted past the progression ceiling here either.
    long_run_cap = None
    events = []
    events_ok = False
    try:
        from primitives.modulation import classify_session_type as _lr_classify
        from icu_api import IcuClient as _Icu
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        athlete_cfg_full = json.loads(ATHLETES_CONFIG.read_text())[slug]
        events = _Icu(athlete_cfg_full["icu_athlete_id"], athlete_cfg_full["icu_api_key"]).get_events(tomorrow, tomorrow)
        events_ok = True
        session_log_path = adir / "session-log.json"
        session_log = json.loads(session_log_path.read_text()) if session_log_path.exists() else []
        long_run_cap = _lr_cap(events, session_log, _lr_classify)
    except Exception as exc:
        print(f"[{slug}] long-run cap pre-compute failed: {exc}", file=sys.stderr)

    # Race eve. This is the surface §8.6 cares about most: "the night before is for
    # confidence, not optimisation", and the single most important rule is to introduce
    # NOTHING new. A generated brief cannot be held to that reliably, so on race eve the
    # brief is replaced by a deterministic message. Every other night is untouched.
    _races = races_lib.load_races(slug, {slug: athlete_cfg})
    _phase = races_lib.race_phase(_races, date.today())
    if _phase["phase"] == "race_eve":
        notify(races_lib.render_pre_race(
            _phase["race"], first_name, phase="race_eve",
            block_fact=_block_fact(athlete_cfg, date.today()),
            focus=_race_focus(slug, profile, athlete_cfg)), chat_id)
        ops_log.record_run("night-before-brief", athlete=slug, ok=True,
                           detail="sent (race eve)")
        return
    if _phase["phase"] == "race_day":
        # 20:30 on the EVENING OF the race. Nothing is sent: a generated session brief
        # here would be a training message hours after a race, and §8.6 is explicit that
        # on a bad day the analysis "does not come the same day at all". The
        # acknowledgement is the morning-after card's job, not this surface's.
        print(f"[{slug}] race day — no night-before brief sent", file=sys.stderr)
        ops_log.record_run("night-before-brief", athlete=slug, ok=True,
                           detail="silent (race day, by design)")
        return

    try:
        pace_bands = _pace_bands(events, run_threshold, css)
    except Exception:
        pace_bands = []
    form = hr_cap = ""
    icu_ftp = None
    try:
        from icu_api import IcuClient as _Icu2
        _c = json.loads(ATHLETES_CONFIG.read_text())[slug]
        form, hr_cap, icu_ftp = _form_and_cap(_Icu2(_c["icu_athlete_id"], _c["icu_api_key"]), date.today())
    except Exception as exc:
        print(f"[{slug}] form / HR cap pre-compute failed: {exc}", file=sys.stderr)
    # Watts and NP use Intervals.icu's Ride FTP (what the device shows); profile.json
    # ftp_watts only if ICU has none (Jamie, 16 Oct 2026: shadow review).
    ftp = icu_ftp or ftp
    try:
        power_bands = _power_bands(events, ftp)
    except Exception:
        power_bands = []
    fuel = ""
    try:
        fuelling = coaching_prefs.fuelling_on(slug)
    except Exception:
        fuelling = True
    try:
        _sl = json.loads((adir / "session-log.json").read_text()) if (adir / "session-log.json").exists() else []
        fuel = _fuel_line(events, athlete_cfg, _sl) if fuelling else ""
    except Exception as exc:
        print(f"[{slug}] fuel pre-compute failed: {exc}", file=sys.stderr)
    # Whether to brief at all is decided here when the calendar was read (_to_brief).
    sessions = title = ""
    targets = None
    if events_ok:
        _brief = _to_brief(events)
        if not _brief:
            print(f"[{slug}] nothing tomorrow worth a brief - silent", file=sys.stderr)
            ops_log.record_run("night-before-brief", athlete=slug, ok=True,
                               detail="silent (no session worth a brief, decided in code)")
            return
        sessions = _sessions_line(_brief)
        title = _title_line(_brief)
        targets = _target_lines(_brief, css, ftp, run_threshold,
                                " Note any active injury protocols from current-state.json." if injuries else "",
                                level=level)
    prompt = _build_prompt(slug, first_name, ftp, css, run_threshold, race_name, injuries,
                           long_run_cap_km=long_run_cap, pace_bands=pace_bands,
                           form=form, hr_cap=hr_cap, fuel=fuel, sessions=sessions,
                           power_bands=power_bands, title=title, targets=targets, level=level,
                           fuelling=fuelling)
    prompt += coaching_prefs.prompt_note(slug)

    with open(log_file, "a") as lf:
        result = claude_call.run_claude(
            prompt, model=claude_call.SONNET, fallback=[claude_call.OPUS],
            allowed_tools=TOOLS, stderr=lf, cwd=PROJECT_DIR, timeout=180, label=slug,
            athlete=slug,
        )

    import re as _re
    raw = (result.stdout or "").strip()
    shadow.launch("night", slug, prompt, raw)       # Haiku twin, never sent (lib/shadow.py)
    m = _re.search(r"<telegram>(.*?)</telegram>", raw, _re.DOTALL)
    output = _strip_questions(m.group(1)) if m else ""
    # HEARTBEAT. Silence is a legitimate outcome here — the prompt is instructed to
    # return empty <telegram></telegram> tags when there is nothing worth saying — so
    # "ran and correctly stayed silent" is recorded as a SUCCESS with detail="silent".
    # The gap check keys on the PRESENCE of an entry, not on its detail, so silence
    # never reads as a missed brief. The one case that is NOT success is silence with
    # no tags at all after a failed/empty model call: that is the model producing
    # nothing, which looks identical to a correct silence in the athlete's thread and
    # is precisely how a dead token would hide.
    if output:
        notify(output, chat_id)
        ops_log.record_run("night-before-brief", athlete=slug, ok=True, detail="sent")
    elif m:
        ops_log.record_run("night-before-brief", athlete=slug, ok=True,
                           detail="silent (empty tags — model chose silence)")
    else:
        ops_log.record_run("night-before-brief", athlete=slug, ok=False,
                           detail=f"no <telegram> block in model output "
                                  f"(rc={result.returncode}, auth_failed={result.auth_failed})")


def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] night-before-brief starting", file=sys.stderr)
    try:
        athletes = json.loads(ATHLETES_CONFIG.read_text())
    except Exception as e:
        print(f"[{ts}] Failed to load athletes config: {e}", file=sys.stderr)
        sys.exit(1)

    for slug, cfg in athletes.items():
        if not cfg.get("active", True):
            continue
        # Tracking-only: this athlete keeps the activity watcher and the bot, but is
        # prescribed nothing and judged on nothing (lib/planning_pause.py).
        if planning_pause.is_paused(slug, cfg):
            print(planning_pause.skip_line(slug, "night-before-brief", cfg), file=sys.stderr)
            continue
        try:
            run_athlete(slug, cfg)
        except Exception as exc:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] night-before-brief error: {exc}", file=sys.stderr)
            # A crash mid-athlete previously left NO trace in run-status.jsonl, so it
            # was indistinguishable from the whole job never running.
            ops_log.alert("night-before-brief", f"exception: {exc}", athlete=slug)


if __name__ == "__main__":
    main()
