"""Smart rule management (Jamie, 2 Oct 2026): athletes see their rules, the system keeps
them short, and asks about one at most a week.

Pinned: the Your rules page (topics, one line, dated rules), keep / change / drop with
the old wording archived word for word, the weekly pick (volatile topic, not new, not
recently checked, never a dated rule), the nightly tidy (only on a verified shortening,
keeps the confirmation lock, keeps the rule's ID) and replace-not-stack in live chat.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import athlete_rules as ar  # noqa: E402
import rule_registry as rr  # noqa: E402
import rule_tidy  # noqa: E402

TODAY = date(2026, 10, 2)
RULES = """# Persistent rules

[perm] Weekday rides: 3 hours a week, one ~2h and one ~1h ride, no fixed days because work is unpredictable.
[perm] Electrolytes: a tab in every bottle on long rides; never plain water bottles on hot days.
[perm] Debriefs: keep them to three lines and lead with the one thing that matters.
[expires:2026-10-06] Ramp test Tue 6 Oct on Zwift in ERG mode, one device only.
"""


def _base(tmp_path, rules=RULES):
    d = tmp_path / "athletes" / "tess"
    (d / "reference").mkdir(parents=True)
    (d / "persistent-rules.md").write_text(rules)
    return tmp_path


def _ids(base):
    return {e["summary"] if False else ar.split_tag(raw)[2][:12]: rid
            for rid, e, raw in ar._active("tess", base)}


def test_groups_by_topic_with_one_line_each(tmp_path):
    base = _base(tmp_path)
    g = ar.groups("tess", base, TODAY)
    assert g["count"] == 4
    labels = [x["label"] for x in g["groups"]]
    assert labels == ["When you train", "Training", "Fuelling", "How I talk to you"]
    ramp = [r for x in g["groups"] for r in x["rules"] if r["expires"]][0]
    assert ramp["expires"] == "2026-10-06" and ramp["summary"].startswith("Ramp test Tue 6 Oct")


def test_change_replaces_keeps_the_id_and_archives_the_old_wording(tmp_path):
    base = _base(tmp_path)
    rid = _ids(base)["Weekday ride"]
    ar.change("tess", rid, "Weekday rides: 4 hours a week, two 2h rides.", base, today=TODAY)
    text = (base / "athletes/tess/persistent-rules.md").read_text()
    assert "[perm] Weekday rides: 4 hours a week, two 2h rides." in text and "3 hours" not in text
    assert _ids(base)["Weekday ride"] == rid                       # same rule, new wording
    notes = ar.notes_path("tess", base).read_text()
    assert "changed by athlete" in notes and "Was: [perm] Weekday rides: 3 hours" in notes


def test_drop_archives_then_removes_and_shows_as_ended(tmp_path):
    base = _base(tmp_path)
    rid = _ids(base)["Debriefs: ke"]
    ar.drop("tess", rid, base)
    assert "Debriefs" not in (base / "athletes/tess/persistent-rules.md").read_text()
    assert "removed by athlete" in ar.notes_path("tess", base).read_text()
    assert ar.groups("tess", base, TODAY)["ended"][0]["reason"] == "removed"


def test_keep_sets_the_check_date_and_unknown_ids_are_refused(tmp_path):
    base = _base(tmp_path)
    rid = _ids(base)["Electrolytes"]
    ar.keep("tess", rid, base, today=TODAY)
    assert rr.load_registry(base, "tess")["rules"][rid]["confirmed"] == "2026-10-02"
    with pytest.raises(LookupError):
        ar.keep("tess", "tess-999", base)


def _age_all(base, first_seen):
    reg = rr.load_registry(base, "tess")
    for e in reg["rules"].values():
        e["first_seen"] = first_seen
    rr.registry_path(base, "tess").write_text(json.dumps(reg))


def test_weekly_pick_one_volatile_unchecked_rule_never_a_dated_one(tmp_path):
    base = _base(tmp_path)
    ar._active("tess", base)                                   # mint IDs
    assert ar.next_check("tess", base, TODAY) is None          # everything is brand new
    _age_all(base, "2026-07-01")
    c = ar.next_check("tess", base, TODAY)
    assert c and c["topic"] in ar.VOLATILE and "Ramp test" not in c["text"]
    ar.mark_asked("tess", c["id"], base, TODAY)
    c2 = ar.next_check("tess", base, TODAY)
    assert c2 and c2["id"] != c["id"]                          # asked one waits 8 weeks
    ar.keep("tess", c2["id"], base, today=TODAY)
    assert ar.next_check("tess", base, TODAY) is None          # style rules are never asked


def test_rule_check_skips_tracking_only_and_opted_out(tmp_path, monkeypatch):
    base = _base(tmp_path)
    ar._active("tess", base)
    _age_all(base, "2026-07-01")
    (base / "config").mkdir()
    spec = importlib.util.spec_from_file_location("rule_check", REPO / "scripts" / "rule-check.py")
    rc_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rc_mod)
    import planning_pause
    monkeypatch.setattr(planning_pause, "is_paused", lambda slug, cfg=None, path=None: bool(cfg.get("planning_paused")))
    for cfg, n in (({"active": True, "chat_id": "web-t"}, 1),
                   ({"active": True, "chat_id": "web-t", "planning_paused": True}, 0),
                   ({"active": True, "chat_id": "web-t", "rule_check": False}, 0)):
        (base / "config" / "athletes.json").write_text(json.dumps({"tess": cfg}))
        assert len(rc_mod.run(dry_run=True, base=base)) == n
    text, markup = rc_mod.message({"callback": "tess:tess-001", "summary": "Weekday rides: 3h"})
    assert [b["callback_data"] for b in markup["inline_keyboard"][0]] == \
        ["rule:keep:tess:tess-001", "rule:change:tess:tess-001", "rule:drop:tess:tess-001"]


# ── nightly tidy ───────────────────────────────────────────────────────────────

LONG = ("[perm] Electrolytes on long rides: put a tab in EVERY bottle, not just some - plain-water "
        "bottles diluted sodium and were the confirmed cause of severe cramp on the Sembrancher "
        "ride (19 Jul 2026); total fluid volume was not the issue. Fluid target about 700-750ml an "
        "hour on hot days. RACE-VALIDATED 29 Aug 2026 at Tour de Stations: a tab in every bottle "
        "all day produced zero cramp. Confirmed by Calum 30 Aug 2026.")


def _fake(responses):
    calls = []

    def llm(prompt, label):
        calls.append(prompt)
        return responses.pop(0)
    llm.calls = calls
    return llm


def test_tidy_shortens_keeps_lock_id_and_archives(tmp_path):
    base = _base(tmp_path, "# r\n\n" + LONG + "\n")
    rid = next(iter(_ids(base).values()))
    llm = _fake([json.dumps({"rule": "Electrolytes: a tab in every bottle on long rides; about "
                                     "700-750ml an hour on hot days.",
                             "backstory": "Cramp at Sembrancher 19 Jul; zero cramp at Tour de Stations."}),
                 json.dumps({"ok": True, "missing": []})])
    out = rule_tidy.tidy("tess", base, llm=llm)
    assert [t["id"] for t in out["tidied"]] == [rid]
    line = (base / "athletes/tess/persistent-rules.md").read_text().splitlines()[-1]
    assert line.startswith("[perm] Electrolytes: a tab in every bottle")
    assert "Confirmed by Calum 30 Aug 2026" in line                 # still locked
    assert next(iter(_ids(base).values())) == rid                   # same ID
    notes = ar.notes_path("tess", base).read_text()
    assert "tidied" in notes and "Sembrancher ride (19 Jul 2026)" in notes and "Backstory:" in notes


def test_tidy_leaves_the_rule_when_the_check_finds_something_missing(tmp_path):
    base = _base(tmp_path, "# r\n\n" + LONG + "\n")
    llm = _fake([json.dumps({"rule": "Electrolytes: a tab in every bottle.", "backstory": "x"}),
                 json.dumps({"ok": False, "missing": ["700-750ml an hour on hot days"]})])
    out = rule_tidy.tidy("tess", base, llm=llm)
    assert out["tidied"] == [] and len(out["skipped"]) == 1
    assert (base / "athletes/tess/persistent-rules.md").read_text().strip().endswith(
        "Confirmed by Calum 30 Aug 2026.")


def test_every_rule_is_reviewed_once_whatever_its_length(tmp_path):
    base = _base(tmp_path)
    # Already all instruction: the model hands each rule back unchanged with no backstory.
    unchanged = [json.dumps({"rule": ar.split_tag(raw)[2], "backstory": ""})
                 for _r, _e, raw in sorted(ar._active("tess", base), key=lambda x: -len(x[2]))]
    llm = _fake(unchanged)
    out = rule_tidy.tidy("tess", base, llm=llm)
    assert out["tidied"] == [] and len(out["skipped"]) == 4 and len(llm.calls) == 4
    before = (base / "athletes/tess/persistent-rules.md").read_text()
    llm2 = _fake([])
    assert rule_tidy.tidy("tess", base, llm=llm2)["skipped"] == [] and llm2.calls == []
    assert (base / "athletes/tess/persistent-rules.md").read_text() == before


def test_digest_stores_topic_and_summary_against_the_current_wording(tmp_path):
    base = _base(tmp_path)
    ids = _ids(base)
    llm = _fake([json.dumps([{"id": ids["Weekday ride"], "topic": "schedule",
                              "summary": "Weekday rides: about 3 hours, no fixed days"}])])
    assert rule_tidy.digest("tess", base, llm=llm) == 1
    g = ar.groups("tess", base, TODAY)
    sched = [r for x in g["groups"] if x["topic"] == "schedule" for r in x["rules"]][0]
    assert sched["summary"] == "Weekday rides: about 3 hours, no fixed days"


# ── replace, not stack (live chat) ─────────────────────────────────────────────

def test_live_chat_may_replace_a_rule_and_the_old_one_is_archived(tmp_path, monkeypatch):
    import rules_capture as rc
    base = _base(tmp_path)
    monkeypatch.setattr(ar, "BASE", base)
    monkeypatch.setattr(rc.bug_fixer, "_surface_bytes", lambda slug, athlete_rules=None: 0)
    before = (base / "athletes/tess/persistent-rules.md").read_text()
    after = before.replace(
        "[perm] Weekday rides: 3 hours a week, one ~2h and one ~1h ride, no fixed days because work is unpredictable.",
        "[perm] Weekday rides: 5 hours a week, one ~3h and one ~2h ride, no fixed days because work is unpredictable.")
    # The silent hourly path may not change a figure...
    text, drops = rc.enforce_rule_guards(before, after, [], slug="tess")
    assert text == before and drops and drops[0][0].startswith("ABORT")
    # ...live chat, where the athlete just said it changed, may.
    text, drops = rc.enforce_rule_guards(before, after, [], slug="tess", allow_supersede=True)
    assert text == after and drops == []
    notes = ar.notes_path("tess", base).read_text()
    assert "superseded" in notes and "Was: [perm] Weekday rides: 3 hours" in notes


def test_an_unrelated_rewrite_is_still_refused(tmp_path, monkeypatch):
    import rules_capture as rc
    base = _base(tmp_path)
    monkeypatch.setattr(ar, "BASE", base)
    monkeypatch.setattr(rc.bug_fixer, "_surface_bytes", lambda slug, athlete_rules=None: 0)
    before = (base / "athletes/tess/persistent-rules.md").read_text()
    after = before.replace(
        "[perm] Electrolytes: a tab in every bottle on long rides; never plain water bottles on hot days.",
        "[perm] Swim on Tuesdays only.")
    text, drops = rc.enforce_rule_guards(before, after, [], slug="tess", allow_supersede=True)
    assert text == before and drops[0][0].startswith("ABORT")


# ── sweep: general methods move to the shared rules ────────────────────────────

def test_sweep_moves_general_methods_merges_and_keeps_personal(tmp_path):
    base = _base(tmp_path, "# r\n\n"
                 "[perm] Jamie's Edge 830 altitude is never used for gradient work.\n"
                 "[perm] When Jamie gives a bare number, check which field and unit it means before writing it.\n"
                 "[perm] When Jamie names a B-race, keep the long ride the day before.\n"
                 "[perm] Debrief tone: lead with the one thing that matters to Jamie.\n")
    sd = base / "athletes" / "_shared"
    sd.mkdir(parents=True)
    (sd / "persistent-rules.md").write_text("# shared\n\n[perm] Debriefs: three lines at most.\n")
    mine = {ar.split_tag(raw)[2][:12]: rid for rid, _e, raw in ar._active("tess", base)}
    shared_id = next(rid for rid, _e, _r in ar._active("_shared", base))
    answer = json.dumps([
        {"id": mine["Jamie's Edge"], "scope": "personal"},
        {"id": mine["When Jamie g"], "scope": "general", "merge_into": None,
         "text": "When the athlete gives a bare number, check which field and unit it means before writing it."},
        {"id": mine["When Jamie n"], "scope": "general", "merge_into": None,
         "text": "Before a B-race, cut the long ride."},
        {"id": mine["Debrief tone"], "scope": "general", "merge_into": shared_id,
         "merged": "Debriefs: three lines at most, leading with the one thing that matters."},
    ])
    # sweep answer, then checks in order: B-race (fails), bare number (ok), debrief merge (ok)
    llm = _fake([answer, json.dumps({"ok": True}), json.dumps({"ok": False, "missing": ["keep the long ride"]}),
                 json.dumps({"ok": True})])
    out = rule_tidy.sweep_general("tess", base, llm=llm)
    assert len(out["moved"]) == 1 and list(out["merged"]) == [shared_id]
    assert out["kept_personal_on_check"] == [mine["When Jamie n"]]
    t = (base / "athletes/tess/persistent-rules.md").read_text()
    assert "Edge 830" in t and "B-race" in t and "bare number" not in t and "Debrief tone" not in t
    sh = (base / "athletes/_shared/persistent-rules.md").read_text()
    assert "[perm] When the athlete gives a bare number" in sh
    assert "Debriefs: three lines at most, leading with the one thing that matters." in sh
    assert "three lines at most.\n" not in sh.replace("matters.\n", "")
    assert "moved to shared rules" in ar.notes_path("tess", base).read_text()
    assert "merged with a rule from tess" in ar.notes_path("_shared", base).read_text()
