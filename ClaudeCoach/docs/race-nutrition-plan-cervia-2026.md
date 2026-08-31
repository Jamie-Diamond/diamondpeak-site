# Race Nutrition Plan — IM Italy Emilia-Romagna, Sat 19 Sep 2026

**Athlete:** Jamie · 82.5 kg · gut-trained (69 g/hr proven on a 32 km run; 400 g+ long sessions logged)
**Projection:** swim 1:07 · bike 4:44 @ 38 km/h · run 3:42 @ 5:15/km · **9:38** · gun 07:30
**Validated by:** the fuelling planner's own engine (`js/fuelling-engine.js`) + gut-backlog model + caffeine
pharmacokinetics + plan check, run headlessly via `ClaudeCoach/scripts/race-fuelling-plan.js`.
Re-run any time: `node ClaudeCoach/scripts/race-fuelling-plan.js`

## Strategy in one line each

- **Carbs:** bike 93 g/hr (440 g), run 81 g/hr (298 g). Bottles are primary fuel on the bike; chews top up.
- **Fluid:** **zero deficit on the bike** (~1,163 ml/hr vs ~1,200 sweat); deficit **allowed to grow on the run**, landing ~2–2.5% BW at the line. Hot afternoon lever: cup at *every* run station instead of alternating.
- **Sodium:** PH1500 tab in each of the three Maurten bottles + PH1000 pickups → bike 1,015 mg/hr, run ~265 mg/hr + cups. Salt pills (×4) pocket backup only.
- **Caffeine:** 400 mg = 4.8 mg/kg, front-loaded then steady: 145–236 mg circulating gun to finish.
- **No gels. No cola. Aid-station pickups are liquid only** (water / PH1000).

## Race morning

| Clock | What | Carbs / caff |
|---|---|---|
| 04:20 | Overnight oats + berry smoothie + banana + ½ rice pudding pot | 148 g |
| 04:30 | **Hi5 Caf Hit** with breakfast | 26 g / 200 mg |
| 05:00 | 500 ml water; sip to 07:00, then stop | — |
| 06:35 | **PF 30 Chew** walking to the start | 30 g |

No ice slurry — September mornings in Cervia are cool. Cooling starts on the run via dousing.
Swim + T1: nothing.

## Bike — 440 g = 93 g/hr · 1,163 ml/hr · 1,015 mg/hr Na · 100 mg caff

**Bottles (set up race morning):**

| Bottle | Contents | Carbs / caff / Na |
|---|---|---|
| 750 #1 (droppable) | Maurten 320 ×1 + CAF 100 ×½ + PH1500 tab | 120 g / 50 mg / 1,095 mg |
| 750 #2 (droppable) | same | 120 g / 50 mg / 1,095 mg |
| Aero 610 (fixed) | Maurten 320 ×1 + PH1500 tab | 80 g / — / 980 mg |
| Frame 400 (fixed) | water | swim-loss payback, then douse duty |

Max concentration is 320/500 ml — 1.5 sachets per 750 is the ceiling.

**Ride it like this** (sip on a 15-min watch alarm, ~⅙ bottle per sip — never gulp):

| Bike time | Action |
|---|---|
| 0:00–0:30 | Drain the **frame 400 ml** (pays back swim sweat) + start Bottle 1 |
| 0:35 | **Choco fudge bar** — solid food while the gut is freshest |
| 0:00–1:25 | **Bottle 1**, empty by 1:25 |
| 0:47 — km 30 | Water pickup: refill frame for dousing |
| 1:15 / 2:15 / 3:15 / 4:15 | **½ SiS Beta Fuel chew** |
| 1:35 — km 60 | **Drop Bottle 1**, grab PH1000 |
| 1:40–2:55 | **Bottle 2**, empty by 2:55 |
| 3:17 — km 125 | **Drop Bottle 2**, grab water/PH1000 |
| 3:10–4:05 | **Aero**, drained by 4:05 — it can't be dropped, so it goes home empty |
| Every station (30/60/87/97/125/157 km) | **Drink a full course bottle**, douse the rest, bin before carpet |
| Final ~30 min | Carbs done — gut arrives at T2 clear |

## Run — 298 g = 81 g/hr · 840 ml/hr · 100 mg caff

| Run time | Action |
|---|---|
| 0:00–0:25 | **Flask: 500 ml Maurten 320 CAF** from T2 — sip in thirds, **ditch by 25 min** (80 g + the run's 100 mg caffeine) |
| 0:36 / 0:52 / 1:24 / 1:40 / 2:12 / 2:44 / 3:16 | **½ Beta Fuel chew** |
| 1:08 / 1:56 | **PF 30 Chew pack** |
| After 3:16 | Nothing — run it home |
| Stations | Alternate water (sip + douse from km 5) and **PH1000 cup**; every station if hot |

## Caffeine ledger — 400 mg (4.8 mg/kg; cap 495)

| When | Source | mg |
|---|---|---|
| 04:30 breakfast | Hi5 Caf Hit | 200 |
| Bike, sipped | ½ CAF sachet per 750 | 100 |
| Run 0–25 min | 320 CAF flask | 100 |

Circulating (model): 161 mg at the gun · 175 mg at run start · 236 mg mid-run · 205 mg at the line.

## Pack list

| Item | Count | Where |
|---|---|---|
| Maurten 320 plain | 3 | bottles ×2 halves… 1 each in 750s + aero |
| Maurten 320 CAF 100 | 2 | ½ + ½ in the 750s, 1 in the T2 flask |
| PH1500 tabs | 3 | one per Maurten bottle |
| SiS Beta Fuel chews | 6 bars | 2 bento, 4 suit/belt (unwrap into pouch) |
| PF 30 Chews | 3 | 1 pre-race, 2 run |
| SiS Choco Fudge bar | 1 | bento |
| Hi5 Caf Hit | 1 | breakfast |
| Salt pills | 4 | pocket, backup only |
| Soft flask 500 ml | 1 | T2 bag |

## Model verdicts (last run 31 Aug)

| Check | Result |
|---|---|
| Gut backlog | Clean — under the 30 g line throughout the race |
| Carb gaps | None >30 min inside bike/run |
| Glucose:fructose | In range at all rates |
| Sodium | Bike 1,015 mg/hr · run 265 + cups |
| Fluid | Engine flags 3.6% race-average deficit at 1,200 ml/hr sweat — **deliberate**: zero on the bike, growing on the run (~2–2.5% BW at finish) |

## Before race day

- **Rehearse the bike hour** (bottle sips + fudge/chew cadence) on the next long ride — includes proving PH1500-in-Maurten mixes cleanly at 1.5×.
- **Rehearse the run cadence** (flask start + 81 g/hr chews) on the last long run — proven is 69 g/hr, this is a step up.
- **Aid stations are the 2025 layout** — re-check km marks and what's on course when the 2026 athlete guide drops (~2 weeks out). If PH1000 isn't served, salt pills move from backup to 2/hr on the bike.
- Sweat rate assumed **1,200 ml/hr on the bike** from history (1.5–2.5 kg net losses on long sessions). A weigh-in on the rehearsal ride sharpens this — update `st.sweatRate` in the script and re-run.
- Carb load: 8–10 g/kg Wed–Thu (660–825 g), 6–8 g/kg Friday, low fibre from Monday.
