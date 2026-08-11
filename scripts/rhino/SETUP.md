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

**Ask which they want before you start**, because it changes whether step 1 is
needed at all:

| They want to model from | Do steps | Notes |
| --- | --- | --- |
| a Claude Code session | 1, 2, 3, 4 | what you are in right now |
| the chat window (`RHINO-CHAT.cmd`) | 2, 3, 4 | **skip step 1** |
| both | 1, 2, 3, 4 | |

### Optional extras, only if they ask for video

The window attaches videos by pulling six frames out of the clip and, if it
can, transcribing what was said. Both tools are looked for at attach time, so
installing either later just works and **neither is needed for anything else** —
without them, videos are the only thing that does not work:

| For | Install | Needed for |
| --- | --- | --- |
| frames | `winget install Gyan.FFmpeg` (Windows), `brew install ffmpeg` (macOS) | attaching a video at all |
| speech | `pip install faster-whisper` | the spoken transcript; runs locally, uploads nothing |

Do not install these by default. Whisper downloads a few hundred MB of model
weights the first time it transcribes, which is a surprise nobody asked for if
they only ever attach photos.

The chat window does not use the registration from step 1. It passes its own
MCP config inline with `--strict-mcp-config`, pointing at the same Python that
is running the window — so registering is neither required nor read. Steps 2 to
4 it does need, because it talks to the same broker and poller.

If they have no preference, the chat window is the one to set up: it needs no
Claude Code knowledge, and it has a Settings screen with everything in it.

## 1. Register the MCP server

*(Skip this if they only want the chat window.)*

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
  task. Do **not** just `Start-Process` — Windows puts a terminal session in a
  job object and kills the whole tree when it closes, so the broker dies with
  your shell.
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

## 5. Open the chat window

*(Only if they want it. Skip to step 6 otherwise.)*

Double-click `RHINO-CHAT.cmd` in this folder, or run `rhino-chat.py` with the
same Python. It needs the `claude` CLI on PATH and an existing Claude
subscription — no API key, and adding one would be a step backwards.

If `claude` is missing, the window's Settings screen shows the install command
for the platform it is running on. Note that on Windows the CLI is sometimes
installed inside Claude Desktop rather than on PATH, so "not found" does not
mean "not installed" — check
`%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\claude-code\` before
telling them to install anything.

## 6. Prove it works

Do not report success from the absence of errors.

- If you did step 1, call your own `rhino_state` tool. A healthy answer names
  the document and its units.
- If you did not, ask the window for something trivial — "what document is
  open?" — and watch it answer.

Either way, say what you found: "connected, the open document is X in
millimetres". A specific answer is the only thing that distinguishes a working
bridge from one that has not been tried.

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
