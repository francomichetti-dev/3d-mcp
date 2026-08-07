# Reference notes — Phase 0 recon

Studied read-only via the GitHub web UI / raw file fetches on 2026-08-04. **No code was copied**; everything below is a paraphrased description of mechanisms and design ideas.

Repos:

1. [ndoo/fusion360-mcp-bridge](https://github.com/ndoo/fusion360-mcp-bridge) — HTTP bridge add-in + thin MCP server, "execute arbitrary script" philosophy
2. [rahayesj/ClaudeFusion360MCP](https://github.com/rahayesj/ClaudeFusion360MCP) — file-polling bridge with ~40 fixed tools, heavy investment in knowledge/skill docs

---

## 1. ndoo/fusion360-mcp-bridge

Layout: `fusion-addin/FusionMCPBridge/` (add-in `.py` + `.manifest`), `mcp-server/server.py` (+ requirements), `scripts/quickstart-mac.sh`, `CLAUDE.md` (all Fusion API knowledge lives here, deliberately outside the code).

Chain: Claude → MCP server (Python, stdio) → HTTP on `127.0.0.1:7654` → Fusion add-in → Fusion API. Only two MCP tools: `fusion_execute` (run arbitrary Python inside Fusion with full `adsk.*` access) and `fusion_screenshot`. The MCP server is a deliberately minimal shim; intelligence lives in the knowledge file.

### Threading / main-thread marshaling

Fusion's Python API is main-thread-only; the HTTP listener runs on a background daemon thread, so every request is marshaled across:

- At `run()`, the add-in calls `app.registerCustomEvent("FusionMCPBridgeEvent")` and connects a handler class; handler references are kept in a module-level list to prevent garbage collection (a classic Fusion add-in requirement).
- Per HTTP request: generate a UUID request id, build a JSON envelope (`id`, `path`, `body`), and call `fireCustomEvent(event_id, json.dumps(envelope))`. Since `additionalInfo` is string-only, **everything travels as one JSON string**.
- The HTTP thread creates a `threading.Event()` per request, stores it in a module-level dict keyed by request id (`_pending_events`), and blocks in the request dispatcher on `event.wait(timeout=30)` (`REQUEST_TIMEOUT = 30` s, hardcoded). Timeout → HTTP 504.
- Fusion's main thread runs the handler's `notify()`: parse `args.additionalInfo`, route by `path`, execute, put the result dict in `_pending_results[req_id]`, then set the event. The HTTP thread pops the result and writes the HTTP response.
- No explicit lock around the pending dicts — relies on the GIL plus the one-writer/one-reader handoff discipline via the Event.

Script execution (`/execute`): the script string is compiled and `exec`-ed into a namespace pre-seeded with `adsk`, `app`, `ui` (userInterface), and `design` (which may be `None` — scripts are expected to check). Stdout is redirected into a StringIO to capture `print()` output; if the script defines a `run(...)` function (Fusion script convention), it is invoked. Exceptions come back as tracebacks in the JSON `result`/`error` fields. Return values are ignored — **printing is the result channel**.

### Auth

- Shared secret: 64-hex-char token generated with Python's `secrets` module by the quickstart script, written to `~/.fusion-mcp-secret` with `chmod 600`.
- Add-in loads the file at startup and checks every request's `Authorization: Bearer <token>` header; mismatch/missing → 401. If the secret file doesn't exist, the add-in **fails open** (accepts requests, warns) — questionable choice; only defensible because the server binds strictly to loopback.
- MCP server reads the same file and attaches the header; on a 401 it returns a human-readable remediation message ("regenerate the shared secret").

### Screenshot

- MCP tool signature: `fusion_screenshot(direction="current", width=1024, height=768)`. Named directions: front, back, left, right, top, bottom, and four iso corners (iso-top-right, iso-top-left, iso-bottom-right, iso-bottom-left) — implemented not via Fusion NamedViews but by **repositioning the camera**: compute a new eye point from the camera target plus a hardcoded normalized direction vector, set an appropriate upVector, assign the camera back to the viewport.
- Capture: write to a temp PNG via the viewport's save-as-image call, read bytes, base64-encode, return JSON `{screenshot, format, width, height}`. The MCP server passes the JSON through (does not convert to an MCP Image type).
- **Broken in practice on current Fusion**: all seven open issues in the repo are about this. `Viewport.saveAsImageFileWithOptions()` on Fusion 2702+/2704 raises "takes 2 positional arguments but 5 were given" — the API now takes a `SaveImageFileOptions` object, not positional args. Proposed fixes in the issues: use the stable 3-argument `Viewport.saveAsImageFile(path, width, height)` instead, or construct the options object property-by-property; one issue also proposes returning a file path instead of inline base64 (payload size concern).

### Lifecycle (run/stop, port)

- `run(context)`: get app → load secret → register custom event + handler (keep references) → create `HTTPServer` bound to `127.0.0.1` on port from `FUSION_MCP_PORT` env var (default 7654) → serve on a **daemon** thread (so a hard Fusion exit can't be held open) → message box confirming startup.
- `stop(context)`: `_http_server.shutdown()` first, then `unregisterCustomEvent(event_id)`, then clear handler references. Daemon flag is the backstop for port cleanup; add-in reload (stop→run) works because shutdown fully closes the listener socket.
- HTTP handler overrides `log_message()` to silence per-request access logs (Fusion's Text Commands palette otherwise gets noisy).

### macOS install / quickstart

`scripts/quickstart-mac.sh` performs, in order:

1. Python deps: prefer an existing venv at `~/venv`, else system Python with `pip --user`, installing from `mcp-server/requirements.txt`.
2. Secret: create `~/.fusion-mcp-secret` (64 hex chars, mode 600) if absent; reuse if present.
3. Add-in: copy the `FusionMCPBridge` folder into `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/`, replacing prior versions.
4. Claude config: create/patch `~/.claude/settings.json` with a `fusion360` entry under `mcpServers` (absolute python path + absolute server.py path); skip if already present. Desktop app users edit `claude_desktop_config.json` instead.
5. Verify `server.py` parses (AST check), then print manual steps: enable the add-in via Fusion's Tools → Add-Ins (checking "run on startup"), restart Claude, and a `curl` health-check command against `/health`.

The MCP server also exposes a `fusion://status` resource that proxies `/health` (Fusion version, active document, port) — cheap connectivity diagnosis.

### Knowledge file (CLAUDE.md) highlights

All Fusion API knowledge is concentrated in CLAUDE.md, read automatically by Claude Code. Gotchas it documents:

- **Units**: Fusion's internal DB unit is **centimeters**; raw floats to `ValueInput` are interpreted as cm. Recommends passing formatted strings ("3 cm", "45 deg") to be explicit.
- **Revolve**: the #1 failure — profile crossing the revolve axis is rejected; the profile must lie entirely on one side (touching is OK, crossing is not).
- **Arcs**: prefer three-point arc creation over center/start/sweep (sweep direction ambiguity causes silent geometry errors).
- **Profiles**: `sketch.profiles.count == 0` triage list — open contours, duplicate edges, endpoints on the axis, wrong sketch plane.
- **Design mode**: parametric vs direct — timeline features and `TemporaryBRepManager` bodies needing a `BaseFeature` wrapper (startEdit/finishEdit) exist only in parametric mode; check `design.designType` before using timeline-dependent APIs.
- **Booleans**: `booleanOperation` mutates the target in place and takes a **single BRepBody**, not an ObjectCollection.
- **Script contract**: define `run(_)`, `print()` all results (return values ignored), defensively check `design is None`.
- Reference list of the root component's construction planes (xY/xZ/yZ) and axes.
- Maintenance protocol: re-test documented patterns after each Fusion update (this repo got burned by exactly this — see screenshot issues); a migration section anticipates native Autodesk MCP support.

### Other gotchas (README/issues)

- "No active document" errors when no design is open — health endpoint exposes document state for diagnosis.
- MCP config paths must be absolute.
- Port 7654 collision if another instance/tool holds it (env var override exists).
- Issue tracker shows no threading-deadlock reports — the Event-with-timeout design degrades to 504 rather than hanging.

---

## 2. rahayesj/ClaudeFusion360MCP

Layout: `mcp-server/fusion360_mcp_server.py`, `fusion-addin/FusionMCP.py` + `.manifest`, `docs/` (SKILL.md, SPATIAL_AWARENESS.md, TOOL_REFERENCE.md, KNOWN_ISSUES.md), `examples/getting_started.md`.

### Transport & threading (contrast with ndoo)

- **File-based polling, not HTTP**: the MCP server writes `command_*.json` into `~/arges_mcp_comm/`; the add-in's background daemon thread globs the directory every 0.1 s, executes, and writes `response_<command_id>.json` back. `run()` creates the comm dir and starts the monitor thread; `stop()` flips a `stop_thread` flag.
- **No main-thread marshaling**: the polling thread appears to call Fusion API directly from the background thread — no CustomEvent hop. This works "often enough" but violates Fusion's threading contract and is a stability risk (intermittent crashes/corruption class of bug). Their issue #3 (`activeEditObject` returning a Component instead of the expected Sketch, breaking draw_rectangle/draw_circle) is the kind of state-assumption fragility this architecture invites; suggested fix was addressing the newest sketch explicitly (`sketches.item(count-1)`) instead of trusting active-edit state.
- ~50 ms round trip per command; they added a `batch_operations` tool (multiple commands executed atomically) claiming 5–10x speedup for multi-step work.
- Fixed-tool surface (~15 implemented in main, docs describe 40+, an issue tracks expansion to 79 tools/7 categories with a 202-test suite): create_sketch/draw_*/extrude/revolve/fillet/chamfer/shell/draft/patterns/mirror, components + move/rotate, revolute/slider joints, inspection (get_body_info, measure, get_design_info), export (STL/STEP/3MF), undo/delete utilities.

### Knowledge/skill file design (the interesting part)

Delivery: users create a **Claude Project and paste SKILL.md into Project Instructions**, optionally adding SPATIAL_AWARENESS.md. README's own verdict: *"The hard part wasn't the code, it was getting Claude to understand where things go in 3D space"* — spatial-reasoning docs were the breakthrough, not tooling.

**SKILL.md** (versioned, v1.0.0 targeting a specific MCP version) structure:

- Session initialization protocol: on session start, take a screenshot/vision check, call get_design_info, compare against expectations (file name, body count, timeline, error badges) before doing anything — guards against state mismatch after crashes or manual user edits.
- Coordinate-system mastery incl. the **Z-negation rule** (below).
- Explicit tool-limitation section (e.g., "extrude always creates a NEW body; no boolean combine via MCP — ask the user to do Modify → Combine manually"). Documenting what the bridge *cannot* do proved as valuable as documenting what it can.
- Join protocol: joining/combining is the FINAL step and requires explicit user approval — never auto-join, because bad joins are the hardest thing to undo.
- Complete tool reference (signature, parameter table, returns, examples per tool).
- Assembly positioning doctrine: create geometry centered at origin, then `move_component` — rather than baking offsets into sketch coordinates.
- Manufacturing guidelines (FDM/SLA/injection wall thicknesses, draft angles, clearance-hole chart for metric fasteners, 0.2–0.5 mm fit clearances).
- "Verified lessons learned": failure post-mortems distilled into hard rules with capitalized enforcement language (MANDATORY/CRITICAL/HARD RULE) *plus the reasoning and the original failure story* — they found prescriptive rules with rationale stick better than neutral reference prose.
- Checklists: session-start (6 items), pre-extrusion (4 items), assembly verification (4 steps).

**SPATIAL_AWARENESS.md** — core principle: *"Never assume spatial relationships — verify them programmatically."*

- Plane mapping table (empirically calibrated, and marked as such): XY sketch → world X/Y, extrude ± → ±Z, no negation; **XZ plane: sketch Y → world −Z (negated!), extrude ± → ±Y; YZ plane: sketch X → world −Z (negated!), extrude ± → ±X**. Offset-plane semantics: offset on XY moves along Z, on XZ along Y, on YZ along X.
- Bounding-box reading guide (which min/max face corresponds to top/bottom/left/right; interior surfaces of hollow bodies at exterior ± wall thickness).
- Five-step pre-operation protocol: state intent in world coords → query existing geometry (measure) → plan plane/offset/direction with negation applied → **predict the resulting bounding box numerically** → execute and re-measure, comparing to prediction within ±0.001.
- Documented error cases as numbered incidents (e.g., "grip ridges floating in space" from extruding away from the body; face/edge **indices shift after every modifying feature** — re-query and identify faces by centroid, never by remembered index).

**KNOWN_ISSUES.md** — 12 issue+workaround pairs, the most build-relevant:

1. Units: everything is cm; mm ÷ 10; treat any dimension > 50 as a red flag (that's half a meter).
2. Extrusion direction per plane (XY→±Z, XZ→±Y, YZ→±X); visualize the normal first.
3. Components spawn at origin — always list_components, then move, then verify.
4. Save vs export: the API path can't "save" the .f3d; ask the user to Ctrl+S, then export.
5. Bevels on thin parts: chamfer (≈ thickness/2), never boolean cuts.
6. Deletion shifts indices — delete highest-index first, or address by name; re-query after.
7. Standard fastener clearance-hole chart (M3 → 0.34 cm, etc.).
8. Mating parts need designed-in 0.2–0.5 mm clearance; verify with measure.
9. Session-state mismatch after crash/recovery — always get_design_info first.
10. Per-command round-trip cost — batch operations.
11. XZ-plane Y-inversion (the Z-negation rule restated; alternative: sketch on XY at an offset instead).
12. Auto-join without verification — require explicit user approval before combine.

---

## Implications for our build

### Threading / marshaling

- **Adopt** (from ndoo): CustomEvent marshaling — registerCustomEvent at startup, fireCustomEvent with a single JSON-string envelope (uuid id + payload) since additionalInfo is string-only, per-request `threading.Event` + result dict keyed by id, `event.wait(timeout)` → 504 on expiry. Proven, deadlock-free-by-timeout, no reported threading issues.
- **Adapt**: add a small lock (or single-entry queue) around the pending dicts rather than relying on GIL luck; keep ndoo's keep-handler-references-alive discipline (Fusion GC eats handlers otherwise).
- **Avoid** (rahayesj): calling Fusion API from a background polling thread with no main-thread hop. Also avoid file-polling transport generally — 100 ms poll latency, filesystem races, litter in `$HOME`.

### Tool surface

- **Adopt** (ndoo): minimal tool surface — `execute` (arbitrary script, namespace pre-seeded with adsk/app/ui/design, stdout captured, tracebacks returned) + `screenshot` + a health resource. Put all CAD intelligence in knowledge docs, not in dozens of bespoke tools. rahayesj's fixed-tool approach needed 79 tools to chase API parity and still couldn't do booleans.
- **Adapt** (rahayesj): their batching insight matters less for us — one `execute` script *is* a batch.

### Auth

- **Adopt** (ndoo): loopback-only bind + shared secret file (`~/.fusion-mcp-secret`, 64 hex via `secrets`, mode 600) checked as a Bearer header; 401 with remediation hint.
- **Adapt**: fail **closed** when the secret file is missing (ndoo fails open with a warning).

### Screenshot

- **Adapt** (ndoo, corrected per their issues #1–#7): capture via the viewport image-save API, but use the stable 3-arg `Viewport.saveAsImageFile(path, width, height)` — `saveAsImageFileWithOptions` changed signature on Fusion 2702+/2704 (now wants a SaveImageFileOptions object) and broke every screenshot. This is the single most concrete failure mode learned from recon.
- **Adopt** (ndoo): named camera directions (6 orthos + 4 isos + "current") implemented by setting eye/upVector relative to the camera target, then fit; return base64 PNG. Consider a size cap or file-path fallback for large captures (their issue #5 raises payload size).

### Lifecycle / install (macOS)

- **Adopt** (ndoo): daemon HTTP thread; `stop()` = server.shutdown() then unregisterCustomEvent then drop handler refs; port via env var (default 7654); silence request logs; message box on start.
- **Adopt** (ndoo): quickstart script shape — deps → secret → copy add-in into `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/` → patch Claude config with absolute paths → print the two manual steps (enable add-in in Tools → Add-Ins with run-on-startup; restart Claude) + a curl `/health` check.

### Knowledge docs (the highest-leverage layer)

- **Adopt** (both): a CLAUDE.md-style knowledge file covering: cm units (pass ValueInput as formatted strings), revolve profile-crossing-axis, three-point arcs over sweep arcs, profiles.count==0 triage, parametric-vs-direct mode and BaseFeature wrapping, single-body booleanOperation, script contract (run(), print results, check design None).
- **Adopt** (rahayesj): the spatial-awareness method — plane-mapping table with the **Z-negation rule for XZ/YZ sketches**, predict-then-verify bounding boxes, re-query face/edge indices after every modifying feature, session-start state check, never auto-join without user approval, screenshot-verify after milestones. Their own conclusion: spatial docs, not code, made the quality difference.
- **Adopt** (rahayesj): document what the bridge *can't* do and the manual-UI fallback for it; write rules as prescriptive checklists with the original failure story attached.
- **Adopt** (ndoo): maintenance protocol — re-verify documented API patterns after each Fusion update (their screenshot breakage proves Fusion does ship breaking API changes mid-cycle).
- **Avoid**: trusting `activeEditObject`/active-selection state between commands (rahayesj issue #3) — within a script, hold explicit references to the sketch/feature objects you create.
