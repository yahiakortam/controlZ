# Examples

## `rogue_agent.py` — an agent goes rogue, and ControlZ puts it back

```bash
python examples/rogue_agent.py
```

Runs in memory. No credentials, no network, nothing to clean up.

The whole library in one file, in five acts:

1. **The plan** — fifteen proposed actions, scored for reversibility before any of them run
2. **The gate** — the policy's verdict on that plan
3. **The mess** — the agent retitles, relabels, closes, and comments on three issues it should have left alone
4. **The rewind** — one call to `tracker.rollback()`
5. **The verdict** — what came back, and the one thing that did not

The issue tracker ends up byte-for-byte as it started. The wire transfer does not, and the report says so.

### Against a real repository

```bash
export CONTROLZ_GITHUB_TOKEN=ghp_...
python examples/rogue_agent.py --repo you/throwaway
```

⚠️ It opens issues, retitles them, relabels them, comments, and closes them — then undoes all of it. **Only point it at a repository you do not mind mutating**, and read the source first.

### Options

| flag | |
| --- | --- |
| `--repo owner/name` | act on a real GitHub repository instead of memory |
| `--ledger PATH` | where to write the session (default `rogue-agent.json`) |
| `--pace SECONDS` | delay between actions (default `0.12`) |
| `--no-rollback` | leave the mess in place, to inspect or rewind later |

With `--no-rollback`, rewind it afterwards from the terminal:

```bash
cz score rogue-agent.json      # what happened, and how recoverable it is
cz rollback rogue-agent.json   # put it back
```

## Watch it instead

```bash
cz watch --demo
```

The same chaos agent, streaming into the TUI. Press `R` to rewind.

## `mcp-github.yaml` — teaching the proxy about a real MCP server

```bash
pip install -e '.[mcp]'      # not on PyPI yet; see Quickstart
cz proxy --spec examples/mcp-github.yaml --ledger run.json \
    -- npx -y @modelcontextprotocol/server-github
```

Point your agent's MCP config at that command instead of the server's. It sees
the same tools; ControlZ records and gates every call on the way through.

The file is worth reading even if you use a different server — it shows what
each classification is claiming, and why `read` is the line that decides whether
a previous value can be restored at all.
