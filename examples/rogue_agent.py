#!/usr/bin/env python3
"""An agent goes rogue on an issue tracker, and ControlZ puts it back.

This is the whole library in one file: score a plan before it runs, record every
action as the agent takes it, then rewind the session and report honestly on
what could not come back.

    python examples/rogue_agent.py

Runs entirely in memory by default — no credentials, no network, nothing to
clean up. To watch it happen on a real GitHub repository instead:

    export CONTROLZ_GITHUB_TOKEN=ghp_...
    python examples/rogue_agent.py --repo you/throwaway

Only point it at a repository you do not mind mutating. It will open issues,
retitle them, relabel them, comment on them, and close them — then undo all of
it. Read the source before you run that version.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from controlz import Ledger, Policy, PolicyViolation, Session, Tracker, reversibility_score
from controlz.agents import chaos_script, seed_repo
from controlz.integrations.github import TOKEN_ENV_VAR, GitHubIntegration
from controlz.integrations.memory import InMemoryGitHub
from controlz.models import Reversibility
from controlz.render import render_blast_radius
from controlz.tui.theme import REVERSIBILITY_COLOR

console = Console()

CLASS_STYLE = {r.value: REVERSIBILITY_COLOR[r] for r in Reversibility}
OUTCOME_STYLE = {
    "restored": REVERSIBILITY_COLOR[Reversibility.REVERSIBLE],
    "nothing_to_do": "dim",
    "skipped": REVERSIBILITY_COLOR[Reversibility.IRREVERSIBLE],
    "conflict": REVERSIBILITY_COLOR[Reversibility.COMPENSATABLE],
    "blocked": REVERSIBILITY_COLOR[Reversibility.COMPENSATABLE],
    "failed": REVERSIBILITY_COLOR[Reversibility.IRREVERSIBLE],
    "not_attempted": "dim",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        default=None,
        help="a REAL throwaway GitHub repo (owner/name). Omitted: run in memory.",
    )
    parser.add_argument("--ledger", default="rogue-agent.json", help="where to write the session")
    parser.add_argument("--pace", type=float, default=0.12, help="seconds between actions")
    parser.add_argument(
        "--no-rollback", action="store_true", help="leave the mess in place at the end"
    )
    return parser.parse_args(argv)


def build(args: argparse.Namespace):
    """Return (integration, repo, issues) for either backend."""
    if args.repo:
        if not os.environ.get(TOKEN_ENV_VAR):
            raise SystemExit(f"{TOKEN_ENV_VAR} is not set, so --repo cannot work")
        integration = GitHubIntegration()
        console.print(
            f"[bold red]acting on the real repository {args.repo}[/bold red]", highlight=False
        )
        issues = seed_repo(integration.client, args.repo)
        return integration, args.repo, issues

    client = InMemoryGitHub()
    integration = GitHubIntegration(client=client)
    repo = "acme/widgets"
    console.print("[dim]running in memory — no credentials, nothing to clean up[/dim]")
    return integration, repo, seed_repo(client, repo)


def snapshot_world(issues: dict) -> dict:
    return {
        name: {
            "title": issue.title,
            "body": issue.body,
            "state": issue.state,
            "labels": sorted(issue.label_names),
            "comments": len(issue.comments),
        }
        for name, issue in issues.items()
    }


def show_world(before: dict, after: dict, title: str) -> None:
    table = Table(title=title, expand=True)
    table.add_column("issue")
    table.add_column("title", overflow="fold")
    table.add_column("state")
    table.add_column("labels")
    table.add_column("comments", justify="right")
    for name in before:
        row = after[name]
        changed = row != before[name]
        style = "yellow" if changed else "green"
        table.add_row(
            name,
            Text(row["title"], style=style),
            Text(row["state"], style=style),
            Text(",".join(row["labels"]) or "—", style=style),
            Text(str(row["comments"]), style=style),
        )
    console.print(table)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    integration, repo, issues = build(args)
    original = snapshot_world(issues)

    # ------------------------------------------------------------------ plan
    console.print(Rule("[bold]1. What the agent intends to do[/bold]"))
    planned = [step.operation() for step in chaos_script(issues, repo)]
    score = reversibility_score(planned, integration)
    render_blast_radius(score, console)

    # ---------------------------------------------------------------- policy
    console.print(Rule("[bold]2. Whether it is allowed to[/bold]"))
    policy = Policy(name="example", minimum_score=60, max_compensatable=3)
    decision = policy.evaluate(score)
    console.print(decision.summary(), highlight=False)
    console.print(
        "\n[dim]the plan needs approval, so a human decides. standing in for them "
        "and allowing it.[/dim]\n"
    )

    tracker = Tracker(
        Ledger(
            Session(agent="rogue-agent", description="an agent having a bad day"),
            path=args.ledger,
            autosave=True,
        ),
        [integration],
        policy=policy,
        approve=lambda _decision: True,
    )

    # --------------------------------------------------------------- the mess
    console.print(Rule("[bold]3. The agent goes to work[/bold]"))
    for step in chaos_script(issues, repo):
        try:
            action = tracker.track(step.operation()).action
        except PolicyViolation as refused:
            console.print(f"[red]refused[/red] {step.api_call}: {refused.decision.decision.value}")
            continue
        style = CLASS_STYLE[action.reversibility.value]
        console.print(
            f"  [{style}]●[/{style}] {action.api_call:<16} [dim]{action.intent}[/dim]",
            highlight=False,
        )
        time.sleep(args.pace)

    # One thing nothing can undo, so the report has something honest to refuse.
    tracker.ledger.record(
        tool="github",
        api_call="wire_transfer",
        args={"amount": 5000, "to": "vendor@example.com"},
        intent="Pay the invoice the user mentioned.",
        reversibility=Reversibility.IRREVERSIBLE,
        state_before={"sent": False},
        state_after={"sent": True, "confirmation": "wt_9f3a21"},
    )
    console.print(
        f"  [{CLASS_STYLE['irreversible']}]●[/{CLASS_STYLE['irreversible']}] "
        f"{'wire_transfer':<16} [dim]Pay the invoice the user mentioned.[/dim]",
        highlight=False,
    )

    console.print(f"\n[dim]{len(tracker.ledger)} actions recorded to {args.ledger}[/dim]")
    show_world(original, snapshot_world(issues), "The damage")

    if args.no_rollback:
        console.print("[yellow]leaving it as-is (--no-rollback)[/yellow]")
        return 0

    # ------------------------------------------------------------- the rewind
    console.print(Rule("[bold]4. Control Z[/bold]"))
    report = tracker.rollback()

    table = Table(expand=True)
    table.add_column("action")
    table.add_column("outcome")
    table.add_column("why", overflow="fold")
    for entry in report.entries:
        style = OUTCOME_STYLE.get(entry.outcome.value, "white")
        table.add_row(
            entry.api_call,
            Text(entry.outcome.value.replace("_", " "), style=style),
            Text(entry.reason, style="dim"),
        )
    console.print(table)

    restored = snapshot_world(issues)
    show_world(original, restored, "After the rewind")

    # ------------------------------------------------------------- the verdict
    console.print(Rule("[bold]5. What actually came back[/bold]"))
    intact = restored == original
    console.print(
        Panel(
            Text.assemble(
                (
                    f"{len(report.restored)} of {len(report.entries)} actions restored\n",
                    "bold green" if intact else "bold yellow",
                ),
                (
                    f"the issue tracker is {'exactly' if intact else 'not'} as it was\n",
                    "green" if intact else "yellow",
                ),
                ("\nand one thing is not coming back:\n", "dim"),
                (
                    "the wire transfer was sent. ControlZ recorded it, classified it\n"
                    "irreversible, and refused to pretend otherwise.",
                    REVERSIBILITY_COLOR[Reversibility.IRREVERSIBLE],
                ),
            ),
            border_style="green" if intact else "yellow",
        )
    )
    console.print(f"[dim]reload the session any time: Ledger.load({args.ledger!r})[/dim]")
    console.print("[dim]watch it happen instead:  cz watch --demo[/dim]")

    return 0 if intact else 1


if __name__ == "__main__":
    sys.exit(main())
