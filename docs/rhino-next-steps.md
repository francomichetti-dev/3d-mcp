# Rhino bridge — the blocker, and how to attack it

> **Superseded in part.** Rhino was not merely deadlocking, it was CRASHING:
> `ucrtbase.dll`, exception `0xc0000409` (`__fastfail`, a C-runtime abort), the
> same fault offset every time. The likely cause is a background daemon thread
> outliving the `rhinocode` script context — when RhinoCode tears the context
> down with a thread still running, the embedded CPython aborts and takes Rhino
> with it. So `rhino-bridge-start.py`, which deliberately leaves a listener
> thread running after the script returns, is UNSAFE. Any in-Rhino code must
> therefore run entirely on the UI thread via `RhinoApp.Idle`, with no threads
> of its own. See "Revised design" at the end.

Everything below was established on real hardware (Rhino 8.32.26160, Windows 11)
during a live spike. Read this before writing any Rhino code; it will save you
the five dead ends it cost us.

## What already works

- **A background HTTP listener survives inside Rhino.** Start it from a script,
  let the script return, and it keeps answering. Proven: `/ping` responded long
  after the starting script had finished.
- **Real geometry can be built from Python inside Rhino** — a car with a body
  filleted on 12 edges, then captured and inspected remotely.
- **State can outlive a script run** via `scriptcontext.sticky`. Attaching to
  the `Rhino` module does not work: it is a .NET namespace and rejects `setattr`
  with *"type does not support setting attributes"*.

## The blocker

**`RhinoApp.InvokeOnUiThread` blocks the caller.** It behaves like
`Control.Invoke` (synchronous), not `BeginInvoke` (fire-and-forget).

Consequences, both observed:

1. Called **from the UI thread**, it deadlocks instantly. A `rhinocode` script
   runs *on* the UI thread — `RhinoApp.InvokeRequired` is `False` — so a script
   that calls it never returns. This is what hung five separate probes, and it
   was never Rhino misbehaving.
2. Called **from a background thread** while Rhino sat idle, the callback still
   never ran. `/state` and `/make` on the mini-bridge both timed out, and no
   callback marker was ever written.

So the marshaling primitive the Fusion bridge relies on has no direct
equivalent, and that is the entire remaining problem.

## The most promising fix: an Idle-driven queue

Rhino raises `RhinoApp.Idle` from its own message loop, so a handler attached to
it runs **on the UI thread with nobody having to marshal anything**. That turns
the problem inside out: instead of pushing work onto the UI thread, leave work
in a queue and let the UI thread collect it.

Sketch:

```python
import queue, threading
import Rhino

_jobs = queue.Queue()          # (callable, Event, result-box)

def _on_idle(sender, args):
    # runs ON the UI thread, courtesy of Rhino's own loop
    while True:
        try:
            fn, done, box = _jobs.get_nowait()
        except queue.Empty:
            return
        try:
            box["value"] = fn()
        except BaseException as exc:
            box["error"] = repr(exc)
        finally:
            done.set()

Rhino.RhinoApp.Idle += _on_idle          # attach once, at startup

def marshal(fn, timeout=60.0):
    """Call from a LISTENER thread. Never from the UI thread."""
    done, box = threading.Event(), {}
    _jobs.put((fn, done, box))
    box["serviced"] = done.wait(timeout)
    return box
```

This mirrors what FusionBridge does with `registerCustomEvent` /
`fireCustomEvent`: the HTTP thread hands work over and waits on a per-request
event while the application's own loop performs it.

**What to verify, in this order:**

1. Does `Idle` fire at all when Rhino is idle and unfocused? If it only fires on
   user interaction, this approach needs a nudge — `RhinoApp.Wait()` from the
   listener thread, or a timer.
2. Does the handler actually run on the UI thread? Compare
   `threading.get_ident()` inside it against a known UI-thread id.
3. Can it create geometry and return a value to the waiting listener thread?
4. Does it stay attached across documents opening and closing?

If `Idle` does not fire reliably, the fallback is a **real Rhino plugin** loaded
at startup rather than a script-hosted listener, which gets proper lifecycle
hooks.

## Rules that cost us time

- **Never call `InvokeOnUiThread` from a `rhinocode` script.** It is the UI
  thread. Instant deadlock.
- **Never wait on the UI thread for background work.** Same reason.
- **`rhinocode script` returns no stdout** — not even tracebacks. A failing
  script looks identical to silence. Log to a file and catch your own
  exceptions, or you will be debugging blind.
- **`ViewCapture.CaptureToBitmap` is not safe off the UI thread.** Use
  `rs.Command('_-ViewCaptureToFile ...')`, which marshals itself.
- **`rs.Command` returns `False` while succeeding.** Check the artefact.
- **`rs.PurgeLayer` cannot purge the *current* layer.** Switch to Default first
  or geometry silently accumulates.
- **The scripting component loads only after `ScriptEditor` runs in the GUI**,
  once per Rhino session, and the first run builds the Python environment
  (about a minute — it looks like a hang).

## Files here

| | |
| --- | --- |
| `rhino-bridge-start.py` | starts the listener and returns — the shape that works |
| `rhino-marshal-diagnose.py` | fires callbacks without waiting; proves what runs |
| `rhino-car-test.py` | builds geometry and captures the viewport |
| `rhino-probe2.py` | bounded environment checks |

Run them with:

```
"C:\Program Files\Rhino 8\System\RhinoCode.exe" script C:\path\to\script.py
```

Each writes a `*-output.txt` beside itself. Read that, not the console.


---

## Revised design: no threads inside Rhino at all

The crash evidence rules out the listener-thread shape. What is left is the
narrowest possible footprint inside Rhino:

* **No HTTP server** in Rhino — it is a client, not a server.
* **No background threads** — nothing survives the script but an `Idle`
  subscription, which is Rhino's own event, not our thread.
* **No `InvokeOnUiThread`** — the `Idle` handler already runs on the UI thread.

```python
import Rhino, urllib.request, json, time

_last = {"t": 0.0}
BROKER = "http://127.0.0.1:7656"

def _on_idle(sender, args):
    # Idle fires constantly, so rate-limit or Rhino feels sluggish.
    now = time.time()
    if now - _last["t"] < 0.25:
        return
    _last["t"] = now
    try:
        # wait=0: must NOT block, this is the UI thread
        with urllib.request.urlopen(BROKER + "/claim?wait=0", timeout=1) as r:
            job = json.loads(r.read())
    except Exception:
        return                      # broker down: stay quiet, try again later
    if not job:
        return
    result = run(job)               # already on the UI thread - nothing to marshal
    post(BROKER + "/result", {"id": job["id"], "result": result})

Rhino.RhinoApp.Idle += _on_idle
```

The trade: polling latency of one `Idle` tick plus the rate limit, against a
CAD-side footprint with nothing in it that can crash the host. Given a modelling
operation takes seconds, a quarter-second of latency is not worth a single
crash.

**Two things still to verify, in this order, and both are cheap:**

1. Does `RhinoApp.Idle` fire while Rhino is unfocused and idle? Attach a handler
   that only appends a timestamp to a file, then look at the file. No threads,
   no sockets, nothing that can crash anything.
2. Does an `Idle` subscription survive without crashing, given the CRT abort
   above? Same test answers it — if Rhino is alive a minute later, yes.

If `Idle` does not fire reliably, the answer is a compiled Rhino plugin with a
real timer, which is where a production version belongs anyway.

---

## Never write a blanket-delete cleanup

Test geometry gets created during development, and the obvious cleanup is:

```python
for o in list(doc.Objects):
    doc.Objects.Delete(o.Id, True)      # NEVER DO THIS
```

That deletes the person's model, not your test cube. It nearly did: a cleanup
written for what was believed to be an empty scratch document ran after Rhino
had restarted into a real project file — 56 objects of someone's real
work. It only failed because the broker happened to be down at that moment.

The document you tested in is not the document you clean up in. Rhino restarts,
people open their own files, and an unsaved scratch document is indistinguishable
from a project by object count alone.

Two rules:

1. **Delete by identity, never by enumeration.** Keep the GUID that
   `AddBrep`/`AddSphere` returned and delete that. If a GUID has been lost, the
   geometry stays — a stray test cube is a far smaller problem than a deleted
   model.
2. **Read the document before writing to it.** `rhino_state` returns the name
   and object count. A named `.3dm` with objects in it is somebody's work;
   stop.

This applies to anything generated too. `fusion_execute` and `rhino_execute`
hand the model arbitrary code, and "clear the scene so I can start fresh" is a
plausible-sounding instruction that would do the same damage.
