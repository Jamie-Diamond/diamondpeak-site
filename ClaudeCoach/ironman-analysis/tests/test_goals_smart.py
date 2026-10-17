"""SMART goals (13 Oct 2026): a target and date for every goal but Keep my fitness, with
realistic ranges worked out from the athlete's start values."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import goals  # noqa: E402

SV = {"ctl": 63.5, "ftp": 270, "run_threshold": "4:30", "css": "1:35"}


def test_realistic_ranges_from_the_start_values():
    assert goals.realistic("ftp", SV, 12)["range"] == [278, 292]
    assert goals.realistic("run", SV, 12)["range"] == ["4:19", "4:25"]
    returning = goals.realistic("ftp", {"ctl": 9.3, "ftp": 212}, 12)["range"]
    assert returning[1] > 212 * 1.08                    # bigger gains coming back from a layoff


def test_targets_are_judged():
    assert goals.assess("run", "4:22", SV, 12) == "realistic"
    assert goals.assess("run", "4:00", SV, 12) == "unrealistic"
    assert goals.assess("run", "4:35", SV, 12) == "too easy"
    assert goals.assess("ftp", 296, SV, 12) == "stretch"
    assert goals.assess("ftp", 310, SV, 12) == "unrealistic"


def test_only_open_ended_goals_may_skip_a_target():
    assert goals.smart_missing({"type": "maintain"}) == []
    assert goals.smart_missing({"type": "run"}) == ["target", "date"]
    assert goals.smart_missing({"type": "run", "target": "5k in 21:30", "by": "2027-01-10"}) == []


def test_any_goal_may_be_open_ended_by_choice():
    """17 Oct 2026: Fred's FTP goal is "seeing how far he can get it with no target in mind"."""
    g = {"type": "ftp", "open_ended": True}
    assert goals.smart_missing(g) == []
    assert goals.smart_missing(dict(g, extras=[{"type": "run"}])) == [
        "Run faster target", "Run faster date"]               # extras still need theirs


def test_goal_setup_open_ended_keeps_the_block_and_a_target_ends_it(tmp_path):
    import json
    import plan_tools
    from datetime import date
    cfg = tmp_path / "athletes.json"
    g = {"type": "ftp", "set": "2026-10-01", "block_weeks": 4, "start": "2026-10-12",
         "start_values": {"ctl": 9.3, "ftp": 212}}
    cfg.write_text(json.dumps({"fred": {"goal": g}}))
    out = plan_tools.goal_setup("fred", "ftp", apply=True, path=cfg, ctl=10,
                                today=date(2026, 10, 17), open_ended=True)
    saved = json.loads(cfg.read_text())["fred"]["goal"]
    assert out["keeps_running_block"] and saved["open_ended"] and saved["start"] == "2026-10-12"
    assert out["smart"]["missing"] == []
    plan_tools.goal_setup("fred", "ftp", apply=True, path=cfg, ctl=10, today=date(2026, 10, 17),
                          target="FTP 240 W", target_value=240, by="2027-01-31")
    saved = json.loads(cfg.read_text())["fred"]["goal"]
    assert "open_ended" not in saved and saved["target"] == "FTP 240 W"


def test_main_plus_extras():
    """13 Oct 2026: James wants run speed, a higher FTP and to keep swimming. The main goal's
    sport gets the focus row, each extra the one-quality-session row, keep sports support."""
    g = {"type": "run", "extras": [{"type": "ftp", "target": "FTP 290 W"}, {"type": "run"},
                                   {"type": "maintain"}], "keep": ["swim"]}
    assert [e["type"] for e in goals.extras(g)] == ["ftp"]       # no duplicate or non-sport
    assert goals.label(g) == "Run faster + Raise my FTP"
    cfg = {"goal": g}
    dist = goals.distribution(cfg)
    assert dist["Run"] == goals._FOCUS_ROW["run"] and dist["Bike"] == goals._EXTRA_ROW["bike"]
    assert dist["Swim"] == goals._SUPPORT_ROW["swim"]
    assert goals.smart_missing(g) == ["target", "date", "Raise my FTP date"]
    assert "sweetspot" in goals.emphasis(g) and "threshold" in goals.emphasis(g)
