"""A scripted agent that makes a mess, for demos and tests.

Not part of the library's job — this exists so the TUI has something honest to
watch, and so the chaos scenario is written down once rather than in every test.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

from controlz.models import Action, Operation, Reversibility
from controlz.tracker import Tracker

__all__ = ["ChaosStep", "chaos_script", "run_chaos_agent", "seed_repo"]

DEMO_REPO = "acme/widgets"


class ChaosStep:
    """One planned step: the call to make, and why the agent thinks it should."""

    __slots__ = ("api_call", "args", "intent")

    def __init__(self, api_call: str, intent: str, **args: Any) -> None:
        self.api_call = api_call
        self.intent = intent
        self.args = args

    def operation(self, tool: str = "github") -> Operation:
        return Operation(tool=tool, api_call=self.api_call, args=self.args, intent=self.intent)


def seed_repo(client: Any, repo: str = DEMO_REPO) -> dict[str, Any]:
    """Create the three issues the chaos script expects to find."""
    backing = client.get_repo(repo)
    return {
        "alpha": backing.create_issue(
            title="Flaky build on main", body="Fails about one run in five.", labels=["triage"]
        ),
        "beta": backing.create_issue(
            title="Docs typo in README", body="Second paragraph.", labels=["bug", "docs"]
        ),
        "gamma": backing.create_issue(title="Add dark mode", body="Requested by three users."),
    }


def chaos_script(issues: dict[str, Any], repo: str = DEMO_REPO) -> list[ChaosStep]:
    """Fifteen changes an agent should not have made.

    Deliberately mixed: mostly reversible, several compensatable, one no-op, and
    one that nothing can undo — so a rollback of this session has something
    honest to report.
    """
    alpha, beta, gamma = issues["alpha"].number, issues["beta"].number, issues["gamma"].number
    return [
        ChaosStep(
            "update_issue",
            "Retitle to match the report.",
            repo=repo,
            issue_number=alpha,
            title="WRONG: build is broken",
        ),
        ChaosStep(
            "add_labels",
            "Mark it as not worth fixing.",
            repo=repo,
            issue_number=alpha,
            labels=["wontfix"],
        ),
        ChaosStep("close_issue", "Close it as resolved.", repo=repo, issue_number=alpha),
        ChaosStep(
            "create_comment",
            "Explain the closure.",
            repo=repo,
            issue_number=alpha,
            body="Closing — this looks resolved to me.",
        ),
        ChaosStep(
            "update_issue",
            "Rewrite the description.",
            repo=repo,
            issue_number=beta,
            body="WRONG: rewrote the whole description.",
        ),
        ChaosStep(
            "remove_labels", "Drop the stale label.", repo=repo, issue_number=beta, labels=["docs"]
        ),
        ChaosStep(
            "add_labels",
            "Reclassify it.",
            repo=repo,
            issue_number=beta,
            labels=["invalid", "duplicate"],
        ),
        ChaosStep(
            "create_comment",
            "Ask for more detail.",
            repo=repo,
            issue_number=beta,
            body="Can you clarify which paragraph?",
        ),
        ChaosStep(
            "update_issue",
            "Retitle for clarity.",
            repo=repo,
            issue_number=gamma,
            title="WRONG: dark mode is out of scope",
        ),
        ChaosStep("close_issue", "Close as out of scope.", repo=repo, issue_number=gamma),
        ChaosStep("reopen_issue", "Reopen — that was a mistake.", repo=repo, issue_number=gamma),
        ChaosStep(
            "create_comment",
            "Apologise for the churn.",
            repo=repo,
            issue_number=gamma,
            body="Sorry for the noise here.",
        ),
        ChaosStep(
            "create_issue",
            "Open a tracking issue.",
            repo=repo,
            title="WRONG: umbrella issue for everything",
        ),
        ChaosStep(
            "add_labels",
            "Make sure it is labelled.",
            repo=repo,
            issue_number=alpha,
            labels=["wontfix"],
        ),  # already there: a no-op
    ]


def run_chaos_agent(
    tracker: Tracker,
    issues: dict[str, Any],
    *,
    repo: str = DEMO_REPO,
    delay: float = 0.0,
    on_step: Callable[[Action], None] | None = None,
    include_irreversible: bool = True,
) -> Iterator[Action]:
    """Run the chaos script through a tracker, yielding each recorded action.

    Args:
        delay: Seconds between steps. The TUI uses a small delay so actions
            visibly stream in rather than appearing all at once.
        on_step: Called with each action as it lands.
        include_irreversible: Append one action nothing can undo, so a rollback
            of this session has something it must honestly refuse.
    """
    for step in chaos_script(issues, repo):
        action = tracker.track(step.operation()).action
        if on_step is not None:
            on_step(action)
        yield action
        if delay:
            time.sleep(delay)

    if include_irreversible:
        # Through the ledger, not the session, so autosave fires for this one too.
        action = tracker.ledger.record(
            tool="github",
            api_call="wire_transfer",
            args={"amount": 5000, "to": "vendor@example.com"},
            intent="Pay the invoice the user mentioned.",
            reversibility=Reversibility.IRREVERSIBLE,
            state_before={"sent": False},
            state_after={"sent": True, "confirmation": "wt_9f3a21"},
        )
        if on_step is not None:
            on_step(action)
        yield action
