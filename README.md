# fusion-mcp

An MCP bridge that lets Claude build 3D models in Autodesk Fusion by prompting: Claude writes
Fusion API Python, runs it inside Fusion, looks at the result through viewport screenshots,
corrects itself, and exports print-ready files. Everything is built from scratch in this repo
and runs entirely on this Mac — the MCP server talks to Claude Code over stdio, and to Fusion
over HTTP on loopback only, with a per-install token. No third-party code executes inside
Fusion, and the tooling adds zero new network surface.

```
Claude Code ←stdio/MCP→ server/ (Python, FastMCP)
                            ↓ HTTP 127.0.0.1:7654 + token
                        addin/ (Fusion 360 add-in, Python)
                            ↓ CustomEvent marshal → main thread
                        Fusion 360 API (adsk.core / adsk.fusion)
```

Fusion has no external API — `adsk.*` only exists inside Fusion, and its API is main-thread-only,
so the add-in's HTTP thread marshals every request onto the main thread via a custom event.

## Requirements

- macOS with Autodesk Fusion installed and launched at least once
- [`uv`](https://docs.astral.sh/uv/), `python3`, and the `claude` CLI on `PATH`

## Install

```sh
scripts/install.sh
```

It creates `~/.fusion-mcp/` (0700) with a random 64-hex-char token (0600), symlinks
`addin/FusionBridge` into Fusion's AddIns folder, creates `~/Documents/fusion-mcp-exports/`,
and registers the MCP server with Claude Code at user scope (absolute paths, so it works from
any directory). Re-running is safe: an existing token is preserved. Use `--rotate-token` to
replace it; the add-in re-reads the token per request, so Fusion does not need restarting.

### The one manual step

Fusion cannot enable an add-in from outside, so once, in Fusion:

> **Tools → Add-Ins → select FusionBridge → Run**
> (auto-starts on later launches — `runOnStartup` is in the manifest)

On recent Fusion builds this dialog lives under **Utilities → Add-Ins**.

## Verify

```sh
curl -sS -H "X-Fusion-Bridge-Token: $(cat ~/.fusion-mcp/token)" http://127.0.0.1:7654/health
# {"ok": true, "app_version": "...", "bridge_version": "1", "document": "...", "busy": null}

claude mcp list   # from any directory — 'fusion' should be listed and connected
```

## Tools

| Tool | What it does |
| --- | --- |
| `fusion_execute` | Runs Python inside Fusion with `adsk`, `app`, `ui`, `design` injected. |
| `fusion_screenshot` | Viewport PNG (`front`, `top`, `right`, `iso`, `fit`) returned as an image. |
| `fusion_export` | STL / STEP / 3MF / USD into `~/Documents/fusion-mcp-exports/`. |
| `fusion_state` | Document, units, timeline count, parameters, top-level bodies and components. |

Fusion's internal length unit is **centimeters** regardless of what the document displays —
20 mm is `2.0`. This and the rest of the contract live in the tool descriptions.

## Timeouts

Modeling operations can take a while, and the layers are deliberately staggered: the add-in
waits up to **60 s** on the main thread, the MCP server's HTTP client waits **75 s**. Claude
Code's own tool timeout must exceed both, or it will give up while Fusion is still working.
Set `MCP_TOOL_TIMEOUT` (milliseconds) to at least `120000` — in your shell profile, or in the
`env` block of `~/.claude/settings.json`:

```json
{ "env": { "MCP_TOOL_TIMEOUT": "120000" } }
```

A 504 from the bridge means the wait was abandoned, not that the code was cancelled — main-thread
execution cannot be interrupted. Do not resend; check `fusion_state` or a screenshot to see what
actually happened.

## Troubleshooting

Two logs, both in `~/.fusion-mcp/`, and they are the only window into failures — Fusion swallows
add-in exceptions silently:

- **`addin.log`** — the add-in: startup, bind errors, every request, full tracebacks.
- **`server.log`** — the MCP server (it can never log to stdout; that would corrupt JSON-RPC).

| Symptom | Likely cause |
| --- | --- |
| `Fusion not running or FusionBridge add-in not enabled` | Fusion closed, or the add-in was never run — see the manual step above. |
| Health check returns 401 | Token mismatch. Re-run `scripts/install.sh` (it preserves the token) or `--rotate-token`. |
| Add-in never starts, `addin.log` says no token | `~/.fusion-mcp/token` is missing or empty. The listener fails closed by design; run `scripts/install.sh`. |
| Add-in loaded but port bind failed | Something else holds 127.0.0.1:7654 — the reason is in `addin.log`. |
| `version mismatch` error from a tool | You edited the add-in. Stop/Run it in Fusion's Add-Ins dialog, or reload it — see below. |
| `fusion` missing from `claude mcp list` | Re-run `scripts/install.sh`; it removes and re-adds the registration. |
| Tool call times out at the Claude Code layer | `MCP_TOOL_TIMEOUT` is too low — see above. |
| `no active Fusion design` | Open or create a document and switch to the Design workspace. |
| A burst of parallel requests gets 503 | The add-in caps concurrent connections at 8 and refuses the rest, so a flood cannot exhaust threads inside Fusion. Send requests serially. |

Work in a scratch Fusion project while iterating — generated code can mangle a design, and the
timeline is the only safety net.

### Reloading after an edit

```sh
curl -sS -X POST -H "X-Fusion-Bridge-Token: $(cat ~/.fusion-mcp/token)" http://127.0.0.1:7654/reload
```

`/reload` is token-authenticated like every other endpoint — without the header it just returns 401.
It reloads the implementation module (`fusion_bridge_impl.py`) only: a change to the manifest or to
`FusionBridge.py` still needs **Utilities → Add-Ins → Stop/Run**. It is refused with 409 while an
execution is in flight, and 503 when the add-in is stopped. A syntax error in the edited file is
refused with 400 and the running bridge is left untouched.

## Uninstall

```sh
scripts/uninstall.sh            # removes the add-in symlink and the MCP registration
scripts/uninstall.sh --purge    # also deletes ~/.fusion-mcp (token + logs)
```

Exports in `~/Documents/fusion-mcp-exports/` are never touched.

## Security

`fusion_execute` runs whatever Python Claude sends, inside Fusion, with your permissions. That is
the entire point — a fixed tool surface cannot cover the Fusion API — so the channel is gated
tightly instead:

- **Loopback only.** The listener binds `127.0.0.1:7654`, never `0.0.0.0`. No remote or LAN access,
  ever, "for convenience" or otherwise.
- **Token on every request**, including `/health`, in the `X-Fusion-Bridge-Token` header, compared
  with `hmac.compare_digest` *before* the body is read. A non-safelisted header means a web page
  cannot reach the bridge: the browser would have to preflight, and the bridge answers no CORS.
- **Fail closed.** No readable token file → the listener does not start, and says why in `addin.log`.
- **Host pinning.** Requests must carry `Host: 127.0.0.1:7654` or `localhost:7654`, which defeats
  DNS rebinding.
- **Bounded.** 5 MB request cap, 64 KB caps on stdout and result, screenshot dimensions bounded to
  64..1920 × 64..1440 — rejected with an error outside that range, never silently clamped — at most
  8 concurrent connections, and one execution at a time (a second concurrent `/execute` gets a 409).
- `~/.fusion-mcp` is 0700 and its files 0600; the token is never logged.

Exports are confined to `~/Documents/fusion-mcp-exports/`; `fusion_execute` is the deliberate
escape hatch for anything else.
