#!/usr/bin/env python3
"""Publish the strength exercise library to the public surface for the app.

config/ is never served (_config.yml excludes it, deliberately); public/ is the one
sanctioned path, the same route publish-session-library.py uses. The exercise library
is shown to every athlete, so it must not name one: this refuses to publish if it does.

Run after any edit to config/exercise-library.json.
"""
import json
import pathlib
import re

BASE = pathlib.Path(__file__).resolve().parent.parent
SRC = BASE / "config" / "exercise-library.json"
DST = BASE / "public" / "exercise-library.json"
NAMES = re.compile(r"\b(Jamie|Kathryn|Calum|Fred|James)\b")


def main() -> int:
    d = json.loads(SRC.read_text())
    leaked = sorted(set(NAMES.findall(json.dumps(d))))
    if leaked:
        print("refusing to publish: athlete names in the library:", leaked)
        return 1
    d["_published"] = "Copy of config/exercise-library.json for the app."
    DST.write_text(json.dumps(d, indent=1, ensure_ascii=False) + "\n")
    print(f"published {DST.relative_to(BASE)}: {len(d['exercises'])} exercises, "
          f"{len(d.get('example_sessions', []))} suggested sessions, {DST.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
