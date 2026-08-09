# Follow-up

Known gaps, kept in the open rather than in somebody's head. Each entry says
why it is still open and what closing it takes. Remove entries as they close —
an entry that quietly stopped being true is worse than none.

## Untested: the agent actually launching its MCP server

The rename broke the spawn once — the agent invoked `arges-mcp` where the CLI
installs as `arges` — and no suite noticed. The model lost every Fusion tool
and reported "bridge disconnected" **while the bridge was answering /health**,
because nothing exercises the agent *spawning* the server: every suite stubs
one side of that join.

Closing it: a test that starts `agent_service` with PATH pointing at a stub
`arges` binary and asserts a session reaches ready — so the next time the
spawn target's name drifts, a test fails instead of a user.

## Live numbers for the combined execute+screenshot

`fusion_execute(screenshot=...)` and `rhino_execute(screenshot=...)` fold the
verify step into the modelling call — one model turn per step instead of two.
The semantics are pinned offline; the cadence gain is not yet measured against
a live CAD. Next session with Fusion open: run a real build and compare
step cadence with and without it.

## Verify the add-in registry after the 2026-08-08 crash fix

Fusion crashed during `terminate()` (see commit 30db799) and never rewrote
`JSLoadedScriptsinfo`, leaving `FusionBridge` registered at a path the rename
had deleted, with `runOnStartup` set. The stale entry was removed by hand
(backup: `JSLoadedScriptsinfo.backup-crash-fix-20260808`). On the next Fusion
launch confirm **Utilities → Add-Ins** lists exactly one entry, **Arges**, and
re-tick **Run on Startup** — the old flag went with the stale record.

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

The attachment-permissions fix and `scripts/cleanup-downloads.ps1` are ready
but the machine has been unreachable. Deploy both on next contact, and run the
cleanup script against the Downloads folder as agreed.

