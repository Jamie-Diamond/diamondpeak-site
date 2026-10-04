"""Permanent record of each athlete's chat (Jamie, 4 Oct 2026).

history.json is the coach's working memory and is trimmed to the last 30 exchanges, so
anything older was deleted from disk and vanished from Peak (Fred's first evening of
messages was already gone). This file keeps every exchange that ever passed through
history.json, never trimmed, so Peak can show the whole conversation.

    athletes/<slug>/telegram/chat-archive.jsonl   one history.json entry per line

sync() upserts the current history.json into it; it runs on every save_history, every
Peak chat load and every refresh-site-data pass, so an entry is archived long before 30
newer exchanges push it out. The coach still reads only history.json.
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

CC = Path(os.environ.get("CC_HOME") or Path(__file__).resolve().parent.parent)   # ClaudeCoach/
ARCHIVE = "chat-archive.jsonl"


def _paths(slug: str) -> tuple[Path, Path]:
    d = CC / "athletes" / slug / "telegram"
    return d / "history.json", d / ARCHIVE


def identity(e: dict) -> str:
    """Same exchange, even after a later edit to its reply or a hide flag. Older entries
    carry no time, so their text is all there is to go on."""
    if e.get("ts") and e.get("user"):
        return f"{e['ts']}|{e['user']}"
    return f"{e.get('ts') or ''}|{e.get('user') or ''}|{e.get('assistant') or ''}"


def identities(entries: list) -> list[str]:
    """identity() per entry, with "#n" on repeats: older untimed entries can be word-for-
    word identical ("Logged.") and are still separate messages."""
    seen: dict[str, int] = {}
    out = []
    for e in entries:
        k = identity(e) if isinstance(e, dict) else ""
        n = seen.get(k, 0)
        seen[k] = n + 1
        out.append(f"{k}#{n}")
    return out


@contextmanager
def _locked(f: Path):
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f.with_suffix(".lock"), "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def _read(f: Path) -> list[dict]:
    out = []
    try:
        for line in f.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
    except OSError:
        pass
    return out


def _write(f: Path, entries: list[dict]) -> None:
    tmp = f.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(e) + "\n" for e in entries))
    tmp.replace(f)


def sync(slug: str, history: list | None = None) -> None:
    """Upsert history.json into the archive. Never raises: losing an archive pass must
    never break a chat reply."""
    try:
        hf, af = _paths(slug)
        if history is None:
            try:
                history = json.loads(hf.read_text())
            except (OSError, ValueError):
                return
        if not isinstance(history, list):
            return
        with _locked(af):
            arch = _read(af)
            at = {k: i for i, k in enumerate(identities(arch))}
            changed = False
            hist = [e for e in history if isinstance(e, dict) and (e.get("user") or e.get("assistant"))]
            for e, k in zip(hist, identities(hist)):
                if k in at:
                    if arch[at[k]] != e:
                        arch[at[k]] = e
                        changed = True
                else:
                    at[k] = len(arch)
                    arch.append(e)
                    changed = True
            if changed:
                _write(af, arch)
    except Exception:
        pass


def sync_file(history_file) -> None:
    """sync() for a history.json path (athletes/<slug>/telegram/history.json)."""
    p = Path(history_file)
    if p.name == "history.json" and p.parent.name == "telegram":
        sync(p.parent.parent.name)


def load(slug: str) -> list[dict]:
    """Every archived exchange, oldest first."""
    return _read(_paths(slug)[1])


def patch(slug: str, match, change) -> bool:
    """Apply change(entry) to every archived entry where match(entry) is true."""
    af = _paths(slug)[1]
    with _locked(af):
        arch = _read(af)
        hit = False
        for e in arch:
            if match(e):
                change(e)
                hit = True
        if hit:
            _write(af, arch)
    return hit


def all_slugs() -> list[str]:
    return sorted(p.parent.parent.name for p in (CC / "athletes").glob("*/telegram/history.json"))


if __name__ == "__main__":
    for s in all_slugs():
        sync(s)
        print(s, len(load(s)))
