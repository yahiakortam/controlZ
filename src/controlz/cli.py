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
from controlz.connect import CLIENTS

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

    connect = sub.add_parser(
        "connect",
        help="wire ControlZ in front of a server, for your agent",
        description=(
            "Put ControlZ between your agent and a tool server, and write the "
            "configuration your agent needs. Checks the spec against the real "
            "server before changing anything."
        ),
    )
    connect.add_argument("server", nargs="?", help="which server (see: cz connect --list)")
    connect.add_argument(
        "path", nargs="?", help="for servers that take one, the directory to confine it to"
    )
    connect.add_argument("--list", action="store_true", help="list the servers ControlZ knows")
    connect.add_argument(
        "--client",
        choices=list(CLIENTS),
        help="which agent to configure (default: whichever is detected)",
    )
    connect.add_argument("--policy", type=Path, help="a YAML policy to enforce on every call")
    connect.add_argument("--ledger", type=Path, help="where to record (default: ~/.controlz)")
    connect.add_argument(
        "-e",
        dest="env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="an environment variable the server needs",
    )
    connect.add_argument(
        "--scope",
        default="user",
        choices=["user", "project", "local"],
        help="Claude Code config scope (default: user)",
    )
    connect.add_argument("--name", help="what to call it in the config")

    sub.add_parser(
        "status",
        help="what is connected, and what it has recorded",
    )

    proxy = sub.add_parser(
        "proxy",
        help="record and gate another MCP server",
        description=(
            "Sit between an agent and an MCP server. The agent sees the same tools; "
            "every call it makes is recorded, classified, and gated on the way through."
        ),
    )
    proxy.add_argument(
        "--spec",
        type=Path,
        help=(
            "a bundled spec name (github, filesystem) or a path to YAML. "
            "Without one, nothing is undoable."
        ),
    )
    proxy.add_argument("--ledger", type=Path, help="where to record the session")
    proxy.add_argument(
        "--check",
        action="store_true",
        help="check the spec against the server's real tool list, then exit",
    )
    proxy.add_argument("--policy", type=Path, help="a YAML policy to enforce on every call")
    proxy.add_argument(
        "upstream",
        nargs=argparse.REMAINDER,
        help="the upstream server to launch, after --  (e.g. -- npx -y some-mcp-server)",
    )

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


def _rollback_over_mcp(args: argparse.Namespace, ledger, provenance: dict) -> int:
    """Roll back a session that was recorded through the proxy.

    Relaunches the server it was recorded against, using the command the ledger
    remembers, and undoes through the same spec.
    """
    import asyncio

    from mcp import Client, StdioServerParameters
    from rich.console import Console

    from controlz.mcp import ControlZProxy
    from controlz.specs import load as load_spec

    console = Console()
    command = provenance.get("command") or []
    if not command:
        raise SystemExit("this ledger does not record how to reach its server")

    spec = load_spec(provenance.get("spec") or "")
    console.print(f"[dim]reconnecting to {' '.join(command[:3])}…[/dim]")

    async def run() -> int:
        upstream = StdioServerParameters(command=command[0], args=command[1:], env=dict(os.environ))
        async with Client(upstream) as session:
            proxy = ControlZProxy(session, spec=spec, ledger=ledger)
            report = await proxy.tracker.arollback(dry_run=args.dry_run, force=bool(args.force))
            console.print(report.summary())
            return 0 if report.complete else 1

    return asyncio.run(run())


def _rollback(args: argparse.Namespace) -> int:
    from rich.console import Console

    from controlz.ledger import Ledger
    from controlz.rollback import RollbackEngine

    console = Console()
    ledger = Ledger.load(args.ledger)

    provenance = (ledger.session.metadata or {}).get("controlz") or {}
    if provenance.get("kind") == "mcp":
        return _rollback_over_mcp(args, ledger, provenance)

    engine = RollbackEngine(ledger.session, [_github_integration()])
    report = engine.run(dry_run=args.dry_run, force=bool(args.force))
    console.print(report.summary())
    return 0 if report.complete else 1


def _connect(args: argparse.Namespace) -> int:
    from rich.console import Console

    from controlz.connect import connect
    from controlz.specs import SERVERS

    console = Console()

    if args.list or not args.server:
        console.print("[bold]servers ControlZ ships a spec for[/bold]\n")
        for name, server in sorted(SERVERS.items()):
            extra = " [dim](takes a directory)[/dim]" if server.takes_path else ""
            console.print(f"  [bold]{name}[/bold]{extra}  —  {server.description}")
            for key, what in server.needs.items():
                console.print(f"      [dim]needs {key}: {what}[/dim]")
        console.print("\n[dim]cz connect github        cz connect filesystem ~/project[/dim]")
        return 0

    env = {}
    for pair in args.env:
        key, _, value = pair.partition("=")
        if not value:
            raise SystemExit(f"-e expects KEY=VALUE, got {pair!r}")
        env[key] = value

    result = connect(
        args.server,
        client=args.client,
        path=args.path,
        policy=args.policy,
        ledger=args.ledger,
        env=env,
        scope=args.scope,
        server_name=args.name,
    )

    if result["client"] == "print":
        console.print("[bold]add this to your agent's MCP configuration:[/bold]\n")
        console.print(result["snippet"])
    else:
        console.print(f"[green]connected[/green] {result['name']} → {result['written']}")

    console.print(f"\nrecording to [bold]{result['ledger']}[/bold]")
    console.print("[yellow]restart your agent for it to pick this up.[/yellow]")
    console.print("\nthen:")
    console.print("  [dim]cz status[/dim]                       what it has recorded")
    console.print(f"  [dim]cz watch {result['ledger']}[/dim]    watch it live")
    console.print(f"  [dim]cz rollback {result['ledger']}[/dim] put things back")
    return 0


def _status(args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.table import Table

    from controlz.connect import LEDGER_HOME
    from controlz.ledger import Ledger
    from controlz.score import reversibility_score

    console = Console()
    ledgers = sorted(LEDGER_HOME.glob("*.json")) if LEDGER_HOME.exists() else []
    if not ledgers:
        console.print(f"[dim]nothing recorded yet ({LEDGER_HOME} is empty)[/dim]")
        console.print("\nconnect something with: [bold]cz connect[/bold]")
        return 0

    table = Table(title=f"ControlZ — {LEDGER_HOME}", expand=True)
    table.add_column("ledger")
    table.add_column("actions", justify="right")
    table.add_column("recoverable", justify="right")
    table.add_column("cannot be undone", overflow="fold")

    unrecoverable_total = 0
    for path in ledgers:
        try:
            ledger = Ledger.load(path)
        except Exception as exc:
            table.add_row(path.stem, "—", "—", f"[red]unreadable: {exc}[/red]")
            continue
        score = reversibility_score(ledger.actions)
        unrecoverable = score.blast_radius.unrecoverable
        unrecoverable_total += len(unrecoverable)
        style = "green" if score.coverage >= 90 else "yellow" if score.coverage >= 50 else "red"
        table.add_row(
            path.stem,
            str(score.total),
            f"[{style}]{score.coverage}%[/{style}]",
            ", ".join(i.api_call for i in unrecoverable) or "[dim]—[/dim]",
        )
    console.print(table)
    if unrecoverable_total:
        console.print(
            f"[yellow]{unrecoverable_total} action(s) cannot be taken back.[/yellow] "
            "[dim]cz score <ledger> for detail[/dim]"
        )
    return 0


def _proxy(args: argparse.Namespace) -> int:
    import asyncio

    try:
        from mcp import Client, StdioServerParameters
    except ImportError:  # pragma: no cover - depends on what is installed
        raise SystemExit("the MCP proxy needs the mcp package: pip install -e '.[mcp]'") from None

    from controlz.ledger import Ledger
    from controlz.mcp import ControlZProxy, ServerSpec
    from controlz.models import Session
    from controlz.policy import Policy

    command = [part for part in args.upstream if part != "--"]
    if not command:
        raise SystemExit(
            "give the upstream server to launch after --, "
            "e.g. cz proxy --spec notes.yaml -- npx -y some-mcp-server"
        )

    from controlz.specs import load as load_spec

    try:
        spec = load_spec(args.spec) if args.spec else ServerSpec.unconfigured()
        policy = Policy.from_yaml(args.policy) if args.policy else None
    except FileNotFoundError as missing:
        raise SystemExit(str(missing) if missing.args else f"no such file: {missing}") from None
    ledger = (
        Ledger(
            Session(agent="mcp-proxy", description=f"proxied {spec.tool}"),
            path=args.ledger,
            autosave=True,
        )
        if args.ledger
        else None
    )

    async def run() -> int:
        # Pass our environment through. Without the proxy the agent would have
        # launched this server itself, with exactly this environment — so
        # anything less breaks every server that needs a credential, and the
        # proxy would be changing behaviour rather than observing it.
        upstream = StdioServerParameters(command=command[0], args=command[1:], env=dict(os.environ))
        async with Client(upstream) as session:
            proxy = ControlZProxy(session, spec=spec, ledger=ledger, policy=policy)
            if args.spec:
                proxy.record_provenance(str(args.spec), command)
            if args.check:
                problems = await proxy.check_spec()
                for problem in problems:
                    print(f"  {problem}")
                if problems:
                    print(f"\n{len(problems)} problem(s): rollbacks using those tools will fail.")
                    return 1
                print(f"spec matches the server: {len(spec.operations)} operations checked")
                return 0
            await proxy.serve_stdio()
        return 0

    return asyncio.run(run())


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    handlers = {
        "connect": _connect,
        "status": _status,
        "watch": _watch,
        "score": _score,
        "rollback": _rollback,
        "proxy": _proxy,
    }
    return handlers[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
