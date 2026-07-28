"""Root conftest — make primitives/ importable, and make the suite incapable of
sending Telegram.

BELT (this file) AND BRACES (lib/coach_alert.py's under-test guard). On 28 Jul 2026
a unit test in tests/test_ops_digest.py reached coach_alert.send() with no dry-run
env and no subprocess stub, so it executed telegram/notify.py for real and delivered
a "ClaudeCoach did not deliver: weekly plan" message to the coach's live thread —
twice, once per suite run. One mechanism was enough to have that happen; two are
enough that a future test file, a direct `import coach_alert`, or a fixture someone
forgets cannot repeat it:

  1. HERE: every test in this tree runs with CC_ALERT_DRY_RUN=1 by default, so
     send() short-circuits to log_outbound(sent=False) before it builds a command.
  2. THERE: send() itself refuses to execute the real notify.py while pytest is in
     the process, and RAISES rather than quietly no-opping. That covers the cases a
     fixture cannot: a test that deliberately unsets the env var (several do — see
     TestSendFailureDoesNotEatTheCooldown), a helper imported and called outside a
     test function, and any new test file added later.

The env var alone is the weaker half BECAUSE tests legitimately override it: the
cooldown-banking tests must see send() take its real path, and they do that with
CC_ALERT_DRY_RUN=0 plus a stubbed coach_alert.subprocess. Guard 2 is written to
allow exactly that shape (a stub cannot reach Telegram) and to stop the unstubbed
shape (which can).
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session", autouse=True)
def _never_telegram():
    """Default the whole session to dry-run.

    SESSION scope, not function scope, on purpose: the variable is a process-wide
    default rather than per-test state, and a function-scoped fixture would have to
    fight monkeypatch.setenv in the tests that legitimately opt out (monkeypatch
    restores the pre-test value, which is this one). Autouse because the protection
    must not depend on a test remembering to ask for it — the test that sent the
    real message asked for `logs` and `monkeypatch`, not for a safety fixture.

    Restored afterwards so running the suite never leaves a mutated environment
    behind for whatever else shares the shell.
    """
    before = os.environ.get("CC_ALERT_DRY_RUN")
    os.environ["CC_ALERT_DRY_RUN"] = "1"
    yield
    if before is None:
        os.environ.pop("CC_ALERT_DRY_RUN", None)
    else:
        os.environ["CC_ALERT_DRY_RUN"] = before
