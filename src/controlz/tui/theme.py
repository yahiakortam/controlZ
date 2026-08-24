"""The palette. Reversibility is the signature — colour carries the meaning.

Restrained on purpose: a near-black ground, one muted foreground, and four
accents that mean exactly one thing each. Nothing else in the interface is
allowed to be green, amber, or red.
"""

from __future__ import annotations

from controlz.models import Reversibility
from controlz.rollback import RollbackOutcome

__all__ = ["INK", "OUTCOME_COLOR", "REVERSIBILITY_COLOR", "color_for", "outcome_color"]

#: The four meanings, and nothing else.
REVERSIBILITY_COLOR: dict[Reversibility, str] = {
    Reversibility.REVERSIBLE: "#3fb950",  # green — comes back exactly
    Reversibility.COMPENSATABLE: "#d29922",  # amber — comes back partly
    Reversibility.IRREVERSIBLE: "#f85149",  # red — does not come back
    Reversibility.UNKNOWN: "#8b949e",  # grey — we do not know, so assume the worst
}

#: Ground, panel, and text. Deliberately unsaturated so the accents carry.
INK = {
    "ground": "#0d1117",
    "panel": "#161b22",
    "line": "#30363d",
    "text": "#c9d1d9",
    "dim": "#6e7681",
    "bright": "#f0f6fc",
    "select": "#1f6feb",
}

#: Rollback outcomes reuse the same four accents wherever the meaning matches.
OUTCOME_COLOR: dict[RollbackOutcome, str] = {
    RollbackOutcome.RESTORED: REVERSIBILITY_COLOR[Reversibility.REVERSIBLE],
    RollbackOutcome.NOTHING_TO_DO: INK["dim"],
    RollbackOutcome.PLANNED: "#58a6ff",
    RollbackOutcome.SKIPPED: REVERSIBILITY_COLOR[Reversibility.IRREVERSIBLE],
    RollbackOutcome.CONFLICT: REVERSIBILITY_COLOR[Reversibility.COMPENSATABLE],
    RollbackOutcome.BLOCKED: REVERSIBILITY_COLOR[Reversibility.COMPENSATABLE],
    RollbackOutcome.FAILED: REVERSIBILITY_COLOR[Reversibility.IRREVERSIBLE],
    RollbackOutcome.NOT_ATTEMPTED: INK["dim"],
}


def color_for(reversibility: Reversibility) -> str:
    return REVERSIBILITY_COLOR.get(reversibility, INK["dim"])


def outcome_color(outcome: RollbackOutcome) -> str:
    return OUTCOME_COLOR.get(outcome, INK["dim"])
