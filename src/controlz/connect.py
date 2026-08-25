"""``cz connect`` — put ControlZ in front of a server, without the ceremony.

Wiring a proxy by hand means composing a nested command, finding the right
config file for whichever agent you use, and restarting it. That is four
chances to get something subtly wrong, and the failure mode is silence: the
agent works fine and records nothing.

This does it for you, checks the spec against the real server before writing
anything, and prints the one manual step that is genuinely left.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from controlz.specs import SERVERS, KnownServer, bundled

__all__ = ["CLIENTS", "LEDGER_HOME", "connect", "proxy_command"]

#: Where ledgers go unless told otherwise. One per connected server, so that
#: several agents recording at once do not share a file — a ledger has one writer.
LEDGER_HOME = Path(os.environ.get("CONTROLZ_HOME", Path.home() / ".controlz"))

#: Agents that can be wired up, and how.
CLIENTS = {
    "claude-code": "Claude Code (the CLI)",
    "claude-desktop": "Claude Desktop",
    "print": "print the config to paste yourself",
}


def cz_executable() -> str:
    """The `cz` a config should point at.

    Order matters here. An agent launches this command with no shell and no
    activated virtualenv, so a `cz` found on PATH may be a version manager's
    shim that resolves to a different interpreter — one without ControlZ
    installed. The console script sitting beside the running interpreter is the
    one that is certain to work, so it is preferred over anything on PATH.
    """
    beside = Path(sys.executable).parent / "cz"
    if beside.exists():
        return str(beside)

    invoked = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if invoked is not None and invoked.name == "cz" and invoked.exists():
        return str(invoked.resolve())

    found = shutil.which("cz")
    if found:
        return found
    return sys.executable


def proxy_command(
    server: KnownServer,
    *,
    ledger: Path,
    path: str | None = None,
    policy: Path | None = None,
) -> list[str]:
    """The full command an agent should launch to reach this server through ControlZ."""
    executable = cz_executable()
    command = [executable]
    if not executable.endswith("cz"):
        command += ["-m", "controlz.cli"]
    command += ["proxy", "--spec", server.name, "--ledger", str(ledger)]
    if policy is not None:
        command += ["--policy", str(policy)]
    command += ["--", *server.launch_command(path)]
    return command


def claude_desktop_config_path() -> Path:
    """Where Claude Desktop keeps its MCP servers, per platform."""
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    if sys.platform == "win32":  # pragma: no cover - not exercised here
        return (
            Path(os.environ.get("APPDATA", Path.home())) / "Claude" / "claude_desktop_config.json"
        )
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"  # pragma: no cover


def detect_client() -> str:
    """Guess which agent to wire up, preferring one we can configure directly."""
    if shutil.which("claude"):
        return "claude-code"
    if claude_desktop_config_path().exists():
        return "claude-desktop"
    return "print"


def _server_entry(command: list[str], env: dict[str, str]) -> dict:
    entry: dict = {"command": command[0], "args": command[1:]}
    if env:
        entry["env"] = env
    return entry


def _write_claude_desktop(name: str, command: list[str], env: dict[str, str]) -> Path:
    path = claude_desktop_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    config = {}
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path} is not valid JSON, so it was left alone: {exc}") from None
    config.setdefault("mcpServers", {})[name] = _server_entry(command, env)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def _add_to_claude_code(name: str, command: list[str], env: dict[str, str], scope: str) -> None:
    argv = ["claude", "mcp", "add", name, "--scope", scope]
    for key, value in env.items():
        argv += ["-e", f"{key}={value}"]
    argv += ["--", *command]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            f"`claude mcp add` failed:\n{result.stderr.strip() or result.stdout.strip()}"
        )


def resolve_env(server: KnownServer, provided: dict[str, str]) -> dict[str, str]:
    """Fill the server's required variables from what was given or the environment."""
    env: dict[str, str] = {}
    missing: list[str] = []
    for key, what in server.needs.items():
        value = provided.get(key) or os.environ.get(key)
        if value:
            env[key] = value
        else:
            missing.append(f"{key} ({what})")
    if missing:
        raise SystemExit(
            "this server needs: " + "; ".join(missing) + "\nSet it in your environment, "
            "or pass it with -e KEY=value."
        )
    return env


def connect(
    name: str,
    *,
    client: str | None = None,
    path: str | None = None,
    policy: Path | None = None,
    ledger: Path | None = None,
    env: dict[str, str] | None = None,
    scope: str = "user",
    server_name: str | None = None,
) -> dict:
    """Wire a bundled server up behind ControlZ.

    Returns a description of what was done, so the caller can print it however
    it likes and tests can assert on it without parsing output.
    """
    if name not in SERVERS:
        raise SystemExit(f"no bundled spec named {name!r}. Available: {', '.join(bundled())}")
    server = SERVERS[name]

    if server.takes_path and not path:
        raise SystemExit(
            f"{name} needs a directory to confine it to, e.g. `cz connect {name} ~/project`"
        )

    resolved_env = resolve_env(server, env or {})
    ledger = ledger or (LEDGER_HOME / f"{name}.json")
    ledger.parent.mkdir(parents=True, exist_ok=True)

    command = proxy_command(server, ledger=ledger, path=path, policy=policy)
    label = server_name or f"controlz-{name}"
    client = client or detect_client()

    written: str | None = None
    if client == "claude-code":
        _add_to_claude_code(label, command, resolved_env, scope)
        written = "Claude Code"
    elif client == "claude-desktop":
        written = str(_write_claude_desktop(label, command, resolved_env))
    elif client != "print":
        raise SystemExit(f"unknown client {client!r}. Choose from: {', '.join(CLIENTS)}")

    return {
        "name": label,
        "client": client,
        "written": written,
        "ledger": ledger,
        "command": command,
        "env": resolved_env,
        "snippet": json.dumps(
            {"mcpServers": {label: _server_entry(command, resolved_env)}}, indent=2
        ),
    }
