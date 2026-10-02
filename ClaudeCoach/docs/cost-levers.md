# Cost per athlete: levers

Measured 2 Oct 2026 from the VM transcripts since the 28 Sep cuts, at API prices (what a
paying athlete would cost). A coached athlete with light chat (Fred, Calum) was about
**$50 a month plus the weekly plan**:

| Job | $/month | Driver |
|---|---|---|
| Chat | ~$22-26 | ~60% of calls on Opus; ~10 tool calls a message; cache rewrites |
| Five daily messages (morning card, prescription, watchdog, night-before, evening check-in) | ~$18 | a fresh ~35-45k prompt each run, mostly for near-template output |
| Session sync + activity debriefs | ~$6 | already skip the model when nothing is new |
| Weekly plan + summary | not yet measured | Sundays; first full numbers after 4 Oct |

Cache writes were 54-72% of every job's cost: each run writes its whole prompt once.

## Shipped 2 Oct 2026

| Lever | Commit | Expected saving / athlete / month |
|---|---|---|
| Watchdog triggers in Python (`lib/watchdog_checks.py`); the model runs only for a NEW trigger | 2325ddde | ~$2.5-3 |
| One resumable chat session per model: a Sonnet<->Opus switch re-sent the whole conversation (~20% of chat cost) | 059eda9d | ~$4-5 |
| Chat live-data block carries event/activity ids, last week's plan done/not done, 3 weeks ahead (`lib/chat_context.py`) | 059eda9d | ~$2-4 (fewer tool calls) |
| Daily prescription: no model on rest days or GO days with nothing to judge (`_no_model_needed`); Sonnet only when a rule fired or a progression / illness / pain flag | see git log | ~$3.5 (rules fired on ~12% of past days) |
| Peak usage page: bug fixes done through the coach chat get their own row | c92d4373 | reporting only |

Re-measure after Fri 9 Oct with the job profile (per job: runs, calls per run, context
per call, $ per run) and compare with the table above.

## On hold (Jamie, 2 Oct 2026)

**Daily messages as Python + Haiku - wait for Haiku 5.5.** Morning card, night-before brief and evening
check-in: Python builds the card (session, load, reminders, flags); Haiku writes only the
one or two sentences of coach voice from a compact data block. Python, not Haiku, decides
what is notable. About $0.01 a message instead of $0.10-0.13; ~$8/athlete/month. Risk:
tone and judgement in the Haiku line.

**Opus routing in chat.** ~60% of chat calls ran on Opus, Fred's and Calum's included,
although Sonnet is the default. Check what `_PLANNING_RE` catches, then narrow it to real
plan changes. Show Jamie ~20 Sonnet vs Opus replies side by side before changing anything
(the July Sonnet trial was reverted on quality). ~$6-8/athlete/month.

**Chat reply after more than an hour.** ~21% of chat cost: the cache has expired, so a
long session (often 80-190k tokens) is re-sent whole. Starting a fresh session instead
would re-send ~40k, but drops the earlier tool output from the conversation. Quality
trade-off, so not done.

## Daily prescription (built 2 Oct 2026; BLOCKED / swap days stay on Sonnet while Haiku is on hold)

Verdict at the check: a Python gate, with Sonnet only on days that need judgement (an estimated
15-30% of athlete-days; no prescription log was available to measure it).

- The go / modified / swapped / blocked decision, % FTP and duration are already made in
  Python (`modulate_session`); the model mostly re-fetches data Python already has and
  writes it up. `templates/session-library.md` is not used.
- **Python only:** no session planned (Sonnet currently runs just to print "Rest day"),
  or no rules fired and no pain, illness or progression flag.
- **Python + Haiku line:** BLOCKED or Z2-swap days.
- **Sonnet:** MODIFIED interval sessions (the engine cannot see interval structure), any
  pain >= 4, illness or progression flag.
- Risks: soft signals (pain, illness, progression) must become explicit gate triggers; the
  heat check needs `home_latlon`; morning-checkin matches the text before the first colon
  of the `<telegram>` line against the calendar, so the template must keep that.

## Not an athlete cost

Some of Jamie's "chat" spend is development done through the coach chat on Opus ("Fix
the bug", git commits). It inflates his per-athlete figure and is not what a paying
athlete would cost.
