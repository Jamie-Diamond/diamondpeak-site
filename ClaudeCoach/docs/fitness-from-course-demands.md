# Fitness from course demands

**What a course needs: the FTP, weight and fitness to make its cut-offs**

Written 17 Oct 2026, for when Calum is coached again and the Mallorca 312 comes up.
**Status: method documented, not built into the app.** The live fitness target for a long
bike day is still the "survive it" floor below (10 h+ day: 50–65). **Calum has not been
told any of this. Jamie decides how he hears it.**

Jamie, 17 Oct 2026: *"use logic: what would his FTP/kg get to, then what fitness would be
required to hold that for that duration?"* and *"it should use generic reference points"*.
His own Ironman is deliberately not used as a reference: a bike leg ridden with a marathon
to come is a different effort.

---

## The method in five steps

| Step | Question | How it's worked out |
|---|---|---|
| 1 | **What does the course make you ride?** | Course physics on the climbs (lib/course.py): the steady climbing power at which every cut-off leaves no more than a bunch-able speed (30 km/h) on the flats and descents. Weight matters here as much as watts |
| 2 | **What's the whole day's load?** | Climbing time at that power, plus the rest of the moving time at bunch power, give a whole-day **normalised power (NP)** over the **moving time** the cut-offs allow |
| 3 | **What can a rider hold for that long?** | The share of FTP a well-trained amateur holds for that duration (generic pacing guidance, the hold table below) |
| 4 | **What FTP does that need?** | FTP needed = day NP ÷ that share. Say it in **W/kg** first, watts in brackets |
| 5 | **What fitness does that need?** | Intensity held scales with √fitness, so **fitness needed = reference fitness × (share of FTP needed ÷ share the reference rider holds)²**. A lower FTP means holding a bigger share, which needs much more fitness |

### The formulas

```
day NP          = ( (climb_h × P_climb⁴ + rest_h × P_rest⁴) / moving_h )^¼
share needed    = day NP / FTP                       (intensity factor, IF)
fitness needed  = F_ref × (share needed / share_ref(duration))²
```

The square is the load model: a day's load is hours × IF² × 100, and fitness is the capacity
to absorb load, so holding a higher IF for the same hours needs fitness in proportion to IF².

---

## Generic reference points

| Reference | Value | Source |
|---|---|---|
| Share of FTP a well-trained amateur holds, by ride length | 2 h 0.85–0.90 · 3 h 0.80–0.85 · 4 h 0.75–0.80 · 6 h 0.70–0.75 · 8 h 0.65–0.70 · 10 h 0.62–0.67 · **10 h+ 0.58–0.64** | Standard endurance pacing guidance; the hold table already in lib/season_view.py |
| Fitness of that "well-trained amateur" | **80** | Middle of our own "Competitive" sportive band, 70–90 (config/event-levels.json). Judgement: it ties the pacing table to a fitness |
| Power in the bunch on the flats at ~30 km/h | **120–160 W** for a ~90 kg rider (solo it would be ~170 W) | Physics with drafting. **The biggest unknown**: a big group can make it lower |
| Climbing | CdA 0.36, Crr 0.005, bike and kit 11 kg | lib/course.py (physics; drafting barely matters on a climb) |
| Feed stops | 4 min each | lib/course.py, judgement |

---

## Two bands: ride it well, or survive it

On a bike you can **go into a much bigger hole and survive**: descents and the bunch give
recovery that a run never gives (Jamie, 17 Oct 2026). So one number is too blunt.

| Band | Meaning | Fitness |
|---|---|---|
| **Ride it well** | Make the cut-offs at a pace your fitness can sustain all day | Step 5 above |
| **Survive it** | Make the cut-offs having dug deep, recovering on descents and in the bunch | The level tables' finish levels by day length: under 6 h 28–40 · 6–10 h 36–48 · **10 h+ 50–65** (an Ironman finisher's fitness). Live in the app now (season_view.DAY_FLOOR) |

Even to survive, the **climbing power is not optional**: the climbs to the first cut-off
are physics. A rider climbing above ~0.85 of FTP for hours before that cut-off isn't digging
a hole, they're redlining. So surviving still needs an FTP of about climbing power ÷ 0.85.

---

## Worked example: Calum, Mallorca 312

**Course** (official): 312 km, ~5,050 m climbing, gun 06:30. Cut-offs (2026 route sheet):
km 97 11:20, km 218 16:40, Artà km 285 19:55. Finish closes **20:40** (2027 rules). Miss
an intermediate cut-off and you're sent onto the shorter route. File:
reference/races/mallorca-312.json.

**Calum today**: 93 kg, FTP 211 W (**2.3 W/kg**) from BikeErg sessions (no road power
meter), fitness 26. Longest ride 8.7 h.

### Step 1–2: what the day demands

| | At 93 kg | At 88 kg (5 kg lighter) |
|---|---|---|
| Climbing power to make every cut-off | **220 W (2.37 W/kg)** | 210 W (2.39 W/kg) |
| Time climbing | ~5.0 h | ~5.0 h |
| Rest of the moving time | ~8.6 h, bunch power ~140 W | ~8.6 h |
| Moving time the cut-offs allow | ~13.6 h | ~13.6 h |
| **Whole-day NP** | **~183 W** (178–189 W for bunch power 120–160 W) | ~176 W |

### Steps 3–5: FTP and fitness needed to ride it well

Share of FTP held for 13.6 h by the reference rider: **0.61**, at fitness 80.

| FTP on the day | Share of FTP held all day | **Fitness needed (93 kg)** | Fitness needed (88 kg) |
|---|---|---|---|
| 2.6 W/kg | 0.76 | **~123** | ~127 |
| 2.8 W/kg | 0.70 | **~106** | ~110 |
| 3.0 W/kg | 0.66 | **~92** | ~96 |
| 3.2 W/kg | 0.61 | **~81** | ~84 |
| 3.4 W/kg | 0.58 | **~72** | ~74 |

The fitness column moves only a little with weight, because the W/kg needed barely changes.
**Lighter mainly lowers the watts needed**, and so how far FTP has to rise.

### What it says

| | Ride it well | Survive it |
|---|---|---|
| FTP | ~3.2 W/kg (≈ 300 W at 93 kg) | ~2.8 W/kg (220 W climbing ÷ 0.85 ≈ 260 W at 93 kg) |
| Fitness | ~80 | 50–65 |
| Calum now | 2.3 W/kg · 26 | 2.3 W/kg · 26 |

A realistic winter (FTP +10–20% from a low base is my estimate, not a measured figure) gets
him to about **2.5–2.7 W/kg**, or ~2.6–2.8 W/kg if he also loses 5 kg. That is **short of
even the survive line** on climbing power. The rules let a rider switch to the 225 km
(or 167 km) route on the day while inside the cut-offs, so **the realistic plan is "aim at
the 312, decide at km 97"**, with the 225 as a planned fallback, not a failure.

Biggest levers (physics, minutes saved on the climbs to km 97): **FTP +10 W ≈ 8 min**,
**3 kg lighter ≈ 5 min**.

---

## Talking it through with Calum

Use plain words. Lead with what the day asks, then where he is, then the choice.

1. **What the 312 asks:** about 5 hours of climbing at ~2.4 W/kg to get through the
   cut-offs, then a long day on top. The climbing part is physics; the bunch can't help on
   a climb.
2. **Where he is:** 2.3 W/kg and fitness 26. The gap is FTP and fitness, about equally.
3. **The two ways to finish:** ride it well (~3.2 W/kg, fitness ~80) or survive it
   (~2.8 W/kg, fitness 50–65).
4. **The plan:** build FTP and fitness through the winter, lose weight if he wants to, and
   go in with the 225 as the planned call at km 97 if the numbers aren't there by April.
5. **What would sharpen it:** a road power meter (his FTP is from the BikeErg, and his
   long-ride durability can't be measured without one), and logging carbs on long rides.

Don't lead with "you won't make it". Lead with the numbers and the decision point.

---

## Limits

| Limit | Effect |
|---|---|
| Bunch power on the flats (120–160 W) is an estimate | A big, well-drafted group lowers every number here. It can't close a 0.5 W/kg climbing gap |
| The reference fitness (80) is a judgement tying generic pacing to our level tables | Validate once coached athletes ride these events with power |
| Wind and heat on the day | Not modelled. They add to the flats, not the climbs |
| Calum's FTP is indoor (BikeErg) | Road FTP can differ by a few per cent either way |
| The 2027 route sheet isn't out | Cut-offs are 2026's. Recheck when it's published |

## Where the numbers come from

| What | Where |
|---|---|
| Course, climbs, cut-off needs, climbing power | lib/course.py, reference/races/mallorca-312.json, `plan_tools.py course --athlete calum --race "Mallorca 312"` |
| Hold table, survive floor (DAY_FLOOR) | lib/season_view.py |
| Bike card (climbing in W/kg, levers) | lib/limiters.py diagnose_bike |
| Level tables (the "Competitive" 70–90 band) | config/event-levels.json |
