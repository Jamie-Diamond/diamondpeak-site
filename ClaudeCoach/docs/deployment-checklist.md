# ClaudeCoach — Pre-Deployment Checklist

Run this before pushing any change that touches the automation scripts, bot, or session log.
Each item maps to a real bug we hit in production.

---

## 1. Local sanity checks (run on Mac before pushing)

- [ ] **Python syntax** — `python3 -m py_compile ClaudeCoach/scripts/activity-watcher.py ClaudeCoach/scripts/telegram-feedback.py ClaudeCoach/scripts/refresh-site-data.py ClaudeCoach/telegram/bot.py`
- [ ] **No Unicode box-drawing characters in Python files** — `grep -Pn '[^\x00-\x7F]' ClaudeCoach/scripts/*.py ClaudeCoach/telegram/bot.py` should return nothing (they break the Edit tool and some terminals)
- [ ] **session-log.json is valid JSON** — `python3 -c "import json; json.load(open('ClaudeCoach/athletes/jamie/session-log.json'))"`
- [ ] **No duplicate activity_ids** — `python3 -c "import json; from collections import Counter; e=json.load(open('ClaudeCoach/athletes/jamie/session-log.json')); ids=[x.get('activity_id') for x in e if x.get('activity_id')]; dupes=[k for k,v in Counter(ids).items() if v>1]; print('Dupes:', dupes or 'none')"`
- [ ] **current-state.json is valid JSON** — `python3 -c "import json; json.load(open('ClaudeCoach/athletes/jamie/current-state.json'))"`

---

## 2. VM state check (run via SSH before deploying)

```bash
ssh root@178.105.95.208
```

- [ ] **Exactly one bot.py running** — `pgrep -c -f bot.py` should print `1`. If 0: `systemctl start claudecoach-bot`. If >1: `systemctl restart claudecoach-bot`.
- [ ] **Claude CLI present and executable** — `/usr/bin/claude --version`
- [ ] **Git remote reachable** — `git -C /Users/diamondpeakconsulting/diamondpeak-site remote get-url origin`
- [ ] **No uncommitted local changes that will block pull** — `git -C /Users/diamondpeakconsulting/diamondpeak-site status --short`
- [ ] **Crontab intact** — `crontab -l | grep -c "ClaudeCoach"` should be ≥ 7
- [ ] **Log directory exists** — `ls /root/Library/Logs/ClaudeCoach/`
- [ ] **athletes.json present** — `cat /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/config/athletes.json` (gitignored — must be written manually on fresh VM). If missing: create `ClaudeCoach/config/` and write the file with credentials from 1Password.
- [ ] **admin_chat_id set in config.json** — `grep admin_chat_id /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/config.json`. Add `"admin_chat_id": "6090343263"` (Jamie's chat ID) if missing. Required for `/invite` and `/approve` commands and onboarding notifications.
- [ ] **pending.json present** — `ls /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/config/pending.json` (gitignored, created automatically on first `/invite`). If missing, run: `echo '[]' > /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/config/pending.json`

---

## 3. Deploy (push → VM pull)

- [ ] Push from Mac: `git push origin main`
- [ ] Pull on VM: `git -C /Users/diamondpeakconsulting/diamondpeak-site pull --rebase origin main`
- [ ] If `bot.py` changed: `systemctl restart claudecoach-bot && sleep 3 && systemctl status claudecoach-bot | grep Active`
- [ ] If a cron script changed: no restart needed — next cron run picks it up automatically

---

## 4. Smoke tests (after deploy)

### Telegram bot
- [ ] Send any message → should get "_On it..._" within 3 seconds (bot.py fast-ack)
- [ ] Send `ankle 3` → should get "Logged ankle pain 3/10" with no "days to Cervia" appended
- [ ] Send `82.5 kg` → should get "Logged 82.5 kg. +3.5 kg to race-day target (79 kg)."

### Onboarding
- [ ] From admin chat: send `/invite <test_chat_id>` → should get "Added ... to pending list"
- [ ] From test chat: send any message → should get the first onboarding question (full name)
- [ ] Walk through questionnaire → on completion, admin should get notification with `/approve <slug>`
- [ ] Send `/approve <slug>` from admin → athlete chat should get "Welcome aboard" message
- [ ] Verify `ClaudeCoach/athletes/<slug>/` folder created with profile.json and system_prompt.txt

### Telegram feedback (cron, runs every 1 min)
- [ ] With a stub in session-log.json: send a message containing RPE → within 2 min should get confirmation with fields parsed
- [ ] With no stub: send any message → should get "No session waiting for feedback right now."
- [ ] Kill `/usr/bin/claude` temporarily (`chmod -x`) → send a message → should get a Python-only diagnostic within 90s, NOT silence. Restore afterwards.

### Activity watcher (cron, runs every 15 min)
- [ ] After a new activity syncs to Intervals.icu: Telegram message arrives within 15 min with coaching analysis
- [ ] Check session-log.json has a new stub entry with the correct activity_id
- [ ] Confirm activity_id does NOT appear twice in the log

### Site refresh
- [ ] After `refresh-site-data.py` runs: `ClaudeCoach/site-data.json` timestamp is recent — `stat ClaudeCoach/site-data.json`
- [ ] Open the website and confirm charts load without JS errors

---

## 5. Known failure modes (what to check when something's silent)

| Symptom | First check |
|---|---|
| No Telegram message for a new activity | `tail -50 /root/Library/Logs/ClaudeCoach/activity-watcher.log` — look for timeout or Claude exit code |
| Feedback message gets no reply | Check `telegram-feedback-state.json` offset; check if `bot.py` consumed the update first |
| "132 days to Cervia" or any text appearing twice | `grep "days_to_race\|days to Cervia" ClaudeCoach/telegram/bot.py` — should return nothing |
| Prescription card silent | `tail -50 /root/Library/Logs/ClaudeCoach/prescription.log` |
| Morning checkin silent | `tail -50 /root/Library/Logs/ClaudeCoach/morning-checkin.log` |
| bot.py not running after reboot | `systemctl status claudecoach-bot` — if failed, check `journalctl -u claudecoach-bot -n 50` |
| New athlete gets "not registered" | Check `pending.json` — is their chat_id listed? Send `/invite <chat_id>` from admin to add them |
| Onboarding stuck on ICU key | Check `onboarding_state.json` for their chat_id step; their API key failed validation; they must resend it |
| `/approve` or `/invite` not working | Check `admin_chat_id` is set in config.json and matches your Telegram chat ID exactly |
| Git push rejected on VM | `git -C /Users/diamondpeakconsulting/diamondpeak-site pull --rebase origin main` first |
| SSH key rejected | Re-add via Hetzner console: `echo "<pubkey>" >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys` |

---

## 6. Rollback

```bash
# On VM — revert to previous commit
git -C /Users/diamondpeakconsulting/diamondpeak-site revert HEAD --no-edit
git -C /Users/diamondpeakconsulting/diamondpeak-site push origin main
systemctl restart claudecoach-bot
```
