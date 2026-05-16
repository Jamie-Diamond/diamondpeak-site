# ClaudeCoach — Pre-Deployment Checklist

Run this before pushing any change that touches the automation scripts, bot, or session log.
Each item maps to a real bug we hit in production.

---

## 0. Quick pre-flight (run locally before anything else)

```bash
python3 ClaudeCoach/scripts/preflight.py
```

This script runs all Section 1 checks automatically and exits non-zero on failure.
If it passes, proceed to Section 2. If it fails, fix before deploying.

---

## 1. Local sanity checks (covered by preflight.py — listed for reference)

- [ ] **Python syntax** — all scripts and bot.py compile without error
- [ ] **No duplicate activity_ids** in any athlete's session-log.json
- [ ] **All JSON files are valid** — session-log, current-state, athletes.json, config.json
- [ ] **All active athletes have chat_id** — `python3 -c "import json; a=json.load(open('ClaudeCoach/config/athletes.json')); missing=[s for s,v in a.items() if v.get('active') and not v.get('chat_id')]; print('Missing chat_id:', missing or 'none')"`

---

## 2. VM state check (run via SSH before deploying)

```bash
ssh root@178.105.95.208
```

- [ ] **Exactly one bot.py running** — `pgrep -c -f bot.py` should print `1`. If 0: `nohup python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/bot.py >> /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/bot.log 2>&1 &`. If >1: `pkill -f bot.py && sleep 2` then restart.
- [ ] **Claude CLI resolved correctly** — `python3 -c "import shutil; print(shutil.which('claude'))"` should print `/usr/bin/claude`. Then confirm: `/usr/bin/claude --version`.
- [ ] **Bot log shows correct binary on last startup** — `grep "bot started" /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/bot.log | tail -1` should include `claude=/usr/bin/claude`
- [ ] **Git remote reachable** — `git -C /Users/diamondpeakconsulting/diamondpeak-site remote get-url origin`
- [ ] **Crontab intact** — `crontab -l | grep -c "ClaudeCoach"` should be ≥ 8 (includes bot watchdog at `*/5`)
- [ ] **Log directory exists** — `ls /root/Library/Logs/ClaudeCoach/`
- [ ] **athletes.json present and all active athletes have chat_id** — `python3 -c "import json; a=json.load(open('/Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/config/athletes.json')); missing=[s for s,v in a.items() if v.get('active') and not v.get('chat_id')]; print('Missing chat_id:', missing or 'none')"`. If missing: add the chat_id — get it from bot.log (unregistered sender lines) or ask the athlete to message @userinfobot.
- [ ] **pending.json present** — `ls /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/config/pending.json`. If missing: `echo '[]' > /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/config/pending.json`
- [ ] **Activity watcher lock not stuck** — `find /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach -name '.activity_watcher.lock' -mmin +25` should return nothing. If something: `rm /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/.activity_watcher.lock`

---

## 3. Deploy (push → VM pull)

- [ ] Push from Mac: `git push origin main`
- [ ] VM auto-pulls via cc-gitpull.sh every 30 min. To force immediately: `ssh root@178.105.95.208 "/usr/local/bin/cc-gitpull.sh"`
- [ ] If `bot.py` changed: `ssh root@178.105.95.208 "pkill -f bot.py; sleep 2; nohup python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/bot.py >> /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/bot.log 2>&1 &"` then wait 5s and check: `tail -4 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/bot.log`
- [ ] If a cron script changed: no restart needed — next cron run picks it up automatically
- [ ] **Never rsync the whole ClaudeCoach/ directory** — this overwrites VM-only files (athletes.json, config.json). Always rsync specific files.

---

## 4. Smoke tests (after deploy)

### Telegram bot
- [ ] Send any message → should get a response (not an error message)
- [ ] Send `ankle 3` → should log pain score
- [ ] Check bot.log for any `Claude error:` or `CRITICAL:` lines since restart

### Activity watcher
- [ ] After a new activity syncs to Intervals.icu: Telegram message arrives within 15 min
- [ ] Check session-log.json has a new stub entry with the correct activity_id
- [ ] Confirm activity_id does NOT appear twice in the log

### Morning / evening checkins
- [ ] After 06:20 or 21:00: check `morning-checkin.log` / `evening-checkin.log` for `SKIP: no chat_id` lines — these mean an athlete is misconfigured

### Site refresh
- [ ] After `refresh-site-data.py` runs: training-data JSON timestamps are recent
- [ ] Open the website and confirm charts load without JS errors

---

## 5. Known failure modes

| Symptom | First check |
|---|---|
| No Telegram message for a new activity | `tail -50 /root/Library/Logs/ClaudeCoach/activity-watcher.log` — timeout or Claude exit code |
| Athlete gets "not registered" | Check athletes.json has their chat_id. Get it from bot.log (unregistered sender lines). |
| Bot sends error message instead of coaching | Check bot.log for `Claude error:` — usually bad binary path or timeout |
| Morning/evening checkin silent for one athlete | Check log for `SKIP: no chat_id` — add chat_id to athletes.json |
| Morning checkin silent for all | Check `morning-checkin.log` for Python errors |
| bot.py not running | Cron watchdog restarts it within 5 min. Check `pgrep -c -f bot.py`. |
| Git push rejected on VM | `git -C /Users/diamondpeakconsulting/diamondpeak-site fetch origin && git merge origin/main --no-edit` |
| Rebase conflict on VM | Never use `git pull --rebase` — use fetch + merge instead |
| Activity watcher lock stuck | `rm /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/.activity_watcher.lock` |
| `/approve` or `/invite` not working | Ensure `chat_id` in config.json matches Jamie's Telegram ID (6090343263) |

---

## 6. Rollback

```bash
# On VM — revert to previous commit
git -C /Users/diamondpeakconsulting/diamondpeak-site revert HEAD --no-edit
git -C /Users/diamondpeakconsulting/diamondpeak-site push origin main
pkill -f bot.py
sleep 2
nohup python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/bot.py \
  >> /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/bot.log 2>&1 &
```
