# attic

Retired scripts kept for reference, **not importable from `lib/`**.

Nothing in `lib/` or `scripts/` may import from here, and nothing here is on any cron. A
file lands in this directory when it has stopped being part of the system but is still
worth being able to read — the working record of how something was once done.

`sys.path` never includes this directory, so an `import` of anything in it fails outright
rather than resurrecting a one-off by accident. That is the point: every LLM job in this
codebase holds `Bash`, so an executable script sitting in `lib/` that writes a named
athlete's calendar is reachable whether or not anyone means it to be.

| File | Retired | Why |
|---|---|---|
| `push_kathryn_plan.py` | 13 Aug 2026 | A June 2026 one-off that wrote six hard-coded weeks straight to one named athlete's Intervals.icu calendar. Never on a cron, but it lived in `lib/` and was executable, so any LLM job with Bash could run it and overwrite six weeks of a real plan. Everything it did is now the Sunday build's job (`scripts/stage1-plan.py`), which is per-athlete, validated and honours the agreed week (`lib/agreed_week.py`). |
