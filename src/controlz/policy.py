"""Policy: what an agent is allowed to do before it does it.

A :class:`Policy` turns a :class:`~controlz.score.ReversibilityScore` into one
of three answers — allow, require approval, or block — by applying rules to the
plan's reversibility profile. Rules are ordinary configuration, in YAML or a
dict, so the people who own the blast radius can set them without touching code.

    policy = Policy.from_yaml("controlz-policy.yaml")
    decision = policy.evaluate(reversibility_score(planned, github))
    if decision.blocked:
        raise SystemExit(decision.summary())

The strictest matching rule wins: any rule that blocks blocks the whole task.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from controlz.models import Operation
from controlz.score import ReversibilityScore, reversibility_score

if TYPE_CHECKING:  # pragma: no cover - typing only
    from controlz.integrations import Integration

__all__ = [
    "Decision",
    "Policy",
    "PolicyDecision",
    "PolicyGate",
    "PolicyViolation",
    "RuleFinding",
]


class PolicyViolation(RuntimeError):
    """Raised when a blocked plan is executed anyway.

    Carries the :class:`PolicyDecision` that caused it, so a caller can report
    the reasons rather than just the failure.
    """

    def __init__(self, decision: PolicyDecision) -> None:
        super().__init__(decision.summary())
        self.decision = decision


class Decision(str, Enum):
    """What a policy says about a plan.

    Ordered by strictness: :meth:`strictest` resolves several findings into one.
    """

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    BLOCK = "block"

    @property
    def rank(self) -> int:
        return {"allow": 0, "require_approval": 1, "block": 2}[self.value]

    @classmethod
    def strictest(cls, decisions: Iterable[Decision]) -> Decision:
        return max(decisions, key=lambda d: d.rank, default=cls.ALLOW)


class RuleFinding(BaseModel):
    """One rule's verdict, and why."""

    model_config = ConfigDict(extra="forbid")

    rule: str = Field(..., description="Which policy setting produced this.")
    decision: Decision
    detail: str = Field(..., description="Plain-language explanation.")

    def describe(self) -> str:
        return f"[{self.decision.value}] {self.rule}: {self.detail}"


class PolicyDecision(BaseModel):
    """The verdict on a plan: the strictest finding, plus every finding."""

    model_config = ConfigDict(extra="forbid")

    decision: Decision
    score: ReversibilityScore
    findings: list[RuleFinding] = Field(default_factory=list)

    @property
    def allowed(self) -> bool:
        """True only when the plan may proceed with no human in the loop."""
        return self.decision is Decision.ALLOW

    @property
    def blocked(self) -> bool:
        return self.decision is Decision.BLOCK

    @property
    def needs_approval(self) -> bool:
        return self.decision is Decision.REQUIRE_APPROVAL

    @property
    def blocking_findings(self) -> list[RuleFinding]:
        return [f for f in self.findings if f.decision is Decision.BLOCK]

    @property
    def approval_findings(self) -> list[RuleFinding]:
        return [f for f in self.findings if f.decision is Decision.REQUIRE_APPROVAL]

    def summary(self) -> str:
        """The verdict, the score, and every reason behind it."""
        lines = [
            f"{self.decision.value.replace('_', ' ')} — "
            f"reversibility score {self.score.coverage}% over {self.score.total} actions",
            f"  blast radius: {self.score.blast_radius.describe()}",
        ]
        lines.extend(f"  {finding.describe()}" for finding in self.findings)
        return "\n".join(lines)


class Policy(BaseModel):
    """Rules for what an agent may do unsupervised.

    The defaults are deliberately cautious: reversible work proceeds, anything
    irreversible or unclassified wants a human, and a plan that is mostly
    unrecoverable is blocked outright.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str = Field(default="default", description="Label for this policy, for reporting.")

    minimum_score: float = Field(
        default=50.0,
        ge=0.0,
        le=100.0,
        description="Block the task when coverage falls below this percentage.",
    )
    below_minimum_score: Decision = Field(
        default=Decision.BLOCK, description="What to do when the score is too low."
    )

    on_reversible: Decision = Field(
        default=Decision.ALLOW, description="Verdict when the plan contains reversible actions."
    )
    on_compensatable: Decision = Field(
        default=Decision.ALLOW, description="Verdict for compensatable actions within the limit."
    )
    max_compensatable: int | None = Field(
        default=None,
        ge=0,
        description="How many compensatable actions are tolerated before escalating.",
    )
    over_compensatable_limit: Decision = Field(
        default=Decision.REQUIRE_APPROVAL,
        description="Verdict once max_compensatable is exceeded.",
    )
    on_irreversible: Decision = Field(
        default=Decision.REQUIRE_APPROVAL,
        description="Verdict when the plan contains anything irreversible.",
    )
    on_unknown: Decision = Field(
        default=Decision.REQUIRE_APPROVAL,
        description="Verdict when the plan contains unclassified actions.",
    )
    max_targets: int | None = Field(
        default=None, ge=0, description="How many distinct targets the plan may touch."
    )
    over_target_limit: Decision = Field(
        default=Decision.REQUIRE_APPROVAL, description="Verdict once max_targets is exceeded."
    )

    @field_validator(
        "below_minimum_score",
        "on_reversible",
        "on_compensatable",
        "over_compensatable_limit",
        "on_irreversible",
        "on_unknown",
        "over_target_limit",
        mode="before",
    )
    @classmethod
    def _normalize_decision(cls, value: Any) -> Any:
        """Accept ``require-approval`` and ``REQUIRE APPROVAL`` in config files."""
        if isinstance(value, str):
            return value.strip().lower().replace("-", "_").replace(" ", "_")
        return value

    # -- loading ------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Policy:
        """Build a policy from a plain dict."""
        return cls.model_validate(data)

    @classmethod
    def from_yaml(cls, path: str | os.PathLike[str]) -> Policy:
        """Load a policy from a YAML file.

        An empty file yields the default policy.
        """
        import yaml

        text = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a YAML mapping, got {type(data).__name__}")
        return cls.from_dict(data)

    def to_yaml(self) -> str:
        """Serialize this policy back to YAML."""
        import yaml

        return yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False)

    # -- evaluation ---------------------------------------------------------

    def evaluate(self, score: ReversibilityScore) -> PolicyDecision:
        """Apply every rule to a scored plan. The strictest finding wins."""
        findings: list[RuleFinding] = []

        if score.total and score.coverage < self.minimum_score:
            findings.append(
                RuleFinding(
                    rule="minimum_score",
                    decision=self.below_minimum_score,
                    detail=(
                        f"reversibility score {score.coverage}% is below the "
                        f"{self.minimum_score}% required"
                    ),
                )
            )

        if score.irreversible:
            names = ", ".join(item.describe() for item in score.blast_radius.irreversible)
            findings.append(
                RuleFinding(
                    rule="on_irreversible",
                    decision=self.on_irreversible,
                    detail=f"{score.irreversible} irreversible action(s): {names}",
                )
            )

        if score.unknown:
            names = ", ".join(item.describe() for item in score.blast_radius.unknown)
            findings.append(
                RuleFinding(
                    rule="on_unknown",
                    decision=self.on_unknown,
                    detail=(
                        f"{score.unknown} unclassified action(s), treated as "
                        f"potentially irreversible: {names}"
                    ),
                )
            )

        if score.compensatable:
            over = (
                self.max_compensatable is not None and score.compensatable > self.max_compensatable
            )
            findings.append(
                RuleFinding(
                    rule="max_compensatable" if over else "on_compensatable",
                    decision=self.over_compensatable_limit if over else self.on_compensatable,
                    detail=(
                        f"{score.compensatable} compensatable action(s), over the "
                        f"limit of {self.max_compensatable}"
                        if over
                        else f"{score.compensatable} compensatable action(s) within limits"
                    ),
                )
            )

        if score.reversible:
            findings.append(
                RuleFinding(
                    rule="on_reversible",
                    decision=self.on_reversible,
                    detail=f"{score.reversible} reversible action(s)",
                )
            )

        if self.max_targets is not None and score.blast_radius.target_count > self.max_targets:
            findings.append(
                RuleFinding(
                    rule="max_targets",
                    decision=self.over_target_limit,
                    detail=(
                        f"touches {score.blast_radius.target_count} targets, over the "
                        f"limit of {self.max_targets}"
                    ),
                )
            )

        decision = Decision.strictest(finding.decision for finding in findings)
        return PolicyDecision(decision=decision, score=score, findings=findings)


class PolicyGate:
    """Scores a plan, applies a policy, and enforces the verdict.

    >>> gate = PolicyGate(policy, [github])                    # doctest: +SKIP
    >>> decision = gate.check(planned_operations)              # doctest: +SKIP
    >>> gate.enforce(planned_operations, approve=ask_a_human)  # doctest: +SKIP
    """

    def __init__(
        self,
        policy: Policy | None = None,
        integrations: Integration | Iterable[Integration] | None = None,
    ) -> None:
        self.policy = policy if policy is not None else Policy()
        self.integrations = integrations

    def score(self, operations: Iterable[Operation]) -> ReversibilityScore:
        return reversibility_score(operations, self.integrations)

    def check(self, operations: Iterable[Operation]) -> PolicyDecision:
        """Score the plan and apply the policy, changing nothing."""
        return self.policy.evaluate(self.score(operations))

    def enforce(
        self,
        operations: Iterable[Operation],
        *,
        approve: Any = None,
    ) -> PolicyDecision:
        """Check the plan and act on the verdict.

        Raises :class:`PolicyViolation` when the plan is blocked, or when it
        needs approval and none was given. ``approve`` is called with the
        decision and should return ``True`` to proceed.

        Returns the decision when the plan may go ahead.
        """
        decision = self.check(operations)
        if decision.blocked:
            raise PolicyViolation(decision)
        if decision.needs_approval and (approve is None or not approve(decision)):
            raise PolicyViolation(decision)
        return decision
