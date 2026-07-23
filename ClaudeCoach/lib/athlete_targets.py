"""Single, atomic writer for an athlete performance TARGET.

profile.json is the AUTHORITATIVE source for an athlete's race targets. Every
other place a target is stored (config/athletes.json, reference/rules.md,
system_prompt.txt, current-state.md) is a MIRROR and MUST be written through
this module so the copies can never silently drift apart again.

Root cause this fixes
---------------------
On 22 Jul 2026 Kathryn's goal race-run pace was corrected 6:12 -> 5:45/km, but
the write was PARTIAL: config/athletes.json + persistent-rules.md were updated
while profile.json, system_prompt.txt and reference/rules.md kept the stale
value. The bot injects system_prompt.txt, so it went on prescribing off 6:12/km.
There was no single writer, so a hand-edit that touched some files but not all
left the athlete's state internally contradictory.

`set_run_pace_target()` updates ALL locations together and atomically:
  1. read every file and compute its new content in memory FIRST;
  2. every prose replacement is fail-loud (the token it means to change MUST be
     present the expected number of times, else it raises and writes nothing);
  3. only if every edit resolves do we write - each file via a temp + os.replace;
  4. a post-write scan re-reads every file and raises if ANY stale token
     survived anywhere. On any exception the pre-edit snapshots are restored.

If a future methodology change moves a target, call this - never hand-edit one
copy. If the prose has drifted so a token no longer matches, it FAILS LOUDLY
rather than leaving a copy stale, which is exactly the guard we want.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # ClaudeCoach/
HALF_MARATHON_KM = 21.0975


class TargetWriteError(RuntimeError):
    """Raised (loudly) when a target cannot be written to every location."""


def _pace_to_seconds(pace: str) -> int:
    """'5:45/km' or '5:45' -> 345 seconds."""
    m = re.match(r"\s*(\d+):(\d{2})", pace)
    if not m:
        raise TargetWriteError(f"unparseable pace {pace!r}")
    return int(m.group(1)) * 60 + int(m.group(2))


def _minutes_to_hmm(total_min: float) -> str:
    """121 -> '2:01' (hours:minutes, matching the profile run_time format)."""
    total = round(total_min)
    return f"{total // 60}:{total % 60:02d}"


def _fail_loud_replace(text: str, old: str, new: str, *, expect: int = 1) -> str:
    n = text.count(old)
    if n != expect:
        raise TargetWriteError(
            f"expected {expect} occurrence(s) of {old!r}, found {n} - aborting "
            "(prose drifted; fix the mapping rather than leave a copy stale)"
        )
    return text.replace(old, new)


def set_run_pace_target(
    slug: str,
    new_pace_per_km: str,
    *,
    prose_pace_subs: dict[str, str] | None = None,
    prose_time_subs: dict[str, str] | None = None,
    verify_absent: list[str] | None = None,
    dist_km: float = HALF_MARATHON_KM,
    base_dir: Path = BASE,
    dry_run: bool = False,
) -> dict:
    """Set an athlete's GOAL RACE-RUN PACE in every stored location, atomically.

    new_pace_per_km : e.g. '5:45/km' - the authoritative human value to write.
    prose_pace_subs : {old_phrase: new_phrase} to replace in the prose files
                      (system_prompt.txt, reference/rules.md, current-state.md).
                      Use CONTEXT-ANCHORED full phrases, never bare tokens like
                      '2:10', so an unrelated occurrence (e.g. a projection band
                      '2:05-2:10') is never clobbered. A phrase is only applied
                      to a file that contains it; each phrase is unique to its
                      file in practice.
    prose_time_subs : same, for the derived run-time / split phrases.
    verify_absent   : bare tokens (e.g. '6:12/km') that MUST NOT survive in any
                      file after the edit - the belt-and-braces remnant scan.

    Returns a dict of {path: change-summary}. Raises TargetWriteError (loudly) if
    any location cannot be written or a stale token survives.
    """
    ath = base_dir / "athletes" / slug
    profile_p = ath / "profile.json"
    rules_p = ath / "reference" / "rules.md"
    sysprompt_p = ath / "system_prompt.txt"
    cstate_p = ath / "current-state.md"
    config_p = base_dir / "config" / "athletes.json"

    pace_sec = _pace_to_seconds(new_pace_per_km)
    run_min = round(dist_km * pace_sec / 60)
    run_time_hmm = _minutes_to_hmm(run_min)
    pace_norm = new_pace_per_km if new_pace_per_km.endswith("/km") else f"{new_pace_per_km}/km"

    prose_pace_subs = {k: (v or pace_norm) for k, v in (prose_pace_subs or {}).items()}
    prose_time_subs = dict(prose_time_subs or {})
    verify_absent = list(verify_absent or [])

    # ---- 1. read everything up front (snapshot for rollback) --------------
    snapshots: dict[Path, str] = {}
    for p in (profile_p, rules_p, sysprompt_p, cstate_p, config_p):
        if not p.exists():
            raise TargetWriteError(f"missing target file: {p}")
        snapshots[p] = p.read_text(encoding="utf-8")

    changes: dict[str, str] = {}
    new_content: dict[Path, str] = {}

    # ---- 2. compute new content (fail-loud) -------------------------------
    # profile.json - AUTHORITATIVE
    prof = json.loads(snapshots[profile_p])
    rt = prof.setdefault("race_targets", {})
    changes[str(profile_p)] = (
        f"run_pace {rt.get('run_pace')!r}->{pace_norm!r}, "
        f"run_time {rt.get('run_time')!r}->{run_time_hmm!r}"
    )
    rt["run_pace"] = pace_norm
    rt["run_time"] = run_time_hmm
    new_content[profile_p] = json.dumps(prof, indent=2, ensure_ascii=False) + "\n"

    # config/athletes.json - engine-read mirror
    cfg = json.loads(snapshots[config_p])
    if slug not in cfg:
        raise TargetWriteError(f"{slug} not in {config_p}")
    splits = cfg[slug].setdefault("race_target_splits", {})
    if splits.get("run_min") != run_min:
        # only rewrite the shared config when the value actually changes, so we
        # never churn/reformat other athletes' entries for a no-op.
        changes[str(config_p)] = f"race_target_splits.run_min {splits.get('run_min')}->{run_min}"
        splits["run_min"] = run_min
        new_content[config_p] = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"
    else:
        changes[str(config_p)] = f"race_target_splits.run_min already {run_min} (no write)"

    # prose files - fail-loud token replacement
    for p in (rules_p, sysprompt_p, cstate_p):
        text = snapshots[p]
        applied = []
        for old, new in {**prose_time_subs, **prose_pace_subs}.items():
            # only touch files that actually contain the token; a file may hold
            # the pace but not the time token, or vice-versa.
            if old in text:
                text = _fail_loud_replace(text, old, new, expect=text.count(old))
                applied.append(f"{old!r}->{new!r}")
        if applied:
            changes[str(p)] = "; ".join(applied)
            new_content[p] = text

    # ---- 3. verify no stale phrase or bare token survives anywhere --------
    # (a) none of the old phrases we replaced should remain in their file;
    # (b) none of the bare stale tokens (verify_absent) should remain in ANY
    #     file - including files we did not touch, which would mean we missed a
    #     copy. This is the guard that turns a partial write into a loud abort.
    survivors: dict[Path, str] = {p: new_content.get(p, snapshots[p])
                                  for p in (profile_p, config_p, rules_p, sysprompt_p, cstate_p)}
    for p, text in survivors.items():
        for tok in verify_absent:
            if tok in text:
                raise TargetWriteError(
                    f"stale token {tok!r} still present in {p} after edit - "
                    "a copy was missed; refusing to write a partial update")

    if dry_run:
        return {"dry_run": True, "changes": changes,
                "derived": {"run_min": run_min, "run_time": run_time_hmm}}

    # ---- 4. write all (atomic per file) + rollback on any failure ---------
    written: list[Path] = []
    try:
        for p, text in new_content.items():
            tmp = p.with_suffix(p.suffix + ".tmp-settarget")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, p)
            written.append(p)
    except Exception as exc:  # restore everything we touched
        for p in written:
            p.write_text(snapshots[p], encoding="utf-8")
        raise TargetWriteError(f"write failed ({exc}); rolled back {len(written)} file(s)") from exc

    return {"changes": changes, "derived": {"run_min": run_min, "run_time": run_time_hmm}}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Atomically set an athlete goal run-pace target everywhere.")
    ap.add_argument("slug")
    ap.add_argument("pace", help="new goal race-run pace, e.g. 5:45/km")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    res = set_run_pace_target(args.slug, args.pace, dry_run=args.dry_run)
    print(json.dumps(res, indent=2, ensure_ascii=False))
