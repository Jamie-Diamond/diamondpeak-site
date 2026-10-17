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

## Status at 2 Oct 2026 (end of session)

| Item | Status | Saves / athlete / month |
|---|---|---|
| Activity watcher called Claude every 5 min for a run logged under its Strava id | Done (ce7009a9) | ~3% of the weekly Max limit a day |
| Watcher skips a second copy of a logged session | Done (1ea90a53) | stops the same loop |
| Duplicate cleanup: one message with Delete / Merge buttons (`lib/duplicates.py`) | Done (836a6786) | data quality |
| Zwift note in the shared rules for every athlete; Fred's correction sent | Done | - |
| Watchdog triggers in Python | Done (2325ddde) | ~$2.5-3 |
| Chat: one resumable session per model | Done (059eda9d) | ~$4-5 |
| Chat: calendar + activities with ids in the live-data block | Done (059eda9d) | ~$2-4 |
| Daily prescription: no model when there is nothing to judge | Done (0ff5e92c) | ~$3.5 |
| Peak usage page: "Bug fixes in chat" row | Done (c92d4373) | reporting (~$20/mo of Jamie's ~$280) |
| Calum: reconnect Garmin in Intervals.icu (no activities since 1 Aug) | Waiting on Calum; asked in Peak, his notifications are off | - |
| Re-measure the savings above | To do after Fri 9 Oct | - |
| Weekly plan + summary cost | To do after Sun 4 Oct | - |
| Daily messages as Python + Haiku (also BLOCKED / swap prescription days) | On hold until Haiku 5.5 | ~$8 |
| Opus routing in chat | On hold: show Jamie ~20 replies side by side first | ~$6-8 |
| Chat replies after an hour's gap | Parked: cheaper, but drops earlier detail | ~$4 |

Expected: a coached athlete goes from ~$50 to ~$35/month with the done items, ~$20 with the held ones.

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

## 6 Oct 2026: measured, price ceiling, Sonnet review

**Measured since the 2 Oct cuts** (4 days, API prices): Fred $31/month, James $38, so a
typical coached athlete is ~$34, on this doc's ~$35. Chat ~$19 of it, at ~$0.18-0.25 per
athlete message (~75% cache writes). A CLI "Say OK" job carries ~17-28k tokens of Claude
Code's own prompt and tools.

**Price ceiling (Jamie):** at most £45 a month including VAT, at a 50% margin, so £18.75
(~$25 at 1.325 $/£) per athlete. Scheduled jobs are ~$15 today and ~$9 once the daily
messages are on Haiku, which leaves ~$15 for chat: the default chat allowance is now $15
(lib/chat_limits.py). Opus stays in chat (Jamie: Sonnet "isn't good enough at the logic
and context"); the allowance caps what that costs. Direct API: not yet.

**Sonnet review** (150 chat turns since 28 Sep, 99 Sonnet). No case of Sonnet getting a
training number or verdict wrong in chat. The wrong numbers came from scheduled messages
and readers (a 2:30-2:50/100m swim pace, "12th October" read as 12 hours). What Sonnet
does get wrong, all behaviour, so prompt-fixable:

| Failure | Seen in | Fix |
|---|---|---|
| Asks before doing what the athlete already said ("Want me to remove it?", 1/2 options) | Jamie 1 Oct ("shouldn't need this level of back and forth"), Fred 1 and 5 Oct, James 2-4 Oct | prompt: when the athlete states a change, make it and say so |
| Hedges and caveats instead of answering ("I haven't checked", "I can't say") | Jamie's "better than Runna" took 4 messages | rebalance the "say what you didn't do" lines |
| Missed the scheduled question it was answering | Jamie's "No", 2 Oct | fixed 2 Oct (engine._missed_since) |
| Shallower digging on a debugging question | missing chat messages, 4 Oct: Opus found the 30-exchange trim next turn | dev work, Dev tab runs Opus |

## 16 Oct 2026: after the Haiku move, and the chat model picker

**Projection** (last 7 days repriced, same tokens on Haiku 5.5): moving the morning card,
night-before brief and activity debrief to Haiku takes a coached athlete from ~$18 to
~$11 a month (Fred $20 -> $13, James $15 -> $9). Those three drop from ~$2-3 to ~$0.15
each. Chat is then about two-thirds of what is left.

**Chat, last 14 days:** Fred $19/month (75% of it Opus), James $18 (35%), Kathryn $9 (71%).
Today's picker (`select_model` in telegram/bot.py) is a word list with stickiness over the
last three messages, which is why most of Fred's chat runs on Opus.

**Decided (Jamie, 16 Oct):** build an AI model picker (Haiku reads each message: Haiku
for simple ones, Sonnet by default, Opus for plan changes, illness/injury or pushback),
judged on ~20 real messages side by side first. Opus -> Sonnet everywhere: undecided,
"more shadowing required". Leaving chat as it is: no.

**Running:** `lib/chat_shadow.py` to 26 Oct. After every real chat reply the picker
chooses, and the reply is written again read-only and never sent, on the picker's model
and on Sonnet whenever Opus answered. The Dev tab is told at 20 pairs; then review with
`python3 ClaudeCoach/lib/chat_shadow.py --report`. Measured on the VM: the picker takes
~3 s through the CLI, so a live picker needs a faster call (direct API) or to run while
the prompt is assembled.

**Activity watcher (same day):** no longer runs the model blind (Intervals.icu
unreadable = wait) or in a loop (the same unlogged activities = at most 3 passes 30 min
apart). Kathryn's 15 Oct was 139 Sonnet runs, ~$31.

### 17 Oct 2026 - picker review (23 messages)
- Picker adequate on 21/23; the two under-picks (a complaint sent to Haiku) fixed in PICKER_PROMPT.
- Opus -> Sonnet: as good or better on 10/13, slightly worse on 3, materially worse on 0.
- **Jamie: corrections to the coach stay on Opus** until the shadow ends on 26 Oct.
- Open: follow-up turns record $0 cost (under-counted chat spend); picks take 3-7 s, too slow to go live as is.
