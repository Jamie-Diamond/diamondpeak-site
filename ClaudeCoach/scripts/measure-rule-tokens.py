#!/usr/bin/env python3
"""Measure the REAL bytes-per-token ratio of the rule surface via count_tokens.

Run on the VM under cc-run so CLAUDE_CODE_OAUTH_TOKEN is present. Never use tiktoken:
it is OpenAI's tokenizer and undercounts Claude by 15-20% on prose, more on numbers.
"""
import json
import os
import pathlib
import urllib.request

BASE = pathlib.Path("/Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach")
PARTS = {
    "jamie_rules": BASE / "athletes/jamie/persistent-rules.md",
    "shared_rules": BASE / "athletes/_shared/persistent-rules.md",
    "system_prompt": BASE / "athletes/jamie/system_prompt.txt",
}


def count(text: str, tok: str) -> int | None:
    body = json.dumps({
        "model": "claude-opus-5",
        "messages": [{"role": "user", "content": text}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages/count_tokens",
        data=body,
        headers={
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "Authorization": "Bearer " + tok,
        },
    )
    try:
        return json.loads(urllib.request.urlopen(req, timeout=60).read())["input_tokens"]
    except Exception as exc:
        print("  count_tokens failed: " + str(exc)[:120])
        return None


def main() -> int:
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
    if not tok:
        print("no CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY in env - run under cc-run")
        return 2
    tb = tt = 0
    for name, p in PARTS.items():
        if not p.exists():
            print(name + ": missing")
            continue
        text = p.read_text()
        n = count(text, tok)
        if n is None:
            continue
        b = len(text)
        tb += b
        tt += n
        print(f"{name:14s} {b:6d} bytes  {n:6d} tokens  {b / n:.2f} bytes/token")
    if tt:
        ratio = tb / tt
        print(f"{'COMBINED':14s} {tb:6d} bytes  {tt:6d} tokens  {ratio:.2f} bytes/token")
        print()
        print(f"the current 41,000-BYTE budget == ~{int(41000 / ratio)} tokens")
        print(f"a 10,000-TOKEN budget == ~{int(10000 * ratio)} bytes")
        print(f"the 400-byte per-rule cap == ~{int(400 / ratio)} tokens per rule")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
