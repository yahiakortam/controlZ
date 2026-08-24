"""``controlz`` / ``cz`` — the command line.

cz watch --demo            # the chaos agent, in-memory, no credentials
cz watch run.json          # follow a live ledger file
cz score run.json          # blast-radius readout for a recorded session
cz rollback run.json       # rewind a session from the terminal
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from controlz import __version__

__all__ = ["main"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="controlz", description="A transaction/rollback layer for AI agents."
    )
    parser.add_argument("--version", action="version", version=f"controlz {__version__}")
    sub = parser.add_subparsers(dest="command")

    watch = sub.add_parser("watch", help="open the live action feed")
    watch.add_argument("ledger", nargs="?", type=Path, help="a ledger JSON file to follow")
    watch.add_argument(
        "--demo",
        action="store_true",
        help="run the built-in chaos agent in memory instead (no credentials needed)",
    )
    watch.add_argument(
        "--demo-delay", type=float, default=0.35, help="seconds between the demo agent's actions"
    )
    watch.add_argument(
        "--rewind-pace", type=float, default=0.18, help="seconds between rows during a rewind"
    )

    score = sub.add_parser("score", help="print the blast radius of a recorded session")
    score.add_argument("ledger", type=Path)

    rollback = sub.add_parser("rollback", help="rewind a recorded session")
    rollback.add_argument("ledger", type=Path)
    rollback.add_argument(
        "--dry-run", action="store_true", help="report what would happen, change nothing"
    )
    rollback.add_argument(
        "--force", action="store_true", help="roll back even where the live state has drifted"
    )
    return parser


def _github_integration():
    """Build a GitHub integration from the environment, or explain why not."""
    from controlz.integrations.github import TOKEN_ENV_VAR, GitHubIntegration

    if not os.environ.get(TOKEN_ENV_VAR):
        raise SystemExit(f"{TOKEN_ENV_VAR} is not set, so ControlZ cannot reach GitHub")
    return GitHubIntegration()


def _watch(args: argparse.Namespace) -> int:
    from controlz.ledger import Ledger
    from controlz.models import Session
    from controlz.tracker import Tracker
    from controlz.tui import ControlZApp

    if args.demo:
        from controlz.integrations.github import GitHubIntegration
        from controlz.integrations.memory import InMemoryGitHub

        tracker = Tracker(
            Ledger(Session(agent="chaos-demo", description="scripted demo run")),
            [GitHubIntegration(client=InMemoryGitHub())],
        )
        app = ControlZApp(
            tracker,
            demo=True,
            demo_delay=args.demo_delay,
            rewind_pace=args.rewind_pace,
        )
    else:
        if args.ledger is None:
            raise SystemExit("give a ledger file to follow, or pass --demo")
        ledger = Ledger.load(args.ledger) if args.ledger.exists() else Ledger(path=args.ledger)
        tracker = Tracker(ledger, [_github_integration()])
        app = ControlZApp(tracker, ledger_path=args.ledger, rewind_pace=args.rewind_pace)

    app.run()
    return 0


def _score(args: argparse.Namespace) -> int:
    from rich.console import Console

    from controlz.ledger import Ledger
    from controlz.render import render_blast_radius, render_plan
    from controlz.score import reversibility_score

    ledger = Ledger.load(args.ledger)
    score = reversibility_score(ledger.actions)
    console = Console()
    render_plan(score, console)
    render_blast_radius(score, console)
    return 0


def _rollback(args: argparse.Namespace) -> int:
    from rich.console import Console

    from controlz.ledger import Ledger
    from controlz.rollback import RollbackEngine

    console = Console()
    ledger = Ledger.load(args.ledger)
    engine = RollbackEngine(ledger.session, [_github_integration()])
    report = engine.run(dry_run=args.dry_run, force=bool(args.force))
    console.print(report.summary())
    return 0 if report.complete else 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    handlers = {"watch": _watch, "score": _score, "rollback": _rollback}
    return handlers[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
