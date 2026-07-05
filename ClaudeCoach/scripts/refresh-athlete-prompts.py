#!/usr/bin/env python3
"""Regenerate the AUTO-SYNC block in each athlete's system_prompt.txt from live
config — daily via cron (04:40, before the 05:00 prescriptions).

Why (methodology audit P1-10): hand-maintained hard numbers in the chat prompts
drift — Kathryn's prompt claimed "+8%/wk ramp, peak CTL 72-78" while the config
said +6 CTL/wk and a 76-80 race band, so in-the-moment questions were answered
against numbers the planner does not use. Everything between the AUTO-SYNC
markers is now sourced from config/athletes.json + the menstrual model on every
run; the rest of the prompt is never touched. For cycle-tracking athletes the
block carries today's phase/day, which also makes the CHAT layer cycle-aware
(it previously wasn't, despite tracking being on).
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # ClaudeCoach/
sys.path.insert(0, str(BASE / "lib"))
import menstrual  # noqa: E402

BEGIN = "### AUTO-SYNC: live config (regenerated daily by refresh-athlete-prompts.py — do not edit) ###"
END   = "### AUTO-SYNC: end ###"


def build_block(slug: str, cfg: dict) -> str:
    lines = [BEGIN,
             "These values come from config/athletes.json and OVERRIDE any number stated "
             "elsewhere in this prompt. Injected per-prompt live data (CTL/ATL/plan) still "
             "overrides these for fitness state.",
             f"- Race: {cfg.get('race_name', '?')} on {cfg.get('race_date', '?')}"]
    ramp = cfg.get("max_ctl_ramp_per_week")
    if ramp:
        lines.append(f"- CTL ramp cap: +{ramp} CTL points/week (NOT a percentage)")
    ct = cfg.get("ctl_targets") or {}
    if ct.get("race_min"):
        band = f"{ct['race_min']}" + (f"-{ct['race_max']}" if ct.get("race_max") else "")
        lines.append(f"- Race-day CTL band: {band}")
    if ct.get("phase_ctl"):
        lines.append(f"- Phase CTL milestones: {json.dumps(ct['phase_ctl'])}")
    n = int(cfg.get("deload_every_n_weeks", 4) or 0)
    if n:
        lines.append(f"- Deload: every {n}th training week at ~62% volume; a week executed "
                     f"under 70% of prescription converts the next week to recovery")
    lines.append("- Taper: final ~2 weeks, volume steps 70/55/40% of maintenance load, "
                 "INTENSITY HELD (never revert taper sessions to all-easy)")
    fuel = cfg.get("nutrition_target_g_hr")
    if fuel:
        lines.append(f"- Race fuelling target: {fuel} g CHO/hr (a per-athlete conversation, "
                     f"not a hard rule — race at the highest rate proven in training)")
    if (cfg.get("menstrual") or {}).get("tracking") or slug == "kathryn":
        try:
            cyc = menstrual.phase_for(slug)
            if cyc and cyc.get("phase"):
                lines.append(f"- Menstrual cycle today ({date.today().isoformat()}): day "
                             f"{cyc['day']}, {cyc['phase'].upper()} phase — placement-only "
                             f"guidance (RPE runs higher in luteal/menstrual; quality windows "
                             f"are follicular/ovulation); never cut load on phase alone")
        except Exception:
            pass
    lines.append(END)
    return "\n".join(lines)


def refresh(slug: str, cfg: dict) -> str:
    p = BASE / "athletes" / slug / "system_prompt.txt"
    if not p.exists():
        return "no prompt file"
    s = p.read_text()
    block = build_block(slug, cfg)
    if BEGIN in s and END in s:
        pre = s[:s.index(BEGIN)]
        post = s[s.index(END) + len(END):]
        p.write_text(pre + block + post)
        return "updated"
    # No markers yet: append the block at the end of the prompt.
    p.write_text(s.rstrip("\n") + "\n\n" + block + "\n")
    return "inserted"


def main():
    athletes = json.loads((BASE / "config" / "athletes.json").read_text())
    for slug, cfg in athletes.items():
        if not cfg.get("active"):
            continue
        print(f"{slug}: {refresh(slug, cfg)}")


if __name__ == "__main__":
    main()
