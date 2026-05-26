# Coaching Levels — Implementation Plan

## Decisions confirmed
- Three levels: `beginner`, `mid` (default), `pro`
- `pro` keeps plain-English labels AND adds acronyms in parens on first use: "Fitness (CTL)", "Fatigue (ATL)", "Load (TSS)", "Form (TSB)"
- Coach-set only (via profile.json on VM): Calum → beginner, Kathryn → mid, Jamie → pro
- Charts deferred (separate backlog item)
- `mid` is the default — zero behaviour change until explicitly set

---

## Level definitions

| Level | Who | Vocabulary | Numbers | Tone |
|-------|-----|-----------|---------|------|
| `beginner` | Calum | Effort language only ("easy", "moderate", "hard") | Duration + distance only — no power, pace/km, zones, TSS | Encouraging, jargon-free |
| `mid` | Kathryn | Plain-English labels: Fitness / Fatigue / Load / Form | Pace, HR, power, zone % — contextualised | Current ClaudeCoach |
| `pro` | Jamie | Plain-English + acronym in parens on first use | Full: zone watts/pace, decoupling %, NP, IF, VI, W' | Terse, data-dense |

---

## Files to change

### 1. `ClaudeCoach/lib/coaching_levels.py` (NEW)
Single source of truth. Import in every script.

```python
"""Coaching level instruction blocks — injected into all Claude prompts."""

DEFAULT = "mid"

_BLOCKS = {
    "beginner": (
        "Coaching level: BEGINNER. "
        "Use effort-based language only: easy, comfortable, moderate, hard, very hard. "
        "Do not reference power, pace per km, zone numbers, TSS, NP, IF, or any training metrics. "
        "Duration and distance are fine. Describe sessions in plain terms. "
        "Tone: encouraging, jargon-free."
    ),
    "mid": (
        "Coaching level: MID (default). "
        "Use plain-English labels throughout: Fitness (not CTL), Fatigue (not ATL), "
        "Load (not TSS), Form (not TSB). "
        "Include supporting numbers (pace, HR, power, zone %) but always contextualise them."
    ),
    "pro": (
        "Coaching level: PRO. "
        "Show plain-English labels with acronyms in parentheses on first use per reply: "
        '"Fitness (CTL)", "Fatigue (ATL)", "Load (TSS)", "Form (TSB)". '
        "After first use in a reply you may use either form. "
        "Include full technical detail: zone watts and pace, decoupling %, NP, IF, VI, W' estimates where relevant. "
        "Be terse and data-dense — skip soft framing, lead with numbers."
    ),
}


def level_block(level: str) -> str:
    """Return the instruction paragraph for injection into Claude prompts."""
    return _BLOCKS.get(level, _BLOCKS[DEFAULT])
```

---

### 2. `ClaudeCoach/telegram/bot.py`
Three functions each read system_prompt.txt and pass it as a string. Inject the level block after reading.

**Functions to change:** `call_claude` (line ~1875), `call_claude_streaming` (line ~1897), `call_claude_with_image` (line ~1961).

**Pattern** — in each function, after `sp_file.read_text().strip()`, append the level block:

```python
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
from coaching_levels import level_block as _level_block

# After reading system_prompt:
sp_text = sp_file.read_text().strip()
_profile_path = sp_file.parent / "profile.json"
if _profile_path.exists():
    import json as _json
    _level = _json.loads(_profile_path.read_text()).get("coaching_level", "mid")
    sp_text = sp_text + "\n\n" + _level_block(_level)
```

**Better approach**: add a helper at module level:

```python
def _system_prompt_with_level(sp_file: Path) -> str:
    text = sp_file.read_text().strip()
    profile_path = sp_file.parent / "profile.json"
    if profile_path.exists():
        try:
            level = json.loads(profile_path.read_text()).get("coaching_level", "mid")
            text = text + "\n\n" + level_block(level)
        except Exception:
            pass
    return text
```

Then replace `sp_file.read_text().strip()` with `_system_prompt_with_level(sp_file)` in all three call functions.

**Also update** the top-level import block to include:
```python
from coaching_levels import level_block
```
(coaching_levels.py is in `../lib` — add to sys.path if needed, or symlink.)

---

### 3. `ClaudeCoach/scripts/activity-watcher.py`
`_build_prompt()` already takes `profile` dict. Add level injection at the end of the prompt string.

In `_build_prompt`:
```python
import sys as _sys; _sys.path.insert(0, str(BASE / "lib"))
from coaching_levels import level_block

# At the end of the return f-string, add:
# \n\n{level_block(profile.get('coaching_level', 'mid') if profile else 'mid')}
```

Also pass `coaching_level` into `_strava_description()` so Haiku descriptions adapt:
- beginner Strava description: aim + how it felt, no metrics
- pro: NP, IF, zone breakdown inline

---

### 4. `ClaudeCoach/scripts/morning-checkin.py`
Already reads profile into `profile` dict. Inject level block into the prompt string:

```python
from coaching_levels import level_block
coaching_level = profile.get("coaching_level", "mid")
# Add {level_block(coaching_level)} near the top of the prompt after athlete context line
```

---

### 5. `ClaudeCoach/scripts/evening-checkin.py`
Same pattern as morning-checkin.

---

### 6. `ClaudeCoach/scripts/daily-prescription.py`
Same pattern. `_build_prompt(slug, name, race_name)` — read profile.json inside or pass level as arg.

---

### 7. `ClaudeCoach/scripts/weekly-summary.py`
Same pattern. Already reads profile dict fully.

---

## VM steps (after code deploy)

SSH to VM and add `coaching_level` to each athlete's `profile.json`:

```bash
# Jamie → pro
python3 -c "
import json; p=json.loads(open('ClaudeCoach/athletes/jamie/profile.json').read())
p['coaching_level']='pro'
open('ClaudeCoach/athletes/jamie/profile.json','w').write(json.dumps(p,indent=2))
"

# Kathryn → mid (default, but explicit)
python3 -c "
import json; p=json.loads(open('ClaudeCoach/athletes/kathryn/profile.json').read())
p['coaching_level']='mid'
open('ClaudeCoach/athletes/kathryn/profile.json','w').write(json.dumps(p,indent=2))
"

# Calum → beginner
python3 -c "
import json; p=json.loads(open('ClaudeCoach/athletes/calum/profile.json').read())
p['coaching_level']='beginner'
open('ClaudeCoach/athletes/calum/profile.json','w').write(json.dumps(p,indent=2))
"
```

Run from `/Users/diamondpeakconsulting/diamondpeak-site/` on VM.

---

## Implementation order

1. `lib/coaching_levels.py` — create first (all others depend on it)
2. `telegram/bot.py` — highest leverage; add `_system_prompt_with_level()` helper, update 3 call functions
3. `scripts/activity-watcher.py` — analysis messages + Strava descriptions
4. Automated scripts in one pass: morning-checkin, evening-checkin, daily-prescription, weekly-summary
5. Deploy + VM profile.json updates
6. Charts label variants — separate task (backlog)

---

## Testing checklist

- [ ] New session with Jamie: reply includes "Fitness (CTL)" on first metric reference
- [ ] Activity watcher fires for Jamie: Strava description includes NP/IF
- [ ] Morning checkin for Calum: no zone numbers, effort language only
- [ ] Kathryn interactive: plain-English labels, no acronyms
- [ ] `level_block("unknown_level")` falls back to "mid" without error
