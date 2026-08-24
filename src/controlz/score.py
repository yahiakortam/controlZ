"""Pre-execution scoring: how much of a plan could be taken back?

:func:`reversibility_score` tallies proposed operations by reversibility class
and returns a single coverage figure plus a blast-radius summary — what the
plan touches, and which parts of it could not be undone.

This runs *before* anything executes. It answers "if this goes wrong, how much
can we recover?" while that is still a hypothetical question.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from controlz.models import Action, Operation, Reversibility

if TYPE_CHECKING:  # pragma: no cover - typing only
    from controlz.integrations import Integration

__all__ = [
    "DEFAULT_WEIGHTS",
    "BlastRadius",
    "ReversibilityScore",
    "ScoredItem",
    "reversibility_score",
]

#: How much credit each class earns toward coverage.
#:
#: A compensatable action is worth half: a retraction is real mitigation, but it
#: is not restoration — the email was still read. Irreversible and unknown earn
#: nothing, and for the same reason: an unclassified action must be assumed
#: unrecoverable until something proves otherwise.
DEFAULT_WEIGHTS: dict[Reversibility, float] = {
    Reversibility.REVERSIBLE: 1.0,
    Reversibility.COMPENSATABLE: 0.5,
    Reversibility.IRREVERSIBLE: 0.0,
    Reversibility.UNKNOWN: 0.0,
}


class ScoredItem(BaseModel):
    """One proposed operation, with its classification and what it touches."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    api_call: str
    reversibility: Reversibility
    target: str = Field(default="", description="Human label for what this touches.")
    intent: str | None = None

    def describe(self) -> str:
        where = f" on {self.target}" if self.target else ""
        return f"{self.tool}.{self.api_call}{where}"


class BlastRadius(BaseModel):
    """What a plan would touch, and which parts of it could not be undone."""

    model_config = ConfigDict(extra="forbid")

    tools: dict[str, int] = Field(default_factory=dict, description="Calls per tool.")
    operations: dict[str, int] = Field(default_factory=dict, description="Calls per api_call.")
    targets: list[str] = Field(default_factory=list, description="Distinct things touched.")
    irreversible: list[ScoredItem] = Field(default_factory=list)
    unknown: list[ScoredItem] = Field(default_factory=list)

    @property
    def target_count(self) -> int:
        return len(self.targets)

    @property
    def unrecoverable(self) -> list[ScoredItem]:
        """Everything that could not be taken back: irreversible and unclassified."""
        return [*self.irreversible, *self.unknown]

    def describe(self) -> str:
        """One line: how wide, how deep, and what cannot be undone."""
        tools = ", ".join(f"{name} x{count}" for name, count in sorted(self.tools.items()))
        targets = f"{self.target_count} target{'s' if self.target_count != 1 else ''}"
        line = f"{tools or 'nothing'} across {targets}"
        if self.unrecoverable:
            line += f"; {len(self.unrecoverable)} cannot be undone"
        return line


class ReversibilityScore(BaseModel):
    """The tally for a proposed plan.

    :attr:`coverage` is the headline: a weighted percentage of the plan that
    could be recovered, where a compensatable action counts half. :attr:`counts`
    holds the raw tally so a caller can apply its own weighting.
    """

    model_config = ConfigDict(extra="forbid")

    total: int = 0
    counts: dict[Reversibility, int] = Field(default_factory=dict)
    coverage: float = Field(default=100.0, description="Weighted recoverability, 0-100.")
    items: list[ScoredItem] = Field(default_factory=list)
    blast_radius: BlastRadius = Field(default_factory=BlastRadius)
    weights: dict[Reversibility, float] = Field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    # -- counts -------------------------------------------------------------

    def count(self, reversibility: Reversibility) -> int:
        return self.counts.get(reversibility, 0)

    @property
    def reversible(self) -> int:
        return self.count(Reversibility.REVERSIBLE)

    @property
    def compensatable(self) -> int:
        return self.count(Reversibility.COMPENSATABLE)

    @property
    def irreversible(self) -> int:
        return self.count(Reversibility.IRREVERSIBLE)

    @property
    def unknown(self) -> int:
        return self.count(Reversibility.UNKNOWN)

    @property
    def recoverable(self) -> int:
        """Actions with some path back, whether restoration or compensation."""
        return self.reversible + self.compensatable

    @property
    def unrecoverable(self) -> int:
        return self.irreversible + self.unknown

    @property
    def recoverable_share(self) -> float:
        """Unweighted percentage with any path back — a looser figure than coverage."""
        if not self.total:
            return 100.0
        return round(self.recoverable / self.total * 100, 1)

    @property
    def fully_reversible_share(self) -> float:
        """Percentage that could be restored exactly — the strictest figure."""
        if not self.total:
            return 100.0
        return round(self.reversible / self.total * 100, 1)

    def summary(self) -> str:
        """A one-paragraph readout, naming what could not be undone."""
        lines = [
            f"reversibility score: {self.coverage}% over {self.total} "
            f"action{'s' if self.total != 1 else ''}",
            f"  {self.reversible} reversible, {self.compensatable} compensatable, "
            f"{self.irreversible} irreversible, {self.unknown} unknown",
            f"  blast radius: {self.blast_radius.describe()}",
        ]
        for item in self.blast_radius.unrecoverable:
            lines.append(f"  cannot be undone: {item.describe()} [{item.reversibility.value}]")
        return "\n".join(lines)


def _classify(
    item: Operation | Action,
    integrations: dict[str, Integration],
) -> tuple[Reversibility, str]:
    """Classify one item and describe what it touches."""
    if isinstance(item, Action):
        # Already recorded: it carries its own classification.
        operation = Operation(
            tool=item.tool, api_call=item.api_call, args=item.args, intent=item.intent
        )
        reversibility = item.reversibility
    else:
        operation = item
        integration = integrations.get(operation.tool)
        reversibility = (
            integration.classify(operation) if integration is not None else Reversibility.UNKNOWN
        )

    integration = integrations.get(operation.tool)
    target = integration.describe_target(operation) if integration is not None else ""
    return reversibility, target


def reversibility_score(
    items: Iterable[Operation | Action],
    integrations: Integration | Iterable[Integration] | None = None,
    *,
    weights: dict[Reversibility, float] | None = None,
) -> ReversibilityScore:
    """Score a proposed plan before any of it runs.

    Args:
        items: The operations being proposed. Recorded :class:`Action`\\ s are
            accepted too and use the classification they already carry.
        integrations: Used to classify :class:`Operation`\\ s and to describe
            their targets. An operation whose tool has no integration is
            ``UNKNOWN`` — the safe reading, not an error.
        weights: Override the credit each class earns. Defaults to
            :data:`DEFAULT_WEIGHTS`.

    Returns:
        A :class:`ReversibilityScore`. An empty plan scores 100%: there is
        nothing to fail to recover.
    """
    from controlz.integrations import Integration as _Integration

    if integrations is None:
        integrations = []
    elif isinstance(integrations, _Integration):
        integrations = [integrations]
    registry = {integration.name: integration for integration in integrations}

    weights = dict(weights) if weights is not None else dict(DEFAULT_WEIGHTS)

    scored: list[ScoredItem] = []
    counts: dict[Reversibility, int] = {}
    radius = BlastRadius()
    targets: list[str] = []

    for item in items:
        reversibility, target = _classify(item, registry)
        entry = ScoredItem(
            tool=item.tool,
            api_call=item.api_call,
            reversibility=reversibility,
            target=target,
            intent=item.intent,
        )
        scored.append(entry)

        counts[reversibility] = counts.get(reversibility, 0) + 1
        radius.tools[entry.tool] = radius.tools.get(entry.tool, 0) + 1
        radius.operations[entry.api_call] = radius.operations.get(entry.api_call, 0) + 1
        if target and target not in targets:
            targets.append(target)
        if reversibility is Reversibility.IRREVERSIBLE:
            radius.irreversible.append(entry)
        elif reversibility is Reversibility.UNKNOWN:
            radius.unknown.append(entry)

    radius.targets = targets
    total = len(scored)
    if total:
        earned = sum(weights.get(entry.reversibility, 0.0) for entry in scored)
        coverage = round(earned / total * 100, 1)
    else:
        coverage = 100.0

    return ReversibilityScore(
        total=total,
        counts=counts,
        coverage=coverage,
        items=scored,
        blast_radius=radius,
        weights=weights,
    )
