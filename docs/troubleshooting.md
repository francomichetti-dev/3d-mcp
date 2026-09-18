# Troubleshooting

Two things to know before the table: **Fusion swallows add-in exceptions silently**, so the logs are
the only window into a failure, and all of them live in `~/.arges/` (or `~/.fusion-mcp/` on an
install made before the rename).

- **`addin.log`** — the add-in: startup, bind errors, every request, full tracebacks.
- **`server.log`** — the MCP server. It can never log to stdout; that would corrupt JSON-RPC.
- **`agent.log`** — the chat service, including anything a Fusion-spawned start printed before it
  could log for itself.

| Symptom | Likely cause |
| --- | --- |
| Arges isn't in the Add-Ins list | Fusion scans that folder at launch only — **restart Fusion**. |
| `Fusion not running or Arges add-in not enabled` | Fusion closed, or the add-in was never run. |
| Health check returns 401 | Token mismatch. Re-run `scripts/install.sh` (preserves the token) or `--rotate-token`. |
| Add-in never starts, `addin.log` says no token | `~/.arges/token` missing or empty. The listener fails closed by design. |
| Add-in loaded but port bind failed | Something else holds 127.0.0.1:7654 — the reason is in `addin.log`. |
| `version mismatch` from a tool | You edited the add-in. Stop/Run it in Fusion, or reload it (below). |
| `fusion` missing from `claude mcp list` | Re-run `scripts/install.sh`; it removes and re-adds the registration. |
| Tool call times out at the Claude Code layer | `MCP_TOOL_TIMEOUT` too low — see the README's install steps. |
| `no active Fusion design` | Open or create a document and switch to the Design workspace. |
| A burst of parallel requests gets 503 | The connection cap (8) refused the excess so a flood cannot exhaust threads inside Fusion. Send requests serially. |
| Panel opens but shows a connection error | The agent service isn't up. `scripts/fusion-chat.sh --status`, then check `agent.log`. |
| Panel says "no design" with a design clearly open | It is not a Design document (a drawing, or a non-Design workspace). The header names what the bridge sees. |
| Save says Fusion reported success but nothing was saved | Fusion is read-only — an expired subscription does that. Saving here writes a local `.f3d` instead, which still works; see the README. |
| A design's chat looks empty after reopening it | Closing a design compresses its chat by design — expand "core context from before this design was closed" at the top. |
| Two unsaved designs seem to share a chat | Neither has been messaged yet: identity is stamped on first message, so both correctly show an empty panel until then. |
| `agent.log` says `uv not found` | Fusion launched from Finder inherits a minimal `PATH`. The panel probes absolute locations; if `uv` is elsewhere, start the service from a terminal with `scripts/fusion-chat.sh`. |
| `agent.log` shows `ModuleNotFoundError: No module named 'encodings'` | Fusion's `PYTHONHOME`/`PYTHONPATH` leaked into the child. The spawn strips every `PYTHON*` variable — if you see this, the add-in is running stale code, so Stop/Run it. |

## A tool call timed out

A 504 means the *wait* was abandoned, **not that the code was cancelled** — main-thread execution
cannot be interrupted. Don't resend; check `fusion_state` or a screenshot to see what actually
happened.

The layers are staggered on purpose: the add-in waits 60 s on the main thread, the MCP server's HTTP
client waits 75 s. Your client's own tool timeout must exceed both, or it gives up while Fusion is
still working. For Claude Code, set `MCP_TOOL_TIMEOUT` (milliseconds) to at least `120000` in
`~/.claude/settings.json`:

```json
{ "env": { "MCP_TOOL_TIMEOUT": "120000" } }
```

## Reloading the add-in after an edit

```sh
curl -sS -X POST -H "X-Arges-Bridge-Token: $(cat ~/.arges/token)" \
     http://127.0.0.1:7654/reload
```

Reloads `arges_impl.py` without restarting Fusion. A change to the manifest or to `Arges.py` still
needs **Stop/Run**. Refused with 409 while an execution is in flight, 503 when the add-in is
stopped, and 400 on a syntax error — with the running bridge left untouched.

## Two Arges entries in the add-in list

If you install *and* also work on a checkout, Fusion can end up with the add-in registered twice —
once at the checkout and once under `API/AddIns`. Both load, and which one wins is a load-order
race.

It does not usually break: the loader compares `realpath`, so the second registration finds the
module already loaded and reuses it rather than raising `ImportError`. But the winner decides
whether you are running the checkout or the installed copy, and a Fusion update can flip it
silently. To see which one is live:

```python
# through fusion_execute
import sys
result = sys.modules["arges_impl"].__file__
```

Fusion's registry is `JSLoadedScriptsinfo`, under
`~/Library/Application Support/Autodesk/Autodesk Fusion 360/<id>/`. Back it up before editing, and
remove the registration you do not want along with its folder — a registration whose path no longer
exists is the ghost entry that shows up as a dead row in the add-ins panel. Fusion rewrites this
file on exit, so make the change with Fusion closed, or verify it survived a restart.
