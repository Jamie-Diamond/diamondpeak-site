import sys
from pathlib import Path
# lib/ sits one level up from this test file (scripts/ or lib/); add it to the path.
_here = Path(__file__).resolve().parent
for cand in (_here / "lib", _here.parent / "lib"):
    if (cand / "rules_lint.py").exists():
        sys.path.insert(0, str(cand))
        break
import rules_lint as R

REQ = {"Run": {"easy", "z3", "high"}, "Bike": {"easy", "z3", "high"}, "Swim": {"easy", "high"}}

def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    assert cond, name

# 1) distribution parsing (en-dash)
d = R.parse_distribution({"Run": "83% Z1–2 / 12% Z3 / 5% Z4–5", "Swim": "70% Z1–2 / 0% Z3 / 30% Z4–5"})
check("parse run", d["Run"] == {"easy": 83, "z3": 12, "high": 5})
check("parse swim z3=0", d["Swim"]["z3"] == 0)

# 2) stale suppression rule IS flagged
stale = "[perm] Hold run quality back this block — no hard runs, no tempo, keep all running easy."
f = R.lint_rules_text(stale, REQ)
check("stale hold-run-quality flagged", any(x["sport"] == "Run" for x in f))

# 3) Kathryn-style superseding rule is NOT flagged (contains SUPERSEDES + is required)
superseded = ("[perm] Intensity-distribution planning policy (SUPERSEDES the prior hold run quality "
              "back philosophy). A genuine run-quality session serving the run Z4-5 slice is required "
              "every build week; do NOT defend a week that misses the distribution.")
check("superseded rule NOT flagged", R.lint_rules_text(superseded, REQ) == [])

# 4) benign non-intensity rule not flagged
benign = "[perm] Cap swim sessions at 2km maximum - Kathryn does not want to swim further than 2km."
check("benign swim-cap not flagged", R.lint_rules_text(benign, REQ) == [])

# 5) expires-tagged suppression is treated as reconciled (self-pruning)
exp = "[expires:2026-08-01] Skip all hard run sessions while ankle settles."
check("expires rule not flagged", R.lint_rules_text(exp, REQ) == [])

print("\nAll unit checks passed.")
