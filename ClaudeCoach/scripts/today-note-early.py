#!/usr/bin/env python3
"""00:15: write each athlete's Today note from the plan and load, before any wellness
exists (lib/today_note.py). The morning runs of morning-checkin.py rewrite it once sleep /
HRV / resting HR land. Its own script so each cron job has one schedule (ops-digest
derives due times from it)."""
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "morning_checkin", Path(__file__).resolve().parent / "morning-checkin.py")
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)
sys.argv = [sys.argv[0], "--early"]
mc.main()
