"""A planned workout's steps in plain words, with the athlete's own numbers (Jamie, 13 Oct
2026: Peak's session sheet said "Tempo 2x25" and nothing about what that meant).

Intervals.icu keeps a planned session's structure in event.workout_doc.steps, as percentages
only ({"duration": 1500, "power": {"start": 76, "end": 84, "units": "%ftp"}}). This turns them
into lines the athlete can ride or run to:

    15 min · warm-up · 114-133 W (60-70% FTP)
    2 × 25 min · tempo · 144-160 W (76-84% FTP), 5 min easy between

Watts from the athlete's FTP, run pace from threshold pace, swim pace from CSS, heart rate
from LTHR; a target without a threshold to convert is shown as its percentage. Pure: the
thresholds are passed in. Repeats ({"reps": 3, "steps": [...]}) come out as "3 ×".
"""
from __future__ import annotations

import re


def _mmss(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def _dur(s) -> str:
    s = int(s or 0)
    if s >= 3600:
        h, m = divmod(round(s / 60), 60)
        return f"{h} h {m:02d}" if m else f"{h} h"
    if s >= 60:
        m, r = divmod(s, 60)
        return f"{m} min" + (f" {r:02d}s" if r else "")
    return f"{s}s"


def _label(pct: float, kind: str) -> str:
    """Intensity word from the middle of a %FTP / %pace / %LTHR range."""
    if kind == "hr":
        bands = ((81, "easy"), (89, "steady"), (94, "tempo"), (100, "threshold"), (999, "hard"))
    elif kind == "pace":
        bands = ((78, "very easy"), (89, "easy"), (95, "tempo"), (102, "threshold"), (999, "fast"))
    else:
        bands = ((56, "easy"), (76, "endurance"), (88, "tempo"), (95, "sweetspot"),
                 (106, "threshold"), (999, "hard"))
    for top, word in bands:
        if pct < top:
            return word
    return "hard"


def _target(step: dict, th: dict) -> tuple[str, float | None, str]:
    """(target text, middle %, kind) for one step."""
    for key, kind in (("power", "power"), ("pace", "pace"), ("hr", "hr")):
        t = step.get(key)
        if not isinstance(t, dict):
            continue
        lo = t.get("start", t.get("value"))
        hi = t.get("end", lo)
        if lo is None:
            continue
        lo, hi = float(lo), float(hi)
        mid = (lo + hi) / 2
        units = str(t.get("units") or "")
        rng = f"{lo:g}-{hi:g}%" if lo != hi else f"{lo:g}%"
        if kind == "power" and "ftp" in units and th.get("ftp"):
            w = (round(th["ftp"] * lo / 100), round(th["ftp"] * hi / 100))
            ws = f"{w[0]}-{w[1]} W" if w[0] != w[1] else f"{w[0]} W"
            return f"{ws} ({rng} FTP)", mid, kind
        if kind == "pace" and "pace" in units and th.get("pace_s"):
            per = th.get("pace_unit", "/km")
            fast, slow = th["pace_s"] / (hi / 100), th["pace_s"] / (lo / 100)
            ps = f"{_mmss(fast)}-{_mmss(slow)}{per}" if lo != hi else f"{_mmss(fast)}{per}"
            return f"{ps} ({rng} of threshold pace)", mid, kind
        if kind == "hr" and th.get("lthr"):
            b = (round(th["lthr"] * lo / 100), round(th["lthr"] * hi / 100))
            return f"{b[0]}-{b[1]} bpm ({rng} LTHR)", mid, kind
        name = {"power": "FTP", "pace": "pace", "hr": "LTHR"}[kind]
        return f"{rng} {name}", mid, kind
    return "", None, ""


def _line(step: dict, th: dict, i: int, n: int) -> str:
    tgt, mid, kind = _target(step, th)
    word = _label(mid, kind) if mid is not None else ""
    if i == 0 and n > 2 and word in ("very easy", "easy", "endurance", "steady"):
        word = "warm-up"
    elif i == n - 1 and n > 2 and word in ("very easy", "easy", "endurance", "steady"):
        word = "cool-down"
    txt = str(step.get("text") or "").strip()
    parts = [_dur(step.get("duration"))] + ([word] if word else []) + ([tgt] if tgt else [])
    return " · ".join(parts) + (f" ({txt})" if txt else "")


def _fold(out: list) -> list:
    """Repeats written out step by step (work, rest, work, rest, work) become
    "3 × work, rest between"; a trailing work without its rest is the last rep."""
    res, i = [], 0
    while i < len(out):
        if i + 2 < len(out) and out[i] == out[i + 2]:
            work, rest, k = out[i], out[i + 1], i
            reps = 0
            while k < len(out) and out[k] == work:
                reps += 1
                if k + 1 < len(out) and out[k + 1] == rest and k + 2 < len(out) and out[k + 2] == work:
                    k += 2
                else:
                    k += 1
                    break
            if reps >= 2:
                trailing_rest = k < len(out) and out[k] == rest
                short = re.sub(r"\s*\([^)]*%[^)]*\)", "", rest).split(" · ")
                rest_txt = (f"{short[0]} {short[1]} ({short[2]})" if len(short) == 3
                            else " ".join(short))
                res.append(f"{reps} × {work}, with {rest_txt} between")
                i = k + (1 if trailing_rest else 0)
                continue
        res.append(out[i])
        i += 1
    return res


def lines(workout_doc: dict | None, thresholds: dict) -> list[str]:
    """The workout as plain lines; [] when there is no structure."""
    steps = (workout_doc or {}).get("steps") or []
    out = []
    n = len(steps)
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            continue
        if s.get("reps") and isinstance(s.get("steps"), list):
            inner = [_line(x, thresholds, 1, 3) for x in s["steps"] if isinstance(x, dict)]
            out.append(f"{int(s['reps'])} × [" + "; ".join(inner) + "]")
        else:
            out.append(_line(s, thresholds, i, n))
    return _fold(out)


def thresholds_for(sport_settings: list, sport: str, profile: dict | None = None) -> dict:
    """{ftp, pace_s, pace_unit, lthr} for one sport from Intervals.icu sport settings (the
    numbers its own percentages are relative to), CSS from the profile for swims."""
    sport = sport or ""
    th = {}
    for ss in sport_settings or []:
        types = ss.get("types") or []
        if not any(t == sport or t in sport or sport in t for t in types):
            continue
        if ss.get("ftp"):
            th["ftp"] = float(ss["ftp"])
        if ss.get("lthr"):
            th["lthr"] = float(ss["lthr"])
        tp = ss.get("threshold_pace")
        if tp:
            if "Swim" in sport:
                th["pace_s"], th["pace_unit"] = 100.0 / float(tp), "/100m"
            else:
                th["pace_s"], th["pace_unit"] = 1000.0 / float(tp), "/km"
        break
    if "Swim" in sport and profile and profile.get("swim_css_per_100m"):
        m = re.match(r"(\d+):(\d{2})", str(profile["swim_css_per_100m"]))
        if m:
            th["pace_s"], th["pace_unit"] = int(m.group(1)) * 60 + int(m.group(2)), "/100m"
    return th
