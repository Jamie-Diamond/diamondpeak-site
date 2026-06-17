#!/usr/bin/env python3
"""Chart generation for ClaudeCoach. Uses QuickChart.io (Chart.js 4) — no extra dependencies."""

import json, math, ssl, urllib.request
from datetime import date as _date

_cafile = "/etc/ssl/cert.pem" if __import__("os").path.exists("/etc/ssl/cert.pem") else None
SSL_CONTEXT = ssl.create_default_context(cafile=_cafile)
QUICKCHART = "https://quickchart.io/chart"

# ── Coaching-level label sets ─────────────────────────────────────────────────
def _lbl(coaching_level: str) -> dict:
    mid = {
        "ctl":          "Fitness",
        "atl":          "Fatigue",
        "fitness_title":"Fitness & Fatigue",
        "fitness_yaxis":"Fitness / Fatigue",
        "tsb_line":     "Form",
        "form_title":   "Form",
        "form_yaxis":   "Form",
        "load_tss":     "Load",
        "load_tsb":     "Form",
    }
    pro = {
        "ctl":          "Fitness (CTL)",
        "atl":          "Fatigue (ATL)",
        "fitness_title":"Fitness (CTL) & Fatigue (ATL)",
        "fitness_yaxis":"CTL / ATL",
        "tsb_line":     "Form (TSB)",
        "form_title":   "Form (TSB)",
        "form_yaxis":   "TSB",
        "load_tss":     "TSS",
        "load_tsb":     "Form (TSB)",
    }
    return pro if coaching_level == "pro" else mid


# Brand colours
C_CTL   = "rgb(26,82,118)"
C_ATL   = "rgb(192,57,43)"
C_TSB_P = "rgba(29,104,64,0.8)"
C_TSB_N = "rgba(192,57,43,0.7)"

ZONE_COLOURS = {
    "Z1":       "#b3d4ff",
    "Z2":       "#56a0d3",
    "Z3":       "#f5a623",
    "Z4":       "#e05c00",
    "Z5+":      "#c0392b",
    "Recovery": "#a8d5a2",
    "WU/CD":    "#d0d0d0",
}

SPORT_COLOURS = {
    "Swim":           "#1a5276",
    "Ride":           "#1d6840",
    "Run":            "#c0392b",
    "Strength":       "#7f8c8d",
    "WeightTraining": "#7f8c8d",
    "Other":          "#b0aaa0",
}
_PLANNED_ALPHA = 0.28


def _norm_sport(s):
    if not s:
        return "Other"
    s = str(s)
    if any(x in s for x in ("Ride", "Cycling", "Gravel", "Virtual")):
        return "Ride"
    if "Run" in s:
        return "Run"
    if "Swim" in s:
        return "Swim"
    if any(x in s for x in ("Strength", "Weight", "Gym")):
        return "Strength"
    return "Other"


def _rgba(hex_colour, alpha):
    r = int(hex_colour[1:3], 16)
    g = int(hex_colour[3:5], 16)
    b = int(hex_colour[5:7], 16)
    return f"rgba({r},{g},{b},{alpha})"


def _fetch(config, width=720, height=460):
    payload = json.dumps({
        "chart": config,
        "width": width,
        "height": height,
        "format": "png",
        "backgroundColor": "white",
        "version": "4",
    }).encode()
    req = urllib.request.Request(
        QUICKCHART, data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20, context=SSL_CONTEXT) as r:
        return r.read()


# ── Shared helper ─────────────────────────────────────────────────────────────

def _today_annotation(today, labels):
    if today and today in labels:
        return {
            "type": "line",
            "xMin": today, "xMax": today,
            "borderColor": "rgba(30,30,30,0.85)",
            "borderWidth": 2.5,
            "label": {
                "display": True, "content": "Today",
                "position": "start",
                "backgroundColor": "rgba(30,30,30,0.85)",
                "color": "#fff", "font": {"size": 11, "weight": "bold"},
            },
        }
    return None


def _projected_box(today, labels):
    """Light shaded box over the future (projected) region — today → end of data."""
    if today and labels and today in labels and labels[-1] != today:
        return {
            "type": "box",
            "xMin": today, "xMax": labels[-1],
            "backgroundColor": "rgba(120,120,120,0.07)",
            "borderWidth": 0, "drawTime": "beforeDatasetsDraw",
        }
    return None


def _parse_fitness_payload(payload):
    if isinstance(payload, dict):
        return payload.get("data", []), payload.get("today")
    return payload, None


def _today_index(data):
    """Last index of a non-projected day (i.e. 'today'). None if all projected/empty."""
    ti = None
    for i, d in enumerate(data):
        if not d.get("projected"):
            ti = i
    return ti


def _phase_box(ph, labels):
    """Box annotation spanning a training-phase window, labelled with the phase name."""
    x0, x1 = ph.get("x0"), ph.get("x1")
    if not (x0 in labels and x1 in labels):
        return None
    return {
        "type": "box",
        "xMin": x0, "xMax": x1,
        "backgroundColor": ph.get("color", "rgba(120,120,120,0.06)"),
        "borderWidth": 0, "drawTime": "beforeDatasetsDraw",
        "label": {
            "display": True, "content": ph.get("name", ""),
            "position": {"x": "center", "y": "start"},
            "color": "rgba(120,120,120,0.7)", "font": {"size": 9},
            "backgroundColor": "transparent",
        },
    }


# ── Fitness chart (CTL + ATL) ──────────────────────────────────────────────────

def fitness_chart(payload, coaching_level="mid"):
    """
    payload: {"today":"MM-DD","data":[{date,ctl,atl,tsb},...]}  or bare list.
    Renders CTL (teal filled) + ATL (purple dashed) — TSB is a separate form_chart.
    """
    data, today = _parse_fitness_payload(payload)
    if isinstance(payload, dict) and "level" in payload:
        coaching_level = payload["level"]
    L = _lbl(coaching_level)
    labels = [d["date"][5:] for d in data]
    ctl    = [round(d["ctl"], 1) for d in data]
    atl    = [round(d["atl"], 1) for d in data]

    # Separate, data-fitted axes so Fitness (CTL) and Fatigue (ATL) each fill the
    # panel — ATL is more volatile and was squashing the CTL line on a shared axis.
    def _yr(vals):
        if not vals:
            return 40, 130
        lo, hi = min(vals), max(vals)
        pad = max(3, (hi - lo) * 0.12)
        return int(lo - pad), int(hi + pad) + 1
    _cmin, _cmax = _yr(ctl)
    _amin, _amax = _yr(atl)

    # Today index = last non-projected day.
    ti = _today_index(data)
    ctl_radius = [0] * len(data)
    atl_radius = [0] * len(data)
    if ti is not None:
        ctl_radius[ti] = 5
        atl_radius[ti] = 5

    annotations = {}
    for i, ph in enumerate(payload.get("phases", []) if isinstance(payload, dict) else []):
        pb = _phase_box(ph, labels)
        if pb:
            annotations[f"phase_{i}"] = pb
    box = _projected_box(today, labels)
    if box:
        annotations["projected"] = box
    ann = _today_annotation(today, labels)
    if ann:
        annotations["today"] = ann

    # Today's CTL/ATL values as labelled dots.
    if ti is not None and today and today in labels:
        annotations["ctl_val"] = {
            "type": "label", "xValue": today, "yValue": ctl[ti], "yScaleID": "y",
            "content": [f"CTL {ctl[ti]:.0f}"], "color": "#2e9c8e",
            "backgroundColor": "rgba(255,255,255,0.85)",
            "font": {"size": 11, "weight": "bold"}, "yAdjust": -12,
        }
        annotations["atl_val"] = {
            "type": "label", "xValue": today, "yValue": atl[ti], "yScaleID": "y1",
            "content": [f"ATL {atl[ti]:.0f}"], "color": "#7c4dff",
            "backgroundColor": "rgba(255,255,255,0.85)",
            "font": {"size": 11, "weight": "bold"}, "yAdjust": -12,
        }

    # Ramp readout (CTL change over the trailing 7 days) — top-left label.
    ramp = None
    if ti is not None and ti - 7 >= 0:
        ramp = round(ctl[ti] - ctl[ti - 7], 1)
    if ramp is not None:
        if ramp > 8:
            _rc = "#c0392b"
        elif ramp > 5 or ramp < -1:
            _rc = "#c98a1f"
        else:
            _rc = "#1d6840"
        annotations["ramp"] = {
            "type": "label", "xValue": labels[0], "yValue": _cmax, "yScaleID": "y",
            "content": [f"Ramp {ramp:+.1f} CTL/wk"], "color": _rc,
            "backgroundColor": "rgba(255,255,255,0.85)",
            "font": {"size": 11, "weight": "bold"},
            "position": {"x": "start", "y": "start"},
            "xAdjust": 4, "yAdjust": 4,
        }

    config = {
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [
                {
                    "label": L["ctl"],
                    "data": ctl,
                    "yAxisID": "y",
                    "borderColor": "#2e9c8e",
                    "backgroundColor": "rgba(46,156,142,0.15)",
                    "borderWidth": 2.5,
                    "pointRadius": ctl_radius,
                    "pointBackgroundColor": "#2e9c8e",
                    "fill": "origin",
                    "tension": 0.3,
                },
                {
                    "label": L["atl"],
                    "data": atl,
                    "yAxisID": "y1",
                    "borderColor": "#7c4dff",
                    "backgroundColor": "transparent",
                    "borderWidth": 2,
                    "borderDash": [6, 3],
                    "pointRadius": atl_radius,
                    "pointBackgroundColor": "#7c4dff",
                    "fill": False,
                    "tension": 0.3,
                },
            ],
        },
        "options": {
            "plugins": {
                "title": {
                    "display": True,
                    "text": L["fitness_title"],
                    "font": {"size": 14},
                },
                "legend": {
                    "position": "top",
                    "labels": {"boxWidth": 12, "font": {"size": 12}},
                },
                "annotation": {"annotations": annotations},
            },
            "scales": {
                "x": {"ticks": {"maxRotation": 45, "autoSkip": True, "maxTicksLimit": 10, "font": {"size": 11}}},
                "y": {
                    "position": "left",
                    "title": {"display": True, "text": L["ctl"], "font": {"size": 12}, "color": "#2e9c8e"},
                    "ticks": {"font": {"size": 11}, "color": "#2e9c8e"},
                    "min": _cmin, "max": _cmax,
                    "grid": {"color": "rgba(0,0,0,0.06)"},
                },
                "y1": {
                    "position": "right",
                    "title": {"display": True, "text": L["atl"], "font": {"size": 12}, "color": "#7c4dff"},
                    "ticks": {"font": {"size": 11}, "color": "#7c4dff"},
                    "min": _amin, "max": _amax,
                    "grid": {"drawOnChartArea": False},
                },
            },
        },
    }
    return _fetch(config, height=420)


# ── Form chart (TSB with coloured zones) ──────────────────────────────────────

def form_chart(payload, coaching_level="mid"):
    """
    Same payload as fitness_chart. Renders TSB as a line with coloured background zones:
      > +5 : teal   — fresh / race-ready
      0 to +5 : light green — optimal
     -20 to 0 : amber  — normal training load
      < -20   : red   — heavy / overreaching risk
    """
    data, today = _parse_fitness_payload(payload)
    if isinstance(payload, dict) and "level" in payload:
        coaching_level = payload["level"]
    L = _lbl(coaching_level)
    labels = [d["date"][5:] for d in data]
    tsb    = [round(d["tsb"], 1) for d in data]

    # Fit y-axis to the TSB data (was a fixed −40–20) but always keep the −20 (heavy)
    # and +5 (fresh) reference lines in view so the zones stay meaningful.
    _lo, _hi = (min(tsb), max(tsb)) if tsb else (-20, 5)
    _pad = max(3, (_hi - _lo) * 0.12)
    _ymin = min(int(_lo - _pad), -22)
    _ymax = max(int(_hi + _pad) + 1, 7)

    annotations = {
        "zone_fresh": {
            "type": "box", "yMin": 5, "yMax": 60,
            "backgroundColor": "rgba(46,156,142,0.10)",
            "borderWidth": 0, "drawTime": "beforeDatasetsDraw",
        },
        "zone_ok": {
            "type": "box", "yMin": 0, "yMax": 5,
            "backgroundColor": "rgba(120,200,140,0.10)",
            "borderWidth": 0, "drawTime": "beforeDatasetsDraw",
        },
        "zone_load": {
            "type": "box", "yMin": -20, "yMax": 0,
            "backgroundColor": "rgba(200,160,60,0.08)",
            "borderWidth": 0, "drawTime": "beforeDatasetsDraw",
        },
        "zone_heavy": {
            "type": "box", "yMin": -60, "yMax": -20,
            "backgroundColor": "rgba(192,57,43,0.09)",
            "borderWidth": 0, "drawTime": "beforeDatasetsDraw",
        },
        "ref_5": {
            "type": "line", "yMin": 5, "yMax": 5,
            "borderColor": "rgba(46,156,142,0.45)", "borderWidth": 1, "borderDash": [4, 3],
            "label": {"display": True, "content": "+5 fresh", "position": "end",
                      "backgroundColor": "transparent", "color": "#2e9c8e", "font": {"size": 9}},
        },
        "ref_0": {
            "type": "line", "yMin": 0, "yMax": 0,
            "borderColor": "rgba(100,100,100,0.30)", "borderWidth": 1,
        },
        "ref_m20": {
            "type": "line", "yMin": -20, "yMax": -20,
            "borderColor": "rgba(192,57,43,0.40)", "borderWidth": 1, "borderDash": [4, 3],
            "label": {"display": True, "content": "−20 heavy", "position": "end",
                      "backgroundColor": "transparent", "color": "#c0392b", "font": {"size": 9}},
        },
    }

    for i, ph in enumerate(payload.get("phases", []) if isinstance(payload, dict) else []):
        pb = _phase_box(ph, labels)
        if pb:
            annotations[f"phase_{i}"] = pb
    box = _projected_box(today, labels)
    if box:
        annotations["projected"] = box
    ann = _today_annotation(today, labels)
    if ann:
        annotations["today"] = ann

    # Today's TSB value as a labelled dot.
    ti = _today_index(data)
    tsb_radius = [0] * len(data)
    if ti is not None:
        tsb_radius[ti] = 5
        if today and today in labels:
            _tv = round(tsb[ti])
            _tlabel = "TSB 0" if _tv == 0 else f"TSB {_tv:+d}"
            annotations["tsb_val"] = {
                "type": "label", "xValue": today, "yValue": tsb[ti], "yScaleID": "y",
                "content": [_tlabel], "color": "#3c3c3c",
                "backgroundColor": "rgba(255,255,255,0.85)",
                "font": {"size": 11, "weight": "bold"}, "yAdjust": -12,
            }

    config = {
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [{
                "label": L["tsb_line"],
                "data": tsb,
                "borderColor": "rgba(60,60,60,0.75)",
                "backgroundColor": "rgba(60,60,60,0.06)",
                "borderWidth": 2,
                "pointRadius": tsb_radius,
                "pointBackgroundColor": "rgba(60,60,60,0.9)",
                "fill": "origin",
                "tension": 0.3,
            }],
        },
        "options": {
            "plugins": {
                "title": {
                    "display": True,
                    "text": L["form_title"],
                    "font": {"size": 14},
                },
                "legend": {"display": False},
                "annotation": {"annotations": annotations},
            },
            "scales": {
                "x": {"ticks": {"maxRotation": 45, "autoSkip": True, "maxTicksLimit": 10, "font": {"size": 11}}},
                "y": {
                    "title": {"display": True, "text": L["form_yaxis"], "font": {"size": 12}},
                    "ticks": {"font": {"size": 11}},
                    "min": _ymin,
                    "max": _ymax,
                    "grid": {"color": "rgba(0,0,0,0.06)"},
                },
            },
        },
    }
    return _fetch(config, height=320)


# ── Training load chart (TSS stacked by sport + TSB overlay) ─────────────────

_K_CTL = 1 - math.exp(-1 / 42)
_K_ATL = 1 - math.exp(-1 / 7)

# Canonical forward-PMC projection (single source shared with the planning CLI).
# Falls back to the identical inline EMA below if the primitive can't be imported,
# so the chart can never break on a path issue.
try:
    import os as _os, sys as _sys
    _IA = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                        "ironman-analysis")
    if _IA not in _sys.path:
        _sys.path.insert(0, _IA)
    from primitives.load import project_pmc_daily as _project_pmc_daily
except Exception:
    _project_pmc_daily = None


def _project_tsb(days, seed_ctl, seed_atl):
    """Return TSB list: historical values for past/today, PMC-projected for future days.

    Forward projection delegates to primitives.load.project_pmc_daily so the chart
    and the conversational planning tools project identical numbers; the inline EMA
    is an exact-math fallback used only if that import fails."""
    today_str = _date.today().strftime("%Y-%m-%d")
    if _project_pmc_daily is not None:
        future_tss = [sum((a.get("tss") or 0) for a in d.get("activities", []))
                      for d in days if d.get("date", "") > today_str]
        proj = iter(_project_pmc_daily(seed_ctl, seed_atl, future_tss))
        return [next(proj)["tsb"] if d.get("date", "") > today_str
                else round(d.get("tsb") or 0, 1)
                for d in days]
    result = []
    ctl, atl = float(seed_ctl), float(seed_atl)
    for d in days:
        if d.get("date", "") > today_str:
            day_tss = sum((a.get("tss") or 0) for a in d.get("activities", []))
            ctl = ctl + (day_tss - ctl) * _K_CTL
            atl = atl + (day_tss - atl) * _K_ATL
            result.append(round(ctl - atl, 1))
        else:
            result.append(round(d.get("tsb") or 0, 1))
    return result


def load_chart(payload, coaching_level="mid"):
    """
    payload: {"today":"MM-DD","days":[{"date":"YYYY-MM-DD","tsb":-8.7,
              "activities":[{"sport":"Ride","tss":117,"dur":120,"status":"completed"},...]},...]}
    Stacked TSS bars by sport (actual=solid, planned=28% alpha) + TSB line on right axis.
    """
    if isinstance(payload, dict):
        days      = payload.get("days", [])
        today     = payload.get("today")
        seed_ctl  = payload.get("seed_ctl")
        seed_atl  = payload.get("seed_atl")
        if "level" in payload:
            coaching_level = payload["level"]
    else:
        return None
    L = _lbl(coaching_level)
    if not days:
        return None

    SPORTS = ["Ride", "Run", "Swim", "Strength", "Other"]
    BASE   = {"Ride": "#1d6840", "Run": "#c0392b", "Swim": "#1a5276", "Strength": "#7f8c8d", "Other": "#b0aaa0"}

    labels   = [d["date"][5:] for d in days]
    datasets = []

    for sport in SPORTS:
        tss_vals, bg_vals, has_data = [], [], False
        for d in days:
            sport_tss, is_planned = 0, False
            for a in d.get("activities", []):
                if _norm_sport(a.get("sport", "")) == sport:
                    sport_tss  += a.get("tss") or 0
                    if a.get("status") == "planned":
                        is_planned = True
            tss_vals.append(round(sport_tss, 1))
            if sport_tss > 0:
                has_data = True
            alpha = (_PLANNED_ALPHA if is_planned else 0.87) if sport_tss > 0 else 0
            bg_vals.append(_rgba(BASE.get(sport, "#9b59b6"), alpha))

        if not has_data:
            continue

        datasets.append({
            "type": "bar", "label": sport,
            "data": tss_vals, "backgroundColor": bg_vals,
            "solidColor": BASE.get(sport, "#9b59b6"),
            "stack": "tss", "order": 2, "yAxisID": "y",
        })

    def _tsb_dot(v):
        if v > 5:   return "rgba(46,156,142,0.90)"   # teal — fresh
        if v >= -20: return "rgba(201,135,31,0.90)"  # amber — load
        return "rgba(192,57,43,0.90)"                # red — heavy

    if seed_ctl is not None and seed_atl is not None:
        tsb_vals = _project_tsb(days, seed_ctl, seed_atl)
    else:
        tsb_vals = [round(d.get("tsb") or 0, 1) for d in days]
    datasets.append({
        "type": "line", "label": L["load_tsb"],
        "data": tsb_vals,
        "borderColor": "rgba(70,70,70,0.80)",
        "backgroundColor": "transparent",
        "borderWidth": 2.5, "pointRadius": 7, "pointHoverRadius": 9,
        "pointBackgroundColor": [_tsb_dot(v) for v in tsb_vals],
        "pointBorderColor": "rgba(255,255,255,0.85)",
        "pointBorderWidth": 1.5,
        "fill": False, "tension": 0.3, "order": 1, "yAxisID": "y1",
    })

    annotations = {
        "zone_fresh": {
            "type": "box", "yMin": 5, "yMax": 40, "yScaleID": "y1",
            "backgroundColor": "rgba(46,156,142,0.10)",
            "borderWidth": 0, "drawTime": "beforeDatasetsDraw",
        },
        "zone_ok": {
            "type": "box", "yMin": 0, "yMax": 5, "yScaleID": "y1",
            "backgroundColor": "rgba(120,200,140,0.10)",
            "borderWidth": 0, "drawTime": "beforeDatasetsDraw",
        },
        "zone_load": {
            "type": "box", "yMin": -20, "yMax": 0, "yScaleID": "y1",
            "backgroundColor": "rgba(200,160,60,0.08)",
            "borderWidth": 0, "drawTime": "beforeDatasetsDraw",
        },
        "zone_heavy": {
            "type": "box", "yMin": -60, "yMax": -20, "yScaleID": "y1",
            "backgroundColor": "rgba(192,57,43,0.09)",
            "borderWidth": 0, "drawTime": "beforeDatasetsDraw",
        },
        "tsb_zero": {
            "type": "line", "scaleID": "y1",
            "value": 0,
            "borderColor": "rgba(100,100,100,0.28)",
            "borderWidth": 1, "borderDash": [4, 3],
        },
    }
    ann = _today_annotation(today, labels)
    if ann:
        annotations["today"] = ann

    config = {
        "type": "bar",
        "data": {"labels": labels, "datasets": datasets},
        "options": {
            "plugins": {
                "title": {
                    "display": True,
                    "text": f"Training load — {L['load_tss']} by sport  ·  {L['load_tsb']} (right axis)",
                    "font": {"size": 14},
                },
                "legend": {
                    "position": "top",
                    "labels": {
                        "boxWidth": 12, "font": {"size": 11},
                        "usePointStyle": False,
                        "generateLabels": "function(chart){return chart.data.datasets.map(function(ds,i){var meta=chart.getDatasetMeta(i);var fill=ds.solidColor||(Array.isArray(ds.backgroundColor)?ds.backgroundColor[0]:ds.backgroundColor);var stroke=ds.type==='line'?ds.borderColor:fill;return{text:ds.label,fillStyle:ds.type==='line'?'transparent':fill,strokeStyle:stroke,lineDash:ds.borderDash||[],lineWidth:ds.type==='line'?2:0,hidden:!chart.getDataVisibility(i),datasetIndex:i,pointStyle:ds.type==='line'?'line':'rect',rotation:0};})}",
                    },
                },
                "annotation": {"annotations": annotations},
            },
            "scales": {
                "x": {
                    "stacked": True,
                    "ticks": {"maxRotation": 45, "autoSkip": True,
                              "maxTicksLimit": 15, "font": {"size": 10}},
                },
                "y": {
                    "stacked": True, "beginAtZero": True, "position": "left",
                    "title": {"display": True, "text": L["load_tss"], "font": {"size": 12}},
                    "ticks": {"font": {"size": 11}},
                    "grid": {"color": "rgba(0,0,0,0.06)"},
                },
                "y1": {
                    "position": "right",
                    "title": {"display": True, "text": L["load_tsb"], "font": {"size": 12}},
                    "ticks": {"font": {"size": 11}},
                    "suggestedMin": -40, "suggestedMax": 20,
                    "grid": {"drawOnChartArea": False},
                },
            },
        },
    }
    return _fetch(config, width=760, height=460)


# ── Week calendar ─────────────────────────────────────────────────────────────

def week_chart(events, title="Training week", week_start=None):
    """
    events: [{date, sport, duration_min, tss (optional), status, name}]
    Planned TSS shown as a dashed line; actual TSS as stacked bars by sport.
    """
    from datetime import datetime, timedelta

    if not events and not week_start:
        return None

    if week_start:
        monday = datetime.strptime(week_start, "%Y-%m-%d")
    else:
        dates = [datetime.strptime(e["date"], "%Y-%m-%d") for e in events]
        first = min(dates)
        monday = first - timedelta(days=first.weekday())

    days     = [monday + timedelta(days=i) for i in range(7)]
    labels   = [d.strftime("%a") + " " + str(d.day) for d in days]
    day_strs = [d.strftime("%Y-%m-%d") for d in days]

    sport_order = ["Swim", "Ride", "Run", "Strength", "WeightTraining"]
    seen = []
    for e in events:
        s = e.get("sport", "Other")
        if s not in seen:
            seen.append(s)
    sports = [s for s in sport_order if s in seen] + [s for s in seen if s not in sport_order]

    planned_tss = [0.0] * 7
    actual      = {s: [0.0] * 7 for s in sports}

    for e in events:
        day_str = e.get("date")
        if day_str not in day_strs:
            continue
        i      = day_strs.index(day_str)
        sport  = e.get("sport", "Other")
        tss    = e.get("tss") or round(e.get("duration_min", 0) * 0.65, 1)
        if e.get("status") == "completed":
            if sport in actual:
                actual[sport][i] += tss
        else:
            planned_tss[i] += tss

    datasets = []

    if any(planned_tss):
        datasets.append({
            "type": "line",
            "label": "Planned TSS",
            "data": [v or None for v in planned_tss],
            "borderColor": "rgba(100,100,100,0.65)",
            "backgroundColor": "transparent",
            "borderWidth": 2,
            "borderDash": [5, 4],
            "pointRadius": 6,
            "pointBackgroundColor": "rgba(100,100,100,0.45)",
            "fill": False,
            "spanGaps": False,
            "order": 0,
        })

    for sport in sports:
        data = [round(v, 1) for v in actual.get(sport, [0]*7)]
        if not any(data):
            continue
        colour = SPORT_COLOURS.get(sport, SPORT_COLOURS["Other"])
        label  = "Strength" if sport == "WeightTraining" else sport
        datasets.append({
            "type": "bar",
            "label": label,
            "data": data,
            "backgroundColor": colour,
            "stack": "actual",
            "order": 1,
        })

    if not datasets:
        return None

    annotations = {
        "sat": {
            "type": "box",
            "xMin": labels[5], "xMax": labels[5],
            "backgroundColor": "rgba(0,0,0,0.04)",
            "borderWidth": 0,
            "drawTime": "beforeDatasetsDraw",
        },
        "sun": {
            "type": "box",
            "xMin": labels[6], "xMax": labels[6],
            "backgroundColor": "rgba(0,0,0,0.04)",
            "borderWidth": 0,
            "drawTime": "beforeDatasetsDraw",
        },
    }

    config = {
        "type": "bar",
        "data": {"labels": labels, "datasets": datasets},
        "options": {
            "plugins": {
                "title": {"display": True, "text": title, "font": {"size": 15}},
                "legend": {"position": "top", "labels": {"boxWidth": 14, "font": {"size": 12}}},
                "annotation": {"annotations": annotations},
            },
            "scales": {
                "x": {"ticks": {"font": {"size": 13}}},
                "y": {
                    "beginAtZero": True,
                    "title": {"display": True, "text": "TSS", "font": {"size": 13}},
                    "ticks": {"font": {"size": 12}},
                    "stacked": True,
                },
            },
        },
    }
    return _fetch(config, height=460)


# ── Session structure ─────────────────────────────────────────────────────────

def session_chart(name, intervals, ftp=316):
    """intervals: [{duration_seconds, average_power, type}]"""
    if not intervals:
        return None

    datasets = []
    for seg in intervals:
        dur_min = round(seg.get("duration_seconds", 0) / 60, 1)
        if dur_min < 0.5:
            continue
        pwr   = seg.get("average_power") or 0
        itype = seg.get("type", "").upper()

        if pwr and ftp:
            pct = pwr / ftp
            if pct < 0.55:
                zone, colour = "Z1",   ZONE_COLOURS["Z1"]
            elif pct < 0.75:
                zone, colour = "Z2",   ZONE_COLOURS["Z2"]
            elif pct < 0.90:
                zone, colour = "Z3",   ZONE_COLOURS["Z3"]
            elif pct < 1.05:
                zone, colour = "Z4",   ZONE_COLOURS["Z4"]
            else:
                zone, colour = "Z5+",  ZONE_COLOURS["Z5+"]
        elif itype == "RECOVERY":
            zone, colour = "Recovery", ZONE_COLOURS["Recovery"]
        else:
            zone, colour = "WU/CD",    ZONE_COLOURS["WU/CD"]

        datasets.append({
            "label": f"{zone} ({dur_min}m)",
            "data": [dur_min],
            "backgroundColor": colour,
            "stack": "s",
        })

    if not datasets:
        return None

    total = sum(d["data"][0] for d in datasets)
    config = {
        "type": "bar",
        "data": {
            "labels": [f"{name}  ({round(total)} min)"],
            "datasets": datasets,
        },
        "options": {
            "indexAxis": "y",
            "plugins": {
                "title": {"display": True, "text": "Session structure", "font": {"size": 15}},
                "legend": {"position": "bottom", "labels": {"boxWidth": 14, "font": {"size": 12}}},
            },
            "scales": {
                "x": {
                    "stacked": True,
                    "title": {"display": True, "text": "Minutes", "font": {"size": 12}},
                    "ticks": {"beginAtZero": True, "font": {"size": 12}},
                },
                "y": {"stacked": True, "ticks": {"font": {"size": 13}}},
            },
        },
    }
    return _fetch(config, height=300)


# ── Power curve ───────────────────────────────────────────────────────────────

def power_curve_chart(efforts, ftp=316):
    """
    efforts: [{"label":"5s","power":980}, {"label":"1m","power":520}, ...]
    Standard duration labels: 5s 15s 30s 1m 2m 5m 10m 20m 30m 60m 90m
    """
    if not efforts:
        return None

    labels = [e["label"] for e in efforts]
    powers = [e["power"] for e in efforts]
    max_p  = max(powers) if powers else ftp * 2

    zone_annotations = {
        "z1": {"type": "box", "yMin": 0,          "yMax": ftp * 0.55, "backgroundColor": "rgba(200,200,200,0.07)", "borderWidth": 0},
        "z2": {"type": "box", "yMin": ftp * 0.55, "yMax": ftp * 0.75, "backgroundColor": "rgba(86,160,211,0.07)",  "borderWidth": 0},
        "z3": {"type": "box", "yMin": ftp * 0.75, "yMax": ftp * 0.90, "backgroundColor": "rgba(245,166,35,0.08)",  "borderWidth": 0},
        "z4": {"type": "box", "yMin": ftp * 0.90, "yMax": ftp * 1.05, "backgroundColor": "rgba(224,92,0,0.09)",    "borderWidth": 0},
        "z5": {"type": "box", "yMin": ftp * 1.05, "yMax": max_p * 1.1, "backgroundColor": "rgba(192,57,43,0.07)", "borderWidth": 0},
        "ftp": {
            "type": "line",
            "yMin": ftp, "yMax": ftp,
            "borderColor": "rgba(26,82,118,0.55)",
            "borderWidth": 1.5,
            "borderDash": [5, 4],
            "label": {
                "display": True,
                "content": f"FTP {ftp}W",
                "position": "end",
                "backgroundColor": "rgba(255,255,255,0.85)",
                "color": C_CTL,
                "font": {"size": 11},
            },
        },
    }

    config = {
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [{
                "label": "Best power",
                "data": powers,
                "borderColor": C_CTL,
                "backgroundColor": "rgba(26,82,118,0.15)",
                "borderWidth": 3,
                "pointRadius": 5,
                "pointBackgroundColor": C_CTL,
                "fill": "origin",
                "tension": 0.35,
            }],
        },
        "options": {
            "plugins": {
                "title": {"display": True, "text": "Power curve — best efforts", "font": {"size": 15}},
                "legend": {"display": False},
                "annotation": {"annotations": zone_annotations},
            },
            "scales": {
                "x": {
                    "title": {"display": True, "text": "Duration", "font": {"size": 12}},
                    "ticks": {"font": {"size": 12}},
                },
                "y": {
                    "title": {"display": True, "text": "Watts", "font": {"size": 12}},
                    "ticks": {"font": {"size": 11}},
                    "beginAtZero": False,
                },
            },
        },
    }
    return _fetch(config, height=480)


# ── Recovery chart (HRV vs own baseline + RHR + sleep) ─────────────────────────

def recovery_chart(payload, coaching_level="mid"):
    """
    payload: {"today":"MM-DD","days":[{date,hrv,rhr,sleep_h},...]}  (~42 days)
    HRV dots vs a 7-day trailing rolling-mean baseline ± 1 trailing SD (shaded band).
    A dot below the band = under-recovered. RHR on a right axis, sleep as faint
    bars pinned low on a hidden third axis so they stay subtle.
    """
    if isinstance(payload, dict):
        days  = payload.get("days", [])
        today = payload.get("today")
        if "level" in payload:
            coaching_level = payload["level"]
    else:
        return None
    if not days:
        return None

    labels = [d["date"][5:] for d in days]
    hrv    = [d.get("hrv") for d in days]
    rhr    = [d.get("rhr") for d in days]
    sleep  = [d.get("sleep_h") for d in days]

    # 7-day trailing baseline (rolling mean) + band (mean ± 1 trailing SD), computed
    # over non-None HRV values inside each trailing window. Fallback SD keeps the band
    # from collapsing when too few points exist — and crucially gives every day a
    # non-null band so fill-between never sees leading nulls (which render garbage).
    baseline, band_lo, band_hi = [], [], []
    for i in range(len(days)):
        window = [hrv[j] for j in range(max(0, i - 6), i + 1) if hrv[j] is not None]
        if not window:
            baseline.append(None)
            band_lo.append(None)
            band_hi.append(None)
            continue
        mean = sum(window) / len(window)
        if len(window) >= 3:
            var = sum((v - mean) ** 2 for v in window) / (len(window) - 1)
            sd  = math.sqrt(var)
        else:
            sd = 0.0
        sd = max(sd, 0.5 * mean * 0.1)  # floor so the band is always visible
        baseline.append(round(mean, 1))
        band_lo.append(round(mean - sd, 1))
        band_hi.append(round(mean + sd, 1))

    # HRV-axis range fitted to the band + dots (don't force 0).
    _vals = [v for v in (hrv + band_lo + band_hi) if v is not None]
    if _vals:
        _lo, _hi = min(_vals), max(_vals)
        _pad = max(2, (_hi - _lo) * 0.12)
        _ymin, _ymax = int(_lo - _pad), int(_hi + _pad) + 1
    else:
        _ymin, _ymax = 0, 100

    # Sleep axis: pin bars to the bottom quarter so they never dominate.
    _smax = max([s for s in sleep if s is not None], default=8) or 8

    # Highlight today's HRV dot.
    today_radius = [0] * len(days)
    if today and today in labels:
        ti = labels.index(today)
        if hrv[ti] is not None:
            today_radius[ti] = 7

    annotations = {}
    ann = _today_annotation(today, labels)
    if ann:
        annotations["today"] = ann

    config = {
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [
                # Sleep bars first (drawn under everything), hidden axis.
                {
                    "type": "bar",
                    "label": "Sleep (h)",
                    "data": [round(s, 1) if s is not None else None for s in sleep],
                    "yAxisID": "y2",
                    "backgroundColor": "rgba(120,120,120,0.16)",
                    "borderWidth": 0,
                    "order": 5,
                },
                # Band: lower then upper with fill:'-1' (fills between the two).
                {
                    "type": "line",
                    "label": "−1 SD",
                    "data": band_lo,
                    "yAxisID": "y",
                    "borderColor": "transparent",
                    "backgroundColor": "rgba(46,156,142,0.14)",
                    "pointRadius": 0,
                    "fill": False,
                    "spanGaps": True,
                    "order": 4,
                },
                {
                    "type": "line",
                    "label": "+1 SD band",
                    "data": band_hi,
                    "yAxisID": "y",
                    "borderColor": "transparent",
                    "backgroundColor": "rgba(46,156,142,0.14)",
                    "pointRadius": 0,
                    "fill": "-1",
                    "spanGaps": True,
                    "order": 4,
                },
                # Baseline rolling-mean line.
                {
                    "type": "line",
                    "label": "Baseline (7d)",
                    "data": baseline,
                    "yAxisID": "y",
                    "borderColor": "#2e9c8e",
                    "backgroundColor": "transparent",
                    "borderWidth": 2,
                    "pointRadius": 0,
                    "fill": False,
                    "spanGaps": True,
                    "tension": 0.3,
                    "order": 2,
                },
                # HRV dots (scatter — no connecting line).
                {
                    "type": "line",
                    "label": "HRV",
                    "data": [round(v, 1) if v is not None else None for v in hrv],
                    "yAxisID": "y",
                    "showLine": False,
                    "borderColor": "#2e9c8e",
                    "pointRadius": [r if r else 3.5 for r in today_radius],
                    "pointBackgroundColor": "#2e9c8e",
                    "pointBorderColor": "rgba(255,255,255,0.85)",
                    "pointBorderWidth": 1,
                    "spanGaps": False,
                    "order": 1,
                },
                # RHR line on the right axis.
                {
                    "type": "line",
                    "label": "RHR",
                    "data": [r if r is not None else None for r in rhr],
                    "yAxisID": "y1",
                    "borderColor": "rgba(150,90,80,0.75)",
                    "backgroundColor": "transparent",
                    "borderWidth": 2,
                    "borderDash": [5, 4],
                    "pointRadius": 0,
                    "fill": False,
                    "spanGaps": True,
                    "tension": 0.3,
                    "order": 3,
                },
            ],
        },
        "options": {
            "plugins": {
                "title": {
                    "display": True,
                    "text": "Recovery — HRV vs baseline",
                    "font": {"size": 14},
                },
                "legend": {
                    "position": "top",
                    # Default legend (a custom generateLabels reliably renders elsewhere but
                    # silently drops here given the mixed scatter/band/bar datasets). Both band
                    # bounds carry honest labels so no 'undefined' entry appears.
                    "labels": {"boxWidth": 12, "font": {"size": 11}},
                },
                "annotation": {"annotations": annotations},
            },
            "scales": {
                "x": {"ticks": {"maxRotation": 45, "autoSkip": True, "maxTicksLimit": 12, "font": {"size": 10}}},
                "y": {
                    "position": "left",
                    "title": {"display": True, "text": "HRV (ms)", "font": {"size": 12}, "color": "#2e9c8e"},
                    "ticks": {"font": {"size": 11}, "color": "#2e9c8e"},
                    "min": _ymin, "max": _ymax,
                    "grid": {"color": "rgba(0,0,0,0.06)"},
                },
                "y1": {
                    "position": "right",
                    "title": {"display": True, "text": "RHR (bpm)", "font": {"size": 12}, "color": "rgba(150,90,80,0.9)"},
                    "ticks": {"font": {"size": 11}, "color": "rgba(150,90,80,0.9)"},
                    "grid": {"drawOnChartArea": False},
                },
                "y2": {
                    "display": False,
                    "position": "right",
                    "min": 0, "max": round(_smax * 3.5, 1),
                    "grid": {"drawOnChartArea": False},
                },
            },
        },
    }
    return _fetch(config, width=760, height=440)


# ── Durability chart (Pa:HR decoupling) ────────────────────────────────────────

def durability_chart(payload, coaching_level="mid"):
    """
    payload: {"today":"MM-DD","sessions":[{date,decoupling_pct,sport,if,duration_min},...]}
    Scatter of decoupling % over date, dots coloured by sport (Ride green / Run red).
    Green reference line at 5% ("good < 5%") and a faint zero line. Lower = better.
    """
    if isinstance(payload, dict):
        sessions = payload.get("sessions", [])
        today    = payload.get("today")
        if "level" in payload:
            coaching_level = payload["level"]
    else:
        return None
    if not sessions:
        return None

    # Category x-axis over the session dates (matches the file's idiom). Duplicate
    # dates would collide on a category axis — suffix them so each point is distinct.
    labels, seen = [], {}
    for s in sessions:
        base = s["date"][5:]
        if base in seen:
            seen[base] += 1
            labels.append(f"{base}·{seen[base]}")
        else:
            seen[base] = 0
            labels.append(base)

    SPORT_BASE = {"Ride": "#1d6840", "Run": "#c0392b"}
    datasets = []
    for sport, colour in SPORT_BASE.items():
        data = []
        present = False
        for i, s in enumerate(sessions):
            if _norm_sport(s.get("sport")) == sport:
                data.append(round(s.get("decoupling_pct", 0), 1))
                present = True
            else:
                data.append(None)
        if present:
            datasets.append({
                "type": "line",
                "label": sport,
                "data": data,
                "showLine": False,
                "borderColor": colour,
                "pointRadius": 6,
                "pointHoverRadius": 8,
                "pointBackgroundColor": colour,
                "pointBorderColor": "rgba(255,255,255,0.85)",
                "pointBorderWidth": 1.5,
                "spanGaps": False,
            })

    _vals = [round(s.get("decoupling_pct", 0), 1) for s in sessions]
    _lo = min(_vals + [0])
    _hi = max(_vals + [5])
    _pad = max(1.5, (_hi - _lo) * 0.12)
    _ymin, _ymax = round(_lo - _pad, 1), round(_hi + _pad, 1)

    annotations = {
        "ref_zero": {
            "type": "line", "yMin": 0, "yMax": 0,
            "borderColor": "rgba(100,100,100,0.30)", "borderWidth": 1,
        },
        "ref_good": {
            "type": "line", "yMin": 5, "yMax": 5,
            "borderColor": "rgba(29,104,64,0.55)", "borderWidth": 1.5, "borderDash": [5, 4],
            "label": {"display": True, "content": "good < 5%", "position": "end",
                      "backgroundColor": "rgba(255,255,255,0.85)", "color": "#1d6840", "font": {"size": 10}},
        },
    }

    config = {
        "type": "line",
        "data": {"labels": labels, "datasets": datasets},
        "options": {
            "plugins": {
                "title": {
                    "display": True,
                    "text": "Aerobic durability — Pa:HR decoupling (lower = better)",
                    "font": {"size": 14},
                },
                "legend": {"position": "top", "labels": {"boxWidth": 12, "font": {"size": 12}, "usePointStyle": True}},
                "annotation": {"annotations": annotations},
            },
            "scales": {
                "x": {"ticks": {"maxRotation": 45, "autoSkip": False, "font": {"size": 11}}},
                "y": {
                    "title": {"display": True, "text": "Decoupling %", "font": {"size": 12}},
                    "ticks": {"font": {"size": 11}},
                    "min": _ymin, "max": _ymax,
                    "grid": {"color": "rgba(0,0,0,0.06)"},
                },
            },
        },
    }
    return _fetch(config, height=420)


# ── Compliance chart (planned vs actual TSS) ───────────────────────────────────

def compliance_chart(payload, coaching_level="mid"):
    """
    payload: {"today":"MM-DD","weeks":[{label,planned,actual},...]}  (~8 weeks)
    Grouped bars per week: planned (faded grey) and actual (green ≥90%, amber 70–90%,
    red <70% of plan; neutral if planned==0).
    """
    if isinstance(payload, dict):
        weeks = payload.get("weeks", [])
        if "level" in payload:
            coaching_level = payload["level"]
    else:
        return None
    if not weeks:
        return None

    labels  = [w["label"] for w in weeks]
    planned = [round(w.get("planned", 0), 1) for w in weeks]
    actual  = [round(w.get("actual", 0), 1) for w in weeks]

    def _actual_colour(p, a):
        if p <= 0:
            return "rgba(150,150,150,0.75)"   # neutral — no plan
        ratio = a / p
        if ratio >= 0.9:
            return "rgba(29,104,64,0.85)"     # green — on plan
        if ratio >= 0.7:
            return "rgba(201,135,31,0.85)"    # amber — under
        return "rgba(192,57,43,0.85)"         # red — well under

    actual_bg = [_actual_colour(p, a) for p, a in zip(planned, actual)]

    config = {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": [
                {
                    "label": "Planned",
                    "data": planned,
                    "backgroundColor": "rgba(120,120,120,0.28)",
                    "borderWidth": 0,
                    "order": 2,
                },
                {
                    "label": "Actual",
                    "data": actual,
                    "backgroundColor": actual_bg,
                    "borderWidth": 0,
                    "order": 1,
                },
            ],
        },
        "options": {
            "plugins": {
                "title": {
                    "display": True,
                    "text": "Compliance — planned vs actual TSS",
                    "font": {"size": 14},
                },
                "legend": {
                    "position": "top",
                    # Default legend: Chart.js represents the per-bar-array 'Actual' colour
                    # with its first entry (green), which reads correctly as the on-plan swatch.
                    "labels": {"boxWidth": 12, "font": {"size": 12}},
                },
            },
            "scales": {
                "x": {"ticks": {"maxRotation": 45, "autoSkip": False, "font": {"size": 11}}},
                "y": {
                    "beginAtZero": True,
                    "title": {"display": True, "text": "TSS", "font": {"size": 12}},
                    "ticks": {"font": {"size": 11}},
                    "grid": {"color": "rgba(0,0,0,0.06)"},
                },
            },
        },
    }
    return _fetch(config, height=420)
