#!/usr/bin/env python3
"""Chart generation for ClaudeCoach. Uses QuickChart.io (Chart.js 4) — no extra dependencies."""

import json, ssl, urllib.request

_cafile = "/etc/ssl/cert.pem" if __import__("os").path.exists("/etc/ssl/cert.pem") else None
SSL_CONTEXT = ssl.create_default_context(cafile=_cafile)
QUICKCHART = "https://quickchart.io/chart"

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
    "Other":          "#9b59b6",
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
            "borderColor": "rgba(60,60,60,0.55)",
            "borderWidth": 1.5,
            "borderDash": [4, 3],
            "label": {
                "display": True, "content": "Today",
                "position": "start",
                "backgroundColor": "rgba(255,255,255,0.9)",
                "color": "#333", "font": {"size": 11},
            },
        }
    return None


def _parse_fitness_payload(payload):
    if isinstance(payload, dict):
        return payload.get("data", []), payload.get("today")
    return payload, None


# ── Fitness chart (CTL + ATL) ──────────────────────────────────────────────────

def fitness_chart(payload):
    """
    payload: {"today":"MM-DD","data":[{date,ctl,atl,tsb},...]}  or bare list.
    Renders CTL (teal filled) + ATL (purple dashed) — TSB is a separate form_chart.
    """
    data, today = _parse_fitness_payload(payload)
    labels = [d["date"][5:] for d in data]
    ctl    = [round(d["ctl"], 1) for d in data]
    atl    = [round(d["atl"], 1) for d in data]

    annotations = {}
    ann = _today_annotation(today, labels)
    if ann:
        annotations["today"] = ann

    config = {
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [
                {
                    "label": "Fitness (CTL)",
                    "data": ctl,
                    "borderColor": "#2e9c8e",
                    "backgroundColor": "rgba(46,156,142,0.15)",
                    "borderWidth": 2.5,
                    "pointRadius": 0,
                    "fill": "origin",
                    "tension": 0.3,
                },
                {
                    "label": "Fatigue (ATL)",
                    "data": atl,
                    "borderColor": "#7c4dff",
                    "backgroundColor": "rgba(124,77,255,0.07)",
                    "borderWidth": 2,
                    "borderDash": [6, 3],
                    "pointRadius": 0,
                    "fill": "origin",
                    "tension": 0.3,
                },
            ],
        },
        "options": {
            "plugins": {
                "title": {
                    "display": True,
                    "text": "Fitness (CTL) & Fatigue (ATL)",
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
                    "title": {"display": True, "text": "CTL / ATL", "font": {"size": 12}},
                    "ticks": {"font": {"size": 11}},
                    "suggestedMin": 40,
                    "suggestedMax": 130,
                    "grid": {"color": "rgba(0,0,0,0.06)"},
                },
            },
        },
    }
    return _fetch(config, height=420)


# ── Form chart (TSB with coloured zones) ──────────────────────────────────────

def form_chart(payload):
    """
    Same payload as fitness_chart. Renders TSB as a line with coloured background zones:
      > +5 : teal   — fresh / race-ready
      0 to +5 : light green — optimal
     -20 to 0 : amber  — normal training load
      < -20   : red   — heavy / overreaching risk
    """
    data, today = _parse_fitness_payload(payload)
    labels = [d["date"][5:] for d in data]
    tsb    = [round(d["tsb"], 1) for d in data]

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

    ann = _today_annotation(today, labels)
    if ann:
        annotations["today"] = ann

    config = {
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [{
                "label": "Form (TSB)",
                "data": tsb,
                "borderColor": "rgba(60,60,60,0.75)",
                "backgroundColor": "rgba(60,60,60,0.06)",
                "borderWidth": 2,
                "pointRadius": 0,
                "fill": "origin",
                "tension": 0.3,
            }],
        },
        "options": {
            "plugins": {
                "title": {
                    "display": True,
                    "text": "Form (TSB)",
                    "font": {"size": 14},
                },
                "legend": {"display": False},
                "annotation": {"annotations": annotations},
            },
            "scales": {
                "x": {"ticks": {"maxRotation": 45, "autoSkip": True, "maxTicksLimit": 10, "font": {"size": 11}}},
                "y": {
                    "title": {"display": True, "text": "TSB", "font": {"size": 12}},
                    "ticks": {"font": {"size": 11}},
                    "suggestedMin": -40,
                    "suggestedMax": 20,
                    "grid": {"color": "rgba(0,0,0,0.06)"},
                },
            },
        },
    }
    return _fetch(config, height=320)


# ── Training load chart (TSS stacked by sport + TSB overlay) ─────────────────

def load_chart(payload):
    """
    payload: {"today":"MM-DD","days":[{"date":"YYYY-MM-DD","tsb":-8.7,
              "activities":[{"sport":"Ride","tss":117,"dur":120,"status":"completed"},...]},...]}
    Stacked TSS bars by sport (actual=solid, planned=28% alpha) + TSB line on right axis.
    """
    if isinstance(payload, dict):
        days  = payload.get("days", [])
        today = payload.get("today")
    else:
        return None
    if not days:
        return None

    SPORTS = ["Ride", "Run", "Swim", "Strength"]
    BASE   = {"Ride": "#1d6840", "Run": "#c0392b", "Swim": "#1a5276", "Strength": "#7f8c8d"}

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

    tsb_vals = [round(d.get("tsb") or 0, 1) for d in days]
    datasets.append({
        "type": "line", "label": "TSB (form)",
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
                    "text": "Training load — TSS by sport  ·  TSB form (right axis)",
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
                    "title": {"display": True, "text": "TSS", "font": {"size": 12}},
                    "ticks": {"font": {"size": 11}},
                    "grid": {"color": "rgba(0,0,0,0.06)"},
                },
                "y1": {
                    "position": "right",
                    "title": {"display": True, "text": "TSB", "font": {"size": 12}},
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
