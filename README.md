# 3d-mcp

[![tests](https://github.com/francomichetti-dev/3d-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/francomichetti-dev/3d-mcp/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![macOS](https://img.shields.io/badge/macOS-Fusion%202704%2B-lightgrey.svg)](#requirements)

**Model in CAD by prompting.** MCP servers that let Claude write CAD API Python, run it inside
a live **Autodesk Fusion** or **Rhino 8** session, *look at the result through viewport
screenshots*, correct itself, and export print-ready files.

<p align="center">
  <img src="docs/images/enclosure-iso.png" alt="A 40x30x15 mm enclosure with 2 mm walls, filleted corners and four M3 screw bosses, modelled in Fusion by Claude" width="720">
</p>

<p align="center"><em>A 40 × 30 × 15 mm enclosure — 2 mm walls, filleted corners, four M3 bosses with
blind pilot bores — built by prompt, verified by measurement, exported to STL.</em></p>

The screenshot loop is the point. A fixed set of "create box / create hole" tools can never cover
the Fusion API, and a model writing CAD code blind gets things subtly wrong — an extrude in the
wrong direction, a profile that grabbed the wrong region — with no exception raised. Giving Claude
**arbitrary API access plus eyes** turns that into a design → look → correct loop.

> [!WARNING]
> That last sentence is literal. `fusion_execute` runs **arbitrary Python inside your Fusion
> session** with your privileges — not a sandbox, and not trying to be one. The only boundary is
> that the listener is bound to `127.0.0.1` and requires a token generated at install. **Never
> expose it to a network.** Generated code can also mangle an open design, so work in a scratch
> Fusion project while you get a feel for it. Read [SECURITY.md](SECURITY.md) before installing.

---

## How it works

```
Claude Code ──┐
              ├── stdio / MCP ──→  server/   (Python, FastMCP)
chat panel ───┘                       │
 (agent/)                             │  HTTP 127.0.0.1:7654 + token
                                      ▼
                              FusionBridge   (Fusion add-in, Python)
                                      │
                                      │  CustomEvent marshal → main thread
                                      ▼
                               Fusion API (adsk.core / adsk.fusion)
```

The add-in lives at `server/src/fusion_mcp/addin/FusionBridge/` — inside the package rather than at
the repo root, so a PyPI install ships it and `fusion-3d-mcp install` can put it where Fusion looks.
A checkout symlinks it from there instead, so edits are live.

Two front ends, one bridge: a Claude Code session, or the [chat panel](#the-chat-panel) docked
inside Fusion. Both speak MCP to the same server.

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

## The chat panel

The MCP tools assume you are already in a Claude Code session. The panel removes that assumption:
it docks a chat inside Fusion, so you describe what you want in the window where the model lives.

Open it either way — both reach the same service, and both are idempotent:

- **In Fusion:** the **Fusion Chat** button in **UTILITIES → ADD-INS**
- **In any Claude Code session, in any project:** `/fusion-chat`

Whichever you use, it starts the agent service if it isn't running and opens (or focuses) the
docked palette. `/fusion-chat --status` reports what is up; `/fusion-chat --stop` closes the panel
and stops the service.

```
palette (webview, docked in Fusion)
   │  HTTP 127.0.0.1:7655
   ▼
agent/   ── Claude Agent SDK ──→  MCP (stdio) ──→ server/ ──→ bridge ──→ Fusion
```

The palette talks to the agent service **directly** rather than through the add-in. That is not an
implementation detail: the add-in's main thread is what serves bridge calls, so routing chat through
it would deadlock on the agent's first Fusion tool call.

### Attaching images

Reference photos, sketches on paper, a screenshot of a part you want copied — three ways to get one
in, because a webview embedded in Fusion may not be permitted to open a native file picker:

- the **＋** button next to the input
- **drag and drop** anywhere onto the panel
- **paste** from the clipboard

Images go into the conversation as real image content, not as a file the model has to open — no tool
call, and because they land in the session itself, a resumed conversation can still see them. Up to
8 per message.

The panel downscales before uploading (longest edge 1568 px, Anthropic's efficient maximum), so a
multi-megapixel phone photo does not cost tokens for detail the model cannot use. The service keeps
the bytes on disk under `~/.fusion-mcp/attachments/` (0700, files 0600) and stores only a reference
in the transcript, so `chats.json` stays small and the panel can redraw a conversation later.
Attachments are deleted when their design's chat is compressed or pruned.

### One chat per design

Every design gets its own conversation, with its own memory. Switch tabs in Fusion and the panel
switches with you — three designs open means three separate chats, and nothing one knows leaks into
another. The header names the design you are talking to.

Identity is the hard part, because `document.name` cannot do it: **every unsaved document reports
the name `Untitled`**. The `(1)`/`(3)` in Fusion's tab strip is decoration for display and never
reaches the API, so two untitled designs are indistinguishable by name. Instead:

| Document | Key | Cost |
| --- | --- | --- |
| Saved | its `dataFile.id` URN | free — chatting never marks a saved design modified |
| Unsaved | a UUID stamped into `design.attributes` | marks it modified, on first message only |

The attribute is checked **first**, so an unsaved design that you later save keeps its conversation
instead of being renamed into a second identity by the `dataFile` that just appeared.

Switching is event-driven: the add-in's `documentActivated` handler keeps a cached key that
`GET /document` answers from the HTTP thread, so following your tabs never occupies Fusion's main
thread or queues behind a long modelling job.

**Switching mid-turn stops the turn.** `fusion_execute` always acts on whatever document is active,
so a turn that outlived a tab switch would start editing the design you just moved to. The panel
says so, and the bridge independently refuses any pinned turn whose design is no longer active —
which closes the gap where a tool call is already in flight.

### Memory, and what happens when a design closes

Conversations survive restarts. `~/.fusion-mcp/chats.json` (0600) stores a pointer to the SDK's own
session plus what the panel needs to redraw; reopening resumes the real context, so the model still
remembers what you were doing.

**Closing a design compresses its chat.** The conversation is replayed once — forked, with no Fusion
tools attached, since the design is gone — and reduced to core context: intent, key dimensions and
parameters, meaningful entity names, decisions and rejected approaches, and anything left unfinished.
Tool mechanics, code, retries and dead ends are dropped. The full session is then discarded, so a
design worked on for months does not carry months of transcript. Reopening it seeds a fresh
conversation with that summary, marked as something to verify rather than trust.

The file is bounded at 50 designs, evicted least-recently-touched.

Modelling runs automatically — being asked to confirm every extrude defeats the point, and modelling
is subtractive, so cuts, combines and deletes are ordinary work. The line is drawn at
**recoverability**, not at how destructive an operation sounds:

| Operation | Recoverable via | Asks? |
| --- | --- | --- |
| `deleteMe()`, `.remove()`, `removeAll()` | timeline / undo | no |
| `deleteAllAfterMarker`, `markerPosition =` | timeline | no |
| `designType =` | undo | no |
| `combineFeatures`, Cut / Intersect | timeline | no |
| `save()`, `saveAs()` | **nothing** — overwrites the saved file | **yes** |
| any `.close(` | **nothing** — discards everything unsaved | **yes** |

Restoring the prompt for deletes is a matter of moving the patterns back into `DESTRUCTIVE_PATTERNS`
in `agent/agent_service.py`; they are kept next to it, commented, for exactly that.

The gate is a **PreToolUse hook**, not a permission callback. A permission callback only fires when
the flow resolves to a prompt, so under `defaultMode: auto` it never ran — a body was deleted
without asking during testing. The hook runs unconditionally, which is what makes the two rows
above actually hold.

> Work in a scratch Fusion project while iterating. With deletes ungated, the timeline is the safety
> net, and it only covers what happened since the document was opened.

> Still worth working in a scratch Fusion project while iterating. Generated code can mangle a
> design in ways no pattern list anticipates.

## Rhino 8

Rhino is supported too, with the same four ideas — arbitrary API access, eyes on the viewport, a
chat window, and no API key. What differs is *how the code reaches the CAD*, and the reason is
worth knowing before reading the code.

| | Fusion | Rhino |
| --- | --- | --- |
| Reaching the CAD | an add-in **pushes** work onto the main thread | a timer inside Rhino **pulls** work |
| Mechanism | `registerCustomEvent` / `fireCustomEvent` | `Eto.Forms.UITimer` polling a broker |
| Interface | MCP server + docked palette | MCP server + a separate window |
| Engine | Claude Agent SDK | the `claude` CLI |

**Why Rhino pulls.** Fusion lets an add-in hand work to its main thread. Rhino's equivalent,
`InvokeOnUiThread`, is *synchronous* — and a `rhinocode` script already runs on the UI thread, so
calling it deadlocks against itself. Running a listener on a background thread instead crashes
Rhino outright: a thread outliving the script context aborts the embedded CPython
(`ucrtbase.dll`, `0xc0000409`).

So Rhino pulls. A timer inside Rhino asks a loopback broker for jobs, runs them, and posts the
results back. Because that timer is already on the UI thread, whatever it runs is on the correct
thread by construction — the marshaling problem disappears rather than being solved.

```
claude ──MCP──▶ rhino_mcp.py ──HTTP──▶ broker (127.0.0.1:7656) ──▶ poller inside Rhino
```

`rhino_mcp.py` has **no dependencies**. FastMCP needs Python 3.10+ and Rhino ships 3.9, so a
framework would mean installing a second Python to forward four JSON messages. Nothing to install
also means setup is one line.

### Setting it up

The Rhino side runs on the Claude subscription you already have — there is no API key. After
installing Claude Code, ask it to do the rest:

```
Set up the Rhino bridge in "<path>/scripts/rhino" — read SETUP.md there and do what it says.
```

`SETUP.md` is written for the agent, not for you: it finds Rhino's Python, registers the MCP
server, starts the broker so it survives the shell, starts the poller, and then *proves* it by
calling `rhino_state` rather than assuming success. The manual commands are in the chat window's
Settings screen if you would rather run them yourself.

> [!WARNING]
> `rhino_execute` runs arbitrary Python inside your Rhino session, exactly as `fusion_execute`
> does for Fusion. Same boundary, same caution: loopback only, token required, and work in a
> scratch document while you get a feel for it.

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
- [`uv`](https://docs.astral.sh/uv/) and `python3` on `PATH`
- **Any MCP client.** The server imports nothing Claude-specific, so Claude Desktop, Claude Code,
  Cline, Zed and anything else that speaks MCP all work. The `claude` CLI is optional — it only
  registers the server automatically and enables the `/fusion-chat` slash command.
- **For the chat panel only:** credentials for the Claude Agent SDK — either a signed-in `claude`
  CLI or an `ANTHROPIC_API_KEY`. The SDK ships its own CLI, so a separate Claude Code install is
  not required.

## Install

```sh
git clone https://github.com/francomichetti-dev/3d-mcp.git
cd 3d-mcp
scripts/install.sh
```

That is the whole install: MCP tools, the docked chat panel, and the Fusion knowledge skill.

> **Not yet on PyPI.** The package is built and its release pipeline is in place, but
> `fusion-3d-mcp` has not been published — so `uvx fusion-3d-mcp` will not work until it is. Once
> it is, `uvx fusion-3d-mcp install` gives you the MCP tools with no checkout. The two differ in
> one way worth knowing: a checkout **symlinks** the add-in so your edits are live, while the
> package **copies** it, because a `uvx` install lives in a disposable cache a symlink would
> outlive.

### Using it without the `claude` CLI

`install.sh` finishes fine without it and prints a ready-to-paste config with the absolute path
already filled in:

```json
"mcpServers": {
  "fusion": {
    "command": "uv",
    "args": ["run", "--frozen", "--no-sync",
             "--directory", "/absolute/path/to/3d-mcp/server", "fusion-3d-mcp"]
  }
}
```

Claude Desktop keeps that in `~/Library/Application Support/Claude/claude_desktop_config.json`.
Other clients have their own location — the shape is the same.

> `install.sh` bakes the **absolute** path of this checkout into the MCP registration and
> symlinks the add-in from it. Moving or renaming the directory afterwards breaks both —
> re-run `scripts/install.sh` from the new location if you do.

The installer creates `~/.fusion-mcp/` (0700) with a random 64-hex-char token (0600), symlinks
the add-in into Fusion's AddIns folder, links the knowledge skill into `~/.claude/skills/`,
builds the server **and agent** venvs with `uv sync`, registers the MCP server with Claude Code at
user scope, and generates the `/fusion-chat` command in `~/.claude/commands/`. Everything is
registered with absolute paths, so it works from any directory and in any project.

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

scripts/fusion-chat.sh --status
# bridge:  {"ok": true, ...}
# agent:   {"ok": true, "agent": true, "agent_error": null, "bridge_token": true}
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

Exports are confined to `~/Documents/fusion-mcp-exports/`, attachments to `~/.fusion-mcp/attachments/`.
Nothing third-party executes inside Fusion, and the bridge, MCP server and chat service make no
outbound calls — the deliberate exceptions are `uv sync` at install time, and the chat panel's own
calls to Anthropic, which is what makes it a chat.

**Work in a scratch Fusion document while iterating.** Generated code can mangle a design, and the
timeline is the only safety net.

The full threat model, including what does and does not count as a vulnerability, is in
[SECURITY.md](SECURITY.md).

## Troubleshooting

Two logs, both in `~/.fusion-mcp/`, and they are the only window into failures — Fusion swallows
add-in exceptions silently.

- **`addin.log`** — the add-in: startup, bind errors, every request, full tracebacks.
- **`server.log`** — the MCP server (it can never log to stdout; that would corrupt JSON-RPC).
- **`agent.log`** — the chat service, including anything a Fusion-spawned start printed before it
  could log for itself.

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
| Panel opens but shows a connection error | The agent service isn't up. `scripts/fusion-chat.sh --status`, then check `agent.log`. |
| Panel says "no design" with a design clearly open | It is not a Design document (drawing, or a non-Design workspace). The header names what the bridge sees. |
| A design's chat looks empty after reopening it | Closing a design compresses its chat by design — expand "core context from before this design was closed" at the top. |
| Two unsaved designs seem to share a chat | Neither has been messaged yet: identity is stamped on first message, so both correctly show an empty panel until then. |
| `agent.log` says `uv not found` | Fusion launched from Finder inherits a minimal `PATH`. The panel probes absolute locations; if `uv` is elsewhere, start the service from a terminal with `scripts/fusion-chat.sh`. |
| `agent.log` shows `ModuleNotFoundError: No module named 'encodings'` | Fusion's `PYTHONHOME`/`PYTHONPATH` leaked into the child. The spawn strips every `PYTHON*` variable — if you see this, the add-in is running stale code, so Stop/Run it. |

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
scripts/uninstall.sh            # add-in symlink, skill link, MCP registration,
                                # /fusion-chat command, and the agent service
scripts/uninstall.sh --purge    # also deletes ~/.fusion-mcp (token, logs, chats)
```

Exports in `~/Documents/fusion-mcp-exports/` are never touched.

## Status

Working and verified end to end against **Fusion 2704.1.36** on macOS: parametric part built and
confirmed by measurement, STL validated, all failure paths (bad code, wrong token, bad host,
oversized requests, timeouts, reload edge cases) exercised. The add-in's concurrency, timeout and
reload behaviour is covered by an offline harness that stubs `adsk`.

The chat panel is verified from a genuine cold start — no service, no palette — by firing the
command definition rather than calling its handler, so the test takes the same path a click does.
Per-design chats are verified against three real open documents: separate conversations, transcripts
restored on switching back, a mid-turn switch stopping the turn, context surviving a service restart
(the model still answers from the resumed session, not just the redrawn transcript), and a closed
design compressing to core context.

**Rhino 8 is working and in real use** on Windows — someone who is not the author has modelled
actual parts with it. Verified end to end on real hardware: `rhino_state` reads the live document,
`rhino_execute` builds geometry, `rhino_screenshot` returns a real capture, and the whole chain
carries non-ASCII intact (Spanish comments, accented layer names, an em-dash and a degree sign —
which cost two real bugs to get right, since Windows pipes default to cp1252, not UTF-8).

The Fusion half is verified on macOS only. Both bridges are plain Python with nothing
platform-specific in them, but `scripts/install.sh` knows only where Fusion keeps its add-ins on
macOS, so Fusion-on-Windows needs that path adding and a look at the launcher.

## Contributing

```sh
tests/run.sh     # offline: no CAD, no network, no API key
```

**345 assertions across six suites**, none of which need Fusion, Rhino, or an internet connection:

| Suite | Covers |
| --- | --- |
| `test_bridge.py` | the Fusion add-in: concurrency, timeouts, hot reload, per-document identity |
| `test_broker.py` | the job queue: single-flight, expiry when a CAD dies mid-job, long-poll wake-up, HTTP auth |
| `test_chat_registry.py` | per-design chats: isolation, resume, compression when a design closes |
| `test_install.py` | the installer: idempotency, token permissions, uninstall |
| `test_mcp_server.py` | the Fusion MCP tools |
| `test_rhino_mcp.py` | the Rhino MCP server: protocol conformance, every failure path, stream hygiene |

`tests/e2e/` additionally stands in for Rhino, so the full `claude → MCP → broker → CAD` chain can
be exercised on a machine with no CAD installed at all.

`fusion_bridge_impl.py` hot-reloads, so the edit loop does not involve restarting Fusion. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the reload endpoint and what review pays attention to.

## Prior art

Built from scratch, but two projects informed the design and deserve credit:
[ndoo/fusion360-mcp-bridge](https://github.com/ndoo/fusion360-mcp-bridge) (closest in shape —
execute + screenshot, custom-event marshalling, token auth) and
[rahayesj/ClaudeFusion360MCP](https://github.com/rahayesj/ClaudeFusion360MCP), whose most useful
finding was that the *knowledge files* mattered more than the bridge code.

## License

MIT — see [LICENSE](LICENSE).
