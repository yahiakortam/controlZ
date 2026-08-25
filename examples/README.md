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

## `cz connect github` — teaching the proxy about a real MCP server

```bash
cz connect github
```

Point your agent's MCP config at that command instead of the server's. It sees
the same tools; ControlZ records and gates every call on the way through.

The spec at `src/controlz/specs/github.yaml` is worth reading even if you use a different server — it shows what
each classification is claiming, and why `read` is the line that decides whether
a previous value can be restored at all.

## `cz connect filesystem` — undo for an agent that writes files

```bash
cz connect filesystem /path/to/project
```

Verified end to end against the real server. Overwriting a file restores the
previous contents exactly; moving one moves it back. **Creating** a file is
reported as un-restored, because that server exposes no way to delete a file —
and the report says exactly that rather than pretending:

```
2 of 3 actions restored
  not undoable: write_file — nothing was read before this call — most often the
  target did not exist yet — so content='$before.text' cannot be filled in, and
  there is no prior state to restore
```

That one absence, no delete tool, decides most of the classifications in
`src/controlz/specs/filesystem.yaml`. Reversibility is a property of what you can reach, not of the idea.
