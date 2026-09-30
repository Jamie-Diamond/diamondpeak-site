#!/usr/bin/env python3
"""Render config/event-levels.json into blueprints/blueprint.md §4.5.

The JSON is the single source (lib/race_fitness.py reads it); the blueprint shows it.
The section sits between the two markers below and is regenerated, never hand-edited.
ironman-analysis/tests/test_event_levels.py fails if the two drift.

    python3 scripts/render-event-levels.py            # print the section
    python3 scripts/render-event-levels.py --write    # rewrite it in blueprint.md
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent            # ClaudeCoach/
LEVELS = BASE / "config" / "event-levels.json"
DOC = BASE / "blueprints" / "blueprint.md"
START, END = "<!-- event-levels:start -->", "<!-- event-levels:end -->"
FIT = {"run": "Running Fitness", "tri": "Total Fitness", "bike": "Total Fitness"}


def _rng(v, unit=""):
    lo, hi = v
    fmt = (lambda x: f"{x:g}")
    return f"{fmt(lo)}–{fmt(hi)}{unit}"


def render() -> str:
    data = json.loads(LEVELS.read_text())["events"]
    out = [START, ""]
    for key, ev in data.items():
        kind = ev["kind"]
        vol = "Run km/week" if kind == "run" else "Hours/week"
        cols = ["Level", "Goal", vol]
        if kind in ("tri", "bike"):
            cols.append("Long ride")
        if kind in ("run", "tri"):
            cols.append("Long run")
        cols.append(f"{FIT[kind]} into taper")
        out += [f"**{ev['label']}** (taper {_rng(ev['taper_days'])} days)", "",
                "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        for lv in ev["levels"]:
            row = [str(lv["level"]), lv["label"],
                   _rng(lv["run_km"]) if kind == "run" else _rng(lv["hours"])]
            if kind in ("tri", "bike"):
                row.append(_rng(lv["long_ride_h"], " h"))
            if kind in ("run", "tri"):
                row.append(_rng(lv["long_run_km"], " km"))
            row.append(_rng(lv["fitness_at_taper"]))
            out.append("| " + " | ".join(row) + " |")
        out.append("")
    out.append(END)
    return "\n".join(out)


def main():
    section = render()
    if "--write" not in sys.argv:
        print(section)
        return
    doc = DOC.read_text()
    if START not in doc or END not in doc:
        raise SystemExit(f"{DOC} has no {START} / {END} markers")
    head, rest = doc.split(START, 1)
    _, tail = rest.split(END, 1)
    DOC.write_text(head + section + tail)
    print(f"wrote §4.5 into {DOC}")


if __name__ == "__main__":
    main()
