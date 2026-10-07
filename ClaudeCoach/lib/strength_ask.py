"""The strength-session debrief question, shared by activity-watcher and evening-checkin."""
from __future__ import annotations

def strength_ask(injuries) -> str:
    """What a strength debrief asks (Jamie, 7 Oct 2026). RPE only when no heart rate was
    recorded: then it is the only measure of the session (plan_tools log-strength turns
    it into load); with HR, load comes from HR and RPE changes nothing. An athlete with
    an active injury is asked about it instead, since that is what changes their plan
    (Fred, rehabbing an ACL on physio strength sessions). Never ask what the session
    name already answers ("Quads", "Hamstring strength")."""
    areas = sorted({str(i.get("location")).strip() for i in injuries or [] if i.get("location")})
    rule = ("- Never ask what the activity name or planned session already says (a session called "
            "\"Quads\" answers \"what was the focus?\"). Ask the focus only when neither says it.\n"
            "- \"RPE?\" ONLY when the activity has NO heart rate data; with HR, never ask RPE.")
    if areas:
        where = " and ".join(areas)
        return (f"\"How did the {where} feel - any pain or swelling during or after?\"\n" + rule)
    return "no question when HR was recorded (just the summary line).\n" + rule
