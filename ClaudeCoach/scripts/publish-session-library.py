#!/usr/bin/env python3
"""Publish a shareable copy of the session library to the public surface.

config/session-library.json cannot be served: _config.yml excludes every ClaudeCoach
subdirectory except public/, deliberately, so athlete config can never reach the public
site. public/ is the one sanctioned path.

This is not just a copy. The library's coaching notes were written referring to one
athlete by name ("Jamie's measured brick-run value", "Jamie's 40-min run minimum"), and
the app shows the same library to all three - so those notes were factually wrong for
Kathryn and Calum before they were a privacy question. Names are generalised on the way
out, which fixes both.

Run after any edit to config/session-library.json; safe to run from the nightly refresh.
"""
import json
import re
import pathlib

BASE = pathlib.Path(__file__).resolve().parent.parent
SRC = BASE / "config" / "session-library.json"
DST = BASE / "public" / "session-library.json"

NAMES = ("Jamie's", "Jamie’s", "Kathryn's", "Kathryn’s", "Calum's", "Calum’s")


def generalise(text: str) -> str:
    for n in NAMES:
        text = text.replace(n, "the athlete's")
    return re.sub(r"\b(Jamie|Kathryn|Calum)\b", "the athlete", text)


def walk(node):
    if isinstance(node, dict):
        return {k: walk(v) for k, v in node.items()}
    if isinstance(node, list):
        return [walk(v) for v in node]
    if isinstance(node, str):
        return generalise(node)
    return node


def main() -> int:
    d = json.loads(SRC.read_text())
    out = walk(d)
    out["_published"] = ("Generalised copy of config/session-library.json for the app. "
                         "Athlete names removed: this library is shown to every athlete.")
    DST.write_text(json.dumps(out, indent=1) + "\n")

    types = sum(len([t for t in v if not t.startswith("_")])
                for v in out["session_types"].values())
    leaked = [n for n in ("Jamie", "Kathryn", "Calum") if n in DST.read_text()]
    print(f"published {DST.relative_to(BASE)}: {types} session types, "
          f"{len(out.get('drills', {}))} drills, {DST.stat().st_size} bytes")
    print("athlete names remaining:", leaked or "none")
    return 1 if leaked else 0


if __name__ == "__main__":
    raise SystemExit(main())
