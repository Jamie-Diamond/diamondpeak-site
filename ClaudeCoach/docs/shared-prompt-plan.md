# ClaudeCoach — Shared Prompt Architecture

**Written:** 2026-05-31  
**Status:** Plan — execute when ready (~3–4h)  
**Problem it solves:** Bug fixes and rule changes applied to one athlete's system_prompt.txt must be
manually repeated for every other athlete. Drift is inevitable and athletes end up with inconsistent
behaviour. The Kathryn date-labelling bug and the Jamie NP/IF conflict both existed because rules
were duplicated, not shared.

---

## Current structure (bad)

```
athletes/jamie/system_prompt.txt      ← 250+ lines, mostly universal rules
athletes/kathryn/system_prompt.txt    ← 200+ lines, mostly universal rules
athletes/calum/system_prompt.txt      ← 150+ lines, mostly universal rules
```

`bot.py` loads only the athlete file. When a rule changes, it must be edited in 3 (soon N) files.

---

## Target structure

```
telegram/shared_prompt.txt            ← all universal rules, one source of truth
athletes/jamie/system_prompt.txt      ← athlete-specific only (~60 lines)
athletes/kathryn/system_prompt.txt    ← athlete-specific only (~60 lines)
athletes/calum/system_prompt.txt      ← athlete-specific only (~60 lines)
onboarding/templates/system_prompt.txt ← already a template; strip universal content
```

`_system_prompt_with_level()` in `bot.py` reads shared first, then appends athlete-specific:
```python
def _system_prompt_with_level(sp_file: Path) -> str:
    shared_file = Path(__file__).parent / "shared_prompt.txt"
    shared = shared_file.read_text().strip() if shared_file.exists() else ""
    athlete = sp_file.read_text().strip()
    text = shared + "\n\n---\n\n" + athlete if shared else athlete
    # existing coaching level injection follows unchanged
    ...
```

---

## What goes in shared_prompt.txt

Everything that is currently duplicated verbatim (or near-verbatim) across all three files:

| Section | Notes |
|---|---|
| CRITICAL OUTPUT FORMAT | `<telegram>` tag rule — identical for all |
| Read before every prescription | Mandate to read rules.md + current-state.md |
| Date and day-of-week discipline | Cross-check rule — just added manually to all 3 |
| Ride/cycle debrief order | Zone distribution first, NP/IF supporting |
| Run analysis (GAP) | Always use GAP, not raw pace |
| Response style rules | No padding, no recaps, no emojis, no tables |
| Auto-log rule | Never ask to log — just log |
| API verification rule | Never say Done without API response |
| Context persistence | Git commit block |
| Charts — embed syntax | `<<<CHART:TYPE:JSON>>>` format |
| Charts — all type specs | load, fitness, form, powercurve, week |
| Session log schema | Identical across all athletes |
| Feedback capture block | Identical schema, only path differs (parameterise) |
| Common CLI endpoints | icu_fetch.py endpoint reference |

---

## What stays athlete-specific

| Section | Why athlete-specific |
|---|---|
| Identity block | Name, race, FTP, CSS, thresholds, weight |
| Anchor values | Different for each athlete |
| Injury/protocol status | Live state, changes frequently |
| Schedule constraints | Jamie: swim Tue/Thu; Kathryn: KB Mon, mobility Tue, swim Fri; Calum: TBD |
| File paths | `--athlete jamie` vs `--athlete kathryn` etc. |
| Read before prescription | *Only* the specific file paths — the mandate itself is shared |
| Coaching level | Profile-driven, injected at runtime — stays as is |

---

## Parameterisation needed

The feedback capture block currently hardcodes the athlete name and file path. Two options:

**Option A — template variable at write time**  
Keep athlete-specific feedback path in the athlete file. Shared prompt omits the feedback block.
Athlete file appends it. Simpler but leaves one block duplicated.

**Option B — runtime injection (cleaner)**  
Pass `slug` into `_system_prompt_with_level()`. Replace `{athlete_slug}` in shared_prompt.txt
at load time. One file, zero duplication.

Recommendation: **Option B**. The function already has access to `sp_file` which contains the slug.

---

## Migration steps

1. **Extract shared content** — copy universal sections from Jamie's system_prompt.txt (most complete)
   into `telegram/shared_prompt.txt`. Parameterise `{athlete_slug}` and `{athlete_name}`.

2. **Strip athlete files** — remove the universal sections from all three system_prompt.txt files,
   leaving only athlete-specific content. Verify each file is ~60 lines.

3. **Update `_system_prompt_with_level()`** — prepend shared_prompt, substitute `{athlete_slug}`
   and `{athlete_name}` from the sp_file path.

4. **Update onboarding template** — strip universal sections from
   `onboarding/templates/system_prompt.txt`; it will inherit from shared at runtime.

5. **Test all three athletes** — send a test message as each athlete, verify:
   - `<telegram>` tag wrapping works
   - Charts render
   - Prescription reads rules.md + current-state.md
   - Date cross-check rule is in effect

6. **Extend to scripts** — morning-checkin.py, evening-checkin.py, activity-watcher.py all have
   inline prompts. A follow-on pass can extract their shared sections into
   `shared_prompt_scripts.txt` or inline via a `load_shared()` helper.

---

## Effort estimate

| Step | Time |
|---|---|
| Extract + write shared_prompt.txt | 1h |
| Strip and verify athlete files | 45m |
| Update bot.py (3 functions) | 30m |
| Update onboarding template | 15m |
| Test all three athletes | 30m |
| **Total** | **~3h** |

---

## What not to do

- Do not merge athlete files into a single file with conditionals — that's worse than duplication.
- Do not put live state (ankle pain, current CTL, current injuries) in shared_prompt — that belongs
  in current-state.md which is read at prescription time.
- Do not touch the scripts in the same pass — scope creep risk is high. Scripts first require
  understanding each inline prompt; that is a separate session.
