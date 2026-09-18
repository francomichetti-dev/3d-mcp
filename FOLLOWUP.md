# Follow-up

Known gaps, kept in the open rather than in somebody's head. Each entry says
why it is still open and what closing it takes. Remove entries as they close —
an entry that quietly stopped being true is worse than none.

## Partly closed: the agent actually launching its MCP server

The rename broke the spawn once — the agent invoked `arges-mcp` where the CLI
installs as `arges` — and no suite noticed. The model lost every Fusion tool
and reported "bridge disconnected" **while the bridge was answering /health**.
Worse, the first fix covered only the agent: `install.sh` still *registered*
the dead name with Claude Code and both READMEs taught it, so every fresh
install recreated the failure.

**Closed:** the name-drift class. The package now installs both `arges` and
`arges-mcp` (the latter is what makes `uvx arges-mcp` work), and
`tests/test_consistency.py` holds every spawn site and doc to
`[project.scripts]` — reverting the alias fails four checks by file name.

**Still open:** the live join — nothing starts `agent_service` and asserts a
session actually reaches its tools. That needs the `claude` CLI in the test
environment; a stub `arges` on PATH plus a session-ready assertion would do.

## Fusion is read-only here: an expired subscription, and what it cost

Found while verifying autosave against a live document (`scooter-grip-kids`,
2026-09-18). The Fusion window title reads:

    scooter-grip-kids* - Autodesk Fusion (Expired Subscription - Read Only)

In that state `Document.save()` **returns True and saves nothing**. Verified,
all of it: no new version (`dataFile.versionNumber` stayed 7 across a real
parameter change plus two saves, and a fresh `findFileById` agreed), the
document stayed dirty, Fusion's own Save command stayed disabled, and
`app.data.activeProject` raises `InternalValidationError: id.size()` rather
than returning None — which an earlier draft of the save code assumed.

Consequences already handled:

- Saving does **not** go through Fusion's save. A save is an `f3d` archive
  written by the export manager, which keeps working in read-only mode
  (verified: 2.96 MB written from this very document).
- `isModified` is a real dirty flag, not a stuck one — it stays True because
  no save can ever succeed here. Do not "fix" it.
- Exports are unaffected: STL, STEP, 3MF and F3D all wrote correctly.

**Still open:** the healthy-subscription path has never run. Nobody has seen
this code on a Fusion that can save, so two things are unverified — whether
Fusion's own cloud save is worth attempting *alongside* the archive when it
would actually work, and whether `isModified` clears promptly enough after a
real save to be used as a skip condition. Revisit when the subscription is
renewed; until then the archive is the only save there is, and the `.f3d` in
the save folder is the only copy of any work done in a read-only session.

## The autosave costs about 1.5–2 s and ~3 MB per modelling step

Measured on `scooter-grip-kids` (two bodies, 2.96 MB archive): `fusion_save`
took 2.3 s, and a `fusion_execute` plus its autosave 1.9 s. The archive is the
whole design, so both numbers scale with the design, and the file is rewritten
after every successful call including ones that changed nothing.

That is the price of "it is always saved", and it was the explicit ask. If it
starts to bite in a long build, the fix is a change fingerprint computed inside
the same snippet — timeline count, body count, parameter expressions — compared
against the last one so an unchanged design skips the write without costing an
extra round trip. Not built: `isModified` would be the obvious signal and it
cannot be trusted here (see above), and guessing from the source is exactly
what this project argues against elsewhere.

## Live numbers for the combined execute+screenshot

`fusion_execute(screenshot=...)` and `rhino_execute(screenshot=...)` fold the
verify step into the modelling call — one model turn per step instead of two.
The semantics are pinned offline; the cadence gain is not yet measured against
a live CAD. Next session with Fusion open: run a real build and compare
step cadence with and without it.

## Known cosmetic: a symlinked checkout shows two Arges rows

Resolved 2026-08-09, live. Fusion discovers a symlinked add-in twice — the
folder scan resolves `AddIns/Arges` to the repo path while the saved
registration keeps the link path, and the two never merge. Consequences, all
verified: the Add-Ins dialog can show two rows, quit writes both paths into
`JSLoadedScriptsinfo`, and every launch calls `run()` twice — the second is
absorbed by the already-running guard and logged as expected. One listener,
one instance, no user-visible fault. Wheel installs (`arges install`) are real
copies, so both paths match and none of this applies.

Do not tidy the registry per quit — it regrows by construction. Revisit only
if Fusion ever stops merging the *listener* side too; the durable fix would be
registering the direct repo path instead of symlinking, which changes
install.sh and the documented checkout flow.

(The 2026-08-08 crash also left a dead `FusionBridge` registration with
`runOnStartup`; removed by hand, backup
`JSLoadedScriptsinfo.backup-crash-fix-20260808`.)

## The rename: landed, with a compatibility layer to remove later

All of it is in: the server package, the add-in folder, the agent package
(`arges-chat`), the `ARGES_*` environment variables, the
`X-Arges-Bridge-Token` header, and `~/.fusion-mcp` → `~/.arges`.

**The skill directory stayed `skill/fusion-360` deliberately.** It names the
CAD application it drives, not this product — the same way a Rhino skill would
be called `rhino`. Renaming it would have said something untrue.

**What is still open is the compatibility layer, and it is load-bearing until
every install has moved.** The pieces upgrade by different commands — the
add-in by `arges install`, the MCP server with the package, the Rhino half by
unzipping a new copy — so no upgrade order can be assumed. Therefore:

- every client sends **both** header names, every server accepts **either**
- every reader prefers `~/.arges` and falls back to `~/.fusion-mcp` when that
  is the only one present
- every `ARGES_*` variable falls back to its `FUSION_*` spelling
- exactly one thing migrates, `arges install`, and it refuses unless the two
  directories are siblings — without that check, redirecting `STATE_DIR` at a
  temp directory (which the token test does) would move the operator's live
  `~/.fusion-mcp` there and delete it on cleanup

`tests/test_consistency.py` holds all of this: dropping the fallback from one
file fails by file name. Remove the layer in one commit once no pre-rename
install can still be out there — after the Rhino machine confirms its update,
at the earliest. Until then it is not dead code.

## §2.3 of the rebrand handoff: the geometry-operation domain model

The handoff document describes a typed geometry-operation model as the core
abstraction. That contradicts the product's thesis — arbitrary API code plus
eyes, precisely because a fixed operation set can never cover a CAD API.
Parked rather than built. Needs a decision before anyone implements §2.3 as
written.

## The remote Rhino machine

The deploy vector is the repo zip: `scripts/rhino/UPDATE.md` is written for
the Claude Code on that machine and covers the whole update — including the
two items that were waiting on contact, re-applying real token permissions
(`icacls`; the old install's chmod restricted nothing on Windows) and the
agreed `cleanup-downloads.ps1` run, which it requires local consent for.
Open until the update is confirmed run.

