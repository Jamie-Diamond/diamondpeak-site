#!/usr/bin/env python3
"""nutrition_store.py - the day log: food entries, supplements, measurements, flags.

Step 1's other half. nutrition_engine.py computes and touches no disk; this module
owns every read and write and computes nothing. Keeping the split clean is what
lets the engine be tested offline against fixtures.

STORAGE SHAPE
  athletes/<slug>/nutrition/YYYY-MM.json     one file per month
  athletes/<slug>/nutrition/cache.json       resolved-item cache (see resolve step)
Monthly files rather than one big file or one per day: a rolling 7-day plant
window and a 7-day weight mean both need several days at once, so per-day files
would mean seven opens for every reply, and a single lifetime file would be
rewritten on every logged snack. `athletes/` is gitignored, and these are written
on the VM by the bot, so never regenerate them from a Mac-local copy - that
mistake has already published stale data once.

EVERY WRITE IS ATOMIC, and for a reason specific to this app. The bot mutates an
open day across many short messages while cron jobs read the same file, so a
truncated-file window would not merely lose one snack, it would lose the day. Same
tempfile-and-replace pattern as illness.py.

DAY LOCAL DATE, NEVER SERVER UTC
Every date here is the athlete's LOCAL date, taken from the ICU athlete profile's
`current_date_local`. Europe/London is UTC+1 in summer, so a UTC-dated write after
23:00 local lands on the wrong day, and a misfiled entry is worse than a missing
one because it corrupts two days at once. Callers pass the date in; this module
never calls date.today() to decide which day an entry belongs to.

CONFIDENCE IS STORED PER ENTRY, NOT PER DAY
`confidence` is one of label | database | estimate, and `source_rung` records
which rung of the resolution ladder produced it. Both are stored so the UI can
show an estimate as an estimate. `resolved_at` exists because UK retailers
reformulate: a cached value older than CACHE_MAX_AGE_DAYS is treated as a miss and
re-resolved rather than trusted.

WHAT IS DELIBERATELY NOT STORED HERE
Body fat percentage is accepted and stored, but see the spec's reasoning: BIA fat
is a residual of weight divided by an assumed hydration constant, so in this
athlete's data it correlates with scale weight at r = 0.999. It is kept for
completeness and must never enter a calculation or a trend chart.
"""

import json
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

CONFIDENCE_LEVELS = ("label", "database", "estimate")
CACHE_MAX_AGE_DAYS = 365        # UK retailers reformulate; older is a cache miss

# Ladder rungs, in preference order. Recorded per entry so a degraded resolution
# is visible rather than silent - the bot states the rung it used.
SOURCE_RUNGS = ("cache", "retailer", "openfoodfacts", "llm")
RUNG_CONFIDENCE = {"cache": None,          # inherits whatever produced it
                   "retailer": "label",
                   "openfoodfacts": "database",
                   "llm": "estimate"}

MEASUREMENT_TYPES = ("weight", "body_fat", "rhr", "hrv")
WEIGHT_TAGS = ("morning", "session_sweat")

FLAG_TYPES = ("fat_frontload", "underfuel", "rhr_elevated", "low_variety",
              "carb_band", "fibre_ceiling_exceeded")


def _atomic_write(path: Path, payload: dict) -> None:
    """Write without a truncated-file window. The bot mutates an open day across
    many messages while cron reads it, so a crash mid-write must not cost the day."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".nut-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _as_iso(d) -> str:
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    s = str(d)
    if len(s) < 10:
        raise ValueError(f"not a usable date: {d!r}")
    return s[:10]


class NutritionStore:
    """One athlete's nutrition log. All dates are the athlete's LOCAL date.

    Instantiate with the athlete directory so this works the same on the VM and
    in tests against a tmpdir - it never resolves the athlete path itself, which
    is what makes the offline tests possible."""

    def __init__(self, athlete_dir):
        self.dir = Path(athlete_dir) / "nutrition"

    # --- file plumbing ------------------------------------------------------

    def _month_path(self, day_iso: str) -> Path:
        return self.dir / f"{day_iso[:7]}.json"

    def _load_month(self, day_iso: str) -> dict:
        p = self._month_path(day_iso)
        if not p.exists():
            return {"days": {}}
        try:
            data = json.loads(p.read_text() or "{}")
        except json.JSONDecodeError:
            # A corrupt month must not take the bot down mid-conversation. Move it
            # aside so the next write starts clean and the damaged file survives
            # for inspection rather than being silently overwritten.
            p.replace(p.with_suffix(".json.corrupt"))
            return {"days": {}}
        data.setdefault("days", {})
        return data

    def _save_month(self, day_iso: str, data: dict) -> None:
        _atomic_write(self._month_path(day_iso), data)

    @staticmethod
    def _blank_day(day_iso: str) -> dict:
        return {"date": day_iso, "entries": [], "supplements": [],
                "measurements": [], "flags": [], "targets": None,
                "day_type": None, "phase": None,
                "closed_at": None, "pushed_to_intervals_at": None}

    # --- days ---------------------------------------------------------------

    def get_day(self, day) -> dict:
        """The day's record, or a blank one. Never returns None, so callers do not
        each invent their own empty shape."""
        iso = _as_iso(day)
        return self._load_month(iso)["days"].get(iso) or self._blank_day(iso)

    def _mutate_day(self, day, fn):
        iso = _as_iso(day)
        data = self._load_month(iso)
        rec = data["days"].get(iso) or self._blank_day(iso)
        result = fn(rec)
        data["days"][iso] = rec
        self._save_month(iso, data)
        return result

    def get_range(self, start, end) -> list:
        """Days from start to end inclusive, blanks included so a caller counting a
        7-day window sees the gaps rather than silently averaging over fewer days."""
        s, e = date.fromisoformat(_as_iso(start)), date.fromisoformat(_as_iso(end))
        out, cur = [], s
        while cur <= e:
            out.append(self.get_day(cur))
            cur += timedelta(days=1)
        return out

    # --- food entries -------------------------------------------------------

    def add_entry(self, day, *, raw_text: str, resolved_name: str = "",
                  portion_g=None, kcal=0, protein_g=0, carb_g=0, fat_g=0,
                  fibre_g=0, dietary_sodium_mg=0, confidence: str = "estimate",
                  source_rung: str = "llm", source_url: str = "",
                  resolved_at=None, in_session: bool = False,
                  species=None, logged_at=None) -> dict:
        """Append one confirmed food entry. Returns the stored entry, including the
        `id` the bot needs for /undo and /edit.

        Only ever called after the user confirms. A silently written entry corrupts
        the longitudinal record and the user has no reason to notice it happened.

        `dietary_sodium_mg` is distinct from the existing in-session
        `nutrition_mg_sodium` upstream: this is salt eaten as food, that is salt
        taken on during a session. They are not interchangeable and must never be
        summed into one figure."""
        if confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"confidence must be one of {CONFIDENCE_LEVELS}")
        if source_rung not in SOURCE_RUNGS:
            raise ValueError(f"source_rung must be one of {SOURCE_RUNGS}")
        iso = _as_iso(day)

        def _add(rec):
            entry = {
                "id": f"{iso}-{len(rec['entries']) + 1:03d}",
                "logged_at": logged_at or f"{iso}T00:00",
                "raw_text": raw_text,
                "resolved_name": resolved_name or raw_text,
                "portion_g": portion_g,
                "kcal": round(float(kcal or 0), 1),
                "protein_g": round(float(protein_g or 0), 1),
                "carb_g": round(float(carb_g or 0), 1),
                "fat_g": round(float(fat_g or 0), 1),
                "fibre_g": round(float(fibre_g or 0), 1),
                "dietary_sodium_mg": round(float(dietary_sodium_mg or 0)),
                "confidence": confidence,
                "source_rung": source_rung,
                "source_url": source_url,
                "resolved_at": _as_iso(resolved_at) if resolved_at else iso,
                "in_session": bool(in_session),
                "species": list(species or []),
            }
            rec["entries"].append(entry)
            return entry

        return self._mutate_day(iso, _add)

    def undo_last(self, day) -> dict | None:
        """Remove the most recently logged entry. Returns it, or None if the day is
        empty. Corrections re-parse rather than patch, per the bot contract, so this
        is the whole of /undo."""
        return self._mutate_day(day, lambda rec: rec["entries"].pop()
                                if rec["entries"] else None)

    def remove_entry(self, day, entry_id: str) -> dict | None:
        def _rm(rec):
            for i, e in enumerate(rec["entries"]):
                if e.get("id") == entry_id:
                    return rec["entries"].pop(i)
            return None
        return self._mutate_day(day, _rm)

    # --- supplements --------------------------------------------------------

    def add_supplement(self, day, *, nutrient: str, dose, unit: str,
                       protein_g=0, timing: str = "", note: str = "") -> dict:
        """A dosed supplement. Confidence is `label` by definition: supplements are
        measured, not estimated, which is what separates them from food entries.

        `protein_g` is stored for collagen and gelatin so the figure is visible, but
        nutrition_engine.counting_protein_g deliberately keeps it out of the 180 g
        protein target: ~15 g with no tryptophan and little leucine would otherwise
        make the app show target met while real protein intake sat 15 g short every
        day. `timing` matters for collagen (30-60 min before tendon loading) and not
        at all for creatine, so it is free text rather than an enum."""
        def _add(rec):
            item = {"id": f"{_as_iso(day)}-s{len(rec['supplements']) + 1:02d}",
                    "nutrient": nutrient, "dose": dose, "unit": unit,
                    "protein_g": round(float(protein_g or 0), 1),
                    "confidence": "label", "timing": timing, "note": note}
            rec["supplements"].append(item)
            return item
        return self._mutate_day(day, _add)

    # --- measurements -------------------------------------------------------

    def add_measurement(self, day, *, type: str, value, logged_at=None,
                        tag: str = "", source: str = "manual") -> dict:
        """A weight, body-fat, RHR or HRV reading.

        `reading_index` is assigned here, counting from 0 for the day's first
        reading of that type, because the morning-versus-sweat distinction depends
        on ORDER and intervals.icu stores only one untimestamped weight per day. If
        this ordering is lost the filtering cannot be reconstructed later.

        Weight readings after the first of the day are auto-tagged session_sweat
        unless the caller says otherwise: on long-ride days the athlete weighs
        repeatedly to measure sweat rate, and those readings sit 2-3 kg low. With
        the deficit driven off rolling weight, letting one into the mean would read
        as progress that did not happen."""
        if type not in MEASUREMENT_TYPES:
            raise ValueError(f"type must be one of {MEASUREMENT_TYPES}")

        def _add(rec):
            same = [m for m in rec["measurements"] if m.get("type") == type]
            idx = len(same)
            resolved_tag = tag
            if type == "weight" and not resolved_tag:
                resolved_tag = "morning" if idx == 0 else "session_sweat"
            item = {"date": _as_iso(day), "type": type, "value": float(value),
                    "reading_index": idx,
                    "logged_at": logged_at or f"{_as_iso(day)}T00:00",
                    "tag": resolved_tag, "source": source}
            rec["measurements"].append(item)
            return item
        return self._mutate_day(day, _add)

    def measurements_range(self, start, end, type: str = "weight") -> list:
        """Flattened measurements across a date range, shaped for the engine's
        rolling_weight_kg (which expects date, value, tag, logged_at)."""
        out = []
        for rec in self.get_range(start, end):
            out.extend(m for m in rec.get("measurements") or []
                       if m.get("type") == type)
        return out

    # --- flags and close-out ------------------------------------------------

    def add_flag(self, day, *, type: str, severity: str = "info", payload=None) -> dict:
        if type not in FLAG_TYPES:
            raise ValueError(f"flag type must be one of {FLAG_TYPES}")

        def _add(rec):
            item = {"date": _as_iso(day), "type": type, "severity": severity,
                    "payload": payload or {}}
            # One flag of each type per day: the guards run on every message, and
            # without this a front-loaded fat day accumulates a dozen identical
            # flags and the flag history stops being readable.
            rec["flags"] = [f for f in rec["flags"] if f.get("type") != type]
            rec["flags"].append(item)
            return item
        return self._mutate_day(day, _add)

    def set_targets(self, day, targets: dict, day_type: str = "", phase: str = "") -> dict:
        """Store the day's computed targets alongside the log.

        Deliberately snapshotted rather than recomputed on read: targets depend on
        activity calories, which ICU revises after the fact, so a day reviewed in
        October must show the targets that were actually in force on the day, not
        what today's data would produce. Recomputing would quietly rewrite history."""
        def _set(rec):
            rec["targets"] = targets
            rec["day_type"] = day_type or targets.get("day_type")
            rec["phase"] = phase or rec.get("phase")
            return rec["targets"]
        return self._mutate_day(day, _set)

    def close_day(self, day, when=None) -> dict:
        def _close(rec):
            rec["closed_at"] = when or f"{_as_iso(day)}T23:59"
            return rec
        return self._mutate_day(day, _close)

    def mark_pushed(self, day, when=None) -> dict:
        """Record an intervals.icu push. Re-pushing after a retrospective edit is
        expected and overwrites this, because ICU wellness writes overwrite rather
        than accumulate: every push carries the full running day total, never a
        per-meal delta, or the record ends up showing only the last meal logged."""
        def _mark(rec):
            rec["pushed_to_intervals_at"] = when or f"{_as_iso(day)}T23:59"
            return rec
        return self._mutate_day(day, _mark)

    # --- totals -------------------------------------------------------------

    def day_totals(self, day) -> dict:
        """Running totals for the day, as the bot's reply and the ICU push need them.

        `protein_g` EXCLUDES collagen and gelatin (see add_supplement); the excluded
        amount is reported separately as `non_counting_protein_g` so it is visible
        rather than vanished. In-session items are counted in the totals - they are
        real calories - but also totalled separately so nothing can propose trimming
        them."""
        rec = self.get_day(day)
        entries = rec.get("entries") or []
        supps = rec.get("supplements") or []

        def s(key, rows=None):
            return round(sum(float(r.get(key) or 0) for r in (rows or entries)), 1)

        non_counting = 0.0
        counting = 0.0
        for e in entries:
            name = (e.get("resolved_name") or e.get("raw_text") or "").lower()
            grams = float(e.get("protein_g") or 0)
            if "collagen" in name or "gelatin" in name or "gelatine" in name:
                non_counting += grams
            else:
                counting += grams
        non_counting += sum(float(x.get("protein_g") or 0) for x in supps
                            if "collagen" in (x.get("nutrient") or "").lower()
                            or "gelatin" in (x.get("nutrient") or "").lower())

        in_sess = [e for e in entries if e.get("in_session")]
        return {
            "date": rec["date"],
            "kcal": s("kcal"),
            "protein_g": round(counting, 1),
            "non_counting_protein_g": round(non_counting, 1),
            "carb_g": s("carb_g"),
            "fat_g": s("fat_g"),
            "fibre_g": s("fibre_g"),
            "dietary_sodium_mg": round(s("dietary_sodium_mg")),
            "in_session_kcal": s("kcal", in_sess),
            "in_session_carb_g": s("carb_g", in_sess),
            "entry_count": len(entries),
            "lowest_confidence": (
                "estimate" if any(e.get("confidence") == "estimate" for e in entries)
                else "database" if any(e.get("confidence") == "database" for e in entries)
                else "label" if entries else None),
        }

    # --- resolved-item cache ------------------------------------------------

    def _cache_path(self) -> Path:
        return self.dir / "cache.json"

    def cache_get(self, key: str, on=None) -> dict | None:
        """A cached resolution, or None if absent or stale.

        Stale is a MISS, not a warning: UK retailers reformulate, so a two-year-old
        cached macro set is unreliable and silently trusting it would be the worst
        of both worlds - label-grade confidence on a figure nobody has checked
        since."""
        p = self._cache_path()
        if not p.exists():
            return None
        try:
            cache = json.loads(p.read_text() or "{}")
        except json.JSONDecodeError:
            return None
        hit = cache.get(key.strip().lower())
        if not hit:
            return None
        resolved = hit.get("resolved_at")
        if resolved:
            ref = date.fromisoformat(_as_iso(on)) if on else date.today()
            try:
                if (ref - date.fromisoformat(resolved[:10])).days > CACHE_MAX_AGE_DAYS:
                    return None
            except ValueError:
                return None
        return hit

    def cache_put(self, key: str, payload: dict) -> None:
        p = self._cache_path()
        cache = {}
        if p.exists():
            try:
                cache = json.loads(p.read_text() or "{}")
            except json.JSONDecodeError:
                cache = {}
        cache[key.strip().lower()] = payload
        _atomic_write(p, cache)

    def log_unresolved(self, raw_text: str, day=None) -> None:
        """Record a string the ladder could not map, for the review queue.

        Unmapped strings are logged rather than dropped because the plant-diversity
        count depends on canonicalisation: an unmapped variant either inflates the
        species count or is missed entirely, and both corrupt the headline metric
        quietly. This is the admin path that lets mappings be added without a
        redeploy."""
        p = self.dir / "unresolved.json"
        rows = []
        if p.exists():
            try:
                rows = json.loads(p.read_text() or "[]")
            except json.JSONDecodeError:
                rows = []
        rows.append({"raw_text": raw_text, "seen_on": _as_iso(day or date.today())})
        _atomic_write_list(p, rows)


def _atomic_write_list(path: Path, payload: list) -> None:
    """Same guarantee as _atomic_write, for the JSON-array files (unresolved queue)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".nut-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
