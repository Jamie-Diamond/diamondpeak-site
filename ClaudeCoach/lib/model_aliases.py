"""Which Claude model each CLI alias ("opus", "fable", "sonnet", "haiku") is today.

Every ClaudeCoach call passes an ALIAS to `claude --model`, never a version id, so
the bot is on the newest model the installed CLI knows. Jamie's call, 27 Sep 2026:
"how do we make sure we always get new models?" - the answer is aliases plus
scripts/claude-cli-update.py, which updates the CLI weekly behind a smoke test.

The catch with an alias is that nothing in the code says which model answered. The
weekly job resolves each alias by actually calling it and writes the result to
RESOLVED_FILE; this module reads it back for the two places that need a real id:
the reply footer label (O5.5, not just O) and raw-API calls such as count_tokens,
which do not accept CLI aliases.
"""

import json
import re
from pathlib import Path

ALIASES = ("opus", "fable", "sonnet", "haiku")

# Written by scripts/claude-cli-update.py. Lives beside the other ops state
# (coach-alert-state.json, run-status.jsonl), not in the repo, because it is a
# fact about THIS box's installed CLI and cc-gitpull must never see it change.
RESOLVED_FILE = Path.home() / "Library/Logs/ClaudeCoach/claude-models.json"

_cache = {"mtime": None, "data": {}}


def read_state() -> dict:
    """{"cli_version": ..., "models": {alias: id}, "checked": ...} or {}.

    Re-read only when the file changes, so the footer can call this on every
    reply and still pick up a model switch without a bot restart."""
    try:
        mtime = RESOLVED_FILE.stat().st_mtime
    except OSError:
        return {}
    if mtime != _cache["mtime"]:
        try:
            _cache["data"] = json.loads(RESOLVED_FILE.read_text())
        except (OSError, ValueError):
            _cache["data"] = {}
        _cache["mtime"] = mtime
    return _cache["data"]


def resolved(model: str) -> str | None:
    """The full model id behind an alias; a full id is returned as-is."""
    if model.startswith("claude-"):
        return model
    return (read_state().get("models") or {}).get(model)


_ID_RE = re.compile(r"^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$")


def pretty(model_id: str) -> tuple[str, str]:
    """("Opus", "5.5") from "claude-opus-5-5". ("", "") if it does not parse."""
    m = _ID_RE.match(model_id or "")
    if not m:
        return ("", "")
    family, major, minor = m.groups()
    return (family.capitalize(), f"{major}.{minor}" if minor else major)


def label(model: str) -> str:
    """Footer label: "O5.5", "S5", "H4.5", "F5.1".

    An alias that has not been resolved yet (fresh box, before the first weekly
    run) falls back to its initial rather than guessing a version."""
    family, version = pretty(resolved(model) or "")
    if family:
        return family[0] + version
    return (model.split("-")[1] if model.startswith("claude-") else model)[:1].upper()
