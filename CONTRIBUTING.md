# Contributing to ControlZ

Thanks for looking. The most useful contribution right now is **a new integration** — every tool ControlZ can undo makes it more useful, and the interface is small on purpose.

## Getting set up

```bash
git clone https://github.com/yahiakortam/controlZ && cd controlZ
pip install -e ".[dev]"
pytest                  # 370 tests, ~10 seconds, no credentials needed
ruff check . && ruff format --check .
```

That is the whole loop. The test suite runs against an in-memory backend, so you never need a token to develop.

---

## Adding an integration

An integration teaches ControlZ four things about a tool: what state an operation is about to change, how reversible it is, how to undo it, and how to run that undo. Plus how to perform the call in the first place, so the tracker can wrap it rather than merely watch it.

Everything lives in one class. Here is a complete, working one.

### 1. Declare the class and its classification map

```python
# src/controlz/integrations/slack.py
from typing import Any, ClassVar

from controlz.integrations import Integration
from controlz.models import Action, Operation, Reversibility, RollbackPlan, RollbackStep


class SlackIntegration(Integration):
    name: ClassVar[str] = "slack"

    classification: ClassVar[dict[str, Reversibility]] = {
        "update_message": Reversibility.REVERSIBLE,
        "post_message": Reversibility.COMPENSATABLE,  # deletable, but people saw it
        "delete_message": Reversibility.COMPENSATABLE,  # repostable, with a new id
    }
```

The map is the whole classification system. **No inference, no LLM, no guessing** — an operation that is not in the map is `UNKNOWN`, which the default policy refuses.

Choosing a class is the most important judgement you will make in the file:

- `REVERSIBLE` — a direct inverse restores the prior state **exactly**. Not "close enough".
- `COMPENSATABLE` — no true inverse, but something limits the damage. If a notification went out, it is this at best.
- `IRREVERSIBLE` — nothing undoes it. The model will reject an executable rollback plan on one of these.
- `UNKNOWN` — leave it out of the map.

When torn between `REVERSIBLE` and `COMPENSATABLE`, pick `COMPENSATABLE`. Being pessimistic costs a user an approval prompt; being optimistic costs them a false promise.

### 2. Snapshot the state an operation will change

```python
    def snapshot(self, operation: Operation) -> dict[str, Any] | None:
        """Read the state this operation is about to change. Must not mutate anything."""
        self._require_supported(operation.api_call)
        args = operation.args
        if operation.api_call == "post_message":
            return {"channel": args["channel"], "message": None}  # nothing exists yet
        message = self.client.get_message(args["channel"], args["ts"])
        return {"channel": args["channel"], "message": {"ts": message.ts, "text": message.text}}

    def snapshot_after(self, operation: Operation, result: Any) -> dict[str, Any] | None:
        """Only needed for operations that *create* the thing they touch."""
        if operation.api_call == "post_message":
            return {
                "channel": operation.args["channel"],
                "message": {"ts": result.ts, "text": result.text},
            }
        return self.snapshot(operation)
```

Snapshots must be **JSON-serializable** — they go through the ledger to disk and back. Capture the fields you would need to restore the thing, plus whatever identifies it. `None` for a sub-object means "did not exist at this point", which is what `state_before` looks like for a create.

### 3. Execute

```python
    def execute(self, operation: Operation) -> Any:
        self._require_supported(operation.api_call)
        return getattr(self, f"_do_{operation.api_call}")(operation.args)

    def _do_post_message(self, args):
        return self.client.post(args["channel"], args["text"])

    def _do_update_message(self, args):
        return self.client.update(args["channel"], args["ts"], args["text"])

    def _do_delete_message(self, args):
        return self.client.delete(args["channel"], args["ts"])
```

Let backend errors propagate. The tracker records the failed attempt as `UNKNOWN` and re-raises.

### 4. Build the rollback plan

```python
    def build_rollback_plan(self, action: Action) -> RollbackPlan | None:
        if action.tool != self.name or not self.supports(action.api_call):
            return None
        if action.state_after is None:
            return None  # it never completed; there is nothing to undo

        if action.api_call == "post_message":
            message = action.state_after["message"]
            return RollbackPlan(
                strategy="delete-posted-message",
                steps=[
                    RollbackStep(
                        tool=self.name,
                        api_call="delete_message",
                        args={"channel": action.state_after["channel"], "ts": message["ts"]},
                        description="Delete the message this action posted.",
                    )
                ],
                notes="Deleting does not un-notify the people already pinged.",
            )

        before = action.state_before["message"]
        if before["text"] == action.args.get("text"):
            return RollbackPlan(strategy="no-op", notes="The update changed nothing.")
        return RollbackPlan(
            strategy="restore-previous-text",
            steps=[
                RollbackStep(
                    tool=self.name,
                    api_call="update_message",
                    args={
                        "channel": action.state_before["channel"],
                        "ts": before["ts"],
                        "text": before["text"],
                    },
                )
            ],
        )
```

Two rules that matter more than they look:

- **Reverse only what the action actually changed.** If the agent added a label the issue already had, the rollback must not strip it. Compute the real delta from `state_before`.
- **Return an empty plan for a no-op**, not `None`. `strategy="no-op"` with no steps reports as `NOTHING_TO_DO` — honest. `None` reports as "no plan recorded", which is a different and misleading claim.

### 5. Detect conflicts

This is where ControlZ earns its keep, so it is worth doing properly.

```python
    def current_state(self, action: Action) -> dict[str, Any] | None:
        """Re-read what this action touched, shaped like state_after."""
        after = action.state_after or {}
        recorded = after.get("message") or {}
        try:
            message = self.client.get_message(after["channel"], recorded["ts"])
        except Exception:
            return {"channel": after.get("channel"), "message": None}  # gone
        return {"channel": after["channel"], "message": {"ts": message.ts, "text": message.text}}

    def check_conflict(self, action: Action) -> list[ConflictDetail]:
        """Compare ONLY the fields this action changed."""
        current = self.current_state(action)
        live = (current or {}).get("message")
        if live is None:
            return [ConflictDetail(field="message", detail="the message is gone")]
        recorded = (action.state_after or {}).get("message") or {}
        if live["text"] != recorded["text"]:
            return [
                ConflictDetail(
                    field="message.text",
                    expected=recorded["text"],
                    actual=live["text"],
                    detail="the message was edited after we posted it",
                )
            ]
        return []
```

Compare the **narrow** thing, not the whole object. A rollback overwrites what the action wrote and nothing else, so an unrelated edit must not block it, while an edit to the exact field being restored must.

If you skip `check_conflict`, the base class compares the whole of `state_after` against the live state — safe, but it will block on edits your rollback would never have touched.

### 6. Execute the rollback

```python
    def execute_rollback(self, action: Action) -> None:
        if action.tool != self.name:
            raise IntegrationError(f"action belongs to {action.tool!r}, not {self.name!r}")
        self.execute_rollback_plan(action)  # runs each step through execute()
```

### 6b. Async (optional)

If your SDK is synchronous — most are — **you are already done**. Every async
hook defaults to running its blocking twin in a worker thread, so your
integration works under `await` without another line of code.

If your SDK is async, override the hooks that touch the network:

```python
    async def asnapshot(self, operation: Operation) -> dict[str, Any] | None: ...
    async def asnapshot_after(self, operation: Operation, result: Any) -> dict | None: ...
    async def aexecute(self, operation: Operation) -> Any: ...
    async def acurrent_state(self, action: Action) -> dict[str, Any] | None: ...
    async def acheck_conflict(self, action: Action) -> list[ConflictDetail]: ...
    async def aexecute_rollback(self, action: Action) -> None: ...
```

One trap worth naming: if you override `asnapshot`, **override
`asnapshot_after` too**. Its default offloads the synchronous `snapshot_after`,
which calls the synchronous `snapshot` — right for a blocking SDK, wrong for
yours. The default cannot just delegate to `asnapshot`, because integrations
override `snapshot_after` precisely to read new identifiers off `result`.

`classify` and `build_rollback_plan` stay synchronous. Neither touches the
network, and having one of each would be two things to keep in step for no gain.

### 7. Test it against an in-memory backend

Do not mock. Write a small fake client that behaves like the real API — including its errors — the way [`controlz/integrations/memory.py`](src/controlz/integrations/memory.py) does for GitHub. It is ~140 lines and it caught real bugs that mocks would have hidden.

Cover at minimum:

- every entry in the classification map
- the snapshot shape, and that snapshotting mutates nothing
- each rollback plan, including the no-op cases
- a conflict for a field the action changed, **and** no conflict for a field it did not
- a full round trip: track → save → reload → rollback

If the tool has a public API, add live tests behind an env-var skip, following [`tests/test_github_live.py`](tests/test_github_live.py). Live tests that skip silently do rot — ours did, and only a real run caught it — so run them before you send the PR.

---

## House rules

**Honesty over convenience.** Every design argument in this codebase resolves the same way: the report must not claim more than happened. `RESTORED` means the plan ran. `complete` and `fully_restored` are separate properties because collapsing them would be a lie. If a change makes ControlZ look better than it is, it is the wrong change.

**Pessimism is the safe default.** Unknown is treated as irreversible. A failed call is treated as possibly-landed. An unreadable state is treated as drift.

**Comments explain why, not what.** The code says what it does. Comment the judgement call — why compensatable and not reversible, why this field and not that one.

**Tests describe behaviour.** `test_add_labels_plan_removes_only_new_labels`, not `test_plan_2`.

## Sending a pull request

1. Branch from `main`.
2. `pytest && ruff check . && ruff format .` — CI runs lint plus tests on Python 3.10–3.13, and it must be green.
3. Write a commit message that explains the reasoning, not just the change.
4. Open the PR. Say what you chose and what you rejected — especially for a classification, where the reasoning *is* the contribution.

New integrations are welcome even if incomplete. An integration covering three operations well beats one covering ten badly.

## Where to start

See [ROADMAP.md](ROADMAP.md). Slack and HubSpot integrations are marked as good first issues, and there is a research task on auto-classification for anyone who would rather think than type.
