#!/usr/bin/env python3
"""Move a stuck Peak sign-up on to the Connect Intervals.icu step (15 Oct 2026).

Sign-ups that stalled at "paste your API key" (or the old set-up walk-through) keep their
answers (name, race...) and are asked the new one-tap Connect step instead. Anyone who
hasn't agreed to the consent step yet is asked that first, as the bot always does. Their
old Intervals.icu buttons are cleared so they can't be tapped.

  python3 scripts/restart-signup-at-connect.py web-d05ed55007            # dry run: what would change
  python3 scripts/restart-signup-at-connect.py web-d05ed55007 --send     # do it and message them

Messages go to their Peak chat (and a phone notification if they turned them on).
Jamie decides who gets restarted (15 Oct 2026: "restart their sign up").
"""
import argparse
import sys
from pathlib import Path

CC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CC / "lib"))
sys.path.insert(0, str(CC / "telegram"))

INTRO = "Good news: linking Intervals.icu is now one tap, no API key needed."


def restart(B, cid: str, send: bool) -> str:
    st = B.load_onboarding_state()
    s = st.get(cid)
    if cid not in B.load_pending() or not s:
        return f"{cid}: not a sign-up in progress - skipped"
    was = s.get("current_key")
    if was in ("icu_connect", "icu_fix") or (was == "consent" and any(
            k == "icu_connect" for k, _q in s.get("queue") or [])):
        return f"{cid}: already at {was} - skipped"
    answers = s.setdefault("answers", {})
    for k in ("icu_has", "icu_key", "icu_id", "icu_setup", "icu_auth"):
        answers.pop(k, None)
    s["queue"] = [q for q in s.get("queue") or [] if not str(q[0]).startswith("icu_")]
    s["icu_data"] = {}
    consented = answers.get("consent") == "yes"
    if consented:
        s["current_key"] = "icu_connect"
        key, text = "icu_connect", INTRO + "\n\n" + B._OB_ICU_CONNECT_Q
    else:
        s["current_key"] = "consent"
        s["queue"].insert(0, ["icu_connect", B._OB_ICU_CONNECT_Q])
        key, text = "consent", B._OB_CONSENT_Q.replace(
            "Hi! I'm your Peak coach. Before we start, one thing to agree.",
            INTRO + " Before we carry on, one thing to agree.")
    line = f"{cid}: {was} -> {s['current_key']} (then Connect Intervals.icu); kept {sorted(answers)}"
    if not send:
        return line + "  [dry run]"
    B.save_onboarding_state(st)
    B._outbox_clear_buttons(cid)
    B._ob_ask(B.load_config()["bot_token"], cid, key, text, answers)
    return line + "  [sent]"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("chat_ids", nargs="+")
    p.add_argument("--send", action="store_true")
    a = p.parse_args()
    import bot as B
    for cid in a.chat_ids:
        print(restart(B, cid, a.send))
    return 0


if __name__ == "__main__":
    sys.exit(main())
