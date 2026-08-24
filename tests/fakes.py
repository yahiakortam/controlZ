"""An in-memory stand-in for PyGithub, mirroring the surface ControlZ uses.

Only the attributes and methods `GitHubIntegration` touches are implemented,
with the same semantics the real API has (issues are looked up live, removing
an absent label is an error, comments get monotonically increasing ids).
"""

from __future__ import annotations

from typing import Any


class FakeGithubError(Exception):
    """Stands in for github.GithubException."""


class FakeNamed:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeUser:
    def __init__(self, login: str) -> None:
        self.login = login


class FakeComment:
    def __init__(self, issue: FakeIssue, comment_id: int, body: str) -> None:
        self._issue = issue
        self.id = comment_id
        self.body = body
        self.html_url = f"{issue.html_url}#issuecomment-{comment_id}"
        self.deleted = False

    def delete(self) -> None:
        self._issue.comments.pop(self.id, None)
        self.deleted = True


class FakeIssue:
    def __init__(
        self,
        repo: FakeRepo,
        number: int,
        title: str,
        body: str | None = None,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> None:
        self._repo = repo
        self.number = number
        self.title = title
        self.body = body
        self.state = "open"
        self.labels = [FakeNamed(name) for name in labels or []]
        self.assignees = [FakeUser(login) for login in assignees or []]
        self.html_url = f"https://github.test/{repo.full_name}/issues/{number}"
        self.comments: dict[int, FakeComment] = {}
        self.edits: list[dict[str, Any]] = []

    @property
    def label_names(self) -> list[str]:
        return [label.name for label in self.labels]

    def edit(self, **kwargs: Any) -> None:
        self.edits.append(dict(kwargs))
        if "title" in kwargs:
            self.title = kwargs["title"]
        if "body" in kwargs:
            self.body = kwargs["body"]
        if "state" in kwargs:
            if kwargs["state"] not in ("open", "closed"):
                raise FakeGithubError(f"invalid state {kwargs['state']!r}")
            self.state = kwargs["state"]
        if "labels" in kwargs:
            self.labels = [FakeNamed(str(name)) for name in kwargs["labels"]]

    def add_to_labels(self, *names: str) -> None:
        for name in names:
            if name not in self.label_names:
                self.labels.append(FakeNamed(str(name)))

    def remove_from_labels(self, name: str) -> None:
        if name not in self.label_names:
            raise FakeGithubError(f"404: label {name!r} is not on issue #{self.number}")
        self.labels = [label for label in self.labels if label.name != name]

    def create_comment(self, body: str) -> FakeComment:
        comment_id = self._repo.next_comment_id()
        comment = FakeComment(self, comment_id, body)
        self.comments[comment_id] = comment
        return comment

    def get_comment(self, comment_id: int) -> FakeComment:
        try:
            return self.comments[comment_id]
        except KeyError:
            raise FakeGithubError(f"404: no comment {comment_id}") from None


class FakeRepo:
    def __init__(self, full_name: str) -> None:
        self.full_name = full_name
        self.issues: dict[int, FakeIssue] = {}
        self._next_issue = 1
        self._next_comment = 1000

    def next_comment_id(self) -> int:
        self._next_comment += 1
        return self._next_comment

    def create_issue(self, **kwargs: Any) -> FakeIssue:
        number = self._next_issue
        self._next_issue += 1
        issue = FakeIssue(
            self,
            number,
            title=kwargs["title"],
            body=kwargs.get("body"),
            labels=list(kwargs.get("labels") or []),
            assignees=list(kwargs.get("assignees") or []),
        )
        self.issues[number] = issue
        return issue

    def get_issue(self, number: int) -> FakeIssue:
        try:
            return self.issues[number]
        except KeyError:
            raise FakeGithubError(f"404: no issue #{number} in {self.full_name}") from None


class FakeGithub:
    """Root fake client. ``get_repo`` returns a live, mutable repo."""

    def __init__(self) -> None:
        self.repos: dict[str, FakeRepo] = {}
        self.get_repo_calls: list[str] = []

    def get_repo(self, full_name: str) -> FakeRepo:
        self.get_repo_calls.append(full_name)
        if full_name not in self.repos:
            self.repos[full_name] = FakeRepo(full_name)
        return self.repos[full_name]
