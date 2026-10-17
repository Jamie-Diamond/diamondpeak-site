"""Strength sessions built from the exercise library, so a planned strength event carries
its exercises, sets, reps and rest (Jamie, 17 Oct 2026: the planner was putting empty
"Strength A (Tier C)" slots in Intervals.icu).

Pure and deterministic: no network, no clock (write_plan's 14-day prune aside).

  build(profile, library, date=, phase=, slot=, tier=, sports=, level=)  -> session | None
  describe(session, library)              -> plain-text list for the Intervals.icu event
  write_plan(slug, event_id, session)     -> athletes/<slug>/strength-plan.json, which
                                             Peak's player reads (strength_log.planned)

A session is the hand-over shape strength_log.planned() expects:
  {"date", "title", "minutes", "parts": [{"title", "order": "suggested"|"any",
     "items": [{"ex", "sets", "reps", "rest_s", "alt", "rpe"?, "pair"?, "note"?}]}]}
plus a few fields for the planner and coach: kind ("library" | "top_up"), slot, tier,
phase, module, gaps (coverage this session could not give).

Rules: config/exercise-library.json (template, modules, dose_by_phase, issues) and
docs/strength-methodology.md. Pick order is the library's: issue protocols first (they
count toward the cap and the week's coverage), then sport module -> equipment tier ->
athlete level -> issue cautions. Where the JSON and the doc disagree the doc wins (§1).

Profile fields read (all optional):
  injuries[]            the athlete's issues (library `issues` block; strength_log.own_sessions
                        reads the same protocol sessions)
  race_date             no strength in race week (blueprint)
  strength_programme    the existing opt-in flag. When it is a dict it may also carry
                        {"kit": [...once-asked extras...], "started": "YYYY-MM-DD",
                         "intro": bool, "impact_sport": bool}  (PROPOSED, not stored yet)
"""
from __future__ import annotations

import json
import os
import re
from datetime import date as _date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent            # ClaudeCoach/
LIBRARY = BASE / "config" / "exercise-library.json"
PLAN_FILE = "strength-plan.json"
KEEP_DAYS = 14

TIER_LABEL = {"A": "Gym", "B": "Dumbbells", "C": "Bodyweight + band"}
LOWER = frozenset({"squat", "hinge", "single_leg", "bridge", "hip_stability", "calf_ankle",
                   "balance", "plyometric", "knee_flexion", "knee_extension"})
UPPER = frozenset({"push_horizontal", "push_vertical", "pull_horizontal", "pull_vertical",
                   "scapular_cuff"})
# Carries load the legs by walking (library knee example: "No carries ... walking load").
LOWER_LOAD = LOWER | {"carry"}
NO_ALT = frozenset({"mobility", "balance"})              # nothing shares their job
FREE_KIT = frozenset({"mat", "wall"})                    # never "taken" in a busy gym
# The lift "the plan says" per pattern; swaps[tier] then resolves it (methodology §7 table).
CANON = {"squat": "back_squat", "hinge": "romanian_deadlift", "single_leg": "bulgarian_split_squat",
         "bridge": "hip_thrust", "push_horizontal": "push_up", "push_vertical": "overhead_press",
         "pull_horizontal": "bent_over_row", "pull_vertical": "lat_pulldown"}
# template.blocks main: "pull (vertical first, else horizontal)", "bridge (or single_leg_rdl)".
FALLBACK = {"squat": ("squat", "single_leg"), "hinge": ("hinge", "bridge"),
            "single_leg": ("single_leg",), "bridge": ("bridge", "hinge"),
            "push_vertical": ("push_vertical", "push_horizontal"),
            "push_horizontal": ("push_horizontal", "push_vertical"),
            "pull_vertical": ("pull_vertical", "pull_horizontal"),
            "pull_horizontal": ("pull_horizontal", "pull_vertical")}
# location text -> library issue area. Achilles -> ankle is the methodology's (§9e); calf,
# shin, foot and heel -> ankle is this file's choice, so a lower-leg issue blocks plyos.
AREA_WORDS = (("knee", ("knee", "acl", "mcl", "pcl", "menisc", "patell", "it band", "itb")),
              ("ankle", ("ankle", "achilles", "calf", "shin", "foot", "heel", "plantar")),
              ("hip", ("hip", "groin", "adductor", "glute")),
              ("low_back", ("back", "lumbar", "spine", "sciatic", "disc")),
              ("shoulder", ("shoulder", "rotator", "cuff")))
AREA_LABEL = {"knee": "Knee", "ankle": "Ankle", "hip": "Hip", "low_back": "Back",
              "shoulder": "Shoulder"}
# "Follow the caution text, or swap": a caution that itself says swap / skip (unconditionally)
# is a swap while the issue is active or in rehab; any other caution is shown as a note.
SWAP_CAUTION = re.compile(r"^\s*(swap\b|skip during\b)", re.I)
SPORTS = {"swim": "swim", "swimming": "swim", "bike": "bike", "ride": "bike", "cycling": "bike",
          "cycle": "bike", "run": "run", "running": "run", "triathlon": "triathlon", "tri": "triathlon",
          "health": "health"}
MODULE_SPORTS = {"run": ("run",), "bike": ("bike",), "swim": ("swim",),
                 "triathlon": ("bike", "run", "swim"), "health": ("health",)}
TIER_C_SHIFT_PHASES = {"base", "build", "specific", "offseason"}   # "8-12 where load can't reach RPE 8"
LOW_END_PHASES = {"peak", "taper"}                                  # ranges read at the low end
RAMP = "Then 1-2 light ramp-up sets of"


def load_library(path: Path | str | None = None) -> dict:
    return json.loads(Path(path or LIBRARY).read_text())


# ── small helpers ──

def _phase(phase) -> tuple[str, bool]:
    """(dose_by_phase key or "race", no_event). A goal block or no phase at all is a
    no-event athlete: base doses, power once any lead-in is done (template.plyo_start)."""
    p = str(phase or "").strip().lower().replace("-", "_")
    if not p or p in ("goal", "none", "general", "health"):
        return "base", True
    if any(w in p for w in ("post", "recovery", "transition", "deload")):
        return "taper", False                     # a light touch, never a full session
    if "race" in p:
        return "race", False
    if p in ("off_season", "offseason", "off"):
        return "offseason", False
    for k in ("offseason", "base", "specific", "build", "peak", "taper"):
        if k in p:
            return k, False
    return "base", False


def _module(sports) -> str:
    s = {SPORTS.get(str(x).strip().lower()) for x in sports or []} - {None}
    if "triathlon" in s or len(s & {"swim", "bike", "run"}) >= 2:
        return "triathlon"
    for m in ("run", "bike", "swim"):
        if m in s:
            return m
    return "health"


def _range(spec) -> tuple[int, int] | None:
    nums = [int(n) for n in re.findall(r"\d+", str(spec or ""))]
    return (nums[0], nums[-1]) if nums else None


def _sets(spec, low: bool) -> int:
    r = _range(spec)
    if not r:
        return 0
    return max(1, r[0] if low else r[1]) if r[1] > 0 else 0


def _side(reps: str, ex: dict) -> str:
    reps = str(reps or "").strip()
    if not ex.get("per_side") or "side" in reps:
        return reps
    return reps + ("/side" if re.search(r"\d(\s?s)?$", reps) else " (each side)")


def _top(reps) -> int:
    r = _range(reps)
    return r[1] if r else 0


def _ids(rows) -> list[str]:
    """Exercise ids from anything week-shaped: hand-over sessions (parts), strength-log
    records (entries), protocol sessions (items), items, or bare ids."""
    out: list[str] = []
    for r in rows or []:
        if isinstance(r, str):
            out.append(r)
            continue
        if not isinstance(r, dict):
            continue
        if r.get("ex"):
            out.append(str(r["ex"]))
        for k in ("entries", "items"):
            out += [str(x["ex"]) for x in r.get(k) or [] if isinstance(x, dict) and x.get("ex")]
        for p in r.get("parts") or []:
            out += [str(x["ex"]) for x in (p or {}).get("items") or [] if isinstance(x, dict) and x.get("ex")]
    return out


def coverage(ids, library: dict, patterns=()) -> set[str]:
    """template.weekly_coverage keys met by these exercises (plus free-text patterns)."""
    exs = library.get("exercises") or {}
    pats = {exs[i].get("pattern") for i in ids if i in exs} | set(patterns)
    idset = set(ids)
    return {key for key, rule in ((library.get("template") or {}).get("weekly_coverage") or {}).items()
            if set(rule.get("patterns") or []) & pats or set(rule.get("also") or []) & idset}


def _area(inj: dict) -> str:
    if inj.get("area"):
        return str(inj["area"])
    text = f"{inj.get('location') or ''} {inj.get('description') or ''}".lower()
    text = text.replace("lower back", "back").replace("low back", "back")
    for area, words in AREA_WORDS:
        if any(w in text for w in words):
            return area
    return "other"


def _counts(s: dict) -> bool:
    return s.get("counts_as_strength", True) is not False


def _per_week(s: dict) -> int:
    try:
        return max(0, int(s.get("per_week", 1)))
    except (TypeError, ValueError):
        return 1


def _issues(profile: dict, lib: dict) -> list[dict]:
    """Current issues (not resolved / cleared), normalised. Missing status reads as active,
    as strength_log._active does."""
    areas = (lib.get("issues") or {}).get("areas") or {}
    out = []
    for inj in profile.get("injuries") or []:
        if not isinstance(inj, dict):
            continue
        status = str(inj.get("status") or "active").lower()
        if status in ("resolved", "cleared"):
            continue
        area = _area(inj)
        acfg = areas.get(area) or areas.get("other") or {}
        prot = inj.get("protocol")
        sess = inj.get("sessions") or (prot.get("sessions") if isinstance(prot, dict) else None) or []
        out.append({"area": area, "lower_limb": bool(acfg.get("lower_limb")),
                    "caution_key": acfg.get("caution_key"), "status": status,
                    "acute": status in ("active", "rehab"),
                    "avoid": [str(a) for a in inj.get("avoid") or []],
                    "plyo_cleared": inj.get("plyo_cleared") is True,
                    "sessions": [s for s in sess if isinstance(s, dict)]})
    return out


def _race_week(profile: dict, day: _date) -> bool:
    try:
        r = _date.fromisoformat(str(profile.get("race_date") or "")[:10])
    except ValueError:
        return False
    return r - timedelta(days=r.weekday()) == day - timedelta(days=day.weekday())


# ── the builder ──

class _Ctx:
    def __init__(self, profile, lib, *, day, phase, tier, sports, level, week_logged, cap):
        self.lib = lib
        self.exs = lib.get("exercises") or {}
        self.tier = tier if tier in ("A", "B", "C") else "C"
        self.level = max(1, min(3, int(level or 1)))
        self.phase, self.no_event = _phase(phase)
        self.module = _module(sports)
        self.mod = (lib.get("modules") or {}).get(self.module) or {}
        self.day = day
        sp = profile.get("strength_programme")
        self.sp = sp if isinstance(sp, dict) else {}
        self.kit = self._kit()
        self.issues = _issues(profile, lib)
        self.avoid = {a for i in self.issues for a in i["avoid"]}
        self.acute_lower = any(i["lower_limb"] and i["acute"] for i in self.issues)
        self.plyo_blocked = any(i["lower_limb"] and i["acute"] and not i["plyo_cleared"]
                                for i in self.issues)
        protocol = [s for i in self.issues for s in i["sessions"]]
        p_items = [it for s in protocol for it in s.get("items") or [] if isinstance(it, dict)]
        self.protocol_ids = [str(it["ex"]) for it in p_items if it.get("ex")]
        self.cap = cap
        self.cap_used = sum(_per_week(s) for s in protocol if _counts(s))
        # A source-led strength programme for a lower-limb issue: never a competing
        # lower-body session (issues.rules.top_up_only).
        self.lower_protocol = any(i["lower_limb"] and i["acute"] and any(_counts(s) for s in i["sessions"])
                                  for i in self.issues)
        self.week_known = week_logged is not None
        self.week_ids = _ids(week_logged)
        self.covered = coverage(self.protocol_ids + _ids(week_logged), lib,
                                [str(it["pattern"]) for it in p_items if it.get("pattern")])
        weeks = self._weeks_in()
        impact = self.module in ("run", "triathlon") or self.sp.get("impact_sport") is True
        self.intro = self.sp.get("intro") is True or (self.level == 1 and (weeks is None or weeks < 3))
        needs_lead_in = (self.level == 1 and not impact) or self.acute_lower
        self.lead_in = needs_lead_in and (weeks is None or weeks < 4)
        dbp = lib.get("dose_by_phase") or {}
        self.dose = dbp.get(self.phase) or dbp.get("base") or {}
        self.low = self.phase in LOW_END_PHASES or self.intro
        pdose = ((dbp.get("build") or {}) if self.no_event else self.dose).get("power")
        self.power_dose = pdose if pdose and _sets(pdose.get("sets"), self.low) > 0 else None
        self.power_on = bool(self.power_dose) and not (self.plyo_blocked or self.intro or self.lead_in)
        # Balance in every session, except a lower-limb issue in rehab (ask the source).
        self.balance_on = not self.acute_lower
        self.lower_blocked = False
        self.sport_scores = MODULE_SPORTS.get(self.module, ("health",))

    def _kit(self) -> set:
        tiers = self.lib.get("equipment_tiers") or {}
        kit = set((tiers.get(self.tier) or {}).get("kit") or [])
        stored = self.sp.get("kit")
        if self.tier != "A" and isinstance(stored, list):     # a full gym has it all anyway
            asked = set((self.lib.get("equipment_ask") or {}).get("once_and_stored") or [])
            kit = (kit - asked) | (set(stored) & asked)
        return kit

    def _weeks_in(self) -> int | None:
        try:
            return (self.day - _date.fromisoformat(str(self.sp.get("started") or "")[:10])).days // 7
        except ValueError:
            return None

    # filters
    def ok(self, ex_id, used=(), exclude=()) -> bool:
        e = self.exs.get(ex_id)
        if not e or ex_id in used or ex_id in exclude or ex_id in self.avoid:
            return False
        if not set(e.get("equipment") or []) <= self.kit:
            return False
        pat = e.get("pattern")
        if pat == "plyometric":
            # template.plyo_start gates plyos, not the exercise level (the health beginner
            # example adds pogo_hops for a level-1 athlete from week 5).
            if not self.power_on:
                return False
        elif int(e.get("level") or 1) > self.level:
            return False
        if self.lower_blocked and pat in LOWER_LOAD:
            return False
        if pat == "balance" and not self.balance_on:
            return False
        if pat == "carry" and self.acute_lower:      # walking load (library knee example)
            return False
        return True

    def must_swap(self, ex_id) -> bool:
        cau = self.exs[ex_id].get("cautions") or {}
        return any(i["acute"] and i["caution_key"] and SWAP_CAUTION.match(cau.get(i["caution_key"]) or "")
                   for i in self.issues)

    def first(self, cands, used=(), exclude=()):
        seen, passers = set(), []
        for c in cands:
            if c not in seen:
                seen.add(c)
                if self.ok(c, used, exclude):
                    passers.append(c)
        return next((c for c in passers if not self.must_swap(c)), passers[0] if passers else None)

    # candidates
    def ladder(self, start) -> list:
        out, cur = [], start
        while cur and cur in self.exs and cur not in out:
            out.append(cur)
            cur = self.exs[cur].get("easier")
        return out

    def via(self, ex_id) -> list:
        sw = ((self.exs.get(ex_id) or {}).get("swaps") or {}).get(self.tier)
        return (self.ladder(sw) if sw else []) + self.ladder(ex_id)

    def score(self, ex_id) -> tuple:
        sp = self.exs[ex_id].get("sports") or {}
        return (sum(int(sp.get(s) or 0) for s in self.sport_scores), int(sp.get("health") or 0))

    def same_pattern(self, pat) -> list:
        ids = [k for k, e in self.exs.items() if e.get("pattern") == pat]
        return sorted(ids, key=lambda k: tuple(-x for x in self.score(k)) + (-int(self.exs[k].get("level") or 1),))

    def pick(self, pattern, used=(), exclude=()):
        cands = ["pull_up"] if pattern == "pull_vertical" and self.level >= 3 else []
        for pat in FALLBACK.get(pattern, (pattern,)):
            if pat in CANON:
                cands += self.via(CANON[pat])
            cands += self.same_pattern(pat)
        return self.first(cands, used, exclude)

    def pick_exact(self, ex_id, used=(), exclude=()):
        if ex_id not in self.exs:
            return None
        return self.first(self.via(ex_id) + self.same_pattern(self.exs[ex_id].get("pattern")), used, exclude)

    def alt(self, ex_id, used=()):
        """A same-job swap for when this one's kit is taken: same pattern first, then the
        same coverage group; never needing the kit it replaces. None for kit-free work."""
        e = self.exs[ex_id]
        own = set(e.get("equipment") or [])
        if e.get("pattern") in NO_ALT or own <= FREE_KIT:
            return None
        groups = coverage([ex_id], self.lib) - {"grip", "bone_heavy"}
        wc = (self.lib.get("template") or {}).get("weekly_coverage") or {}
        group_pats = [p for g in sorted(groups) for p in (wc.get(g) or {}).get("patterns") or []]
        group_also = [a for g in sorted(groups) for a in (wc.get(g) or {}).get("also") or []]
        declared = [e.get("easier")] + list((e.get("swaps") or {}).values())
        cands = (declared + self.same_pattern(e.get("pattern"))
                 + [k for p in group_pats for k in self.same_pattern(p)] + group_also)
        target = set((e.get("muscles") or {}).get("target") or [])

        def same_job(c):     # the library's own easier/swaps, else a shared target muscle
            return c in declared or bool(target & set((self.exs[c].get("muscles") or {}).get("target") or []))
        fits = [c for c in cands
                if c and c != ex_id and c in self.exs and c not in used and self.ok(c)
                and not self.must_swap(c) and same_job(c)
                and not ((set(self.exs[c].get("equipment") or []) & own) - FREE_KIT)]
        same = [c for c in fits if self.exs[c].get("pattern") == e.get("pattern")]
        job = [c for c in fits if self.exs[c].get("pattern") in group_pats or c in group_also]
        return (same or job or [None])[0]

    def note(self, ex_id, extra: str = "") -> str:
        cau = self.exs[ex_id].get("cautions") or {}
        bits, seen = [], set()
        for i in self.issues:
            k = i["caution_key"]
            if k and cau.get(k) and k not in seen:
                seen.add(k)
                bits.append(f"{AREA_LABEL.get(k, k)}: {cau[k]}")
        if extra:
            bits.append(extra)
        return " ".join(bits)

    # items
    def _item(self, ex_id, sets, reps, rest_s, used, rpe=None, pair=None, note="") -> dict:
        e = self.exs[ex_id]
        it = {"ex": ex_id, "sets": int(sets), "reps": _side(reps, e), "rest_s": int(rest_s or 0)}
        if rpe and e.get("pattern") not in NO_ALT:
            it["rpe"] = str(rpe)
        if pair:
            it["pair"] = pair
        it["alt"] = self.alt(ex_id, used)
        n = self.note(ex_id, note)
        if n:
            it["note"] = n
        return it

    def main_item(self, ex_id, used, pair=None) -> dict:
        e = self.exs[ex_id]
        pat = e.get("pattern")
        upper = pat not in LOWER
        cls = "upper" if upper else ("single_leg" if e.get("per_side") else "main")
        intro = ((self.lib.get("dose_by_phase") or {}).get("base") or {}).get("intro") or {}
        d = (intro.get("main") if self.intro else None) or self.dose.get(cls) or self.dose.get("main") or {}
        loadable = float(e.get("load_step_kg") or 0) > 0 and self.tier != "C"
        reps = str(d.get("reps") or (e.get("dose") or {}).get("reps") or "8-10")
        own = str((e.get("dose") or {}).get("reps") or reps)
        building = self.phase in TIER_C_SHIFT_PHASES and not self.intro
        if building and not upper and (pat == "bridge" or self.tier == "B"):
            # Hip thrust stays the blueprint's 3 x 8, not the 4-6 heavy range; at tier B
            # the heaviest dumbbell caps the load, so the exercise's own range (the tier B
            # example sessions: goblet squat 8, RDL 8).
            reps = own
        if not loadable:
            if upper:
                reps = own
            elif self.phase in TIER_C_SHIFT_PHASES or self.intro:
                reps = own if _top(own) >= 12 else "8-12"      # tier C rep shift
        pair_rest = (self.dose.get("upper") or {}).get("rest_s") or 60
        rest = pair_rest if pair else (d.get("rest_s") or (e.get("dose") or {}).get("rest_s") or 60)
        return self._item(ex_id, _sets(d.get("sets") or "3", self.low), reps, rest, used,
                          rpe=d.get("rpe"), pair=pair)

    def light_item(self, ex_id, used, sets, rest_cap, note="") -> dict:
        dose = self.exs[ex_id].get("dose") or {}
        rest = min(int(dose.get("rest_s") or 0), rest_cap)
        return self._item(ex_id, sets, dose.get("reps") or "10", rest, used, rpe=dose.get("rpe"), note=note)

    def power_item(self, ex_id, used) -> dict:
        d = self.power_dose or {}
        name = "pogo" if ex_id == "pogo_hops" else "squat jump" if ex_id == "squat_jump" else None
        reps = None
        if name:
            for part in str(d.get("reps") or "").split(" or "):
                if part.strip().lower().startswith(name):
                    reps = part.strip()[len(name):].strip()
            if reps and ex_id == "pogo_hops" and "contact" not in reps:
                reps += " contacts"
        reps = reps or (self.exs[ex_id].get("dose") or {}).get("reps") or "5"
        sets = _sets(d.get("sets"), self.low)
        cap = d.get("max_contacts")
        if ex_id == "pogo_hops" and cap:
            while sets > 1 and sets * _top(reps) > int(cap):
                sets -= 1
        return self._item(ex_id, sets, reps, d.get("rest_s") or 60, used, rpe=d.get("rpe"))


def _session(ctx: _Ctx, slot: str) -> dict:
    lib, used = ctx.lib, []
    tmpl = lib.get("template") or {}
    slots = (tmpl.get("session_slots") or {}).get(slot) or {}

    # Main: two pairs, lower + upper, the upper lift filling the lower lift's rest.
    a_squat = ctx.pick(((tmpl.get("session_slots") or {}).get("A") or {}).get("pair_1", ["squat"])[0])
    main = []
    for pn, key in enumerate(("pair_1", "pair_2"), 1):
        lower_pat, upper_pat = (slots.get(key) or [None, None])[:2]
        exclude = [a_squat] if slot == "B" and a_squat else []     # tier C: not BSS twice a week
        lo = ctx.pick(lower_pat, used, exclude) if lower_pat else None
        if lo:
            used.append(lo)
        up = ctx.pick(upper_pat, used) if upper_pat else None
        if up:
            used.append(up)
        pair = str(pn) if lo and up else None
        main += [ctx.main_item(x, used, pair) for x in (lo, up) if x]

    # Power: fresh, before the main lifts, from build (template.plyo_start).
    power = []
    if ctx.power_on:
        pw = (ctx.mod.get("power") or {}).get(slot)
        other = (ctx.mod.get("power") or {}).get("B" if slot == "A" else "A")
        p = ctx.first([pw, "pogo_hops", other] + ctx.same_pattern("plyometric"), used)
        if p:
            used.append(p)
            power.append(ctx.power_item(p, used))

    # Sport block: module picks, minus anything an issue protocol already gives this week.
    sport_sets = 1 if ctx.phase in LOW_END_PHASES else 2
    sport_ids = []
    for ex_id in (ctx.mod.get("sport_block") or {}).get(slot) or []:
        if ex_id in ctx.protocol_ids:
            continue
        got = ctx.pick_exact(ex_id, used + sport_ids)
        if got:
            sport_ids.append(got)
    if ctx.lead_in:                         # 4-week calf lead-in before any plyos
        for ex_id in (tmpl.get("plyo_start") or {}).get("lead_in", {}).get("exercises") or []:
            if ex_id not in used + sport_ids and ex_id not in ctx.protocol_ids and ctx.ok(ex_id):
                sport_ids.append(ex_id)
    if slot == "B" and ctx.week_known:      # module must_include, across the week
        have = set(ctx.protocol_ids) | set(used) | set(sport_ids) | set(ctx.week_ids)
        for ex_id in ctx.mod.get("must_include") or []:
            if ex_id in ctx.exs and ex_id not in have and ctx.ok(ex_id, used + sport_ids):
                sport_ids.append(ex_id)
    used += sport_ids
    sport = [ctx.light_item(x, used, sport_sets, 30) for x in sport_ids]

    # Core: anti-extension + anti-rotation + lateral; B may swap one for a carry.
    rounds = _sets((ctx.dose.get("core") or {}).get("rounds") or "2", ctx.low)
    has_lateral = "core_lateral" in coverage(used, lib)
    core_ids = []
    for want in ("front_plank" if slot == "A" else "dead_bug", "pallof_press",
                 None if has_lateral else "side_plank"):
        if not want:
            continue
        if (want == "pallof_press" and slot == "B" and ctx.ok("farmers_carry", used + core_ids)
                and (not ctx.week_known or "core_anti_rotation" in ctx.covered)):
            want = "farmers_carry"
        got = ctx.pick_exact(want, used + core_ids)
        if got:
            core_ids.append(got)
    if (ctx.week_known and "grip" not in ctx.covered | coverage(used + core_ids, lib)
            and ctx.ok("farmers_carry", used + core_ids)):
        core_ids.append("farmers_carry")
    used += core_ids
    core = [ctx.light_item(x, used, rounds, 45 if ctx.exs[x].get("pattern") == "carry" else 20)
            for x in core_ids]

    # Warm-up: leg swings + a lower mobility drill + module adds + balance + ramp-up note.
    adds = list(ctx.mod.get("warm_up_add") or [])
    wu = ["leg_swings"]
    if ctx.balance_on and "single_leg_balance" not in used:
        wu.append("single_leg_balance")
    if not {"knee_to_wall", "hip_flexor_stretch"} & set(adds):
        wu.append("knee_to_wall" if slot == "A" else "hip_flexor_stretch")
    wu += adds
    wu_ids = []
    for ex_id in wu:
        if ex_id not in wu_ids and ex_id not in used and ctx.ok(ex_id, wu_ids):
            wu_ids.append(ex_id)
    first_lift = next((it["ex"] for it in main), None)
    warm = []
    for n, ex_id in enumerate(wu_ids):
        e = ctx.exs[ex_id]
        reps = "30 s" if ex_id == "single_leg_balance" else (e.get("dose") or {}).get("reps") or "10"
        note = ("Comfortable, no strain." if ex_id == "single_leg_balance" else "")
        if n == len(wu_ids) - 1 and first_lift:
            note = (note + f" {RAMP} {ctx.exs[first_lift].get('name')}.").strip()
        it = ctx._item(ex_id, 1, reps, 0, used + wu_ids, note=note)
        it["alt"] = None                    # warm-up: skip it if the kit is taken
        warm.append(it)

    parts = [{"title": "Warm-up", "order": "any", "items": warm}]
    if power:
        parts.append({"title": "Power", "order": "suggested", "items": power})
    parts.append({"title": "Main", "order": "suggested", "items": main})
    if sport:
        parts.append({"title": "Balance & grip" if ctx.module == "health" else "Sport",
                      "order": "any", "items": sport})
    if core:
        parts.append({"title": "Core", "order": "any", "items": core})
    have = coverage([it["ex"] for p in parts for it in p["items"]], lib)
    gaps = [k for k in ("push", "pull") if k not in have]
    if ctx.balance_on and "balance" not in have:
        gaps.append("balance")
    return {"kind": "library", "title": f"Strength {slot} · {TIER_LABEL[ctx.tier]}",
            "parts": parts, "gaps": gaps}


# Top-up picks per missing coverage key (issues.rules.top_up_only).
TOP_UP = (("push", "push_horizontal", True), ("pull", "pull_vertical", True),
          ("core_anti_extension", "dead_bug", False), ("core_anti_rotation", "pallof_press", False),
          ("core_lateral", "side_plank", False), ("squat", "squat", True), ("hinge", "hinge", True),
          ("single_leg", "single_leg", True), ("calf_ankle", "calf_raise", False),
          ("balance", "single_leg_balance", False))
TOP_UP_MAX = 6


def _top_up(ctx: _Ctx) -> dict | None:
    """What the protocol (and the week so far) leaves missing, usually upper body and
    core, 10-20 min appended to a protocol session. Never lower body for a lower-limb
    issue; never plyos, carries or balance there either (ask the source)."""
    ctx.lower_blocked = ctx.acute_lower
    used, upper, core = [], [], []
    for key, what, is_pattern in TOP_UP:
        if key in ctx.covered or len(used) >= TOP_UP_MAX:
            continue
        got = ctx.pick(what, used) if is_pattern else ctx.pick_exact(what, used)
        if not got:
            continue
        used.append(got)
        (upper if ctx.exs[got].get("pattern") in UPPER | LOWER else core).append(got)
    if not used:
        return None
    pair = "1" if sum(1 for x in upper if ctx.exs[x].get("pattern") in UPPER) == 2 else None
    rounds = _sets((ctx.dose.get("core") or {}).get("rounds") or "2", ctx.low)
    parts = []
    if upper:
        parts.append({"title": "Upper body" if all(ctx.exs[x].get("pattern") in UPPER for x in upper)
                      else "Strength", "order": "suggested",
                      "items": [ctx.main_item(x, used, pair if ctx.exs[x].get("pattern") in UPPER else None)
                                for x in upper]})
    if core:
        parts.append({"title": "Core", "order": "any",
                      "items": [ctx.light_item(x, used, rounds, 20) for x in core]})
    have = ctx.covered | coverage(used, ctx.lib)
    gaps = [k for k in ("push", "pull", "bone_impact", "grip") if k not in have]
    title = "Upper body + core top-up" if not any(ctx.exs[x].get("pattern") in LOWER for x in used) else "Strength top-up"
    return {"kind": "top_up", "title": title, "parts": parts, "gaps": gaps}


# ── time ──

def _item_seconds(it: dict, lib: dict) -> float:
    e = (lib.get("exercises") or {}).get(it.get("ex")) or {}
    sides = 2 if e.get("per_side") else 1
    work = 20 if e.get("pattern") == "plyometric" else 30
    m = re.search(r"(\d+)(?:-\d+)?\s*s\b", str(it.get("reps") or ""))
    if e.get("log") == "seconds" and m:
        work = min(int(m.group(1)), 60)
    return int(it.get("sets") or 1) * (sides * work + int(it.get("rest_s") or 0))


def estimate_minutes(session: dict, library: dict) -> int:
    secs = sum(_item_seconds(it, library) for p in session.get("parts") or [] for it in p.get("items") or [])
    return max(10, int(round(secs / 60 / 5)) * 5)


def _trim(session: dict, minutes: int, library: dict) -> None:
    """Shorten to fit `minutes`, least important first; balance always stays."""
    def part(title_words):
        return next((p for p in session["parts"] if any(w in p["title"].lower() for w in title_words)), None)

    def keep(p, n):
        if not p:
            return
        bal = [it for it in p["items"] if it["ex"] == "single_leg_balance"]
        rest = [it for it in p["items"] if it["ex"] != "single_leg_balance"]
        p["items"] = (bal + rest)[:max(n, len(bal))] if bal else rest[:n]

    sport, core, warm = part(("sport", "balance &")), part(("core",)), part(("warm",))
    ramp = next((it["note"][it["note"].index(RAMP):] for it in (warm or {"items": []})["items"]
                 if RAMP in (it.get("note") or "")), None)
    steps = [lambda: keep(sport, 2), lambda: keep(core, 2), lambda: keep(warm, 3),
             lambda: keep(sport, 1), lambda: [it.update(sets=min(it["sets"], 2))
                                              for it in (part(("main",)) or {"items": []})["items"]],
             lambda: keep(core, 1)]
    for step in steps:
        if estimate_minutes(session, library) <= minutes:
            break
        step()
    session["parts"] = [p for p in session["parts"] if p["items"]]
    if ramp and warm and warm["items"]:     # the ramp-up note stays on the last warm-up item
        for it in warm["items"]:
            if ramp in (it.get("note") or ""):
                it["note"] = it["note"].replace(ramp, "").strip()
                if not it["note"]:
                    it.pop("note")
        last = warm["items"][-1]
        last["note"] = f"{last.get('note') or ''} {ramp}".strip()


# ── public ──

def build(profile: dict, library: dict, *, date: str, phase: str, slot: str, tier: str,
          sports: list, level: int, minutes: int | None = None, week_logged: list | None = None,
          cap: int = 2) -> dict | None:
    """One planned strength session in the hand-over shape, or None (race week, or an issue
    protocol already gives everything the week needs).

    slot A|B (alternate across the week), tier A|B|C (this week's equipment), sports = the
    athlete's event sports (2+ of swim/bike/run = triathlon module, none = health), level
    1-3. minutes caps the length. week_logged: the week's other strength sessions so far
    (hand-over sessions, strength-log records or ids), NOT the issue protocol; it drives
    weekly coverage and makes a repeat top-up call return None. cap = day_rules.strength_max.
    """
    day = _date.fromisoformat(str(date)[:10])
    slot = "B" if str(slot).upper() == "B" else "A"
    tier = str(tier).upper()
    ctx = _Ctx(profile or {}, library, day=day, phase=phase, tier=tier, sports=sports,
               level=level, week_logged=week_logged, cap=cap)
    if ctx.phase == "race" or _race_week(profile or {}, day):
        return None
    top = ctx.lower_protocol or ctx.cap_used >= cap
    sess = _top_up(ctx) if top else _session(ctx, slot)
    if sess is None:
        return None
    if minutes and not top:
        _trim(sess, int(minutes), library)
    out = {"date": day.isoformat(), "title": sess["title"],
           "minutes": estimate_minutes(sess, library), "kind": sess["kind"],
           "slot": slot, "tier": ctx.tier, "phase": ctx.phase, "module": ctx.module,
           "parts": sess["parts"], "gaps": sess["gaps"]}
    return out


def _rest(s) -> str:
    s = int(s or 0)
    if s <= 0:
        return ""
    if s < 60 or (s < 120 and s % 60):
        return f"{s} s"
    return f"{s / 60:g} min"


def describe(session: dict | None, library: dict) -> str:
    """The plain-text list for the Intervals.icu event description: one line per exercise
    ("Back squat 3 × 5 · rest 2 min"), part headings between. No RPE (athletes tap
    easy / right / hard instead) and no line starts with "-", so nothing is read as a
    structured workout step."""
    if not session:
        return ""
    exs = library.get("exercises") or {}
    head = session.get("title") or "Strength"
    if session.get("minutes"):
        head += f" · about {session['minutes']} min"
    lines = [head]
    has_main = False
    for p in session.get("parts") or []:
        items = [it for it in p.get("items") or [] if isinstance(it, dict) and it.get("ex")]
        if not items:
            continue
        title = str(p.get("title") or "")
        has_main = has_main or title.lower() in ("main", "power")
        paired = any(str(it.get("pair") or "").isdigit() for it in items)
        if paired:
            title += " (alternate the two lifts in each pair)"
        elif p.get("order") == "any" and len(items) > 1:
            title += " (any order)"
        lines += ["", title]
        warm_part = "warm" in str(p.get("title") or "").lower()
        letters: dict = {}
        for it in items:
            name = (exs.get(it["ex"]) or {}).get("name") or it.get("name") or it["ex"].replace("_", " ").capitalize()
            pr = str(it.get("pair") or "")
            if pr.isdigit():
                letters[pr] = letters.get(pr, 0) + 1
                name = f"{pr}{'abcdefgh'[letters[pr] - 1]} {name}"
            sets = it.get("sets")
            dose = (str(it.get("reps") or "") if warm_part and str(sets) == "1"
                    else f"{sets} × {it.get('reps')}" if sets else str(it.get("reps") or ""))
            line = f"{name} {dose}".strip()
            if _rest(it.get("rest_s")):
                line += f" · rest {_rest(it.get('rest_s'))}"
            if it.get("note"):
                line += f" · {it['note']}"
            lines.append(line)
    if has_main:
        lines += ["", "Order is a guide: if kit is busy, do any exercise next. Power first, core last."]
    return "\n".join(lines)


def write_plan(slug: str, event_id, session: dict | None, base: Path | str | None = None) -> dict:
    """Put one event's session into athletes/<slug>/strength-plan.json (session None
    removes it), keep every other event, drop entries dated more than KEEP_DAYS ago.
    Atomic: written to a temp file, then renamed over. Returns the plan written."""
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", str(slug or "")):
        raise ValueError("bad slug")
    if session is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(session.get("date") or "")[:10]):
        raise ValueError("session has no date")
    p = (Path(base) if base else BASE) / "athletes" / slug / PLAN_FILE
    try:
        plan = json.loads(p.read_text())
        plan = plan if isinstance(plan, dict) else {}
    except (OSError, ValueError):
        plan = {}
    key = str(event_id)
    if session is None:
        plan.pop(key, None)
    else:
        plan[key] = session
    cutoff = (_date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    plan = {k: v for k, v in plan.items()
            if isinstance(v, dict) and str(v.get("date") or "")[:10] >= cutoff}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(plan, indent=1, ensure_ascii=False))
    os.replace(tmp, p)
    return plan


if __name__ == "__main__":                 # dry-run: print one session and its event text
    import argparse
    ap = argparse.ArgumentParser(description="Build one strength session (no writes).")
    ap.add_argument("--profile", help="profile.json path (optional)")
    ap.add_argument("--date", default=_date.today().isoformat())
    ap.add_argument("--phase", default="build")
    ap.add_argument("--slot", default="A")
    ap.add_argument("--tier", default="C")
    ap.add_argument("--sports", default="swim,bike,run")
    ap.add_argument("--level", type=int, default=2)
    ap.add_argument("--minutes", type=int)
    a = ap.parse_args()
    lib_ = load_library()
    prof = json.loads(Path(a.profile).read_text()) if a.profile else {}
    s = build(prof, lib_, date=a.date, phase=a.phase, slot=a.slot, tier=a.tier,
              sports=[x for x in a.sports.split(",") if x], level=a.level, minutes=a.minutes)
    print(json.dumps(s, indent=1, ensure_ascii=False))
    print("\n" + describe(s, lib_))
