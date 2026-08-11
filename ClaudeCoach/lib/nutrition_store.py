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

import contextlib
import fcntl
import json
import re
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nutrition_engine import NON_COUNTING_PROTEIN_SOURCES  # noqa: E402

# `computed` is a real fourth level, not a shade of estimate: the M&S nut collection was
# derived from an equal blend of four named nuts per the product listing, which is far
# more defensible than a model guess and far less than a printed panel. Collapsing it
# into either neighbour would misreport how good the figure is.
CONFIDENCE_LEVELS = ("label", "computed", "database", "estimate")
CACHE_MAX_AGE_DAYS = 365        # UK retailers reformulate; older is a cache miss

# Ladder rungs, in preference order. Recorded per entry so a degraded resolution
# is visible rather than silent - the bot states the rung it used.
SOURCE_RUNGS = ("cache", "vendor", "retailer", "cofid", "usda", "openfoodfacts",
                "nutritionix", "manual", "computed", "web", "llm")
RUNG_CONFIDENCE = {# A chain publishing figures for its own dish IS the manufacturer of
                   # that dish, so this is label data - not a database lookup, and
                   # certainly not an estimate.
                   "vendor": "label",
                   "cache": None,          # inherits whatever produced it
                   "retailer": "label",    # the actual product listing
                   "cofid": "label",       # PHE McCance & Widdowson, UK whole foods
                   "manual": "label",      # the athlete read it off the pack
                   "computed": "computed",  # summed from a known ingredient blend
                   "usda": "database",
                   "openfoodfacts": "database",
                   "nutritionix": "database",
                   # `web` is the model doing a real search. Its confidence depends on
                   # WHAT it found, so callers pass it explicitly: a manufacturer or
                   # retailer page is label data, anything vaguer is an estimate.
                   "web": "database",
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

    @contextlib.contextmanager
    def _month_lock(self, day_iso: str):
        """Cross-process exclusive lock on one month, held across read-modify-write.

        Atomic writes stop a TORN file; they do not stop a LOST UPDATE. Two writers
        that both load the month, both append, and both replace leave only the
        second writer's entry, and the first is gone with no error anywhere. That
        is a live risk here rather than a theoretical one: the bot mutates an open
        day across many short messages while refresh-site-data runs on a */10 cron
        and the evening close-out fires.

        bot.py's `_chat_lock` cannot serve for this - it is an in-process
        threading.Lock keyed by chat_id, so it serialises one bot process against
        itself and is blind to cron. Hence flock, on a sidecar lock file rather
        than the month file itself: os.replace swaps the inode, so a lock held on
        the old file would not be seen by the next writer."""
        with self._file_lock(day_iso[:7]):
            yield

    @contextlib.contextmanager
    def _file_lock(self, name: str):
        """flock on a sidecar lock file. Used by every read-modify-write in here,
        including the cache, which is a single shared file that the resolution
        ladder will hammer."""
        self.dir.mkdir(parents=True, exist_ok=True)
        fh = open(self.dir / f".{name}.lock", "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()

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
        # next_seq / next_supp_seq are MONOTONIC and never derived from list length.
        # Deriving from length reuses ids after a removal: log 001-003, /undo, log
        # again and the new entry is another 003, so remove_entry and the ICU
        # re-push after a retrospective edit both act on the wrong row. Sequence
        # numbers are only ever handed out, never handed back.
        return {"date": day_iso, "entries": [], "supplements": [],
                "measurements": [], "flags": [], "targets": None,
                "day_type": None, "phase": None, "next_seq": 1, "next_supp_seq": 1,
                "closed_at": None, "pushed_to_intervals_at": None}

    # --- days ---------------------------------------------------------------

    def get_day(self, day) -> dict:
        """The day's record, or a blank one. Never returns None, so callers do not
        each invent their own empty shape.

        Not side-effect free: a month file that will not parse is moved aside to
        `.json.corrupt` here, so a cron READ can mutate the directory. That is
        deliberate (a corrupt file must not take the bot down mid-conversation) but
        surprising enough to be worth stating."""
        iso = _as_iso(day)
        return self._load_month(iso)["days"].get(iso) or self._blank_day(iso)

    def _next_seq(self, rec: dict, key: str, prefix: str, width: int) -> str:
        """Hand out the next sequence number, tolerating a record written before
        these counters existed by seeding past the highest id already present."""
        nxt = rec.get(key)
        if not nxt:
            field = "entries" if key == "next_seq" else "supplements"
            used = [int((r.get("id") or "").rsplit(prefix, 1)[-1] or 0)
                    for r in rec.get(field) or []]
            nxt = (max(used) + 1) if used else 1
        rec[key] = nxt + 1
        return f"{rec['date']}-{prefix}{nxt:0{width}d}"

    def _mutate_day(self, day, fn):
        """Read-modify-write under the month lock. Every mutation goes through here,
        which is what makes the locking complete rather than best-effort."""
        iso = _as_iso(day)
        with self._month_lock(iso):
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
                  fibre_g=0, dietary_sodium_mg=0, plants_claimed=None,
                  confidence: str = "estimate",
                  source_rung: str = "llm", source_url: str = "",
                  resolved_at=None, in_session: bool = False,
                  species=None, ingredients: str = "", logged_at=None,
                  meal: str = "") -> dict:
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
                "id": self._next_seq(rec, "next_seq", "", 3),
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
            "meal": (meal or "").strip().lower(),
                # Each species is {"id", "score"}: the score is the one MATCHED, which
                # is 0 for a refined derivative. Storing bare ids lost it and read the
                # category default back, turning sunflower oil into sunflower.
                "species": [s if isinstance(s, dict) else {"id": s, "score": None}
                            for s in (species or [])],
                # Kept so species can be re-derived after a table change without going
                # back to the network.
                "ingredients": ingredients or "",
                # What the pack CLAIMS, when it says so. Kept beside what we could
                # actually name, never instead of it.
                "plants_claimed": plants_claimed,
            }
            rec["entries"].append(entry)
            return entry

        return self._mutate_day(iso, _add)

    # The four buckets the app renders. "snacks" is the catch-all rather than an
    # unlabelled fifth, so nothing can land nowhere.
    MEALS = ("breakfast", "lunch", "dinner", "snacks")

    def set_meal(self, day, entry_id: str, meal: str) -> dict | None:
        """Say which meal an entry belongs to.

        Meals were inferred from the clock alone, so an entry logged at 08:52 was
        breakfast and one logged at 11:30 was lunch whatever it actually was - and there
        was no way to correct it, because entries carried no meal at all. Telling the bot
        "that was breakfast" had nowhere to land.

        The STATED meal always wins over the clock: he knows when he ate, and a log
        written up an hour later is normal."""
        meal = (meal or "").strip().lower()
        if meal in ("snack", "snacking"):
            meal = "snacks"
        if meal not in self.MEALS:
            return None

        def fn(rec):
            for e in rec.get("entries") or []:
                if e.get("id") == entry_id:
                    e["meal"] = meal
                    return e
            return None
        return self._mutate_day(day, fn)

    def find_entry(self, day, text: str = "") -> dict | None:
        """The entry he means: named if he named one, otherwise the most recent.

        Matched on shared words rather than a substring, so "the oats" finds "M&S Salted
        Caramel Overnight Oats" without "the bar" also matching every bar-shaped name it
        appears inside."""
        entries = self.get_day(day).get("entries") or []
        if not entries:
            return None
        words = {w for w in re.split(r"[^a-z0-9]+", (text or "").lower())
                 if len(w) > 2} - {"was", "the", "that", "this", "for", "had", "and"}
        if words:
            best, score = None, 0
            for e in entries:
                name = {w for w in re.split(r"[^a-z0-9]+",
                                            (e.get("resolved_name") or "").lower())
                        if len(w) > 2}
                hit = len(words & name)
                if hit > score:
                    best, score = e, hit
            if best is not None:
                return best
        return entries[-1]

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
            item = {"id": self._next_seq(rec, "next_supp_seq", "s", 2),
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
        as progress that did not happen.

        `reading_index` is derived from the count because there is deliberately no
        removal path for measurements: a weigh-in is a fact, and correcting one
        means correcting the value, not deleting the reading. If a removal path is
        ever added, this must move to a monotonic counter like next_seq, or indices
        will be reused and the morning-versus-sweat ordering will be wrong."""
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
            # `rows if rows is not None` - NOT `rows or entries`. An EMPTY in-session
            # list is falsy, so the `or` fell through to the whole day and
            # in_session_kcal reported every calorie eaten as protected in-session
            # fuel whenever nothing was actually in-session.
            src = entries if rows is None else rows
            return round(sum(float(r.get(key) or 0) for r in src), 1)

        # The non-counting token list lives ONCE, in nutrition_engine. An earlier
        # cut inlined the same three tokens here, which would have meant adding
        # "bone broth" to the engine and having the store quietly keep counting it.
        # Same divergence trap this project refused for the fuelling constant.
        counting = non_counting = 0.0
        for e in entries:
            name = (e.get("resolved_name") or e.get("raw_text") or "").lower()
            grams = float(e.get("protein_g") or 0)
            if any(tok in name for tok in NON_COUNTING_PROTEIN_SOURCES):
                non_counting += grams
            else:
                counting += grams
        non_counting += sum(
            float(x.get("protein_g") or 0) for x in supps
            if any(tok in (x.get("nutrient") or "").lower()
                   for tok in NON_COUNTING_PROTEIN_SOURCES))

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
            # Worst wins, in ladder order, so the day can never read better than its
            # weakest item.
            "lowest_confidence": next(
                (lvl for lvl in ("estimate", "database", "computed", "label")
                 if any(e.get("confidence") == lvl for e in entries)), None),
            # Front-of-pack plant claims we could not name. A composite product saying
            # "14 plants" whose ingredients we never retrieved leaves the count honestly
            # incomplete rather than silently low.
            "plants_claimed_unresolved": sum(
                max(0, int(e.get("plants_claimed") or 0) - len(e.get("species") or []))
                for e in entries),
        }

    # --- resolved-item cache ------------------------------------------------

    # --- conversation ------------------------------------------------------
    #
    # Kept because a coach who forgets the previous sentence is not having a conversation.
    # "why?" and "what about the other one instead?" are the natural follow-ups and both
    # are unanswerable without the turn before them.
    #
    # Deliberately SHORT and deliberately not permanent: enough to hold a thread, not a
    # second record of what he ate. The log is the record; this is working memory.
    CHAT_TURNS_KEPT = 16

    def _chat_path(self) -> Path:
        return self.dir / "chat.json"

    def append_chat(self, role: str, text: str, when=None) -> None:
        """One turn. Under the file lock, because the bot can be answering a message
        while a photo callback writes another."""
        if not (text or "").strip():
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._file_lock("chat"):
            p = self._chat_path()
            try:
                turns = json.loads(p.read_text()) if p.exists() else []
            except (json.JSONDecodeError, OSError):
                turns = []
            turns.append({"role": role, "text": text[:2000],
                          "at": (when or datetime.now()).isoformat(timespec="minutes")})
            p.write_text(json.dumps(turns[-self.CHAT_TURNS_KEPT:], indent=1))

    def recent_chat(self, limit: int = None) -> list:
        p = self._chat_path()
        if not p.exists():
            return []
        try:
            turns = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return []
        return turns[-(limit or self.CHAT_TURNS_KEPT):]

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
        with self._file_lock("cache"):
            cache = {}
            if p.exists():
                try:
                    cache = json.loads(p.read_text() or "{}")
                except json.JSONDecodeError:
                    cache = {}
            cache[key.strip().lower()] = payload
            _atomic_write(p, cache)

    def log_unresolved(self, raw_text: str, day) -> None:
        """Record a string the ladder could not map, for the review queue.

        Unmapped strings are logged rather than dropped because the plant-diversity
        count depends on canonicalisation: an unmapped variant either inflates the
        species count or is missed entirely, and both corrupt the headline metric
        quietly. This is the admin path that lets mappings be added without a
        redeploy.

        `day` is required, not defaulted to date.today(): this module never decides
        which local day something belongs to, because a UTC-dated write after 23:00
        London time lands on the wrong day."""
        p = self.dir / "unresolved.json"
        with self._month_lock(_as_iso(day)):
            rows = []
            if p.exists():
                try:
                    rows = json.loads(p.read_text() or "[]")
                except json.JSONDecodeError:
                    rows = []
            rows.append({"raw_text": raw_text, "seen_on": _as_iso(day)})
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
