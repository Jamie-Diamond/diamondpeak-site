# ClaudeCoach

A multi-athlete AI endurance-coaching system. It plans training, prescribes daily
sessions, captures session feedback, and talks to athletes over Telegram — backed
by live [intervals.icu](https://intervals.icu) data.

**Active athletes:** `jamie` (Full Ironman), `kathryn` (70.3), `calum` (Sportive).
Per-athlete race goals, constraints, and protocols live in each athlete's
`profile.json` + `reference/` and in the project custom instructions — **not** in
this file.

---

## Two interfaces, one workspace

1. **Automation (the VM).** Cron scripts in `scripts/` run the cadence —
   plan generation, daily prescription, morning/evening check-ins, an activity
   watcher, a watchdog, and weekly summaries — and the Telegram bot (`telegram/`)
   handles athlete replies. These run on the production VM (`root@178.105.95.208`),
   not the Mac.
2. **Interactive Claude sessions.** Ad-hoc analysis, weekly reviews, and
   debugging, using the same files + the IcuSync MCP for live data.

Both share the tested analytics in `ironman-analysis/` — *code handles arithmetic,
conversation/LLM handles judgement.*

---

## Layout

```
ClaudeCoach/
├── README.md                  # this file
├── config/
│   └── athletes.json          # roster config: chat ids, plan_start, phase_tss,
│                              #   ctl_targets, api keys  (GITIGNORED)
├── athletes/<slug>/           # per-athlete (GITIGNORED — data, not code)
│   ├── profile.json           #   FTP, CSS, threshold, race, coaching_level …
│   │                          #   AUTHORITATIVE source for athlete targets
│   ├── current-state.md/.json #   subjective layer + ankle/weight (IcuSync can't see)
│   ├── reference/             #   rules.md, decision-points.md,
│   │                          #   training-blueprint.md + training-blueprint.json (sidecar)
│   ├── session-log.json       #   per-session RPE/gut/heat/fuelling capture
│   ├── heat-log.json, swim-log.json, persistent-rules.md, …
│   └── daily-prescription-latest.md
├── scripts/                   # the cron cadence (see below)
├── telegram/                  # bot.py, notify.py, charts.py
├── ironman-analysis/          # pure, tested analytics primitives + pytest suite
├── lib/                       # icu_fetch.py and helpers (intervals.icu I/O)
├── blueprints/blueprint.md    # the universal periodisation methodology (reference)
├── docs/                      # plans (e.g. remediation-plan.md) and backlog
├── templates/                 # weekly-checkin, race-week-countdown, session-library
└── *.html, site-data.json     # coach/athlete web dashboards (GitHub Pages)
```

### Key scripts (`scripts/`)

| Script | Cadence | Does |
|---|---|---|
| `weekly-plan.sh` → `stage1-plan.py` | Sun 18:00 | Two-stage engine: refreshes each athlete's ICU FTP from eFTP (raise-only), then the LLM proposes each week's SHAPE and deterministic code computes load/fuel/structure, validates against the athlete's protocol, and only pushes a clean week (gated). `--week-start` replans a specific week. |
| `generate-blueprint.py` | manual | Emits `training-blueprint.md` + `.json` sidecar from `athletes.json` phase config. |
| `daily-prescription.py` | daily | Modulates today's session vs readiness; writes `daily-prescription-latest.md` (no Telegram). |
| `morning-checkin.py` | 06:30 | Athlete morning card; surfaces the prescription's key points. |
| `activity-watcher.py` | poll | Per-activity analysis + capture prompts within ~15 min. |
| `watchdog.py` | poll | Detect-and-log only — **silent, never messages**. |
| `weekly-summary.py` | weekly | Trend review vs blueprint targets. |

---

## Where authority lives

| Source | Holds | Read when |
|---|---|---|
| **Project custom instructions** | Per-athlete race plan, build targets, heat/fuelling/sodium/rehab protocols, race-day rules. Single source of truth for the plan. | Session start. |
| `athletes/<slug>/reference/rules.md` | Athlete-specific hard DOs/DON'Ts. | Before any prescription. |
| `athletes/<slug>/current-state.md` | Subjective layer IcuSync can't see. | Before weekly check-in / daily readiness. |
| `athletes/<slug>/reference/training-blueprint.json` | Machine-readable phase windows, distribution, bricks, tests. Phase boundaries derive from `config/athletes.json` (`plan_start` + `phase_tss`). | Planning / validation. |
| **IcuSync MCP / `lib/icu_fetch.py`** | Activities, fitness (CTL/ATL/TSB), planned calendar, wellness. System of record. | Whenever data is needed — never fabricate. |

### Precedence when sources disagree (planning)

The 22 Jul failure — the bot improvised Kathryn's forward week from prose rules,
zeroed her Build-phase Z4–5 run slice yet asserted it was on spec, and narrated
week-13 Build as "start of Peak" — traced to there being no stated ranking
between the numeric blueprint and the prose rules. The ranking is:

1. **The per-sport intensity distribution in `training-blueprint.json` is the
   spec** (e.g. `Run 78% Z1–2 / 12% Z3 / 10% Z4–5`). It sets how much of each
   zone the week must contain.
2. **Prose rules** (`rules.md`, `persistent-rules.md`, standing/global rules,
   session notes) may refine *how* a slice is delivered — which day, session
   shape, cues — but **may not zero out or reduce a zone slice the blueprint
   requires.** If prose appears to remove a required slice, the blueprint wins.
3. **The only exception** — the one thing that may zero a required quality slice
   — is an **injury/illness hard-gate read from structured `current-state.json`**
   (e.g. an uncleared-ankle flag), never from prose and never from memory. The
   injury half is `lib/injury.py`; the illness half is `lib/illness.py`, whose
   `training_gate` field is the only thing in that flag that may reduce a plan
   (see `docs/illness-flag.md`).
4. **Phase labels come from config** (`athletes.json` phase weeks, resolved by
   `primitives.blueprint`), never narrated from memory.
5. **Forward-plan questions** ("what will next week look like", "how do we hit X")
   are answered from the **deterministic engine** (`weekly-plan.sh` →
   `stage1-plan.py` → `plan_builder`), not free-associated from prose. A stated
   week is checked against the distribution in **both directions** (excess quality
   *and* a missing/under-dosed Z3/Z4–5 slice) by `lib/plan_distribution.py` before
   any "on spec" claim. This precedence is enforced in the bot prompt via
   `engine._AUTHORITY_RULE` (always present) and reinforced by the FORWARD WEEK
   block in the live context.

---

## Standing rules (non-negotiable)

- **UK English. Concise. Tables when comparing. Flag uncertainty explicitly.**
- **L2 reasoning trail on every prescription:** `[signal] → [rule] → [adjustment] → [expected effect]`. Signal cites a real number; rule traces to `rules.md`. No trail = no prescription.
- **Pull data via IcuSync — never fabricate.** If it's down, say so and ask for a manual paste.
- **Multi-signal corroboration before any load reduction.** HRV alone is never the trigger.
- Per-athlete hard rules (ankle gating, fuel aversions, heat protocol, etc.) live in each athlete's `rules.md` / `persistent-rules.md` and the project instructions.

---

## Athlete targets — one authoritative source

`athletes/<slug>/profile.json` is the **authoritative** record of an athlete’s
race targets (goal paces, split times, thresholds). Every other place a target is
stored — `config/athletes.json` (`race_target_splits`, engine-read),
`athletes/<slug>/reference/rules.md`, `athletes/<slug>/system_prompt.txt` (injected into
the bot), `athletes/<slug>/current-state.md` — is a **mirror**.

Never hand-edit one copy. Write targets through `lib/athlete_targets.py`
(`set_run_pace_target`), which updates every location together, atomically, and
**fails loudly** if any copy cannot be written or a stale value would survive. This
exists because a partial hand-edit on 22 Jul 2026 left Kathryn’s goal run pace stale
in the bot’s injected prompt while other files were corrected.

---

## Per-athlete modes

Three settings, not one on/off switch:

| Setting | Where | Effect |
| --- | --- | --- |
| `active: false` | `config/athletes.json` | Everything off, including activity tracking. |
| tracking-only | `config/planning-paused.json` (tracked in git) or `planning_paused: true` in `config/athletes.json` | Nothing is **prescribed** and nothing is **judged**: no weekly plan, daily prescription, blueprint, check-in, night-before brief, watchdog trigger, plan audit or weekly summary. The activity watcher, the Telegram bot and the public site data keep running, and the coach prompt is told not to chase missed sessions. |
| `coaching_level` | `athletes/<slug>/profile.json` | Vocabulary and depth only (`beginner` / `mid` / `pro`). |

Pause an athlete by adding them to `config/planning-paused.json` and pushing —
`cc-gitpull` picks it up on the VM within 30 minutes, no shell access needed.
Resume by deleting the entry. Reference: `lib/planning_pause.py`.

## Race week

The race is the biggest single session of the year and, until 6 Sep 2026, the one
thing on the calendar nothing costed — `validate_week` only ever sees the built
proposal, so the race sat outside the week total, the load cap and the CTL-ramp
projection. The taper ladder's final step (40% of maintenance) is a *whole-week*
figure and was being handed to the planner as a *training* budget: ~300 TSS of
training in the seven days around a ~540 TSS Ironman.

Now the race is costed (`primitives.planned_tss.race_tss`) and deducted first:

    training budget = max(15% of 7xCTL, whole-week taper figure - race TSS)

For any long-course race the race exceeds the whole-week figure, so the openers
floor is what gets prescribed — which is the right answer. `validate_week` also
hard-fails a session scheduled on race day (`session_on_race_day`).

Race load comes from, best first: an explicit `race_tss`; the athlete's OWN
previous race at that distance (`profile.prev_race`, summed per leg — Jamie's IM
Italy is 556 TSS against a 539 event default that was never his); an expected
finish time (`race_expected_hours`); the event default. Defaults are whole-event
`hours x IF^2 x 100`: Full Ironman ~539, 70.3 ~320, Olympic ~178, sportive ~294.

The bike IF must be read from `prev_race.bike_if` as recorded, never recomputed
from NP against today's FTP — 201 W against an FTP that has gone 275 -> 307 reads
as 0.65 rather than the 0.73 it was, and the race comes out ~70 TSS light.

**Openers are above race intensity for long course.** Long-course racing happens
at or below the top of Z2 (Jamie's IM bike: 230 W against FTP 307, i.e. 75% of
FTP, exactly the top of the Coggan Z2 band), so "a few minutes at race effort" is
easier than the athlete's normal easy riding and primes nothing. For an event
whose race IF is at or below `_LONG_COURSE_IF`, race-week openers are short
threshold/VO2 bursts and race-pace work is pacing and fuelling rehearsal only.
Short-course racing is the other way round: there, race pace IS the sharpening
intensity.

Race week carries **no protected long ride and no long-run target**
(`session_library`): `long_ride_missing` is an unconditional hard blocker, so a
race week whose budget is ~113 TSS would fail every attempt for want of a 4-hour
ride and deliver no week at all. The same applies to post-race recovery weeks.

The race on the calendar is safe: intervals.icu tags it category `RACE`, so
`_is_workout` drops it (no double-count with the deduction) and the replace-push
delete filter (`WORKOUT` only) cannot remove it. A hand-entered race named like
the race is skipped by `session_on_race_day` so it cannot false-block its own
week.

`plan_tools.DOWN_WEEK_TYPES` is the single list of weeks that are light by design
(`deload`, `taper`, `race`, `post_race`) — used both to relax the quality floors
and to stop a deliberately light week reading as a missed one.

## After the race

A race in the **past** puts the athlete in a `transition` phase, not a taper
(`plan_tools.required_tss`, `primitives.blueprint.current_phase`). Intensity
stays off and the weekly load ceiling stays armed — a taper's is deliberately
`None`, which post-race meant no ceiling at all.

- **Weeks 1-3** — recovery. Volume returns at 30/45/60% of the ~7xCTL load.
- **Week 4 onward** — work toward a **maintenance CTL**, handled like any other
  CTL target: the weekly load that converges on it over ~4 weeks, ramp-capped.
  Above it the athlete comes down to it, below it they build gently back up, and
  either way it settles instead of drifting.

Set the number per athlete as `ctl_targets.maintenance_ctl` — what someone holds
between goals is a coaching decision, not arithmetic. Until it is set it is
DERIVED at 60% of their own peak/race CTL and flagged `maintenance_ctl_source:
"derived"` with `needs_maintenance_target`, so the assumption is in front of the
coach rather than buried. With no peak or `race_min` to derive from, the athlete
simply holds current CTL.

Do not use a fixed fraction of 7xCTL as a "hold": 7xCTL is exactly the load that
keeps CTL level, so a fraction of it prescribes less every week as CTL falls and
CTL chases it down — from 45 that reaches 23 in twelve weeks, asymptote zero.

Countdowns come from `races.countdown`, which counts to the next **upcoming**
race and shows nothing once every configured race has been run. Configure the
next race and regenerate the blueprint to start a new block.

## Deployment

- **Code** ships via `git push` → on the VM, `cc-gitpull.sh` (`git pull`). Branch: `main`.
- **`bot.py` / `charts.py` changes** require `systemctl restart claudecoach-bot` on the VM.
- **Gitignored data** (`config/athletes.json`, `athletes/`, sidecars) does **not** travel via git — regenerate or sync it on the VM directly.
- Run a one-line diagnostic on the VM before issuing command sequences; verify the service restarted after a deploy.

---

## Analytics (`ironman-analysis/`)

Pure functions (dicts in, dicts out), ~1:1 test:source ratio — run `pytest` there
before any change. Covers Banister CTL/ATL/TSB, plan load maths, session modulation
(R1–R7), compliance, environmental pacing, debrief, and the blueprint sidecar
contract. See `ironman-analysis/SKILL.md` for the invocation contract.
