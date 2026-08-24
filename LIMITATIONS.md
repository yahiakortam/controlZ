# Limitations

ControlZ is an undo layer, not a time machine. This page is the honest account of what it cannot do. If you only read one page of these docs, read this one — a tool that tells you what it can't do is more useful than one that lets you find out later.

The short version: **ControlZ can undo API calls. It cannot undo consequences.**

---

## 1. Side effects escape immediately

Deleting a comment is not the same as un-posting it. By the time ControlZ rolls anything back, the world has already reacted:

| what ControlZ undoes | what stays undone |
| --- | --- |
| the comment | the notification email in forty inboxes |
| the issue | the Slack message the integration posted |
| the label | the automation that fired on `label:urgent` |
| the pull request | the CI run, the deploy it triggered, the bill for the compute |
| the file | the copy someone already downloaded |

This is exactly why `COMPENSATABLE` exists as a separate class from `REVERSIBLE`, and why it earns only half credit in the reversibility score. A compensating action limits damage. It does not restore the prior state, and ControlZ never claims it does.

**What to do about it:** treat the score as a measure of recoverable *state*, not recoverable *impact*. For anything with an audience, assume the audience saw it.

## 2. Webhooks and downstream automation are invisible

ControlZ records what your agent called. It has no idea what that call set in motion.

If closing an issue triggers a webhook that updates a customer record in another system, reopening the issue does not update that record back. ControlZ sees one reversible action and reports it as fully restored — which is true of the issue, and false of everything downstream.

**What to do about it:** in systems with heavy automation, classify the triggering action as `COMPENSATABLE` or `IRREVERSIBLE` yourself, or add a rollback step that explicitly reverses the downstream effect. The classification map is yours to edit — see [CONTRIBUTING.md](CONTRIBUTING.md#adding-an-integration).

## 3. Concurrent edits are detected, not resolved

Before restoring anything, ControlZ re-reads the live state and compares it to the ledger. If someone else changed the field being restored, it refuses and marks the action `CONFLICT`.

That refusal is the feature. But note precisely what it does and does not cover:

- It compares **only the fields the action actually changed**. An unrelated edit by someone else will not block a rollback that would never have touched it. That is deliberate — otherwise any activity at all would freeze the whole session.
- It is a **check, then an action** — not a transaction. Between the check and the write there is a window, small but real, in which someone else can change the same field. ControlZ has no lock, because the APIs it talks to do not offer one.
- It compares **snapshots, not history**. If a value was changed away and then changed back, that reads as no drift.
- **Only ControlZ's own view is checked.** Actions taken outside the tracker are not in the ledger, so nothing knows they happened.

**What to do about it:** rewind soon after the mistake. The longer a session sits, the more the world moves under it.

## 4. Irreversible means irreversible

There is no clever trick behind `IRREVERSIBLE`. A settled payment, a hard-deleted production table, a sent SMS, an email to an external address — ControlZ records these, classifies them, refuses to pretend, and reports them as un-restored with the reason.

The model enforces this: an `IRREVERSIBLE` action may not carry an executable rollback plan. If some plan would genuinely limit the damage, the action is `COMPENSATABLE` instead, and that distinction is checked at validation time rather than left to a docstring.

**What to do about it:** use the policy gate. `on_irreversible: require_approval` puts a human in front of the decision while it is still a decision.

## 5. It only knows what it was told

ControlZ classifies operations from a **hardcoded table**. There is deliberately no inference, no LLM in the loop, no guessing:

- An operation missing from the table is `UNKNOWN` — treated as potentially irreversible, and refused by the default policy.
- If a classification in that table is *wrong*, ControlZ will be confidently wrong with it. `REVERSIBLE` is a claim the integration author makes, and it is only as good as their understanding of the API.
- Actions your agent takes outside the tracker are not recorded at all. ControlZ cannot undo what it never saw.

**What to do about it:** route every side-effecting call through the tracker, and read an integration's classification map before trusting it in production.

## 6. The snapshot is a summary, not the whole object

Snapshots capture the fields relevant to undoing an operation — for a GitHub issue: title, body, state, labels, assignees. Not milestones, not projects, not reactions, not the timeline.

A rollback restores what was captured. Anything outside the snapshot is neither compared for conflicts nor restored.

**What to do about it:** widen the snapshot in the integration if your workflow depends on a field that is not there. It is a few lines.

## 7. Partial failures are recorded, not repaired

If a call raises halfway through, ControlZ records the attempt classified `UNKNOWN` with no `state_after` and no rollback plan — because a failed call may still have partially landed. The same applies to a rollback that fails midway through a multi-step plan: earlier steps stay applied, and the entry is reported as `FAILED` with the error.

ControlZ deliberately does not retry, roll forward, or attempt to repair these. It surfaces them for a human.

**What to do about it:** read `report.failures` and `report.conflicts`. `report.complete` is false whenever either is non-empty.

## 8. Practical limits

- **No concurrency control on the ledger.** One `Ledger` object per session, written by one process. Two processes writing the same file will clobber each other — the write is atomic, so you get one whole version or the other, never a corrupted file, but the loser's actions are gone.
- **The ledger holds whatever you snapshot.** Issue bodies, comment text, and arguments are written to disk in plain JSON. Do not put secrets in `args` and expect them to stay private, and think before committing a ledger file.
- **Sessions are linear.** One agent run, one ordered log. There is no branching, merging, or partial-session replay.
- **The whole ledger is held in memory.** Fine for the hundreds or thousands of actions a session realistically produces; not designed for millions.
- **No authentication or multi-tenancy.** ControlZ is a library that runs with your agent's credentials and your agent's permissions.

## What ControlZ is actually for

Given all of the above — it earns its place by making the *reversibility of a plan* visible before it runs, and the *cost of a mistake* explicit after it does. Not by promising a clean undo.

The most valuable thing it prints is often the line saying what it could not put back.

---

*Found something we got wrong, or a limitation that is not listed? [Open an issue](https://github.com/yahiakortam/controlZ/issues). Corrections to this page are as welcome as code.*
