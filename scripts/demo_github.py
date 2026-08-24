#!/usr/bin/env python3
"""Run a real GitHub operation through ControlZ and show what lands in the ledger.

    export CONTROLZ_GITHUB_TOKEN=ghp_...
    python scripts/demo_github.py --repo you/throwaway

By default it opens an issue, labels it, comments on it, and closes it — four
tracked actions — then prints the ledger and writes it to disk. Pass
``--rollback`` to unwind the whole session afterwards.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from controlz import Ledger, Operation, Session, Tracker
from controlz.integrations.github import TOKEN_ENV_VAR, GitHubIntegration

console = Console()

REVERSIBILITY_STYLE = {
    "reversible": "green",
    "compensatable": "yellow",
    "irreversible": "red",
    "unknown": "magenta",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        default=os.environ.get("CONTROLZ_TEST_REPO"),
        help="owner/name of a throwaway repo (default: $CONTROLZ_TEST_REPO)",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=Path("controlz-demo.json"),
        help="where to write the session (default: controlz-demo.json)",
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="undo every recorded action after showing the ledger",
    )
    return parser.parse_args(argv)


def render_session(session: Session) -> None:
    table = Table(title=f"ControlZ session {session.session_id[:8]}", expand=True)
    table.add_column("#", justify="right", style="dim")
    table.add_column("api_call")
    table.add_column("reversibility")
    table.add_column("before → after")
    table.add_column("rollback")

    for index, action in enumerate(session.actions, start=1):
        style = REVERSIBILITY_STYLE[action.reversibility.value]
        plan = action.rollback_plan
        rollback = "—"
        if plan is not None:
            rollback = plan.strategy if plan.is_executable else f"{plan.strategy} (nothing to run)"
        table.add_row(
            str(index),
            action.api_call,
            f"[{style}]{action.reversibility.value}[/{style}]",
            f"{summarize(action.state_before)} → {summarize(action.state_after)}",
            rollback,
        )
    console.print(table)


def summarize(state: dict | None) -> str:
    """One-line gist of a snapshot, for the table."""
    if state is None:
        return "[dim]none[/dim]"
    if "error" in state:
        return "[red]error[/red]"
    issue = state.get("issue")
    if "issue" in state:
        if issue is None:
            return "[dim]no issue[/dim]"
        labels = ",".join(issue.get("labels") or []) or "-"
        return f"#{issue['issue_number']} {issue['state']} [{labels}]"
    comment = state.get("comment")
    if comment is None:
        return "[dim]no comment[/dim]"
    return f"comment {comment['comment_id']}"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.repo:
        console.print("[red]--repo (or $CONTROLZ_TEST_REPO) is required[/red]")
        return 2
    if not os.environ.get(TOKEN_ENV_VAR):
        console.print(f"[red]{TOKEN_ENV_VAR} is not set[/red]")
        return 2

    tag = uuid.uuid4().hex[:8]
    tracker = Tracker(
        Ledger(Session(agent="controlz-demo", description=f"demo run {tag}"), path=args.ledger),
        [GitHubIntegration()],
    )
    gh = tracker.tool("github")

    console.rule(f"[bold]Acting on {args.repo}[/bold]")

    issue = gh.create_issue(
        repo=args.repo,
        title=f"[controlz] demo {tag}",
        body="Opened by the ControlZ demo script. Safe to close.",
        _intent="Open the issue the demo is about.",
    )
    console.print(f"opened issue #{issue.number} → {issue.html_url}")
    created_id = tracker.last_action().operation_id

    gh.add_labels(
        repo=args.repo,
        issue_number=issue.number,
        labels=["controlz-demo"],
        _intent="Tag the issue so it is easy to find later.",
    )
    console.print("added label 'controlz-demo'")

    # The long form: an explicit Operation, with intent and a dependency on the
    # issue it comments on.
    tracker.track(
        Operation(
            tool="github",
            api_call="create_comment",
            args={
                "repo": args.repo,
                "issue_number": issue.number,
                "body": "ControlZ recorded this comment, and knows how to delete it.",
            },
            intent="Show a compensatable action with a real undo.",
        ),
        dependencies=[created_id],
    )
    console.print("posted a comment")

    gh.close_issue(
        repo=args.repo,
        issue_number=issue.number,
        _intent="Finish the demo with the issue closed.",
    )
    console.print("closed the issue")

    console.rule("[bold]Ledger[/bold]")
    render_session(tracker.ledger.session)

    first = tracker.ledger.actions[0]
    console.print(
        Panel(
            Syntax(
                json.dumps(first.model_dump(mode="json"), indent=2),
                "json",
                theme="ansi_dark",
                word_wrap=True,
            ),
            title="Action 1, in full",
            border_style="dim",
        )
    )

    path = tracker.ledger.save()
    console.print(f"\nwrote {len(tracker.ledger)} actions to [bold]{path}[/bold]")
    console.print(f"reload with: [dim]Ledger.load({str(path)!r})[/dim]")

    if args.rollback:
        console.rule("[bold]Rolling back[/bold]")
        undone = tracker.rollback_session(stop_on_error=False)
        for action in undone:
            console.print(f"undid {action.api_call} ({action.rollback_plan.strategy})")
        console.print(f"[green]rolled back {len(undone)} of {len(tracker.ledger)} actions[/green]")
        console.print("[dim]the issue itself stays closed — GitHub cannot delete issues[/dim]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
