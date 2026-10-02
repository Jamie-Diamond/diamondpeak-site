"""One coach manual for everyone, a short brief per athlete (Jamie, 2 Oct 2026).

Pinned: the brief says who the athlete is (race or goal, background, injuries, hours,
server-side notes) and keeps its AUTO-SYNC block; the manual is put in front of a new-style
brief with the athlete's name and slug, and never doubled onto an old full brief.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import athlete_brief as ab  # noqa: E402


def test_render_goal_athlete_with_an_injury():
    out = ab.render("Fred Hayes",
                    {"experience": "8 years, half Ironman", "max_hours_per_week": 5,
                     "injuries": [{"description": "Recovering from ACL surgery", "location": "knee",
                                   "protocol": "one hamstring + one quad session a week"}]},
                    {"goal": {"type": "ftp"}})
    assert out.splitlines()[:5] == [
        "About Fred Hayes:", "- No race booked. Goal: Raise my FTP.",
        "- Background: 8 years, half Ironman",
        "- Injuries / constraints: Recovering from ACL surgery (knee) - one hamstring + one quad session a week",
        "- Max training: 5 h/week"]


def test_render_race_athlete_and_a_no_injury():
    out = ab.render("Sam", {"a_goal": "sub 5:30", "race_name": "70.3 Test", "race_date": "2099-06-01",
                            "injuries": [{"description": "No"}]},
                    {"race_name": "70.3 Test", "race_date": "2099-06-01"})
    assert "- Race: 70.3 Test, 2099-06-01. Goal: sub 5:30." in out
    assert "- Injuries / constraints: none" in out


def test_rebuild_keeps_autosync_and_notes_and_backs_up(tmp_path):
    d = tmp_path / "athletes" / "tess"
    (d / "reference").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "athletes.json").write_text(json.dumps({"tess": {"goal": {"type": "fitter"}}}))
    (d / "profile.json").write_text(json.dumps({"name": "Tess T", "max_hours_per_week": 6}))
    (d / "reference" / "brief-notes.md").write_text("Prefers mornings.")
    (d / "system_prompt.txt").write_text(
        "You are ClaudeCoach, a long old brief...\n\n### AUTO-SYNC: live config ###\n- Race: none\n"
        "### AUTO-SYNC: end ###\n")
    r = ab.rebuild("tess", tmp_path)
    new = (d / "system_prompt.txt").read_text()
    assert new.startswith("About Tess T:") and "Prefers mornings." in new
    assert new.rstrip().endswith("### AUTO-SYNC: end ###") and "- Race: none" in new
    assert list(d.glob("system_prompt.txt.bak-brief-*")) and r["after_bytes"] < 400


def test_manual_names_the_athlete_and_keeps_chart_json():
    m = ab.manual("Fred", "fred")
    assert m.startswith("You are ClaudeCoach, Fred's coach in Peak")
    assert "--athlete fred --endpoint" in m and "$name" not in m and "$slug" not in m
    assert '<<<CHART:load:{"today":"MM-DD"' in m


def test_engine_puts_the_manual_in_front_of_a_new_brief_only(tmp_path):
    import engine
    d = tmp_path / "athletes" / "tess"
    d.mkdir(parents=True)
    (d / "profile.json").write_text(json.dumps({"name": "Tess T"}))
    sp = d / "system_prompt.txt"
    sp.write_text("About Tess T:\n- No race booked.\n")
    text = engine.system_prompt_with_level(sp)
    assert text.startswith("You are ClaudeCoach, Tess's coach in Peak")
    assert "About Tess T:" in text and "--athlete tess" in text
    sp.write_text("You are ClaudeCoach, Tess's old full brief.\n")
    text = engine.system_prompt_with_level(sp)
    assert text.count("You are ClaudeCoach") == 1          # never doubled onto an old brief


def test_a_past_race_reads_as_last_race_and_a_stale_goal_is_dropped():
    out = ab.render("Kat", {"a_goal": "Sub 5:30"}, {"race_name": "70.3 Old", "race_date": "2020-09-20"})
    assert "- No race booked (last race: 70.3 Old, 2020-09-20)." in out
    out = ab.render("Jo", {"a_goal": "Sub 9:30", "race_name": "Ironman X", "race_date": "2020-09-19"},
                    {"race_name": "Brighton Marathon", "race_date": "2099-04-04"})
    assert "Brighton Marathon, 2099-04-04" in out and "9:30" not in out
