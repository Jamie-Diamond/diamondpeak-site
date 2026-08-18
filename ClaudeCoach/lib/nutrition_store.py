#!/usr/bin/env python3
"""nutrition_store.py - the day log: food entries, supplements, measurements, flags.

Step 1's other half. nutrition_engine.py computes and touches no disk; this module
owns every read and write and computes nothing. Keeping the split clean is what
lets the engine be tested offline against fixtures.

STORAGE SHAPE
  athletes/<slug>/nutrition/YYYY-MM.json     one file per month
  athletes/<slug>/nutrition/cache.json       resolved-item cache (see resolve step).
                                             A flat dict, whose rows are either a
                                             payload or an {"alias_of": key} pointer -
                                             see the cache section below.
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
import copy
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
# The allowed fact fields live ONCE, where the decision that produces them is validated.
# Restating them here is the divergence trap this file already refuses for the
# non-counting protein tokens: a field the model may return and the store will not keep
# fails silently, which is the worst of both.
from nutrition_nlu import MEALS, PRODUCT_FACT_FIELDS, normalise_meal  # noqa: E402,F401

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

# Clock bounds for the FALLBACK, when nothing he said named a meal. Named constants
# because they are a judgement about his day, not a fact: he eats breakfast late on a
# rest day and dinner is never before half five.
MEAL_BREAKFAST_BEFORE = "11:00"
MEAL_LUNCH_BEFORE = "15:00"
MEAL_SNACKS_BEFORE = "17:30"      # from here on it is dinner


def meal_from_clock(logged_at: str) -> str:
    """Which meal a time of day implies, or "" if the time is unreadable.

    THE FALLBACK, AND ONLY THE FALLBACK. It runs at LOG time rather than at publish
    time, against the time the entry claims to have been eaten at - so an 08:30 slice of
    rye bread written up at 13:49 is breakfast, which is the whole complaint (Jamie,
    13 Aug 2026: "often added to wrong category"). Whatever it decides is marked
    `meal_inferred`, because a guess that cannot be told from a statement is a guess the
    athlete has no reason to check."""
    hhmm = (logged_at or "")[11:16] if len(logged_at or "") > 11 else (logged_at or "")
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", hhmm or ""):
        return ""
    if hhmm < MEAL_BREAKFAST_BEFORE:
        return "breakfast"
    if hhmm < MEAL_LUNCH_BEFORE:
        return "lunch"
    if hhmm < MEAL_SNACKS_BEFORE:
        return "snacks"
    return "dinner"


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
                  meal: str = "", stated_fields=None) -> dict:
        """Append one confirmed food entry. Returns the stored entry, including the
        `id` the bot needs for /undo and /edit.

        Only ever called after the user confirms. A silently written entry corrupts
        the longitudinal record and the user has no reason to notice it happened.

        `dietary_sodium_mg` is distinct from the existing in-session
        `nutrition_mg_sodium` upstream: this is salt eaten as food, that is salt
        taken on during a session. They are not interchangeable and must never be
        summed into one figure.

        `stated_fields` names the figures above that came from HIM rather than from a
        lookup - see the note beside the entry dict."""
        if confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"confidence must be one of {CONFIDENCE_LEVELS}")
        if source_rung not in SOURCE_RUNGS:
            raise ValueError(f"source_rung must be one of {SOURCE_RUNGS}")
        iso = _as_iso(day)
        stamp = logged_at or f"{iso}T00:00"
        # WHICH MEAL, DECIDED HERE AND NOW rather than by the app when it renders. What he
        # said wins; the clock is consulted only when he said nothing, and its answer is
        # flagged as inferred so a correction and the app can both tell the two apart.
        #
        # In-session fuel is NOT a meal and never gets one, whatever arrived in `meal`: a
        # gel taken at 13:00 is not lunch, and filing it as lunch is what makes a day's
        # meals look like they contain the fuelling. Forced rather than refused - a
        # mislabelled gel must not cost him the log.
        chosen = "" if in_session else normalise_meal(meal)
        inferred = False
        if not chosen and not in_session:
            chosen = meal_from_clock(stamp)
            inferred = bool(chosen)
        # Normalised to plain strings and no further. The store deliberately does NOT check
        # these names against a field list: rescale_item already ignores anything outside
        # its own `_RESCALE_FIELDS` and skips a None, so a stray name is inert, whereas a
        # second definition here of "which figures count" would drift from that one and from
        # `MACRO_FIELDS`. Different case from `confidence` and `source_rung`, which are
        # closed enums this module owns and so are validated above.
        his = [str(f) for f in (stated_fields or ())]

        def _add(rec):
            entry = {
                "id": self._next_seq(rec, "next_seq", "", 3),
                "logged_at": stamp,
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
                "meal": chosen,
                # True when the CLOCK chose that meal, not him. publish reads `meal` for
                # the bucket and this for whether to caveat it.
                "meal_inferred": inferred,
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
            # WHICH OF THE FIGURES ABOVE ARE HIS (17 Aug 2026). resolve() can lay a macro the
            # athlete stated over whatever the ladder found - "chicken salad with 21g protein"
            # keeps his 21 g - and rescale_item reads this list to know what it may not
            # recompute when he later answers "how much was it?". This signature had no such
            # keyword, so the flag died at the commit and only the flag did: the 21 g went into
            # the record looking exactly like a figure a lookup produced. The very next
            # correction against that row multiplied it, and the reply showed him 42 g of
            # protein under his own name. His feedback log already carries an invented RPE 8
            # and an invented 300 mg of sodium; this is the same class and he escalated both.
            #
            # WRITTEN ONLY WHEN THERE IS SOMETHING TO SAY. These month files are a
            # longitudinal record he keeps and reads, and an entry nobody stated a figure for
            # - still almost every entry - stays byte-for-byte the shape it has always been,
            # rather than growing a `"stated_fields": []` that means nothing. Rows written
            # before today simply do not have the key, which is the same absence, so no
            # migration is needed and every reader already spells it
            # `.get("stated_fields") or ()`.
            if his:
                entry["stated_fields"] = his
            rec["entries"].append(entry)
            return entry

        return self._mutate_day(iso, _add)

    # Kept as a class attribute for the callers that read store.MEALS; the list itself
    # lives at module level with the aliases and the clock bounds it belongs beside.
    MEALS = MEALS

    # `logged_at` and `meal` are patchable because WHEN he ate something is a fact he can
    # state after the fact and the app buckets entries into meals by the clock: an 08:30
    # slice of rye bread written up at 14:00 read as lunch, and the only way to move it
    # was to edit the month file by hand. Still not identity - see update_entry.
    #
    # `stated_fields` is patchable for the same reason it is stored at all (17 Aug 2026):
    # it is the only thing that tells a later rescale which of the row's figures are not
    # ours to recompute, and a row that lost it silently starts multiplying his own number.
    # It is not a figure, so it does not belong with the six above; it is the note saying
    # which of them he gave us. Note that add_entry omits the key entirely when he stated
    # nothing, and this list does not restore that: passing `stated_fields=None` here sets
    # the key to None rather than removing it. Harmless, because every reader spells it
    # `.get("stated_fields") or ()`, but it is why the label path below pops the key instead
    # of patching it.
    UPDATABLE = ("kcal", "protein_g", "carb_g", "fat_g", "fibre_g",
                 "dietary_sodium_mg", "portion_g", "portion_used_g",
                 "portion_estimated", "portion_assumed", "raw_text",
                 "logged_at", "meal", "meal_inferred", "stated_fields")

    def update_entry(self, day, entry_id: str, **fields) -> dict | None:
        """Patch an existing entry in place - the QUANTITY correction path.

        Added 13 Aug 2026: "that's 100g, I had 160g" against a committed entry had no
        way to land except delete-and-relog, and the relog re-ran the ladder, which is
        how a correction that is pure arithmetic came back as a different food. Only
        amount-shaped fields are patchable: identity (resolved_name, source, species)
        stays immutable here, because changing WHAT it was is a re-resolution, not a
        patch."""
        patch = {k: v for k, v in fields.items() if k in self.UPDATABLE}
        if not patch:
            return None

        def fn(rec):
            for e in rec.get("entries") or []:
                if e.get("id") == entry_id:
                    e.update(patch)
                    # A RETIME MOVES A GUESSED MEAL WITH IT. Meals used to be re-derived
                    # from logged_at every time the app was published, so retiming an
                    # entry moved its bucket for free; freezing the meal at log time
                    # silently took that away. "The initial rye bread was 830am" has to
                    # take it out of lunch and into breakfast, or the retime verb fixes
                    # the timestamp and leaves the visible mistake in place.
                    #
                    # A meal HE stated survives, deliberately: a late breakfast at 14:00
                    # is a real thing, and it is exactly what this ticket is about.
                    if ("logged_at" in patch and "meal" not in patch
                            and not e.get("in_session")
                            and (e.get("meal_inferred") or not e.get("meal"))):
                        moved = meal_from_clock(e.get("logged_at") or "")
                        if moved:
                            e["meal"] = moved
                            e["meal_inferred"] = True
                    return e
            return None
        return self._mutate_day(day, fn)

    # Whose figures survive a renaming. A label or a manually-entered pack reading is HIS
    # OWN measurement of the thing in his hand, so calling it by its right name does not
    # make the numbers wrong. Anything from a lookup is the opposite case: the name IS
    # what produced the figures, so a new name invalidates them and the item has to be
    # re-resolved rather than relabelled.
    RENAMEABLE_CONFIDENCE = ("label",)
    RENAMEABLE_RUNGS = ("manual",)

    def rename_entry(self, day, entry_id: str, name: str,
                     ingredients: str = None) -> dict | None:
        """Correct an entry's NAME while keeping its figures. None if refused.

        A separate verb from update_entry rather than a widening of it: update_entry
        deliberately blocks identity, and that block is what stops a quantity correction
        turning into a different food. This is the one case where the athlete outranks
        the ladder - "the 160g was a pack of bbq chicken" against figures he read off
        that pack himself. Refuses on a database or estimate entry, where a new name
        means the lookup was wrong and re-resolution is the honest answer.

        The old ingredients and species go with the old name: they described a product
        this entry is not, and leaving them would credit the plant-diversity count to a
        food he never ate."""
        name = (name or "").strip()
        if not name:
            return None

        def fn(rec):
            for e in rec.get("entries") or []:
                if e.get("id") != entry_id:
                    continue
                if (e.get("confidence") not in self.RENAMEABLE_CONFIDENCE
                        and e.get("source_rung") not in self.RENAMEABLE_RUNGS):
                    return None
                e["renamed_from"] = e.get("renamed_from") or e.get("resolved_name")
                e["resolved_name"] = name
                if ingredients is not None:
                    e["ingredients"] = ingredients
                    return e
                e["ingredients"] = ""
                e["species"] = []
                return e
            return None
        return self._mutate_day(day, fn)

    # The figure fields a label supersedes. Kept beside RENAMEABLE_CONFIDENCE because the
    # two answer the same question from opposite ends: that one is when a NAME may change
    # without the figures, this is when the FIGURES may change - and the name with them.
    LABEL_FIGURE_FIELDS = ("kcal", "protein_g", "carb_g", "fat_g", "fibre_g",
                           "dietary_sodium_mg")

    def apply_label_to_entry(self, day, entry_id: str, label: dict) -> dict | None:
        """Replace an entry's figures with a photographed label's. None if not found.

        THE BUG THIS EXISTS FOR (14 Aug 2026). He logged "Coop Chianti beef pizza" by name,
        got a web figure of 1,147 kcal, then photographed the pack to correct it - and the
        label path treats every panel as a NEW item, so the pizza was offered a second time
        and logged twice. A label is the manufacturer's own printed panel: the best rung
        this bot ever holds, and against an entry priced off a lookup it is a correction,
        not a second dinner.

        A separate verb from both update_entry and rename_entry, and the ONE place identity
        may move with the figures: a label supersedes the lookup that produced the old name,
        so keeping "Chianti beef pizza" while writing the pack's figures would leave an
        entry describing neither. That is not the rename guard's concern - that guard exists
        to stop a NAME change silently keeping figures a lookup produced for a different
        food, which is the opposite direction."""
        if not isinstance(label, dict):
            return None

        def fn(rec):
            for e in rec.get("entries") or []:
                if e.get("id") != entry_id:
                    continue
                replaced = []
                for f in self.LABEL_FIGURE_FIELDS:
                    v = label.get(f)
                    if v is None:
                        continue
                    e[f] = (round(float(v)) if f == "dietary_sodium_mg"
                            else round(float(v), 1))
                    replaced.append(f)
                # A FIGURE THE PANEL JUST OVERWROTE IS NO LONGER HIS (17 Aug 2026). New here
                # only because entries carry `stated_fields` from today: before that, a
                # stored row could not claim his authorship of anything. Now it can, and the
                # loop above has just replaced the number the claim was about with the
                # manufacturer's. Left alone, the row would say the pack's protein came from
                # him - the next rescale would refuse to scale it and fmt_confirm would
                # caption it "your own figure", which is the same fabricated attribution
                # this whole change exists to stop, only pointing the other way.
                #
                # Same argument the function already makes two paragraphs down for scrubbing
                # `species` and `ingredients`: what described the old figures stops being
                # true the moment the figures move. Only the fields the label actually
                # carried are dropped - a panel with no fibre row does not disturb a fibre
                # figure he gave. The key is popped rather than set empty, to leave the row
                # in the shape add_entry would have written.
                if e.get("stated_fields") and replaced:
                    kept = [f for f in e["stated_fields"] if f not in replaced]
                    if kept:
                        e["stated_fields"] = kept
                    else:
                        e.pop("stated_fields", None)
                grams = label.get("portion_used_g") or label.get("portion_g")
                if grams:
                    e["portion_g"] = float(grams)
                    e["portion_used_g"] = float(grams)
                    e["portion_estimated"] = False
                    e["portion_assumed"] = f"{float(grams):.0f} g - from the label"
                # THE BASIS TRAVELS, so the next "I had 160g" is a multiplication rather
                # than a fresh search - the whole reason label_to_item keeps it.
                if label.get("per_100g"):
                    e["per_100g"] = label["per_100g"]
                if label.get("pack_g"):
                    e["pack_g"] = float(label["pack_g"])
                name = (label.get("resolved_name") or "").strip()
                if name and name != e.get("resolved_name"):
                    e["renamed_from"] = e.get("renamed_from") or e.get("resolved_name")
                    e["resolved_name"] = name
                    # The old ingredients and species described the food the LOOKUP found.
                    # Same rule as rename_entry: they go with the old name, or the plant
                    # count keeps crediting something he never ate.
                    e["species"] = []
                    e["ingredients"] = label.get("ingredients") or ""
                elif label.get("ingredients"):
                    e["ingredients"] = label["ingredients"]
                else:
                    # A BREAKDOWN THAT NO LONGER ADDS UP GOES, even when the name is
                    # unchanged. A costed meal commits its component rows into
                    # `ingredients`, and the app renders them under the entry's total: with
                    # the total replaced by a pack's panel, those rows contradict the
                    # heading they sit beneath. Same rule as the bot's drop_stale_breakdown,
                    # for the same reason - the rows described the old figures and stop
                    # being true the moment the figures move. `species` stays, because it
                    # is stored in its own field and the food itself has not changed.
                    e["ingredients"] = e.get("resolved_name") or ""
                e["confidence"] = "label"
                e["source_rung"] = "manual"
                e["source_url"] = label.get("source_url") or "photo of the product label"
                return e
            return None
        return self._mutate_day(day, fn)

    def set_meal(self, day, entry_id: str, meal: str) -> dict | None:
        """Say which meal an entry belongs to.

        Meals were inferred from the clock alone, so an entry logged at 08:52 was
        breakfast and one logged at 11:30 was lunch whatever it actually was - and there
        was no way to correct it, because entries carried no meal at all. Telling the bot
        "that was breakfast" had nowhere to land.

        The STATED meal always wins over the clock: he knows when he ate, and a log
        written up an hour later is normal. So this also clears `meal_inferred` - the
        entry is now filed because he said so, and nothing downstream should keep
        caveating it as a guess."""
        meal = normalise_meal(meal)
        if not meal:
            return None

        def fn(rec):
            for e in rec.get("entries") or []:
                if e.get("id") == entry_id:
                    e["meal"] = meal
                    e["meal_inferred"] = False
                    return e
            return None
        return self._mutate_day(day, fn)

    def set_in_session(self, day, entry_id: str, in_session: bool) -> dict | None:
        """Move an entry in or out of session fuel after the fact.

        The meal follows the move, because in-session fuel is not a meal. Leaving a stale
        `lunch` on a gel he has just told us was taken on the bike is not cosmetic: publish
        buckets a STATED meal ahead of its in-session check, so the gel would keep
        rendering under lunch while the flag beside it said in-session."""
        def fn(rec):
            for e in rec.get("entries") or []:
                if e.get("id") != entry_id:
                    continue
                e["in_session"] = bool(in_session)
                if in_session:
                    e["meal"] = ""
                    e["meal_inferred"] = False
                elif not e.get("meal"):
                    # Back out of session, so it is food again and needs a bucket. The
                    # clock is all there is to go on, and it says so.
                    e["meal"] = meal_from_clock(e.get("logged_at") or "")
                    e["meal_inferred"] = bool(e["meal"])
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

    # --- rejected candidates ------------------------------------------------

    def add_exclusion(self, day, phrase: str) -> list:
        """Remember that the athlete has REJECTED a resolution, for the rest of the day.

        THE BUG THIS EXISTS FOR. "butter" resolved to "Peanut butter, smooth" six times
        on 12 Aug 2026, including twice after he said "I never said peanut butter" - a
        correction re-ran the ladder from scratch, the ladder is deterministic, so it
        returned the same wrong thing and asked him to confirm it again. A correction has
        to leave a mark or the loop is unbreakable by design.

        Stored per DAY rather than forever: he is rejecting this match for this item, not
        telling the app that peanut butter is never food. A standing blocklist would need
        managing, and nobody manages one."""
        phrase = (phrase or "").strip().strip(".,;:!?\"'").lower()
        if not phrase:
            return []

        def _add(rec):
            out = list(rec.get("exclusions") or [])
            if phrase not in out:
                out.append(phrase)
            rec["exclusions"] = out
            return out
        return self._mutate_day(day, _add)

    def get_exclusions(self, day) -> list:
        """Phrases the athlete has rejected today. Passed to the resolution ladder."""
        return list(self.get_day(day).get("exclusions") or [])

    def undo_last(self, day) -> dict | None:
        """Remove the most recently logged entry. Returns it, or None if the day is
        empty. Corrections re-parse rather than patch, per the bot contract, so this
        is the whole of /undo."""
        return self._mutate_day(day, lambda rec: rec["entries"].pop()
                                if rec["entries"] else None)

    def move_entry(self, from_day, entry_id: str, to_day, logged_at=None,
                   meal: str = None) -> dict | None:
        """Move one entry to ANOTHER DAY. Returns {"moved", "removed"}, or None when the
        entry is not on `from_day` to begin with.

        THE DEFECT THIS EXISTS FOR (16 Aug 2026). "Dinner last night was a big salad" was
        costed correctly and written to today, and his correction - "that was for
        yesterday's dinner" - had nowhere to land: retime could move an entry's clock time
        and nothing could move it across days. The entry was moved by hand in the month
        file, which is the outcome this whole module exists to make unnecessary.

        A DEEP COPY, NOT A RE-ADD. Routing this through add_entry would look tidier and
        would quietly rebuild the entry: it re-derives the meal from the clock, re-rounds
        every macro, rebuilds `species` from whatever it was handed, and its literal names
        no `portion_used_g`, `portion_estimated`, `portion_assumed`, `species_from` or
        `species_unmatched` at all, so a moved entry would come out shorter than it went
        in. Provenance is the point of this record; a move must not cost any of it. Only
        the id changes - the target day hands out its own, because ids carry their date -
        plus logged_at and meal when the caller says so.

        TARGET FIRST, THEN THE SOURCE. The month lock is per month file, so a move from
        the 1st to the 31st of the month before is two lock acquisitions and cannot be
        atomic. Written in this order, a failure between them leaves a duplicate he can
        see and delete rather than an entry that no longer exists anywhere - the same rule
        the bot already follows when a confirmed replacement removes what it replaced."""
        from_iso, to_iso = _as_iso(from_day), _as_iso(to_day)
        original = next((e for e in self.get_day(from_iso).get("entries") or []
                         if e.get("id") == entry_id), None)
        if original is None:
            return None
        if from_iso == to_iso:
            # Not a move. Refused rather than performed as a copy-and-delete, which would
            # hand the entry a new id for no reason and repoint /undo at it.
            return None
        moved = copy.deepcopy(original)
        if logged_at:
            moved["logged_at"] = logged_at
        elif len(str(moved.get("logged_at") or "")) >= 16:
            # KEEP HIS TIME OF DAY, change only the date. "That was yesterday's dinner"
            # says nothing about the clock, and re-stamping it would move the entry out of
            # dinner as a side effect of moving it out of today.
            moved["logged_at"] = f"{to_iso}T{str(moved['logged_at'])[11:16]}"
        else:
            moved["logged_at"] = f"{to_iso}T00:00"
        if meal:
            moved["meal"] = normalise_meal(meal) or moved.get("meal") or ""
            moved["meal_inferred"] = False
        moved["resolved_at"] = moved.get("resolved_at") or to_iso

        def _put(rec):
            moved["id"] = self._next_seq(rec, "next_seq", "", 3)
            rec["entries"].append(moved)
            return moved

        written = self._mutate_day(to_iso, _put)
        removed = self.remove_entry(from_iso, entry_id)
        return {"moved": written, "removed": removed}

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

    # ONE PAYLOAD, SEVERAL WAYS OF ASKING FOR IT (18 Aug 2026). The cache file is still a
    # flat {key: row} dict, but a row is now one of two things: a PAYLOAD, or an ALIAS -
    # {"alias_of": "<the payload's key>"} - so several keys can reach one resolution.
    #
    # Aliases are POINTERS rather than copies on purpose. Copying the payload under each
    # key was the obvious alternative and it rots: re-resolve a product and only the key
    # you happened to come in under is refreshed, so the same food answers with two
    # different macro sets depending on which words were used - the exact class of
    # silent divergence this module is arranged against. A pointer has one source of
    # truth, and a dangling one is a MISS, not a crash.
    #
    # OLD CACHE FILES NEED NO MIGRATION. Every row in a file written before this is a
    # payload keyed on the athlete's words; nothing in here treats an unknown row shape
    # as an alias, so those rows keep answering exactly as they did. They simply have no
    # alias rows pointing at them until the next time that food is resolved and written
    # back. There is no migration to run on the live VM, which is the whole point.

    def _cache_path(self) -> Path:
        return self.dir / "cache.json"

    def _cache_read(self) -> dict:
        """The whole cache file, or {} when it is absent or unreadable. A broken cache
        must degrade to re-resolving, never to a crash mid-message."""
        p = self._cache_path()
        if not p.exists():
            return {}
        try:
            cache = json.loads(p.read_text() or "{}")
        except json.JSONDecodeError:
            return {}
        return cache if isinstance(cache, dict) else {}

    def _cache_fresh(self, row: dict, on=None) -> dict | None:
        """The row if it is still good, else None.

        Stale is a MISS, not a warning: UK retailers reformulate, so a two-year-old
        cached macro set is unreliable and silently trusting it would be the worst
        of both worlds - label-grade confidence on a figure nobody has checked
        since."""
        if not row:
            return None
        resolved = row.get("resolved_at")
        if resolved:
            ref = date.fromisoformat(_as_iso(on)) if on else date.today()
            try:
                if (ref - date.fromisoformat(resolved[:10])).days > CACHE_MAX_AGE_DAYS:
                    return None
            except ValueError:
                return None
        return row

    def cache_get(self, key: str, on=None) -> dict | None:
        """A cached resolution, or None if absent or stale.

        Follows ONE alias hop and no more. One hop is all the writer ever creates, and
        refusing to chain means a corrupted file cannot spin here inside a message."""
        cache = self._cache_read()
        hit = cache.get(key.strip().lower())
        if isinstance(hit, dict) and hit.get("alias_of"):
            hit = cache.get(str(hit["alias_of"]).strip().lower())
            if isinstance(hit, dict) and hit.get("alias_of"):
                return None          # a chain, or a loop: treat as absent
        return self._cache_fresh(hit if isinstance(hit, dict) else None, on=on)

    def cache_rows(self, on=None) -> list:
        """Every fresh PAYLOAD in the cache as (key, payload), aliases left out.

        For the caller that has to search the cache by something other than a key -
        the resolution ladder matching a product identity against what the athlete
        said this time. Alias rows are omitted so each saved resolution appears
        exactly once and a search cannot read the same product as two candidates."""
        out = []
        for key, row in self._cache_read().items():
            if not isinstance(row, dict) or row.get("alias_of"):
                continue
            fresh = self._cache_fresh(row, on=on)
            if fresh is not None:
                out.append((key, fresh))
        return out

    def cache_put(self, key: str, payload: dict, aliases=()) -> None:
        """Write one payload, plus any number of alias keys pointing at it.

        Aliases are written in the SAME locked read-modify-write as the payload: an
        alias that landed without its target, or a target without its aliases, would
        be a cache that answers differently depending on when it was interrupted."""
        p = self._cache_path()
        primary = key.strip().lower()
        with self._file_lock("cache"):
            cache = {}
            if p.exists():
                try:
                    cache = json.loads(p.read_text() or "{}")
                except json.JSONDecodeError:
                    cache = {}
            cache[primary] = payload
            for alias in aliases or ():
                alias = (alias or "").strip().lower()
                if alias and alias != primary:
                    cache[alias] = {"alias_of": primary}
            _atomic_write(p, cache)

    # --- remembered product facts -------------------------------------------
    #
    # athletes/<slug>/nutrition/product-facts.json
    #   {"sis rego": {"scoop_g": 25, "note": "...", "set_at": "2026-08-13T14:02"}}
    #
    # WHAT THIS IS FOR. "A rego scoop is half a portion" was a fact the bot could hear and
    # not keep, so every scoop of REGO was another round of him telling it the same thing.
    # The cache next to this file remembers RESOLUTIONS - what a product's macros are. This
    # remembers what the athlete has TOLD us about a product: how big its scoop is, what
    # its pack weighs, what his shorthand for it means.
    #
    # PERMANENT and per-athlete, unlike the day's exclusions, which is why the fields are a
    # closed set and the values are coerced before they land. It is consulted
    # deterministically on every resolution, so a wrong entry here is a wrong entry for
    # ever rather than for a day.

    def _facts_path(self) -> Path:
        return self.dir / "product-facts.json"

    def product_facts(self) -> dict:
        """Everything remembered about products. {} when the file is absent or broken -
        a lost fact must degrade to asking him again, never to a crash mid-message."""
        p = self._facts_path()
        if not p.exists():
            return {}
        try:
            got = json.loads(p.read_text() or "{}")
        except (json.JSONDecodeError, OSError):
            return {}
        return got if isinstance(got, dict) else {}

    def set_product_fact(self, product: str, field: str, value,
                         note: str = "", when=None) -> dict | None:
        """Remember one fact about one product. None if the field or value is unusable.

        Read-modify-write under the same flock every other writer here takes: this is a
        single shared file and the bot can be storing a fact while a cron reads it."""
        key = (product or "").strip().lower()
        if not key or field not in PRODUCT_FACT_FIELDS:
            return None
        if field == "means":
            value = str(value or "").strip()
            if not value:
                return None
        else:
            try:
                value = float(value)
            except (TypeError, ValueError):
                return None
            if value <= 0:
                return None
        with self._file_lock("product-facts"):
            facts = self.product_facts()
            rec = dict(facts.get(key) or {})
            rec[field] = value
            if note:
                rec["note"] = note
            rec["set_at"] = (when or datetime.now()).isoformat(timespec="minutes")
            facts[key] = rec
            _atomic_write(self._facts_path(), facts)
        return facts[key]

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
