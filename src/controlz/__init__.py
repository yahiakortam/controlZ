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
from controlz.tracker import ToolProxy, TrackedCall, Tracker, TrackingError

__version__ = "0.1.0"

__all__ = [
    "Action",
    "Integration",
    "IntegrationError",
    "Ledger",
    "LedgerError",
    "Operation",
    "Reversibility",
    "RollbackPlan",
    "RollbackStep",
    "Session",
    "ToolProxy",
    "TrackedCall",
    "Tracker",
    "TrackingError",
    "UnsupportedOperationError",
]
