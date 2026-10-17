# Strength Library & Session-Building Methodology (DRAFT for red-line)

Status: **draft for Jamie's red-line** · 17 Oct 2026 · feeds `config/exercise-library.json`.
Builds on `blueprints/strength.md` (signed off 10 Jun 2026). **Nothing here is live** until you sign it off.

| In one look | |
|---|---|
| **What it is** | 40 exercises, 5 modules (run, bike, swim, triathlon, health), an **athlete-issues layer**, one session template, doses by phase, 6 example sessions |
| **What stays** | Everything the blueprint decided: 2/wk cap, RPE not 1RM, tiers A/B/C, A/B sessions, phase table, scheduling rules |
| **What changes** | Upper body added at **no extra time**, a 3-min **power block**, a **sport block** per sport, a **learning block** for new lifters, and **each athlete's own issues** (§9) |
| **What you decide** | 8 decisions in §13. The top 3: upper body for all, the peak/taper planner gap, plyometrics for all |

---

## 1. How to read this

- **Confidence tags:** ✅ verified (research, cited) · 🟡 extrapolated from verified anchors · 🔴 weak evidence / needs your judgment. **Red-line the 🟡 and 🔴 first.**
- Every ✅ has author, year and a link. **I checked each link resolves** (17 Oct).
- **RPE** uses the reps-in-reserve scale: **RPE 8 = about 2 reps left**, RPE 7 = about 3. ✅ Zourdos et al. 2016, [doi:10.1519/JSC.0000000000001049](https://doi.org/10.1519/JSC.0000000000001049).
- Muscle names follow **ExRx's anatomical naming**. All wording is original.
- The JSON is the machine version of this doc. **If they disagree, this doc wins** until you red-line it.

---

## 2. What strength is for

Two jobs. **Make the sport faster and more durable.** Keep the athlete **healthy for life**.

### 2a. Sport performance

| Claim | Tag | Source |
|---|---|---|
| Adding **heavy or explosive** strength improves **running economy**. **Heavy** strength is the one recommended for **cycling economy** | ✅ | Rønnestad & Mujika 2014, Scand J Med Sci Sports, [doi:10.1111/sms.12104](https://doi.org/10.1111/sms.12104) |
| Female cyclists: heavy strength improved **40-min power** and **cycling economy** | ✅ | Vikmoen et al. 2016, Scand J Med Sci Sports, [PubMed 25892654](https://pubmed.ncbi.nlm.nih.gov/25892654/) |
| **1 session a week** in-season kept the gains and **raised** well-trained cyclists' performance | ✅ | Rønnestad et al. 2010, Eur J Appl Physiol, [doi:10.1007/s00421-010-1622-4](https://doi.org/10.1007/s00421-010-1622-4) |
| In-season, **1 session a week kept** strength, sprint and jump gains. **Every other week lost them** (footballers) | ✅ | Rønnestad et al. 2011, JSCR, [doi:10.1519/JSC.0b013e31822dcd96](https://doi.org/10.1519/JSC.0b013e31822dcd96) |
| Running economy: explosive about **4.8%** better, heavy about **3.7%**. **Longer programmes, bigger gains** | ✅ | Denadai et al. 2017, Sports Med, [doi:10.1007/s40279-016-0604-z](https://doi.org/10.1007/s40279-016-0604-z) |
| High-load, **plyometric** and combined training all improve running economy. Light-load and isometric work less so | ✅ | Llanos-Lagos et al. 2024, Sports Med, [doi:10.1007/s40279-023-01978-y](https://doi.org/10.1007/s40279-023-01978-y) |
| Heavy resistance beat plyometrics for running economy (small effect) | ✅ | Eihara et al. 2022, Sports Med Open, [doi:10.1186/s40798-022-00511-1](https://doi.org/10.1186/s40798-022-00511-1) |
| Highly trained runners: a **large** economy gain, but from only **5 small studies** | ✅ | Balsalobre-Fernández et al. 2016, JSCR, [PubMed 26694507](https://pubmed.ncbi.nlm.nih.gov/26694507/) |
| Below sprint speed, the **soleus and gastrocnemius** give most of the vertical support in running | ✅ | Dorn, Schache & Pandy 2012, J Exp Biol, [PubMed 22573774](https://pubmed.ncbi.nlm.nih.gov/22573774/) |
| Swim **dry-land strength**: the evidence is **thin**. Maximal strength looked best; most positive studies were in older swimmers | ✅ (that it's thin) | Amaro et al. 2019, Science & Sports, [doi:10.1016/j.scispo.2018.07.003](https://doi.org/10.1016/j.scispo.2018.07.003) |

### 2b. Injury resilience

| Claim | Tag | Source |
|---|---|---|
| **Strength training cut sports injuries to under a third** (risk ratio 0.315). Balance training 0.55. **Stretching: no effect** | ✅ | Lauersen, Bertelsen & Andersen 2014, BJSM, [doi:10.1136/bjsports-2013-092538](https://doi.org/10.1136/bjsports-2013-092538) |
| Home **balance training cut repeat ankle sprains by 35%** (22% vs 33%) | ✅ | Hupperets et al. 2009, BMJ, [doi:10.1136/bmj.b2684](https://doi.org/10.1136/bmj.b2684) |
| The **Copenhagen** exercise cut groin problems (odds 41% lower) in footballers | ✅ | Harøy et al. 2019, BJSM, [doi:10.1136/bjsports-2017-098937](https://doi.org/10.1136/bjsports-2017-098937) |
| **Heavy slow-lowering calf work** got 15 athletes with chronic Achilles pain back to running | ✅ (small, not randomised) | Alfredson et al. 1998, Am J Sports Med, [PubMed 9617396](https://pubmed.ncbi.nlm.nih.gov/9617396/) |
| Loading a sore Achilles under a **pain-monitoring rule** did no harm | ✅ | Silbernagel et al. 2007, Am J Sports Med, [doi:10.1177/0363546506298279](https://doi.org/10.1177/0363546506298279) |

### 2c. All-round health

| Claim | Tag | Source |
|---|---|---|
| UK CMOs: adults do **strength on 2+ days a week**. Older adults: **strength, balance and flexibility on 2+ days**. Impact helps **bone** | ✅ | UK Chief Medical Officers 2019, [gov.uk](https://www.gov.uk/government/publications/physical-activity-guidelines-uk-chief-medical-officers-report) |
| WHO: adults do muscle-strengthening for **all major muscle groups on 2+ days**. Over-65s add **balance + strength on 3+ days** | ✅ | Bull et al. 2020 (WHO guidelines), BJSM, [doi:10.1136/bjsports-2020-102955](https://doi.org/10.1136/bjsports-2020-102955) |
| Exercise cuts **falls in over-60s by 23%**. Balance and functional work: 24% | ✅ | Sherrington et al. 2019, Cochrane, [doi:10.1002/14651858.CD012424.pub2](https://doi.org/10.1002/14651858.CD012424.pub2) |
| **Heavy lifting + impact**, twice a week for 8 months, raised **spine and hip bone density** in post-menopausal women with low bone mass, safely | ✅ | Watson et al. 2018 (LIFTMOR), JBMR, [doi:10.1002/jbmr.3284](https://doi.org/10.1002/jbmr.3284) |
| The same approach has been trialled in **older men** with low bone mass | ✅ | Harding et al. 2020 (LIFTMOR-M), JBMR, [doi:10.1002/jbmr.4008](https://doi.org/10.1002/jbmr.4008) |
| **Road cyclists** tend to have **low bone density**, e.g. at the spine | ✅ | Olmedillas et al. 2012, BMC Med, [doi:10.1186/1741-7015-10-168](https://doi.org/10.1186/1741-7015-10-168) |
| **Swimmers'** bone density is about the same as **inactive** people's | ✅ | Gómez-Bruton et al. 2013, PLoS ONE, [doi:10.1371/journal.pone.0070119](https://doi.org/10.1371/journal.pone.0070119) |
| Each **5 kg less grip = 16% higher risk of death** (140,000 adults, 17 countries) | ✅ | Leong et al. 2015 (PURE), Lancet, [doi:10.1016/S0140-6736(14)62000-6](https://doi.org/10.1016/S0140-6736%2814%2962000-6) |
| UK Biobank (500,000 adults): **lower grip** linked to higher all-cause and heart-disease death | ✅ | Celis-Morales et al. 2018, BMJ, [doi:10.1136/bmj.k1651](https://doi.org/10.1136/bmj.k1651) |
| Older adults: lift **2-3 days a week**. Beginners 10-15 reps, strength 6-12. **Power** work explosive at 40-60% of max | ✅ | Fragala et al. 2019 (NSCA position), JSCR, [doi:10.1519/JSC.0000000000003230](https://doi.org/10.1519/JSC.0000000000003230) |

### 2d. What I could not verify (🟡 / 🔴)

| Claim used in the library | Tag | Why not ✅ |
|---|---|---|
| Strength cuts injuries **in endurance athletes** specifically | 🟡 | Lauersen's trials were mostly team sports |
| Copenhagen protects **runners' and triathletes'** groins | 🟡 | The trial was in footballers |
| **Training grip** improves health | 🟡 | Grip studies show a link, not cause. The PURE authors say so |
| Heavy + impact loading helps **healthy younger** athletes' bones | 🟡 | LIFTMOR tested older adults with low bone mass |
| Lat, cuff and shoulder-blade work **prevents swimmer's shoulder** or makes you faster | 🔴 | No trial checked. Standard practice |
| Bent-knee calf raise **targets the soleus** | 🟡 | Standard anatomy (bent knee slackens the gastrocnemius). Not checked against a study |
| Progression rule (top of range at RPE 7 or less, twice running) | 🟡 | My version of the common "2-for-2" rule, which secondary sources credit to NSCA/ACSM ([HPRC](https://www.hprc-online.org/physical-fitness/training-performance/guidelines-progress-your-physical-training-over-time)) |
| Pain up to **3/10** is OK if gone by next morning | 🟡 | A cautious version of Silbernagel's model. I couldn't confirm the paper's exact threshold |
| 4 weeks of calf work and ~25 single-leg calf raises **before hopping** | 🔴 | Coaching convention, not checked |
| Max **60 foot contacts** a session in build | 🔴 | My number |
| **No plyos** during lower-limb rehab unless the source clears them | 🟡 | A cautious default, not trial-based |
| Heavy = **4-6 reps at RPE 8** | 🟡 | Blueprint's signed-off 3×5 @ RPE 8, and the review's "heavy" label. Rep counts not checked |
| Pairing upper-body lifts into lower-body rests costs nothing | 🟡 | Different muscles, so little cross-fatigue. Not trial-checked |

---

## 3. Movement patterns and weekly coverage

**18 patterns.** Every exercise has exactly one.

| Pattern | Exercises in the 40 |
|---|---|
| squat | back_squat, goblet_squat, bodyweight_squat |
| hinge | romanian_deadlift, single_leg_rdl, kettlebell_swing |
| single_leg | bulgarian_split_squat, split_squat, step_up, reverse_lunge |
| bridge | hip_thrust, glute_bridge, single_leg_glute_bridge |
| hip_stability | banded_lateral_walk, copenhagen_plank |
| calf_ankle | calf_raise, bent_knee_calf_raise, banded_ankle_eversion |
| balance | single_leg_balance |
| plyometric | pogo_hops, squat_jump |
| push_horizontal | push_up, dumbbell_bench_press |
| push_vertical | overhead_press |
| pull_horizontal | bent_over_row |
| pull_vertical | lat_pulldown, pull_up, straight_arm_pulldown |
| scapular_cuff | band_pull_apart, band_external_rotation |
| carry | farmers_carry |
| anti_extension | dead_bug, front_plank |
| anti_rotation | pallof_press, bird_dog |
| lateral_core | side_plank |
| mobility | leg_swings, knee_to_wall, hip_flexor_stretch, thoracic_open_book |

### Weekly coverage rule (everyone, every week)

| Must cover | Met by | Tag |
|---|---|---|
| **Squat** | squat pattern | 🟡 |
| **Hinge** | hinge or bridge. From level 2, at least one true hinge | 🟡 |
| **Single-leg** | single_leg pattern, or single_leg_rdl | 🟡 |
| **Push** | push_horizontal or push_vertical | ✅ WHO "all major muscle groups" |
| **Pull** | pull_horizontal or pull_vertical | ✅ WHO "all major muscle groups" |
| **Calf / ankle** | calf_ankle pattern | 🟡 (✅ for runners via Dorn 2012) |
| **Core: anti-extension** | dead_bug, front_plank | 🟡 |
| **Core: anti-rotation** | pallof_press, bird_dog | 🟡 |
| **Core: lateral** | side_plank, copenhagen_plank, farmers_carry | 🟡 |
| **Balance** | single_leg_balance, single_leg_rdl | ✅ CMO, Sherrington |
| **Bone: impact** | pogo_hops, squat_jump (once cleared) | ✅ CMO "impact helps bone" |
| **Bone: heavy** | 1 lower-body lift at 6 reps or fewer, RPE 7+ (level 2+) | ✅ LIFTMOR (older adults) · 🟡 others |
| **Grip** | farmers_carry, pull_up, romanian_deadlift, kettlebell_swing | 🟡 |

**How two sessions cover it all:** Session A and B fill fixed slots (§5). I checked the two triathlon example sessions: together they hit every item (bone-heavy via back squat 3 × 5 @ RPE 8).

---

## 4. Sport modules and the health module

The **module decides what fills each slot**. Multi-sport athletes use **triathlon**. **Health is layered on everyone.**

| Module | Priority (first = most important) | Power A / B | Sport block A | Sport block B | Tag |
|---|---|---|---|---|---|
| **Run** | calves, single-leg, plyometric, hip stability, hinge, squat | pogo_hops / squat_jump | calf_raise, bent_knee_calf_raise, single_leg_balance | copenhagen_plank, banded_lateral_walk, calf_raise | ✅ economy + calves · 🟡 hips |
| **Bike** | squat, hinge, bridge, single-leg, trunk, plyometric | squat_jump / kettlebell_swing | bulgarian_split_squat, calf_raise | single_leg_glute_bridge, copenhagen_plank, hip_flexor_stretch | ✅ heavy for economy · ✅ bone gap |
| **Swim** | lats, straight-arm pull, cuff and shoulder blades, row, trunk | squat_jump / pogo_hops | straight_arm_pulldown, band_external_rotation, thoracic_open_book | straight_arm_pulldown, band_pull_apart, dead_bug | 🔴 thin evidence |
| **Triathlon** | squat, hinge, single-leg, calves, plyometric, lats, cuff | pogo_hops / squat_jump | as Run A | copenhagen_plank, banded_lateral_walk, straight_arm_pulldown | 🟡 blend |
| **Health** | squat, hinge, push, pull, single-leg, balance, carry, plyometric | pogo_hops / squat_jump (after week 4) | single_leg_balance, calf_raise | farmers_carry, single_leg_balance | ✅ CMO / WHO |

An athlete's **own issues** (injury, rehab, a physio programme) sit **on top of** the module. See §9.

### Module notes

| Module | What matters |
|---|---|
| **Run** | **Calves twice a week**, straight and bent knee: they carry the load. Single-leg is Session B's main lift. Plyos from build |
| **Bike** | **The heaviest module.** Heavy two-leg lifts drive cycling economy. Impact stays in because **riding doesn't load bone** |
| **Swim** | Lats and cuff first. Legs stay for health, and because **swimming gives bone little stimulus**. Big swim weeks: overhead press drops to 2 sets |
| **Triathlon** | **Bike sets the main lifts, run sets the sport block, swim lives in the warm-up and the pull slot.** So swim work costs no extra time |
| **Health** | Every major muscle group, heavy + impact for bone, balance, grip. For athletes with no sport, it **is** the programme |

---

## 5. Session template

**35-45 min, A/B alternation** (blueprint).

| Block | Min | What goes in | vs blueprint |
|---|---|---|---|
| **Warm-up** | 5 | leg_swings + 1 lower mobility drill + module warm-up adds + 1-2 ramp-up sets. Easy spin/row first if there's a machine | Kept, plus mobility and swim prep |
| **Power** | 3 | module.power[A or B], from the module's start phase. Done fresh | **New** |
| **Main** | 20 | **Two pairs**, each = 1 lower lift + 1 upper lift. The upper lift fills the lower lift's rest | Lower lifts kept. **Upper added at no extra time** |
| **Sport** | 8 | module.sport_block[A or B]. Skips anything an issue protocol already covered that week | Replaces the blueprint's fixed block |
| **Core** | 5 | 2 rounds: 1 anti-extension + 1 anti-rotation + 1 lateral. B may use farmers_carry. 7 min in base (no power block yet) | Was 7 min. Same exercises plus options |

### Slots

| Slot | Session A | Session B |
|---|---|---|
| Power | pogo-type | jump or swing |
| Pair 1 | **squat** + pull (vertical first, else row) | **single-leg** + push (overhead first, else horizontal) |
| Pair 2 | **hinge** + push_horizontal | **bridge** (or single_leg_rdl) + pull_horizontal |

The blueprint's lower-body lifts sit in exactly the same places.

### How exercises are picked (in this order)

**First:** place the athlete's **issue protocol sessions** (§9). They count toward the cap and coverage. Steps 1-4 then **only fill the gaps**.

| Step | Filter | Rule |
|---|---|---|
| 1 | **Sport module** | Sets the patterns per slot, the power pick and the sport block |
| 2 | **Equipment tier** | This week's tier. Missing kit: use `swaps[tier]`. Higher tier with an upgrade listed: use the upgrade |
| 3 | **Athlete level** | Only exercises at or below the athlete's level (1, 2, 3). Move with `easier` / `harder` |
| 4 | **Athlete issues** | For each current issue: drop its `avoid` list, block plyos if it's a lower-limb issue in rehab, and apply each exercise's cautions for that area (§9) |

**One-session week:** run Session A, swap the squat for the single-leg pick every other week, keep the sport block.

### How this maps to the planner's session types

| `session-library.json` type | Built from |
|---|---|
| `strength_general` (40 min) | The full template, A or B |
| `core` (20 min) | Core block × 3 rounds, plus copenhagen_plank and bird_dog |
| `mobility` (20 min) | Mobility drills + single_leg_balance + thoracic_open_book |
| `ankle_mobility` / `mobility` (short) | An athlete's own short issue protocol, outside the cap (§9, example 2) |

---

## 6. Dose by phase

**Blueprint phase table kept.** I've added numbers where it only had words.

| Phase | Freq | Main lift | Single-leg | Upper | Power | Core | Rule | Tag |
|---|---|---|---|---|---|---|---|---|
| **Base · intro** (level 1, 3 wks) | 2/wk | 2-3 × 8-12 @ RPE 6-7 | same | same | none | 2 rounds | Learn the moves | 🟡 **new** |
| **Base** | 2/wk | 3 × 5-6 @ RPE 8 | 3 × 6-8/side @ 8 | 2-3 × 8-12 @ 7 | pogo 2 × 10 from wk 4 | 2 rounds, 5-7 min | Add load weekly | Blueprint ✅ kept |
| **Build** | 2/wk | 3 × 4-6 @ RPE 8, fast up | 3 × 6-8/side @ 8 | 3 × 6-10 @ 7-8 | 3 sets, ≤60 contacts | 2 rounds | Add load weekly | Blueprint kept · power new |
| **Specific** | 2/wk | **2** × 4-6 @ 7-8 | 2 × 6-8/side | 2 × 6-10 @ 7 | 2 sets, ≤40 contacts | 2 rounds | **Hold load, nothing new** | Blueprint kept exactly |
| **Peak** | 1-2/wk | 2 × 3-5 @ 7 | 2 × 5/side | 1-2 × 6-8 | 2 × 3-5 jumps | 1-2 rounds | Same load, fewer reps | Blueprint kept · ✅ Rønnestad 2010 |
| **Taper** | 1/wk light, **none in race week** | 1-2 × 3 @ 6 | 1 × 4/side | 0-1 set | 1-2 × 3 jumps | 1 round | Neuromuscular touch only | Blueprint kept |
| **Off-season** | 2/wk | 3-4 × 4-8 @ 8 | 3 × 6-10/side | 3 × 8-12 | 3 sets, ≤80 contacts | 2-3 rounds | Biggest strength block of the year | 🟡 **new** |

| Rest | Time |
|---|---|
| Main lifts | **~2.5 min** (the paired upper lift fills it) |
| Single-leg | 60-90 s after both legs |
| Upper (when not paired) | 60-90 s |
| Calf / prehab / core | 15-45 s |

**Taper rule:** no slow-lowering (eccentric) emphasis. It causes soreness. **Never train to failure, any phase.**

---

## 7. Equipment tiers and swaps

**Tiers match the blueprint exactly.** Equipment is still **asked every week**, Tier C by default.

| Tier | Kit | Main lifts (blueprint) |
|---|---|---|
| **A** full gym | barbell, rack, bench, lat pulldown, pull-up bar + all of B | back_squat, romanian_deadlift (barbell), hip_thrust (barbell) |
| **B** dumbbells / kettlebells | dumbbells or kettlebells + all of C. **No bench assumed** | goblet_squat, single_leg_rdl (DB), split_squat (DB), kettlebell_swing |
| **C** bodyweight + band (default) | band, mini band, step, chair, wall, mat | bulgarian_split_squat, single_leg_rdl, step_up, hip_thrust (banded) |

### Swap rule

`swaps[tier]` = the exercise to use on that tier. **Missing tier = keep this exercise.**

| If the plan says | Tier B gets | Tier C gets |
|---|---|---|
| back_squat | goblet_squat | bulgarian_split_squat |
| romanian_deadlift | (itself, dumbbells) | single_leg_rdl |
| kettlebell_swing | (itself) | single_leg_rdl |
| lat_pulldown / pull_up | bent_over_row | straight_arm_pulldown |
| dumbbell_bench_press | (itself, or floor press) | push_up |
| overhead_press | (itself) | push_up |
| bent_over_row | (itself) | band_pull_apart |
| farmers_carry | (itself) | **no swap**: grip gap at tier C |

| Upgrades (higher tier) | |
|---|---|
| bodyweight_squat → goblet_squat | at A or B |
| glute_bridge → hip_thrust | at A or B |
| push_up → dumbbell_bench_press | at A |

**Kit gaps to ask about:** a **band** (needed for 7 exercises), a **pull-up bar** (doorway bars count), a **bench** (else floor press). See decision 8.

---

## 8. Progression and regression

### When to move

| Signal | Action | Tag |
|---|---|---|
| Every set at the **top of the rep range** at **RPE 7 or less**, **2 sessions running** | Add the smallest load step (next dumbbell, or 2.5 kg on a bar). Restart at the bottom of the range | 🟡 |
| Can't add load (tier C, heaviest dumbbell) | Add reps → slow the lowering to 3-4 s → move to `harder` | 🟡 |
| Reps below range, or **RPE 9+**, **2 sessions running** | Drop 5-10% load, or move to `easier` | 🟡 |
| Pain **above 3/10**, or still there next morning | Move to `easier` or swap (§9) | 🟡 |

### The ladders (within the 40)

| Family | Easier → harder |
|---|---|
| Squat | bodyweight_squat → goblet_squat → back_squat |
| Single-leg | bodyweight_squat → step_up → split_squat → reverse_lunge → bulgarian_split_squat |
| Hinge | glute_bridge → romanian_deadlift → single_leg_rdl · romanian_deadlift → kettlebell_swing |
| Bridge | glute_bridge → single_leg_glute_bridge → hip_thrust |
| Calf → plyo | calf_raise → pogo_hops → squat_jump |
| Vertical pull | lat_pulldown → pull_up |
| Anti-extension | dead_bug → front_plank |
| Anti-rotation | bird_dog → pallof_press |
| Lateral | side_plank → copenhagen_plank |

**No ladder** (progress by load, reps or the note's variations): push, overhead press, row, carries, cuff work, balance, mobility.

---

## 9. Athlete-specific issues

**Every athlete can carry their own issues**: an injury, a rehab programme, something they manage themselves. The library is the same for everyone. **The issue layer makes it fit one person.** JSON: the `issues` block.

### 9a. Where issues live

They live in the profile's existing **`injuries[]`** list. **I extend it. No new store.**

| Field | New? | What it holds |
|---|---|---|
| `location` | existing | Free text. The debrief asks about it (`lib/strength_ask.py`) |
| `area` | **new** | knee, ankle, hip, low_back, shoulder, other. Picks which exercise cautions apply |
| `side` | **new** | left, right, both, none |
| `description` | existing | Free text |
| `status` | existing | **active, rehab, managed, resolved**. Old "cleared" reads as resolved |
| `source` | existing | physio, doctor, self, coach |
| `priority` | **new** | **first** = protocol sessions go in before any other training that week. Default normal |
| `protocol` | existing, **extended** | Still allows plain text. Now can also be `{summary, sessions: [{title, per_week, items}]}` |
| `protocol` items | **new** | `ex` (library id) **or** `name` (free text), plus sets, reps, load, note |
| `avoid` | **new** | Exercise ids never to prescribe while the issue is current |
| `plyo_cleared` | **new** | Only the source sets this. Default false |
| `review_by` | **new** | Date to check in on the issue |
| `physio_allowance`, `ramp_state` | existing | Run/bike quality caps (`lib/injury.py`). Unchanged |

**Build note:** `lib/athlete_brief.py` prints `protocol` as text. When it's an object, it must print `protocol.summary`.

### 9b. Rules

| Rule | What it means |
|---|---|
| **Priority first** | The protocol's sessions go in first. Swim, bike and run fit round them |
| **Counts toward the cap** | Protocol strength sessions count toward the 2/week cap. Two of them fill it |
| **Counts toward coverage** | Protocol items count toward the weekly coverage list (§3) |
| **Top up only** | The builder adds **only what's missing**: usually upper body + core, 10-20 min, added to a protocol session. **Never a competing lower-body session** |
| **Never edit the source** | Never change a physio's or doctor's exercises, sets, reps or loads. The coach may **only point out a conflict** and suggest the athlete checks with the source |
| **Self-managed can progress** | Source self or coach: the normal progression rules (§8) apply |
| **Cautions apply automatically** | Every library exercise's caution for the issue's area applies: follow it, or swap |
| **Avoid list** | Never prescribed while the issue is current |
| **No plyos in lower-limb rehab** | Knee, ankle or hip issue that is active or in rehab: **no plyometrics** unless the source clears them. Managed issues don't block. 🟡 |
| **Free-text exercises allowed** | Shown **without an animation**. Added to the library candidates list (§9f) |
| **Debrief** | Asks about the issue area. Already built (`lib/strength_ask.py`) |
| **Review date** | On the `review_by` date, ask for an update. Never let an issue expire silently |
| **Short sessions outside the cap** | Balance or mobility work with `counts_as_strength: false` uses the `mobility` or `ankle_mobility` session type |

### 9c. Worked example 1: knee ligament rehab (physio-led)

> An athlete recovering from **knee ligament surgery**. The physio sets **one hamstring session and one quad session** every week. **Strength comes first; bike fits round it.**

Issue: area knee · status **rehab** · source **physio** · priority **first** · plyo_cleared **false**.

| Hamstring session | In library? | Quad session | In library? |
|---|---|---|---|
| Single-leg deadlift | single_leg_rdl | Barbell back squat | back_squat |
| Hip thrust | hip_thrust | Bulgarian split squat | bulgarian_split_squat |
| Single-leg hamstring curl (machine) | **free text** | Single-leg leg extension (machine) | **free text** |
| Single-leg calf raise | calf_raise | Lateral step-down | **free text** |
| Single-leg hamstring bridge, foot raised | **free text** | Copenhagen plank | copenhagen_plank |

**Loads: set by the physio.** The library never copies or changes them.

**What the builder does:**

| Step | Result |
|---|---|
| Cap | The 2 physio sessions **fill the 2/week cap**. No library strength session is added |
| Covered by the physio | **squat, hinge, single-leg, bridge, calf, adductors**, lateral core, balance |
| Missing | push, pull, anti-extension and anti-rotation core, bone impact, grip |
| Top-up (15 min, added to a physio session) | dumbbell_bench_press + lat_pulldown (paired), dead_bug, pallof_press |
| Blocked | **pogo_hops, squat_jump**: knee in rehab, not cleared |
| Grip | No carries (walking load on the knee). Gap until the physio says otherwise |
| Conflict to flag | A hard ride within 8 h of a physio session. **Flag it; never move the physio session** |
| Debrief | "How did the knee feel - any pain or swelling during or after?" |

### 9d. Worked example 2: old ankle sprain (self-managed)

> A **runner with an old ankle sprain**, self-managed.

Issue: area ankle · status **managed** · source **self** · priority normal.

| Item | Dose |
|---|---|
| calf_raise | 3 × 12/side, 3 s lowering |
| single_leg_balance | 3 × 30-45 s/side, reach or a gentle push |
| banded_ankle_eversion | 2 × 15-20/side, light band |
| knee_to_wall | 1 × 10/side |

| Step | Result |
|---|---|
| Cap | **Outside the cap**: a 15-min `ankle_mobility` session, 3 times a week. The 2 strength sessions stay |
| Coverage | Counts toward calf and balance, so the run sport block can lean on soleus and hips |
| Plyos | **Allowed**: managed, not rehab |
| Cautions | Ankle cautions apply (hold a wall on single-leg work, firm flat floor for hops) |
| Progression | Source is self, so normal progression applies |

**This is where the old ankle content now lives:** one athlete's issue, not a library rule.

### 9e. Cautions by area

Each exercise carries cautions **only for the areas it affects**. They switch on automatically for an athlete's issue areas.

| Area | Avoid or modify | Swap to | Tag |
|---|---|---|---|
| **Knee** | Deep loaded knee bend if painful; jump landings | Shorter range, box squat depth, step_up on a low step, hip-led work (romanian_deadlift, hip_thrust), reverse not forward lunges | 🟡 |
| **Ankle** | Hops on uneven ground; unsupported single-leg work early on | Hold a wall; low two-foot hops only | 🟡 |
| **Hip** | Deep hip bend with a pinch at the front; big lunges; groin pain on the Copenhagen | Stop above the pinch, wider stance, shorter steps, side_plank instead of copenhagen_plank | 🟡 **new** |
| **Low back** | Bar on the back; ballistic swings in a flare-up; loaded rounding | goblet_squat / bulgarian_split_squat, hip_thrust / single_leg_glute_bridge, supported one-arm row, dead_bug, bird_dog, side_plank | 🟡 |
| **Shoulder** | Overhead pressing if painful; behind-neck pulls; very high reach | push_up with tucked elbows, palms-facing grips, lat_pulldown over pull_up, band_external_rotation | 🟡 |
| **Achilles** (area: ankle) | Fast bouncing reps when sore | Flat-ground calf raises, slow lowering, pain 3/10 or less | ✅ Alfredson, Silbernagel · 🟡 threshold |
| **Other** | No automatic cautions | The issue's `avoid` list and the source's protocol | n/a |

**Stop and refer** (not a library job): sharp or worsening pain, swelling, pain at night or at rest, giving way. 🟡

### 9f. Next library candidates

The four free-text exercises from example 1 are **first in line** for the next batch:

| # | Exercise | Pattern it would add |
|---|---|---|
| 1 | Single-leg hamstring curl (machine) | knee flexion (new pattern) |
| 2 | Single-leg leg extension (machine) | knee extension (new pattern) |
| 3 | Lateral step-down | single_leg |
| 4 | Single-leg hamstring bridge, foot raised | bridge |

---

## 10. Beginners and masters athletes

| Group | Rule | Tag |
|---|---|---|
| **Beginners (level 1)** | 3-week learning block (8-12 reps, RPE 6-7). Level-1 exercises only | 🟡 (✅ NSCA for older beginners) |
| | **No plyometrics for 4 weeks**, then pogo 2 × 10 | 🔴 |
| | First 2 sessions: 2 sets, technique first | 🟡 |
| **Masters** | **Same programme.** Heavy strength still works | ✅ Fragala 2019 |
| | Keep **heavy + impact** in (bone), and **balance** every week (falls) | ✅ LIFTMOR, Sherrington |
| | Power work stays: explosive intent at moderate load (squat jumps, swings) | ✅ Fragala 2019 |
| | Longer warm-up (add 2 min). Plyo volume at the low end of the phase range | 🟡 |
| **Women peri / post-menopause** | Prioritise heavy lifts and impact | ✅ LIFTMOR (women with low bone mass) |
| **Over 65** | WHO says balance + strength on **3+ days**. We cap strength at 2 | ✅ WHO · see decision 5 |

---

## 11. Scheduling and interference

**All existing rules kept.** The evidence backs them.

| Rule (blueprint) | Evidence | Tag |
|---|---|---|
| Max **2 sessions a week** (`strength_max`) | 2 a week meets CMO and WHO. 1 a week maintains gains | ✅ CMO, WHO, Rønnestad 2010/2011 |
| Slot 1 = Wednesday spare slot | Planner rule | kept |
| Slot 2 = after a swim day (Tue/Thu) | Swimming loads the legs least | 🟡 |
| **Never the day before the long ride**; **8+ h from quality bike/run** | 0 h apart blunts strength gains; 6-24 h is better | ✅ Robineau 2016 |
| Strength **never displaces** swim, bike or run to hit TSS | Planner rule. Strength is the support act: adding it **helps** endurance performance, and interference mainly hits **power** gains | ✅ Rønnestad & Mujika 2014, Wilson 2012 · kept |
| Power block counts as strength | Same slot, same rules. Not before a quality run | 🟡 |
| Issue protocols with priority **first** | Placed before swim, bike and run. They count toward the cap | rule (§9) |
| Compliance flag (T11) | Unchanged | kept |

**Planner gap:** `session-library.json` peak and taper menus **don't include `strength_general`**. The blueprint says peak 1-2/wk and taper 1/wk. So today the planner **can't place** the strength the blueprint asks for. See decision 2.

---

## 12. What changes vs today's programme

| | Item | Detail |
|---|---|---|
| **Kept** | 2/wk cap, 35-45 min | Unchanged |
| **Kept** | RPE, no 1RM tests | Unchanged |
| **Kept** | Equipment asked weekly, Tier C default | Unchanged |
| **Kept** | Tier A/B/C main-lift mapping | Same lifts, now exercise ids |
| **Kept** | A/B lower-body lifts | A: squat + hinge. B: single-leg + hip thrust |
| **Kept** | Core exercises | side plank, Pallof press, dead bug, 2 rounds |
| **Kept** | Phase table and scheduling rules | All of them |
| **Changed** | Upper body | Legs-only → **push + pull every session**, paired into rests |
| **Changed** | Fixed block after the main lifts | Now a **sport block** per module. Anything specific to one athlete moves to the **issues layer** (§9) |
| **Changed** | Core block | 7 → 5 min once the power block starts. More exercise options |
| **Changed** | Warm-up | Adds mobility drills and swim shoulder prep |
| **Changed** | Doses | Words → numbers per phase. Tier C rep shift kept (8-12) |
| **New** | 40-exercise library | Patterns, muscles, levels, ladders, swaps, cues, cautions |
| **New** | Power block | 3 min of plyos from build |
| **New** | Sport and health modules | Run, bike, swim, triathlon, health |
| **New** | Athlete-issues layer | Extends the profile's `injuries[]`: structured protocols, counted toward cap and coverage, avoid list, plyo block (§9) |
| **New** | Library candidates | 4 exercises from a rehab protocol, first in line for the next batch (§9f) |
| **New** | Weekly coverage rule | §3 |
| **New** | Progression / regression rules | §8 |
| **New** | Learning block for new lifters | §6 |
| **New** | Off-season dose | The planner's offseason phase had no strength dose |
| **New** | Example sessions | 6, all built from the rules |

**Stale pointer:** `templates/session-library.md` points to `reference/run-form-and-strength.md`, which **isn't in the repo**. This library would replace it.

---

## 13. Decisions for Jamie

### 1. Upper body for every athlete?
The blueprint is lower body only. WHO says **all major muscle groups**.

| Option | Impact |
|---|---|
| **a. Yes, everyone** (push + pull paired into rests) | **No extra time.** Meets WHO/CMO. Swimmers get lats and cuff. Slightly more fatigue |
| b. Swimmers, triathletes and health-only athletes | Runners and cyclists miss the health benefit |
| c. Keep legs-only | Simplest. "All-round health" claim doesn't hold |

**Recommend a.** It's free in time and it's what "all-round health" means.

### 2. Fix the peak / taper planner gap?
The planner's peak and taper menus have **no `strength_general`**. The blueprint wants strength in both.

| Option | Impact |
|---|---|
| **a. Add it to both** with the §6 doses | Matches the blueprint. 1/wk keeps gains (✅ Rønnestad 2010) |
| b. Peak only | Taper goes strength-free. Small detraining risk |
| c. Leave it | Strength stops after specific. Contradicts the signed-off blueprint |

**Recommend a.** It's a menu edit plus a test.

### 3. Plyometrics (power block) for everyone?

| Option | Impact |
|---|---|
| **a. Everyone from build**, after 4 weeks of calf work, contact caps per phase | Better running economy (✅). Bone loading for all (✅). Small injury risk if overdone |
| b. Runners and triathletes only | Cyclists and swimmers miss the bone benefit they most need (✅ Olmedillas, Gómez-Bruton) |
| c. None | Simplest. Loses both benefits |

**Recommend a.** Separately, a lower-limb issue in rehab blocks plyos until its source clears them (§9). That's a rule, not part of this choice.

### 4. Learning block for new lifters?
The blueprint starts at 3×5 @ RPE 8 from day one.

| Option | Impact |
|---|---|
| **a. 3-week block (8-12 reps, RPE 6-7) for level-1 athletes only** | Safer start for beginners. Experienced lifters unchanged |
| b. Everyone | Costs experienced lifters 3 weeks |
| c. None | Beginners go heavy before learning the lifts |

**Recommend a.**

### 5. Over-65s: a third balance day?
WHO says over-65s do balance + strength on **3+ days**. Our cap is 2 strength sessions.

| Option | Impact |
|---|---|
| **a. Add a 10-min balance + mobility session** (`mobility` type, outside the cap) | Meets WHO. No extra strength load |
| b. Fold balance into the 2 sessions only | Meets CMO (2 days), not WHO (3) |
| c. Raise the cap to 3 for over-65s | More load on older athletes. Changes a validator rule |

**Recommend a.** 🔴 your judgment on the age line (65, or masters 50+?).

### 6. Issue protocols in taper and race week?
The blueprint says **light strength in taper, none in race week**. An athlete's own protocol may say something different.

| Option | Impact |
|---|---|
| **a. Follow the source. If it says nothing: keep rehab sessions, drop the top-ups** | Rehab never stalls for a race. Library extras go quiet |
| b. Drop everything in race week, protocol included | Clean rule. Can break a physio's programme |
| c. Keep everything as normal | Top-ups add fatigue in race week |

**Recommend a.** Managed (self-run) protocols follow the normal taper doses.

### 7. Pain rule?

| Option | Impact |
|---|---|
| **a. Up to 3/10 during, gone by next morning = OK** | Athletes keep training through minor niggles. Cautious version of Silbernagel (✅ no harm at its threshold) |
| b. Any pain = stop and swap | Safer-sounding. Lots of needless swaps |

**Recommend a.** A source's own pain rule always wins (§9).

### 8. Extend the equipment ask?
The weekly ask is A/B/C. The library also needs a **band**, a **pull-up bar** and a **bench**.

| Option | Impact |
|---|---|
| **a. Ask the 3 extras once, store them, re-ask only the tier weekly** | One extra question, once. Unlocks 7 band exercises and pull-ups |
| b. Assume every tier has a band | Band exercises fail silently for athletes without one |
| c. Ask all of it weekly | More nagging every Sunday |

**Recommend a.**
