# Roadmap

Where ControlZ is going, and where to start if you want to help.

## Shipped

- **Core model** — `Action`, `Reversibility`, `Session`, and a durable JSON ledger with atomic writes
- **Interception** — the `Integration` interface and a `Tracker` that snapshots, executes, classifies, plans, and records
- **GitHub integration** — eight issue operations, hardcoded classifications, verified against a live repository
- **Rollback** — reverse dependency order, conflict detection that refuses to overwrite drift, and a report that accounts for every action exactly once
- **Reversibility score and policy gate** — weighted coverage, blast radius, and allow / require-approval / block from YAML
- **The watch window** — live feed, before/after diff, and the rewind

## Next

### More integrations

The interface is small and the returns are linear: every tool ControlZ can undo makes it useful to another set of people. **Slack** and **HubSpot** are written up as good first issues below.

Also wanted: Linear, Jira, Notion, Google Calendar, the filesystem, S3.

### Auto-classification research

Every classification today is hand-written. That is the right default — it is auditable and it never guesses — but it does not scale to hundreds of endpoints. Whether a machine can propose classifications that a human then approves is an open question, and a genuinely interesting one. Written up below.

### Beyond the current design

- **Selective rollback** — undo actions 3, 7, and 9 rather than a whole session, with dependency checks
- **Multi-tool sessions** — one agent run touching GitHub and Slack, unwound in the right order across both
- **A policy audit log** — record blocked and approved decisions, not just executed actions
- **Ledger compaction** — sessions long enough to want streaming rather than in-memory
- **`@controlz.tracked` decorator** — wrap an existing agent's tool functions without rewriting the call sites

---

## Good first issues

Three self-contained pieces of work. Each says what "done" looks like, so you are not guessing.

### 🟢 Add a Slack integration

**Why:** Agents post to Slack constantly, and a wrong message in the wrong channel is exactly the mistake ControlZ should catch. It is also the cleanest illustration of `COMPENSATABLE` in the codebase: you can delete a Slack message, but everyone was already notified.

**Scope:** `src/controlz/integrations/slack.py` — `SlackIntegration`, built on `slack-sdk`.

Suggested operations and classifications:

| operation | class | rollback |
| --- | --- | --- |
| `post_message` | `COMPENSATABLE` | delete it — the notification already went out |
| `update_message` | `REVERSIBLE` | restore the previous text |
| `delete_message` | `COMPENSATABLE` | repost the text, with a new timestamp and no thread history |
| `add_reaction` | `REVERSIBLE` | remove it |
| `remove_reaction` | `REVERSIBLE` | add it back |
| `pin_message` / `unpin_message` | `REVERSIBLE` | the inverse |

**Design questions worth thinking about** (and worth writing in the PR):

- Is `post_message` into a channel with `@here` really the same class as one into a quiet channel? Probably, but say why.
- Should a threaded reply declare a `dependency` on the message it replies to, so rollback unwinds them in order? (It should.)
- Slack edits carry an `edited` timestamp — a better conflict signal than comparing text. Use it if you can.

**Done when:** the integration ships with an in-memory Slack backend under `tests/`, unit tests covering every operation and its rollback plan, a conflict test where someone edits the message first, and live tests behind an env-var skip. Follow the walkthrough in [CONTRIBUTING.md](CONTRIBUTING.md#adding-an-integration).

**Difficulty:** moderate — the pattern is fully worked out in `integrations/github.py`; the judgement is in the classifications.

---

### 🟢 Add a HubSpot integration

**Why:** This is where the stakes get real. An agent mangling a CRM touches revenue, and "which of these 40 contact updates can we take back?" is a question people actually have to answer. It also stresses parts of the design GitHub does not: bulk operations, custom properties, and downstream workflow automation.

**Scope:** `src/controlz/integrations/hubspot.py` — `HubSpotIntegration`, built on `hubspot-api-client`.

Suggested operations and classifications:

| operation | class | rollback |
| --- | --- | --- |
| `update_contact` | `REVERSIBLE` | restore the previous property values |
| `create_contact` | `COMPENSATABLE` | archive it — HubSpot keeps deleted contacts recoverable for 90 days |
| `delete_contact` | `COMPENSATABLE` | restore from the recycle bin, within the window |
| `add_to_list` / `remove_from_list` | `REVERSIBLE` | the inverse |
| `create_note` | `COMPENSATABLE` | delete it |
| `send_marketing_email` | `IRREVERSIBLE` | **nothing.** It is in their inbox |

**The interesting problem:** HubSpot workflows fire on property changes. Setting `lifecyclestage` can trigger an email sequence that ControlZ will never see, and reverting the property does not unsend those emails. That is [limitation #2](LIMITATIONS.md#2-webhooks-and-downstream-automation-are-invisible) in its most expensive form. Consider whether `update_contact` on a workflow-triggering property should be classified `COMPENSATABLE` rather than `REVERSIBLE`, and whether the integration should warn when it cannot tell.

Getting this reasoning right matters more than the code.

**Done when:** as above — in-memory backend, unit tests, conflict tests, live tests behind an env-var skip against a HubSpot sandbox account.

**Difficulty:** moderate to hard. The API is larger than GitHub's and the classification calls are genuinely harder.

---

### 🔬 Research: can classification be automated?

**Why:** Hand-written classification is auditable and never guesses, which is why it is the default and will stay the default for anything shipped. But it does not scale past a few dozen endpoints, and it is the main reason adding an integration takes an afternoon rather than ten minutes.

**The question:** given an API's documentation — an OpenAPI spec, a method signature, a docstring — can a classification be *proposed* accurately enough to be worth a human's review time? A proposal a maintainer accepts or rejects is useful. A proposal quietly trusted is a footgun, and would undo the honesty the rest of the project is built on.

**Scope:** research, not a shipped feature. `research/auto_classification/` with findings written up in Markdown.

Worth trying:

- Hand-label ~100 operations across several APIs as ground truth. This is the actual deliverable; everything else is measured against it.
- Try the cheap heuristics first. How far does the verb get you — `get`/`list` → no side effect, `create` → compensatable, `delete` → depends entirely on whether the API has a recycle bin? Publish the confusion matrix.
- Then try an LLM over the docs. Compare against the heuristic baseline. Report where it is confidently wrong, which matters far more than aggregate accuracy.
- Report the asymmetry explicitly: predicting `REVERSIBLE` for something irreversible is a serious failure; predicting `IRREVERSIBLE` for something reversible is a minor annoyance. An approach that is 95% accurate but fails in the dangerous direction is worse than one that is 80% accurate and fails safe.

**Done when:** there is a labelled dataset, a baseline, at least one comparison, and an honest write-up — including "this does not work well enough" if that is the answer. A negative result documented properly is a real contribution here.

**Difficulty:** open-ended. Good for someone who would rather think than type.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). New integrations are welcome even if incomplete — three operations done well beat ten done badly.

If you want to pick up one of the issues above, say so on the issue first so two people do not build the same thing.
