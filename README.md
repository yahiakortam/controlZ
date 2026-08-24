# ControlZ

**A transaction/rollback layer for AI agents.**

Agents act on real systems — they open pull requests, send email, write files, move
money. Most of those actions can be taken back; some can only be compensated for;
a few cannot be undone at all. ControlZ records what an agent did, classifies how
reversible each step was, and keeps the plan for undoing it.

> **Status: early.** The data model, the durable ledger, the interception layer,
> and the GitHub integration are here and tested. The TUI is not.

## Install

```bash
pip install -e ".[dev]"
```

## Quick start

Wrap an integration in a `Tracker` and every call it makes lands in the ledger,
with the state before, the state after, and the plan for undoing it:

```python
from controlz import Ledger, Session, Tracker
from controlz.integrations.github import GitHubIntegration

tracker = Tracker(
    Ledger(Session(agent="triage-bot"), path="run.json", autosave=True),
    [GitHubIntegration(token="ghp_...")],
)
gh = tracker.tool("github")

issue = gh.create_issue(
    repo="acme/widgets",
    title="Broken build",
    _intent="File the failure the user reported.",
)
gh.add_labels(repo="acme/widgets", issue_number=issue.number, labels=["bug"])

action = tracker.last_action()
action.reversibility  # <Reversibility.REVERSIBLE: 'reversible'>
action.state_before["issue"]["labels"]  # []
action.rollback_plan.strategy  # 'remove_labels-to-restore'

tracker.rollback_session()  # undo everything, newest first
```

Or record without the proxy, for full control over intent and dependencies:

```python
from controlz import Operation

tracked = tracker.track(
    Operation(
        tool="github",
        api_call="create_comment",
        args={"repo": "acme/widgets", "issue_number": issue.number, "body": "On it."},
        intent="Acknowledge the report.",
    ),
    dependencies=[action.operation_id],
)
tracked.action  # the ledger entry
tracked.result  # PyGithub's IssueComment
```

Reload a session in another process and undo it there:

```python
reloaded = Ledger.load("run.json")
for action in reloaded.session.undo_order():
    print(action.api_call, action.reversibility.value)
```

## The model

| Type | What it is |
| --- | --- |
| `Action` | One recorded operation: `operation_id`, `session_id`, `timestamp`, `tool`, `api_call`, `args`, `intent`, `state_before`, `state_after`, `reversibility`, `rollback_plan`, `dependencies`. |
| `Operation` | The forward half of an action — what the agent intends to call, before it has run. |
| `Reversibility` | `REVERSIBLE`, `COMPENSATABLE`, `IRREVERSIBLE`, `UNKNOWN`. |
| `RollbackPlan` / `RollbackStep` | An ordered recipe for undoing an action, shaped like the forward call so one executor can run both. |
| `Session` | An ordered log of actions from one agent run. |
| `Ledger` | Appends actions to a session and persists it to a JSON file (atomically), then reloads it. |
| `Integration` | The abstract backend: `snapshot`, `classify`, `build_rollback_plan`, `execute_rollback` — plus `execute`, so the tracker can wrap a call rather than merely observe one. |
| `Tracker` | The interception layer: snapshot → execute → snapshot → classify → plan → record. |

`UNKNOWN` is the default classification, and it is deliberately the *unsafe*
one: an unclassified action should be treated as potentially irreversible until
something proves otherwise. A call that raises is recorded as `UNKNOWN` too — it
may have partially landed, so it wants a human rather than an automatic undo. An
`IRREVERSIBLE` action may not carry an executable rollback plan; if a plan would
limit the damage, the action is `COMPENSATABLE`.

## GitHub integration

Classification is a hardcoded table — nothing is inferred, and an operation
missing from the table is unsupported.

| Operation | Reversibility | Rollback |
| --- | --- | --- |
| `update_issue` | `REVERSIBLE` | Restore the previous value of each field the call actually changed. |
| `add_labels` | `REVERSIBLE` | Remove only the labels that were not already there. |
| `remove_labels` | `REVERSIBLE` | Re-add only the labels that were actually present. |
| `close_issue` | `REVERSIBLE` | Reopen. |
| `reopen_issue` | `REVERSIBLE` | Close. |
| `create_issue` | `COMPENSATABLE` | Close it — GitHub cannot delete issues over the REST API, and subscribers were already notified. |
| `create_comment` | `COMPENSATABLE` | Delete it — which does not un-send the notification email. |
| `delete_comment` | `COMPENSATABLE` | Re-post the body as a new comment, with a new id and timestamp. |

Plans reverse only what a call actually changed. Adding a label the issue
already carried is a no-op, and its rollback must not strip it; that case
records an empty plan (`strategy="no-op"`) rather than a wrong one.

### Try it against a real repo

```bash
export CONTROLZ_GITHUB_TOKEN=ghp_...          # needs issues:write
python scripts/demo_github.py --repo you/throwaway --rollback
```

The script opens an issue, labels it, comments, and closes it — four tracked
actions — prints the ledger, writes it to `controlz-demo.json`, and (with
`--rollback`) unwinds the session.

## Development

```bash
pytest                  # unit tests; live GitHub tests skip themselves
ruff check .            # lint
ruff format .           # format
```

The live integration tests run against a real repository and are skipped unless
both variables are set:

```bash
export CONTROLZ_GITHUB_TOKEN=ghp_...
export CONTROLZ_TEST_REPO=you/throwaway   # a repo you do not mind mutating
pytest -m live
```

## License

Apache-2.0. See [LICENSE](LICENSE).
