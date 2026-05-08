#!/usr/bin/env python3
"""Chart generation for ClaudeCoach. Uses QuickChart.io — no extra dependencies."""

import json, ssl, urllib.request
from pathlib import Path

_cafile = "/etc/ssl/cert.pem" if __import__("os").path.exists("/etc/ssl/cert.pem") else None
SSL_CONTEXT = ssl.create_default_context(cafile=_cafile)
QUICKCHART = "https://quickchart.io/chart"

# Brand colours matching Diamond Peak site
C_CTL   = "rgb(26, 82, 118)"          # dark blue
C_ATL   = "rgb(192, 57, 43)"          # red
C_TSB_P = "rgba(29, 104, 64, 0.75)"   # green (positive form)
C_TSB_N = "rgba(192, 57, 43, 0.65)"   # red (negative form)

ZONE_COLOURS = {
    "Z1": "#b3d4ff",
    "Z2": "#56a0d3",
    "Z3": "#f5a623",
    "Z4": "#e05c00",
    "Z5": "#c0392b",
    "Recovery": "#a8d5a2",
    "WU/CD": "#d0d0d0",
}


def _fetch(config, width=900, height=420):
    payload = json.dumps({
        "chart": config,
        "width": width,
        "height": height,
        "format": "png",
        "backgroundColor": "white",
        "version": "2",
    }).encode()
    req = urllib.request.Request(
        QUICKCHART, data=payload,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20, context=SSL_CONTEXT) as r:
        return r.read()


def fitness_chart(data):
    """
    data: list of dicts {date, ctl, atl, tsb}
    Returns PNG bytes.
    """
    labels = [d["date"][5:] for d in data]   # MM-DD
    ctl    = [d["ctl"] for d in data]
    atl    = [d["atl"] for d in data]
    tsb    = [d["tsb"] for d in data]
    tsb_colours = [C_TSB_P if v >= 0 else C_TSB_N for v in tsb]

    config = {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": [
                {
                    "type": "line",
                    "label": "CTL (fitness)",
                    "data": ctl,
                    "borderColor": C_CTL,
                    "backgroundColor": "transparent",
                    "borderWidth": 2.5,
                    "pointRadius": 2,
                    "fill": False,
                    "yAxisID": "y1",
                    "order": 1,
                },
                {
                    "type": "line",
                    "label": "ATL (fatigue)",
                    "data": atl,
                    "borderColor": C_ATL,
                    "backgroundColor": "transparent",
                    "borderWidth": 2,
                    "borderDash": [6, 3],
                    "pointRadius": 2,
                    "fill": False,
                    "yAxisID": "y1",
                    "order": 2,
                },
                {
                    "label": "TSB (form)",
                    "data": tsb,
                    "backgroundColor": tsb_colours,
                    "yAxisID": "y2",
                    "order": 3,
                },
            ],
        },
        "options": {
            "title": {"display": True, "text": "Fitness — CTL / ATL / TSB", "fontSize": 14},
            "legend": {"position": "top"},
            "scales": {
                "yAxes": [
                    {
                        "id": "y1",
                        "position": "left",
                        "scaleLabel": {"display": True, "labelString": "CTL / ATL"},
                        "ticks": {"suggestedMin": 40, "suggestedMax": 130},
                    },
                    {
                        "id": "y2",
                        "position": "right",
                        "scaleLabel": {"display": True, "labelString": "TSB"},
                        "gridLines": {"drawOnChartArea": False},
                        "ticks": {"suggestedMin": -50, "suggestedMax": 25},
                    },
                ],
                "xAxes": [
                    {"ticks": {"maxRotation": 45, "autoSkip": True, "maxTicksLimit": 15}}
                ],
            },
        },
    }
    return _fetch(config)


def session_chart(name, intervals, ftp=316):
    """
    intervals: list of {duration_seconds, average_power, type}
    Returns PNG bytes, or None if no intervals.
    """
    if not intervals:
        return None

    datasets = []
    for seg in intervals:
        dur_min = round(seg.get("duration_seconds", 0) / 60, 1)
        if dur_min < 0.5:
            continue
        pwr = seg.get("average_power") or 0
        itype = seg.get("type", "").upper()

        if pwr and ftp:
            pct = pwr / ftp
            if pct < 0.55:
                zone, colour = "Z1", ZONE_COLOURS["Z1"]
            elif pct < 0.75:
                zone, colour = "Z2", ZONE_COLOURS["Z2"]
            elif pct < 0.90:
                zone, colour = "Z3", ZONE_COLOURS["Z3"]
            elif pct < 1.05:
                zone, colour = "Z4", ZONE_COLOURS["Z4"]
            else:
                zone, colour = "Z5+", ZONE_COLOURS["Z5"]
        elif itype == "RECOVERY":
            zone, colour = "Recovery", ZONE_COLOURS["Recovery"]
        else:
            zone, colour = "WU/CD", ZONE_COLOURS["WU/CD"]

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
        "type": "horizontalBar",
        "data": {
            "labels": [f"{name}  ({round(total)} min)"],
            "datasets": datasets,
        },
        "options": {
            "title": {"display": True, "text": "Session structure", "fontSize": 13},
            "legend": {"position": "bottom", "labels": {"boxWidth": 12}},
            "scales": {
                "xAxes": [
                    {
                        "stacked": True,
                        "scaleLabel": {"display": True, "labelString": "Minutes"},
                        "ticks": {"beginAtZero": True},
                    }
                ],
                "yAxes": [{"stacked": True}],
            },
        },
    }
    return _fetch(config, width=900, height=260)
