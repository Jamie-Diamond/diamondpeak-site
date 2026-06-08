"""Drift guard: every repo path the README references must actually resolve
(remediation-plan WS F, Issue #6).

The root README drifted once already — it pointed new sessions at a defunct
single-athlete layout. This parses the backtick-quoted path references out of
README.md and asserts each resolves, so that class of drift fails a test instead
of misleading a human.

Scope: backtick-quoted tokens that look like real repo paths (contain "/").
- Committed code/dirs (scripts/, lib/, ironman-analysis/, …) must exist.
- `<slug>`/`{slug}`-templated paths are checked against real athlete dirs; since
  athletes/ is GITIGNORED, they're skipped when no athlete data is present (fresh
  clone / CI) rather than failing.
Bare filenames in prose (e.g. `rules.md` with no directory) are intentionally not
checked — they're contextual references, not file-map entries.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
README = REPO / "README.md"


def _athlete_slugs() -> list[str]:
    adir = REPO / "athletes"
    if not adir.is_dir():
        return []
    return [p.name for p in adir.iterdir()
            if p.is_dir() and not p.name.startswith(".")]


def _referenced_paths() -> list[str]:
    """Backtick-quoted path tokens whose first segment is a real top-level repo
    entry (or athletes/<slug>). Pairing is per-line — pairing backticks across the
    whole file mis-aligns when an earlier line has an odd backtick count.

    The top-level filter keeps full paths (`scripts/...`, `athletes/<slug>/...`)
    while dropping bare per-athlete shorthand like `reference/` that only exists
    as a subdirectory."""
    top = {p.name for p in REPO.iterdir()}
    out: set[str] = set()
    for line in README.read_text().splitlines():
        for tok in re.findall(r"`([^`]+)`", line):
            tok = tok.strip()
            if not tok or " " in tok or "*" in tok or "/" not in tok:
                continue
            if tok.startswith(("http://", "https://")):
                continue
            if tok.split("/")[0] in top:
                out.add(tok)
    return sorted(out)


def _resolves(rel: str) -> bool:
    p = (REPO / rel.rstrip("/")).resolve()
    return p.exists()


def _is_gitignored(rel: str) -> bool:
    """True if the path is gitignored — so an absent gitignored path (config/
    athletes.json, athletes/ data) is 'not present in this checkout', not drift."""
    try:
        r = subprocess.run(["git", "check-ignore", "-q", rel.rstrip("/")],
                           cwd=REPO, capture_output=True)
        return r.returncode == 0
    except Exception:
        return False


def test_readme_exists():
    assert README.is_file()


def test_readme_references_some_paths():
    # Guard against the parser silently matching nothing (which would make the
    # path checks vacuously pass).
    assert len(_referenced_paths()) >= 5


@pytest.mark.parametrize("rel", [p for p in _referenced_paths()
                                 if "<slug>" not in p and "{slug}" not in p])
def test_committed_path_resolves(rel):
    if not _resolves(rel) and _is_gitignored(rel):
        pytest.skip(f"`{rel}` is gitignored and not present in this checkout")
    assert _resolves(rel), f"README references `{rel}` but it does not exist under {REPO}"


@pytest.mark.parametrize("rel", [p for p in _referenced_paths()
                                 if "<slug>" in p or "{slug}" in p])
def test_slug_templated_path_resolves(rel):
    slugs = _athlete_slugs()
    if not slugs:
        pytest.skip("no athlete dirs present (gitignored) — cannot check templated path")
    ok = any(_resolves(rel.replace("<slug>", s).replace("{slug}", s)) for s in slugs)
    assert ok, f"README references `{rel}` but it resolves for no athlete in {slugs}"
