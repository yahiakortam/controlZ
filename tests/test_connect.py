"""Bundled specs, `cz connect`, and `cz status`.

The friction these remove is the whole point, so the tests are mostly about the
generated configuration being *correct* — a config that looks right and does not
work is worse than no config, because the agent runs fine and records nothing.
"""

import json
import sys
from pathlib import Path

import pytest

from controlz import Reversibility
from controlz.connect import (
    claude_desktop_config_path,
    connect,
    cz_executable,
    proxy_command,
    resolve_env,
)
from controlz.specs import SERVERS, bundled, load, resolve


class TestBundledSpecs:
    def test_the_shipped_names(self):
        assert bundled() == ["filesystem", "github"]

    def test_a_name_resolves_to_a_packaged_file(self):
        path = resolve("github")
        assert path.name == "github.yaml"
        assert path.exists()
        # Inside the package, so it survives installation.
        assert "controlz" in path.parts

    def test_a_path_still_works(self, tmp_path):
        custom = tmp_path / "mine.yaml"
        custom.write_text("tool: mine\noperations: {}\n", encoding="utf-8")
        assert resolve(custom) == custom

    def test_an_unknown_name_lists_what_there_is(self):
        with pytest.raises(FileNotFoundError, match="filesystem, github"):
            resolve("hubspot")

    def test_load_returns_a_usable_spec(self):
        spec = load("filesystem")
        assert spec.tool == "filesystem"
        assert spec.operations["write_file"].reversibility is Reversibility.COMPENSATABLE

    def test_every_bundled_name_has_a_file_and_loads(self):
        for name in bundled():
            assert SERVERS[name].spec_path.exists()
            assert load(name).operations


class TestLaunchCommands:
    def test_filesystem_is_given_a_directory(self, tmp_path):
        command = SERVERS["filesystem"].launch_command(str(tmp_path))
        assert command[-1] == str(tmp_path.resolve())

    def test_filesystem_refuses_without_one(self):
        """Confining it to a directory is the server's safety boundary."""
        with pytest.raises(ValueError, match="directory"):
            SERVERS["filesystem"].launch_command()

    def test_github_takes_no_path(self):
        assert SERVERS["github"].launch_command() == [
            "npx",
            "-y",
            "@modelcontextprotocol/server-github",
        ]


class TestExecutableResolution:
    def test_it_prefers_the_interpreter_it_is_running_under(self):
        """A `cz` on PATH may be a version-manager shim for a different install.

        An agent launches this with no shell and no activated virtualenv, so a
        shim would resolve to an interpreter without ControlZ in it.
        """
        chosen = cz_executable()
        beside = Path(sys.executable).parent / "cz"
        if beside.exists():
            assert chosen == str(beside)

    def test_the_command_points_at_a_real_executable(self):
        command = proxy_command(SERVERS["github"], ledger=Path("/tmp/l.json"))
        assert Path(command[0]).exists()

    def test_the_command_carries_spec_ledger_and_upstream(self, tmp_path):
        ledger = tmp_path / "run.json"
        command = proxy_command(SERVERS["github"], ledger=ledger)

        assert "proxy" in command
        assert command[command.index("--spec") + 1] == "github"
        assert command[command.index("--ledger") + 1] == str(ledger)
        # Everything after the separator is the upstream, untouched.
        assert command[command.index("--") + 1 :] == SERVERS["github"].launch_command()

    def test_a_policy_is_included_when_given(self, tmp_path):
        command = proxy_command(
            SERVERS["github"], ledger=tmp_path / "l.json", policy=tmp_path / "p.yaml"
        )
        assert "--policy" in command


class TestRequiredEnvironment:
    def test_it_reads_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "from-env")
        assert resolve_env(SERVERS["github"], {}) == {"GITHUB_PERSONAL_ACCESS_TOKEN": "from-env"}

    def test_an_explicit_value_wins(self, monkeypatch):
        monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "from-env")
        env = resolve_env(SERVERS["github"], {"GITHUB_PERSONAL_ACCESS_TOKEN": "explicit"})
        assert env["GITHUB_PERSONAL_ACCESS_TOKEN"] == "explicit"

    def test_it_says_what_is_missing_and_why(self, monkeypatch):
        monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)
        with pytest.raises(SystemExit, match="a GitHub token with repo access"):
            resolve_env(SERVERS["github"], {})

    def test_a_server_needing_nothing_is_fine(self):
        assert resolve_env(SERVERS["filesystem"], {}) == {}


class TestConnect:
    def test_print_mode_changes_nothing_and_yields_a_snippet(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "t")
        result = connect("github", client="print", ledger=tmp_path / "l.json")

        assert result["written"] is None
        snippet = json.loads(result["snippet"])
        entry = snippet["mcpServers"]["controlz-github"]
        assert entry["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": "t"}
        assert "proxy" in entry["args"]

    def test_it_writes_claude_desktop_config(self, tmp_path, monkeypatch):
        config = tmp_path / "claude_desktop_config.json"
        monkeypatch.setattr("controlz.connect.claude_desktop_config_path", lambda: config)
        result = connect("filesystem", client="claude-desktop", path=str(tmp_path))

        written = json.loads(config.read_text())
        assert "controlz-filesystem" in written["mcpServers"]
        assert result["written"] == str(config)

    def test_it_preserves_other_servers_in_that_config(self, tmp_path, monkeypatch):
        config = tmp_path / "claude_desktop_config.json"
        config.write_text(
            json.dumps({"mcpServers": {"something-else": {"command": "x"}}}), encoding="utf-8"
        )
        monkeypatch.setattr("controlz.connect.claude_desktop_config_path", lambda: config)
        connect("filesystem", client="claude-desktop", path=str(tmp_path))

        written = json.loads(config.read_text())
        assert "something-else" in written["mcpServers"]
        assert "controlz-filesystem" in written["mcpServers"]

    def test_it_refuses_to_clobber_unreadable_config(self, tmp_path, monkeypatch):
        config = tmp_path / "claude_desktop_config.json"
        config.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr("controlz.connect.claude_desktop_config_path", lambda: config)
        with pytest.raises(SystemExit, match="not valid JSON"):
            connect("filesystem", client="claude-desktop", path=str(tmp_path))
        assert config.read_text() == "{not json"

    def test_the_default_ledger_is_named_after_the_server(self, tmp_path, monkeypatch):
        monkeypatch.setattr("controlz.connect.LEDGER_HOME", tmp_path)
        result = connect("filesystem", client="print", path=str(tmp_path))
        assert result["ledger"] == tmp_path / "filesystem.json"

    def test_an_unknown_server_is_refused(self):
        with pytest.raises(SystemExit, match="filesystem, github"):
            connect("hubspot", client="print")

    def test_filesystem_without_a_directory_is_refused(self):
        with pytest.raises(SystemExit, match="needs a directory"):
            connect("filesystem", client="print")

    def test_an_unknown_client_is_refused(self, tmp_path):
        with pytest.raises(SystemExit, match="unknown client"):
            connect("filesystem", client="emacs", path=str(tmp_path))

    def test_the_name_can_be_overridden(self, tmp_path):
        result = connect("filesystem", client="print", path=str(tmp_path), server_name="my-files")
        assert result["name"] == "my-files"
        assert "my-files" in json.loads(result["snippet"])["mcpServers"]


class TestDesktopConfigPath:
    def test_it_is_platform_appropriate(self):
        path = claude_desktop_config_path()
        assert path.name == "claude_desktop_config.json"
        if sys.platform == "darwin":
            assert "Application Support" in str(path)


class TestCli:
    def test_connect_list_needs_no_arguments(self, capsys):
        from controlz.cli import main

        assert main(["connect", "--list"]) == 0
        out = capsys.readouterr().out
        assert "github" in out
        assert "filesystem" in out

    def test_connect_with_no_server_lists_them(self, capsys):
        from controlz.cli import main

        assert main(["connect"]) == 0
        assert "filesystem" in capsys.readouterr().out

    def test_status_with_nothing_recorded(self, tmp_path, monkeypatch, capsys):
        from controlz.cli import main

        monkeypatch.setattr("controlz.connect.LEDGER_HOME", tmp_path)
        assert main(["status"]) == 0
        assert "nothing recorded yet" in capsys.readouterr().out

    def test_status_summarises_each_ledger(self, tmp_path, monkeypatch, capsys):
        from controlz.cli import main
        from controlz.ledger import Ledger
        from controlz.models import Session

        ledger = Ledger(Session(agent="x"), path=tmp_path / "github.json")
        ledger.record(tool="github", api_call="close_issue", reversibility=Reversibility.REVERSIBLE)
        ledger.record(tool="github", api_call="wire", reversibility=Reversibility.IRREVERSIBLE)
        ledger.save()

        monkeypatch.setattr("controlz.connect.LEDGER_HOME", tmp_path)
        assert main(["status"]) == 0
        out = capsys.readouterr().out
        assert "github" in out
        assert "wire" in out  # the thing that cannot be taken back is named
        assert "cannot be taken back" in out

    def test_status_survives_an_unreadable_ledger(self, tmp_path, monkeypatch, capsys):
        from controlz.cli import main

        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setattr("controlz.connect.LEDGER_HOME", tmp_path)

        assert main(["status"]) == 0
        assert "unreadable" in capsys.readouterr().out


class TestProvenance:
    """A ledger has to know how it was recorded, or it cannot be undone later."""

    def test_the_proxy_records_how_to_reconnect(self):
        from controlz.mcp import ControlZProxy, ServerSpec

        proxy = ControlZProxy(session=None, spec=ServerSpec.unconfigured("notes"))
        proxy.record_provenance("github", ["npx", "-y", "some-server"])

        recorded = proxy.ledger.session.metadata["controlz"]
        assert recorded["kind"] == "mcp"
        assert recorded["spec"] == "github"
        assert recorded["command"] == ["npx", "-y", "some-server"]

    def test_it_survives_a_ledger_round_trip(self, tmp_path):
        from controlz.ledger import Ledger
        from controlz.mcp import ControlZProxy, ServerSpec

        proxy = ControlZProxy(session=None, spec=ServerSpec.unconfigured("notes"))
        proxy.record_provenance("github", ["npx", "-y", "some-server"])
        path = proxy.ledger.save(tmp_path / "run.json")

        reloaded = Ledger.load(path)
        assert reloaded.session.metadata["controlz"]["command"] == [
            "npx",
            "-y",
            "some-server",
        ]

    def test_no_credentials_are_stored(self, tmp_path, monkeypatch):
        """The command is recorded; the token that made it work is not."""
        monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "super-secret")
        from controlz.mcp import ControlZProxy, ServerSpec

        proxy = ControlZProxy(session=None, spec=ServerSpec.unconfigured("notes"))
        proxy.record_provenance("github", SERVERS["github"].launch_command())
        path = proxy.ledger.save(tmp_path / "run.json")

        assert "super-secret" not in path.read_text()
