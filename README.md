# 3d-mcp

**Model in Autodesk Fusion by prompting.** An MCP server that lets Claude write Fusion API
Python, run it inside a live Fusion session, *look at the result through viewport screenshots*,
correct itself, and export print-ready files.

<p align="center">
  <img src="docs/images/enclosure-iso.png" alt="A 40x30x15 mm enclosure with 2 mm walls, filleted corners and four M3 screw bosses, modelled in Fusion by Claude" width="720">
</p>

<p align="center"><em>A 40 × 30 × 15 mm enclosure — 2 mm walls, filleted corners, four M3 bosses with
blind pilot bores — built by prompt, verified by measurement, exported to STL.</em></p>

The screenshot loop is the point. A fixed set of "create box / create hole" tools can never cover
the Fusion API, and a model writing CAD code blind gets things subtly wrong — an extrude in the
wrong direction, a profile that grabbed the wrong region — with no exception raised. Giving Claude
**arbitrary API access plus eyes** turns that into a design → look → correct loop.

---

## How it works

```
Claude Code  ←── stdio / MCP ──→  server/   (Python, FastMCP)
                                     │
                                     │  HTTP 127.0.0.1:7654 + token
                                     ▼
                                  addin/    (Fusion add-in, Python)
                                     │
                                     │  CustomEvent marshal → main thread
                                     ▼
                              Fusion API (adsk.core / adsk.fusion)
```

Fusion has no external API — `adsk.*` exists only inside Fusion, and it is **main-thread-only**.
So the add-in runs an HTTP listener on a background thread and marshals every request onto Fusion's
main thread through a registered custom event, waiting on a per-request event for the reply. One
job occupies the main thread at a time; a second concurrent request is refused immediately rather
than queued.

## Tools

| Tool | What it does |
| --- | --- |
| `fusion_execute` | Runs Python inside Fusion with `adsk`, `app`, `ui`, `design` injected. The namespace persists across calls. |
| `fusion_screenshot` | Viewport PNG (`front`, `top`, `right`, `iso`, `fit`) returned as a real image, not base64 text. |
| `fusion_export` | STL / STEP / 3MF / USD into `~/Documents/fusion-mcp-exports/`. |
| `fusion_state` | Document, units, design type, timeline count, parameters, top-level bodies and components. |

Four tools, deliberately. `fusion_execute` is the product; the others exist because they are
needed on every single session and shouldn't require bespoke code each time.

A failing script is a **normal result**, not a tool error — the traceback comes back verbatim so
Claude can read it and fix its own code.

## The knowledge layer

`skill/fusion-360/` is a Claude Code skill installed alongside the server, and it matters more than
the bridge code. Getting a model to understand *where things go in 3D space* is the hard part, not
executing the call.

Every API pattern in it was checked against Autodesk's reference and carries its doc URL. Where a
claim could not be verified, it says so and tells Claude to probe at runtime rather than asserting
a rule — **a plausible-but-wrong pattern is worse than a missing one**, because it actively
misleads mid-session.

It uses progressive disclosure: `SKILL.md` (~6 KB) is always in context, and
`references/{patterns,gotchas,spatial,printing,workflow}.md` load on demand.

A sample of what it documents, all verified against a running Fusion 2704:

- Internal length units are **centimetres** regardless of what the document displays, and angles
  are radians. 20 mm is `2.0`. This is the single most common error.
- `camera.viewOrientation` accepts a value on the camera copy, then is **silently reverted** when
  the camera is assigned back to the viewport. Named views must be set via `eye`/`upVector`.
- Querying faces in the *same* call that created a feature returns **stale topology, and the stale
  read succeeds** — a shell operation silently hollowed a sealed void instead of removing the top
  face, with no exception.
- Exporting to `part.usd` actually writes `part.usd.usdz`, so an existence check on the requested
  path reports a false failure for a successful export.

## Requirements

- macOS with Autodesk Fusion installed and launched at least once
- [`uv`](https://docs.astral.sh/uv/), `python3`, and the `claude` CLI on `PATH`

## Install

```sh
git clone https://github.com/<you>/3d-mcp.git
cd 3d-mcp
scripts/install.sh
```

> `install.sh` bakes the **absolute** path of this checkout into the MCP registration and
> symlinks the add-in from it. Moving or renaming the directory afterwards breaks both —
> re-run `scripts/install.sh` from the new location if you do.

The installer creates `~/.fusion-mcp/` (0700) with a random 64-hex-char token (0600), symlinks
`addin/FusionBridge` into Fusion's AddIns folder, links the knowledge skill into `~/.claude/skills/`,
builds the server venv with `uv sync`, and registers the MCP server with Claude Code at user scope
using absolute paths, so it works from any directory.

Re-running is safe — an existing token is preserved. `--rotate-token` replaces it; the add-in
re-reads the token per request, so Fusion does not need restarting.

### The one manual step

Fusion cannot enable an add-in from outside, so once, in Fusion:

> **Utilities → Add-Ins → select FusionBridge → Run**
> (older builds put this under **Tools**; `Shift+S` works either way)

It auto-starts on later launches — `runOnStartup` is set in the manifest.

> **If FusionBridge isn't in the list, restart Fusion.** It scans its add-ins folder only at
> launch, so an add-in installed while Fusion was running will not appear until you relaunch.

### Verify

```sh
curl -sS -H "X-Fusion-Bridge-Token: $(cat ~/.fusion-mcp/token)" http://127.0.0.1:7654/health
# {"ok": true, "app_version": "...", "bridge_version": "1", "document": "...", "busy": null}

claude mcp list   # from any directory — 'fusion' should be listed and connected
```

## Timeouts

Modelling operations take time, and the layers are deliberately staggered: the add-in waits **60 s**
on the main thread, the MCP server's HTTP client waits **75 s**. Claude Code's own tool timeout must
exceed both or it gives up while Fusion is still working. Set `MCP_TOOL_TIMEOUT` (milliseconds) to at
least `120000`, in your shell profile or in `~/.claude/settings.json`:

```json
{ "env": { "MCP_TOOL_TIMEOUT": "120000" } }
```

A 504 means the *wait* was abandoned, **not that the code was cancelled** — main-thread execution
cannot be interrupted. Don't resend; check `fusion_state` or a screenshot to see what actually
happened.

## Security

`fusion_execute` runs whatever Python Claude sends, inside Fusion, with your permissions. That is
the entire point, so the channel is gated tightly instead:

- **Loopback only.** Binds `127.0.0.1:7654`, never `0.0.0.0`. No remote or LAN access, ever.
- **Token on every request**, including `/health`, in the `X-Fusion-Bridge-Token` header, compared
  with `hmac.compare_digest` *before* the body is read. Being a non-safelisted header, a web page
  cannot reach the bridge: the browser must preflight, and the bridge answers no CORS.
- **Fail closed.** No readable token file → the listener does not start, and says why in `addin.log`.
- **Host pinning.** Requests must carry `Host: 127.0.0.1:7654` or `localhost:7654`, defeating DNS
  rebinding.
- **Bounded.** 5 MB request cap; 64 KB caps on stdout and result; screenshot dimensions bounded to
  64..1920 × 64..1440 and *rejected* outside that range, never silently clamped; at most 8
  concurrent connections; one execution at a time.
- `~/.fusion-mcp` is 0700 and its files 0600; the token is never logged.

Exports are confined to `~/Documents/fusion-mcp-exports/`. Nothing third-party executes inside
Fusion, and the tooling adds zero new network surface — the one deliberate exception is `uv sync`
at install time.

**Work in a scratch Fusion document while iterating.** Generated code can mangle a design, and the
timeline is the only safety net.

## Troubleshooting

Two logs, both in `~/.fusion-mcp/`, and they are the only window into failures — Fusion swallows
add-in exceptions silently.

- **`addin.log`** — the add-in: startup, bind errors, every request, full tracebacks.
- **`server.log`** — the MCP server (it can never log to stdout; that would corrupt JSON-RPC).

| Symptom | Likely cause |
| --- | --- |
| FusionBridge isn't in the Add-Ins list | Fusion scans that folder at launch only — **restart Fusion**. |
| `Fusion not running or FusionBridge add-in not enabled` | Fusion closed, or the add-in was never run — see the manual step. |
| Health check returns 401 | Token mismatch. Re-run `scripts/install.sh` (preserves the token) or `--rotate-token`. |
| Add-in never starts, `addin.log` says no token | `~/.fusion-mcp/token` missing or empty. The listener fails closed by design. |
| Add-in loaded but port bind failed | Something else holds 127.0.0.1:7654 — the reason is in `addin.log`. |
| `version mismatch` from a tool | You edited the add-in. Stop/Run it in Fusion, or reload it (below). |
| `fusion` missing from `claude mcp list` | Re-run `scripts/install.sh`; it removes and re-adds the registration. |
| Tool call times out at the Claude Code layer | `MCP_TOOL_TIMEOUT` too low — see above. |
| `no active Fusion design` | Open or create a document and switch to the Design workspace. |
| A burst of parallel requests gets 503 | The connection cap (8) refused the excess so a flood cannot exhaust threads inside Fusion. Send requests serially. |

### Reloading after an edit

```sh
curl -sS -X POST -H "X-Fusion-Bridge-Token: $(cat ~/.fusion-mcp/token)" \
     http://127.0.0.1:7654/reload
```

Reloads `fusion_bridge_impl.py` without restarting Fusion. A change to the manifest or to
`FusionBridge.py` still needs **Stop/Run**. Refused with 409 while an execution is in flight, 503
when the add-in is stopped, and 400 on a syntax error — with the running bridge left untouched.

## Uninstall

```sh
scripts/uninstall.sh            # removes the add-in symlink, skill link and MCP registration
scripts/uninstall.sh --purge    # also deletes ~/.fusion-mcp (token + logs)
```

Exports in `~/Documents/fusion-mcp-exports/` are never touched.

## Status

Working and verified end to end against **Fusion 2704.1.36** on macOS: parametric part built and
confirmed by measurement, STL validated, all failure paths (bad code, wrong token, bad host,
oversized requests, timeouts, reload edge cases) exercised. The add-in's concurrency, timeout and
reload behaviour is covered by an offline harness that stubs `adsk`.

Not yet done: a GPU render pipeline for photoreal product shots and turntables of exported models.

## Prior art

Built from scratch, but two projects informed the design and deserve credit:
[ndoo/fusion360-mcp-bridge](https://github.com/ndoo/fusion360-mcp-bridge) (closest in shape —
execute + screenshot, custom-event marshalling, token auth) and
[rahayesj/ClaudeFusion360MCP](https://github.com/rahayesj/ClaudeFusion360MCP), whose most useful
finding was that the *knowledge files* mattered more than the bridge code.

## License

MIT — see [LICENSE](LICENSE).
