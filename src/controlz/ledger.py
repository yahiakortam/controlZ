"""Append-only ledger: the durable record of what an agent did.

The ledger owns one :class:`~controlz.models.Session` and persists it to a
single JSON file. Writes are atomic — a crash mid-save leaves the previous
good file in place rather than a truncated one.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from controlz.models import Action, Session

__all__ = ["Ledger", "LedgerError"]

SCHEMA_VERSION = 1


class LedgerError(RuntimeError):
    """Raised when a ledger file is missing, malformed, or unreadable."""


class Ledger:
    """Appends actions to a session and persists it as JSON.

    >>> ledger = Ledger(path="run.json")
    >>> _ = ledger.record(tool="github", api_call="create_issue")
    >>> ledger.save()                       # doctest: +SKIP
    >>> Ledger.load("run.json").session     # doctest: +SKIP
    """

    def __init__(
        self,
        session: Session | None = None,
        path: str | os.PathLike[str] | None = None,
        *,
        autosave: bool = False,
    ) -> None:
        if autosave and path is None:
            raise ValueError("autosave requires a path")
        self.session = session if session is not None else Session()
        self.path = Path(path) if path is not None else None
        self.autosave = autosave

    # -- recording ---------------------------------------------------------

    def append(self, action: Action) -> Action:
        """Append an already-built action to the session."""
        self.session.append(action)
        self._maybe_autosave()
        return action

    def record(self, **kwargs: Any) -> Action:
        """Build an action for this session from keyword arguments and append it."""
        action = self.session.record(**kwargs)
        self._maybe_autosave()
        return action

    def _maybe_autosave(self) -> None:
        if self.autosave:
            self.save()

    # -- persistence -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serializable snapshot of the ledger, including the schema version."""
        return {
            "schema_version": SCHEMA_VERSION,
            "session": self.session.model_dump(mode="json"),
        }

    def _target_for(self, path: str | os.PathLike[str] | None) -> Path:
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ValueError("no path given and this ledger has no default path")
        return target

    def _payload(self) -> str:
        """Serialize the session to JSON text.

        Kept separate from the write so the async path can serialize on the
        event loop — where no other coroutine can mutate the action list
        mid-dump — and only hand the finished bytes to a worker thread.
        """
        return json.dumps(self.to_dict(), indent=2, sort_keys=False)

    @staticmethod
    def _write(target: Path, payload: str) -> None:
        """Write ``payload`` to ``target`` atomically.

        The write goes to a temporary file in the same directory and is then
        renamed over the target, so readers never observe a partial file.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def save(self, path: str | os.PathLike[str] | None = None) -> Path:
        """Write the session to ``path`` (or the ledger's own path) as JSON.

        Atomic: a crash mid-write leaves the previous good file in place.
        Returns the path written.
        """
        target = self._target_for(path)
        self._write(target, self._payload())
        if path is not None:
            self.path = target
        return target

    async def asave(self, path: str | os.PathLike[str] | None = None) -> Path:
        """Write the session without blocking the event loop.

        The JSON is built here, on the loop, and only the file write is handed
        to a thread — so a concurrent ``atrack`` cannot append to the action
        list while it is being serialized.
        """
        target = self._target_for(path)
        payload = self._payload()
        await asyncio.to_thread(self._write, target, payload)
        if path is not None:
            self.path = target
        return target

    async def aappend(self, action: Action) -> Action:
        """Append an action, persisting without blocking the loop if autosaving."""
        self.session.append(action)
        if self.autosave:
            await self.asave()
        return action

    async def arecord(self, **kwargs: Any) -> Action:
        """Build an action for this session from keyword arguments and append it."""
        return await self.aappend(Action(session_id=self.session.session_id, **kwargs))

    @classmethod
    async def aload(cls, path: str | os.PathLike[str], *, autosave: bool = False) -> Ledger:
        """Read a ledger back from disk without blocking the event loop."""
        return await asyncio.to_thread(cls.load, path, autosave=autosave)

    @classmethod
    def load(cls, path: str | os.PathLike[str], *, autosave: bool = False) -> Ledger:
        """Read a ledger back from a JSON file written by :meth:`save`."""
        target = Path(path)
        try:
            raw = target.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise LedgerError(f"no ledger at {target}") from exc
        except OSError as exc:
            raise LedgerError(f"could not read ledger at {target}: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"ledger at {target} is not valid JSON: {exc}") from exc

        if not isinstance(data, dict) or "session" not in data:
            raise LedgerError(f"ledger at {target} is missing a 'session' object")

        version = data.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise LedgerError(
                f"ledger at {target} has schema_version {version!r}; "
                f"this build of controlz reads version {SCHEMA_VERSION}"
            )

        session = Session.model_validate(data["session"])
        return cls(session=session, path=target, autosave=autosave)

    # -- convenience -------------------------------------------------------

    @property
    def actions(self) -> list[Action]:
        return self.session.actions

    def __len__(self) -> int:
        return len(self.session)

    def __repr__(self) -> str:
        return (
            f"Ledger(session_id={self.session.session_id!r}, "
            f"actions={len(self)}, path={str(self.path) if self.path else None!r})"
        )
