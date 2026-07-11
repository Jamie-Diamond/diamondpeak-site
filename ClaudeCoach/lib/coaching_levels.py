"""Coaching level instruction blocks — injected into all Claude prompts."""

DEFAULT = "mid"

_BLOCKS = {
    "beginner": (
        "Coaching level: BEGINNER. "
        "These rules override any sport-specific format instructions in this prompt. "
        "Use effort-based language: easy, comfortable, moderate, hard, very hard. "
        "Heart rate (bpm and avg/max) is fine to include. "
        "For running, pace (min/km) is fine to include. "
        "For swimming, pace per 100m is fine to include. "
        "For cycling, use effort and HR only — do not reference speed or power. "
        "Do not reference zone numbers, TSS, NP, IF, or any other training metrics. "
        "Duration, distance, HR, run pace, and swim pace are fine. Describe sessions in plain terms. "
        "Tone: encouraging, jargon-free."
    ),
    "mid": (
        "Coaching level: MID (default). "
        "Use plain-English labels throughout: Fitness (not CTL), Fatigue (not ATL), "
        "Load (not TSS), Form (not TSB). "
        "Include supporting numbers (pace, HR, power, zone %) but always contextualise them. "
        "Tone: direct and informative — matter-of-fact, never gushing, occasionally dry."
    ),
    "pro": (
        "Coaching level: PRO. "
        "These rules override any sport-specific format instructions in this prompt. "
        "Lead with a short narrative verdict, then a single session-appropriate key-numbers line. "
        "Surface only the few metrics that matter for this session, not a full stat dump. "
        "Use plain-English labels with the acronym in parentheses on first use per reply "
        '("Fitness (CTL)", "Fatigue (ATL)", "Load (TSS)", "Form (TSB)"); either form after that. '
        "Give the full technical detail (zone watts and pace, decoupling %, NP, IF, VI, W' estimates) "
        "only when the athlete asks for it. "
        "Tone: direct and matter-of-fact, lightly technical, never gushing."
    ),
}

# Applies to every coaching level and every surface (chat, debrief, scheduled cards).
_UNIVERSAL = (
    " Give the number and a one-line plain-English driver; "
    "never print derivation formulas unless the athlete asks for them."
)


def level_block(level: str) -> str:
    """Return the instruction paragraph for injection into Claude prompts."""
    return _BLOCKS.get(level, _BLOCKS[DEFAULT]) + _UNIVERSAL
