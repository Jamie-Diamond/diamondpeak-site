#!/usr/bin/env python3
"""plants.py - plant species canonicalisation and the rolling diversity metric.

Spec v0.1 §6. The spec calls canonicalisation the hard part and it is right:
without it the count inflates and the headline metric is worthless. Two strings
that name the same species must collapse to one, and a refined derivative must
score zero while still being recognised as that species.

THE SCORING, AND WHAT IT IS A PROXY FOR
  whole plant (veg, fruit, legume, nut, seed, wholegrain)   1.0
  herb or spice                                             0.25
  refined derivative (white flour, seed oil, sugar)         0.0
A species eaten in several forms on one day counts ONCE, at its best score, so
brown rice and black rice are one species and rice flour alongside them does not
subtract. The 0.25 herb weighting is a ZOE scoring convention, not a study
finding, and this module labels it as such via `basis`.

WHY THE MATCHER CONSUMES WHAT IT MATCHES
Synonyms are tried longest-phrase-first and the matched span is blanked out before
the next attempt. Without that, "rice flour" would match the refined form AND then
match "rice" again as a whole grain, so a bag of flour would score 1.0. The same
mechanism is what stops "spring onion" also counting as "onion", and
"peanut butter" also counting as... nothing, but it would have counted the peanut
twice on a two-pass matcher.

Word boundaries are mandatory, not tidiness: "rice" is inside "liquorice", "corn"
is inside "cornish", "date" is inside "update", "pea" is inside "peach". Every
match is anchored on both sides.

Plurals are handled at match time (an optional trailing s or es on the final word)
rather than by storing both forms, because the table is hand-maintained and every
duplicated entry is a chance for the two to drift.

THE METRIC THAT MATTERS IS NEW SPECIES PER DAY (§6.2)
Daily totals mislead: one nutrient-dense supermarket meal can carry 14 plants, so
a single repeated shopping habit produces a big daily number and a weak weekly
unique count. `unique_7d` is the headline against the target of 30;
`new_species_today` is the actionable secondary.

PRESENT IT AS A VARIETY PROMPT, NOT A PRECISION TARGET (§6.4)
The 30 figure comes from the American Gut Project (McDonald et al., 2018), an
observational comparison of people eating 30+ plants weekly against those eating
10 or fewer. There is no evidence 30 is a threshold or that 28 versus 32 means
anything. So this module returns `target_basis` saying so, and deliberately
exposes no streak, no score out of 100 and no pass/fail: §10.4 forbids them, and
they would be unsupportable here even if it did not.

UNMAPPED STRINGS ARE LOGGED, NEVER DROPPED
An unmapped variant either inflates the count (if a later pass guesses) or is
missed entirely, and both corrupt the metric quietly. `match_text` returns what it
could not place so the caller can queue it for review; the table is read from disk
at runtime so mappings can be added without a redeploy.
"""

import json
import re
from datetime import date, timedelta
from pathlib import Path

SCORE_WHOLE = 1.0
SCORE_HERB_SPICE = 0.25
SCORE_REFINED = 0.0

HERB_SPICE_CATEGORY = "herb_spice"
DIVERSITY_TARGET_7D = 30
LOW_VARIETY_NEW_SPECIES = 5      # consecutive days below this prompts variety

TARGET_BASIS = (
    "30 plants a week comes from the American Gut Project (McDonald et al., 2018), "
    "an observational comparison of 30+ against 10 or fewer. It is not a threshold, "
    "and 28 versus 32 is not a meaningful difference. Treat it as a variety prompt."
)

_DEFAULT_TABLE = Path(__file__).resolve().parent.parent / "config" / "species.json"

# Tokens that are never a species on their own and would otherwise match a
# substring of a real one. Kept explicit rather than inferred so the reason is
# visible when one is added.
_NOISE = re.compile(r"[^a-z0-9\s]+")


class SpeciesTable:
    """The canonicalisation table plus its compiled matcher.

    Loaded from JSON at runtime, not baked into code, so mappings can be added
    without a redeploy. Compiling once and reusing matters: the matcher runs on
    every logged item and the pattern list is a few hundred entries long."""

    def __init__(self, path=None, data=None):
        if data is None:
            path = Path(path or _DEFAULT_TABLE)
            data = json.loads(path.read_text())
        self.version = data.get("version", "0")
        self.species = {}
        entries = []
        for s in data.get("species", []):
            sid = s["id"]
            base = (SCORE_HERB_SPICE if s.get("category") == HERB_SPICE_CATEGORY
                    else SCORE_WHOLE)
            self.species[sid] = {"id": sid, "canonical": s.get("canonical", sid),
                                 "latin": s.get("latin", ""),
                                 "category": s.get("category", ""),
                                 "score": base}
            for variant in s.get("synonyms", []):
                entries.append((variant.strip().lower(), sid, base))
            for variant in s.get("refined", []):
                entries.append((variant.strip().lower(), sid, SCORE_REFINED))
        # Longest phrase first: word count, then character length. This ordering is
        # load-bearing, not cosmetic - see the module docstring on span consumption.
        entries.sort(key=lambda e: (len(e[0].split()), len(e[0])), reverse=True)
        self.patterns = [(self._compile(v), v, sid, score) for v, sid, score in entries]

    @staticmethod
    def _compile(variant: str):
        """Word-boundary pattern with an optional plural on the final word.

        Three English plural forms, all of them load-bearing here. A first cut only
        allowed a trailing s or es, so "blueberries" did not match "blueberry" and
        every berry silently vanished from the count - the exact class of quiet
        failure this metric is vulnerable to, because nothing errors and the number
        just comes out lower. -y to -ies covers the berries and cherries, -f to -ves
        covers "bay leaf" to "bay leaves"."""
        words = variant.split()
        if not words:
            return re.compile(r"(?!)")
        last = words[-1]
        if len(last) > 2 and last.endswith("y") and last[-2] not in "aeiou":
            tail = re.escape(last[:-1]) + r"(?:y|ies)"
        elif len(last) > 2 and last.endswith("f"):
            tail = re.escape(last[:-1]) + r"(?:f|ves)"
        else:
            tail = re.escape(last) + r"(?:e?s)?"
        parts = [re.escape(w) for w in words[:-1]] + [tail]
        return re.compile(r"\b" + r"\s+".join(parts) + r"\b")

    def normalise(self, text: str) -> str:
        return _NOISE.sub(" ", (text or "").lower()).strip()

    def match_text(self, text: str) -> dict:
        """Find every species in a free-text ingredient string.

        Returns {'species': [{id, canonical, latin, score, matched}], 'unmatched': str}.
        `unmatched` is what remained after every match was consumed, with the words
        that matched blanked out, so the caller can queue genuinely unknown text for
        review without re-reporting the parts that resolved."""
        working = self.normalise(text)
        if not working:
            return {"species": [], "unmatched": ""}
        found = {}
        for pattern, variant, sid, score in self.patterns:
            for m in list(pattern.finditer(working)):
                # Keep the BEST score per species: a day with brown rice and rice
                # flour scores the species 1.0, not 0.0, and not 1.25.
                prev = found.get(sid)
                if prev is None or score > prev["score"]:
                    found[sid] = {**self.species[sid], "score": score, "matched": variant}
                # Consume the span so a shorter synonym cannot match inside it.
                working = working[:m.start()] + " " * (m.end() - m.start()) + working[m.end():]
        return {"species": sorted(found.values(), key=lambda s: s["id"]),
                "unmatched": " ".join(working.split())}


def species_for_entries(entries, table: SpeciesTable) -> dict:
    """Species present across a set of food entries, best score per species.

    Reads a pre-resolved `species` list on an entry if one is there (the bot stores
    what it matched at log time, so a later table change does not silently rewrite
    history), and falls back to matching the text."""
    found, unmatched = {}, []
    for e in entries or []:
        stored = e.get("species")
        if stored:
            for entry in stored:
                # Accept {"id","score"} or a bare id. Storing only the ID threw the
                # matched score away and read it back as the species' CATEGORY DEFAULT,
                # which promoted every refined derivative to a whole plant: sunflower OIL
                # came back as sunflower the seed, rapeseed oil as swede, glucose syrup as
                # maize, soy lecithin as soybean, sugar as beet and potato starch as
                # potato. Six free species per ingredient list, and it is what inflated a
                # 7-day count to 40.
                if isinstance(entry, dict):
                    sid, score = entry.get("id"), entry.get("score")
                else:
                    sid, score = entry, None
                meta = table.species.get(sid)
                if not meta:
                    continue
                rec = dict(meta)
                if score is not None:
                    rec["score"] = float(score)
                if sid not in found or rec["score"] > found[sid]["score"]:
                    found[sid] = rec
            continue
        res = table.match_text(e.get("resolved_name") or e.get("raw_text") or "")
        for s in res["species"]:
            if s["id"] not in found or s["score"] > found[s["id"]]["score"]:
                found[s["id"]] = s
        if res["unmatched"]:
            unmatched.append(res["unmatched"])
    return {"species": found, "unmatched": unmatched}


def _scoring_species(found: dict) -> dict:
    """Only species with a score above zero count. A day of nothing but white flour
    and seed oil has recognised species but no diversity, which is the point."""
    return {sid: s for sid, s in found.items() if s["score"] > 0}


def diversity(days, table: SpeciesTable, on=None, window: int = 7) -> dict:
    """The rolling diversity read.

    `days` is a list of day records (as nutrition_store.get_range returns). Returns
    the headline `unique_7d` count, the weighted total (herbs at 0.25), today's new
    species, and the species list. No streak, no score, no pass/fail: §10.4 forbids
    them and the underlying evidence would not support them anyway."""
    on = on or date.today()
    cutoff = on - timedelta(days=window - 1)

    window_species, today_species, unmatched = {}, {}, []
    per_day_new = {}
    claimed_gap = 0
    # Days carrying entries. NOT a compliance measure and NOT a caveat on the count:
    # unique_7d is a count of DISTINCT species, so 40 species from one logged day means 40
    # species really were eaten in the window. If anything a small sample makes it an
    # UNDERCOUNT of true variety, since the unlogged days also had food in them. An
    # earlier cut framed low coverage as an overstatement, which was backwards.
    days_with_entries = 0
    # Entries written before scores were stored have no way to distinguish a whole plant
    # from a refined derivative, so their contribution is an OVERSTATEMENT of unknown size:
    # sunflower oil reads as sunflower, sugar as beet. A count built on them cannot be
    # stood behind, so it is marked provisional rather than presented as a figure.
    unscored = 0
    for rec in days or []:
        d = rec.get("date")
        if not d:
            continue
        dd = date.fromisoformat(d[:10])
        if dd < cutoff or dd > on:
            continue
        for e in rec.get("entries") or []:
            claimed_gap += max(0, int(e.get("plants_claimed") or 0)
                               - len(e.get("species") or []))
            for sp in e.get("species") or []:
                if not isinstance(sp, dict) or sp.get("score") is None:
                    unscored += 1
        if rec.get("entries"):
            days_with_entries += 1
        res = species_for_entries(rec.get("entries"), table)
        scoring = _scoring_species(res["species"])
        unmatched.extend(res["unmatched"])
        for sid, s in scoring.items():
            if sid not in window_species or s["score"] > window_species[sid]["score"]:
                window_species[sid] = s
        if dd == on:
            today_species = scoring
        per_day_new[dd] = scoring

    # New today = today's species minus everything in the PREVIOUS days of the
    # window. Computed against the prior days rather than the whole window, or
    # today's own species would cancel themselves out.
    prior = set()
    for dd, spec in per_day_new.items():
        if dd < on:
            prior |= set(spec)
    new_ids = sorted(set(today_species) - prior)

    weighted = round(sum(s["score"] for s in window_species.values()), 2)
    # A count built on species with no stored score cannot distinguish a whole plant from
    # a refined derivative, so it is not reportable. Return None rather than a number with
    # a caveat: a caveated wrong number is still a wrong number, and it gets read as the
    # number. Anything logged since scores were stored counts correctly, so this clears
    # itself as real days accumulate.
    reportable = unscored == 0
    return {
        "unique_7d": len(window_species) if reportable else None,
        "unique_7d_upper_bound": len(window_species),
        "weighted_7d": weighted if reportable else None,
        "target": DIVERSITY_TARGET_7D,
        "target_basis": TARGET_BASIS,
        "new_species_today": len(new_ids),
        "new_species_today_names": [window_species[i]["canonical"] for i in new_ids
                                    if i in window_species],
        "species": sorted((s["canonical"] for s in window_species.values())),
        "herb_spice_count": sum(1 for s in window_species.values()
                                if s["category"] == HERB_SPICE_CATEGORY),
        "unmatched_strings": unmatched,
        # Plants a pack claimed that we could not NAME. Reported so the headline count
        # is honestly incomplete rather than quietly low: a nutrient-dense ready meal
        # saying "14 plants" whose ingredient list was never retrieved contributes only
        # what could be resolved from its title.
        "claimed_unresolved": claimed_gap,
        # True when any species in the window lacks a stored score, which makes the count
        # an upper bound rather than a figure.
        "days_logged": days_with_entries,
        # The count covers only the days that were logged. Stated so the figure is read
        # as "across N days sampled" rather than as a week.
        "coverage_days": days_with_entries,
        "provisional": unscored > 0,
        "unscored_species": unscored,
        "window_days": window,
    }


def low_variety_flag(days, table: SpeciesTable, on=None,
                     consecutive: int = 2) -> dict | None:
    """Prompt variety when new species per day stays low for consecutive days (§6.2).

    A prompt, never a failure. Returns None when there is nothing to say, so the
    caller has no way to render an absence as a red state."""
    on = on or date.today()
    lows = []
    for i in range(consecutive):
        d = on - timedelta(days=i)
        div = diversity(days, table, on=d)
        if div["new_species_today"] < LOW_VARIETY_NEW_SPECIES:
            lows.append((d.isoformat(), div["new_species_today"]))
    if len(lows) < consecutive:
        return None
    return {"type": "low_variety", "severity": "info", "days": lows,
            "message": "Same plants for a couple of days. Worth adding something new."}
