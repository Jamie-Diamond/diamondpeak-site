#!/usr/bin/env python3
"""What ClaudeCoach would cost on the pay-per-use Claude API, per athlete per month.

Why (27 Sep 2026): the VM runs on Jamie's subscription today. Before paying users he
needs to know whether the product is economically viable on API pricing, from real
usage rather than a guess.

Source: the Claude CLI's own session transcripts on the VM
(/root/.claude/projects/*/*.jsonl), which record every API response's model and token
usage. Each response is split across several transcript rows (thinking, text, tool
use) that all carry the same usage, so rows are de-duplicated by message id.

Coverage caveat: until 27 Sep 2026 lib/claude_call.run_claude passed
--no-session-persistence by default, so most scheduled jobs left no transcript. From
27 Sep every run_claude call is recorded; a report run 7+ days later (after a Sunday,
for the weekly jobs) covers every job.

Prices are the Claude API list prices (per million tokens), with cache writes at 1.25x
input (5-minute) or 2x (1-hour) and cache reads at the model's read rate. Confirm
against the live pricing page before quoting a number externally.

Usage: python3 scripts/usage-report.py [--days 30] [--json]
"""
from __future__ import annotations

import argparse
import glob
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PROJECTS = Path("/root/.claude/projects")
# Only ClaudeCoach's own projects: the repo (bot + scheduled jobs) and the
# bug-fixer / smoke-test temp dirs. The expense bot on the same VM is excluded.
# Peak's API runs some jobs from ClaudeCoach/api (spoken replies, food logging), and the
# rule-prune jobs from their own temp dirs (-tmp-cc-prune-*, matched by -tmp-cc-*).
PROJECT_GLOBS = ("-Users-diamondpeakconsulting-diamondpeak-site",
                 "-Users-diamondpeakconsulting-diamondpeak-site-ClaudeCoach-api",
                 "-tmp-cc-*", "-tmp-cca", "-tmp-cctest")

# $ per million tokens: (input, output, cache_read). Writes derive from input.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5":   (10.0, 50.0, 1.00),
    "claude-opus-5-5":  (4.0, 20.0, 0.20),
    "claude-opus-5":    (5.0, 25.0, 0.50),
    "claude-opus-4-8":  (5.0, 25.0, 0.50),
    "claude-opus-4-7":  (5.0, 25.0, 0.50),
    "claude-opus-4-6":  (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),   # checked on the live pricing page 28 Sep 2026
    "claude-sonnet-5":  (2.0, 10.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}


def price_key(model: str) -> str | None:
    m = re.sub(r"-\d{8}$", "", model or "")      # drop a date suffix
    return m if m in PRICES else None


UNPRICED: set = set()


def cost(model: str, u: dict, writes_5m: bool = False) -> float:
    """API list-price cost of one response. writes_5m re-prices every cache write at the
    5-minute rate: on a subscription Claude Code writes the main conversation with the
    1-hour TTL (2x input), but on an API key its default is 5 minutes (1.25x) - see
    code.claude.com/docs/en/prompt-caching, "Which TTL each request gets". A one-shot
    scheduled job never idles between its calls, so the 1-hour lifetime buys it nothing."""
    k = price_key(model)
    if not k:
        if model and not model.startswith("<"):
            UNPRICED.add(model)
        return 0.0
    p_in, p_out, p_read = PRICES[k]
    cc = u.get("cache_creation") or {}
    w1h = cc.get("ephemeral_1h_input_tokens")
    w5m = cc.get("ephemeral_5m_input_tokens")
    total_w = u.get("cache_creation_input_tokens") or 0
    if w1h is None and w5m is None:
        w1h, w5m = 0, total_w
    if writes_5m:
        w1h, w5m = 0, (w1h or 0) + (w5m or 0)
    return ((u.get("input_tokens") or 0) * p_in
            + (w5m or 0) * p_in * 1.25 + (w1h or 0) * p_in * 2.0
            + (u.get("cache_read_input_tokens") or 0) * p_read
            + (u.get("output_tokens") or 0) * p_out) / 1e6


JOBS = [  # (label, regex on the first prompt) - first match wins. Checked against
          # the real prompts of 28 Sep 2026, the first day every job was logged.
    ("chat",            r"^You are ClaudeCoach, "),
    ("session sync",    r"^Session sync|ClaudeCoach session sync"),
    ("voice rewrite",   r"^Rewrite the following coaching reply"),
    ("strava write-up", r"^Write a Strava activity description"),
    ("evening check-in", r"^Evening training log check"),
    ("activity debrief", r"^Check for new activities for"),
    ("morning card",    r"morning briefing|morning (card|check-?in)"),
    ("daily prescription", r"daily session prescription"),
    ("watchdog",        r"daily watchdog"),
    ("night-before brief", r"night-before"),
    ("weekly summary",  r"weekly summary"),
    ("weekly plan",     r"Stage.?1|propos\w+ (the|a|next) week|weekly plan"),
    ("bug fixer",       r"bug-triage|bug.?fix"),
    ("nutrition",       r"nutrition|fuelling"),
    ("smoke test",      r"^Say OK"),
]


def classify(prompt: str) -> str:
    for label, rx in JOBS:
        if re.search(rx, prompt[:3000], re.I):
            return label
    return "other"


def athlete_of(prompt: str, athletes: dict) -> str:
    # A chat prompt names its athlete in its first words; trust that over anything
    # later in the prompt (a chat can quote another athlete's file as an example).
    m = re.match(r"You are ClaudeCoach, (\w+)", prompt)
    if m:
        for slug, a in athletes.items():
            if m.group(1).lower() in (slug, (a.get("name") or "").split()[0].lower()):
                return slug
    # Then the athlete's own file paths anywhere in the prompt (session sync names
    # the athlete only in paths, deep in a long prompt).
    paths = {slug: len(re.findall(r"athletes/%s/" % re.escape(slug), prompt)) for slug in athletes}
    if any(paths.values()):
        return max(paths.items(), key=lambda kv: kv[1])[0]
    head = prompt[:4000]
    scores = {}
    for slug, a in athletes.items():
        names = {slug, (a.get("name") or "").split()[0]} - {""}
        scores[slug] = sum(len(re.findall(r"\b%s\b" % re.escape(n), head, re.I)) for n in names)
    best = max(scores.items(), key=lambda kv: kv[1]) if scores else (None, 0)
    return best[0] if best[1] > 0 else "system"


def first_prompt(rows):
    for r in rows:
        if r.get("type") == "user":
            c = (r.get("message") or {}).get("content")
            if isinstance(c, list):
                c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
            if c:
                return c
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--as-recorded", action="store_true", dest="as_recorded",
                    help="price cache writes at the TTL the subscription used (1h), not the API default")
    args = ap.parse_args()
    athletes = json.loads((BASE / "config/athletes.json").read_text())
    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    by_ath = defaultdict(float)
    by_job = defaultdict(float)
    by_ath_job = defaultdict(float)
    by_model = defaultdict(float)
    runs = defaultdict(int)
    first_ts = last_ts = None
    for pat in PROJECT_GLOBS:
        for f in glob.glob(str(PROJECTS / pat / "*.jsonl")):
            rows = []
            for line in open(f, errors="replace"):
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
            msgs = {}
            for r in rows:
                if r.get("type") != "assistant":
                    continue
                m = r.get("message") or {}
                ts = r.get("timestamp")
                if not m.get("usage") or not ts:
                    continue
                t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if t < since:
                    continue
                msgs[m.get("id") or r.get("requestId") or id(r)] = (m.get("model"), m["usage"], t)
            if not msgs:
                continue
            prompt = first_prompt(rows)
            job = "bug fixer" if "-tmp-cc-bugfix" in f else classify(prompt)
            ath = athlete_of(prompt, athletes) if job not in ("bug fixer", "smoke test") else "system"
            session_cost = 0.0
            # Priced as an API deployment would bill it: jobs on the 5-minute cache (the
            # API-key default), chat on the 1-hour cache (promptCacheTtl=1h - a person
            # replies minutes apart, so the longer cache pays for itself there).
            # --as-recorded prices every write exactly as the subscription recorded it.
            five = (job != "chat") and not args.as_recorded
            for model, u, t in msgs.values():
                c = cost(model, u, writes_5m=five)
                session_cost += c
                by_model[price_key(model) or model] += c
                first_ts = t if first_ts is None or t < first_ts else first_ts
                last_ts = t if last_ts is None or t > last_ts else last_ts
            by_ath[ath] += session_cost
            by_job[job] += session_cost
            by_ath_job[(ath, job)] += session_cost
            runs[(ath, job)] += 1

    if UNPRICED:
        print(f"WARNING: no price for {sorted(UNPRICED)} - those calls are counted as $0; "
              f"add them to PRICES", file=__import__("sys").stderr)
    span_days = max(1.0, ((last_ts - first_ts).total_seconds() / 86400) if first_ts else 1.0)
    scale = 30.0 / span_days
    out = {
        "window_days": round(span_days, 1),
        "per_athlete_month": {a: round(v * scale, 2) for a, v in sorted(by_ath.items())},
        "per_job_month": {j: round(v * scale, 2) for j, v in sorted(by_job.items(), key=lambda kv: -kv[1])},
        "per_model_month": {m: round(v * scale, 2) for m, v in sorted(by_model.items(), key=lambda kv: -kv[1])},
        "per_athlete_job_month": {f"{a}|{j}": {"usd": round(v * scale, 2), "runs": runs[(a, j)]}
                                  for (a, j), v in sorted(by_ath_job.items())},
        "total_month": round(sum(by_ath.values()) * scale, 2),
    }
    if args.json:
        print(json.dumps(out, indent=1))
        return
    print(f"Window: {out['window_days']} days of transcripts, scaled to 30 days (USD, API list prices)")
    print("\nPer athlete / month:")
    for a, v in out["per_athlete_month"].items():
        print(f"  {a:10} ${v:8.2f}")
    print("\nPer job / month:")
    for j, v in out["per_job_month"].items():
        print(f"  {j:20} ${v:8.2f}")
    print("\nPer model / month:")
    for m, v in out["per_model_month"].items():
        print(f"  {m:20} ${v:8.2f}")
    print(f"\nTotal / month: ${out['total_month']:.2f}")


if __name__ == "__main__":
    main()
