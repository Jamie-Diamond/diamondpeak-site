# W' for intensity riders: plan (PAUSED)

Status: **paused by Jamie, 17 Oct 2026** ("more effort than benefit for now").
Nothing below is built. Pick it up from §7 when it comes back.

---

## 1. What Jamie decided (17 Oct 2026)

| Decision | Detail |
|---|---|
| **Bike sessions stay in %FTP** | W' never replaces FTP as the prescription unit |
| **W' is a check on hard sets only** | VO2, short VO2 and over-unders get reps and rest sized to the athlete's W' |
| **Intensity riders only** | Road racing, crits, hill climbs and similar. **Never for triathletes or endurance riders**: there it is "more likely to be wrong than right" |
| **Passive estimate** | From normal rides (Intervals.icu power-curve models), not a forced lab-style test |
| **Keep it fresh by nudging** | Ask for the odd all-out hill, or an indoor test |
| **Test at the start of build** | Only if the estimate is still stale by then |
| **Live check adjusts W'** | A hard session moves W' up or down. It never gives up and falls back for good |

## 2. What the athlete would see (needs Jamie's yes before any build)

> For an athlete training for intensity riding, Peak estimates W' from their normal rides, asks for
> the odd all-out hill or indoor test when the estimate goes stale, sizes their VO2 and
> over-under sets to it (the session note says so), and nudges W' up or down after each of those
> sessions. Triathletes and endurance riders never see W'.

## 3. Blocker: there are no intensity-riding events yet

`config/event-levels.json` has run, tri, **sportive** and **gravel** events only. The bike
goal in `lib/goals.py` is "Raise my FTP". **No athlete can be in scope until intensity-riding
events exist**, and adding them is the bigger job:

| Needed | Where |
|---|---|
| Event types: road race, crit, hill climb, XC MTB, cyclocross, track | `config/event-levels.json` (levels, fitness at taper, taper days) |
| Phase menus and emphasis for those events | `config/session-library.json` `events` |
| Classifying an athlete's race as intensity riding | **The model reads the race the athlete adds** (no keyword matching, per Jamie's AI-over-regex rule) and stores `kind: "bike_intensity"` on the race |
| Sessions those riders need that the library lacks | Short anaerobic repeats, sprint and surge work (`session_types.bike`) |

On 17 Oct no athlete qualified: Jamie (marathon), Kathryn (10k), Calum (Mallorca 312, endurance),
Fred (FTP block), James (run block plus FTP).

## 4. The four pieces

### A. Estimate: new `lib/wprime.py`

| Rule | Detail |
|---|---|
| **Source** | ICU `power-curves` (42-day curve) → `powerModels`: MS_2P, MORTON_3P, FFT_CURVES, ECP |
| **Never use** | Wellness `sportInfo.wPrime`. It is an old fit that ICU slowly lowers over time, not fresh data (Jamie on 17 Oct: shows 24.3 kJ, while his 42-day models give 11–21 kJ) |
| **Valid when** (proposed, tune on real data) | The models agree within ~10%, **and** the 42 days contain a real max effort of 3–5 min **and** one of 12–20 min |
| **Output** | `{cp, w_prime_j, status: valid/stale/unknown, reason, as_of}` |
| **Stored** | `athletes/<slug>/wprime.json`, with history (value, source, date, reason) |

### B. Keeping it fresh

| When | What happens |
|---|---|
| Status goes **stale** | The weekly note asks for one all-out effort: a 3–5 min hill, or 12–20 min if the short one is already fresh. Indoor riders get a 3+12 test (3 min all-out, 30 min easy, 12 min all-out) |
| **Start of build**, still stale | A "3+12 test" session goes in their week. It uses the same pattern as the goal tests in `lib/goals.py` (`"test": {...}`) |
| A fresh effort lands | `wprime.py` re-reads the models. If they now agree, status goes to valid |

### C. Sizing the hard sets

| Rule | Detail |
|---|---|
| **Which sessions** | `vo2`, `vo2_short`, `over_under` in `config/session-library.json` |
| **What W' changes** | **Rest and rep count only.** Power stays at the library's %FTP |
| **Target** | Simulate the planned watts with `lib/wbal.py` `wbal_curve`. W' balance should end the last rep at about 10–25%, never below 0 before the last rep |
| **No valid W'** | The library's default progression, exactly as today |
| **Where** | Layer 2 session instantiation (`lib/plan_builder.py`), deterministic, no LLM |
| **Athlete sees** | One line in the session note, e.g. "Rests sized to your W' (18 kJ)" |

### D. Live check after each W'-sized session

`scripts/activity-watcher.py` runs `wbal_curve` on the ride's power stream with the athlete's
current CP and W'.

| What happened | W' change |
|---|---|
| Held every rep, effort felt hard, ended near the planned floor | None |
| **Cracked early** (power fell more than ~8% below target, or stopped) | Set W' to what they actually used up to the crack. **Max −10% per session** |
| **Finished easily** (low RPE, or held above target) | **+5%** |
| Bad day (readiness red, illness flag, heat, or RPE note says unwell) | **No change** |

A fresh passive estimate (piece A) overrides the live-adjusted value.

## 5. Out of scope

| Not doing | Why |
|---|---|
| W' for triathlon, run or endurance riding | Jamie's call, 17 Oct |
| A W' race-pacing "surge budget" for hilly races | Worth doing later for intensity riders. Separate piece |
| Showing W' on Trends or Settings | The session note is enough to start |

## 6. Build order

| Step | Size | Done when |
|---|---|---|
| 0. Jamie's yes on §2 | – | Sentence agreed |
| 1. Intensity-riding events (§3) | **Large** | A road racer can sign up and get a sensible plan |
| 2. `lib/wprime.py` + tests | Small | Gives the right status for every athlete on real ICU data |
| 3. Nudges and the start-of-build test | Small | A stale athlete gets one ask a week at most, never more |
| 4. Sizing in `plan_builder.py` + tests | Medium | A dry-run week shows rests sized to W', and identical output when W' is invalid |
| 5. Live adjust in `activity-watcher.py` + tests | Medium | Replaying a cracked session lowers W' within the cap |

## 7. Questions for Jamie when this comes back

1. **Is the §2 sentence right?**
2. **Which events count as intensity riding?** The proposed list is in §3.
3. **Is the validity gate right?** Models within 10%, plus a fresh 3–5 min and 12–20 min max. Tune on the first real rider.
