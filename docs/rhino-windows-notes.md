# Rhino 8 on Windows — findings

Notes from a live spike against Rhino 8.32.26160.13001 on Windows 11 Pro
(10.0.26200). Recorded because most of it was found empirically and none of it
would have been guessed.

Status: **both are now proven.** This file was written mid-spike, when only the
modelling loop worked. It has been corrected rather than deleted, because two
of its conclusions were later inverted and the reason why is the most useful
thing in it.

---

## What is proven

A script running inside Rhino can build real parametric geometry and the result
can be inspected remotely — the same design → look → correct loop the Fusion
bridge runs.

Verified by building a car (body, cabin, four wheels) and filleting the body on
**12 edges**, then capturing the viewport and inspecting the PNG.

```
document : (unsaved)
units    : Centimeters
1 m      : 100.0 document units
  +  body / cabin / 4 wheels
  body filleted on 12 edges
```

The full bridge is proven too, and someone who did not build it has modelled
real parts through it.

## How the open questions resolved

This section used to ask whether a **persistent HTTP listener** survives inside
Rhino and whether `RhinoApp.InvokeOnUiThread` returns a value to a background
thread. Both were answered, and both answers were no:

- **A listener inside Rhino does not survive.** Any thread that outlives the
  script context aborts the embedded CPython and takes Rhino down with it —
  `ucrtbase.dll`, `0xc0000409`. This happened repeatedly and is not
  recoverable.
- **`InvokeOnUiThread` cannot be used from a `rhinocode` script.** It is
  synchronous, and such a script *already runs on the UI thread*, so it
  deadlocks against itself. See "the probe that hangs" below — the hang was the
  answer, not a faulty probe.

So the bridge does not marshal at all. Rhino **pulls**: an `Eto.Forms.UITimer`
inside Rhino polls a broker for jobs. Because that timer already runs on the UI
thread, whatever it executes is on the right thread automatically — the problem
is removed rather than solved. `RhinoApp.Idle` was tried first and fires **zero**
times while Rhino is unfocused; the UITimer managed 148 ticks in 74 seconds.

---

## Environment

| | |
| --- | --- |
| Python | 3.9.10 (CPython, real Python 3) |
| RhinoCommon | imports normally |
| `rhinoscriptsyntax` | available |
| Document units | **user-configurable** — was Centimeters here |
| `RhinoApp.InvokeOnUiThread` | exists |

**Units are not fixed.** Fusion is always centimetres; Rhino is whatever the
document says. Size geometry relative to the document:

```python
metre = Rhino.RhinoMath.UnitScale(Rhino.UnitSystem.Meters, doc.ModelUnitSystem)
```

## Driving Rhino remotely

`RhinoCode.exe` ships beside `Rhino.exe` and runs scripts in a **live** Rhino:

```
"C:\Program Files\Rhino 8\System\RhinoCode.exe" list
"C:\Program Files\Rhino 8\System\RhinoCode.exe" script C:\path\to\script.py
```

Three things that cost time:

1. **The scripting component is not loaded until a script command runs in the
   GUI.** A freshly launched Rhino is invisible to `rhinocode list` — it reports
   *"no running instance of Rhino detected"* — until somebody types
   `ScriptEditor` in Rhino once. This is per Rhino session, not once per machine.
2. **First `ScriptEditor` run builds the Python environment** under
   `%USERPROFILE%\.rhinocode` (`py39-rh8`, `py27-rh8`). Takes about a minute and
   looks like a hang.
3. **stdout does not come back.** `rhinocode script` runs the script inside
   Rhino, so `print()` goes to Rhino's console and the caller sees nothing —
   including tracebacks, so a failing script looks like silence. Write results
   to a file and read that; wrap everything in `try/except` and log the
   traceback yourself.

## Sharp edges found by hitting them

**Viewport capture: the answer depends on where you are calling from, and it
inverts.** This cost the most time of anything here, because both halves are
true and each one looks like a general rule.

| Calling from | `Rhino.Display.ViewCapture` | `rs.Command('_-ViewCaptureToFile')` |
| --- | --- | --- |
| a bare `rhinocode` script | kills the run at that line | works, marshals itself |
| the poller's `Eto` UITimer | **correct — this is what ships** | never returns |

The original finding — that `CaptureToBitmap` is unsafe and `rs.Command` is the
way — was recorded here as a rule. It was a rule about *one context*. Once
capture moved into the UITimer it reversed completely: the timer runs on the UI
thread, so the direct API call is fine, while `rs.Command` re-enters Rhino's
command pipeline from a message dispatch and hangs forever.

`scripts/rhino/rhino-poller.py` therefore uses:

```python
capture = Rhino.Display.ViewCapture()
bitmap = capture.CaptureToBitmap(view)
```

The transferable lesson is not about either call. It is that on this platform
"is X safe?" has no answer without "on which thread?" — and a `rhinocode`
script and a UITimer callback are not the same thread.

**`rs.Command` returns `False` while succeeding.** The capture above returned
`False` and wrote a perfectly good PNG. Check the artefact, not the return
value. (Still true, and still worth knowing wherever `rs.Command` is used.)

**`_Width`/`_Height` are ignored by `_-ViewCaptureToFile`.** Asked for
1200×800, got 1116×323 — the viewport's aspect. Size the viewport, not the
capture.

**`rs.PurgeLayer` cannot purge the current layer.** Setting the layer current
and then purging it on a later run silently leaves the old objects behind, so
geometry accumulates across runs. Switch to Default first.

**The probe that hangs.** Waiting on `InvokeOnUiThread` while pumping
`RhinoApp.Wait()` in a loop hung at the same point three runs running, and took
Rhino down with it. The hypothesis at the time — that a `rhinocode` script runs
*on* the UI thread, so the loop occupies the very thread the callback needs —
is now confirmed. The probe was not faulty; the hang was the finding, and it is
why nothing in the shipped design marshals.

## Windows

**A non-interactive session gets the SYSTEM PATH, not the user's.** This is
what a scheduled task or any service-style launch sees. Observed:
`C:\WINDOWS\system32\config\systemprofile\AppData\Local\Microsoft\WindowsApps`
in `$env:PATH`. Anything installed per-user — `uv`, `gh`, Claude Code — is
absent, and looks uninstalled when it is not. It matters here because the
broker runs as a scheduled task. Read the user PATH explicitly:

```powershell
[Environment]::GetEnvironmentVariable("PATH","User")
```

This is the same class of bug as Fusion inheriting a minimal PATH on macOS, and
any Windows installer has to resolve tools by absolute path rather than by name.

**Claude Code may live inside Claude Desktop.** Found at
`%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code\<version>\claude.exe`
rather than on `PATH`. "`claude` is not on PATH" does not mean it is not
installed.

**A command someone has to copy is a command that arrives mangled.**
`powershell -File "C:\Program Files\..."` turned up with an em-dash instead of
a hyphen and with the quotes stripped, twice, in transit. Ship a
double-clickable `.cmd` rather than a line to paste — and write it with CRLF
line endings, since `cmd.exe` mis-parses LF-only batch files.

## How the architecture actually ported

The expectation here was that most of `arges_impl.py` would be reused
with only the marshaling primitive and the namespace swapped. That was wrong in
one important place — the row this table originally got backwards is marked:

| | Fusion | Rhino 8 |
| --- | --- | --- |
| direction | the add-in is **pushed** into | Rhino **pulls** ⟵ *not as predicted* |
| marshaling | `registerCustomEvent` + `fireCustomEvent` | none — the timer is already on the UI thread |
| lives inside the CAD | HTTP listener | `Eto.Forms.UITimer` only |
| namespace | `adsk.core` / `adsk.fusion` | `Rhino` / `rhinoscriptsyntax` |
| units | centimetres, always | document units, variable |
| entry point | add-in with `run()`/`stop()` | a script run once per session |

The listener, token auth, Host pinning, single-flight guard, size caps and
logging did all carry over — but into `broker.py`, which runs *outside* Rhino,
not into anything living inside it. That relocation is the whole port: the
CAD-agnostic 80% was real, it just could not stay in the same process.

The 69 assertions in `tests/test_bridge.py` cover the Fusion side of that shared
portion and `tests/test_broker.py` covers the Rhino side, so both are protected.
