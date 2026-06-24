#!/usr/bin/env python3
"""
ClaudeCoach shared engine — the transport-agnostic coaching brain.

Prompt assembly + Claude generation, with NO Telegram or HTTP concerns. Imported
by BOTH the Telegram bot (telegram/bot.py) and the web API (FastAPI; Phase 0 of
the web-app transition, docs/app-transition-plan.md). The streaming entry point
`stream_claude` yields plain ('chunk'|'final', text) events that each transport
renders its own way — Telegram via editMessageText, the web via SSE. Keep this
module free of transport code so the bot and the API can never diverge.
"""
import json, subprocess, sys, time, shutil, os
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).parent.parent                # ClaudeCoach/
sys.path.insert(0, str(Path(__file__).parent))     # lib/ on path for coaching_levels

try:
    from coaching_levels import level_block as _level_block
except Exception:
    def _level_block(level: str) -> str:  # type: ignore[misc]
        return ""


def _resolve_claude_bin() -> str:
    found = shutil.which("claude")
    if found:
        return found
    for candidate in ("/usr/bin/claude", "/usr/local/bin/claude",
                      os.path.expanduser("~/.local/bin/claude")):
        if os.path.isfile(candidate):
            return candidate
    return "claude"


CLAUDE_BIN = _resolve_claude_bin()
TOOLS = "Read,Write,Edit,Bash"
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_OPUS   = "claude-opus-4-8"
MODEL_HAIKU  = "claude-haiku-4-5-20251001"
SYSTEM_PROMPT_FILE = BASE / "athletes/jamie/system_prompt.txt"


def log(msg):
    """Default logger (stderr). The Telegram bot points this at bot.log; the web
    API can point it at its own sink — engine code calls log() either way."""
    print(f"[engine] {msg}", file=sys.stderr)


def render_history(history, athlete_name):
    lines = []
    for h in history:
        stamp = ""
        ts = h.get("ts", "")
        if ts:
            try:
                stamp = datetime.fromisoformat(ts).strftime("[%a %H:%M] ")
            except Exception:
                stamp = ""
        lines.append(f"{stamp}{athlete_name}: {h['user']}")
        lines.append(f"ClaudeCoach: {h['assistant']}")
    return lines


def load_persistent_rules(sp_file) -> str:
    """Contents of persistent-rules.md adjacent to the system prompt, or ''."""
    rules_file = Path(sp_file).parent / "persistent-rules.md"
    if rules_file.exists():
        text = rules_file.read_text().strip()
        return text if text else ""
    return ""


def system_prompt_with_level(sp_file) -> str:
    """Read system_prompt.txt and append the athlete's coaching-level block."""
    sp_file = Path(sp_file)
    text = sp_file.read_text().strip()
    profile_path = sp_file.parent / "profile.json"
    if profile_path.exists():
        try:
            level = json.loads(profile_path.read_text()).get("coaching_level", "mid")
            block = _level_block(level)
            if block:
                text = text + "\n\n" + block
        except Exception:
            pass
    return text


_FEEDBACK_LOG_RULE = (
    "HARD RULE — feedback acknowledgement: When the athlete logs feedback, corrections, "
    "or session data in a single message, reply with exactly one word: Logged. "
    "Do not echo, summarise, or acknowledge the content."
)


def build_prompt(user_message, history, system_prompt, athlete_name, context,
                 persistent_rules=""):
    parts = [system_prompt, ""]
    if persistent_rules:
        parts.append("## Standing rules — always apply (athlete-agreed, session-derived)")
        parts.append(persistent_rules)
        parts.append("")
    parts.append(_FEEDBACK_LOG_RULE)
    parts.append("")
    if context:
        parts.append(context)
        parts.append("")
    if history:
        parts.append("Recent conversation:")
        parts.extend(render_history(history, athlete_name))
        parts.append("")
    parts.append(f"{athlete_name}: {user_message}")
    return "\n".join(parts)


def claude_cmd(prompt, model, extra_args=None):
    cmd = [CLAUDE_BIN, "-p", prompt, "--allowedTools", TOOLS,
           "--model", model, "--no-session-persistence"]
    if extra_args:
        cmd.extend(extra_args)
    return cmd


def _assemble(user_message, history, system_prompt_file, athlete_name, context):
    sp_file = Path(system_prompt_file) if system_prompt_file else SYSTEM_PROMPT_FILE
    return build_prompt(
        user_message, history,
        system_prompt_with_level(sp_file), athlete_name, context,
        persistent_rules=load_persistent_rules(sp_file),
    )


def call_claude(user_message, config, history, model=MODEL_SONNET,
                system_prompt_file=None, athlete_name="Jamie", context=""):
    full_prompt = _assemble(user_message, history, system_prompt_file, athlete_name, context)
    try:
        result = subprocess.run(
            claude_cmd(full_prompt, model),
            capture_output=True, text=True,
            cwd=config["project_dir"], timeout=300,
        )
        return result.stdout.strip() or result.stderr.strip() or "(no response)"
    except subprocess.TimeoutExpired:
        return "Sorry, that took too long. Try a simpler question or break it into steps."
    except Exception as e:
        log(f"Claude error: {e}")
        return f"Error calling claude: {e}"


def stream_claude(user_message, config, history, model=MODEL_SONNET,
                  system_prompt_file=None, athlete_name="Jamie", context=""):
    """Generator over a streaming Claude run. Yields (kind, text):
      ('chunk', snapshot) — growing reply to display live (transport throttles edits)
      ('final', full)     — authoritative full reply, emitted exactly once at the end
    assistant events are full snapshots (replace); content_block_delta events are
    incremental (append); result replaces all. Transport-agnostic by design."""
    full_prompt = _assemble(user_message, history, system_prompt_file, athlete_name, context)
    streamed = ""
    final = None
    try:
        proc = subprocess.Popen(
            claude_cmd(full_prompt, model, ["--output-format", "stream-json", "--verbose"]),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, cwd=config["project_dir"],
        )
        for raw_line in proc.stdout:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                ev = json.loads(raw_line)
            except Exception:
                continue
            ev_type = ev.get("type", "")
            if ev_type == "result":
                final = ev.get("result", "") or final
                continue
            elif ev_type == "assistant":
                snapshot = ""
                for block in (ev.get("message") or {}).get("content", []):
                    if block.get("type") == "text":
                        snapshot = block.get("text", "")
                if snapshot:
                    streamed = snapshot
            elif ev_type == "content_block_delta":
                delta = ev.get("delta", {})
                if delta.get("type") == "text_delta":
                    streamed += delta.get("text", "")
            else:
                continue
            if streamed.strip():
                yield ("chunk", streamed.strip())
        proc.wait(timeout=10)
        yield ("final", (final if final is not None else streamed).strip() or "(no response)")
    except subprocess.TimeoutExpired:
        yield ("final", (final if final is not None else streamed).strip() or "Sorry, that took too long.")
    except Exception as e:
        log(f"Claude stream error: {e}")
        yield ("final", (final if final is not None else streamed).strip() or f"Error: {e}")


def call_claude_with_image(img_path, caption, config, history, model=MODEL_SONNET,
                           system_prompt_file=None, athlete_name="Jamie", context=""):
    sp_file = Path(system_prompt_file) if system_prompt_file else SYSTEM_PROMPT_FILE
    system_prompt = system_prompt_with_level(sp_file)
    parts = [system_prompt, ""]
    persistent_rules = load_persistent_rules(sp_file)
    if persistent_rules:
        parts.append("## Standing rules — always apply (athlete-agreed, session-derived)")
        parts.append(persistent_rules)
        parts.append("")
    if context:
        parts.append(context)
        parts.append("")
    if history:
        parts.append("Recent conversation:")
        parts.extend(render_history(history, athlete_name))
        parts.append("")
    question = caption if caption else "analyse this"
    user_msg = f"{athlete_name} sent an image. Read it from {img_path} then {question}."
    parts.append(user_msg)
    full_prompt = "\n".join(parts)
    try:
        result = subprocess.run(
            claude_cmd(full_prompt, model),
            capture_output=True, text=True,
            cwd=config["project_dir"], timeout=300,
        )
        return result.stdout.strip() or result.stderr.strip() or "(no response)"
    except subprocess.TimeoutExpired:
        return "Sorry, that took too long. Try a simpler question or break it into steps."
    except Exception as e:
        log(f"Claude image error: {e}")
        return f"Error calling claude: {e}"
