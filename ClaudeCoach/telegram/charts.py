#!/usr/bin/env python3
"""Chart generation for ClaudeCoach.

fitness / form / recovery / durability / compliance render LOCALLY with matplotlib
(Agg backend → PNG bytes). The remaining charts (session / week / power-curve / load)
still use QuickChart.io (Chart.js 4) via _fetch.
"""

import json, math, ssl, urllib.request
from datetime import date as _date, datetime as _datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D

_cafile = "/etc/ssl/cert.pem" if __import__("os").path.exists("/etc/ssl/cert.pem") else None
SSL_CONTEXT = ssl.create_default_context(cafile=_cafile)
QUICKCHART = "https://quickchart.io/chart"

# ── Local-render house style ───────────────────────────────────────────────────

GRID_COL = "#e8e3da"


def _render(fig):
    """Save a figure to PNG bytes (white bg) and close it."""
    import io
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def _col(c, alpha=None):
    """Parse a colour (hex, rgb(), rgba(), or named) → an (r,g,b,a) tuple in 0..1.

    matplotlib rejects CSS rgb()/rgba() strings, and the phase `color` field plus
    several brand constants are exactly that format, so everything payload- or
    constant-sourced is routed through here. `alpha` overrides any embedded alpha.
    """
    a = 1.0
    if isinstance(c, str) and c.strip().lower().startswith(("rgb(", "rgba(")):
        nums = c[c.index("(") + 1:c.rindex(")")].split(",")
        r = int(float(nums[0])) / 255.0
        g = int(float(nums[1])) / 255.0
        b = int(float(nums[2])) / 255.0
        if len(nums) >= 4:
            a = float(nums[3])
        rgba = (r, g, b, a)
    else:
        import matplotlib.colors as _mc
        rgba = _mc.to_rgba(c)
    if alpha is not None:
        rgba = (rgba[0], rgba[1], rgba[2], alpha)
    return rgba


def _style_ax(ax, twin=False):
    """Apply the shared house style to a primary or twin axis."""
    if twin:
        ax.spines["top"].set_visible(False)          # keep right spine for the twin series
    else:
        ax.spines[["top", "right"]].set_visible(False)
    ax.grid(True, axis="y", color=GRID_COL, linewidth=0.6, alpha=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(labelsize=9)


def _parse_dt(s):
    """YYYY-MM-DD → datetime (date-only)."""
    return _datetime.strptime(s[:10], "%Y-%m-%d")


def _date_axis(ax, dts, max_ticks=9):
    """Format an x-axis of real datetimes: ~8-10 ticks, '%d %b', rotated 30°."""
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=max_ticks, minticks=4))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    if dts:
        ax.set_xlim(dts[0], dts[-1])
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=9)


def _mmdd_to_dt(mmdd, dts, mmdd_labels):
    """Resolve an MM-DD reference to its datetime by index match against the data
    (never reconstruct a year — the 42+14 window can cross a year boundary)."""
    if mmdd in mmdd_labels:
        return dts[mmdd_labels.index(mmdd)]
    return None

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

_CTL_COL = "#2e9c8e"   # teal
_ATL_COL = "#7c4dff"   # purple


def _phase_spans(payload, dts, mmdd_labels):
    """Resolve payload phases → list of (x0_dt, x1_dt, name, color) in datetime space."""
    out = []
    for ph in (payload.get("phases", []) if isinstance(payload, dict) else []):
        d0 = _mmdd_to_dt(ph.get("x0"), dts, mmdd_labels)
        d1 = _mmdd_to_dt(ph.get("x1"), dts, mmdd_labels)
        if d0 is not None and d1 is not None and d1 > d0:
            out.append((d0, d1, ph.get("name", ""), ph.get("color", "rgba(120,120,120,0.06)")))
    return out


def _draw_phase_bands(ax, spans, label_y=None):
    """Shade phase windows and label them just inside the top of the axis."""
    for d0, d1, name, col in spans:
        ax.axvspan(d0, d1, facecolor=_col(col), edgecolor="none", zorder=0)
        if name:
            ax.text(d0 + (d1 - d0) / 2, 0.975, name, transform=ax.get_xaxis_transform(),
                    ha="center", va="top", fontsize=8, color="#8a857c", zorder=1, clip_on=True)


def fitness_chart(payload, coaching_level="mid"):
    """
    payload: {"today":"MM-DD","data":[{date,ctl,atl,tsb,projected?},...]}  or bare list.
    CTL (teal filled area, left axis) + ATL (purple dashed, right twin axis).
    Projected days shaded grey; phase bands; Today line; today dots; ramp box.
    """
    data, today = _parse_fitness_payload(payload)
    if isinstance(payload, dict) and "level" in payload:
        coaching_level = payload["level"]
    L = _lbl(coaching_level)
    if not data:
        return None

    dts    = [_parse_dt(d["date"]) for d in data]
    mmdd   = [d["date"][5:] for d in data]
    ctl    = [round(d["ctl"], 1) for d in data]
    atl    = [round(d["atl"], 1) for d in data]
    ti     = _today_index(data)
    today_dt = _mmdd_to_dt(today, dts, mmdd)

    fig, ax = plt.subplots(figsize=(7.6, 4.2))
    ax1 = ax.twinx()
    _style_ax(ax)
    _style_ax(ax1, twin=True)

    # Projected region shading (today → end).
    if today_dt is not None and dts[-1] > today_dt:
        ax.axvspan(today_dt, dts[-1], facecolor="#9a9a9a", alpha=0.07, zorder=0)

    # Fit each axis to its own data so neither squashes the other (set before fill
    # so the area reaches the final axis floor).
    def _lim(vals):
        lo, hi = min(vals), max(vals)
        pad = max(3, (hi - lo) * 0.12)
        return lo - pad, hi + pad
    ax.set_ylim(*_lim(ctl))
    ax1.set_ylim(*_lim(atl))

    # CTL filled area on left axis.
    ax.plot(dts, ctl, color=_CTL_COL, linewidth=2.5, zorder=4)
    ax.fill_between(dts, ctl, ax.get_ylim()[0], color=_CTL_COL, alpha=0.15, zorder=2)
    # ATL dashed on right twin.
    ax1.plot(dts, atl, color=_ATL_COL, linewidth=2.0, linestyle=(0, (6, 3)), zorder=3)

    # Phase bands.
    _draw_phase_bands(ax, _phase_spans(payload, dts, mmdd))

    # Today line + dots + value labels.
    if today_dt is not None:
        ax.axvline(today_dt, color=(0.12, 0.12, 0.12, 0.85), linewidth=2.0, zorder=5)
    if ti is not None and today_dt is not None:
        ax.scatter([today_dt], [ctl[ti]], s=42, color=_CTL_COL, zorder=6, edgecolors="white", linewidths=1)
        ax1.scatter([today_dt], [atl[ti]], s=42, color=_ATL_COL, zorder=6, edgecolors="white", linewidths=1)
        ax.annotate(f"CTL {ctl[ti]:.0f}", (today_dt, ctl[ti]), textcoords="offset points",
                    xytext=(0, 10), ha="center", fontsize=10, fontweight="bold", color=_CTL_COL,
                    bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.85), zorder=7)
        ax1.annotate(f"ATL {atl[ti]:.0f}", (today_dt, atl[ti]), textcoords="offset points",
                     xytext=(0, 10), ha="center", fontsize=10, fontweight="bold", color=_ATL_COL,
                     bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.85), zorder=7)

    # Ramp readout (trailing-7-day CTL change), top-left.
    if ti is not None and ti - 7 >= 0:
        ramp = round(ctl[ti] - ctl[ti - 7], 1)
        rc = "#1d6840" if ramp <= 5 else ("#c98a1f" if ramp <= 8 else "#c0392b")
        ax.text(0.015, 0.97, f"Ramp {ramp:+.1f} CTL/wk", transform=ax.transAxes,
                ha="left", va="top", fontsize=10.5, fontweight="bold", color=rc,
                bbox=dict(boxstyle="round,pad=0.35", fc="white", ec=rc, alpha=0.9), zorder=8)

    # Axes labels coloured to series.
    ax.set_ylabel(L["ctl"], fontsize=11, color=_CTL_COL)
    ax.tick_params(axis="y", labelcolor=_CTL_COL)
    ax1.set_ylabel(L["atl"], fontsize=11, color=_ATL_COL)
    ax1.tick_params(axis="y", labelcolor=_ATL_COL)
    ax1.spines["right"].set_color(_ATL_COL)
    _date_axis(ax, dts)
    ax.set_title(L["fitness_title"], fontsize=14, fontweight="bold")

    handles = [Line2D([0], [0], color=_CTL_COL, lw=2.5, label=L["ctl"]),
               Line2D([0], [0], color=_ATL_COL, lw=2.0, linestyle=(0, (6, 3)), label=L["atl"])]
    ax.legend(handles=handles, loc="upper right", frameon=False, fontsize=10)
    return _render(fig)


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
    if not data:
        return None

    dts    = [_parse_dt(d["date"]) for d in data]
    mmdd   = [d["date"][5:] for d in data]
    tsb    = [round(d["tsb"], 1) for d in data]
    ti     = _today_index(data)
    today_dt = _mmdd_to_dt(today, dts, mmdd)

    # Fit y to the data but always keep −20 (heavy) and +5 (fresh) refs in view.
    _lo, _hi = min(tsb), max(tsb)
    _pad = max(3, (_hi - _lo) * 0.12)
    ymin = min(_lo - _pad, -22)
    ymax = max(_hi + _pad, 7)

    fig, ax = plt.subplots(figsize=(7.6, 4.2))
    _style_ax(ax)
    ax.set_ylim(ymin, ymax)

    # Coloured zone bands across the full width.
    ax.axhspan(5, ymax,   facecolor=_CTL_COL,  alpha=0.10, zorder=0)   # fresh
    ax.axhspan(0, 5,      facecolor="#78c88c", alpha=0.12, zorder=0)   # ok
    ax.axhspan(-20, 0,    facecolor="#c8a03c", alpha=0.10, zorder=0)   # load
    ax.axhspan(ymin, -20, facecolor="#c0392b", alpha=0.10, zorder=0)   # heavy

    # Projected shading.
    if today_dt is not None and dts[-1] > today_dt:
        ax.axvspan(today_dt, dts[-1], facecolor="#9a9a9a", alpha=0.07, zorder=1)

    # Phase bands.
    _draw_phase_bands(ax, _phase_spans(payload, dts, mmdd))

    # Reference lines (labelled at the right edge).
    ax.axhline(5,   color="#2e9c8e", linewidth=1.0, linestyle=(0, (4, 3)), alpha=0.55, zorder=2)
    ax.axhline(0,   color=(0.4, 0.4, 0.4, 0.45), linewidth=1.0, zorder=2)
    ax.axhline(-20, color="#c0392b", linewidth=1.0, linestyle=(0, (4, 3)), alpha=0.55, zorder=2)
    ax.text(dts[-1], 5,   " +5 fresh",  va="bottom", ha="right", fontsize=8.5, color="#2e9c8e", zorder=3)
    ax.text(dts[-1], -20, " −20 heavy", va="top",    ha="right", fontsize=8.5, color="#c0392b", zorder=3)

    # TSB line (filled to zero).
    ax.plot(dts, tsb, color=(0.24, 0.24, 0.24, 0.85), linewidth=2.0, zorder=4)
    ax.fill_between(dts, tsb, 0, color=(0.24, 0.24, 0.24, 0.08), zorder=3)

    # Today line + dot + label.
    if today_dt is not None:
        ax.axvline(today_dt, color=(0.12, 0.12, 0.12, 0.85), linewidth=2.0, zorder=5)
    if ti is not None and today_dt is not None:
        ax.scatter([today_dt], [tsb[ti]], s=42, color="#3c3c3c", zorder=6, edgecolors="white", linewidths=1)
        _tv = round(tsb[ti])
        _tlabel = "TSB 0" if _tv == 0 else f"TSB {_tv:+d}"
        ax.annotate(_tlabel, (today_dt, tsb[ti]), textcoords="offset points", xytext=(0, 10),
                    ha="center", fontsize=10, fontweight="bold", color="#3c3c3c",
                    bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.85), zorder=7)

    ax.set_ylabel(L["form_yaxis"], fontsize=11)
    _date_axis(ax, dts)
    ax.set_title(L["form_title"], fontsize=14, fontweight="bold")
    return _render(fig)


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

    import numpy as np
    dts    = [_parse_dt(d["date"]) for d in days]
    mmdd   = [d["date"][5:] for d in days]
    hrv    = [d.get("hrv") for d in days]
    rhr    = [d.get("rhr") for d in days]
    sleep  = [d.get("sleep_h") for d in days]
    today_dt = _mmdd_to_dt(today, dts, mmdd)

    HRV_COL = "#2e9c8e"
    RHR_COL = "#96604f"   # muted red

    # 7-day trailing baseline (rolling mean) + band (mean ± 1 trailing SD) over
    # non-None HRV in each window. nan (not None) so fill_between leaves clean gaps.
    baseline, band_lo, band_hi = [], [], []
    for i in range(len(days)):
        window = [hrv[j] for j in range(max(0, i - 6), i + 1) if hrv[j] is not None]
        if not window:
            baseline.append(np.nan); band_lo.append(np.nan); band_hi.append(np.nan)
            continue
        mean = sum(window) / len(window)
        sd = math.sqrt(sum((v - mean) ** 2 for v in window) / (len(window) - 1)) if len(window) >= 3 else 0.0
        sd = max(sd, 0.05 * mean)  # floor so the band stays visible
        baseline.append(mean); band_lo.append(mean - sd); band_hi.append(mean + sd)

    baseline = np.array(baseline); band_lo = np.array(band_lo); band_hi = np.array(band_hi)

    fig, ax = plt.subplots(figsize=(7.6, 4.2))
    ax1 = ax.twinx()    # RHR
    ax3 = ax.twinx()    # sleep (hidden)
    _style_ax(ax)
    _style_ax(ax1, twin=True)

    # Sleep bars first, pinned to the bottom ~quarter via a tall hidden scale.
    smax = max([s for s in sleep if s is not None], default=8) or 8
    sleep_x = [d for d, s in zip(dts, sleep) if s is not None]
    sleep_y = [s for s in sleep if s is not None]
    if sleep_y:
        ax3.bar(sleep_x, sleep_y, width=0.8, color=(0.47, 0.47, 0.47, 0.16),
                zorder=0, edgecolor="none")
    ax3.set_ylim(0, smax * 4)
    ax3.axis("off")

    # HRV ±1SD band + baseline line.
    ax.fill_between(dts, band_lo, band_hi, color=HRV_COL, alpha=0.14, zorder=2)
    ax.plot(dts, baseline, color=HRV_COL, linewidth=2.0, zorder=4)

    # HRV scatter (filter nan pairs).
    hx = [d for d, v in zip(dts, hrv) if v is not None]
    hy = [v for v in hrv if v is not None]
    ax.scatter(hx, hy, s=26, color=HRV_COL, edgecolors="white", linewidths=0.8, zorder=5)

    # RHR line on right twin (nan gaps).
    rhr_np = np.array([r if r is not None else np.nan for r in rhr], dtype=float)
    ax1.plot(dts, rhr_np, color=RHR_COL, linewidth=2.0, linestyle=(0, (5, 4)), zorder=3)

    # HRV axis range fitted to band + dots.
    _vals = [v for v in (hy + list(band_lo[~np.isnan(band_lo)]) + list(band_hi[~np.isnan(band_hi)]))]
    if _vals:
        lo, hi = min(_vals), max(_vals)
        pad = max(2, (hi - lo) * 0.12)
        ax.set_ylim(lo - pad, hi + pad)

    # Today line + labelled values.
    if today_dt is not None:
        ax.axvline(today_dt, color=(0.12, 0.12, 0.12, 0.85), linewidth=2.0, zorder=6)
        ti = mmdd.index(today) if today in mmdd else None
        if ti is not None:
            if hrv[ti] is not None:
                ax.scatter([today_dt], [hrv[ti]], s=70, color=HRV_COL, edgecolors="white", linewidths=1.2, zorder=7)
                ax.annotate(f"HRV {hrv[ti]:.0f}", (today_dt, hrv[ti]), textcoords="offset points",
                            xytext=(0, 11), ha="center", fontsize=9.5, fontweight="bold", color=HRV_COL,
                            bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.85), zorder=8)
            if rhr[ti] is not None:
                ax1.annotate(f"RHR {rhr[ti]:.0f}", (today_dt, rhr[ti]), textcoords="offset points",
                             xytext=(0, -14), ha="center", fontsize=9.5, fontweight="bold", color=RHR_COL,
                             bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.85), zorder=8)

    ax.set_ylabel("HRV (ms)", fontsize=11, color=HRV_COL)
    ax.tick_params(axis="y", labelcolor=HRV_COL)
    ax1.set_ylabel("RHR (bpm)", fontsize=11, color=RHR_COL)
    ax1.tick_params(axis="y", labelcolor=RHR_COL)
    ax1.spines["right"].set_color(RHR_COL)
    _date_axis(ax, dts)
    ax.set_title("Recovery — HRV vs baseline", fontsize=14, fontweight="bold")

    handles = [
        Line2D([0], [0], marker="o", color="none", markerfacecolor=HRV_COL,
               markeredgecolor="white", markersize=7, label="HRV"),
        Line2D([0], [0], color=HRV_COL, lw=2.0, label="Baseline (7d)"),
        mpatches.Patch(facecolor=HRV_COL, alpha=0.14, label="±1 SD"),
        Line2D([0], [0], color=RHR_COL, lw=2.0, linestyle=(0, (5, 4)), label="RHR"),
        mpatches.Patch(facecolor=(0.47, 0.47, 0.47, 0.16), label="Sleep (h)"),
    ]
    ax.legend(handles=handles, loc="upper center", ncol=5, frameon=False,
              fontsize=8.5, bbox_to_anchor=(0.5, 1.0), columnspacing=1.0, handletextpad=0.4)
    return _render(fig)


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

    SPORT_BASE = {"Ride": "#1d6840", "Run": "#c0392b"}

    dts  = [_parse_dt(s["date"]) for s in sessions]
    vals = [round(s.get("decoupling_pct", 0), 1) for s in sessions]

    fig, ax = plt.subplots(figsize=(7.6, 4.2))
    _style_ax(ax)

    # Reference lines.
    ax.axhline(0, color=(0.4, 0.4, 0.4, 0.30), linewidth=1.0, zorder=1)
    ax.axhline(5, color="#1d6840", linewidth=1.5, linestyle=(0, (5, 4)), alpha=0.6, zorder=2)

    present = {}
    for sport, colour in SPORT_BASE.items():
        sx = [d for d, s in zip(dts, sessions) if _norm_sport(s.get("sport")) == sport]
        sy = [v for v, s in zip(vals, sessions) if _norm_sport(s.get("sport")) == sport]
        if sx:
            ax.scatter(sx, sy, s=60, color=colour, edgecolors="white", linewidths=1.2, zorder=5)
            present[sport] = colour

    lo = min(vals + [0]); hi = max(vals + [5])
    pad = max(1.5, (hi - lo) * 0.12)
    ax.set_ylim(lo - pad, hi + pad)

    # "good < 5%" label at the right edge.
    ax.text(dts[-1], 5, " good < 5%", va="bottom", ha="right", fontsize=9.5, color="#1d6840", zorder=3)

    ax.set_ylabel("Decoupling %", fontsize=11)
    _date_axis(ax, dts)
    ax.set_title("Aerobic durability — Pa:HR decoupling (lower = better)",
                 fontsize=13, fontweight="bold")

    handles = [Line2D([0], [0], marker="o", color="none", markerfacecolor=c,
                      markeredgecolor="white", markersize=8, label=sp)
               for sp, c in present.items()]
    if handles:
        ax.legend(handles=handles, loc="upper right", frameon=False, fontsize=10)
    return _render(fig)


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

    import numpy as np
    labels  = [w["label"] for w in weeks]
    planned = [round(w.get("planned", 0), 1) for w in weeks]
    actual  = [round(w.get("actual", 0), 1) for w in weeks]

    C_NEUTRAL = "#969696"
    C_GREEN   = "#1d6840"
    C_AMBER   = "#c9871f"
    C_RED     = "#c0392b"

    def _actual_colour(p, a):
        if p <= 0:
            return C_NEUTRAL
        ratio = a / p
        if ratio >= 0.9:
            return C_GREEN
        if ratio >= 0.7:
            return C_AMBER
        return C_RED

    actual_bg = [_actual_colour(p, a) for p, a in zip(planned, actual)]

    fig, ax = plt.subplots(figsize=(7.6, 4.2))
    _style_ax(ax)

    x = np.arange(len(weeks))
    w = 0.4
    ax.bar(x - w / 2, planned, width=w, color=(0.47, 0.47, 0.47, 0.28), zorder=2, label="Planned")
    ax.bar(x + w / 2, actual,  width=w, color=actual_bg, zorder=3)

    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=9)
    ax.set_ylabel("TSS", fontsize=11)
    ax.set_ylim(0, max(planned + actual + [1]) * 1.12)
    ax.set_title("Compliance — planned vs actual TSS", fontsize=14, fontweight="bold")

    handles = [
        mpatches.Patch(facecolor=(0.47, 0.47, 0.47, 0.28), label="Planned"),
        mpatches.Patch(facecolor=C_GREEN, label="Actual ≥90%"),
        mpatches.Patch(facecolor=C_AMBER, label="70–90%"),
        mpatches.Patch(facecolor=C_RED,   label="<70%"),
    ]
    ax.legend(handles=handles, loc="upper left", frameon=False, fontsize=9, ncol=2)
    return _render(fig)
