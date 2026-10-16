# Claude API key for athletes' coaching

Set up 16 Oct 2026, **switched off** until the daily messages are on Haiku (Jamie, 16 Oct).

## How it works

| Piece | Where |
|---|---|
| The key (one for the whole business) | `/etc/claudecoach/anthropic-api-key` on the VM, chmod 600 |
| The switch and who is on the key | `config/claude-billing.json` (tracked; deploys through cc-gitpull) |
| Routing, retry, breaker | `lib/claude_billing.py` |
| Breaker state | `~/Library/Logs/ClaudeCoach/claude-billing-tripped.json` |

- **One key, not one per athlete.** A self sign-up needs no action: with `api_athletes: "all"`
  every registered athlete is on the key. Per-athlete cost comes from `scripts/usage-report.py`.
- **Routing is explicit.** A call goes on the key only when its caller names the athlete.
  Unnamed calls (dev work, bug fixes, shadow runs, rule tidy, hand runs) stay on Max.
- **`subscription_athletes` always stays on Max.** Jamie is in it: his chat is dev-heavy.
- **A refused key** (credit used up, spend limit, revoked) is retried once on Max. The breaker
  then keeps everyone on Max for an hour and tries the key again. One ops-log line per outage,
  and a Dev-tab note at most once a day. The athlete never sees the refusal.

## What is on the key once switched on

| Job | On the key | Wired in |
|---|---|---|
| Chat, streamed and not, and photos | yes | `engine.scoped_env` |
| Morning card, daily prescription, watchdog, night-before brief, weekly summary, weekly plan, session sync | yes | `run_claude(athlete=slug)` |
| Activity debrief, Strava name and description | yes | `run_claude` / `claude_billing.run` |
| Evening check-in, capture reminder, Strava write-up script | yes | `claude_billing.run` |
| Capture reader (bot.py) | yes | `run_claude(athlete=slug)` |
| Food logging (Peak Food tab) | **no, still Max** | ~15 calls through `lib/nutrition_nlu.py`'s `runner`; ~$5/month |
| Spoken rewrite (voice mode), race lookup at sign-up | **no, still Max** | no athlete to hand; pennies |
| Dev tab, bug fixer, shadow runs, rule tidy | **no, by design** | dev work |

Food logging must move before the public launch (a customer's coaching on a personal
subscription).

## Switching on

1. Key saved and `python3 ClaudeCoach/lib/claude_billing.py --test-key` passes.
2. Set `"enabled": true` in `config/claude-billing.json`, commit, push. Cron jobs pick it up
   on the next cc-gitpull (:05 / :35); chat after the API restarts (nightly, or by hand).
3. `python3 ClaudeCoach/lib/claude_billing.py` shows who is on `api`.
4. After a week, compare the Console bill with `usage-report.py` for those athletes.

To move more athletes, add their slugs to `api_athletes` (or set it to `"all"`).
