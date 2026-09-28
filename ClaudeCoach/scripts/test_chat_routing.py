#!/usr/bin/env python3
"""Offline tests for chat model routing (telegram/bot.py select_model, 28 Sep 2026).
Run: python3 ClaudeCoach/scripts/test_chat_routing.py

Jamie's rule: Sonnet by default, Opus for planning; prompting is the first response to a
Sonnet quality problem and Opus the last resort. Guards both directions: everyday chat
must not drift back onto Opus (the cost), and planning / pushback must never be answered
by Sonnet (the July quality problem)."""
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here.parent / "lib"))
sys.path.insert(0, str(_here.parent / "telegram"))
import bot as B   # noqa: E402

CASES = [
    ("how am I looking?", [], B.MODEL_SONNET),
    ("thanks", [], B.MODEL_SONNET),
    ("what's today's session?", [], B.MODEL_SONNET),
    ("why was my HR high on that run?", [], B.MODEL_SONNET),
    ("82.5 kg", [], B.MODEL_SONNET),
    ("can you move Saturday's long ride to Sunday", [], B.MODEL_OPUS),
    ("build me a plan for the off-season", [], B.MODEL_OPUS),
    ("what pacing should I use for the half?", [], B.MODEL_OPUS),
    ("I need to skip tomorrow", [], B.MODEL_OPUS),
    ("that's wrong, I did 12k not 10k", [], B.MODEL_OPUS),
    ("I already told you my RPE", [], B.MODEL_OPUS),
    ("are you sure?", [], B.MODEL_OPUS),
    ("opus: why was Tuesday so hard?", [], B.MODEL_OPUS),
    ("ok", [{"user": "replan my week please"}], B.MODEL_OPUS),            # planning thread sticks
    ("ok", [{"user": "that's wrong"}], B.MODEL_OPUS),                     # pushback sticks
    ("ok", [{"user": "replan my week"}, {"user": "thanks"}, {"user": "great"},
            {"user": "cool"}], B.MODEL_SONNET),                            # ...for 3 messages
    ("ok", [{"user": "hi", "assistant": "Your plan for next week is built."}], B.MODEL_SONNET),
]
fails = [(t, B.select_model(t, h), w) for t, h, w in CASES if B.select_model(t, h) != w]
for t, got, want in fails:
    print(f"FAIL {t!r}: got {got}, want {want}")
print("ALL PASS" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
