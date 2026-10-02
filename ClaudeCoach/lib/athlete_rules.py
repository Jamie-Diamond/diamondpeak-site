"""An athlete's standing rules as THEY see and manage them (Jamie, 2 Oct 2026: "athletes
being able to see their rules, but a 'smart' way to manage it, not just asking the user to
read 20 rules in one go").

The rules themselves stay where every reader already looks for them:
athletes/<slug>/persistent-rules.md, one "[perm] ..." or "[expires:YYYY-MM-DD] ..." line
each. Identity, topic and plain-English summary live in the rule registry sidecar
(lib/rule_registry.py, athletes/<slug>/reference/rule-registry.json), keyed by a stable ID,
so nothing here changes a rule's wording except an explicit change.

Nothing is ever lost: every rule that is changed, tidied, superseded, removed or that
expires is written, word for word, to athletes/<slug>/reference/rule-notes.md first. That
archive is what lets the live rules shrink to the current instruction - the backstory
moves out of the text sent with every message, not out of existence.

    groups(slug)            the Your rules page: topics, one line each, expiring dates
    keep(slug, rid)         "still right": resets its check clock
    change(slug, rid, text) the athlete's new wording replaces the rule
    drop(slug, rid)         removed (archived first)
    next_check(slug, today) the one rule worth asking about this week, or None
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent            # ClaudeCoach/
RULES_FILE = "persistent-rules.md"
NOTES_FILE = "rule-notes.md"

TOPICS = (("schedule", "When you train"), ("training", "Training"),
          ("fuelling", "Fuelling"), ("health", "Health and body"),
          ("data", "Devices and data"), ("style", "How I talk to you"),
          ("other", "Other"))
TOPIC_KEYS = tuple(k for k, _ in TOPICS)

# The weekly check (next_check): topics that go out of date as life changes, how long a
# rule must have stood before it is worth asking about, and how long a "still right"
# holds.
VOLATILE = ("schedule", "training", "health", "fuelling")
CHECK_AFTER_DAYS = 56
NEW_RULE_GRACE_DAYS = 28

_TAG = re.compile(r"^\s*\[(perm|expires:(\d{4}-\d{2}-\d{2}))\]\s*", re.I)

# Plain-word topic guess for a rule the nightly digest has not summarised yet.
_TOPIC_WORDS = (
    ("fuelling", r"carb|fuel|gel|drink|fluid|sodium|electrolyte|bottle|eat|food|nutrition|caffeine|protein"),
    ("health", r"injur|pain|ankle|knee|sleep|weight|ill|sick|physio|hrv|recover"),
    ("schedule", r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekday|weekend|"
                 r"hours|days?\b|morning|evening|commute|work|travel|holiday"),
    ("training", r"ride|run|swim|session|interval|threshold|vo2|tempo|long|brick|strength|zone|"
                 r"ftp|pace|test|ramp|race"),
    ("data", r"garmin|strava|zwift|intervals|watch|device|upload|sync|power meter|heart.?rate strap"),
    ("style", r"message|tone|reply|say|call|address|emoji|format|debrief|wording"),
)


def _dir(slug: str, base: Path | None) -> Path:
    return (base or BASE) / "athletes" / slug


def _registry(base: Path | None):
    import rule_registry
    return rule_registry


def _rules_path(slug: str, base: Path | None = None) -> Path:
    return _dir(slug, base) / RULES_FILE


def notes_path(slug: str, base: Path | None = None) -> Path:
    return _dir(slug, base) / "reference" / NOTES_FILE


def archive(slug: str, entries: list, base: Path | None = None) -> None:
    """Append rules to the notes archive before they leave (or change in) the live file.
    entries: [{"reason", "rule", "id"?, "backstory"?, "now"?}]."""
    if not entries:
        return
    p = notes_path(slug, base)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="minutes")
    out = []
    for e in entries:
        out.append(f"## {stamp} · {e.get('reason', 'changed')}"
                   + (f" · {e['id']}" if e.get("id") else ""))
        out.append(f"Was: {e.get('rule', '').strip()}")
        if e.get("now"):
            out.append(f"Now: {e['now'].strip()}")
        if e.get("backstory"):
            out.append(f"Backstory: {e['backstory'].strip()}")
        out.append("")
    with open(p, "a", encoding="utf-8") as fh:
        if p.stat().st_size == 0:
            fh.write("# Rule notes - kept, never sent with messages\n\n"
                     "Every rule that was changed, tidied, removed or expired, word for word.\n\n")
        fh.write("\n".join(out) + "\n")


def split_tag(line: str) -> tuple[str, str | None, str]:
    """(tag, expires, text) for one rule line."""
    m = _TAG.match(line or "")
    if not m:
        return "", None, (line or "").strip()
    return m.group(0).strip(), m.group(2), line[m.end():].strip()


def guess_topic(text: str) -> str:
    t = (text or "").lower()
    for key, pat in _TOPIC_WORDS:
        if re.search(pat, t):
            return key
    return "other"


def fallback_summary(text: str, words: int = 16) -> str:
    first = re.split(r"(?<=[.!?])\s|:\s|\s-\s|;\s", (text or "").strip(), maxsplit=1)[0]
    w = first.split()
    return " ".join(w[:words]) + ("…" if len(w) > words else "")


def _active(slug: str, base: Path | None = None) -> list:
    """[(rid, entry, raw_line)] for every rule in the live file, in file order. Syncs the
    registry first so every line has an ID."""
    rr = _registry(base)
    p = _rules_path(slug, base)
    if not p.exists():
        return []
    reg = rr.sync(base or BASE, slug, write=True)["registry"]
    by_hash = {e.get("hash"): (rid, e) for rid, e in reg["rules"].items()
               if e.get("status") == "active" and e.get("file") == RULES_FILE}
    out = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        if not _TAG.match(raw):
            continue
        hit = by_hash.get(rr.content_hash(raw.strip()))
        if hit:
            out.append((hit[0], hit[1], raw.strip()))
    return out


def groups(slug: str, base: Path | None = None, today: date | None = None) -> dict:
    """The Your rules page: {"count", "groups": [{"topic", "label", "rules": [...]}],
    "ended": [...]}. A rule: {id, summary, text, expires, checked}."""
    today = today or date.today()
    by_topic = {k: [] for k in TOPIC_KEYS}
    for rid, e, raw in _active(slug, base):
        _tag, expires, text = split_tag(raw)
        topic = e.get("topic") if e.get("topic") in TOPIC_KEYS else guess_topic(text)
        summary = (e.get("summary") if e.get("summary_hash") == e.get("hash") else None) \
            or fallback_summary(text)
        by_topic[topic].append({"id": rid, "summary": summary, "text": text,
                                "expires": expires, "checked": e.get("confirmed")})
    out = [{"topic": k, "label": label, "rules": by_topic[k]}
           for k, label in TOPICS if by_topic[k]]
    return {"count": sum(len(g["rules"]) for g in out), "groups": out,
            "ended": recently_ended(slug, base, today)}


def recently_ended(slug: str, base: Path | None = None, today: date | None = None,
                   days: int = 30) -> list:
    """Rules that expired or were removed in the last `days`, newest first: [{when, rule}]."""
    today = today or date.today()
    p = notes_path(slug, base)
    if not p.exists():
        return []
    out, cur = [], None
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^## (\d{4}-\d{2}-\d{2})\S* · (expired|removed)\b", line)
        if m:
            cur = {"when": m.group(1), "reason": m.group(2)}
            continue
        if cur and line.startswith("Was: "):
            cur["rule"] = fallback_summary(split_tag(line[5:])[2])
            if (today - date.fromisoformat(cur["when"])).days <= days:
                out.append(cur)
            cur = None
    return list(reversed(out))[:10]


def _write_rules(slug: str, lines_out: list, base: Path | None, why: str) -> None:
    p = _rules_path(slug, base)
    bak = p.with_name(p.name + f".bak-{why}-{datetime.now():%Y%m%d%H%M%S}")
    shutil.copy2(p, bak)
    p.write_text("\n".join(lines_out) + "\n", encoding="utf-8")


def _find(slug: str, rid: str, base: Path | None):
    for r, e, raw in _active(slug, base):
        if r == rid:
            return e, raw
    raise LookupError("that rule isn't on file any more")


def _set(slug: str, rid: str, base: Path | None, **fields) -> None:
    rr = _registry(base)
    reg = rr.load_registry(base or BASE, slug)
    if rid in reg["rules"]:
        reg["rules"][rid].update(fields)
        p = rr.registry_path(base or BASE, slug)
        p.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def keep(slug: str, rid: str, base: Path | None = None, today: date | None = None) -> dict:
    _find(slug, rid, base)
    _set(slug, rid, base, confirmed=(today or date.today()).isoformat())
    return {"ok": True, "message": "Kept. I won't ask about that one for a while."}


def drop(slug: str, rid: str, base: Path | None = None, who: str = "athlete") -> dict:
    e, raw = _find(slug, rid, base)
    archive(slug, [{"reason": f"removed by {who}", "id": rid, "rule": raw}], base)
    p = _rules_path(slug, base)
    lines = p.read_text(encoding="utf-8").splitlines()
    out, gone = [], False
    for l in lines:
        if not gone and l.strip() == raw:
            gone = True
            continue
        out.append(l)
    _write_rules(slug, out, base, "rule-drop")
    _registry(base).sync(base or BASE, slug, write=True)
    return {"ok": True, "message": "Removed. I'll stop applying it."}


def change(slug: str, rid: str, text: str, base: Path | None = None,
           who: str = "athlete", today: date | None = None) -> dict:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) < 4:
        raise ValueError("write the rule as you want it")
    e, raw = _find(slug, rid, base)
    tag, _exp, _old = split_tag(raw)
    new_line = f"{tag or '[perm]'} {text}"
    archive(slug, [{"reason": f"changed by {who}", "id": rid, "rule": raw, "now": new_line}], base)
    rr = _registry(base)
    # Carry the ID over to the new wording (a reworded rule must keep its identity).
    reg = rr.load_registry(base or BASE, slug)
    if rid in reg["rules"]:
        reg["rules"][rid].update({"hash": rr.content_hash(new_line),
                                  "fingerprint": rr.fingerprint(new_line),
                                  "confirmed": (today or date.today()).isoformat(),
                                  "summary": None, "summary_hash": None})
        rr.registry_path(base or BASE, slug).write_text(
            json.dumps(reg, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    p = _rules_path(slug, base)
    out, done = [], False
    for l in p.read_text(encoding="utf-8").splitlines():
        if not done and l.strip() == raw:
            out.append(new_line)
            done = True
        else:
            out.append(l)
    _write_rules(slug, out, base, "rule-change")
    rr.sync(base or BASE, slug, write=True)
    return {"ok": True, "message": "Changed. That's the version I'll use from now on."}


def next_check(slug: str, base: Path | None = None, today: date | None = None) -> dict | None:
    """The single rule most worth asking "still right?" about this week, or None.

    Only [perm] rules on a topic that goes out of date (VOLATILE), that have stood at
    least NEW_RULE_GRACE_DAYS, and that have not been confirmed or asked about in the last
    CHECK_AFTER_DAYS. The longest-unchecked one wins. Dated ([expires:]) rules end on their
    own and are never asked about."""
    today = today or date.today()
    cut = (today - timedelta(days=CHECK_AFTER_DAYS)).isoformat()
    grace = (today - timedelta(days=NEW_RULE_GRACE_DAYS)).isoformat()
    best = None
    for rid, e, raw in _active(slug, base):
        tag, expires, text = split_tag(raw)
        if expires:
            continue
        topic = e.get("topic") if e.get("topic") in TOPIC_KEYS else guess_topic(text)
        if topic not in VOLATILE:
            continue
        if (e.get("first_seen") or "") > grace:
            continue
        last = max(e.get("confirmed") or "", e.get("asked") or "", e.get("first_seen") or "")
        if last > cut:
            continue
        if best is None or last < best[0]:
            summary = (e.get("summary") if e.get("summary_hash") == e.get("hash") else None) \
                or fallback_summary(text)
            best = (last, {"id": rid, "summary": summary, "text": text, "topic": topic})
    return best[1] if best else None


def mark_asked(slug: str, rid: str, base: Path | None = None, today: date | None = None) -> None:
    _set(slug, rid, base, asked=(today or date.today()).isoformat())
