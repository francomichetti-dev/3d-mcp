# PLAN — fusion-mcp, step by step

Read `HANDOFF.md` first (directives: build-it-ourselves, 100% local; GPU facts; safety rules). Phases are ordered; each ends with a verifiable checkpoint. Phases 0–3 are the product (Franco explicitly wants the Phase-3 renders — "cool renders" is a committed feature, not a nice-to-have); Phase 4 is optional.

Presence tags: **[GUI]** = needs Fusion 360 open on the Mac (Franco present or asked to open it) · **[headless]** = no Fusion needed · **[NAS]** = needs NAS reachable over SSH. Parallelism: server code, install script, and Phase 3 steps 1–3 are all **[headless]** and may proceed in parallel once `fusion_export`'s contract is fixed; only the gauntlet, the Phase 2 checkpoint, and the Phase 3 checkpoint gate on **[GUI]**.

> Plan revised 2026-08-04 after a 5-lens review (architecture, security, Fusion API, MCP design, completeness; 40 findings → 21 applied edits, load-bearing claims fact-checked against Autodesk/MCP docs) plus read-only recon of two reference bridges — see `docs/reference-notes.md`.

---

## Phase 0 — Recon (~30 min)

1. **[GUI]** Confirm Fusion 360 launches on this Mac and note its exact version. (App found at `~/Applications/Autodesk Fusion.app`, non-sandboxed webdeploy install.)
2. **[GUI]** Confirm the add-ins path exists: `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/` *(confirmed 2026-08-04)*. Then verify Fusion actually loads a **symlinked** add-in folder on this Mac with a trivial hello add-in — Autodesk docs are silent on symlinks; multiple macOS projects rely on them, but it's unverified here. If symlinks don't load, `install.sh` copies instead and gains an `--update` mode.
3. ~~Skim the two reference repos~~ *(done 2026-08-04 — notes in `docs/reference-notes.md`; read-only, no code copied)*. Key adoptions: JSON-envelope CustomEvent marshaling with per-request Event; stable 3-arg `saveAsImageFile` (the `WithOptions` variant is broken on Fusion 2702+); fail-closed token auth; minimal tool surface (rahayesj's 79-tool approach is the anti-pattern).
4. **[headless]** `git init`; commit a `.gitignore` covering `.venv/`, `__pycache__/`, `*.pyc`, `*.log`, `exports/`, `renders/`, `.DS_Store`, `reference-clones/`; first commit; private repo `francomichetti-dev/fusion-mcp`.

**Checkpoint:** repo exists, reference notes written, Fusion launches, symlink-loading verified (or copy-mode decided).

## Phase 1 — The bridge, built from scratch (core deliverable)

### 1a. Repo layout
```
fusion-mcp/
  addin/FusionBridge/        # Fusion 360 add-in (Python)
    FusionBridge.py          # thin loader — rarely edited
    fusion_bridge_impl.py    # all logic — reloadable via /reload
    FusionBridge.manifest
  server/                    # MCP server (Python, FastMCP, stdio)
    mcp_server.py
    pyproject.toml
  skill/FUSION-KNOWLEDGE.md  # Phase 2
  scripts/install.sh         # symlink add-in, generate token, register MCP
  scripts/uninstall.sh       # remove add-in, claude mcp remove, optionally ~/.fusion-mcp
  docs/
  README.md                  # install steps, the one manual Fusion step,
                             # troubleshooting → ~/.fusion-mcp/addin.log, uninstall
```

### 1b. Add-in (`addin/FusionBridge/`) **[headless to write, [GUI] to test]**

**Loader/impl split.** `FusionBridge.py` is a thin loader: wraps the impl import + `run()` in try/except that appends to `~/.fusion-mcp/addin.log` **even on import-time failure** and shows a one-time `ui.messageBox` on bootstrap errors — otherwise an import crash produces no log, no listener, no symptom. All logic lives in `fusion_bridge_impl.py`, reloadable via a `POST /reload` endpoint (`importlib.reload`) so handler edits don't need the Fusion Add-Ins dialog each iteration. Load it **by explicit path** (`importlib.util.spec_from_file_location`) under that namespaced key rather than putting the add-in dir on `sys.path` — all Fusion add-ins share one interpreter and one `sys.modules`, so a generic module name can collide in either direction. `/reload` **byte-compiles the on-disk source before tearing the listener down** and refuses on `SyntaxError` (the likeliest outcome of an edit-reload loop), and restores the listener if the reload fails anyway.

**Manifest.** JSON: `autodeskProduct: "Fusion360"` (docs say "Fusion" but every shipping Autodesk example uses "Fusion360"), `type: "addin"`, a generated GUID `id`, `version`, `supportedOS: "mac"`, `description`, and **`runOnStartup: true`** — repo-controlled, so the only manual step left is running it once in the current session. A missing/invalid manifest makes the add-in silently absent from the dialog. Note: add-ins start while Fusion is still initializing — if startup flakiness appears, defer listener start to `Application.startupCompleted`.

**HTTP listener.** Stdlib `ThreadingHTTPServer` (no third-party deps inside Fusion) bound to **127.0.0.1:7654**, `daemon_threads = True` and `block_on_close = False` so a request still waiting on Fusion's main thread can never hold up add-in reload or Fusion's own quit. Serialization is enforced by the **single-flight guard** (`_active_job` under a lock), not by the socket — that is what makes it safe to handle connections concurrently, and it is what lets `/health` stay answerable during a long execute (a liveness probe that blocks behind the thing you're probing is useless) and lets a second `/execute` get its 409 immediately instead of queueing behind a 60 s wait and then landing late against the server's 75 s client timeout. `allow_reuse_address` (default) makes restarts rebind cleanly. Wrap the bind in try/except — log + `messageBox` on `EADDRINUSE`, never die silently.

**Main-thread marshal contract.** Fusion's API is main-thread-only; **every** `adsk.*` touch — `/execute` *and* `/screenshot` — goes through one marshal path:
- `app.registerCustomEvent(EVENT_ID)` once at startup; keep the handler instance in a **module-level `handlers` list** for the add-in's lifetime — Fusion holds handlers weakly, and a GC'd handler makes `fireCustomEvent` silently do nothing (the classic "built it and nothing happens" failure).
- Per request: a `uuid`; the CustomEvent payload is a JSON string `{id, kind, ...}` (`additionalInfo` is string-only). Replies land in a dict keyed by id, each with its own `threading.Event`; the HTTP thread waits with timeout.
- **Timeout ~60 s abandons the wait only** — main-thread `exec()` cannot be cancelled, and custom events are deferred while Fusion shows a modal dialog. A timed-out entry is marked abandoned; any late reply for it is dropped and logged, **never delivered to a later caller**. The timeout error text must say: *"code may still be executing; do not resend; check fusion_state/screenshot."*
- **Single-flight:** while a previous (possibly abandoned) execution is still running on the main thread, reject new `/execute` with 409 "previous execution still running".
- **The single-flight guard must never wedge.** Three ways it could and all are closed: the main-thread handler catches `BaseException` (not just `Exception`) so generated code raising a bare `BaseException` can't escape and leave the job uncompleted; an unparseable event envelope explicitly fails the active job instead of returning silently; and `run()` clears any state stranded by a previous `stop()` (a request in flight when the add-in stops is abandoned by its waiter, but its queued event never dispatches once the event is unregistered — so without this, toggling the add-in, which is exactly how a user recovers from a hang, would leave every later request at 409 until Fusion itself restarts).
- At the top of the event handler, terminate any active command (per Autodesk's threading guidance).

**`POST /execute`** — body: Python source. One **persistent module-level namespace** reused across calls (so Claude can reference entities from prior calls), pre-loaded with `adsk.core`, `adsk.fusion`; `app`, `ui`, `design` are **re-resolved and re-injected at the start of every request** in the main-thread handler: `design = adsk.fusion.Design.cast(app.activeProduct)` — if `None` (no document open, or active product isn't a Design), short-circuit with `{ok:false, error:"no active Fusion design — open or create one and switch to the Design workspace"}` without running `exec()`. Optional `{reset: true}` clears the namespace. `result` variable convention + captured stdout. Returns `{ok, result, stdout, traceback}`.

**`POST /screenshot`** — a canned marshaled job on the same queue. Exact capture sequence: `cam = viewport.camera` → `cam.viewOrientation = adsk.core.ViewOrientations.<Front|Top|Right|IsoTopRight>ViewOrientation` for named views → **`cam.isSmoothTransition = False`** (defaults `True`; otherwise the assignment animates and the capture happens mid-flight) → `viewport.camera = cam` → `viewport.fit()` (doubles as the `fit` view) → `viewport.refresh()`/`adsk.doEvents()` (safe — we're on the main thread) → **`viewport.saveAsImageFile(path, w, h)`** — the stable 3-arg form; do **not** use `saveAsImageFileWithOptions` (broken on Fusion 2702+, see reference notes). Default 1200×800; clamp to ≤1920×1440 with a clear error above. Returns base64 PNG.

**`GET /health`** — answered **directly from the HTTP thread from cached data only** (app version cached at startup; last document name piggybacked from each marshaled request), so it stays a liveness probe even mid-execution and can report `busy: request running N s`. Returns version + `BRIDGE_PROTOCOL_VERSION` + active doc name.

**Auth — fail closed.** If `~/.fusion-mcp/token` is missing/empty/unreadable, **do not start the listener**; log why and show a one-time `messageBox` (silence here means either a confusing dead bridge or an unauthenticated RCE endpoint). Token required on **all** endpoints including `/health`, carried in `X-Fusion-Bridge-Token` (a non-safelisted header forces a CORS preflight the bridge never answers — kills browser-origin POSTs), compared with `hmac.compare_digest`, checked **before** reading the body, re-read per request with an mtime cache (rotation without restarting Fusion). Validate `Host` is exactly `127.0.0.1:7654` or `localhost:7654` (defeats DNS rebinding). Never emit CORS headers.

**Limits.** Per-connection socket timeout ~70 s; require `Content-Length`, cap ~5 MB (413 on excess); cap stdout and result at ~64 KB each with an explicit `[truncated N bytes]` marker, always preserving the **tail** of a traceback.

**Lifecycle.** `stop()`: set a shutting-down flag (marshal waits check it in short `Event.wait` slices) → `httpd.shutdown()` + `server_close()` + `thread.join(timeout=5)` → remove handler from `handlers`, `app.unregisterCustomEvent(EVENT_ID)`. A stuck `/execute` must not hang Fusion quit or add-in reload; the port must not leak.

**Logging.** `~/.fusion-mcp/` mode 0700, `addin.log` 0600. Never log the token or raw headers. Log endpoint, body size, ok/fail, duration, full tracebacks; executed code only truncated or behind a debug flag. Rotate at ~5 MB. This log is the only debugging window — Fusion swallows exceptions silently.

### 1c. MCP server (`server/mcp_server.py`) **[headless]**

- Python ≥3.11, `fastmcp`, stdio transport. **Stdio discipline:** never `print()` to stdout — any stray stdout write corrupts JSON-RPC framing and the server just "fails to connect". All logging to stderr or `~/.fusion-mcp/server.log`.
- **No bridge contact at import/startup** — reachability is checked lazily per call (the round-trip is the check). HTTP client timeout explicitly **75 s** (longer than the add-in's 60 s wait — httpx defaults to 5 s, which would kill every nontrivial modeling op at the wrong layer). Claude Code's `MCP_TOOL_TIMEOUT` must exceed both (document in README).
- **Version handshake:** `BRIDGE_PROTOCOL_VERSION` shared constant, returned by `/health` and in an `X-Bridge-Version` header on every response; on mismatch raise `ToolError` — *"FusionBridge add-in is vX, server expects vY — restart the add-in (Utilities → Add-Ins → stop/run)"*. Doubles as the "you edited the add-in, reload it" signal.
- **Tool descriptions carry the contract** — they're the only steering surface guaranteed in context every call (the Phase 2 skill loads conditionally and doesn't exist yet in Phase 1). `fusion_execute`'s description MUST state: (1) internal length unit is **cm** regardless of display units — "20 mm = 2.0"; (2) the injected names (`adsk`, `app`, `ui`, `design`), the `result` convention, captured stdout; (3) "after any geometry change, call fusion_screenshot to verify". `fusion_screenshot`'s description lists the valid view names. Keep descriptions tight; the skill file is the long-form home.
- Tools (few and powerful):
  - `fusion_execute(code: str)` — Fusion-code failure is a **successful tool call** returning `{ok:false, traceback, stdout}` verbatim — never a raised exception (FastMCP masks generic exception details, which would destroy the self-correction loop). Bridge-level failures (connection refused, 401, timeout, version mismatch) raise `ToolError` with an actionable message: *"Fusion not running or FusionBridge add-in not enabled — check Utilities → Add-Ins"*.
  - `fusion_screenshot(view: str = "iso", width: int = 1200, height: int = 800)` — decodes the add-in's base64 and returns FastMCP's `Image` type so it serializes as an MCP ImageContent block Claude actually **sees** — never base64-as-text.
  - `fusion_export(format: "stl"|"step"|"3mf"|"usd", body_or_component: str = "", path: str = "")` — `usd` included now so Phase 3 reuses this wrapper (decided in Phase 1, not mid-Phase-3). Build the generated snippet by embedding parameters via `json.dumps` and parsing inside the snippet — never raw f-string interpolation (quotes in component names break generated code). Path: `expanduser`+`realpath`, required under `~/Documents/fusion-mcp-exports/` (clear error otherwise — `fusion_execute` is the escape hatch for exotic destinations); create the dir at server start; timestamp default filenames. Per-format geometry semantics (verified against ExportManager docs): default (empty name) = whole design/root component for all formats; `stl`/`3mf` take BRepBody, Occurrence, or Component — resolve the name against bodies, then occurrences/components; `step` is **Component-only** — a body name returns a clear error; an ambiguous bare name returns an error listing the candidates so Claude can retry. `meshRefinement` defaults high. Document per-format behavior in the tool description.
  - `fusion_state()` — bounded payload: doc name, `designType` (parametric/direct), units string, timeline item count, user parameters (name/expression/value), and a **depth-1** listing of root-level occurrences and root bodies by name with counts ("Housing:1 (3 bodies)") — hard cap ~50 entries with a "truncated, N more" marker. Same no-active-design guard as `/execute`. Description says deeper introspection goes through `fusion_execute`.

### 1d. Install & registration **[headless to write]**

`scripts/install.sh`:
- `umask 077` at top; `mkdir -p -m 700 ~/.fusion-mcp`.
- Token created **atomically** (`os.open` with `O_CREAT|O_EXCL`, 0600, `secrets.token_hex(32)`); if one already exists, **keep it** and print "existing token preserved" — blind regeneration silently 401s a running Fusion. `--rotate-token` for explicit regeneration.
- Detect the AddIns dir exists, fail loudly if not; symlink `addin/FusionBridge` into it (or copy + `--update` if Phase 0 falsified symlink loading).
- No separate `uv venv` step — `uv run` creates/syncs the venv from `pyproject.toml`. Register with the **absolute** repo path baked in: `claude mcp add fusion -s user -- uv run --directory /abs/path/to/repo/server mcp_server.py` (Claude Code launches from arbitrary cwds).
- Print the one manual step: *"Tools → Add-Ins → select FusionBridge → Run (auto-starts on later launches — runOnStartup is in the manifest)"*. After Franco enables it, verify with `GET /health` before declaring install success.

`scripts/uninstall.sh`: remove the add-in link/copy, `claude mcp remove fusion`, optionally `rm -rf ~/.fusion-mcp`.

### 1e. Test gauntlet **[GUI]** (do all, in a scratch Fusion design)

1. `fusion_state` → correct doc info.
2. Execute: create sketch + 20 mm cube (**API units are cm** — 20 mm = 2.0).
3. Screenshot → cube visible, and it arrives as a **rendered image** in Claude Code, not base64 text. Request `front` then `iso` back-to-back → two distinct correct views (no mid-animation captures).
4. Parametric part: 40×30×15 mm enclosure, 2 mm walls, 4× M3 screw bosses, filleted edges — iterate using screenshots until right. **Record the tool-call count** (baseline for the Phase 2 checkpoint).
5. Export STL, open it in a viewer/slicer on the Mac. Also: export targeting a component whose name contains a quote character.
6. API surface probe: one `/execute` listing `exportManager`'s `create*Options` methods — confirms the installed API's export surface (incl. USD for Phase 3).
7. Failure paths:
   a. Bad Python → full multi-line traceback comes back (Fusion doesn't hang).
   b. Code sleeping past the timeout → timeout error returns, **and a follow-up request does not receive the stale reply**.
   c. Fusion closed → clean `ToolError`.
   d. Wrong token → 401.
   e. Token file absent → listener refuses to start, reason in `addin.log`.
   f. Zero documents open → clean structured error, no exec.
   g. Toggle the add-in off/on twice → `/execute` still works (no port leak, no dead handler).
   h. Edit `fusion_bridge_impl.py` → `POST /reload` → new behavior without restarting Fusion. Also: introduce a syntax error, `/reload` → refused, **bridge still serving on the old code**.
   j. Abandon a request (7b), then toggle the add-in off/on → `/execute` works again (stranded single-flight state cleared, not a permanent 409).
   k. While a long `/execute` is in flight, `GET /health` answers immediately and reports `busy`.
   i. From a directory **outside** the repo, `claude mcp list` shows fusion connected.

**Checkpoint:** the enclosure test passes end-to-end. Commit, tag `v0.1`.

## Phase 2 — Knowledge layer (what makes it *good*) **[headless to write, [GUI] to verify]**

Delivery is layered — **not** a repo CLAUDE.md (which only loads when the session cwd is inside this repo; Franco prompts CAD from arbitrary directories, which is why the server is user-scoped):
- (a) The non-negotiables (cm units, result convention, screenshot-after-every-change) live in the MCP tool descriptions (done in 1c).
- (b) `skill/FUSION-KNOWLEDGE.md` installed by `install.sh` as a **user-level skill** (`~/.claude/skills/`) with a trigger-rich description (Fusion 360, CAD, 3D print, STL, enclosure, bracket, …).

Content:
- Boilerplate patterns: get app/design, new component, sketch on plane/face, extrude (new body/join/cut), revolve, fillet/chamfer, holes, patterns, mirror, user parameters.
- **Gotchas:** internal units are cm (pass `ValueInput` as strings like `"3 cm"` where possible); parametric vs direct modeling (`design.designType`); `.item(i)` collections; profiles must be selected from `sketch.profiles` (`profiles.count == 0` triage); everything about `ObjectCollection`; single-body `booleanOperation` takes a body, not a collection; revolve profiles must not cross the axis.
- **The BaseFeature recipe:** in parametric designs, temp-BRep operations and `Component.bRepBodies.add()` only work inside `baseFeatures.add()` + `startEdit()`/`finishEdit()`; they work directly only in direct-modeling designs. LLM-generated code reaches for temp-BRep constantly and new documents default to parametric.
- **Bridge-safety rules for generated code:** never create/execute UI commands, call `ui.messageBox`, or call `adsk.doEvents()` inside `fusion_execute` code — dialogs stall the custom-event queue and deadlock the bridge. Chunk big operations across multiple calls; never write unbounded loops — the timeout cannot cancel main-thread code.
- **Stale references:** entity proxies go stale after timeline edits/deletes — on `RuntimeError("object is invalid")`, re-look-up by name/index. Face/edge indices shift after every feature — re-query, identify by centroid.
- Spatial conventions: axis orientation, which plane for which view, the **Z-negation rule** (sketch coords mapping to world Z are negated on XZ/YZ planes), how to place features relative to existing geometry (the #1 thing prompting gets wrong).
- Franco's 3D-printing defaults: clearance/tolerance values, min wall thickness, M3 boss/insert dimensions, orientation-for-printing notes.
- Workflow rules for Claude: session-start state check (`fusion_state` + screenshot before assuming anything); **after every geometry change, screenshot and verify before continuing**; predict the bounding box before an operation, verify it after.

**Checkpoint:** re-run gauntlet test 4 in a fresh session — tool-call count **numerically lower** than the Phase 1 baseline. Commit, tag `v0.2`.

## Phase 3 — NAS GPU render pipeline (committed feature — the "cool renders")

Goal: photoreal renders/turntables of exported designs using the NAS's GTX 1660 Super — Fusion itself can never use a remote GPU (see HANDOFF).

**Interchange format (decided up front, fact-checked):** the Fusion API has **no glTF export**, and stock Blender **cannot import STEP** — the originally planned chain was broken on both legs. Primary: **USD** (`ExportManager.createUSDExportOptions`, exported via the same `fusion_export` wrapper; Blender imports USD natively, materials → Principled BSDF). Fallback: STL (geometry-only). **Early spike before building the container:** export one colored part as USD, import in Blender on the Mac or NAS, verify appearances survive. If they don't, fall back to geometry-only import + per-preset materials + an optional accent-color parameter (clay/dark/blueprint never needed source materials anyway).

1. **[NAS]** Blender container on TrueNAS with `--gpus all` (own Dockerfile: `blender` + CUDA-capable base; verify Cycles sees CUDA with `blender -b --python-expr` probe). NAS-local compose under `/mnt/fast_pool/apps/blender-render/` (like mosquitto/nextcloud pattern). No ports exposed — jobs go in over SSH.
2. **[headless]** `render/render_job.py`: import **USD or STL only**, camera auto-framing, Cycles CUDA, output PNG(s) or turntable frames. **Render presets** (asset-light, no external HDRI downloads — 100% local rule): `studio` (three-point lighting, soft shadows, neutral backdrop), `clay` (matcap-style single material — great for form review), `dark` (glossy dark-glass hero shot), `blueprint` (freestyle line-art on blueprint blue). Developable **without Fusion**: generate a local test asset with `blender -b --python` emitting a cube/monkey as USD+STL.
3. **[NAS]** Turntables: render N frames of a 360° rotation, assemble to MP4 with ffmpeg **on the NAS** (already used by the video-editor pipeline), copy the MP4 back.
4. **[NAS]** MCP tools — **submit/poll, not blocking** (a 150-frame Cycles turntable on ~2.1 GB VRAM plausibly runs 30–90+ min; a single blocking MCP call gets killed or backgrounded by Claude Code's timeouts — verified against current docs):
   - `fusion_render_nas(preset, resolution, transparent_bg)` / `fusion_turntable_nas(preset, seconds, fps)` — preflight `ssh nas true` (clear *"NAS unreachable — check Twingate/Tailscale"* error), export USD → `scp` up, detached start (`nohup` + status file), return a **job id** immediately. Fast stills may still return inline when done within the call budget.
   - `fusion_render_status(job_id)` — progress / frames done / final results; completed images returned as MCP images so Claude can quality-check the shot.
   - All ssh/scp: `-o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10`; one-time documented `known_hosts` seeding for **both** aliases (`nas` and `truenas` are separate entries).
   - Per-job dirs via `ssh mktemp -d` under `/mnt/fast_pool/apps/blender-render/jobs/`; frames render as individual numbered files so CUDA OOM or disconnect **resumes from the last completed frame**; ffmpeg assembly is idempotent and runs only when all frames exist; results copy back size-capped; job dir deleted after copy-back.
5. **VRAM etiquette (hard rules):** only ~2.1 GB free (Ollama holds 3.8 GB for the home agent; securehomemonitor CV has the rest — NEVER touch either). Renders run as batch, retry/queue if CUDA OOM, allow Cycles system-RAM spill. If bigger scenes are ever needed, discuss Ollama `keep_alive` coordination with Franco first.

**Checkpoint:** enclosure from Phase 1 → 1920×1080 `studio` render + a 5 s turntable MP4 land back on the Mac, **retrieved via the poll path**. Commit, tag `v0.3`.

## Phase 4 — Experimental (explicitly optional, only if Franco asks)

- Multi-angle ortho grid (front/top/right/iso in one image) as richer Claude feedback — cheap, uses Phase 3 plumbing or Fusion viewport.
- Local text-to-mesh (TripoSR-class) on the 1660: 6 GB shared VRAM makes this a toy — organic shells (Baymax!) to import as reference meshes. Do not let this distract from the CAD bridge.

---

## First prompt for the build session

> Read HANDOFF.md and PLAN.md, then start Phase 0. Fusion 360 is installed; ask me to open it when you need it running.
