# Strength Library & Session-Building Methodology (DRAFT for red-line)

Status: **decisions taken 17 Oct; one item open (§13)** · 17 Oct 2026 · feeds `config/exercise-library.json`.
Builds on `blueprints/strength.md` (signed off 10 Jun 2026). **Nothing here is live** until you sign it off.

| In one look | |
|---|---|
| **What it is** | 44 exercises, 5 modules (run, bike, swim, triathlon, health), an **athlete-issues layer**, one session template, doses by phase, 6 example sessions |
| **What stays** | Everything the blueprint decided: 2/wk cap, RPE not 1RM, tiers A/B/C, A/B sessions, phase table, scheduling rules |
| **What changes** | Upper body added at **no extra time**, a 3-min **power block**, a **sport block** per sport, a **learning block** for new lifters, **each athlete's own issues** (§9), **logged weights and reps** with a one-tap effort answer that sets next time's weight (§8), and **any order in a busy gym** (§5) |
| **Decided** | 8 decisions taken 17 Oct (§13). **One open:** issue protocols in taper and race week |

---

## 1. How to read this

- **Confidence tags:** ✅ verified (research, cited) · 🟡 extrapolated from verified anchors · 🔴 weak evidence / needs your judgment. **Red-line the 🟡 and 🔴 first.**
- Every ✅ has author, year and a link. **I checked each link resolves** (17 Oct).
- **RPE** uses the reps-in-reserve scale: **RPE 8 = about 2 reps left**, RPE 7 = about 3. Athletes never see RPE: they tap **easy / right / hard** (§8b). ✅ Zourdos et al. 2016, [doi:10.1519/JSC.0000000000001049](https://doi.org/10.1519/JSC.0000000000001049).
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
| Double progression driven by the effort answer (§8c) | 🟡 | A standard method, close to the "2-for-2" rule that secondary sources credit to NSCA/ACSM ([HPRC](https://www.hprc-online.org/physical-fitness/training-performance/guidelines-progress-your-physical-training-over-time)). The exact steps are mine |
| Athletes can rate effort within about 1 rep | ✅ for trained lifters, 0-3 reps from failure | Zourdos et al. 2016 (above). Less sure for beginners 🟡 |
| Pain up to **3/10** is OK if gone by next morning | 🟡 | A cautious version of Silbernagel's model. I couldn't confirm the paper's exact threshold |
| A **4-week calf lead-in** before plyos for beginners and lower-limb issues | 🔴 | Coaching convention, not checked |
| Max **60 foot contacts** a session in build | 🔴 | My number |
| **No plyos** during lower-limb rehab unless the source clears them | 🟡 | A cautious default, not trial-based |
| Heavy = **4-6 reps at RPE 8** | 🟡 | Blueprint's signed-off 3×5 @ RPE 8, and the review's "heavy" label. Rep counts not checked |
| Pairing upper-body lifts into lower-body rests costs nothing | 🟡 | Different muscles, so little cross-fatigue. Not trial-checked |

---

## 3. Movement patterns and weekly coverage

**20 patterns.** Every exercise has exactly one.

| Pattern | Exercises in the 44 |
|---|---|
| squat | back_squat, goblet_squat, bodyweight_squat |
| hinge | romanian_deadlift, single_leg_rdl, kettlebell_swing |
| single_leg | bulgarian_split_squat, split_squat, step_up, reverse_lunge, lateral_step_down |
| bridge | hip_thrust, glute_bridge, single_leg_glute_bridge, raised_single_leg_hamstring_bridge |
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
| knee_flexion | single_leg_hamstring_curl **(new)** |
| knee_extension | single_leg_leg_extension **(new)** |

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
| **Balance** | **Every session**, all ages: a short single_leg_balance item (decided 17 Oct) | ✅ CMO, Sherrington |
| **Bone: impact** | pogo_hops, squat_jump, from build (§5) | ✅ CMO "impact helps bone" |
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
| **Warm-up** | 5 | leg_swings + 1 lower mobility drill + module warm-up adds + **a short balance item** + 1-2 ramp-up sets. Easy spin/row first if there's a machine | Kept, plus mobility, balance and swim prep |
| **Power** | 3 | module.power[A or B], **from build**. Who starts straight away: see below. Done fresh | **New** |
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

### Who starts plyos when (decided 17 Oct)

| Athlete | Plyos |
|---|---|
| Level 2+ **or** already does an impact sport (running, team sport), **and** no active or rehab lower-limb issue | **Straight away** from build |
| Level 1 with no impact sport | **4-week calf lead-in** first (calf_raise, bent_knee_calf_raise) |
| Active or rehab lower-limb issue | Lead-in, **and** blocked until the source clears them (§9) |
| No event (no phases) | Start once any lead-in is done |

### Balance in every session (decided 17 Oct)

**Every athlete, every session, all ages:** single_leg_balance, 1-2 × 30 s per side, in the warm-up. Skipped if the session already has it. **Comfortable, no strain.**

### Order is flexible in the gym (decided 17 Oct)

The listed order is **a suggestion**. If kit is busy, **do any exercise next**.

| Soft rule | Why |
|---|---|
| Power and plyos first | They need you fresh |
| Heavy main lifts before their accessories, when possible | Best quality on the lifts that matter most |
| Core last | A tired trunk makes heavy lifts riskier |
| Pairs can be split | Do the sets with plain rest if the partner's kit is taken |
| Rest still applies | Between sets of the same exercise |

Each example-session part is marked **order: any** or **suggested**. Each item has an **alt**: a same-job swap for when that kit is taken (none for kit-free exercises).

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
| **Base** | 2/wk | 3 × 5-6 @ RPE 8 | 3 × 6-8/side @ 8 | 2-3 × 8-12 @ 7 | none (lead-in athletes do calf work) | 2 rounds, 5-7 min | Progress per §8c | Blueprint ✅ kept |
| **Build** | 2/wk | 3 × 4-6 @ RPE 8, fast up | 3 × 6-8/side @ 8 | 3 × 6-10 @ 7-8 | **starts here**: 3 sets, ≤60 contacts | 2 rounds | Progress per §8c | Blueprint kept · power new |
| **Specific** | 2/wk | **2** × 4-6 @ 7-8 | 2 × 6-8/side | 2 × 6-10 @ 7 | 2 sets, ≤40 contacts | 2 rounds | **Hold load, nothing new** (progression paused) | Blueprint kept exactly |
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
| **A** full gym | barbell, rack, bench, lat pulldown, pull-up bar, leg curl and leg extension machines + all of B | back_squat, romanian_deadlift (barbell), hip_thrust (barbell) |
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
| single_leg_hamstring_curl | raised_single_leg_hamstring_bridge | raised_single_leg_hamstring_bridge |
| single_leg_leg_extension | lateral_step_down | lateral_step_down |

| Upgrades (higher tier) | |
|---|---|
| bodyweight_squat → goblet_squat | at A or B |
| glute_bridge → hip_thrust | at A or B |
| push_up → dumbbell_bench_press | at A |

**Asked once and stored** (decided 17 Oct): a **band** (needed for 7 exercises), a **mini band**, a **pull-up bar** (doorway bars count), a **bench** (else floor press). Only the tier is asked weekly.

---

## 8. Logging, effort and progression

**Jamie, 17 Oct:** "Make sure you log weights and reps", "be clear on how to build", and say "what should be comfortable". **Logs + one tap → next session's suggestion.**

### 8a. What gets logged

Every exercise has a **`log`** type.

| Type | Logged every set | Used for |
|---|---|---|
| **weight_reps** | kg + reps (0 kg = bodyweight) | Barbell, dumbbell, machine lifts, loaded single-leg work |
| **reps** | reps (kg too if load is added) | Bodyweight: push-ups, bridges, plyos, dead bugs |
| **seconds** | seconds (kg too if something is held) | Planks, balance, carries |
| **band_reps** | band + reps (cable logs kg) | Band exercises |

| Rule | Detail |
|---|---|
| **Per side** | One-leg and one-arm exercises log **left and right separately**. Loads can differ (common in rehab) |
| Ramp-up sets | Logged, ignored by progression |
| Different kit | Log what was used (a band instead of a weight) |
| Skipped | Logged as skipped. Next suggestion stays the same |

### 8b. The effort answer (one tap after each exercise)

**"How did that feel?"** Asked once per exercise, not per set. Not asked for warm-ups, mobility or balance.

| Answer | Label the athlete sees | RPE | Next time |
|---|---|---|---|
| **easy** | Could have done 4+ more | 6 or less | **Add** |
| **right** | 2-3 more in the tank | 7-8 | **Build** |
| **hard** | 0-1 more, or form slipped | 9-10 | **Hold or drop** |

**Most working sets should feel "right".** Main lifts sit at its hard end: about 2 left. Every exercise has an **`effort`** line saying how it should feel, e.g. "Last 2 reps hard but clean: you could do 2 more." **Warm-ups, mobility and balance: "Comfortable, no strain."**

### 8c. How to build (double progression)

| Answer | Weighted (`load_step_kg` > 0) | Bodyweight (`load_step_kg` = 0) | Band |
|---|---|---|---|
| **easy** | Add the load step, same reps | +2 reps a set (holds +10 s). At the top: the `harder` exercise | Next band up |
| **right**, below top of range | Same weight, **+1 rep a set** | +1 rep a set (holds +5 s) | +1 rep a set |
| **right**, top of range on every set | Add the load step, back to the bottom of the range | The `harder` exercise | Next band up, bottom of range |
| **hard**, reps in range | Same weight, same target | Same | Same band |
| **hard**, reps below range or form broke | **Drop about 10%** (rounded to the step) | The `easier` exercise | Band below |
| **hard** twice running | `easier` exercise, or drop 10% | `easier` | `easier` |

| Rule | Detail |
|---|---|
| **Load steps** | 2.5 kg barbell and machines, 2 kg dumbbells, 4 kg kettlebells, 0 for bodyweight, band and time |
| **Per side** | Each side progresses on its own |
| **First time** | Every exercise has a **`start`** line: how to pick a weight with no history. That first log is the baseline |
| **Plain English** | Every exercise has a **`progress`** line, e.g. "When every set reaches 10 with 2 left in the tank, take the next dumbbell up and drop back to 6." |
| **No weights this week** | Tier C week: the load step counts as 0 |
| **Phases** | Active in base, build and off-season. **Paused** in specific, peak and taper (hold load); "hard" still drops |
| **Physio or doctor items** | Never auto-progressed. Show the source's numbers |
| **Pain** | Above 3/10, or still there next morning: `easier` or swap, whatever the answer |

### 8d. The ladders (within the 44)

| Family | Easier → harder |
|---|---|
| Squat | bodyweight_squat → goblet_squat → back_squat |
| Single-leg | bodyweight_squat → step_up → split_squat → reverse_lunge → bulgarian_split_squat · step_up → lateral_step_down → bulgarian_split_squat |
| Hinge | glute_bridge → romanian_deadlift → single_leg_rdl · romanian_deadlift → kettlebell_swing |
| Bridge | glute_bridge → single_leg_glute_bridge → hip_thrust · single_leg_glute_bridge → raised_single_leg_hamstring_bridge |
| Calf → plyo | calf_raise → pogo_hops → squat_jump |
| Vertical pull | lat_pulldown → pull_up |
| Anti-extension | dead_bug → front_plank |
| Anti-rotation | bird_dog → pallof_press |
| Lateral | side_plank → copenhagen_plank |

**No ladder** (progress by load, reps or the note's variations): push, overhead press, row, carries, cuff work, balance, mobility, hamstring curl, leg extension. Swaps link the machines to their home versions (§7).

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
| **The protocol is the strength** | **Decided 17 Oct (Jamie: "Strength = physio").** An athlete's own protocol sessions are their strength work. Peak adds **nothing on top**: no top-up, no extra session. A physio session is an ordinary strength session whose sets, reps and weights are set exactly; Peak logs them as set and never progresses them |
| **Never edit the source** | Never change a physio's or doctor's exercises, sets, reps or loads. The coach may **only point out a conflict** and suggest the athlete checks with the source |
| **Physio items never auto-progress** | Source physio or doctor: the log shows their numbers; the progression engine stays off |
| **Self-managed can progress** | Source self or coach: the normal progression rules (§8c) apply |
| **Cautions apply automatically** | Every library exercise's caution for the issue's area applies: follow it, or swap |
| **Avoid list** | Never prescribed while the issue is current |
| **No plyos in lower-limb rehab** | Knee, ankle or hip issue that is active or in rehab: **no plyometrics** unless the source clears them. Managed issues don't block. 🟡 |
| **Free-text exercises allowed** | Shown **without an animation**. Added to the library candidates list (§9f) |
| **Logged per side** | Left and right are logged separately, so a rehab leg's numbers stay visible (§8a) |
| **Taper and race week** | **Open decision** (§13). Until then: follow the source; with no instruction, **ask the athlete when the taper starts** |
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
| Single-leg hamstring curl (machine) | single_leg_hamstring_curl **(new)** | Single-leg leg extension (machine) | single_leg_leg_extension **(new)** |
| Single-leg calf raise | calf_raise | Lateral step-down | lateral_step_down **(new)** |
| Single-leg hamstring bridge, foot raised | raised_single_leg_hamstring_bridge **(new)** | Copenhagen plank | copenhagen_plank |

**Loads: set by the physio.** The library never copies or changes them. **All 10 exercises are now in the library**, so every one has an animation slot.

**What the builder does:**

| Step | Result |
|---|---|
| Cap | The 2 physio sessions **fill the 2/week cap**. No library strength session is added |
| Covered by the physio | **squat, hinge, single-leg, bridge, calf, adductors**, lateral core, balance |
| Missing | push, pull, anti-extension and anti-rotation core, bone impact, grip |
| Added by Peak | **Nothing.** The two physio sessions are the week's strength |
| Blocked | **pogo_hops, squat_jump**: knee in rehab, not cleared |
| Grip | No carries (walking load on the knee). Gap until the physio says otherwise |
| Conflict to flag | A hard ride within 8 h of a physio session. **Flag it; never move the physio session** |
| Balance, upper body, jumps | Balance comes from the physio's single-leg deadlift. Anything else is **the physio's call** |
| Logging | Each side logged separately; the physio's numbers shown, never auto-progressed |
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

### 9f. Library candidates

The four free-text exercises from example 1 were **added to the library on 17 Oct**, with full entries:

| Exercise | Library id | Pattern | Tier |
|---|---|---|---|
| Single-leg hamstring curl (machine) | single_leg_hamstring_curl | knee_flexion **(new)** | A (leg curl machine) |
| Single-leg leg extension (machine) | single_leg_leg_extension | knee_extension **(new)** | A (leg extension machine) |
| Lateral step-down | lateral_step_down | single_leg | C (a step) |
| Single-leg hamstring bridge, foot raised | raised_single_leg_hamstring_bridge | bridge | C (bench or box) |

**The candidates list is now empty.** New free-text items from athletes' protocols land there for the next batch.

---

## 10. Beginners and masters athletes

| Group | Rule | Tag |
|---|---|---|
| **Beginners (level 1)** | 3-week learning block (8-12 reps, RPE 6-7). Level-1 exercises only | 🟡 (✅ NSCA for older beginners) |
| | **4-week calf lead-in** before plyos, unless they already do an impact sport (§5) | 🔴 |
| | First 2 sessions: 2 sets, technique first | 🟡 |
| **Masters** | **Same programme.** Heavy strength still works | ✅ Fragala 2019 |
| | Keep **heavy + impact** in (bone), and **balance** every week (falls) | ✅ LIFTMOR, Sherrington |
| | Power work stays: explosive intent at moderate load (squat jumps, swings) | ✅ Fragala 2019 |
| | Longer warm-up (add 2 min). Plyo volume at the low end of the phase range | 🟡 |
| **Women peri / post-menopause** | Prioritise heavy lifts and impact | ✅ LIFTMOR (women with low bone mass) |
| **Everyone, all ages** | A short **balance item in every session** (decided 17 Oct). WHO's 3+ days for over-65s is met only if they also do a mobility session | ✅ CMO, Sherrington · 🟡 WHO gap |

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

**Planner gap:** `session-library.json` peak and taper menus **don't include `strength_general`**. The blueprint says peak 1-2/wk and taper 1/wk. So today the planner **can't place** the strength the blueprint asks for. **Decided 17 Oct: add it** (a later code step).

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
| **Changed** | Plyos | From build. Straight away for level 2+ or impact-sport athletes; 4-week calf lead-in for beginners and lower-limb issues |
| **New** | 44-exercise library | Patterns, muscles, levels, ladders, swaps, cues, cautions, log type, start, progress and effort lines |
| **New** | Power block | 3 min of plyos from build |
| **New** | Sport and health modules | Run, bike, swim, triathlon, health |
| **New** | Athlete-issues layer | Extends the profile's `injuries[]`: structured protocols, counted toward cap and coverage, avoid list, plyo block (§9) |
| **New** | 4 more exercises | From a physio rehab protocol: hamstring curl, leg extension, lateral step-down, raised hamstring bridge (§9f) |
| **New** | Logging | Weight and reps (or seconds, or band) every set, left and right separately (§8a) |
| **New** | Effort answer | One tap after each exercise: easy, right, hard (§8b) |
| **New** | Progression engine | Logs + effort answer set next time's weight and reps (§8c). Replaces my earlier "RPE 7, two sessions" rule |
| **New** | Balance in every session | All athletes, all ages (§5) |
| **New** | Flexible order | Any order when gym kit is busy; every example item has an alt (§5) |
| **New** | Weekly coverage rule | §3 |
| **New** | Progression / regression rules | §8 |
| **New** | Learning block for new lifters | §6 |
| **New** | Off-season dose | The planner's offseason phase had no strength dose |
| **New** | Example sessions | 6, all built from the rules |

**Stale pointer:** `templates/session-library.md` points to `reference/run-form-and-strength.md`, which **isn't in the repo**. This library would replace it.

---

## 13. Decisions

### Decided 17 Oct

| # | Decision | What it means | Where |
|---|---|---|---|
| 1 | **Upper body for everyone** | Push + pull every session, paired into lower-body rests. No extra time | §5 |
| 2 | **Fix the peak / taper gap** | Add `strength_general` to the planner's peak and taper menus with the §6 doses. **The menu change is a later code step** | §6, §11 |
| 3 | **Plyos from build for everyone** | Level 2+ or impact-sport athletes start straight away. Beginners and lower-limb issues do a 4-week calf lead-in first | §5 |
| 4 | **Learning block for new lifters** | 3 weeks at 8-12 reps, RPE 6-7, for level 1 only | §6 |
| 5 | **Balance for everyone** | A short balance item in every session, all ages (not just over-65s) | §5 |
| 7 | **Pain rule** | Up to 3/10 during and gone by next morning is OK. A source's own pain rule always wins | §8c, §9 |
| 8 | **Equipment extras asked once** | Band, mini band, pull-up bar, bench: asked once and stored. Only the tier is asked weekly | §7 |
| 9 | **Add the 4 physio exercises to the library** | Full entries; the knee example now uses them | §9f |

### Open

#### 6. Issue protocols in taper and race week
The blueprint says **light strength in taper, none in race week**. An athlete's own protocol may say something different. **Jamie: "unsure".**

| Option | Impact |
|---|---|
| a. Follow the source; with no instruction, keep rehab sessions and drop library sessions | Rehab never stalls for a race. Library sessions go quiet |
| b. Drop everything in race week, protocol included | Clean rule. Can break a physio's programme |
| c. Keep everything as normal | Library sessions add fatigue in race week |

**Default until decided:** follow the source's instruction. If there is none, **ask the athlete when the taper starts.**
