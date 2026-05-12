#!/bin/bash
# Weekly summary — runs via VM crontab at 20:00 every Sunday.
# Delegates to weekly-summary.py which fetches IcuSync data directly (no MCP).
# Safe to run manually: bash weekly-summary.sh

cd /Users/diamondpeakconsulting/diamondpeak-site
exec python3 ClaudeCoach/scripts/weekly-summary.py --athlete jamie
