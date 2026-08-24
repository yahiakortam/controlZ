"""Rich readouts for the blast radius and the policy verdict.

Kept apart from the logic so that scoring and policy stay usable with no
terminal attached — a service can serialize the same models to JSON.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from controlz.policy import Decision, PolicyDecision
from controlz.score import ReversibilityScore

__all__ = ["REVERSIBILITY_STYLE", "render_blast_radius", "render_decision", "render_plan"]

REVERSIBILITY_STYLE = {
    "reversible": "green",
    "compensatable": "yellow",
    "irreversible": "red",
    "unknown": "magenta",
}

DECISION_STYLE = {
    "allow": "green",
    "require_approval": "yellow",
    "block": "red",
}


def _coverage_style(coverage: float) -> str:
    if coverage >= 90:
        return "green"
    if coverage >= 50:
        return "yellow"
    return "red"


def render_plan(score: ReversibilityScore, console: Console | None = None) -> None:
    """One row per proposed action, coloured by how recoverable it is."""
    console = console or Console()
    table = Table(title="Planned actions", expand=True)
    table.add_column("#", justify="right", style="dim")
    table.add_column("action")
    table.add_column("target")
    table.add_column("reversibility")
    table.add_column("intent", overflow="fold")

    for index, item in enumerate(score.items, start=1):
        style = REVERSIBILITY_STYLE[item.reversibility.value]
        table.add_row(
            str(index),
            f"{item.tool}.{item.api_call}",
            item.target or "—",
            f"[{style}]{item.reversibility.value}[/{style}]",
            item.intent or "",
        )
    console.print(table)


def render_blast_radius(score: ReversibilityScore, console: Console | None = None) -> None:
    """The headline score, the class tally, and what could not be undone."""
    console = console or Console()

    style = _coverage_style(score.coverage)
    headline = Text.assemble(
        ("reversibility score  ", "bold"),
        (f"{score.coverage}%", f"bold {style}"),
        (f"   ({score.recoverable} of {score.total} actions have a way back)", "dim"),
    )

    tally = Table.grid(padding=(0, 2))
    tally.add_column(justify="right")
    tally.add_column()
    for label, count in (
        ("reversible", score.reversible),
        ("compensatable", score.compensatable),
        ("irreversible", score.irreversible),
        ("unknown", score.unknown),
    ):
        if count:
            style_name = REVERSIBILITY_STYLE[label]
            tally.add_row(f"[{style_name}]{count}[/{style_name}]", label)

    body = Table.grid(padding=(1, 0))
    body.add_column()
    body.add_row(headline)
    body.add_row(tally)
    body.add_row(Text(f"blast radius: {score.blast_radius.describe()}"))

    if score.blast_radius.unrecoverable:
        warning = Text("cannot be undone:\n", style="bold red")
        for item in score.blast_radius.unrecoverable:
            warning.append(f"  {item.describe()}  [{item.reversibility.value}]\n", style="red")
        body.add_row(warning)

    console.print(Panel(body, title="Blast radius", border_style=style))


def render_decision(decision: PolicyDecision, console: Console | None = None) -> None:
    """The verdict and every rule that contributed to it."""
    console = console or Console()
    style = DECISION_STYLE[decision.decision.value]
    verdict = decision.decision.value.replace("_", " ").upper()

    table = Table(title="Policy", expand=True)
    table.add_column("rule")
    table.add_column("verdict")
    table.add_column("why", overflow="fold")
    for finding in decision.findings:
        finding_style = DECISION_STYLE[finding.decision.value]
        table.add_row(
            finding.rule,
            f"[{finding_style}]{finding.decision.value}[/{finding_style}]",
            finding.detail,
        )
    if not decision.findings:
        table.add_row("—", "[green]allow[/green]", "nothing proposed")
    console.print(table)

    console.print(Panel(Text(verdict, style=f"bold {style}"), border_style=style, expand=False))
    if decision.decision is Decision.REQUIRE_APPROVAL:
        console.print("[yellow]a human must approve this plan before it runs[/yellow]")
    elif decision.decision is Decision.BLOCK:
        console.print("[red]this plan will not run under the current policy[/red]")
