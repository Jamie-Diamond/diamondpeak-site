# Ironman Italy 2026 — analysis layer

Deterministic Python primitives for training analytics across the 21-week build to Ironman Italy Emilia-Romagna, 19 September 2026.

## Why this exists

Conversational training analysis drifts subtly in framing every time. Encoding the methodology in code gives consistent outputs, lets us reason about projections, and produces tests that catch schema drift from upstream MCPs.

Conversation handles judgement. Code handles arithmetic.

## Methodology — locked

| Concern | Method | Source |
| --- | --- | --- |
| Load (CTL/ATL/TSB) | Banister 42d/7d EWMA on daily TSS | This package + Intervals.icu cross-check |
| Ramp | 7-day ΔCTL | Computed |
| TSB units | Absolute *and* % (`tsb_pct = tsb / ctl * 100`) | Both reported |
| Daily TSS | Sum of activity TSS by athlete-local date, deduplicated | `get_training_history` |
| Volume | Weekly km/hours/TSS by sport, 7d:28d acute:chronic | `get_training_history` |
| Heat acclimation | Qualifying-session count, days-since-last (7-10 d decay), distribution vs 14-20 target | Manual or `get_events` |
| HRV | lnRMSSD, 7d rolling vs 60d baseline +/- 1 SD; multi-signal corroboration with RHR + sleep (subjective ignored per athlete decision) | `get_wellness` |
| Session | NP, IF, VI, decoupling (Pw:HR), plan-vs-actual delta | `get_activity_detail`, `get_extended_metrics` |

See `SKILL.md` for invocation contract. See `schemas/` for observed MCP response shapes.

## Repo layout

```
ironman-analysis/
├── README.md                # this file
├── SKILL.md                 # how Claude invokes this package
├── pyproject.toml           # package metadata + deps
├── primitives/              # pure functions, one module per concern
│   ├── __init__.py
│   └── load.py              # CTL/ATL/TSB, ramp, gap counter, flags
├── tests/                   # pytest, no MCP coupling
│   ├── conftest.py          # shared fixtures
│   └── test_load.py
├── schemas/
│   └── intervals_icu.md     # observed shapes from IcuSync MCP
├── fixtures/                # JSON snapshots for tests + worked examples
└── runs/                    # worked-output reports, dated
```

## Running

### Anywhere with Python 3.10+

```bash
cd ironman-analysis
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Tests use only fixtures — no MCP, no network.

### From Cowork

I'll fetch live MCP data, feed dicts into the primitives, write the report into `runs/`.

### From Claude Code (locally)

Same code paths. Either feed it pre-fetched JSON, or wire up your Intervals.icu API key for direct HTTP calls (not done yet — would replace the MCP fetch step in any orchestrator).

## Phasing

| Phase | Deliverable | Status |
| --- | --- | --- |
| 1 | `load.py` + tests + worked output | In progress |
| 2 | `volume.py`, `heat.py` | Pending |
| 3 | `hrv.py` (HRV + RHR + sleep multi-signal; subjective ignored) | Pending |
| 4 | `session.py` (NP/IF/VI/decoupling, plan-vs-actual) | Pending |
| 5 | `weekly_review.py` orchestrator | Pending — only after primitives stable |

## Standing rules (from project knowledge)

- UK English. Concise. Flag speculation explicitly.
- Heat is the binding constraint on this race — re-evaluate every recommendation against heat impact.
- Do not assume the ankle is healed. Always ask for current status before prescribing run load.
- Never recommend gels as primary fuel for this athlete.
- Future-dated fitness rows from `get_fitness` are zero-training projections. Always label them as such.

## Data

Fitness, training, and wellness data flow from Garmin devices to Intervals.icu via direct sync (no Strava licensing gap). Activities sourced from Garmin display "Data provided by Garmin®" attribution where required.
