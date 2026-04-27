# Diamond Peak — Design Guide

How we make pages look like Diamond Peak. Read before designing or generating any new page.

## Identity in one line

**Ink on warm paper.** Editorial serif headings, technical mono labels, generous whitespace, restrained colour. Never glassy, never neumorphic, never gradient-heavy. The site should feel like a well-set field notebook, not a SaaS dashboard.

## Palette (canonical — `style.css` `:root`)

| Token | Hex | Use |
|---|---|---|
| `--bg` | `#f8f5ef` | Page background (warm paper) |
| `--card` | `#ffffff` | Panel / card surface |
| `--ink` | `#18160f` | Primary text, headings |
| `--ink2` | `#4a4535` | Secondary text, body paragraphs, labels |
| `--muted` | `#9a9080` | Tertiary text, kickers, hints, footer |
| `--border` | `#ddd8cc` | All borders, dividers |
| `--green` | `#1d6840` | Primary accent, focus rings, positive results |
| `--green-bg` | `#e6f4ec` | Positive result background |
| `--blue` | `#1a5276` | Secondary accent, links |
| `--red` | `#b53a1e` | Errors, negative results |
| `--red-bg` | `#fdeee9` | Negative result background |
| `--amber` | `#b87c20` | Warnings, caution states |

**Hero exception:** the homepage hero uses a near-black background `#06080a` with the animated topo canvas. Sub-pages do not use a dark hero.

**Don't introduce new colours** without a reason that can't be solved with the existing tokens. If a tool genuinely needs a new accent (e.g. a chart series), add it as a new `--*` variable in `style.css`, don't inline a hex.

## Type system

| Role | Font | Weight | Notes |
|---|---|---|---|
| H1 | Libre Baskerville (serif) | 700 | `clamp(26px, 5vw, 38px)`, line-height `1.15` |
| Body | DM Sans | 300 | Default body weight is **light** — preserve it |
| Labels, kickers, mono input | DM Mono | 400–500 | Uppercase + letter-spacing for kickers/panel-titles |
| Numeric inputs | DM Mono | 400 | 16px, never less (iOS zoom guard) |

Always include the same Google Fonts `<link>` tag from the page template. Don't swap the fonts per-page.

## Spacing & sizing

Roughly an **8-point scale**, with 4-point half-steps for tight UI. Observed values from `style.css`: 4, 8, 10, 12, 14, 16, 20, 24, 36, 48, 56, 72.

- Page max-width: `820px` (default), `720px` (property pages override). Don't go wider.
- Panel padding: `20px` mobile, `24px` ≥520px.
- Field height: `48px` (touch target).
- Border radius: `8px` panels, `4px` inputs. **No fully rounded pills.**
- Page top padding: `36px` mobile, `56px` ≥600px.

## Component anatomy (from `style.css`)

```
.page                 ← container, max-width 820px
  .back               ← "← Back to tools", mono 11px, muted
  .kicker             ← uppercase mono eyebrow, 10px, muted, 0.2em tracking
  h1                  ← serif, 700
  .intro              ← DM Sans, 14px, ink2, line-height 1.7
  .panel              ← white card, 1px border, 8px radius
    .panel-title      ← uppercase mono 9px, bottom-bordered
    .field
      label           ← 11px, ink2, 500
      input/select    ← mono 16px, 48px tall, green focus border
      .hint           ← mono 10px, muted
    .input-row        ← flex pair of fields
  .footer             ← mono 10px, muted, centered
```

**Class prefixes for collision avoidance** (when a page heavily customises panels/fields):
- `.gc-` ftp-calculator, power-to-weight
- `.gr-` gear-ratio-calculator
- `.ps-` pacing-strategy-builder
- `.tss-` tss-calculator
- `.v-` vo2max-estimator
- `.tp-` triathlon-race-predictor

If a new tool will redefine `.panel`/`.field` heavily, add a new prefix. If it just consumes them as-is, no prefix.

## Result/output blocks

When a tool produces a result (calculation output), the convention is:

- A panel with a `.panel-title` like `RESULT` or `BREAKDOWN`
- Numbers in DM Mono, large (24–32px), `--ink`
- Units/labels in DM Mono small (10–11px), `--muted`
- Positive deltas: `--green` text on `--green-bg`. Negative: `--red`/`--red-bg`. Caution: `--amber`.

## Tool selection — when to reach for what

We have several design tools installed (see `DESIGN_SETUP.md`). They each fit a different job:

### `frontend-design` skill — *default for new pages*
**Use when:** building a new tool page from scratch, redesigning an existing page, designing a hero variant, or building a non-tool page (about, case study).
**Brief it with:** the palette tokens, type system, and component anatomy from this file. Tell it the page is **vanilla HTML + inline `<style>` + the shared `style.css`**, no React, no Tailwind.
**Don't use it for:** the React `cda-calculator/` app — different stack.

### `ui-ux-pro-max` (uipro) skill — *for design-system reasoning*
**Use when:** you need a palette suggestion, font pairing rationale, chart-type recommendation, or a UX heuristic check ("is this form layout right?"). It's a reference, not a generator.
**Don't use it for:** generating final code. Treat its output as advisory — translate into our existing token system, don't import its.

### Google Stitch (via stitch-mcp) — *for visual mockups*
**Use when:** sketching a concept or alternative layout, or producing a high-fidelity mockup of a new page before coding it.
**Workflow:** generate in Stitch → review → translate by hand (or with `frontend-design`) into our HTML/CSS using **our** tokens. **Never paste Stitch's generated CSS/Tailwind directly** — it will break our identity.

### `shadcn` MCP — *cda-calculator only*
**Scope:** `~/diamondpeak-site/cda-calculator/` (the React app). Configured via `cda-calculator/.mcp.json`, not the repo root.
**Don't use it on the static site** — adding React components to vanilla HTML pages is a regression.

### Claude.ai native image generation — *for imagery*
**Workflow:** generate in claude.ai (web), download the file, drop into the repo. There is no `assets/` directory yet — create one at the repo root (`~/diamondpeak-site/assets/`) for shared images, or `<tool>/assets/` for tool-specific imagery. Optimise to ≤200KB; prefer SVG for icons, WebP/JPG for photos.

## New tool page checklist

Before pushing a new sub-page:

- [ ] File lives in correct section folder (`cycling/`, `property/`, etc.)
- [ ] `<head>` includes the canonical Google Fonts `<link>` (Libre Baskerville + DM Sans 300/400/500/600 + DM Mono 400/500)
- [ ] `<link rel="stylesheet" href="../style.css">` present
- [ ] Page-specific styles in inline `<style>`, using `--*` tokens (no raw hex)
- [ ] `.page > .back > .kicker > h1 > .intro` structure in that order
- [ ] All inputs are `48px` tall, mono 16px (iOS zoom guard)
- [ ] Focus state is `--green` border on inputs
- [ ] Result panel uses the result/output convention above
- [ ] `.footer` with `&copy; 2025 Diamond Peak Consulting`
- [ ] Linked from `index.html` in the right section as a `.tool-card`
- [ ] Mobile check at 375px wide — no horizontal scroll, panel padding feels right
- [ ] No console errors, no broken images, no Chart.js without CDN script

## Anti-patterns — don't do these

- **Tailwind utility classes** in HTML files. We don't have Tailwind. Inline `<style>` only.
- **Raw hex codes** outside `style.css`. Use `var(--*)`.
- **New fonts** mid-site. Stick to the three.
- **Pill buttons / fully rounded corners.** Max radius is 8px (panels) / 4px (inputs).
- **Drop shadows on panels.** The border-only treatment is the look.
- **Gradients** anywhere except the homepage topo canvas.
- **Emoji icons** in tool cards or content. Use SVG or text.
- **maji branding or disclaimers.** This is Diamond Peak — public, no auth, no maji.
- **Build steps** for the static site. Edit, commit, push.

## Briefing template — for `frontend-design` or Stitch

When asking a design tool to produce something for this site, paste this:

```
Brief: Diamond Peak Consulting — static HTML site, GitHub Pages, no build step,
no framework, no Tailwind. Pages are self-contained HTML with inline <style>
linking a shared style.css.

Identity: ink-on-warm-paper editorial. Background #f8f5ef, ink #18160f, accents
green #1d6840 and blue #1a5276. Borders #ddd8cc. Use only these tokens.

Type: Libre Baskerville 700 for h1 (serif), DM Sans 300 for body, DM Mono for
labels/inputs/numerics. Kickers and panel titles are uppercase mono with letter-
spacing.

Layout: max-width 820px, white panels with 1px border, 8px radius, no shadows.
Inputs 48px tall, 4px radius, mono 16px, green focus border. Spacing on an
8-point scale.

Output: a single self-contained HTML file with inline <style>. Reference variables
from style.css (--bg, --ink, --ink2, --muted, --border, --green, --blue, --card)
— do not redefine them. No Tailwind, no React, no external CSS frameworks.
```
