# Layer 0 — Quality-Session Library & Periodisation Methodology (DRAFT for red-line)

Status: **draft for Jamie's red-line** · 2026-06-15 · feeds `docs/planning-architecture.md` Layer 0.
This is the "good session" engine: the encoded coaching IP that makes prescribed sessions
sound, varied, and progressive. Once you red-line it, I encode it as JSON the planner consumes.

## How to read this
- **Confidence tags:** ✅ verified (research, cited) · 🟡 extrapolated from verified anchors ·
  🔴 weak evidence / needs your judgment. Red-line the 🟡/🔴 first.
- **Units are per-discipline and never cross-applied:** bike = %FTP (power); run = %vVO2max or
  Daniels pace; swim = %CSS pace. Recovery is wall-clock or work:rest ratio.
- **Sources:** Coggan/TrainingPeaks power levels; Daniels VDOT (vdoto2.com); Seiler/Stöggl–Sperlich
  TID; Filipas 2022 (PYR→POL); Muñoz/Seiler 2014 (Ironman TID); Sellés-Pérez 2019 (70.3 template).

---

## 0. Zone models — CANONICAL reference & mapping (READ FIRST)

**DECIDED (Jamie, 15 Jun): canonical = the athlete's intervals.icu / Coggan zones.** Session
prescriptions are expressed in these device zones; TID targets stay in the 3-zone LOW/MOD/HIGH
model (that's how polarized/pyramidal are *defined*) and map onto the device zones below.

**Canonical zones — pulled from Jamie's live ICU sport settings (not textbook):**

*Bike — power, %FTP:*
| Z1 Recovery | Z2 Endurance | Z3 Tempo | Z4 Threshold | Z5 VO2max | Z6 Anaerobic | Z7 Neuromuscular |
|---|---|---|---|---|---|---|
| <55 | 55–75 | 75–90 | 90–105 | 105–120 | 120–150 | 150+ |

*Run — pace, % of threshold pace (faster = higher %):*
| Z1 | Z2 | Z3 | Z4 (threshold) | Z5a | Z5b | Z5c |
|---|---|---|---|---|---|---|
| <77.5 | 77.5–87.7 | 87.7–94.3 | 94.3–100 | 100–103.4 | 103.4–111.5 | 111.5+ |

*Swim — pace, % of CSS:* same boundary structure as run pace (Z4 = at CSS).
**Sweet spot = 88–94% FTP** (straddles upper Z3 / low Z4) — a named sub-band, not its own zone.

**3-zone TID → ICU-zone mapping (the bridge):**
- **LOW** (below LT1, easy aerobic) ≈ bike **Z1–Z2** / run **Z1–Z2** — *the base to maximise*.
- **MOD / grey zone** (LT1–LT2, tempo) ≈ bike **Z3** (+ low sweetspot) / run **Z3**.
- **HIGH** (above LT2) ≈ bike **Z4–Z5+** / run **Z4–Z5+**.

So Muñoz's "minimise grey-zone Z2 bike" = **minimise bike Z3 tempo / junk sweetspot**, while
**maximising bike Z2 endurance**. The blueprint's `Z1–2 / Z3 / Z4–5` grouping already matches this.
The validator/audit must compare TID like-for-like via this mapping.

**⚠️ Data discrepancies found during the audit — reconcile before encoding:**
1. **Bike FTP:** ICU sport-settings = **300 W**, but the system prompt / memory say **316 W**.
   Zones are %FTP so the *boundaries* are unaffected, but the *absolute watts* the planner writes
   depend on which FTP is used — these must agree (use ICU as authoritative per the MCP rule).
2. **Swim threshold pace:** ICU returns `threshold_pace = 1.01` but Jamie's CSS is **1:39/100m**.
   Likely a units/stale-field issue — verify the swim threshold before any %CSS prescription.
3. **Run threshold pace:** ICU = **4:08/km**; profile says **4:02/km**. Minor; use ICU (authoritative).

## 1. Intensity distribution (the TID targets per phase) — in 3-zone LOW/MOD/HIGH (see §0)

✅ **Verified models:** Polarized ≈ 75–80% LOW / ~5% MOD / 15–20% HIGH · Pyramidal ≈ 80% LOW /
~10% MOD / 5–10% HIGH · Threshold ≈ 40–60% MOD.
✅ **Periodisation principle:** **base pyramidal → specific polarized** (Filipas 2022 PYR→POL beat
all other orderings), i.e. *polarise toward the race*. Magnitude is modest — treat as directional.
✅ **Long-course (IM/70.3):** anchor volume in **LOW (Coggan Z1–Z2 endurance)**, and **minimise the
MOD grey zone — i.e. Coggan Z3 tempo / junk sweetspot on the bike** (Muñoz 2014: more LOW → faster,
more grey-zone bike → slower), *even though you race in the MOD zone*. NB: this does **not** mean
cut endurance (Coggan Z2) riding — that's the zone to build.

These map onto the blueprint's per-phase `distribution` field via §0; this section makes the
targets evidence-based and event-specific (see §4).

---

## 2. Bike session library (Coggan %FTP) ✅ structures verified; 🟡 progressions

| Type | Intensity (ICU zone) | Structure → progression (base→build→peak) | Recovery | Notes |
|---|---|---|---|---|
| **VO2max** | 105–120% FTP (Z5) | 5×3min → 6×3min → 5×4min | 1:1–2:1 (≈3min) | ✅ zone/dur; 🟡 progression. Longer bouts > micro-intervals for time-at-VO2. |
| **Threshold/LT** | 90–105% FTP (Z4) | 3×10 → 4×10 → 3×15 → 2×20min | short, 3–5min | ✅ zone/dur (Coggan "10–30min repeats"); 🟡 progression |
| **Sweetspot** | 88–94% FTP (top Z3 / low Z4) | 3×12 → 2×20 → 2×25 → 3×20min | 5–8min | 🟡 aerobic-builder, base/build; high TSS-per-hour |
| **Race-pace (long-course)** | IM ~68–75% (Z2) · 70.3 ~80–88% (Z3) FTP | 2×30 → 3×30 → 2×45min within long ride | 10min Z2 | 🟡 specificity work; IM stays Z2, 70.3 is Z3 grey-zone *by design* (race intensity) |
| **Endurance** | 55–75% FTP (Z2) | continuous; grow duration to event anchor | — | the long ride — the LOW base |

## 3. Run session library (Daniels VDOT) ✅ structures verified; 🟡 progressions

| Type | Intensity (ICU pace zone) | Structure → progression | Recovery | Cap (✅) |
|---|---|---|---|---|
| **VO2max (I)** | ~3–5k pace ≈ 104–112% thr-pace (Z5b) | 4×3min → 5×3 → 6×3min | 1:1 (≈3min jog) | ≤ lesser of 8% wk mileage / 10k. Canonical: **4×3min @95% vVO2max, 3min rec** ✅ |
| **Threshold (T)** | ~1hr race pace ≈ 96–100% thr-pace (Z4) | 20min steady → 2×15 → 3×10 (cruise) | short (1min) | ~10% wk mileage ✅ |
| **Reps (R, economy)** | mile/1500 pace ≈ 112%+ thr-pace (Z5c) | 6–8×400m → 10×400 / ≤2min bouts | full recovery | ≤ lesser of 5% mileage / 5mi ✅ |
| **Race-pace/specific** | marathon ~88–94% (Z3) · HM ~94–100% (Z4) thr-pace | marathon: 2×4mi→3×4mi @ MP; HM: 3×3km @ HMP | float | 🟡 event-specific |
| **Long / easy** | Z1–Z2 (<87.7% thr-pace) | grow 10–15%/wk to event need | — | the LOW base; ankle guard applies |

## 4. Swim session library (%CSS) 🔴 NEEDS YOUR RED-LINE (weakest evidence)

The research did **not** verify concrete CSS structures/rest — these are my best-practice draft;
please correct. Intensities below are %CSS, which maps to the ICU swim pace zones (Z4 = at CSS;
faster-than-CSS = Z5; easy/drills = Z1–Z2). **⚠️ Verify the swim threshold first** — ICU returns a
threshold pace that doesn't match your 1:39 CSS (§0 flag); a wrong CSS makes every %CSS target
wrong. Also: swim renders in **time** today (ICU reads "400m" as minutes) — I'll fix distance→time
via CSS pace so sets read as distance.

| Type | Intensity | Structure → progression | Rest | Confidence |
|---|---|---|---|---|
| **Drills / technique** | easy / technique pace | 6–10×50 drill (catch-up, single-arm, fingertip-drag, sculling, 6-3-6) ± 4×50 kick → add complexity/volume | 15–20s | 🟡 — see note |
| **Skills (open-water)** | easy–steady | sighting every 6–9 strokes, drafting, turns, deep-water starts; OW sim sets | — | 🟡 — for OW events (5k swim, tri) |
| **CSS/threshold** | at CSS | 8×100 → 6×150 → 5×200 → 4×300 | 10–20s 🔴 | 🔴 draft |
| **Speed/VO2** | faster than CSS (−3–6s/100) | 12×50 → 16×50 / 8×75 | 15–30s | 🔴 draft |
| **Aerobic/endurance** | CSS +5–10s/100 | continuous 1500–3000m / pull sets | — | 🟡 |
| **Race-pace (5k OW)** | target OW pace | 3×800 → 2×1500 continuous | 30s | 🟡 |

**Drills/skills policy 🟡:** technique is trained two ways — (1) as a **standalone technique swim**
(drills + kick + easy form work), weighted higher in **base** and for weaker swimmers; and (2) as
an **embedded warm-up/skill block** in CSS/aerobic/speed sessions (e.g. 200 easy + 6×50 drill before
the main set). Open-water **skills** (sighting, drafting, turns) ramp through the specific phase for
tri and 5k-OW events. Pull-buoy (PB) work sits with aerobic/CSS, not technique. **[your red-line:
drill menu, base-phase technique weighting, OW-skill progression.]**

---

## 5. Per-event matrix (TID by phase · key sessions · periodisation · bricks)

✅ = research-grounded · 🟡 = extrapolated to the event (your red-line zone)

| Event | Base TID | Build/Spec TID | Emphasised quality | Periodisation | Bricks |
|---|---|---|---|---|---|
| **Ironman** ✅ Z1-anchor | 85/10/5 PYR | 80/12/8 → polarised | Race-pace bike, sweetspot, long run/ride; **low VO2** | long base + long specific | near-weekly in specific, long |
| **70.3** ✅ template | 83/12/5 | **80/11/9** (Sellés-Pérez) | Threshold+race-pace bike, CSS swim, brick | **7wk gen + 13wk spec (4/4/5)** | near-weekly in specific |
| **Olympic tri** 🟡 | 80/12/8 PYR | 75/10/15 POL | **VO2 + threshold** (bike+run), CSS, sharp bricks | gen + specific, more intensity | weekly, shorter/faster |
| **Marathon** 🟡 | 85/12/3 PYR | 80/8/12 POL | **Threshold + MP long runs**, some VO2 in base | PYR→POL, big endurance | n/a |
| **Half-marathon** 🟡 | 82/13/5 | 78/10/12 POL | **Threshold-heavy + VO2 + HMP** | PYR→POL | n/a |
| **10k** 🟡 | 80/12/8 | 75/8/17 POL | **VO2max + threshold + reps** | PYR→POL | n/a |
| **5k** 🟡 | 80/10/10 | 72/8/20 POL | **VO2max + reps (R) + threshold** | PYR→POL, high hard-share | n/a |
| **Long sportive/gran fondo** 🟡 | 82/13/5 | 78/15/7 | **Sweetspot + threshold + durability long rides** | base→build, durability-led | n/a |
| **5k swim (OW)** 🔴 | endurance-heavy | + race-pace | **CSS endurance + aerobic volume + OW race-pace**; pacing/sighting | base→build | n/a |

Intensity-share triplets are **LOW / MOD / HIGH %** (3-zone, per §0). HIGH share rises as the event
shortens (5k/10k highest), LOW (endurance) share rises as it lengthens (IM highest) — per §1 and the
verified economy-vs-VO2 tuning (economy 80–90% LIT; VO2 75–85% LIT).

---

## 6. Brick policy (triathlon) 🟡
Bike→run transition runs: near-weekly in the **specific** phase ✅ (70.3 template). Scale to event:
Olympic = short/sharp off race-pace bike; 70.3 = race-pace bike + 20–35min run @ race pace;
IM = long aerobic bike + 20–40min Z2 run. Run-off-bike at **goal race pace**, not easy.

## 7. Confidence summary & what to red-line
- ✅ **Resolved (§0):** canonical = ICU/Coggan zones (Jamie, 15 Jun); zones audited against Jamie's
  live ICU settings and aligned (bike 90–105/105–120 etc.); render-library bands confirmed to sit
  within the canonical zones; TID expressed LOW/MOD/HIGH with an explicit ICU-zone bridge.
- ⚠️ **Reconcile before encoding (data, not methodology):** bike FTP 300 (ICU) vs 316 (prompt);
  swim threshold pace ICU `1.01` vs CSS 1:39; run threshold 4:08 (ICU) vs 4:02 (profile). See §0.
- ✅ **Trust:** bike (Coggan) + run (Daniels) zones/structures, TID models, PYR→POL, IM/70.3 anchors.
- 🟡 **Check my extrapolation:** the week-to-week *progression numbers*, short-course event triplets,
  race-pace zones, brick scaling.
- 🔴 **Needs your call:** the **swim library** — incl. the new **drills/technique + OW-skills** rows
  (drill menu, base-phase technique weighting, OW-skill progression), CSS structures/rest, 5k-swim.
- The progression *numbers* (3×10→4×10→2×20 etc.) are reasonable but were **not** published
  prescriptions — your coaching judgment overrides freely.

## 8. Next step
On your red-line, I encode this as `config/session-library.json` (per discipline: session types
with parameterised segments + per-phase progression index; per event: TID + periodisation +
emphasis + brick policy), which Layer 1/2 consume to instantiate sessions. Then the planner
*selects and parameterises from this*, instead of the LLM inventing intervals.
