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

## 0. Zone models — canonical definitions & mapping (READ FIRST)

Three zone models appear in the sources; mixing them silently inverts prescriptions (e.g.
"minimise Z2" means opposite things). **The library standardises like this:**

- **TID / intensity-distribution targets use the 3-ZONE model** (this is how polarized/pyramidal
  are *defined*): **LOW** = below LT1/VT1 (all easy aerobic) · **MOD** = the *grey zone* between
  LT1 and LT2 (tempo) · **HIGH** = above LT2 (threshold + VO2). When this doc writes a TID triplet
  it means **LOW / MOD / HIGH**, not device zones.
- **Session prescriptions use the COGGAN / intervals.icu zone model** (what the athlete + Garmin
  see): Z1 recovery · **Z2 endurance (60–75% FTP) — the GOOD base zone** · Z3 tempo · Z4 threshold
  · Z5 VO2.
- **Mapping (the bridge):** 3-zone **LOW** ≈ Coggan **Z1–Z2** · 3-zone **MOD/grey** ≈ Coggan **Z3 +
  low sweetspot** · 3-zone **HIGH** ≈ Coggan **Z4–Z5+**.

So Muñoz's "minimise grey-zone Z2 bike" = **minimise Coggan Z3 tempo / junk sweetspot**, while
**maximising Coggan Z2 endurance**. The blueprint's `78% Z1–2 / 14% Z3 / 8% Z4–5` is already a
Coggan-grouped distribution; the validator/audit must compare like-for-like in ONE model.
**[needs your confirm: standardise on Coggan/ICU zones as canonical, TID expressed as LOW/MOD/HIGH?]**

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

| Type | Intensity | Structure → progression (base→build→peak) | Recovery | Notes |
|---|---|---|---|---|
| **VO2max** | 106–120% FTP | 5×3min → 6×3min → 5×4min | 1:1–2:1 (≈3min) | ✅ zone/dur; 🟡 progression. Longer bouts > micro-intervals for time-at-VO2. |
| **Threshold/LT** | 91–105% FTP | 3×10 → 4×10 → 3×15 → 2×20min | short, 3–5min | ✅ zone/dur (Coggan "10–30min repeats"); 🟡 progression |
| **Sweetspot** | 88–94% FTP | 3×12 → 2×20 → 2×25 → 3×20min | 5–8min | 🟡 aerobic-builder, base/build; high TSS-per-hour |
| **Race-pace (long-course)** | race watts (IM ~70–76%, 70.3 ~80–85% FTP) | 2×30 → 3×30 → 2×45min within long ride | 10min Z2 | 🟡 specificity work |
| **Endurance** | 60–75% FTP (Z2) | continuous; grow duration to event anchor | — | the long ride |

## 3. Run session library (Daniels VDOT) ✅ structures verified; 🟡 progressions

| Type | Intensity | Structure → progression | Recovery | Cap (✅) |
|---|---|---|---|---|
| **VO2max (I)** | ~95–100% vVO2max (~3–5k pace) | 4×3min → 5×3 → 6×3min | 1:1 (≈3min jog) | ≤ lesser of 8% wk mileage / 10k. Canonical: **4×3min @95% vVO2max, 3min rec** ✅ |
| **Threshold (T)** | ~1hr race pace (~tempo) | 20min steady → 2×15 → 3×10 (cruise) | short (1min) | ~10% wk mileage ✅ |
| **Reps (R, economy)** | mile/1500 pace | 6–8×400m → 10×400 / ≤2min bouts | full recovery | ≤ lesser of 5% mileage / 5mi ✅ |
| **Race-pace/specific** | goal-race pace | marathon: 2×4mi→3×4mi @ MP; HM: 3×3km @ HMP | float | 🟡 event-specific |
| **Long** | Z2; long-run progression rule (existing) | grow 10–15%/wk to event need | — | ankle guard applies |

## 4. Swim session library (%CSS) 🔴 NEEDS YOUR RED-LINE (weakest evidence)

The research did **not** verify concrete CSS structures/rest — these are my best-practice draft;
please correct. Also: swim renders in **time** today (ICU reads "400m" as minutes) — I'll fix
distance→time via CSS pace so sets read as distance.

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
- ⚠️ **Confirm first (§0):** standardise on Coggan/ICU zones as canonical, with TID as LOW/MOD/HIGH?
  This resolves the 3-zone vs 5/7-zone ambiguity you flagged and must be settled before encoding.
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
