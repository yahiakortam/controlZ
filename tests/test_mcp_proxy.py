"""The MCP proxy, running against a real in-process MCP server.

No mocks: an actual `MCPServer` with tools that mutate real state, an actual
client speaking the protocol, and the proxy in between. If the protocol shape
changes under us, these fail.
"""

import json

import pytest
from mcp import Client, types
from mcp.server.mcpserver import MCPServer

from controlz import Ledger, Policy, Reversibility, RollbackOutcome, Session, Tracker
from controlz.mcp import ControlZProxy, MCPIntegration, ServerSpec


class Notes:
    """The world the upstream server acts on."""

    def __init__(self):
        self.notes: dict[int, dict] = {}
        self.next_id = 1
        self.emails_sent: list[str] = []


@pytest.fixture
def world() -> Notes:
    return Notes()


@pytest.fixture
def upstream(world) -> MCPServer:
    """A small but genuine MCP server: create, rename, archive, and send."""
    server = MCPServer("notes")

    @server.tool()
    def create_note(title: str) -> str:
        """Create a note."""
        note_id = world.next_id
        world.next_id += 1
        world.notes[note_id] = {"id": note_id, "title": title, "archived": False}
        return json.dumps(world.notes[note_id])

    @server.tool()
    def get_note(note_id: int) -> str:
        """Read a note. Changes nothing."""
        return json.dumps(world.notes.get(note_id) or {})

    @server.tool()
    def rename_note(note_id: int, title: str) -> str:
        """Rename a note."""
        world.notes[note_id]["title"] = title
        return json.dumps(world.notes[note_id])

    @server.tool()
    def archive_note(note_id: int) -> str:
        """Archive a note."""
        world.notes[note_id]["archived"] = True
        return json.dumps(world.notes[note_id])

    @server.tool()
    def unarchive_note(note_id: int) -> str:
        """Unarchive a note."""
        world.notes[note_id]["archived"] = False
        return json.dumps(world.notes[note_id])

    @server.tool()
    def delete_note(note_id: int) -> str:
        """Delete a note permanently."""
        world.notes.pop(note_id, None)
        return json.dumps({"deleted": note_id})

    @server.tool()
    def send_email(to: str) -> str:
        """Send an email. Cannot be unsent."""
        world.emails_sent.append(to)
        return json.dumps({"sent": to})

    return server


#: What ControlZ has been *told* about the notes server. Nothing is inferred.
SPEC = ServerSpec.model_validate(
    {
        "tool": "notes",
        "operations": {
            "create_note": {
                "reversibility": "compensatable",
                "undo": {"tool": "delete_note", "args": {"note_id": "$result.id"}},
            },
            "rename_note": {
                "reversibility": "reversible",
                "read": {"tool": "get_note", "args": {"note_id": "$args.note_id"}},
                "undo": {
                    "tool": "rename_note",
                    # $before is the point: the title as it was *before* the
                    # rename. Without a declared read tool this is unknowable,
                    # and the undo would restore the wrong value.
                    "args": {"note_id": "$args.note_id", "title": "$before.title"},
                },
                "conflict_fields": ["title"],
            },
            "archive_note": {
                "reversibility": "reversible",
                "undo": {"tool": "unarchive_note", "args": {"note_id": "$args.note_id"}},
            },
            "send_email": {"reversibility": "irreversible"},
        },
    }
)


async def proxied(upstream, spec=SPEC, **kwargs):
    """Open a client to the upstream and wrap it in a proxy."""
    client = Client(upstream)
    session = await client.__aenter__()
    proxy = ControlZProxy(session, spec=spec, **kwargs)
    return client, proxy


class TestPassThrough:
    async def test_the_tool_list_is_forwarded_verbatim(self, upstream):
        client, proxy = await proxied(upstream)
        try:
            direct = await proxy.session.list_tools()
            through = await proxy.list_tools()

            assert [t.name for t in through.tools] == [t.name for t in direct.tools]
            # Schemas too: the agent must not behave differently through ControlZ.
            assert [t.input_schema for t in through.tools] == [t.input_schema for t in direct.tools]
        finally:
            await client.__aexit__(None, None, None)

    async def test_a_call_reaches_the_upstream(self, upstream, world):
        client, proxy = await proxied(upstream)
        try:
            result = await proxy.call_tool(
                None, types.CallToolRequestParams(name="create_note", arguments={"title": "Hi"})
            )
            assert not result.is_error
            assert world.notes[1]["title"] == "Hi"
        finally:
            await client.__aexit__(None, None, None)

    async def test_the_result_is_returned_unchanged(self, upstream):
        client, proxy = await proxied(upstream)
        try:
            through = await proxy.call_tool(
                None, types.CallToolRequestParams(name="create_note", arguments={"title": "Hi"})
            )
            payload = json.loads(through.content[0].text)
            assert payload == {"id": 1, "title": "Hi", "archived": False}
        finally:
            await client.__aexit__(None, None, None)


class TestRecording:
    async def test_calls_land_in_the_ledger(self, upstream):
        client, proxy = await proxied(upstream)
        try:
            for title in ("one", "two"):
                await proxy.call_tool(
                    None,
                    types.CallToolRequestParams(name="create_note", arguments={"title": title}),
                )

            assert len(proxy.ledger) == 2
            action = proxy.ledger.actions[0]
            assert action.tool == "notes"
            assert action.api_call == "create_note"
            assert action.args == {"title": "one"}
            assert action.state_after["result"]["id"] == 1
        finally:
            await client.__aexit__(None, None, None)

    async def test_classification_comes_from_the_config(self, upstream):
        client, proxy = await proxied(upstream)
        try:
            await proxy.call_tool(
                None, types.CallToolRequestParams(name="create_note", arguments={"title": "x"})
            )
            await proxy.call_tool(
                None, types.CallToolRequestParams(name="send_email", arguments={"to": "a@b.c"})
            )

            assert proxy.ledger.actions[0].reversibility is Reversibility.COMPENSATABLE
            assert proxy.ledger.actions[1].reversibility is Reversibility.IRREVERSIBLE
        finally:
            await client.__aexit__(None, None, None)

    async def test_an_unconfigured_tool_is_unknown_not_a_guess(self, upstream):
        """delete_note is a real upstream tool that the config never mentions."""
        client, proxy = await proxied(upstream)
        try:
            await proxy.call_tool(
                None, types.CallToolRequestParams(name="create_note", arguments={"title": "x"})
            )
            await proxy.call_tool(
                None, types.CallToolRequestParams(name="delete_note", arguments={"note_id": 1})
            )

            assert proxy.ledger.actions[1].reversibility is Reversibility.UNKNOWN
            assert proxy.ledger.actions[1].rollback_plan is None
        finally:
            await client.__aexit__(None, None, None)

    async def test_unconfigured_server_records_everything_and_undoes_nothing(self, upstream):
        client, proxy = await proxied(upstream, spec=ServerSpec.unconfigured("notes"))
        try:
            await proxy.call_tool(
                None, types.CallToolRequestParams(name="create_note", arguments={"title": "x"})
            )
            report = await proxy.tracker.arollback()

            assert len(proxy.ledger) == 1
            assert report.restored == []
            assert len(report.skipped_irreversible) == 1
            assert "unknown" in report.skipped_irreversible[0].reason
        finally:
            await client.__aexit__(None, None, None)


class TestUndo:
    async def test_a_declared_undo_runs(self, upstream, world):
        client, proxy = await proxied(upstream)
        try:
            await proxy.call_tool(
                None,
                types.CallToolRequestParams(name="create_note", arguments={"title": "Original"}),
            )
            await proxy.call_tool(
                None,
                types.CallToolRequestParams(
                    name="rename_note", arguments={"note_id": 1, "title": "WRONG"}
                ),
            )
            assert world.notes[1]["title"] == "WRONG"

            entry = await proxy.tracker.arollback_action(proxy.ledger.actions[1])

            assert entry.outcome is RollbackOutcome.RESTORED
            assert world.notes[1]["title"] == "Original"  # from $before.title
        finally:
            await client.__aexit__(None, None, None)

    async def test_placeholders_resolve_from_args_and_result(self, upstream, world):
        client, proxy = await proxied(upstream)
        try:
            await proxy.call_tool(
                None, types.CallToolRequestParams(name="create_note", arguments={"title": "n"})
            )
            plan = proxy.ledger.actions[0].rollback_plan

            assert plan.strategy == "declared-undo:delete_note"
            assert plan.steps[0].args == {"note_id": 1}  # from $result.id
        finally:
            await client.__aexit__(None, None, None)

    async def test_a_whole_session_rewinds(self, upstream, world):
        client, proxy = await proxied(upstream)
        try:
            await proxy.call_tool(
                None,
                types.CallToolRequestParams(name="create_note", arguments={"title": "Keep"}),
            )
            await proxy.call_tool(
                None,
                types.CallToolRequestParams(
                    name="rename_note", arguments={"note_id": 1, "title": "WRONG"}
                ),
            )
            await proxy.call_tool(
                None, types.CallToolRequestParams(name="archive_note", arguments={"note_id": 1})
            )

            report = await proxy.tracker.arollback()

            assert len(report.restored) == 3
            # create_note's undo is deletion, so the note is gone entirely.
            assert world.notes == {}
        finally:
            await client.__aexit__(None, None, None)

    async def test_the_irreversible_call_is_reported_not_undone(self, upstream, world):
        client, proxy = await proxied(upstream)
        try:
            await proxy.call_tool(
                None, types.CallToolRequestParams(name="create_note", arguments={"title": "n"})
            )
            await proxy.call_tool(
                None, types.CallToolRequestParams(name="send_email", arguments={"to": "a@b.c"})
            )

            report = await proxy.tracker.arollback()

            assert len(report.skipped_irreversible) == 1
            assert "irreversible" in report.skipped_irreversible[0].reason
            assert not report.fully_restored
            assert world.emails_sent == ["a@b.c"]  # still sent
        finally:
            await client.__aexit__(None, None, None)


class TestReadingPriorState:
    """A declared read tool is what makes restoring a previous value possible."""

    async def test_prior_state_is_captured(self, upstream, world):
        client, proxy = await proxied(upstream)
        try:
            await proxy.call_tool(
                None,
                types.CallToolRequestParams(name="create_note", arguments={"title": "First"}),
            )
            await proxy.call_tool(
                None,
                types.CallToolRequestParams(
                    name="rename_note", arguments={"note_id": 1, "title": "Second"}
                ),
            )
            rename = proxy.ledger.actions[1]

            assert rename.state_before["captured"] is True
            assert rename.state_before["before"]["title"] == "First"
            assert rename.state_after["after"]["title"] == "Second"
        finally:
            await client.__aexit__(None, None, None)

    async def test_without_a_read_tool_nothing_is_captured(self, upstream):
        client, proxy = await proxied(upstream)
        try:
            await proxy.call_tool(
                None, types.CallToolRequestParams(name="create_note", arguments={"title": "x"})
            )
            created = proxy.ledger.actions[0]

            # create_note declares no read tool, and the record says so plainly.
            assert created.state_before["captured"] is False
            assert "before" not in created.state_before
            assert "not conflict-checked" in created.rollback_plan.notes
        finally:
            await client.__aexit__(None, None, None)

    async def test_a_drifted_value_is_refused(self, upstream, world):
        client, proxy = await proxied(upstream)
        try:
            await proxy.call_tool(
                None,
                types.CallToolRequestParams(name="create_note", arguments={"title": "First"}),
            )
            await proxy.call_tool(
                None,
                types.CallToolRequestParams(
                    name="rename_note", arguments={"note_id": 1, "title": "Agent"}
                ),
            )
            # Someone else renames it before the rollback runs.
            world.notes[1]["title"] = "A human was here"

            entry = await proxy.tracker.arollback_action(proxy.ledger.actions[1])

            assert entry.outcome is RollbackOutcome.CONFLICT
            assert entry.conflicts[0].field == "title"
            assert world.notes[1]["title"] == "A human was here"
        finally:
            await client.__aexit__(None, None, None)

    async def test_an_unrelated_change_does_not_block(self, upstream, world):
        """conflict_fields narrows the comparison to what the undo overwrites."""
        client, proxy = await proxied(upstream)
        try:
            await proxy.call_tool(
                None,
                types.CallToolRequestParams(name="create_note", arguments={"title": "First"}),
            )
            await proxy.call_tool(
                None,
                types.CallToolRequestParams(
                    name="rename_note", arguments={"note_id": 1, "title": "Agent"}
                ),
            )
            # A field the rename never touched, and the undo will not overwrite.
            world.notes[1]["archived"] = True

            entry = await proxy.tracker.arollback_action(proxy.ledger.actions[1])

            assert entry.outcome is RollbackOutcome.RESTORED
            assert world.notes[1]["title"] == "First"
            assert world.notes[1]["archived"] is True  # left alone
        finally:
            await client.__aexit__(None, None, None)

    async def test_a_conflict_can_be_forced(self, upstream, world):
        client, proxy = await proxied(upstream)
        try:
            await proxy.call_tool(
                None,
                types.CallToolRequestParams(name="create_note", arguments={"title": "First"}),
            )
            await proxy.call_tool(
                None,
                types.CallToolRequestParams(
                    name="rename_note", arguments={"note_id": 1, "title": "Agent"}
                ),
            )
            world.notes[1]["title"] = "A human was here"

            entry = await proxy.tracker.arollback_action(proxy.ledger.actions[1], force=True)

            assert entry.outcome is RollbackOutcome.RESTORED
            assert world.notes[1]["title"] == "First"
        finally:
            await client.__aexit__(None, None, None)


class TestPolicyGate:
    async def test_a_blocked_call_never_reaches_the_upstream(self, upstream, world):
        """The whole point of gating at the proxy."""
        policy = Policy(minimum_score=0, on_irreversible=__import__("controlz").Decision.BLOCK)
        client, proxy = await proxied(upstream, policy=policy)
        try:
            result = await proxy.call_tool(
                None, types.CallToolRequestParams(name="send_email", arguments={"to": "a@b.c"})
            )

            assert result.is_error
            assert "ControlZ refused" in result.content[0].text
            assert world.emails_sent == []  # never sent
            assert proxy.ledger.actions == []  # never happened, never recorded
        finally:
            await client.__aexit__(None, None, None)

    async def test_the_refusal_explains_itself_to_the_agent(self, upstream):
        from controlz import Decision

        policy = Policy(minimum_score=0, on_unknown=Decision.BLOCK)
        client, proxy = await proxied(upstream, policy=policy)
        try:
            result = await proxy.call_tool(
                None, types.CallToolRequestParams(name="delete_note", arguments={"note_id": 1})
            )
            text = result.content[0].text

            assert result.is_error
            assert "on_unknown" in text
            assert "unclassified" in text
        finally:
            await client.__aexit__(None, None, None)

    async def test_an_allowed_call_still_goes_through(self, upstream, world):
        from controlz import Decision

        policy = Policy(minimum_score=0, on_unknown=Decision.BLOCK)
        client, proxy = await proxied(upstream, policy=policy)
        try:
            result = await proxy.call_tool(
                None, types.CallToolRequestParams(name="create_note", arguments={"title": "ok"})
            )
            assert not result.is_error
            assert world.notes[1]["title"] == "ok"
        finally:
            await client.__aexit__(None, None, None)

    async def test_approval_can_be_async(self, upstream, world):
        from controlz import Decision

        asked = []

        async def approve(decision):
            asked.append(decision)
            return True

        policy = Policy(minimum_score=0, on_irreversible=Decision.REQUIRE_APPROVAL)
        client, proxy = await proxied(upstream, policy=policy, approve=approve)
        try:
            result = await proxy.call_tool(
                None, types.CallToolRequestParams(name="send_email", arguments={"to": "a@b.c"})
            )
            assert not result.is_error
            assert len(asked) == 1
            assert world.emails_sent == ["a@b.c"]
        finally:
            await client.__aexit__(None, None, None)


class TestSpecLoading:
    def test_from_yaml(self, tmp_path):
        path = tmp_path / "notes.yaml"
        path.write_text(
            "tool: notes\n"
            "operations:\n"
            "  create_note:\n"
            "    reversibility: compensatable\n"
            "    undo:\n"
            "      tool: delete_note\n"
            "      args: {note_id: '$result.id'}\n",
            encoding="utf-8",
        )
        spec = ServerSpec.from_yaml(path)

        assert spec.tool == "notes"
        assert spec.operations["create_note"].reversibility is Reversibility.COMPENSATABLE
        assert spec.operations["create_note"].undo.tool == "delete_note"

    def test_unknown_keys_are_rejected(self):
        with pytest.raises(ValueError):
            ServerSpec.model_validate({"tool": "x", "operations": {"a": {"reversible": True}}})

    def test_unconfigured_knows_nothing(self):
        spec = ServerSpec.unconfigured("x")
        assert spec.operations == {}


class TestSyncIsRefused:
    """The proxy is async by nature; the sync half must say so, not misbehave."""

    def test_sync_methods_raise_clearly(self):
        from controlz.integrations import IntegrationError
        from controlz.models import Operation

        integration = MCPIntegration(session=None, spec=SPEC)
        for call in (
            lambda: integration.execute(Operation(tool="notes", api_call="x")),
            lambda: integration.snapshot(Operation(tool="notes", api_call="x")),
        ):
            with pytest.raises(IntegrationError, match="async only"):
                call()


class TestLedgerIntegration:
    async def test_the_session_saves_and_reloads(self, upstream, tmp_path):
        ledger = Ledger(Session(agent="mcp"), path=tmp_path / "run.json", autosave=True)
        client, proxy = await proxied(upstream, ledger=ledger)
        try:
            await proxy.call_tool(
                None, types.CallToolRequestParams(name="create_note", arguments={"title": "x"})
            )
        finally:
            await client.__aexit__(None, None, None)

        reloaded = Ledger.load(tmp_path / "run.json")
        assert len(reloaded) == 1
        assert reloaded.actions[0].api_call == "create_note"

    async def test_a_reloaded_session_can_be_scored(self, upstream, tmp_path):
        from controlz import reversibility_score

        client, proxy = await proxied(upstream)
        try:
            await proxy.call_tool(
                None, types.CallToolRequestParams(name="create_note", arguments={"title": "x"})
            )
            await proxy.call_tool(
                None, types.CallToolRequestParams(name="send_email", arguments={"to": "a@b.c"})
            )
        finally:
            await client.__aexit__(None, None, None)

        score = reversibility_score(proxy.ledger.actions)
        assert score.total == 2
        assert score.irreversible == 1
        assert score.coverage == 25.0


class TestBuildServer:
    def test_the_server_registers_both_handlers(self, upstream):
        proxy = ControlZProxy(session=None, spec=SPEC)
        server = proxy.build_server()

        assert server.get_request_handler("tools/list") is not None
        assert server.get_request_handler("tools/call") is not None

    def test_the_proxy_names_itself_after_the_upstream(self):
        proxy = ControlZProxy(session=None, spec=SPEC)
        assert proxy.name == "controlz(notes)"

    def test_a_tracker_is_wired_up(self):
        proxy = ControlZProxy(session=None, spec=SPEC)
        assert isinstance(proxy.tracker, Tracker)
        assert proxy.tracker.tools == ["notes"]


class TestEndToEndOverStdio:
    """The path a real user takes: `cz proxy` launched as an MCP server.

    Spawns the CLI as a subprocess with a real upstream behind it, and talks to
    it as any MCP client would. Slower than the in-process tests, and worth it:
    this is the only thing that exercises serve_stdio and the CLI wiring.
    """

    UPSTREAM = '''
import json
from mcp.server.mcpserver import MCPServer

NOTES = {}
server = MCPServer("notes")

@server.tool()
def create_note(title: str) -> str:
    """Create a note."""
    nid = len(NOTES) + 1
    NOTES[nid] = {"id": nid, "title": title}
    return json.dumps(NOTES[nid])

@server.tool()
def send_email(to: str) -> str:
    """Send an email. Cannot be unsent."""
    return json.dumps({"sent": to})

if __name__ == "__main__":
    import anyio
    anyio.run(server.run_stdio_async)
'''

    SPEC_YAML = (
        "tool: notes\n"
        "operations:\n"
        "  create_note:\n"
        "    reversibility: compensatable\n"
        "  send_email:\n"
        "    reversibility: irreversible\n"
    )
    POLICY_YAML = "minimum_score: 0\non_irreversible: block\n"

    async def test_a_real_client_sees_the_tools_and_the_gate(self, tmp_path):
        import sys

        from mcp import StdioServerParameters

        upstream = tmp_path / "upstream.py"
        upstream.write_text(self.UPSTREAM, encoding="utf-8")
        spec = tmp_path / "spec.yaml"
        spec.write_text(self.SPEC_YAML, encoding="utf-8")
        policy = tmp_path / "policy.yaml"
        policy.write_text(self.POLICY_YAML, encoding="utf-8")
        ledger = tmp_path / "run.json"

        proxy = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "controlz.cli",
                "proxy",
                "--spec",
                str(spec),
                "--policy",
                str(policy),
                "--ledger",
                str(ledger),
                "--",
                sys.executable,
                str(upstream),
            ],
        )

        async with Client(proxy) as session:
            tools = await session.list_tools()
            assert [t.name for t in tools.tools] == ["create_note", "send_email"]

            allowed = await session.call_tool("create_note", {"title": "Through ControlZ"})
            assert not allowed.is_error
            assert json.loads(allowed.content[0].text)["title"] == "Through ControlZ"

            refused = await session.call_tool("send_email", {"to": "a@b.c"})
            assert refused.is_error
            assert "ControlZ refused this call" in refused.content[0].text

        # The allowed call was recorded; the blocked one never happened.
        reloaded = Ledger.load(ledger)
        assert [a.api_call for a in reloaded.actions] == ["create_note"]
        assert reloaded.actions[0].reversibility is Reversibility.COMPENSATABLE


class TestShippedExampleSpec:
    """The example that ships must parse and mean what it says."""

    def test_it_parses(self):
        from pathlib import Path

        spec = ServerSpec.from_yaml(
            Path(__file__).resolve().parents[1] / "examples" / "mcp-github.yaml"
        )
        assert spec.tool == "github"
        assert spec.operations["create_issue"].reversibility is Reversibility.COMPENSATABLE
        assert spec.operations["merge_pull_request"].reversibility is Reversibility.IRREVERSIBLE

    def test_only_the_operation_with_a_read_tool_claims_full_reversibility(self):
        from pathlib import Path

        spec = ServerSpec.from_yaml(
            Path(__file__).resolve().parents[1] / "examples" / "mcp-github.yaml"
        )
        for name, operation in spec.operations.items():
            if operation.reversibility is Reversibility.REVERSIBLE:
                assert operation.read is not None, (
                    f"{name} claims REVERSIBLE without a read tool, so it cannot know "
                    "the value it would restore"
                )


class TestSpecChecking:
    """A spec naming a tool the server does not have is the worst silent failure.

    The classification promises recovery, the score counts it, and the failure
    only appears when someone actually needs the undo.
    """

    def test_referenced_tools_are_collected(self):
        referenced = SPEC.referenced_tools()

        assert "create_note" in referenced
        assert "delete_note" in referenced  # named only as an undo
        assert "get_note" in referenced  # named only as a read
        assert referenced["get_note"] == ["read for rename_note"]

    def test_a_missing_tool_is_reported(self):
        problems = SPEC.check_against({"create_note", "rename_note", "get_note"})

        assert any("delete_note" in p for p in problems)
        assert any("undo for create_note" in p for p in problems)

    def test_a_complete_server_reports_nothing(self):
        available = set(SPEC.referenced_tools()) | {"unarchive_note"}
        assert SPEC.check_against(available) == []

    async def test_the_proxy_checks_against_the_live_server(self, upstream):
        client, proxy = await proxied(upstream)
        try:
            assert await proxy.check_spec() == []
        finally:
            await client.__aexit__(None, None, None)

    async def test_a_wrong_spec_is_caught_against_the_live_server(self, upstream):
        broken = ServerSpec.model_validate(
            {
                "tool": "notes",
                "operations": {
                    "create_note": {
                        "reversibility": "compensatable",
                        "undo": {"tool": "obliterate_note", "args": {}},
                    }
                },
            }
        )
        client, proxy = await proxied(upstream, spec=broken)
        try:
            problems = await proxy.check_spec()
            assert len(problems) == 1
            assert "obliterate_note" in problems[0]
        finally:
            await client.__aexit__(None, None, None)

    async def test_warnings_go_to_stderr_not_stdout(self, upstream, capsys):
        """stdout carries the protocol; anything else there corrupts the stream."""
        broken = ServerSpec.model_validate(
            {
                "tool": "notes",
                "operations": {
                    "create_note": {
                        "reversibility": "compensatable",
                        "undo": {"tool": "obliterate_note", "args": {}},
                    }
                },
            }
        )
        client, proxy = await proxied(upstream, spec=broken)
        try:
            await proxy.warn_about_spec()
            captured = capsys.readouterr()

            assert "obliterate_note" in captured.err
            assert captured.out == ""
        finally:
            await client.__aexit__(None, None, None)
