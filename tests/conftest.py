import pytest

from controlz import Ledger, Tracker
from controlz.integrations.github import GitHubIntegration
from controlz.integrations.memory import InMemoryGitHub


@pytest.fixture
def fake_github() -> InMemoryGitHub:
    return InMemoryGitHub()


@pytest.fixture
def github(fake_github) -> GitHubIntegration:
    return GitHubIntegration(client=fake_github)


@pytest.fixture
def tracker(github) -> Tracker:
    return Tracker(Ledger(), [github])


@pytest.fixture
def repo_name() -> str:
    return "acme/widgets"


@pytest.fixture
def issue(fake_github, repo_name):
    """A pre-existing open issue with one label, as a starting state."""
    return fake_github.get_repo(repo_name).create_issue(
        title="Original title", body="Original body", labels=["triage"]
    )
