# mcp-rhino — working notes for the tester's Claude Code

Hi. Franco and I (Claude, on his machine) have been building a bridge that lets
an AI model in CAD by writing code inside the application. It works in Fusion
360. We are now making it work in **Rhino 8**, and your machine is where that is
being figured out.

This folder is the handover point. If you point your own Claude Code at this
directory, everything below is the context it needs.

---

## Sorry about the crashes

Rhino closing on you repeatedly today was **my fault**, not your machine's. I
was calling `RhinoApp.InvokeOnUiThread` from a script, which deadlocks, and
leaving background threads running after scripts finished, which crashed the
embedded Python — `ucrtbase.dll`, exception `0xc0000409`, three times in five
minutes in your Windows event log.

Both mistakes are now understood and neither is in the current design. Nothing
here starts a thread.

---

## What we are building

```
   your machine                                    Franco's machine
   ─────────────                                   ────────────────
   Rhino 8
     └── poller (a UI timer, no threads)
              │  "any work for me?"
              ▼
        broker  (small HTTP server on 127.0.0.1:7656)
              ▲
              │
        MCP server  ◀── the AI model asks for CAD operations
```

The important design decision, and why it looks like this:

**Rhino cannot be pushed into.** Fusion lets an add-in hand work to its main
thread. Rhino's equivalent (`InvokeOnUiThread`) is synchronous and deadlocks. So
instead of pushing work into Rhino, Rhino **pulls**: a timer inside Rhino asks
the broker for jobs, runs them, and posts results back. Because that timer
already runs on Rhino's UI thread, whatever it runs is on the correct thread
automatically. The problem disappears rather than being solved.

---

## What is proven to work (measured, not assumed)

| Thing | Result |
| --- | --- |
| Python inside Rhino | 3.9.10, CPython |
| Building geometry from a script | yes — a car, body filleted on 12 edges |
| Capturing the viewport | yes, via `_-ViewCaptureToFile` |
| `Eto.Forms.UITimer` | **fires reliably** — 148 ticks over 74s, Rhino stable |
| `RhinoApp.Idle` | **useless** — 0 ticks in 25s when nobody touches Rhino |
| `InvokeOnUiThread` | **deadlocks**, do not use |
| Background threads in a script | **crashes Rhino**, do not use |

---

## Rules — these each cost us hours

1. **Never call `InvokeOnUiThread`.** A `rhinocode` script already runs on the
   UI thread (`RhinoApp.InvokeRequired` is `False`), so it deadlocks against
   itself.
2. **Never start a thread.** A thread outliving the script context aborts the
   embedded CPython and takes Rhino with it.
3. **Never block in a script or a timer tick.** Both run on the UI thread; any
   wait freezes Rhino.
4. **`rhinocode script` gives you no stdout — not even tracebacks.** A failing
   script looks exactly like a silent one. Always write to a log file and catch
   your own exceptions.
5. **`rs.Command` returns `False` even when it succeeded.** Check the artefact,
   not the return value.
6. **`rs.PurgeLayer` cannot purge the *current* layer.** Switch to Default
   first, or geometry quietly piles up between runs.
7. **`ScriptEditor` must be run once per Rhino session** before `rhinocode` can
   see the instance. The first ever run builds the Python environment and takes
   about a minute — it is not frozen.

---

## Files here

| File | What it does |
| --- | --- |
| `scripts/rhino-poller.py` | the poller — start this inside Rhino |
| `scripts/rhino-poller-stop.py` | stops it |
| `broker.py` | the HTTP job broker (runs outside Rhino) |
| `docs/rhino-next-steps.md` | the full technical history |
| `docs/rhino-windows-notes.md` | Windows and Rhino gotchas |

## Running it

```powershell
# 1. broker (outside Rhino) - use Rhino's own Python, nothing to install
& "$env:USERPROFILE\.rhinocode\py39-rh8\python.exe" "$env:USERPROFILE\3d-mcp\run_broker.py"

# 2. in Rhino: type ScriptEditor once, then from another terminal:
& "C:\Program Files\Rhino 8\System\RhinoCode.exe" script "$env:USERPROFILE\3d-mcp\scripts\rhino-poller.py"

# 3. check it
curl -H "X-Fusion-Bridge-Token: <token>" http://127.0.0.1:7656/health
```

The token is a shared secret in `%USERPROFILE%\.fusion-mcp\token`. The broker
binds loopback only and refuses any request without it.

---

## Status: the chain WORKS end to end

Proven on your machine:

```
submit  -> broker -> poller (Rhino UI thread) -> result
```

* `state` returns the live document: units, tolerance, object count, layers.
* `execute` runs arbitrary Rhino Python. A sphere and a box were built this
  way, `before: 0 -> after: 2`, stdout captured, and they are still in the
  document.
* Rhino stayed up throughout - no crashes with this design.

The broker runs as a scheduled task (`scripts/broker-service.ps1`), because a
process launched over SSH dies when the session ends: Windows puts it in a job
object and kills the tree. The scheduled task is owned by the task scheduler
instead, and survives.

Viewport capture works too, via `screenshot`:

```json
{"kind": "screenshot", "payload": {"view": "perspective", "width": 1200, "height": 800}}
```

Views: `perspective`, `top`, `front`, `right`, `fit`. Returns the PNG as
base64, the same contract the Fusion bridge uses. Size is validated and
REJECTED if out of range rather than quietly clamped - a capture that silently
differs from what was asked for hides bugs.

**Why not `rs.Command('_-ViewCaptureToFile ...')`:** it re-enters Rhino's
command pipeline from inside a timer tick, never returns, and takes the broker
down with it. `Rhino.Display.ViewCapture` is a direct API call and works fine
from the same place - and unlike the command, it honours the requested size.
The command ignored `_Width`/`_Height` entirely and returned the viewport's
aspect (1116x323 when 1200x800 was asked for).

## What would genuinely help

`export` is the last Fusion feature Rhino lacks - STL/STEP/3DM out of the
document. Everything needed is in place; it is another handler beside the
three that already work.

After that: `rhino-poller.py` has handlers for `execute` and `state`. Adding
`screenshot` (via `_-ViewCaptureToFile`) and `export` would bring Rhino to
parity with what Fusion already does.

If you fix or improve anything, tell Franco — changes get pulled back and
committed to the repo with attribution.

Thanks for lending your machine. It is the only Rhino we have.
