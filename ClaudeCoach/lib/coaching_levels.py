"""Coaching level instruction blocks — injected into all Claude prompts."""

DEFAULT = "mid"

_BLOCKS = {
    "beginner": (
        "Coaching level: BEGINNER. "
        "Use effort-based language: easy, comfortable, moderate, hard, very hard. "
        "Heart rate (bpm and avg/max) is fine to include. "
        "For running, pace (min/km or min/mile) is fine to include. "
        "Do not reference power, zone numbers, TSS, NP, IF, or any other training metrics. "
        "Duration, distance, HR, and run pace are fine. Describe sessions in plain terms. "
        "Tone: encouraging, jargon-free."
    ),
    "mid": (
        "Coaching level: MID (default). "
        "Use plain-English labels throughout: Fitness (not CTL), Fatigue (not ATL), "
        "Load (not TSS), Form (not TSB). "
        "Include supporting numbers (pace, HR, power, zone %) but always contextualise them."
    ),
    "pro": (
        "Coaching level: PRO. "
        "Show plain-English labels with acronyms in parentheses on first use per reply: "
        '"Fitness (CTL)", "Fatigue (ATL)", "Load (TSS)", "Form (TSB)". '
        "After first use in a reply you may use either form. "
        "Include full technical detail: zone watts and pace, decoupling %, NP, IF, VI, "
        "W' estimates where relevant. Be terse and data-dense — skip soft framing, lead with numbers."
    ),
}


def level_block(level: str) -> str:
    """Return the instruction paragraph for injection into Claude prompts."""
    return _BLOCKS.get(level, _BLOCKS[DEFAULT])
