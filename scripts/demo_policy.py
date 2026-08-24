#!/usr/bin/env python3
"""Score a planned task, print its blast radius, and enforce a policy.

Nothing here touches GitHub: scoring and policy run entirely before execution,
which is the point — the question "how much of this could we take back?" is only
useful while it is still hypothetical.

    python scripts/demo_policy.py                        # a mixed, allowable plan
    python scripts/demo_policy.py --plan risky           # needs approval
    python scripts/demo_policy.py --plan reckless        # blocked
    python scripts/demo_policy.py --policy controlz-policy.yaml --plan risky --approve
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.rule import Rule

from controlz import Operation, Policy, PolicyGate, PolicyViolation
from controlz.integrations.github import GitHubIntegration
from controlz.render import render_blast_radius, render_decision, render_plan

console = Console()
REPO = "acme/widgets"


def plan(name: str) -> list[Operation]:
    """Three planned tasks, from routine to alarming."""
    if name == "safe":
        return [
            Operation(
                tool="github",
                api_call="update_issue",
                args={"repo": REPO, "issue_number": 12, "title": "Fix the flaky build"},
                intent="Retitle to match what the user reported.",
            ),
            Operation(
                tool="github",
                api_call="add_labels",
                args={"repo": REPO, "issue_number": 12, "labels": ["bug", "ci"]},
                intent="Route it to the right team.",
            ),
            Operation(
                tool="github",
                api_call="close_issue",
                args={"repo": REPO, "issue_number": 9},
                intent="This duplicate is already fixed.",
            ),
        ]

    if name == "risky":
        return [
            *plan("safe"),
            Operation(
                tool="github",
                api_call="create_issue",
                args={"repo": REPO, "title": "Track the flaky build"},
                intent="Open a tracking issue for the wider problem.",
            ),
            Operation(
                tool="github",
                api_call="create_comment",
                args={"repo": REPO, "issue_number": 12, "body": "Working on this."},
                intent="Tell the reporter someone is on it.",
            ),
            Operation(
                tool="github",
                api_call="create_comment",
                args={"repo": REPO, "issue_number": 9, "body": "Closing as duplicate."},
                intent="Explain the closure.",
            ),
            Operation(
                tool="github",
                api_call="create_comment",
                args={"repo": REPO, "issue_number": 3, "body": "Related to #12."},
                intent="Cross-link the older report.",
            ),
        ]

    if name == "reckless":
        return [
            *plan("risky"),
            Operation(
                tool="github",
                api_call="delete_repository",
                args={"repo": REPO},
                intent="Clean up what looks like a stale fork.",
            ),
            Operation(
                tool="stripe",
                api_call="refund",
                args={"resource": "ch_1234", "amount": 4200},
                intent="The user mentioned a billing problem.",
            ),
        ]

    raise SystemExit(f"unknown plan {name!r}; choose safe, risky, or reckless")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan",
        default="risky",
        choices=["safe", "risky", "reckless"],
        help="which example task to score (default: risky)",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=None,
        help="a YAML policy file (default: the built-in cautious defaults)",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="stand in for the human who approves a plan that needs one",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    policy = Policy.from_yaml(args.policy) if args.policy else Policy()
    gate = PolicyGate(policy, GitHubIntegration(token="not-used-nothing-executes"))
    proposed = plan(args.plan)

    console.print(Rule(f"[bold]Planned task: {args.plan}[/bold] — policy {policy.name!r}"))

    score = gate.score(proposed)
    render_plan(score, console)
    render_blast_radius(score, console)

    decision = gate.check(proposed)
    render_decision(decision, console)

    console.print(Rule("[bold]Enforcement[/bold]"))
    try:
        gate.enforce(proposed, approve=(lambda d: True) if args.approve else None)
    except PolicyViolation as violation:
        verdict = violation.decision
        if verdict.blocked:
            console.print("[red]refused to run this task[/red]")
        else:
            console.print(
                "[yellow]held for approval — rerun with --approve to stand in "
                "for the human[/yellow]"
            )
        for finding in verdict.blocking_findings + verdict.approval_findings:
            console.print(f"  [dim]{finding.describe()}[/dim]")
        return 1

    console.print(f"[green]cleared to run {len(proposed)} actions[/green]")
    if decision.needs_approval:
        console.print("[dim]with explicit human approval[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
