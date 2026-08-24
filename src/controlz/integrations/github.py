"""GitHub integration backed by PyGithub.

Every supported operation has a hardcoded reversibility and a hardcoded recipe
for undoing it. Nothing here infers anything: if an ``api_call`` is not in
:attr:`GitHubIntegration.classification`, it is not supported.

Snapshots are plain dicts so they survive a JSON round-trip through the ledger:

    issue snapshot   {"repo", "issue": {"issue_number", "title", "body",
                                        "state", "labels", "assignees", "url"}}
    comment snapshot {"repo", "issue_number", "comment": {"comment_id", "body", "url"}}

A ``None`` under ``issue``/``comment`` means "did not exist at this point",
which is what ``state_before`` looks like for a create.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, ClassVar

from controlz.integrations import Integration, IntegrationError
from controlz.models import Action, Operation, Reversibility, RollbackPlan, RollbackStep
from controlz.rollback import ConflictDetail

if TYPE_CHECKING:  # pragma: no cover - typing only
    from github import Github
    from github.Issue import Issue
    from github.IssueComment import IssueComment

__all__ = ["GitHubIntegration"]

#: Environment variable read when no token or client is passed explicitly.
TOKEN_ENV_VAR = "CONTROLZ_GITHUB_TOKEN"

#: Fields of an issue that ``update_issue`` may change, and that its rollback restores.
_EDITABLE_FIELDS = ("title", "body", "state", "labels")


class GitHubIntegration(Integration):
    """Record and undo GitHub issue operations.

    >>> integration = GitHubIntegration(token="ghp_...")           # doctest: +SKIP
    >>> integration.classify(Operation(tool="github", api_call="create_issue"))
    <Reversibility.COMPENSATABLE: 'compensatable'>
    """

    name: ClassVar[str] = "github"

    #: Hardcoded classification. Absent operations are UNKNOWN and unsupported.
    classification: ClassVar[dict[str, Reversibility]] = {
        # A direct inverse restores the prior state exactly.
        "update_issue": Reversibility.REVERSIBLE,
        "add_labels": Reversibility.REVERSIBLE,
        "remove_labels": Reversibility.REVERSIBLE,
        "close_issue": Reversibility.REVERSIBLE,
        "reopen_issue": Reversibility.REVERSIBLE,
        # GitHub issues cannot be deleted through the REST API, so the best
        # available undo is to close the issue we opened. Someone was notified.
        "create_issue": Reversibility.COMPENSATABLE,
        # A comment can be deleted, but not un-seen — subscribers were emailed.
        "create_comment": Reversibility.COMPENSATABLE,
        # Exists to serve create_comment's rollback. Undoing it re-posts the
        # body as a *new* comment, with a new id and timestamp.
        "delete_comment": Reversibility.COMPENSATABLE,
    }

    def __init__(self, client: Github | None = None, token: str | None = None) -> None:
        if client is not None and token is not None:
            raise ValueError("pass either a client or a token, not both")
        self._client = client
        self._token = token
        self._repo_cache: dict[str, Any] = {}

    # -- plumbing -----------------------------------------------------------

    @property
    def client(self) -> Github:
        """The PyGithub client, built from the token on first use."""
        if self._client is None:
            from github import Auth, Github

            token = self._token or os.environ.get(TOKEN_ENV_VAR)
            if not token:
                raise IntegrationError(
                    f"no GitHub client or token given, and {TOKEN_ENV_VAR} is unset"
                )
            self._client = Github(auth=Auth.Token(token))
        return self._client

    def _repo(self, args: dict[str, Any]) -> Any:
        full_name = self._require(args, "repo")
        if full_name not in self._repo_cache:
            self._repo_cache[full_name] = self.client.get_repo(full_name)
        return self._repo_cache[full_name]

    def _issue(self, args: dict[str, Any]) -> Issue:
        return self._repo(args).get_issue(int(self._require(args, "issue_number")))

    @staticmethod
    def _require(args: dict[str, Any], key: str) -> Any:
        if key not in args or args[key] is None:
            raise IntegrationError(f"missing required argument {key!r}")
        return args[key]

    @staticmethod
    def _labels_arg(args: dict[str, Any]) -> list[str]:
        labels = GitHubIntegration._require(args, "labels")
        if isinstance(labels, str) or not isinstance(labels, (list, tuple)):
            raise IntegrationError("'labels' must be a list of label names")
        return [str(label) for label in labels]

    # -- state capture ------------------------------------------------------

    @staticmethod
    def _issue_state(issue: Issue) -> dict[str, Any]:
        return {
            "issue_number": issue.number,
            "title": issue.title,
            "body": issue.body,
            "state": issue.state,
            "labels": sorted(label.name for label in issue.labels),
            "assignees": sorted(user.login for user in issue.assignees),
            "url": issue.html_url,
        }

    @staticmethod
    def _comment_state(comment: IssueComment) -> dict[str, Any]:
        return {"comment_id": comment.id, "body": comment.body, "url": comment.html_url}

    def snapshot(self, operation: Operation) -> dict[str, Any] | None:
        """Read the current state of whatever this operation is about to touch."""
        self._require_supported(operation.api_call)
        args = operation.args
        repo = str(self._require(args, "repo"))

        if operation.api_call == "create_issue":
            # Nothing exists yet; the snapshot records that fact.
            return {"repo": repo, "issue": None}

        if operation.api_call == "create_comment":
            return {
                "repo": repo,
                "issue_number": int(self._require(args, "issue_number")),
                "comment": None,
            }

        if operation.api_call == "delete_comment":
            comment = self._issue(args).get_comment(int(self._require(args, "comment_id")))
            return {
                "repo": repo,
                "issue_number": int(args["issue_number"]),
                "comment": self._comment_state(comment),
            }

        return {"repo": repo, "issue": self._issue_state(self._issue(args))}

    def snapshot_after(self, operation: Operation, result: Any) -> dict[str, Any] | None:
        """Capture state after the call, reading new ids off ``result``."""
        repo = str(operation.args.get("repo", ""))

        if operation.api_call == "create_issue":
            return {"repo": repo, "issue": self._issue_state(result)}

        if operation.api_call == "create_comment":
            return {
                "repo": repo,
                "issue_number": int(operation.args["issue_number"]),
                "comment": self._comment_state(result),
            }

        if operation.api_call == "delete_comment":
            return {
                "repo": repo,
                "issue_number": int(operation.args["issue_number"]),
                "comment": None,
            }

        return self.snapshot(operation)

    # -- classification -----------------------------------------------------

    def classify(self, operation: Operation) -> Reversibility:
        """Look the operation up in the hardcoded table. No inference."""
        return self.classification.get(operation.api_call, Reversibility.UNKNOWN)

    # -- execution ----------------------------------------------------------

    def execute(self, operation: Operation) -> Any:
        """Perform the call against GitHub and return PyGithub's result."""
        self._require_supported(operation.api_call)
        args = operation.args
        handler = getattr(self, f"_do_{operation.api_call}")
        return handler(args)

    def _do_create_issue(self, args: dict[str, Any]) -> Any:
        kwargs: dict[str, Any] = {"title": str(self._require(args, "title"))}
        if args.get("body") is not None:
            kwargs["body"] = str(args["body"])
        if args.get("labels") is not None:
            kwargs["labels"] = self._labels_arg(args)
        if args.get("assignees") is not None:
            kwargs["assignees"] = list(args["assignees"])
        return self._repo(args).create_issue(**kwargs)

    def _do_update_issue(self, args: dict[str, Any]) -> Any:
        issue = self._issue(args)
        kwargs = {field: args[field] for field in _EDITABLE_FIELDS if field in args}
        if not kwargs:
            raise IntegrationError(
                f"update_issue needs at least one of {', '.join(_EDITABLE_FIELDS)}"
            )
        issue.edit(**kwargs)
        return issue

    def _do_close_issue(self, args: dict[str, Any]) -> Any:
        issue = self._issue(args)
        issue.edit(state="closed")
        return issue

    def _do_reopen_issue(self, args: dict[str, Any]) -> Any:
        issue = self._issue(args)
        issue.edit(state="open")
        return issue

    def _do_add_labels(self, args: dict[str, Any]) -> Any:
        issue = self._issue(args)
        labels = self._labels_arg(args)
        if labels:
            issue.add_to_labels(*labels)
        return issue

    def _do_remove_labels(self, args: dict[str, Any]) -> Any:
        issue = self._issue(args)
        for label in self._labels_arg(args):
            issue.remove_from_labels(label)
        return issue

    def _do_create_comment(self, args: dict[str, Any]) -> Any:
        return self._issue(args).create_comment(str(self._require(args, "body")))

    def _do_delete_comment(self, args: dict[str, Any]) -> Any:
        comment = self._issue(args).get_comment(int(self._require(args, "comment_id")))
        comment.delete()
        return comment

    # -- conflict detection -------------------------------------------------

    def current_state(self, action: Action) -> dict[str, Any] | None:
        """Re-read the issue or comment this action touched.

        Identifiers come from ``state_after`` (falling back to the arguments),
        so this works for creates, where the target did not exist beforehand.
        A target that has since vanished reads back as ``None`` rather than
        raising, so the caller can tell "gone" from "unreachable".
        """
        after = action.state_after or {}
        before = action.state_before or {}
        repo = after.get("repo") or before.get("repo") or action.args.get("repo")

        if action.api_call in ("create_comment", "delete_comment"):
            recorded = after.get("comment") or before.get("comment") or {}
            issue_number = after.get("issue_number") or before.get("issue_number")
            comment_id = recorded.get("comment_id") or action.args.get("comment_id")
            state: dict[str, Any] = {
                "repo": repo,
                "issue_number": issue_number,
                "comment": None,
            }
            if comment_id is None:
                return state
            try:
                comment = self._issue({"repo": repo, "issue_number": issue_number}).get_comment(
                    int(comment_id)
                )
            except Exception:
                return state  # deleted, or never there
            state["comment"] = self._comment_state(comment)
            return state

        recorded_issue = after.get("issue") or before.get("issue") or {}
        issue_number = recorded_issue.get("issue_number") or action.args.get("issue_number")
        if issue_number is None:
            return {"repo": repo, "issue": None}
        try:
            issue = self._issue({"repo": repo, "issue_number": issue_number})
        except Exception:
            return {"repo": repo, "issue": None}
        return {"repo": repo, "issue": self._issue_state(issue)}

    def check_conflict(self, action: Action) -> list[ConflictDetail]:
        """Compare only the fields this action actually changed.

        A rollback overwrites what the action wrote, and nothing else — so an
        unrelated edit by someone else must not block it, while an edit to the
        very field being restored must. Anything unreadable is drift too.
        """
        if action.tool != self.name or not self.supports(action.api_call):
            return []
        try:
            current = self.current_state(action)
        except Exception as exc:
            return [
                ConflictDetail(
                    field="<state>",
                    detail=f"could not read the current state: {exc}",
                    expected="readable state",
                )
            ]

        checker = getattr(self, f"_conflicts_{action.api_call}")
        return checker(action, current or {})

    def _conflicts_create_issue(self, action: Action, current: dict) -> list[ConflictDetail]:
        # Rolling this back only closes the issue; edits others made to it are
        # none of our business. All that matters is that it still exists.
        if current.get("issue") is None:
            recorded = (action.state_after or {}).get("issue") or {}
            return [
                ConflictDetail(
                    field="issue",
                    expected=f"issue #{recorded.get('issue_number')} exists",
                    actual=None,
                    detail="the issue is gone or unreadable",
                )
            ]
        return []

    def _conflicts_create_comment(self, action: Action, current: dict) -> list[ConflictDetail]:
        recorded = (action.state_after or {}).get("comment") or {}
        live = current.get("comment")
        if live is None:
            return [
                ConflictDetail(
                    field="comment",
                    expected=f"comment {recorded.get('comment_id')} exists",
                    actual=None,
                    detail="the comment is already gone",
                )
            ]
        if live.get("body") != recorded.get("body"):
            return [
                ConflictDetail(
                    field="comment.body",
                    expected=recorded.get("body"),
                    actual=live.get("body"),
                    detail="the comment was edited after we posted it",
                )
            ]
        return []

    def _conflicts_delete_comment(self, action: Action, current: dict) -> list[ConflictDetail]:
        # The comment is gone by definition; re-posting it overwrites nothing.
        return []

    def _conflicts_update_issue(self, action: Action, current: dict) -> list[ConflictDetail]:
        issue = current.get("issue")
        if issue is None:
            return [self._missing_issue(action)]
        recorded = (action.state_after or {}).get("issue") or {}
        conflicts = []
        for field in _EDITABLE_FIELDS:
            if field not in action.args:
                continue
            if issue.get(field) != recorded.get(field):
                conflicts.append(
                    ConflictDetail(
                        field=f"issue.{field}",
                        expected=recorded.get(field),
                        actual=issue.get(field),
                        detail=f"{field} changed after this action ran",
                    )
                )
        return conflicts

    def _conflicts_close_issue(self, action: Action, current: dict) -> list[ConflictDetail]:
        return self._conflicts_state(action, current)

    def _conflicts_reopen_issue(self, action: Action, current: dict) -> list[ConflictDetail]:
        return self._conflicts_state(action, current)

    def _conflicts_state(self, action: Action, current: dict) -> list[ConflictDetail]:
        issue = current.get("issue")
        if issue is None:
            return [self._missing_issue(action)]
        recorded = (action.state_after or {}).get("issue") or {}
        if issue.get("state") != recorded.get("state"):
            return [
                ConflictDetail(
                    field="issue.state",
                    expected=recorded.get("state"),
                    actual=issue.get("state"),
                    detail="the issue was opened or closed by someone else since",
                )
            ]
        return []

    def _conflicts_add_labels(self, action: Action, current: dict) -> list[ConflictDetail]:
        return self._conflicts_labels(action, current, adding=True)

    def _conflicts_remove_labels(self, action: Action, current: dict) -> list[ConflictDetail]:
        return self._conflicts_labels(action, current, adding=False)

    def _conflicts_labels(
        self, action: Action, current: dict, *, adding: bool
    ) -> list[ConflictDetail]:
        issue = current.get("issue")
        if issue is None:
            return [self._missing_issue(action)]

        before = set((action.state_before or {}).get("issue", {}).get("labels") or [])
        requested = [str(label) for label in action.args.get("labels", [])]
        if adding:
            affected = [label for label in requested if label not in before]
        else:
            affected = [label for label in requested if label in before]
        live = set(issue.get("labels") or [])

        conflicts = []
        for label in affected:
            if adding and label not in live:
                conflicts.append(
                    ConflictDetail(
                        field="issue.labels",
                        expected=f"{label!r} present",
                        actual=sorted(live),
                        detail=f"{label!r} was already removed by someone else",
                    )
                )
            elif not adding and label in live:
                conflicts.append(
                    ConflictDetail(
                        field="issue.labels",
                        expected=f"{label!r} absent",
                        actual=sorted(live),
                        detail=f"{label!r} was put back by someone else",
                    )
                )
        return conflicts

    @staticmethod
    def _missing_issue(action: Action) -> ConflictDetail:
        recorded = (action.state_after or {}).get("issue") or {}
        return ConflictDetail(
            field="issue",
            expected=f"issue #{recorded.get('issue_number')} readable",
            actual=None,
            detail="the issue is gone or unreadable",
        )

    # -- rollback -----------------------------------------------------------

    def build_rollback_plan(self, action: Action) -> RollbackPlan | None:
        """Build the undo recipe for a recorded action.

        Returns ``None`` when the action is unsupported or was never completed
        (no ``state_after``). Returns an empty — non-executable — plan when the
        action turned out to be a no-op, so the ledger still records that it
        was considered.
        """
        if action.tool != self.name or not self.supports(action.api_call):
            return None
        if action.state_after is None:
            return None

        builder = getattr(self, f"_plan_{action.api_call}")
        return builder(action)

    def _plan_create_issue(self, action: Action) -> RollbackPlan:
        after = (action.state_after or {}).get("issue") or {}
        repo, number = action.state_after.get("repo"), after.get("issue_number")
        return RollbackPlan(
            strategy="close-created-issue",
            steps=[
                RollbackStep(
                    tool=self.name,
                    api_call="close_issue",
                    args={"repo": repo, "issue_number": number},
                    description=f"Close issue #{number}, which this action opened.",
                )
            ],
            notes=(
                "GitHub cannot delete issues over the REST API, and subscribers "
                "were already notified. Closing is a compensation, not an undo."
            ),
        )

    def _plan_create_comment(self, action: Action) -> RollbackPlan:
        after = action.state_after or {}
        comment = after.get("comment") or {}
        return RollbackPlan(
            strategy="delete-created-comment",
            steps=[
                RollbackStep(
                    tool=self.name,
                    api_call="delete_comment",
                    args={
                        "repo": after.get("repo"),
                        "issue_number": after.get("issue_number"),
                        "comment_id": comment.get("comment_id"),
                    },
                    description="Delete the comment this action posted.",
                )
            ],
            notes="Deletion does not un-send the notification email subscribers received.",
        )

    def _plan_delete_comment(self, action: Action) -> RollbackPlan:
        before = action.state_before or {}
        comment = before.get("comment") or {}
        return RollbackPlan(
            strategy="repost-deleted-comment",
            steps=[
                RollbackStep(
                    tool=self.name,
                    api_call="create_comment",
                    args={
                        "repo": before.get("repo"),
                        "issue_number": before.get("issue_number"),
                        "body": comment.get("body"),
                    },
                    description="Re-post the deleted comment's body.",
                )
            ],
            notes="The replacement comment gets a new id, author, and timestamp.",
        )

    def _plan_update_issue(self, action: Action) -> RollbackPlan:
        before = (action.state_before or {}).get("issue") or {}
        changed = [
            field
            for field in _EDITABLE_FIELDS
            if field in action.args and action.args[field] != before.get(field)
        ]
        if not changed:
            return RollbackPlan(strategy="no-op", notes="The update changed nothing.")

        args: dict[str, Any] = {
            "repo": (action.state_before or {}).get("repo"),
            "issue_number": before.get("issue_number"),
        }
        args.update({field: before.get(field) for field in changed})
        return RollbackPlan(
            strategy="restore-previous-fields",
            steps=[
                RollbackStep(
                    tool=self.name,
                    api_call="update_issue",
                    args=args,
                    description=f"Restore {', '.join(changed)} to the previous value.",
                )
            ],
        )

    def _plan_add_labels(self, action: Action) -> RollbackPlan:
        return self._plan_label_change(action, adding=True)

    def _plan_remove_labels(self, action: Action) -> RollbackPlan:
        return self._plan_label_change(action, adding=False)

    def _plan_label_change(self, action: Action, *, adding: bool) -> RollbackPlan:
        before = (action.state_before or {}).get("issue") or {}
        had = set(before.get("labels") or [])
        requested = [str(label) for label in action.args.get("labels", [])]
        # Only reverse labels the call actually changed: adding a label the issue
        # already carried is a no-op, and removing it again would lose state.
        if adding:
            affected = [label for label in requested if label not in had]
        else:
            affected = [label for label in requested if label in had]

        if not affected:
            return RollbackPlan(
                strategy="no-op",
                notes=(
                    "Every requested label was already present."
                    if adding
                    else "None of the requested labels were present."
                ),
            )

        inverse = "remove_labels" if adding else "add_labels"
        return RollbackPlan(
            strategy=f"{inverse}-to-restore",
            steps=[
                RollbackStep(
                    tool=self.name,
                    api_call=inverse,
                    args={
                        "repo": (action.state_before or {}).get("repo"),
                        "issue_number": before.get("issue_number"),
                        "labels": affected,
                    },
                    description=f"{inverse.replace('_', ' ').capitalize()}: {', '.join(affected)}.",
                )
            ],
        )

    def _plan_close_issue(self, action: Action) -> RollbackPlan:
        return self._plan_state_change(action, inverse="reopen_issue", was="closed")

    def _plan_reopen_issue(self, action: Action) -> RollbackPlan:
        return self._plan_state_change(action, inverse="close_issue", was="open")

    def _plan_state_change(self, action: Action, *, inverse: str, was: str) -> RollbackPlan:
        before = (action.state_before or {}).get("issue") or {}
        if before.get("state") == was:
            return RollbackPlan(
                strategy="no-op", notes=f"The issue was already {was} before this action."
            )
        return RollbackPlan(
            strategy=f"{inverse.replace('_', '-')}",
            steps=[
                RollbackStep(
                    tool=self.name,
                    api_call=inverse,
                    args={
                        "repo": (action.state_before or {}).get("repo"),
                        "issue_number": before.get("issue_number"),
                    },
                    description=f"Return the issue to {before.get('state')!r}.",
                )
            ],
        )

    def execute_rollback(self, action: Action) -> None:
        """Run the action's rollback plan, step by step, against GitHub."""
        if action.tool != self.name:
            raise IntegrationError(
                f"action {action.operation_id} belongs to tool {action.tool!r}, not {self.name!r}"
            )
        plan = action.rollback_plan or self.build_rollback_plan(action)
        if plan is None:
            raise IntegrationError(f"no rollback plan available for {action.operation_id}")
        if not plan.is_executable:
            raise IntegrationError(
                f"rollback plan for {action.operation_id} has no steps (strategy {plan.strategy!r})"
            )
        for step in plan.steps:
            self.execute(Operation(tool=step.tool, api_call=step.api_call, args=step.args))
