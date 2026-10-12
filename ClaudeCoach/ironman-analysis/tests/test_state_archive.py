"""lib/state_archive: old log entries leave current-state.md for an archive, standing
sections never move (12 Oct 2026)."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import state_archive as sa  # noqa: E402

MD = """# Tess
Last updated: 2026-10-10

## Injury
- 2026-05-01: ankle sprain, rehab done
- Protocol: ease runs over 5/10
- Lunge test 2cm

## Watchdog flags
- **2026-05-10 — T2 (Tier 2):** old one
  continuation of the old one
- **2026-06-01 — T3 (Tier 1):** old two
- **2026-10-09 — T6 (Tier 1):** recent
"""


def test_only_old_log_entries_move(tmp_path):
    d = tmp_path / "athletes" / "tess"
    d.mkdir(parents=True)
    (d / "current-state.md").write_text(MD)
    n = sa.archive("tess", base=tmp_path, today=date(2026, 10, 12))
    out = (d / "current-state.md").read_text()
    arc = (d / "reference" / "current-state-archive.md").read_text()
    assert n == 3                                            # two entries + a continuation
    assert "old one" not in out and "continuation" not in out and "recent" in out
    assert "old one" in arc and "continuation of the old one" in arc and "## Watchdog flags" in arc
    assert "2026-05-01: ankle sprain" in out                 # a standing section, untouched
    assert "reference/current-state-archive.md" in out       # pointer under the log heading
    assert sa.archive("tess", base=tmp_path, today=date(2026, 10, 12)) == 0   # idempotent
