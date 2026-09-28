#!/usr/bin/env python3
"""Daily: update the Claude CLI behind a smoke test, and tell Jamie when a model changes.

Why (27 Sep 2026, Jamie: "how do we make sure we always get new models?"): every
call names its model by CLI alias ("opus", "sonnet", "haiku", "fable"), so it runs
the newest model the INSTALLED CLI knows. New models only arrive with a new CLI,
and the VM's sat on 2.1.144 from May until it was updated by hand. At that point
it rejected Opus 5.5 and Fable 5.1 outright. This job keeps the CLI current.

  1. npm has a newer CLI -> install it to a throwaway folder (the canary).
  2. Smoke-test the canary (or the live CLI when nothing is new) with the bot's
     own call shape: every alias through stream-json with the bot's tool allow and
     deny lists, plus one real tool call on Opus. The model each alias answered
     as is read from the stream, which is how the alias map is learned.
  3. All pass -> install exactly that version globally and write the alias map
     to model_aliases.RESOLVED_FILE. Any failure -> the live CLI is untouched.
  4. An alias now answering as a different model -> Telegram (MODEL_CHANGED).
     A failed update is a digest line only: the bot keeps working on the CLI it
     already has, so there is nothing for Jamie to do at 03:15.

Daily, not weekly, since 28 Sep 2026: Sonnet 5.5 shipped hours after that
Monday's 03:15 run and would have waited a week. A no-change day is ~20 seconds
and five one-word replies.

No bot restart is needed: engine spawns the CLI per reply, so the next reply runs
the new binary, and the footer re-reads the alias map when the file changes.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # ClaudeCoach/
sys.path.insert(0, str(BASE / "lib"))
import coach_alert      # noqa: E402
import model_aliases    # noqa: E402
import ops_log          # noqa: E402

SCRIPT  = "claude-cli-update"
PACKAGE = "@anthropic-ai/claude-code"
CLAUDE  = "/usr/bin/claude"
NPM     = shutil.which("npm") or "/usr/bin/npm"
CALL_TIMEOUT = 300      # Fable can take two minutes to say "ok"
TOOL_TOKEN = "cc-smoke-7431"


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def _run(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def cli_version(binary) -> str | None:
    try:
        out = _run([str(binary), "--version"], timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"\d+\.\d+\.\d+", out)
    return m.group(0) if m else None


def latest_version() -> str | None:
    try:
        out = _run([NPM, "view", PACKAGE, "version"], timeout=120).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out if re.fullmatch(r"\d+\.\d+\.\d+", out) else None


def newer(a: str, b: str) -> bool:
    return tuple(map(int, a.split("."))) > tuple(map(int, b.split(".")))


def install_canary(version: str) -> Path | None:
    prefix = Path(tempfile.mkdtemp(prefix="cc-canary-"))
    try:
        _run([NPM, "install", "--prefix", str(prefix), f"{PACKAGE}@{version}"],
             timeout=600)
    except (OSError, subprocess.SubprocessError):
        pass
    binary = prefix / "node_modules/.bin/claude"
    return binary if binary.exists() else None


def parse_stream(stdout: str) -> dict:
    """What one stream-json run tells us: the model that answered, the final
    result text, whether the CLI flagged an error, and which tools it used."""
    out = {"model": None, "result": "", "is_error": True, "tools": []}
    for line in (stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "assistant":
            msg = ev.get("message") or {}
            out["model"] = msg.get("model") or out["model"]
            out["tools"] += [b.get("name") for b in msg.get("content") or []
                             if b.get("type") == "tool_use"]
        elif ev.get("type") == "result":
            out["result"] = ev.get("result") or ""
            out["is_error"] = bool(ev.get("is_error"))
    return out


def _call(binary, alias: str, prompt: str) -> dict:
    import engine   # here, not at the top: the tests never need its imports
    cmd = [str(binary), "-p", "--allowedTools", engine.TOOLS,
           "--disallowedTools", engine.DISALLOWED_TOOLS, "--model", alias,
           "--output-format", "stream-json", "--verbose"]
    with tempfile.TemporaryDirectory(prefix="cc-smoke-") as cwd:
        try:
            p = _run(cmd, input=prompt, timeout=CALL_TIMEOUT, cwd=cwd)
        except subprocess.TimeoutExpired:
            return {"model": None, "result": "timed out", "is_error": True, "tools": []}
    got = parse_stream(p.stdout)
    if not got["result"] and p.stderr:
        got["result"] = p.stderr.strip()[-200:]
    return got


def _check(binary, alias: str, prompt: str, passed) -> tuple[dict, str]:
    """Run one check, once more on failure: a single overloaded response must
    not cost a week on the old CLI."""
    for _ in range(2):
        got = _call(binary, alias, prompt)
        if not got["is_error"] and got["model"] and passed(got):
            return got, ""
    return got, f"{alias}: {got['result'][:120] or 'no result'}"


def smoke(binary) -> tuple[dict, list]:
    """({alias: model id}, [problems]). Empty problems means safe to run on."""
    models, problems = {}, []
    for alias in model_aliases.ALIASES:
        got, problem = _check(binary, alias, "Reply with just: ok",
                              lambda g: "ok" in g["result"].lower())
        if problem:
            problems.append(problem)
        else:
            models[alias] = got["model"]
    _, problem = _check(
        binary, "opus",
        f"Use the Bash tool to run: echo {TOOL_TOKEN}. Then reply with its output only.",
        lambda g: TOOL_TOKEN in g["result"] and "Bash" in g["tools"])
    if problem:
        problems.append("tool use " + problem)
    return models, problems


def swap(version: str) -> str:
    """Install `version` globally. Returns "" on success, else what went wrong."""
    try:
        p = _run([NPM, "install", "-g", f"{PACKAGE}@{version}"], timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        return f"npm install -g failed: {e}"
    now = cli_version(CLAUDE)
    if now != version:
        return f"{CLAUDE} reports {now} after installing {version}: {p.stderr[-200:]}"
    return ""


def write_state(version: str, models: dict) -> None:
    f = model_aliases.RESOLVED_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"cli_version": version, "models": models,
                               "checked": datetime.now().isoformat(timespec="seconds")},
                              indent=2))
    tmp.replace(f)


def _name(model_id: str) -> str:
    family, version = model_aliases.pretty(model_id)
    return f"{family} {version}" if family else model_id


def change_lines(old: dict, new: dict) -> list:
    """"Opus 5.5 → Opus 6" for each alias that now answers as another model.
    Nothing on the first run: there is no "before" to compare against."""
    return [f"{_name(old[a])} → {_name(new[a])}"
            for a in model_aliases.ALIASES
            if old.get(a) and new.get(a) and old[a] != new[a]]


def change_message(lines: list) -> str:
    return ("🤖 The coach is on a new model:\n" + "\n".join(f"• {l}" for l in lines)
            + "\nReply footers show the new version from now on.")


def main() -> int:
    live = cli_version(CLAUDE)
    latest = latest_version()
    prev = (model_aliases.read_state().get("models") or {})
    log(f"live CLI {live}, npm latest {latest}")
    if not live:
        ops_log.alert(SCRIPT, f"cannot read the version of {CLAUDE}")
        return 1

    target, binary = None, CLAUDE
    if latest and newer(latest, live):
        binary = install_canary(latest)
        if not binary:
            ops_log.alert(SCRIPT, f"could not install CLI {latest} to test it - "
                                  f"staying on {live}")
            return 1
        target = latest

    try:
        models, problems = smoke(binary)
    finally:
        if target:
            shutil.rmtree(Path(binary).parents[2], ignore_errors=True)

    if problems:
        head = (f"CLI {target} failed its smoke test - staying on {live}" if target
                else f"CLI {live} failed its daily smoke test")
        ops_log.alert(SCRIPT, head + ": " + "; ".join(problems))
        return 1

    if target:
        err = swap(target)
        if err:
            restore = swap(live)
            ops_log.alert(SCRIPT, f"swap to {target} failed ({err}); restore to {live} "
                                  + ("ok" if not restore else f"ALSO failed: {restore}"))
            return 1
        log(f"CLI updated {live} -> {target}")

    write_state(target or live, models)
    log(f"aliases: {models}")
    lines = change_lines(prev, models)
    if lines:
        action = coach_alert.send(coach_alert.MODEL_CHANGED, change_message(lines),
                                  key=json.dumps(models, sort_keys=True))
        log(f"model change {lines}: Telegram {action}")
    ops_log.record_run(SCRIPT, ok=True, detail="checked ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
