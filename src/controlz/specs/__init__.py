"""Bundled specs: what ControlZ knows about common MCP servers.

Shipped inside the package so that using one is a name rather than a path::

    cz connect github
    cz proxy --spec github -- npx -y @modelcontextprotocol/server-github

Each one is a hand-written judgement about what can be taken back, verified
against the real server. Read the file before trusting it — the reasoning is
written down in the comments, and the reasoning is the part that matters.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from controlz.mcp.integration import ServerSpec

__all__ = ["SERVERS", "KnownServer", "bundled", "load", "resolve"]

_HERE = Path(__file__).parent


class KnownServer:
    """A server ControlZ ships a spec for, and how to launch it."""

    __slots__ = ("command", "description", "name", "needs", "takes_path")

    def __init__(
        self,
        name: str,
        command: list[str],
        description: str,
        needs: dict[str, str] | None = None,
        takes_path: bool = False,
    ) -> None:
        self.name = name
        self.command = command
        self.description = description
        #: Environment variables the server needs, mapped to what they are for.
        self.needs = needs or {}
        #: Whether the server is launched with a directory to confine it to.
        self.takes_path = takes_path

    @property
    def spec_path(self) -> Path:
        return _HERE / f"{self.name}.yaml"

    def launch_command(self, path: str | None = None) -> list[str]:
        command = list(self.command)
        if self.takes_path:
            if not path:
                raise ValueError(f"{self.name} must be given a directory to confine it to")
            command.append(str(Path(path).expanduser().resolve()))
        return command


#: The servers ControlZ ships a verified spec for.
SERVERS: dict[str, KnownServer] = {
    "filesystem": KnownServer(
        name="filesystem",
        command=["npx", "-y", "@modelcontextprotocol/server-filesystem"],
        description="Files an agent writes, edits, and moves",
        takes_path=True,
    ),
    "github": KnownServer(
        name="github",
        command=["npx", "-y", "@modelcontextprotocol/server-github"],
        description="GitHub issues and pull requests",
        needs={"GITHUB_PERSONAL_ACCESS_TOKEN": "a GitHub token with repo access"},
    ),
}


def bundled() -> list[str]:
    """The names of the specs that ship with ControlZ."""
    return sorted(SERVERS)


def resolve(name_or_path: str | os.PathLike[str]) -> Path:
    """Turn ``"github"`` or a file path into a path to a spec file.

    A bundled name wins over a same-named file in the working directory, so
    ``--spec github`` means the same thing wherever it is run.
    """
    text = str(name_or_path)
    if text in SERVERS:
        return SERVERS[text].spec_path
    candidate = Path(text).expanduser()
    if candidate.exists():
        return candidate
    raise FileNotFoundError(
        f"no spec named {text!r}. Bundled: {', '.join(bundled())}, or give a path to a YAML file."
    )


def load(name_or_path: str | os.PathLike[str]) -> ServerSpec:
    """Load a bundled spec by name, or any spec by path."""
    from controlz.mcp.integration import ServerSpec

    return ServerSpec.from_yaml(resolve(name_or_path))
