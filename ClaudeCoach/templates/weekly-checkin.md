# Weekly check-in — output template

**Purpose:** the structured form Claude fills in for a weekly review. Triggered by the **Weekly check-in** prompt in `reference/prompts.md`. Claude pulls data via IcuSync, reads `current-state.md`, then writes a response in this shape.

> Claude: this is the **output structure**, not a fill-in form for the user. Use these headings and sections in the reply, with real numbers from IcuSync. Never paste this template back at the user — produce a filled response.

---

## Week of [Mon date – Sun date]

### Top line

> One sentence on the week. Trend, key event, or signal.

### Load trajectory (IcuSync pull)

| Metric | Mon | Sun | Δ |
|---|---|---|---|
| CTL | | | |
| ATL | | | |
| TSB (abs / %) | / | / | / |
| 7-day ramp (ΔCTL) | — | | vs cap +4/wk (ankle in rehab) |
| ATL−CTL gap | | | |

Flags:
- [ ] Ramp >4 CTL/wk while ankle in rehab → hard flag
- [ ] ATL > CTL by >25 for >5 consecutive days → recovery trigger
- [ ] Run-km weekly increase >10% → flag

### Plan vs completed (by discipline)

| Discipline | Planned hrs | Completed hrs | Planned TSS | Completed TSS | Notes |
|---|---|---|---|---|---|
| Swim | | | | | |
| Bike | | | | | |
| Run | | | | | |
| Strength | | | — | — | |
| Heat (sessions) | | | — | — | |
| **Total** | | | | | |

### Key sessions analysed

For each (max 3), pull the activity stream:

**[Session name, date]**
- Targets: [power / pace / HR zones, duration]
- Achieved: [actuals]
- Drift: [1st third vs last third]
- Decoupling (HR vs power/pace): [%]
- Adaptation achieved: [yes / partially / no — one-line rationale]

### Subjective layer (from current-state.md)

- Ankle: [pain during / next morning / week's km vs cap]
- Other niggles: [list or none]
- Missed / cut sessions: [list]
- Body weight 7-day avg: [kg vs target trend]
- Heat sessions this week: [N / cumulative N of 14–20 target]
- Open actions in motion: [highlights]

### Build trajectory check

Where are we vs the build-table CTL targets in the project plan?

| Milestone | Target CTL | Current projection | Δ | Status |
|---|---|---|---|---|
| End base (end May) | ~85 | | | on track / behind / ahead |
| End build (end June) | ~95 | | | |
| End specific (end July) | ~105 | | | |
| Peak (mid-Aug) | 110–115 | | | |

(Use `ironman-analysis/` orchestrator output if available; otherwise a 14-day-avg-ramp projection — label as naive.)

### Next week — what and why

| Day | Discipline | Session | Rationale (1 sentence) |
|---|---|---|---|
| Mon | | | |
| Tue | | | |
| Wed | | | |
| Thu | | | |
| Fri | | | |
| Sat | | | |
| Sun | | | |

- Total hours by discipline: [swim / bike / run / strength]
- Primary stimulus of the week: [one phrase]
- Single most important session: [which, why]
- What's at risk if it's missed: [one sentence]

### IcuSync push confirmation

- [ ] Sessions written to Intervals.icu calendar on the correct dates
- [ ] Each session includes warm-up, main set with targets, cool-down, total time/distance
- [ ] Brick included if week calls for one
- [ ] Weather-dependent sessions flagged

### One-line week summary

> [One sentence to close.]

---

Data provided by Garmin® (where activity-detail data is included).
