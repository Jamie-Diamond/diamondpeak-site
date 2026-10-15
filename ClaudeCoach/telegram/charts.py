#!/usr/bin/env python3
"""Chart generation for ClaudeCoach.

Trimmed to load_chart only (Jamie, 27 Aug 2026): the coach webapp now covers
fitness/form/duration/recovery/durability/compliance/power-curve/week/session as
interactive pages, so the telegram-side chart renderers for those were removed —
load_chart survives because the morning briefing sends it directly. Renders
LOCALLY with matplotlib (Agg backend → PNG bytes). The legacy QuickChart.io path
(_fetch / QUICKCHART / the *_annotation/_box/_rgba helpers) is retained as cheap
insurance but is no longer wired to any chart.
"""

import json, logging, math, ssl, urllib.request
from datetime import date as _date, datetime as _datetime, timedelta as _timedelta

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
    # Peak's font (Plus Jakarta Sans, OFL; variable TTF so weights come from the default
    # instance) for charts drawn to match the app (Jamie, 15 Oct 2026).
    _jp = _os.path.join(_FONT_DIR, "PlusJakartaSans-Variable.ttf")
    if _os.path.exists(_jp):
        _fm.fontManager.addfont(_jp)
        _JAKARTA = True
except Exception:
    pass  # default font — never break rendering on a font issue
try:
    _JAKARTA
except NameError:
    _JAKARTA = False
try:
    pass
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


# ── Form chart (TSB with coloured zones) ──────────────────────────────────────


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
    The morning card's "seven days either side" chart, drawn to match Peak (Jamie, 15 Oct
    2026: "ugly by our modern standards"): Peak's sport colours and font, rounded stacked
    bars (planned faded), Form as a smooth line with its latest value tagged, Fatigue
    dashed, plain-English labels, no zone bands, today marked on the date axis.
    """
    if not isinstance(payload, dict):
        return None
    days, today = payload.get("days", []), payload.get("today")
    seed_ctl, seed_atl = payload.get("seed_ctl"), payload.get("seed_atl")
    if not days:
        return None

    import numpy as np
    from matplotlib.patches import FancyBboxPatch
    INK, MUTED, HAIR, ACCENT = "#0b0d10", "#8a8f98", "#e6e9ee", "#10656b"
    SPORTS = ["Ride", "Run", "Swim", "Strength", "Other"]
    BASE = {"Ride": "#10656b", "Run": "#ba3c32", "Swim": "#1d4e73",
            "Strength": "#78848e", "Other": "#a6acb4"}
    fam = "Plus Jakarta Sans" if _JAKARTA else matplotlib.rcParams["font.family"]

    n = len(days)
    x = np.arange(n)
    fig, ax = plt.subplots(figsize=(7.8, 4.6))
    fig.patch.set_facecolor("white")
    ax1 = ax.twinx()
    for a in (ax, ax1):
        for sp in a.spines.values():
            sp.set_visible(False)
        a.tick_params(length=0, labelsize=9.5, colors=MUTED)
    ax.grid(True, axis="y", color=HAIR, linewidth=0.8)
    ax.set_axisbelow(True)

    # Stacked bars by sport with rounded corners; planned faded.
    W = 0.62
    bottom = np.zeros(n)
    present = []
    tops = np.zeros(n)
    for d_i, d in enumerate(days):
        tops[d_i] = sum((a.get("tss") or 0) for a in d.get("activities", []))
    ymax = max(tops.max(), 1) * 1.18
    ax.set_ylim(0, ymax)
    ax.set_xlim(-0.6, n - 0.4)
    # rounding in display-consistent units: x is in bar-widths, y in load points
    aspect = ymax / (n * 0.55)
    for sport in SPORTS:
        any_ = False
        for d_i, d in enumerate(days):
            tss, planned = 0.0, False
            for a in d.get("activities", []):
                if _norm_sport(a.get("sport", "")) == sport:
                    tss += a.get("tss") or 0
                    planned = planned or a.get("status") == "planned"
            if tss <= 0:
                continue
            any_ = True
            ax.add_patch(FancyBboxPatch(
                (x[d_i] - W / 2, bottom[d_i]), W, tss,
                boxstyle="round,pad=0,rounding_size=0.13", mutation_aspect=aspect,
                facecolor=BASE[sport], alpha=0.32 if planned else 0.92,
                edgecolor="white", linewidth=1.2, zorder=2))
            bottom[d_i] += tss
        if any_:
            present.append(sport)

    # Fatigue: trailing 7-day mean of daily load, dashed.
    roll = [float(np.mean(bottom[max(0, i - 6):i + 1])) for i in range(n)]
    ax.plot(x, roll, color="#6b7380", linewidth=1.8, linestyle=(0, (4, 3)), zorder=3)

    # Form on the right axis: a line with soft dots and its latest value tagged.
    tsb = (_project_tsb(days, seed_ctl, seed_atl) if seed_ctl is not None and seed_atl is not None
           else [round(d.get("tsb") or 0, 1) for d in days])
    lo, hi = min(tsb + [-10]), max(tsb + [10])
    pad = max(3, (hi - lo) * 0.14)
    ax1.set_ylim(lo - pad, hi + pad)
    ax1.plot(x, tsb, color=ACCENT, linewidth=2.4, zorder=4, solid_joinstyle="round")
    ax1.scatter(x, tsb, s=22, color=ACCENT, edgecolors="white", linewidths=1.2, zorder=5)
    ax1.annotate(f"{tsb[-1]:+.0f}", (x[-1], tsb[-1]), xytext=(0, 12), textcoords="offset points",
                 ha="center", fontsize=10, fontweight="bold", color="white", family=fam,
                 bbox=dict(boxstyle="round,pad=0.35,rounding_size=0.6", fc=ACCENT, ec="none"),
                 zorder=7)

    # Dates as "Thu 15"; today bold in the accent with a faint column behind it.
    labels = []
    t_idx = None
    for i, d in enumerate(days):
        dt = _date.fromisoformat(d["date"][:10])
        labels.append(f"{dt.strftime('%a')[:1]}\n{dt.day}")
        if d["date"][5:10] == today:
            t_idx = i
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=9, color=MUTED, family=fam, linespacing=1.3)
    if t_idx is not None:
        ax.axvspan(t_idx - 0.5, t_idx + 0.5, color=ACCENT, alpha=0.06, zorder=0)
        tl = ax.get_xticklabels()[t_idx]
        tl.set_color(ACCENT); tl.set_fontweight("bold")

    ax.set_ylabel("Daily load", fontsize=10, color=MUTED, family=fam, labelpad=8)
    ax1.set_ylabel("Form", fontsize=10, color=ACCENT, family=fam, labelpad=8)
    ax1.tick_params(axis="y", colors=ACCENT)
    fig.text(0.02, 0.985, "Seven days either side", fontsize=13.5, fontweight="bold",
             color=INK, family=fam, va="top")

    handles = [mpatches.Patch(facecolor=BASE[s], label=s) for s in present]
    handles += [Line2D([0], [0], color=ACCENT, lw=2.4, marker="o", markersize=5,
                       markeredgecolor="white", label="Form"),
                Line2D([0], [0], color="#6b7380", lw=1.8, linestyle=(0, (4, 3)),
                       label="Fatigue (7-day load)"),
                mpatches.Patch(facecolor="#a6acb4", alpha=0.32, label="Planned")]
    leg = fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, -0.02),
                     ncol=len(handles), frameon=False, fontsize=9, handlelength=1.2,
                     handletextpad=0.5, columnspacing=1.1, prop={"family": fam, "size": 9})
    for t in leg.get_texts():
        t.set_color("#3a3f47")
    fig.subplots_adjust(left=0.08, right=0.92, top=0.88, bottom=0.2)
    return _render(fig)


# ── Heat acclimation ───────────────────────────────────────────────────────────


# ── Week calendar ─────────────────────────────────────────────────────────────


# ── Session structure ─────────────────────────────────────────────────────────


# ── Power curve ───────────────────────────────────────────────────────────────


# ── Recovery chart (HRV vs own baseline + RHR + sleep) ─────────────────────────


# ── Durability chart (Pa:HR decoupling) ────────────────────────────────────────


# ── Compliance chart (planned vs actual TSS) ───────────────────────────────────


# ── Duration chart (CTL-style rolling hours/week, with season overlay) ────────


_DUR_COL      = "#2e9c8e"   # teal — same series colour as CTL in fitness_chart
_DUR_PREV_COL = "#9a9080"   # muted grey — "Last season"
_DUR_PREV2_COL = "#1a5276"  # blue — the season before that


