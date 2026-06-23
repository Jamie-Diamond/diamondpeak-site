#!/usr/bin/env python3
"""Chart generation for ClaudeCoach.

All charts render LOCALLY with matplotlib (Agg backend → PNG bytes): fitness, form,
recovery, durability, compliance, load, week, session, power-curve. The legacy
QuickChart.io path (_fetch / QUICKCHART / the *_annotation/_box/_rgba helpers) is
retained as cheap insurance but is no longer wired to any chart.
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

# ── Brand palette (from the Diamond Peak site CLAUDE.md) ────────────────────────

BRAND_INK    = "#18160f"   # titles
BRAND_SECOND = "#4a4535"
BRAND_MUTED  = "#9a9080"   # axis labels + ticks
BRAND_HAIR   = "#ddd8cc"   # spines / hairlines
GRID_COL     = "#e8e3da"   # very light grid

# ── Brand font (DM Sans) + global softening ─────────────────────────────────────
# Register the committed TTFs so the VM picks them up too; fall back silently to
# the matplotlib default if anything is missing so rendering can NEVER break.
try:
    import os as _os
    from matplotlib import font_manager as _fm
    _FONT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fonts")
    _registered = False
    for _fn in ("DMSans-Regular.ttf", "DMSans-Medium.ttf", "DMSans-Bold.ttf"):
        _fp = _os.path.join(_FONT_DIR, _fn)
        if _os.path.exists(_fp):
            _fm.fontManager.addfont(_fp)
            _registered = True
    if _registered:
        matplotlib.rcParams["font.family"] = "DM Sans"
except Exception:
    pass  # default font — never break rendering on a font issue

# Tasteful global defaults: lighter base type, muted axis furniture, thin hairline
# spines, no top/right, very light grid, round line caps, antialiasing on.
matplotlib.rcParams.update({
    "font.size":            10.5,
    "axes.titlesize":       12.5,
    "axes.titleweight":     "bold",
    "axes.titlecolor":      BRAND_INK,
    "axes.labelsize":       10.5,
    "axes.labelcolor":      BRAND_MUTED,
    "axes.edgecolor":       BRAND_HAIR,
    "axes.linewidth":       0.8,
    "axes.spines.top":      False,
    "axes.spines.right":    False,
    "xtick.color":          BRAND_MUTED,
    "ytick.color":          BRAND_MUTED,
    "xtick.labelsize":      9,
    "ytick.labelsize":      9,
    "grid.color":           GRID_COL,
    "grid.linewidth":       0.6,
    "grid.alpha":           0.7,
    "lines.solid_capstyle": "round",
    "lines.dash_capstyle":  "round",
    "lines.antialiased":    True,
    "patch.antialiased":    True,
    "axes.unicode_minus":   False,  # use a hyphen-minus the font definitely has
})

# Shape-preserving smoother for trend lines (interpolates every point, no overshoot
# → never hides or fabricates a spike). PCHIP is preferred; if scipy is unavailable
# the callers fall back to plotting the raw (thinner) line.
try:
    from scipy.interpolate import PchipInterpolator as _Pchip
except Exception:
    _Pchip = None

# ── Local-render house style ───────────────────────────────────────────────────


def _render(fig):
    """Save a figure to PNG bytes (white bg) and close it."""
    import io
    buf = io.BytesIO()
    # dpi 400 ≈ 4× — crispest on high-DPI phone screens when tapped to full-screen.
    # Telegram's sendPhoto still recompresses the inline preview, so the gain over 3×
    # shows mainly on zoom; files stay well under Telegram's 10MB photo limit (~0.5-1.3MB).
    fig.savefig(buf, format="png", dpi=400, bbox_inches="tight", facecolor="white")
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
    ax.grid(True, axis="y", color=GRID_COL, linewidth=0.6, alpha=0.7)
    ax.set_axisbelow(True)
    ax.tick_params(labelsize=9, colors=BRAND_MUTED, length=3, width=0.8)


def _smooth_xy(dts, ys, n=320):
    """Gently shape-preserving-smooth a trend line over a datetime x-axis.

    Returns (xs_num, ys_smooth) in matplotlib date-number space so callers can plot
    directly (and fill_between) on the same float axis the date locator uses. PCHIP
    interpolates every real point and never overshoots, so genuine spikes survive
    and none are fabricated. Falls back to the raw points if scipy is missing or
    there are too few points to interpolate.
    """
    import numpy as np
    xnum = mdates.date2num(dts)
    yarr = np.asarray(ys, dtype=float)
    mask = ~np.isnan(yarr)
    if _Pchip is None or mask.sum() < 3:
        return xnum, yarr  # straight (thinner) line — honest fallback
    xv, yv = xnum[mask], yarr[mask]
    # PCHIP needs strictly increasing x; date numbers are already sorted/unique here.
    grid = np.linspace(xv[0], xv[-1], n)
    return grid, _Pchip(xv, yv)(grid)


def _parse_dt(s):
    """YYYY-MM-DD → datetime (date-only)."""
    return _datetime.strptime(s[:10], "%Y-%m-%d")


def _date_axis(ax, dts, max_ticks=9):
    """Format an x-axis of real datetimes: ~8-10 ticks, '%d %b', rotated 30°."""
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=max_ticks, minticks=4))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    if dts:
        ax.set_xlim(dts[0], dts[-1])
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=9, color=BRAND_MUTED)


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

    # CTL filled area on left axis — gently smoothed (shape-preserving), softer fill.
    cx, cy = _smooth_xy(dts, ctl)
    ax.plot(cx, cy, color=_CTL_COL, linewidth=1.8, alpha=0.95, zorder=4)
    ax.fill_between(cx, cy, ax.get_ylim()[0], color=_CTL_COL, alpha=0.12, zorder=2)
    # ATL dashed on right twin — smoothed too.
    ax_x, ax_y = _smooth_xy(dts, atl)
    ax1.plot(ax_x, ax_y, color=_ATL_COL, linewidth=1.4, alpha=0.9,
             linestyle=(0, (6, 3)), zorder=3)

    # Phase bands.
    _draw_phase_bands(ax, _phase_spans(payload, dts, mmdd))

    # Today line + dots + value labels.
    if today_dt is not None:
        ax.axvline(today_dt, color=_col(BRAND_SECOND, 0.55), linewidth=1.3, zorder=5)
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
    ax.set_ylabel(L["ctl"], fontsize=10.5, color=_CTL_COL)
    ax.tick_params(axis="y", labelcolor=_CTL_COL)
    ax1.set_ylabel(L["atl"], fontsize=10.5, color=_ATL_COL)
    ax1.tick_params(axis="y", labelcolor=_ATL_COL)
    ax1.spines["right"].set_color(_ATL_COL)
    _date_axis(ax, dts)
    ax.set_title(L["fitness_title"], fontsize=12.5, fontweight="bold", color=BRAND_INK)

    handles = [Line2D([0], [0], color=_CTL_COL, lw=1.8, label=L["ctl"]),
               Line2D([0], [0], color=_ATL_COL, lw=1.4, linestyle=(0, (6, 3)), label=L["atl"])]
    ax.legend(handles=handles, loc="upper right", frameon=False, fontsize=9.5)
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
    ax.axhline(5,   color="#2e9c8e", linewidth=0.9, linestyle=(0, (4, 3)), alpha=0.5, zorder=2)
    ax.axhline(0,   color=(0.4, 0.4, 0.4, 0.4), linewidth=0.9, zorder=2)
    ax.axhline(-20, color="#c0392b", linewidth=0.9, linestyle=(0, (4, 3)), alpha=0.5, zorder=2)
    ax.text(dts[-1], 5,   " +5 fresh",  va="bottom", ha="right", fontsize=8.5, color="#2e9c8e", zorder=3)
    ax.text(dts[-1], -20, " -20 heavy", va="top",    ha="right", fontsize=8.5, color="#c0392b", zorder=3)

    # TSB line (smoothed, filled to zero) — soft near-ink grey.
    tx, ty = _smooth_xy(dts, tsb)
    ax.plot(tx, ty, color=_col(BRAND_INK, 0.75), linewidth=1.8, zorder=4)
    ax.fill_between(tx, ty, 0, color=_col(BRAND_INK, 0.07), zorder=3)

    # Today line + dot + label.
    if today_dt is not None:
        ax.axvline(today_dt, color=_col(BRAND_SECOND, 0.55), linewidth=1.3, zorder=5)
    if ti is not None and today_dt is not None:
        ax.scatter([today_dt], [tsb[ti]], s=42, color="#3c3c3c", zorder=6, edgecolors="white", linewidths=1)
        _tv = round(tsb[ti])
        _tlabel = "TSB 0" if _tv == 0 else f"TSB {_tv:+d}"
        ax.annotate(_tlabel, (today_dt, tsb[ti]), textcoords="offset points", xytext=(0, 10),
                    ha="center", fontsize=10, fontweight="bold", color="#3c3c3c",
                    bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.85), zorder=7)

    ax.set_ylabel(L["form_yaxis"], fontsize=10.5, color=BRAND_MUTED)
    _date_axis(ax, dts)
    ax.set_title(L["form_title"], fontsize=12.5, fontweight="bold", color=BRAND_INK)
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
    Stacked TSS bars by sport (actual solid, planned faded) + TSB line on a right axis
    with fresh/load/heavy zone bands. Local matplotlib render.
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

    import numpy as np
    SPORTS = ["Ride", "Run", "Swim", "Strength", "Other"]
    BASE   = {"Ride": "#1d6840", "Run": "#c0392b", "Swim": "#1a5276",
              "Strength": "#7f8c8d", "Other": "#b0aaa0"}

    mmdd = [d["date"][5:] for d in days]
    n    = len(days)
    x    = np.arange(n)

    fig, ax = plt.subplots(figsize=(7.8, 4.4))
    ax1 = ax.twinx()
    _style_ax(ax)
    _style_ax(ax1, twin=True)

    # Stacked TSS bars by sport; planned segments faded, actual solid.
    bottom  = np.zeros(n)
    present = []
    for sport in SPORTS:
        tss_vals, colours, has_data = [], [], False
        for d in days:
            sport_tss, is_planned = 0.0, False
            for a in d.get("activities", []):
                if _norm_sport(a.get("sport", "")) == sport:
                    sport_tss += a.get("tss") or 0
                    if a.get("status") == "planned":
                        is_planned = True
            tss_vals.append(round(sport_tss, 1))
            if sport_tss > 0:
                has_data = True
            alpha = (_PLANNED_ALPHA if is_planned else 0.87) if sport_tss > 0 else 0
            colours.append(_col(BASE[sport], alpha))
        if not has_data:
            continue
        ax.bar(x, tss_vals, bottom=bottom, width=0.8, color=colours, zorder=2)
        bottom += np.array(tss_vals)
        present.append(sport)

    # TSB line on the right twin, with coloured zone bands behind it.
    if seed_ctl is not None and seed_atl is not None:
        tsb_vals = _project_tsb(days, seed_ctl, seed_atl)
    else:
        tsb_vals = [round(d.get("tsb") or 0, 1) for d in days]

    tlo = min(tsb_vals + [-25]); thi = max(tsb_vals + [10])
    tpad = max(3, (thi - tlo) * 0.10)
    ax1.set_ylim(tlo - tpad, thi + tpad)
    y1lo, y1hi = ax1.get_ylim()
    ax1.axhspan(5, y1hi,    facecolor=_CTL_COL,  alpha=0.09, zorder=0)   # fresh
    ax1.axhspan(0, 5,       facecolor="#78c88c", alpha=0.10, zorder=0)   # ok
    ax1.axhspan(-20, 0,     facecolor="#c8a03c", alpha=0.08, zorder=0)   # load
    ax1.axhspan(y1lo, -20,  facecolor="#c0392b", alpha=0.09, zorder=0)   # heavy
    ax1.axhline(0, color=(0.4, 0.4, 0.4, 0.3), linewidth=0.8,
                linestyle=(0, (4, 3)), zorder=1)

    def _tsb_dot(v):
        if v > 5:    return "#2e9c8e"   # fresh
        if v >= -20: return "#c9871f"   # load
        return "#c0392b"                # heavy

    ax1.plot(x, tsb_vals, color=_col(BRAND_SECOND, 0.7), linewidth=1.4, zorder=4)
    ax1.scatter(x, tsb_vals, s=34, color=[_tsb_dot(v) for v in tsb_vals],
                edgecolors="white", linewidths=1.0, zorder=5)

    # Today line.
    if today in mmdd:
        ax.axvline(mmdd.index(today), color=_col(BRAND_SECOND, 0.55), linewidth=1.3, zorder=6)

    # Sparse, rotated date ticks (bars are categorical → tick by index).
    step = max(1, n // 12)
    ticks = list(range(0, n, step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([days[i]["date"][5:] for i in ticks], rotation=45, ha="right",
                       fontsize=8.5, color=BRAND_MUTED)
    ax.set_xlim(-0.6, n - 0.4)

    ax.set_ylabel(L["load_tss"], fontsize=10.5, color=BRAND_MUTED)
    ax.set_ylim(0, max(bottom.max(), 1) * 1.12)
    ax1.set_ylabel(L["load_tsb"], fontsize=10.5, color=BRAND_SECOND)
    ax1.tick_params(axis="y", labelcolor=BRAND_SECOND)
    ax.set_title(f"Training load — {L['load_tss']} by sport  ·  {L['load_tsb']} (right)",
                 fontsize=12, fontweight="bold", color=BRAND_INK)

    handles = [mpatches.Patch(facecolor=BASE[s], label=s) for s in present]
    handles.append(Line2D([0], [0], color=_col(BRAND_SECOND, 0.7), lw=1.4,
                          marker="o", markerfacecolor="#c9871f", markeredgecolor="white",
                          markersize=7, label=L["load_tsb"]))
    ax.legend(handles=handles, loc="upper left", frameon=False, fontsize=8.5,
              ncol=min(len(handles), 4), columnspacing=1.0, handletextpad=0.4)
    return _render(fig)


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

    import numpy as np
    x = np.arange(7)

    # Drop sports with no actual TSS this week, keeping the sport order.
    active = [s for s in sports if any(actual.get(s, [0]*7))]
    if not active and not any(planned_tss):
        return None

    fig, ax = plt.subplots(figsize=(7.6, 4.4))
    _style_ax(ax)

    # Weekend shading (Sat/Sun = index 5,6) in categorical index space.
    ax.axvspan(4.5, 6.5, facecolor=(0, 0, 0, 0.035), zorder=0)

    # Actual TSS as stacked bars by sport.
    bottom = np.zeros(7)
    for sport in active:
        data   = np.array([round(v, 1) for v in actual.get(sport, [0]*7)])
        colour = SPORT_COLOURS.get(sport, SPORT_COLOURS["Other"])
        label  = "Strength" if sport == "WeightTraining" else sport
        ax.bar(x, data, bottom=bottom, width=0.7, color=_col(colour, 0.87),
               zorder=2, label=label)
        bottom += data

    # Planned TSS as a dashed line with markers (None days leave gaps).
    if any(planned_tss):
        py = np.array([v if v else np.nan for v in planned_tss], dtype=float)
        ax.plot(x, py, color=_col(BRAND_SECOND, 0.7), linewidth=1.6,
                linestyle=(0, (5, 4)), zorder=4)
        ax.scatter(x, py, s=42, color=_col(BRAND_SECOND, 0.6),
                   edgecolors="white", linewidths=1.0, zorder=5, label="Planned TSS")

    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=11, color=BRAND_MUTED)
    ax.set_xlim(-0.6, 6.6)
    ax.set_ylabel("TSS", fontsize=10.5, color=BRAND_MUTED)
    ax.set_ylim(0, max(float(bottom.max()), max(planned_tss + [0]), 1) * 1.12)
    ax.set_title(title, fontsize=12.5, fontweight="bold", color=BRAND_INK)
    ax.legend(loc="upper right", frameon=False, fontsize=9.5, ncol=2,
              columnspacing=1.2, handletextpad=0.5)
    return _render(fig)


# ── Session structure ─────────────────────────────────────────────────────────

def session_chart(name, intervals, ftp=316):
    """intervals: [{duration_seconds, average_power, type}]
    Horizontal stacked bar of segments coloured by power zone. Local matplotlib render."""
    if not intervals:
        return None

    segs = []
    for seg in intervals:
        dur_min = round(seg.get("duration_seconds", 0) / 60, 1)
        if dur_min < 0.5:
            continue
        pwr   = seg.get("average_power") or 0
        itype = seg.get("type", "").upper()

        if pwr and ftp:
            pct = pwr / ftp
            if pct < 0.55:
                zone = "Z1"
            elif pct < 0.75:
                zone = "Z2"
            elif pct < 0.90:
                zone = "Z3"
            elif pct < 1.05:
                zone = "Z4"
            else:
                zone = "Z5+"
        elif itype == "RECOVERY":
            zone = "Recovery"
        else:
            zone = "WU/CD"
        segs.append((zone, dur_min, ZONE_COLOURS[zone]))

    if not segs:
        return None

    total = sum(d for _, d, _ in segs)
    fig, ax = plt.subplots(figsize=(7.6, 2.6))
    _style_ax(ax)
    ax.grid(False)

    left = 0.0
    seen = set()
    for zone, dur, colour in segs:
        ax.barh(0, dur, left=left, height=0.6, color=_col(colour, 0.92),
                edgecolor="white", linewidth=0.8, zorder=2,
                label=zone if zone not in seen else None)
        seen.add(zone)
        left += dur

    ax.set_yticks([])
    ax.set_ylim(-0.5, 0.5)
    ax.set_xlim(0, total)
    ax.set_xlabel("Minutes", fontsize=10.5, color=BRAND_MUTED)
    ax.spines["left"].set_visible(False)
    ax.set_title(f"Session structure — {name}  ({round(total)} min)",
                 fontsize=12, fontweight="bold", color=BRAND_INK)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.32), ncol=min(len(seen), 7),
              frameon=False, fontsize=9, columnspacing=1.0, handletextpad=0.4)
    return _render(fig)


# ── Power curve ───────────────────────────────────────────────────────────────

def power_curve_chart(efforts, ftp=316):
    """
    efforts: [{"label":"5s","power":980}, {"label":"1m","power":520}, ...]
    Standard duration labels: 5s 15s 30s 1m 2m 5m 10m 20m 30m 60m 90m
    """
    if not efforts:
        return None

    import numpy as np
    labels = [e["label"] for e in efforts]
    powers = [e["power"] for e in efforts]
    max_p  = max(powers) if powers else ftp * 2
    x      = np.arange(len(labels))

    BLUE = _col(C_CTL)
    fig, ax = plt.subplots(figsize=(7.6, 4.6))
    _style_ax(ax)

    ymax = max_p * 1.08
    # Power-zone bands behind the curve.
    ax.axhspan(0,          ftp * 0.55, facecolor="#c8c8c8", alpha=0.10, zorder=0)
    ax.axhspan(ftp * 0.55, ftp * 0.75, facecolor="#56a0d3", alpha=0.10, zorder=0)
    ax.axhspan(ftp * 0.75, ftp * 0.90, facecolor="#f5a623", alpha=0.11, zorder=0)
    ax.axhspan(ftp * 0.90, ftp * 1.05, facecolor="#e05c00", alpha=0.11, zorder=0)
    ax.axhspan(ftp * 1.05, ymax,       facecolor="#c0392b", alpha=0.10, zorder=0)

    # Smoothed best-power curve filled to the floor.
    if _Pchip is not None and len(x) >= 3:
        grid = np.linspace(x[0], x[-1], 320)
        gy   = _Pchip(x, np.asarray(powers, dtype=float))(grid)
        ax.plot(grid, gy, color=BLUE, linewidth=1.8, alpha=0.95, zorder=4)
        ax.fill_between(grid, gy, 0, color=BLUE, alpha=0.12, zorder=2)
    else:
        ax.plot(x, powers, color=BLUE, linewidth=1.8, alpha=0.95, zorder=4)
        ax.fill_between(x, powers, 0, color=BLUE, alpha=0.12, zorder=2)
    ax.scatter(x, powers, s=34, color=BLUE, edgecolors="white", linewidths=1.0, zorder=5)

    # FTP reference line + label.
    ax.axhline(ftp, color=_col(C_CTL, 0.55), linewidth=1.2, linestyle=(0, (5, 4)), zorder=3)
    ax.text(x[-1], ftp, f" FTP {ftp}W", va="bottom", ha="right", fontsize=9.5,
            color=_col(C_CTL), zorder=6,
            bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.8))

    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=9.5, color=BRAND_MUTED)
    ax.set_xlim(-0.3, len(labels) - 0.7)
    ax.set_ylim(min(powers) * 0.9 if powers else 0, ymax)
    ax.set_xlabel("Duration", fontsize=10.5, color=BRAND_MUTED)
    ax.set_ylabel("Watts", fontsize=10.5, color=BRAND_MUTED)
    ax.set_title("Power curve — best efforts", fontsize=12.5, fontweight="bold", color=BRAND_INK)
    return _render(fig)


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

    # Two stacked panels sharing the date axis: HRV+RHR (tall) over Sleep (short).
    fig, (ax, axs) = plt.subplots(
        2, 1, figsize=(7.6, 5.0), sharex=True,
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.12})
    ax1 = ax.twinx()    # RHR (inverted so lower RHR sits high = "better up")
    _style_ax(ax)
    _style_ax(ax1, twin=True)
    _style_ax(axs)

    # HRV ±1SD band + baseline line. The band stays on the raw points (it's a
    # statistical envelope, not a trend); only the baseline LINE is smoothed.
    ax.fill_between(dts, band_lo, band_hi, color=HRV_COL, alpha=0.12, zorder=2)
    bx, by = _smooth_xy(dts, baseline)
    ax.plot(bx, by, color=HRV_COL, linewidth=1.8, alpha=0.95, zorder=4)

    # HRV scatter (filter nan pairs) — left as honest dots, not smoothed.
    hx = [d for d, v in zip(dts, hrv) if v is not None]
    hy = [v for v in hrv if v is not None]
    ax.scatter(hx, hy, s=26, color=HRV_COL, edgecolors="white", linewidths=0.8,
               alpha=0.9, zorder=5)

    # RHR line on right twin (nan gaps) — left straight (not in the smooth set), thinner.
    rhr_np = np.array([r if r is not None else np.nan for r in rhr], dtype=float)
    ax1.plot(dts, rhr_np, color=RHR_COL, linewidth=1.4, alpha=0.9,
             linestyle=(0, (5, 4)), zorder=3)

    # HRV axis range fitted to band + dots.
    _vals = [v for v in (hy + list(band_lo[~np.isnan(band_lo)]) + list(band_hi[~np.isnan(band_hi)]))]
    if _vals:
        lo, hi = min(_vals), max(_vals)
        pad = max(2, (hi - lo) * 0.12)
        ax.set_ylim(lo - pad, hi + pad)

    # RHR axis: fit, THEN invert (order matters — inverting after set_ylim un-flips).
    _rvals = [r for r in rhr if r is not None]
    if _rvals:
        rlo, rhi = min(_rvals), max(_rvals)
        rpad = max(1.5, (rhi - rlo) * 0.15)
        ax1.set_ylim(rlo - rpad, rhi + rpad)
    ax1.invert_yaxis()   # lower RHR (better) now at the top, aligning with high HRV

    # Sleep bars on the lower panel, cropped to a relevant range so night-to-night
    # variation is legible (bars zoom into ~min-1h .. max+0.5h, not 0..max).
    sleep_x = [d for d, s in zip(dts, sleep) if s is not None]
    sleep_y = [s for s in sleep if s is not None]
    if sleep_y:
        axs.bar(sleep_x, sleep_y, width=0.85, color=_col("#6b6256", 0.45),
                edgecolor="none", zorder=2)
        slo = max(0, math.floor(min(sleep_y)) - 1)
        shi = math.ceil(max(sleep_y)) + 0.5
        # Faint 8h reference if it's in view.
        if slo < 8 < shi:
            axs.axhline(8, color=_col("#1d6840", 0.45), linewidth=0.9,
                        linestyle=(0, (4, 3)), zorder=1)
    else:
        slo, shi = 0, 9
    axs.set_ylim(slo, shi)
    axs.set_ylabel("Sleep (h)", fontsize=9.5, color=BRAND_MUTED)

    # Today line + labelled values (drawn on BOTH panels so they line up).
    if today_dt is not None:
        ax.axvline(today_dt,  color=_col(BRAND_SECOND, 0.55), linewidth=1.3, zorder=6)
        axs.axvline(today_dt, color=_col(BRAND_SECOND, 0.55), linewidth=1.3, zorder=6)
        ti = mmdd.index(today) if today in mmdd else None
        if ti is not None:
            if hrv[ti] is not None:
                ax.scatter([today_dt], [hrv[ti]], s=70, color=HRV_COL, edgecolors="white", linewidths=1.2, zorder=7)
                ax.annotate(f"HRV {hrv[ti]:.0f}", (today_dt, hrv[ti]), textcoords="offset points",
                            xytext=(0, 11), ha="center", fontsize=9.5, fontweight="bold", color=HRV_COL,
                            bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.85), zorder=8)
            if rhr[ti] is not None:
                ax1.scatter([today_dt], [rhr[ti]], s=60, color=RHR_COL, edgecolors="white", linewidths=1.2, zorder=7)
                ax1.annotate(f"RHR {rhr[ti]:.0f}", (today_dt, rhr[ti]), textcoords="offset points",
                             xytext=(0, 12), ha="center", fontsize=9.5, fontweight="bold", color=RHR_COL,
                             bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.85), zorder=8)

    ax.set_ylabel("HRV (ms) — higher better", fontsize=10, color=HRV_COL)
    ax.tick_params(axis="y", labelcolor=HRV_COL)
    ax1.set_ylabel("RHR (bpm) — lower better", fontsize=10, color=RHR_COL)
    ax1.tick_params(axis="y", labelcolor=RHR_COL)
    ax1.spines["right"].set_color(RHR_COL)
    _date_axis(axs, dts)              # date ticks only on the bottom panel
    plt.setp(ax.get_xticklabels(), visible=False)
    ax.set_title("Recovery — HRV & resting HR (both: up = better)",
                 fontsize=12.5, fontweight="bold", color=BRAND_INK)

    handles = [
        Line2D([0], [0], marker="o", color="none", markerfacecolor=HRV_COL,
               markeredgecolor="white", markersize=7, label="HRV"),
        Line2D([0], [0], color=HRV_COL, lw=1.8, label="Baseline (7d)"),
        mpatches.Patch(facecolor=HRV_COL, alpha=0.12, label="±1 SD"),
        Line2D([0], [0], color=RHR_COL, lw=1.4, linestyle=(0, (5, 4)), label="RHR (inv)"),
    ]
    ax.legend(handles=handles, loc="upper center", ncol=4, frameon=False,
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
    ax.axhline(0, color=(0.4, 0.4, 0.4, 0.28), linewidth=0.9, zorder=1)
    ax.axhline(5, color="#1d6840", linewidth=1.1, linestyle=(0, (5, 4)), alpha=0.55, zorder=2)

    present = {}
    for sport, colour in SPORT_BASE.items():
        sx = [d for d, s in zip(dts, sessions) if _norm_sport(s.get("sport")) == sport]
        sy = [v for v, s in zip(vals, sessions) if _norm_sport(s.get("sport")) == sport]
        if sx:
            ax.scatter(sx, sy, s=58, color=colour, edgecolors="white", linewidths=1.2,
                       alpha=0.9, zorder=5)
            present[sport] = colour

    lo = min(vals + [0]); hi = max(vals + [5])
    pad = max(1.5, (hi - lo) * 0.12)
    ax.set_ylim(lo - pad, hi + pad)

    # "good < 5%" label at the right edge.
    ax.text(dts[-1], 5, " good < 5%", va="bottom", ha="right", fontsize=9.5, color="#1d6840", zorder=3)

    ax.set_ylabel("Decoupling %", fontsize=10.5, color=BRAND_MUTED)
    _date_axis(ax, dts)
    ax.set_title("Aerobic durability — Pa:HR decoupling (lower = better)",
                 fontsize=12, fontweight="bold", color=BRAND_INK)

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
    ax.bar(x - w / 2, planned, width=w, color=(0.47, 0.47, 0.47, 0.26), zorder=2, label="Planned")
    ax.bar(x + w / 2, actual,  width=w, color=[_col(c, 0.9) for c in actual_bg], zorder=3)

    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=9, color=BRAND_MUTED)
    ax.set_ylabel("TSS", fontsize=10.5, color=BRAND_MUTED)
    ax.set_ylim(0, max(planned + actual + [1]) * 1.12)
    ax.set_title("Compliance — planned vs actual TSS", fontsize=12.5, fontweight="bold", color=BRAND_INK)

    handles = [
        mpatches.Patch(facecolor=(0.47, 0.47, 0.47, 0.28), label="Planned"),
        mpatches.Patch(facecolor=C_GREEN, label="Actual ≥90%"),
        mpatches.Patch(facecolor=C_AMBER, label="70–90%"),
        mpatches.Patch(facecolor=C_RED,   label="<70%"),
    ]
    ax.legend(handles=handles, loc="upper left", frameon=False, fontsize=9, ncol=2)
    return _render(fig)
