# What rival AI coaches teach us: plan and decisions

Status: **decided with Jamie, 17 Oct 2026.** Built items shipped in Peak 4.026.
Rival names are kept off this public repo on purpose (Jamie, 17 Oct 2026). "App A-F"
below are six AI training apps, cycling, running and triathlon; the names are in the
private notes.

Sources: one rival's forum thread and subreddit (100 posts, Feb-Oct 2026), App Store
reviews of five more (US, GB, AU, 307 reviews, fetched 17 Oct 2026), Reddit threads in
triathlon and cycling communities. Peak was checked in code the same day; backups were
checked live on the VM.

---

## 1. The three findings

| # | Finding | Why it matters |
|---|---|---|
| 1 | **Rivals' 1-star reviews are about trust, not features**: updates that broke sign-in, wiped data, support that never replied, "chat says done, calendar unchanged" | These end a paid beta faster than any missing feature |
| 2 | **Plans wrong in obvious ways**: too easy for fit athletes, too hard (70% of sessions "high stress"), recovery weeks heavier than build weeks, VO2 straight after a layoff, a hard ride the day before a race | Each one is a check we either have or can add |
| 3 | **Triathletes pay more than we assumed**: tri apps charge $13-39 a month; one charges $29 for multisport against $17 for one sport | The ~EUR10 "fair" figure came from a price-sensitive forum crowd |

---

## 2. Decisions and status

| # | Item | Jamie's decision (17 Oct) | Status |
|---|---|---|---|
| 1 | Coach holds the line on key sessions | **Make the case, then do it.** One or two lines on what the session is for and what skipping costs, offer a shorter or moved version; if they still want to skip, do it. Agree at once when ill, in pain or readiness is off | **Done**: `lib/coach_manual.md` "SKIPPING OR CUTTING A KEY SESSION" |
| 2 | New plan checks from rivals' mistakes | Most are caught already. Two hard days are fine if reasonable. VO2 after a week off is fine if the metrics support it. **No new checks** except race eve (below) | Checked: rest day, weekly load floor/cap, CTL ramp and race-day session already block |
| 2a | Race eve | **Code check plus chat rule.** Found that the A race's eve was protected only by prompt text, B/C eves only at build time (IF 0.90+), and the chat coach had no rule | **Done**: `plan_audit.race_eve_findings` blocks more than 10 min at >= 90% of threshold, or a 90+ min session, the day before any race; manual line "RACE EVE" |
| 2b | Two big days back to back | **Leave it** | No change |
| 3 | Chat only says "done" once the calendar shows it | Already built (Aug 2026) | `lib/write_verify.py` checks every claimed calendar or Strava write |
| 4 | Over-unders and ramp tests in the done-session chart | **Show the overs, unders and steps**, only when that is what was planned; every other session keeps the 13 Oct one-block rule | **Done**: `workout_profile.detailed_kind` reads the paired planned workout; `completed(fine=True)` keeps each step. New sessions only (cached ones stay as drawn) |
| 5 | Backups | Test restore | **Done**: 16 Oct off-server archive restored on the VM, all 7 athletes and `athletes.json`; temp copy deleted. `system/crontab.template` now matches the live backup lines |
| 6 | Stated reply time for beta athletes | **No stated promise** | No change |
| 7 | Payments and clean billing | Stripe, adapted from Jamie's majaco set-up, **when the time is right** | Not started |
| 8 | Stale threshold pointer in the coach manual | Fix | **Done**: rules.md thresholds are history; the CURRENT THRESHOLDS block wins |
| 9 | Move a session with one tap | **Leave it to chat** | No change |
| 10 | Referrals | Comes with payments | Not started |
| 11 | Share cards | **Both an overlay and a ready-made image, minimal, the athlete picks the stats** | **Done** (4.026): share button on a done session; Image (dark topo background) or Overlay (transparent, for their own photo); up to 4 stats, a 90-day best shown with a gold star; Share sheet on phones, Save image elsewhere |
| 12 | Build-in-public posts | **Later, near the public launch** | Parked to January |
| 13 | Apple Watch / Apple Health | Keep parked | Waits for the iOS app plan |
| 14 | Coach portal | Keep parked | |
| 15 | MCP / ChatGPT log-in | **No interest**: it dilutes Peak's value proposition | Dropped |
| 16 | Price | **Decide later; multiple tiers worth exploring** | Ceiling stays GBP45/mo inc VAT (`docs/cost-levers.md`) until decided |

---

## 3. What rivals get praised and blamed for

| App | Price | Praised for | Blamed for |
|---|---|---|---|
| **A** (chat coach, cycling first, ~50k sign-ups in a year) | $12.99/mo, ~10 free messages | Chat, season plan, direct device push, fast fixes | Data wiped on re-login; easy to talk out of sessions; invents readiness when data is missing; over-unders merged into one block; free users stall when next week needs a message |
| **B** (tri) | $17 one sport, $29 multisport | Moving and replanning easily; Apple Watch; dashboards | Too easy for fit athletes; updates broke sign-in; no support replies; "replanned" but the session stayed |
| **C** (tri) | $29-199/mo | "Open the app and execute"; no injuries | Overtraining; app freezes; billed after cancelling |
| **D** (tri) | ~$20/mo | Flexible, science-based | Recovery weeks heavier than build; no rest day in 4 months; AI commentary doesn't change the plan |
| **E** (tri, HRV-led) | $13-25/mo | Ships fast: memory, plan prompt, coach portal, model choice in chat | VO2 straight after a layoff; planner ignores constraints |
| **F** (cycling) | EUR17/mo | Workouts export everywhere | Only 7 days visible; can't swap workouts; referral-heavy home screen |

**Where Peak is already ahead:** web app (App A's most repeated request), plan rolls forward
on its own every Sunday, missing data stated plainly (`lib/recovery_score.py`,
`lib/engine.py`), thresholds live every turn (`lib/thresholds.py`), duplicate handling
(`lib/duplicates.py`), food logging, triathlon first.
