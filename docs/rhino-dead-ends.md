# Rhino bridge — the blocker, and how it was solved

**Solved.** This was written as a plan of attack while the Rhino bridge was
blocked. The plan it proposed did not work, and the two designs sketched in
earlier versions of this file would both crash Rhino if implemented today. It is
kept as a record of the dead ends, because every one of them is a thing a
reasonable person would try first.

What shipped is `scripts/rhino/rhino-poller.py`. The whole answer is four lines
of its header comment:

> A `rhinocode` script runs **on** Rhino's UI thread. A background thread that
> outlives the script context aborts the embedded CPython and takes Rhino down.
> `RhinoApp.Idle` fires zero times when nobody is touching Rhino.
> `Eto.Forms.UITimer` fires reliably and runs on the UI thread.

So: one Eto UITimer, no threads, no listener, no marshaling. Whatever the timer
runs is already on the correct thread, which removes the problem instead of
solving it.

---

## The dead ends, in the order they were hit

Each of these was tried on real hardware (Rhino 8.32.26160, Windows 11) and each
one cost hours. They are listed because the reasoning that led to each is sound
— they are not careless mistakes, they are the obvious moves.

**1. Marshal onto the UI thread, like Fusion does.** Fusion's bridge hands work
to the main thread with `registerCustomEvent` / `fireCustomEvent`, and
`RhinoApp.InvokeOnUiThread` looks like the equivalent. It is not: it behaves
like `Control.Invoke` (synchronous), not `BeginInvoke`. Called from a
`rhinocode` script it deadlocks instantly, because that script is *already on
the UI thread* — `RhinoApp.InvokeRequired` is `False`. This hung five separate
probes. Called from a background thread while Rhino sat idle, the callback
simply never ran.

**2. Run an HTTP listener inside Rhino.** An earlier version of this document
recorded, under "what already works", that a background listener survives inside
Rhino — `/ping` answered long after the starting script returned. That
observation was real and the conclusion was wrong. It survives right up until
RhinoCode tears down the script context with the thread still running, at which
point the embedded CPython aborts: `ucrtbase.dll`, exception `0xc0000409`
(`__fastfail`), the same fault offset every time, three crashes in five minutes.
**Nothing inside Rhino may start a thread.**

**3. Drive it from `RhinoApp.Idle`.** With threads and marshaling both ruled
out, Rhino's own idle event looks perfect: it fires on the UI thread, so a
handler could collect queued work without anyone marshaling anything. Two
designs in this file were built on it. It fires **zero** times when Rhino is
unfocused and nobody is interacting with it — measured by attaching a handler
that appended a timestamp to a file and then leaving Rhino alone. An event that
only fires when someone is already at the keyboard cannot drive a remote bridge.

**4. `Eto.Forms.UITimer`** — this one worked. 148 ticks over 74 seconds with
Rhino stable and unfocused, on the UI thread. It is what ships.

The shape of the answer is worth noting: three of the four attempts tried to
move work *onto* the UI thread. The one that worked started there and never
left.

## Rules that cost us time

- **Never call `InvokeOnUiThread` from a `rhinocode` script.** It is already the
  UI thread. Instant deadlock.
- **Never start a thread inside Rhino.** It aborts the CRT when the script
  context is torn down, and takes Rhino with it.
- **Never wait on the UI thread for background work.** Same deadlock, different
  route.
- **`rhinocode script` returns no stdout** — not even tracebacks. A failing
  script looks identical to a silent one. Log to a file and catch your own
  exceptions, or you will be debugging blind.
- **Viewport capture depends on which thread you are on, and the answer
  inverts.** From the poller's UITimer, use `Rhino.Display.ViewCapture` —
  `rs.Command('_-ViewCaptureToFile ...')` re-enters Rhino's command pipeline
  from a message dispatch and never returns. From a bare `rhinocode` script it
  is the other way round. `docs/rhino-windows-notes.md` has the table.
- **`rs.Command` returns `False` while succeeding.** Check the artefact.
- **`rs.PurgeLayer` cannot purge the *current* layer.** Switch to Default first
  or geometry silently accumulates across runs.
- **The scripting component loads only after `ScriptEditor` runs in the GUI**,
  once per Rhino session, and the first run builds the Python environment
  (about a minute — it looks like a hang).
- **State outlives a script run via `scriptcontext.sticky`.** Attaching to the
  `Rhino` module does not work: it is a .NET namespace and rejects `setattr`
  with *"type does not support setting attributes"*.

## The probe scripts

Deleted once they had produced their findings — every result they measured is
recorded above, and they remain in the git history if the raw code is ever
wanted. Two of them are unsafe to run as written, which is the other reason they
are not lying around. What ships is in `scripts/rhino/`.

---

## Never write a blanket-delete cleanup

Test geometry gets created during development, and the obvious cleanup is:

```python
for o in list(doc.Objects):
    doc.Objects.Delete(o.Id, True)      # NEVER DO THIS
```

That deletes the person's model, not your test cube. It nearly did: a cleanup
written for what was believed to be an empty scratch document ran after Rhino
had restarted into a real project — 56 objects of someone's actual work. It only
failed because the broker happened to be down at that moment.

The document you tested in is not the document you clean up in. Rhino restarts,
people open their own files, and an unsaved scratch document is
indistinguishable from a project by object count alone.

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
