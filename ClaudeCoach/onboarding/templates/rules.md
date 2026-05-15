# $name — Coaching Rules

Read this before every prescription. All hard constraints live here.

---

## Anchor values

| Metric | Value | Source |
|---|---|---|
| Bike FTP (outdoor) | $ftp_watts W | profile.json |
| Bike FTP (indoor) | not set | update profile.json when tested |
| Swim CSS (/100m) | $swim_css | profile.json |
| Run threshold pace (/km) | $run_threshold | profile.json (approximate — retest to firm up) |
| LTHR | $lthr bpm | profile.json |
| Max weekly hours | $max_hours | profile.json |
| Race | $race_name | $race_date |

Update this table whenever a test resets an anchor. Never use stale numbers.

---

## A goal: $a_goal

**B goal:** $b_goal

---

## Weekly TSS framework

Based on $max_hours h/week max and current fitness level.

| Phase | Weeks to race | Weekly TSS target | Notes |
|---|---|---|---|
| Base | — | — | Aerobic volume, technique work |
| Build | — | — | Race-specific intensity blocks |
| Peak | — | — | Highest load — short, hard blocks |
| Taper | 3–1 | — | Maintain sharpness, shed fatigue |
| Race week | 0 | 80–120 | Travel, short openers only |

_TSS ranges will be populated by generate-blueprint.py based on current fitness._

Ramp rate cap: **+8% week-on-week TSS** maximum. If previous week was disrupted (illness, travel, missed sessions), reset the ramp from the actual TSS delivered, not the target.

---

## TSS-delivery rule for time-capped sessions (critical)

Duration is the primary training stimulus — always prefer more time when available. This rule applies only when a session is time-capped by external constraints.

**When a time cap means easy effort would deliver < 75% of the session's TSS target, add intensity to close the gap.**

### How to apply

1. Calculate the session's TSS target based on its share of the weekly target.
2. Estimate what easy effort (Z2 power / easy run pace) would deliver within the time cap.
3. If ≥ 75% of target is achievable at easy effort → keep it easy, execute on time.
4. If < 75% → add structured intensity to lift TSS (sweetspot blocks on bike; tempo km on run).
5. Never replace the full session with intensity — always keep warm-up and cool-down.
6. Do not escalate stress beyond the session's planned load.

---

## Discipline priority

For $race_distance — gains follow this priority order when weekly volume must be trimmed:

1. **Bike** — largest share of race time; highest TSS leverage
2. **Run** — determines finishing place more than swim at this distance
3. **Swim** — maintain, don't sacrifice

---

## Fuelling guidelines

To be populated after initial sessions. Target:
- Sessions > 90 min: start at 60g CHO/hr, build toward 65-75g/hr in Build phase.
- Hydration: 500-700ml/hr in temperate conditions.

---

## Injury notes

Check `profile.json → injuries` before any run prescription. If an active injury is flagged:
- Ask for current pain score (0–10) before prescribing run sessions
- Do not escalate run load if pain > 2/10 reported during previous session

---

## DOs and DON'Ts

**DO:**
- Apply intensity-over-duration rule whenever a session is time-capped
- Pull live data from Intervals.icu before any prescription — never use stale numbers
- Ramp week-on-week by ≤ 8%

**DON'T:**
- Accept low weekly TSS because a time-capped session delivered less at easy effort
- Prescribe run load without checking injury status first
- Use another athlete's anchor values — every athlete has different FTP, CSS, pace
