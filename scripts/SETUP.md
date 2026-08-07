# Set up the Rhino bridge

You are Claude Code, and someone has asked you to set this up. Work through it
yourself rather than handing the commands back — they asked you precisely so
they would not have to run them.

Everything below is in the folder this file sits in. Use absolute paths built
from that folder, not relative ones; you may be running from anywhere.

## What you are connecting

```
you ──MCP──▶ rhino_mcp.py ──HTTP──▶ broker ──▶ poller inside Rhino
```

Three pieces: the MCP server (you talk to this), a broker on 127.0.0.1:7656,
and a poller running as a timer inside Rhino. All three must be up.

## 1. Register the MCP server

The MCP server has no dependencies and must run on **Rhino's own Python**,
because that is the interpreter guaranteed to exist on a machine with Rhino.
Find it:

- Windows: `%USERPROFILE%\.rhinocode\py39-rh8\python.exe`
- macOS: `~/.rhinocode/py*-rh*/bin/python3`

The version folder changes between Rhino releases, so glob rather than assume
`py39-rh8`. If nothing matches, Rhino has not built its Python yet — the person
needs to open Rhino and run the `ScriptEditor` command once. Tell them that and
stop; the rest cannot work without it.

Then register, with absolute paths:

```
claude mcp add rhino -- "<rhino python>" "<this folder>/rhino_mcp.py"
```

Verify with `claude mcp list`. It should report `rhino` connected. If it says
failed, run the server by hand and read what it prints — it writes nothing to
stdout except protocol, so a traceback on stderr is the real error.

## 2. Make sure a bridge token exists

Both halves authenticate with a shared secret at `~/.fusion-mcp/token`
(`%USERPROFILE%\.fusion-mcp\token` on Windows). If it is missing, create it with
64 random hex characters and lock it down:

- Windows: `icacls "<path>" /inheritance:r /grant:r "%USERNAME%":F`
  **`os.chmod(0600)` does nothing for access control on Windows** — it only
  toggles the read-only attribute, so the file stays readable by every other
  account on the machine. Use `icacls`.
- macOS/Linux: `chmod 600`

## 3. Start the broker

It must outlive the shell that starts it.

- Windows: run `broker-service.ps1` in this folder, which registers a scheduled
  task. Do **not** just `Start-Process` — Windows puts an SSH or terminal
  session in a job object and kills the whole tree when it closes, so the broker
  dies with your shell.
- macOS/Linux: `./start-broker.sh`, which stays in the foreground. Leave it
  running.

Check it: `GET http://127.0.0.1:7656/health` with the header
`X-Fusion-Bridge-Token: <token>` should return JSON.

## 4. Start the poller inside Rhino

Rhino must be open, and `ScriptEditor` must have been run once **this session** —
that is what loads Rhino's scripting host. Without it the next command reports
it cannot find a running Rhino.

```
"C:\Program Files\Rhino 8\System\RhinoCode.exe" script "<this folder>/rhino-poller.py"
```

On macOS, `RhinoCode` lives inside the Rhino app bundle; `start-poller.sh`
finds it.

The very first ever run builds Rhino's Python environment and takes about a
minute. It is not frozen — say so if the person is watching.

## 5. Prove it works

Do not report success from the absence of errors. Call your own `rhino_state`
tool. A healthy answer names the document and its units. Then say what you
found — "connected, the open document is X in millimetres" — so they can see it
is real.

If `/health` shows `poller_connected: false`, the broker is up and Rhino is not:
Rhino open, `ScriptEditor` once, then step 4 again.

## Things that will waste your time

- **Rhino restarting stops the poller.** Step 4 again after any restart. The
  broker and the conversation survive.
- **`rs.Command` from inside the poller's timer never returns.** It re-enters
  Rhino's command pipeline from a message dispatch. Use the RhinoCommon API.
- **Never call `InvokeOnUiThread`** — it is synchronous, and a rhinocode script
  already runs on the UI thread, so it deadlocks against itself.
- **Never start a thread inside Rhino.** One outliving the script context aborts
  the embedded CPython and takes Rhino down with it.
- **`rhinocode script` prints nothing** — not even tracebacks. A failing script
  looks exactly like a silent one; read `rhino-poller.log` instead.

## Never do this

Do not delete geometry by enumerating the document:

```python
for o in list(doc.Objects):     # NO
    doc.Objects.Delete(o.Id, True)
```

That empties whatever file happens to be open, which is somebody's work. If you
create test geometry, keep the GUID the create call returned and delete that
one. Read `rhino_state` before writing: a named `.3dm` with objects in it is a
project, not a scratchpad.
