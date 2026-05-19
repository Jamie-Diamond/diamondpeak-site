# Bot Response Speed — Direct SDK Migration Plan

## Problem
Every bot response spawns a new `claude` CLI process (Node.js init, auth check, plugin sync etc.) — ~2–3s overhead before inference starts, on every message.

## Solution
Replace `subprocess([CLAUDE_BIN, "-p", ...])` calls in `bot.py` with direct `anthropic` Python SDK calls. No process spawn, no startup cost. Response starts arriving in ~200ms.

## Prerequisites
- Anthropic API key (`sk-ant-api03-...`) on the VM, set as `ANTHROPIC_API_KEY` env var
- `pip install anthropic` on VM (or add to requirements.txt)
- The VM's Claude Max subscription gives API access — user needs to generate a key at console.anthropic.com

## What changes in bot.py
1. Replace `call_claude()` with `anthropic.Anthropic().messages.create()`
2. Replace `call_claude_streaming()` with streaming SDK calls (`stream=True`)
3. Tool calls (Read/Write/Bash) would need to be handled by the Python code rather than the CLI — this is the main complexity
4. `call_claude_with_image()` becomes a messages call with an `image` content block (base64 JPEG)

## Complexity
The CLI handles tool use automatically (Read, Write, Bash). The SDK requires implementing a tool-use loop in Python. Moderate effort — ~half a day.

## Decision gate
Worth doing if response latency is still frustrating after streaming deploy. Streaming already eliminates the perceived wait; this eliminates the actual wait.

## Estimated impact
- Current: ~3s startup + inference time (~5–15s total)
- After SDK: ~0.2s to first token + inference time (~3–12s total)
