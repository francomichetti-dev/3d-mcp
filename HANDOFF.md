# HANDOFF — fusion-mcp

Start here. This folder is the dedicated workspace for building an **MCP bridge that lets Claude create 3D objects in Fusion 360 by prompting** — Claude writes Fusion API Python, sees the result via screenshots, iterates, and exports print-ready files. `PLAN.md` (same folder) is the step-by-step build plan.

## Non-negotiable directives (from Franco, 2026-08-04)

1. **Build it ourselves.** Anything that could be insecure — above all the bridge, which is an arbitrary-code-execution channel into Fusion — is written from scratch in this repo. Existing open-source projects are **design references only** (read their ideas, never install or vendor their code). Nothing third-party executes inside Fusion.
2. **100% local.** MCP over stdio; the add-in's HTTP listener binds to **127.0.0.1 only** (never 0.0.0.0); random per-install token (file mode 0600); zero external calls, zero telemetry from our code. The GPU render stage talks only to the NAS over LAN/SSH, and its Blender presets must not download assets (no external HDRIs). (Fusion itself is Autodesk cloud-connected software — we can't change that — but our tooling must add **zero** new network surface.)

## Architecture (the proven Blender-MCP pattern)

```
Claude Code ←stdio/MCP→ server/ (Python, FastMCP)
                            ↓ HTTP 127.0.0.1:7654 + token
                        addin/ (Fusion 360 add-in, Python)
                            ↓ CustomEvent marshal → main thread
                        Fusion 360 API (adsk.core / adsk.fusion)
```

Why this shape: Fusion has **no external API** — `adsk.*` only runs inside Fusion as an add-in, and its API is main-thread-only, so the add-in's HTTP thread must marshal every request via `app.registerCustomEvent` / `fireCustomEvent`. Few powerful tools beat many shallow ones: `fusion_execute` (arbitrary `adsk` Python) + `fusion_screenshot` (viewport PNG) gives Claude full capability **and eyes** — the design→look→correct loop is what makes prompting work.

Design references (read-only): [ndoo/fusion360-mcp-bridge](https://github.com/ndoo/fusion360-mcp-bridge) (closest to our design: execute+screenshot, CustomEvent marshaling, token auth, macOS quickstart), [rahayesj/ClaudeFusion360MCP](https://github.com/rahayesj/ClaudeFusion360MCP) (their finding: the Fusion-API *knowledge files* mattered more than the bridge code — hence our Phase 2).

## GPU question — verified answer (nvidia-smi on NAS, 2026-08-04)

Franco asked whether the NAS's **GTX 1660 Super** can help even though Fusion runs on the Mac.

- **Fusion's own in-app rendering cannot offload to a remote GPU. Period.** Anything "render on NAS" means exporting the model and rendering it elsewhere.
- **What the 1660 CAN do (Phase 3 — a committed feature, Franco wants the cool renders):** headless **Blender Cycles CUDA** renders in a container — photoreal product shots and turntable MP4s of exported designs, plus multi-angle orthographic preview grids as extra feedback for Claude. Driver 550.142, CUDA 12.4 confirmed on the host.
- **VRAM reality check:** 4.0 / 6.1 GB is already taken — Ollama `llama-server` holds **3.8 GB resident** (home agent's LLM, `ix-ollama` container) and `python main.py` (~212 MB) is the **securehomemonitor CV process — NEVER touch it** (it's the home security camera monitor). So ~2.1 GB is free: enough for simple/medium Cycles scenes (Cycles can also spill to system RAM, slower). For heavy scenes, coordinate with the home agent's Ollama (model unload/keep_alive) rather than evicting anything by force.
- Slicing (PrusaSlicer CLI) is CPU-only — no GPU relevance.
- AI text-to-mesh (TripoSR etc.) on 6 GB shared VRAM: experimental toy at best, Phase 4, optional.

## Operational facts

- **Mac-local tool**: Fusion's GUI must be open for the bridge to work; the MCP server runs on the Mac (register in Claude Code user scope). Not an agentvm thing.
- Fusion 360 is installed on this Mac (and is hereby OFF the storage-deletion candidate list). macOS add-ins live at `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/`.
- NAS access for Phase 3: ssh alias `nas` (LAN, needs Twingate settled or home Wi-Fi) or `truenas` (Tailscale, needs Tailscale running). If publickey auth fails: `/usr/bin/ssh-add --apple-load-keychain` (Apple binary, not Homebrew).
- Repo: create git in this folder, push private under `francomichetti-dev` (gh CLI auths as `onembyte`).
- **Fusion API gotcha to burn into the `fusion_execute` tool description AND the knowledge file**: the API's internal length unit is **cm** (not mm) regardless of document display units.

## Hard safety rules

- Bridge listener: 127.0.0.1 only, token required on every request (fail closed — no token file, no listener). Per-request timeout that **abandons the wait** — main-thread execution cannot be cancelled, so generated code must avoid unbounded loops and chunk big operations. stdout/result truncated at ~64 KB with explicit markers; screenshot dimensions clamped in the add-in; HTTP request body capped ~5 MB.
- Logs never contain the token; `~/.fusion-mcp` is 0700, its files 0600.
- NAS jobs: SSH with BatchMode + strict host-key checking, per-job dirs, cleanup after copy-back.
- `fusion_execute` runs whatever Claude sends — that's the point — but ONLY reachable from localhost with the token. Never add remote/LAN access "for convenience".
- Phase 3 NAS jobs: never kill/restart `ix-ollama` or the securehomemonitor processes; renders are batch jobs that wait their turn.
- Advise Franco to work in a scratch Fusion project while iterating — generated code can mangle a design (Fusion's timeline/undo is the safety net, but still).

## Related memory files

`project_fusion_mcp.md` (this project), `project_truenas_docker_state.md` (GPU/containers), `project_home_agent.md` (the Ollama VRAM tenant), `project_storage_management.md`.
