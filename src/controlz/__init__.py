"""ControlZ — a transaction/rollback layer for AI agents.

Record what an agent does as it does it, classify how reversible each step is,
and keep a durable plan for taking it back.
"""

from controlz.integrations import Integration, IntegrationError, UnsupportedOperationError
from controlz.ledger import Ledger, LedgerError
from controlz.models import (
    Action,
    Operation,
    Reversibility,
    RollbackPlan,
    RollbackStep,
    Session,
)
from controlz.policy import (
    Decision,
    Policy,
    PolicyDecision,
    PolicyGate,
    PolicyViolation,
    RuleFinding,
)
from controlz.rollback import (
    ConflictDetail,
    RollbackEngine,
    RollbackEntry,
    RollbackOutcome,
    RollbackReport,
    dependency_order,
)
from controlz.score import (
    BlastRadius,
    ReversibilityScore,
    ScoredItem,
    reversibility_score,
)
from controlz.tracker import ToolProxy, TrackedCall, Tracker, TrackingError

__version__ = "0.1.0"

__all__ = [
    "Action",
    "BlastRadius",
    "ConflictDetail",
    "Decision",
    "Integration",
    "IntegrationError",
    "Ledger",
    "LedgerError",
    "Operation",
    "Policy",
    "PolicyDecision",
    "PolicyGate",
    "PolicyViolation",
    "Reversibility",
    "ReversibilityScore",
    "RollbackEngine",
    "RollbackEntry",
    "RollbackOutcome",
    "RollbackPlan",
    "RollbackReport",
    "RollbackStep",
    "RuleFinding",
    "ScoredItem",
    "Session",
    "ToolProxy",
    "TrackedCall",
    "Tracker",
    "TrackingError",
    "UnsupportedOperationError",
    "dependency_order",
    "reversibility_score",
]
