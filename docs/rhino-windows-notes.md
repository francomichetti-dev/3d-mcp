# Rhino 8 on Windows — findings

Notes from a live spike against Rhino 8.32.26160.13001 on Windows 11 Pro
(10.0.26200), driven over SSH from macOS. Recorded because most of it was found
empirically and none of it would have been guessed.

Status: **the modelling loop is proven, the bridge architecture is not yet.**

---

## What is proven

A script running inside Rhino can build real parametric geometry and the result
can be inspected remotely — the same design → look → correct loop the Fusion
bridge runs.

Verified by building a car (body, cabin, four wheels) and filleting the body on
**12 edges**, then capturing the viewport and pulling the PNG back over SSH.

```
document : (unsaved)
units    : Centimeters
1 m      : 100.0 document units
  +  body / cabin / 4 wheels
  body filleted on 12 edges
```

## What is NOT yet proven

Whether a **persistent HTTP listener** survives inside Rhino, and whether
`RhinoApp.InvokeOnUiThread` returns a value to a background thread. Those are
what a bridge actually needs, and they are still open — see "the probe that
hangs" below.

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

**`ViewCapture.CaptureToBitmap` is not safe off the UI thread.** A `rhinocode`
script does not run on the UI thread, and calling the display pipeline from
there killed the run every time, at exactly that line. Use the command
pipeline, which marshals itself:

```python
rs.Command('_-ViewCaptureToFile "%s" _Width=1200 _Height=800 _Enter' % path, False)
```

**`rs.Command` returns `False` while succeeding.** The capture above returned
`False` and wrote a perfectly good PNG. Check the artefact, not the return
value.

**`_Width`/`_Height` are ignored by `_-ViewCaptureToFile`.** Asked for
1200×800, got 1116×323 — the viewport's aspect. Size the viewport, not the
capture.

**`rs.PurgeLayer` cannot purge the current layer.** Setting the layer current
and then purging it on a later run silently leaves the old objects behind, so
geometry accumulates across runs. Switch to Default first.

**The probe that hangs.** Waiting on `InvokeOnUiThread` while pumping
`RhinoApp.Wait()` in a loop hung at the same point three runs running, and took
Rhino down with it. If a `rhinocode` script runs *on* the UI thread, that loop
occupies the very thread the callback needs. Any test of marshaling must use
bounded waits and must not pump in a loop.

## Windows, over SSH

**An SSH session gets the SYSTEM PATH, not the user's.** Observed:
`C:\WINDOWS\system32\config\systemprofile\AppData\Local\Microsoft\WindowsApps`
in `$env:PATH`. Anything installed per-user — `uv`, `gh`, Claude Code — is
absent, and looks uninstalled when it is not. Read the user PATH explicitly:

```powershell
[Environment]::GetEnvironmentVariable("PATH","User")
```

This is the same class of bug as Fusion inheriting a minimal PATH on macOS, and
any Windows installer has to resolve tools by absolute path rather than by name.

**Claude Code may live inside Claude Desktop.** Found at
`%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code\<version>\claude.exe`
rather than on `PATH`. "`claude` is not on PATH" does not mean it is not
installed.

**Quoting through chat clients is a real hazard.** `powershell -File "C:\Program
Files\..."` arrived with an em-dash instead of a hyphen and with the quotes
stripped, twice. Ship a double-clickable `.cmd` instead of a command to paste —
and write it with CRLF line endings, since `cmd.exe` mis-parses LF-only batch
files.

## If the architecture does port

Roughly 80% of `fusion_bridge_impl.py` is CAD-agnostic — the HTTP listener,
token auth, Host pinning, single-flight guard, size caps, logging, reload. Only
the marshaling primitive and the injected namespace differ:

| Fusion | Rhino 8 |
| --- | --- |
| `registerCustomEvent` + `fireCustomEvent` | `RhinoApp.InvokeOnUiThread` |
| `adsk.core` / `adsk.fusion` | `Rhino` / `rhinoscriptsyntax` |
| centimetres, always | document units, variable |
| add-in with `run()`/`stop()` | plugin, or a script run once |

The 69 assertions in `tests/test_bridge.py` cover that shared portion, so they
protect the extraction.
