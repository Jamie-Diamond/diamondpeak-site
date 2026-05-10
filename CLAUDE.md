# Diamond Peak Consulting — Agent Reference

Static site hosted on GitHub Pages. All pages are self-contained HTML with inline CSS/JS. No build step, no framework.

## Repository

- **Remote**: `https://github.com/Jamie-Diamond/diamondpeak-site.git`
- **Branch**: `main` (deploy branch, local may show `master`)
- **Hosting**: GitHub Pages

## Site Structure

```
diamondpeak-site/
├── index.html                  # Homepage — hero with topo canvas, tool cards by section
├── style.css                   # Shared base CSS (variables, reset, typography, panels, fields, footer)
│
├── cycling/                    # Performance / cycling & triathlon tools
│   ├── ftp-calculator.html
│   ├── fuelling-calculator.html
│   ├── gear-ratio-calculator.html
│   ├── pacing-strategy-builder.html
│   ├── power-to-weight.html
│   ├── run-pace-converter.html
│   ├── sweat-rate-calculator.html
│   ├── triathlon-race-predictor.html
│   ├── tss-calculator.html
│   ├── vo2max-estimator.html
│   └── cervia-wetsuit.html
│
├── property/                   # Property & finance tools
│   ├── stamp-duty.html
│   ├── student-loan.html
│   ├── should-you-wait.html
│   └── recovery-analysis.html
│
└── cda-calculator/             # CdA Calculator (Vite/React app, separate build)
    ├── src/
    └── assets/
```

## Branding & Styling

- **Heading font**: Libre Baskerville (serif, Google Fonts)
- **Body font**: DM Sans (weight 300, Google Fonts)
- **Mono/labels**: DM Mono (Google Fonts)
- **Colours**:
  - Background: `#f8f5ef` (warm paper)
  - Ink: `#18160f`
  - Secondary: `#4a4535`
  - Muted: `#9a9080`
  - Border: `#ddd8cc`
  - Green accent: `#1d6840`
  - Blue accent: `#1a5276`
- **Hero**: dark background `#06080a` with animated topo canvas

## Shared CSS (`style.css`)

All 16 pages link to `style.css` which provides:
- `:root` CSS variables (colours above)
- Universal reset and body typography
- `.page` container (max-width `820px`, pages override if needed)
- `.back` link, `.kicker` eyebrow, `h1`, `.intro` paragraph
- `.panel` / `.panel-title` card styles
- `.field` / `.field label` / `.field input` form styles
- `.footer`

Pages keep tool-specific styles inline in `<style>` and override shared defaults as needed (e.g. `.page { max-width: 720px; }` on property pages).

### Class naming

Some pages use prefixed class names to avoid collisions with shared styles:
- `ftp-calculator`, `power-to-weight`: `.gc-` prefix (`.gc-panel`, `.gc-field`)
- `gear-ratio-calculator`: `.gr-` prefix
- `pacing-strategy-builder`: `.ps-` prefix
- `tss-calculator`: `.tss-` prefix
- `vo2max-estimator`: `.v-` prefix
- `triathlon-race-predictor`: `.tp-` prefix

Pages using `.panel`, `.field` etc. directly inherit from `style.css`.

## Page Template (sub-pages)

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tool Name — Diamond Peak</title>
<link href="https://fonts.googleapis.com/css2?family=Libre+Baskerville:ital,wght@0,400;0,700;1,400&family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="../style.css">
<style>
  /* Page-specific styles and overrides */
</style>
</head>
<body>
<div class="page">
  <a href="../index.html" class="back">&larr; Back to tools</a>
  <div class="kicker">Category</div>
  <h1>Tool Name</h1>
  <p class="intro">Description.</p>
  <!-- Tool content -->
  <div class="footer">&copy; 2025 Diamond Peak Consulting</div>
</div>
</body>
</html>
```

## Index Page Sections

The homepage (`index.html`) organises tools into sections:
1. **FMCG** — factory/consulting tools (RAG Study, Giveaway Calculator, MHW Simulator)
2. **Performance** — all cycling/tri tools + CdA calculator + Cervia wetsuit predictor
3. **Property & Finance** — stamp duty, student loan, should-you-wait, recovery analysis
4. **Other** — random number generator

Each tool is a `.tool-card` link card with an icon, title, description, and arrow.

## Key Conventions

- **No build step** — all pages are static HTML, edit and push
- **No auth** — this site is fully public (unlike maji which has password gates)
- **No maji branding** — this is the Diamond Peak site, no maji disclaimers or references
- **Self-contained pages** — each tool has all its JS inline, no shared JS files
- **Google Fonts via CDN** — every page includes the same Google Fonts `<link>` tag
- **Charts** — some pages use Chart.js via CDN (`recovery-analysis.html`)

## ClaudeCoach Automation — NEVER use CronCreate

All ClaudeCoach scheduled tasks (watchdog, activity-watcher, refresh-site-data, checkins) run via **crontab on the VM only**. The scripts live in `ClaudeCoach/scripts/`. Do NOT use `CronCreate` for any of these — it injects prompts into the user's interactive session and survives context compaction. If asked to schedule something, add it to the VM crontab instead.

## Git Workflow

- Push directly to `main` — GitHub Pages deploys automatically
- Remote may drift — use `git pull --rebase origin main` before pushing
- If remote is missing: `git remote add origin https://github.com/Jamie-Diamond/diamondpeak-site.git`
