# ClaudeCoach bot latency — phase 2 plan (Agent SDK migration)

Status: phases 1–3 DEPLOYED 2 Jul 2026 (commit c48efcf, live on VM). This doc
covers what shipped, how to read the new data, and the remaining phase: moving
`lib/engine.py` off the `claude -p` subprocess onto the Claude Agent SDK.

## Background

The June 2026 optimisation round (concurrent poll loop, offloaded Whisper,
prefetch cache, history trim) targeted cross-athlete blocking and small fixed
overheads. Single-message latency barely moved because it is dominated by:

1. a cold `claude` CLI process per message (Node boot + harness init),
2. zero prompt caching — `--no-session-persistence` meant the full prompt
   (~25–35KB: Claude Code system prompt, athlete system prompt, rules,
   prefetch block, 12 history pairs) was re-ingested uncached every reply,
3. Opus 4.8 generation + tool round-trips.

## What shipped 2 Jul (phases 1–3)

- **Timing instrumentation** — every reply logs a line to `bot.log`:
  `[engine] [timing] stream model=… session=new|resume|stateless boot=X s
  first_text=Y s total=Z s`. boot = spawn→first stream event (CLI startup);
  first_text = spawn→first visible token (startup + ingest + thinking);
  total = whole generation.
- **Sonnet 5 default** — `select_model` now returns `claude-sonnet-5` unless
  the message/thread matches `_HARD_RE` (planning topics), which stays on
  Opus 4.8. Footer label: `S5` vs `O`. The old "Sonnet makes mistakes" verdict
  was about Sonnet 4.6; this is a trial — revert by flipping `select_model`'s
  final return back to `MODEL_OPUS`. A rate-limit guard retries once on Opus
  if the Sonnet bucket caps (June 17 incident class).
- **Per-athlete session resume** — `engine.py` keeps one persisted CLI session
  per athlete (`athletes/<slug>/.chat_session.json`), resumed with
  `--resume <id>`. Follow-ups send only the live-context block + new message;
  the session carries system prompt + conversation, so rapid exchanges hit the
  server-side prompt cache. Rotation: 30 turns or 24h; invalidated when the
  system prompt / rules / coaching level change (fingerprint); exchanges made
  outside the session (voice fast-paths, buttons) are injected as a catch-up
  block on the next resume. Any resume failure falls back to a fresh
  full-prompt session. Opt-out: `"session_resume": false` in
  `telegram/config.json`.

Local measurements (Mac, toy prompt): fresh session ~3.4–47s (tool-using),
resumed streaming reply boot 0.4s / first text 2.9s / total 4.1s.

## Review checkpoint (~9 Jul)

After a week, pull the timing lines:

```bash
ssh root@178.105.95.208 "grep '\[timing\]' /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/bot.log | tail -200"
```

Questions to answer before phase 4:
1. What does `boot` average? That is exactly what the SDK migration removes —
   if it's under ~2s, the migration is low value.
2. `session=resume` share of replies, and resume vs new `first_text` delta —
   confirms the prompt-cache win is real in production.
3. Sonnet 5 quality: any athlete-visible mistakes on `S5`-footered replies?
   Any planning messages that should have routed to Opus but didn't?
4. Session hygiene: `~/.claude/projects/-Users-diamondpeakconsulting-diamondpeak-site/`
   on the VM accumulates session JSONL files (30-turn cap each). Add a weekly
   `find … -mtime +14 -delete` cron if growth is noticeable.

## Phase 4 — Agent SDK migration (engine.py only)

Goal: eliminate the per-message CLI boot entirely by replacing the
`subprocess claude -p` calls in `lib/engine.py` with the **Claude Agent SDK**
(`pip install claude-agent-sdk` on the VM — note the `anthropic` package is
deliberately NOT installed there; the Agent SDK is a separate decision and
authenticates via the same `CLAUDE_CODE_OAUTH_TOKEN` in `/root/.claude/cc.env`).

Scope — deliberately identical seams to today:
- `call_claude`, `stream_claude`, `call_claude_with_image` keep their
  signatures; only the transport under them changes.
- The SDK is Claude Code as a library: same tools (Read/Write/Edit/Bash), same
  CLAUDE.md loading from `project_dir`, same permissions model — so coaching
  behaviour should carry over, unlike a raw Messages-API rewrite.
- Session-per-athlete maps onto SDK client sessions; keep the same rotation +
  fingerprint logic.
- Feature-gate it (`"engine_sdk": true` in config.json, default off) and
  deploy dark, USE_WORKBOOK-style; flip per-athlete once verified.

Risks / checks:
- SDK availability + version on the VM's Python 3 (check `python3 --version`).
- The bot's thread-pool workers each need an event loop or a sync wrapper
  (SDK is async-first).
- Verify tool permissions default sanely headless (the CLI path relies on
  `--allowedTools`).
- Rollback = flip the config flag; the subprocess path stays in the file.

Decision rule: if the timing data shows `boot` + (new-session `first_text` −
resume `first_text`) is a small share of typical totals, skip phase 4 — the
remaining time is model generation, and the lever is model/routing, not
transport.
