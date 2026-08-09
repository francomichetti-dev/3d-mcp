# mcp-rhino — everything Claude needs on this machine

You are Claude Code, running on the machine where Rhino is installed. This
folder is a working copy of part of `3d-mcp`, a project that lets someone model
in CAD by asking for what they want.

It works — real parts have been modelled with it. The person you are working
with may now ask you to change or fix something. This file is what you need to
do that well.

---

## What this is

```
chat window ──▶ claude ──MCP──▶ rhino_mcp.py ──HTTP──▶ broker ──▶ poller in Rhino
```

Four pieces, three of which run on this machine:

| | |
| --- | --- |
| `rhino-chat.py` | the desktop window: chat and settings, nothing else |
| `rhino_mcp.py` | an MCP server — this is what Claude Code talks to |
| `broker.py` | a job queue on 127.0.0.1:7656 |
| `rhino-poller.py` | a timer inside Rhino that pulls work and runs it |

It runs on the Claude subscription that is already installed. There is no API
key anywhere, and adding one would be a step backwards.

**Why Rhino pulls instead of being pushed into.** Fusion lets an add-in hand
work to its main thread. Rhino's equivalent, `InvokeOnUiThread`, is synchronous
and deadlocks against itself. So a timer inside Rhino asks the broker for jobs
instead. Because that timer already runs on the UI thread, whatever it executes
is on the correct thread automatically — the problem disappears rather than
being solved. Do not try to reintroduce a listener inside Rhino; it was tried
and it crashed Rhino repeatedly.

---

## Setting it up

`SETUP.md`, in this folder, is written for you. The person can just ask you to
set it up and you should be able to, without handing them commands to run.

---

## If you are asked to change something

The repository is `github.com/francomichetti-dev/3d-mcp`. This folder is a copy
of its `scripts/`, not a clone, so you cannot commit from here.

Two ways to get a change back upstream, in order of preference:

1. **Push it.** If the person has access to the repo, clone it properly, make the
   change there, run the tests, and open a pull request. Say what you changed
   and why in the description — the commit history in this project explains
   reasoning, not just mechanics.
2. **Hand it off.** Otherwise write the change plus a short note explaining the
   problem, what you tried, and what worked, and hand that over to be sent
   on. A diff with the reasoning attached is far more useful than a description
   of a diff.

Either way: **run the test suite** (`tests/run.sh` in the repo) before saying it
works. There are 817 assertions and they are quick.

---

## What will waste your time if you do not know it

These each cost hours to discover. They are not hypothetical.

**Inside Rhino**

- **Never call `InvokeOnUiThread`.** It is synchronous, and a `rhinocode` script
  already runs on the UI thread, so it deadlocks against itself.
- **Never start a thread.** One outliving the script context aborts the embedded
  CPython and takes Rhino down — `ucrtbase.dll`, `0xc0000409`.
- **`RhinoApp.Idle` never fires** when nobody is touching Rhino. `Eto.Forms.UITimer`
  does, reliably, and that is what the poller uses.
- **`rs.Command` from inside the timer never returns.** It re-enters Rhino's
  command pipeline from a message dispatch. Use the RhinoCommon API —
  `Rhino.Display.ViewCapture`, not `_-ViewCaptureToFile`.
- **`rhinocode script` prints nothing.** Not even tracebacks. A failing script
  looks exactly like a silent one; read `rhino-poller.log`.
- **`ScriptEditor` must be run once per Rhino session** before `rhinocode` can
  see the instance. The first ever run builds the Python environment and takes
  about a minute — it is not frozen.

**On Windows**

- **`os.chmod(0600)` does not restrict access.** It only toggles the read-only
  attribute; the file stays readable by every other account. Use
  `icacls <path> /inheritance:r /grant:r "%USERNAME%":F`.
- **A process started from a shell dies with that shell.** Windows puts the
  session in a job object and kills the whole tree when it closes, so a broker
  launched with `Start-Process` goes with the terminal that started it. That is
  why it runs as a scheduled task instead.
- **`subprocess` with `text=True` uses cp1252, not UTF-8.** Anything with an
  accent or an em-dash raises `UnicodeDecodeError`. Always pass
  `encoding="utf-8", errors="replace"`. This bit twice.
- **Read stderr on a thread.** Reading it only after stdout is exhausted
  deadlocks once the child writes more than the pipe buffer holds.

---

## Never do this

Do not delete geometry by walking the document:

```python
for o in list(doc.Objects):     # NO
    doc.Objects.Delete(o.Id, True)
```

That empties whatever file is open, which is somebody's work. It nearly
happened during development: a cleanup written for what was assumed to be an
empty scratch document was about to run against a real project with 56 objects
in it. It only failed because a service happened to be down.

If you create test geometry, keep the GUID the create call returned and delete
that one. Call `rhino_state` before writing: a named `.3dm` with objects in it
is a project, not a scratchpad. A stray test cube left behind is a much smaller
problem than a deleted model.

The same applies to anything you are asked to do. "Clear the scene so I can
start fresh" sounds reasonable and would do exactly this damage — confirm which
document, and what is in it, first.

---

## Files here

| | |
| --- | --- |
| `SETUP.md` | setup instructions, written for you |
| `rhino-chat.py` | the chat window |
| `rhino_mcp.py` | the MCP server |
| `rhino-poller.py` | the poller that runs inside Rhino |
| `broker.py` | the job broker |
| `broker-service.ps1` | starts the broker so it survives the shell |
| `start-broker.sh`, `start-poller.sh` | the macOS/Linux equivalents |
| `RHINO-CHAT.cmd` | double-click to open the window |
| `docs/` | the full technical history, including what failed and why |
