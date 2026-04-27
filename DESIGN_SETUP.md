# Design Stack — Install Guide

One-time setup for the Claude Code design tooling we use across our repos. Free stack — no paid subscriptions, no billing.

This guide is **repo-agnostic**. The install steps below set up tooling at user scope, so once done they apply across every project on your machine. The only repo-specific bit is step 4 (shadcn), and only if the repo has a React subfolder.

For project-specific design language (palettes, components, conventions), see the project's `DESIGN.md` (e.g. `diamondpeak-site/DESIGN.md`).

## What you'll have after this

| Tool | Type | Scope | Use for |
|---|---|---|---|
| `frontend-design` | Skill (Anthropic) | User | Building/redesigning pages from scratch — default for new UI work |
| `ui-ux-pro-max` | Skill (third-party) | User | Palette/font/UX reasoning — design-system advice, not generation |
| Stitch MCP | MCP server | User | Visual mockups via Google Stitch — sketch concepts before coding |
| shadcn MCP | MCP server | Per-project | React component installs — only in React/Vite/Next subfolders |

## Prerequisites

```bash
node --version    # 20+
npm --version
```

`gcloud` is **not required** if you use the API-key path for Stitch (recommended below).

## 1. `frontend-design` skill (Anthropic, official)

Likely already installed via Claude Code's plugin system — check first:

```
/plugin list
```

If `frontend-design` is not listed, install via the marketplace:

```
/plugin marketplace add anthropics/claude-code
/plugin install frontend-design
```

(The git-clone-into-`~/.claude/skills/` path also works but the marketplace install keeps it updateable.)

## 2. `ui-ux-pro-max` skill (uipro)

uipro v2.x has no `--global` flag — it always installs `.claude/skills/ui-ux-pro-max/` relative to CWD. To get user-scope (available across all projects), install via a scratch dir and move the folder:

```bash
npm install -g uipro-cli
mkdir -p /tmp/uipro-install && cd /tmp/uipro-install
uipro init --ai claude
mkdir -p ~/.claude/skills
mv /tmp/uipro-install/.claude/skills/ui-ux-pro-max ~/.claude/skills/
rm -rf /tmp/uipro-install
```

The scratch-dir step is a safety measure: running `uipro init` directly in `~` would let it write `.claude/` entries next to your live `settings.json`, sessions, plugins. The CLI is well-behaved (only writes inside `.claude/skills/ui-ux-pro-max/`), but routing through `/tmp` removes any risk of collision.

The skill activates automatically when you ask for UI/UX work — no manual invocation needed.

## 3. Stitch MCP — API-key path (recommended)

The official Stitch docs recommend an API key over OAuth/gcloud: simpler, doesn't expire, no Google Cloud project needed.

**Step 1 — Get an API key.** Sign in to [Stitch](https://stitch.withgoogle.com), open settings, generate an API key.

**Step 2 — Register the MCP server at user scope:**

```bash
claude mcp add -s user stitch -e STITCH_API_KEY=YOUR_API_KEY -- npx @_davideast/stitch-mcp proxy
```

The `-s user` flag is important — without it the server is registered at *local* scope, only active in the directory where you ran the command. With `-s user` it's available from every project.

This writes to `~/.claude.json`. The resulting config block looks like:

```json
{
  "mcpServers": {
    "stitch": {
      "type": "stdio",
      "command": "npx",
      "args": ["@_davideast/stitch-mcp", "proxy"],
      "env": { "STITCH_API_KEY": "YOUR_API_KEY" }
    }
  }
}
```

**Step 3 — Verify the key works:**

```bash
STITCH_API_KEY=YOUR_API_KEY npx -y @_davideast/stitch-mcp doctor --json
```

Expected: `"allPassed": true` with both "API Key" and "Stitch API" checks passing. If the API check fails with a 4xx, the key is invalid or revoked.

**Don't commit the API key.** It lives in `~/.claude.json` outside any repo. If you ever need to share config, redact the key.

### Alternative — gcloud OAuth path

Only if you specifically need OAuth (e.g. workplace policy bans long-lived API keys):

```bash
gcloud auth login
gcloud config set project YOUR_GCP_PROJECT_ID
gcloud auth application-default login
gcloud beta services mcp enable stitch.googleapis.com --project=YOUR_GCP_PROJECT_ID
```

Note: GCP billing-enablement requirements for `stitch.googleapis.com` are not documented on the public Stitch MCP setup page — check before assuming "free tier covers it." If gcloud refuses to enable the API without billing, that answers the question.

## 4. shadcn MCP — per-project, React folders only

shadcn is a React component registry. **Only install it inside React/Vite/Next subfolders.** Don't put it at a repo root if the main site is static HTML or a non-React framework — it'll suggest React components in places they don't belong.

In a React project (or React subfolder), create `.mcp.json` next to `package.json`:

```json
{
  "mcpServers": {
    "shadcn": {
      "command": "npx",
      "args": ["-y", "shadcn@latest", "mcp"]
    }
  }
}
```

No auth required. The MCP only activates when Claude Code is started with that folder as CWD.

**Repo with mixed stacks** (e.g. static site at root, React subfolder): place `.mcp.json` *inside* the React subfolder, not at the root. Example: `diamondpeak-site/cda-calculator/.mcp.json` — shadcn is only available when working in the calculator app.

**Add to `.gitignore`?** No — the file contains no secrets and is genuinely shared config. Commit it.

## Verify the whole stack

Restart Claude Code, then:

```
/mcp
```

Expected:
- `stitch` — `Connected`
- `shadcn` — `Connected` only when CWD is a React folder with the `.mcp.json`, otherwise absent

```
/plugin list
```

Expected: `frontend-design` listed.

Skills (`ui-ux-pro-max`, `frontend-design`) load on demand when you describe UI/UX work — there's no "connected" state to check. Trigger them by asking for design work and watch for the skill being invoked.

## Troubleshooting

- **`stitch` shows `Failed to connect`** → first run downloads the npx package and may exceed Claude's connect timeout. Restart Claude Code or run `npx -y @_davideast/stitch-mcp doctor --json` once to warm the cache, then re-check `/mcp`.
- **`stitch` registered but not visible from other projects** → it was added at local scope. Run `claude mcp remove stitch` then re-add with `-s user`.
- **`stitch` API key invalid** → run the doctor command above with the key inline. If the API check returns 401/403, regenerate the key in Stitch settings.
- **`uipro init` fails** → ensure `~/.claude/skills/` exists (`mkdir -p ~/.claude/skills`), retry.
- **`shadcn` connecting at repo root when it shouldn't** → an `.mcp.json` exists at root; move it into the React subfolder.
- **Stitch generates Tailwind/React for a non-React project** → expected. Translate into the project's CSS by hand using its tokens. Never paste Stitch output verbatim into a vanilla-CSS codebase.

## How to use these in a new repo

1. Add a `DESIGN.md` to the repo capturing house style: palette, type system, component anatomy, spacing scale, anti-patterns. (Use `diamondpeak-site/DESIGN.md` as a template.)
2. Inside `DESIGN.md`, include a **briefing template** to paste when invoking `frontend-design` or Stitch — locks the tools to your tokens and prevents generic AI aesthetics. The Diamond Peak template is at the bottom of its `DESIGN.md`.
3. Reference this `DESIGN_SETUP.md` for the install steps (or copy it into the repo).
4. If the repo has a React subfolder, follow step 4 above for shadcn.

## What this stack deliberately doesn't include

- **No paid Figma/Framer plugins** — out of scope, free stack only.
- **No Tailwind** auto-install — many of our sites are hand-written CSS. Add Tailwind only if the project already uses it.
- **No image-generation MCP** — use claude.ai's native generation, save the file, drop into the repo manually.
