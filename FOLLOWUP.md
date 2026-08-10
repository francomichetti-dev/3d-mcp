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

## The rename is half-landed (branch `rename/arges`)

Done: the server package (`server/src/arges_mcp`), the add-in folder
(`addin/Arges`, manifest id preserved), spawn sites pointing at `arges`.
Not yet: the agent/chat package name, the `FUSION_*` environment variables,
the `X-Fusion-Bridge-Token` header name, the skill directory, CI references,
and the `~/.fusion-mcp` → `~/.arges` state migration (migrate-on-startup, its
own commit). The half-done state is deliberate — each rename lands with its
tests — but do not cut a release from this branch until the set is complete.

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

