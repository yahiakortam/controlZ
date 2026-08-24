# ControlZ

**A transaction/rollback layer for AI agents.**

Agents act on real systems — they open pull requests, send email, write files, move
money. Most of those actions can be taken back; some can only be compensated for;
a few cannot be undone at all. ControlZ records what an agent did, classifies how
reversible each step was, and keeps the plan for undoing it.

> **Status: early.** The data model, the durable ledger, the interception layer,
> the GitHub integration, conflict-aware rollback, and the pre-execution policy
> gate are here and tested. The TUI is not.

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
| `ReversibilityScore` / `BlastRadius` | Pre-execution scoring: weighted coverage, plus what the plan touches. |
| `Policy` / `PolicyGate` | Rules in YAML or a dict, turned into allow / require approval / block. |
| `RollbackReport` | Per-action account of a rollback: restored, skipped, conflicts, failures. |

`UNKNOWN` is the default classification, and it is deliberately the *unsafe*
one: an unclassified action should be treated as potentially irreversible until
something proves otherwise. A call that raises is recorded as `UNKNOWN` too — it
may have partially landed, so it wants a human rather than an automatic undo. An
`IRREVERSIBLE` action may not carry an executable rollback plan; if a plan would
limit the damage, the action is `COMPENSATABLE`.

## Reversibility score and policy gate

Before anything runs, score the plan:

```python
from controlz import Policy, PolicyGate, reversibility_score
from controlz.integrations.github import GitHubIntegration

score = reversibility_score(planned_operations, GitHubIntegration(token=...))
score.coverage  # 83.3
print(score.summary())
```

```
reversibility score: 83.3% over 6 actions
  3 reversible, 3 compensatable, 0 irreversible, 0 unknown
  blast radius: github x6 across 3 targets
```

Coverage is **weighted**, and the weights encode a judgement: a reversible
action counts 1.0, a compensatable one counts 0.5, and irreversible and unknown
count nothing. Compensation is real but partial — the retraction went out, but
the email was still read. Two unweighted figures sit alongside it when the
distinction matters: `recoverable_share` (has *any* way back) and
`fully_reversible_share` (restores exactly).

The blast radius answers a different question — not "can we undo it?" but "how
much of the world does this touch?": calls per tool, calls per operation,
distinct targets, and every action that could not be taken back, named.

### Policy

```yaml
# controlz-policy.yaml
name: example
minimum_score: 60          # block outright below this coverage
below_minimum_score: block

on_reversible: allow       # ordinary work needs no supervision
on_compensatable: allow
max_compensatable: 3       # a few is fine, a pile is not
over_compensatable_limit: require_approval

on_irreversible: require_approval
on_unknown: require_approval   # unclassified is treated as irreversible
max_targets: 10
over_target_limit: require_approval
```

```python
policy = Policy.from_yaml("controlz-policy.yaml")  # or Policy.from_dict({...})
gate = PolicyGate(policy, github)

decision = gate.check(planned)  # score + verdict, changes nothing
gate.enforce(planned, approve=ask_a_human)  # raises PolicyViolation if refused
```

Every rule reports, including the ones that would have allowed the plan, and the
**strictest verdict wins**. A block is not approvable: approval is for judgement
calls, and a plan under the minimum score is not one.

Wire the gate into a tracker and it stops calls rather than merely describing
them. A blocked call never executes and is never recorded, because it never
happened:

```python
tracker = Tracker(ledger, [github], policy=policy, approve=ask_a_human)
tracker.call("github", "create_issue", ...)  # PolicyViolation if the policy refuses
```

See it end to end, with no GitHub credentials and nothing executed:

```bash
python scripts/demo_policy.py --plan safe        # cleared
python scripts/demo_policy.py --plan risky       # held for approval
python scripts/demo_policy.py --plan reckless    # refused
```

## Rollback

`session.rollback(integration)` unwinds a session and returns a report. Three
rules govern it.

**Order.** Actions are undone in reverse *dependency* order — anything built on
another action is undone first. With no declared dependencies that is plain
reverse chronological order; with them, the graph decides. Dependency cycles are
reported, never silently reordered.

**Never overwrite a surprise.** Before restoring anything, ControlZ re-reads the
live state and compares it to what the ledger recorded — but only the fields the
action actually changed. An unrelated edit by someone else does not block a
rollback that would not have touched it; an edit to the very field being
restored does. That action is marked `CONFLICT` and left alone until a human
confirms:

```python
report = session.rollback(github)
for entry in report.conflicts:
    print(entry.reason)  # "live state no longer matches the ledger: …"

# Confirm explicitly, per action or with a callback:
session.rollback(github, on_conflict=lambda action, conflicts: ask_the_user(action))
session.rollback(github, force=[some_operation_id])
```

If the current state cannot be read at all, that is also a conflict. An
unreadable target is not a green light.

**Say what happened.** Every action appears in the report exactly once, with an
outcome it earned:

| Outcome | Meaning |
| --- | --- |
| `RESTORED` | The rollback plan ran without error. |
| `NOTHING_TO_DO` | The action changed nothing, so its plan had no steps. |
| `SKIPPED` | Not undoable: irreversible, unclassified, or planless. |
| `CONFLICT` | The live state had drifted. Left untouched. |
| `BLOCKED` | A dependent of this action could not be rolled back. |
| `FAILED` | The rollback was attempted and raised. |
| `PLANNED` | Dry run only. |
| `NOT_ATTEMPTED` | The run stopped before reaching it. |

An irreversible action is never quietly dropped — it is reported as un-restored,
with the reason. The report exposes the four headline categories (`restored`,
`skipped_irreversible`, `conflicts`, `failures`) plus `blocked`,
`nothing_to_do`, `not_attempted`, and `unrestored`.

Two summary properties, deliberately distinct:

- `report.complete` — nothing is left for a human to act on.
- `report.fully_restored` — everything actually came back.

A session containing one irreversible action is `complete` but **not**
`fully_restored`. Blurring those two together is exactly the dishonesty this
layer exists to prevent.

```python
report = session.rollback(github, dry_run=True)  # check everything, change nothing
report.counts()  # {'restored': 13, 'nothing_to_do': 1, 'skipped': 1}
print(report.summary())
```

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
