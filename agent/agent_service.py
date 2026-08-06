"""NIBBLER chat service — a local agent loop that drives Fusion by prompt.

Architecture
------------
    Fusion palette (webview)  ──HTTP/SSE──▶  this service
                                                 │  Claude Agent SDK
                                                 ▼
                                          claude (headless)
                                                 │  MCP over stdio
                                                 ▼
                                          server/mcp_server.py
                                                 │  HTTP 127.0.0.1:7654 + token
                                                 ▼
                                          FusionBridge add-in ─▶ Fusion

The palette talks to THIS service directly and never through the add-in's
Python.  That is deliberate: the add-in's main thread is what serves bridge
calls, so routing the chat through it would deadlock the moment the agent
called a Fusion tool (palette → add-in → agent → bridge → same main thread).

Everything here is loopback-only.  The one outbound connection is the Claude
Agent SDK's own call to Anthropic, which is the point of the feature.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
from aiohttp import web

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

REPO = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO / "server"

BIND_HOST = "127.0.0.1"
BIND_PORT = int(os.environ.get("NIBBLER_PORT") or 7655)

BRIDGE_URL = "http://127.0.0.1:7654"
TOKEN_PATH = Path("~/.fusion-mcp/token").expanduser()

STATIC = Path(__file__).resolve().parent / "static"

log = logging.getLogger("nibbler")


# --------------------------------------------------------------------------
# Destructive-operation gate
#
# Additive work runs immediately so the chat feels conversational; anything
# that can destroy existing geometry stops and asks.  Fusion's undo does not
# cover everything a script does, so this is the only real safety net.
# --------------------------------------------------------------------------

DESTRUCTIVE_PATTERNS: list[tuple[str, str]] = [
    (r"\.deleteMe\s*\(", "deletes a body, feature, sketch or component"),
    (r"deleteAllAfterMarker", "deletes every timeline feature after the marker"),
    (r"\.markerPosition\s*=", "rolls the timeline back over existing work"),
    (r"\.designType\s*=", "switches parametric/direct mode, which erases the timeline"),
    (r"combineFeatures", "boolean-combines bodies (can consume existing geometry)"),
    (r"CutFeatureOperation|IntersectFeatureOperation",
     "cuts or intersects against existing geometry"),
    (r"\.remove\s*\(|removeAll\s*\(", "removes entities from the design"),
    (r"documents\.\w+\.close\s*\(", "closes a document"),
    (r"\.saveAs\s*\(|\.save\s*\(", "writes over a saved document"),
]


def destructive_reason(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """Return why this call is destructive, or None if it is safe to auto-run."""
    if not tool_name.endswith("fusion_execute"):
        return None
    code = tool_input.get("code") or ""
    for pattern, reason in DESTRUCTIVE_PATTERNS:
        if re.search(pattern, code):
            return reason
    return None


# --------------------------------------------------------------------------
# One chat session
# --------------------------------------------------------------------------

SYSTEM_APPEND = """
You are driving Autodesk Fusion 360 through the `fusion` MCP tools, inside a
panel docked next to the model. The person can see the viewport, so show your
work rather than describing it.

Non-negotiables (these cost real work when broken):
- The Fusion API's length unit is CENTIMETRES regardless of display units:
  20 mm is 2.0. Angles are radians.
- After ANY geometry change, call fusion_screenshot and actually look at it.
  A wrong extrude direction or a profile that grabbed the wrong region raises
  no exception — the screenshot is the only way to catch it.
- Never query faces or edges in the SAME fusion_execute call that created a
  feature: the read succeeds but returns stale topology. Split them.
- Never call ui.messageBox, adsk.doEvents(), or create/execute UI commands —
  a modal dialog deadlocks the bridge. Never write unbounded loops.
- Prefer user parameters over hardcoded numbers so the design stays editable.

Keep replies short. The person is watching geometry appear, not reading prose.
"""


class Session:
    """Wraps one ClaudeSDKClient plus the plumbing to stream it to a browser."""

    def __init__(self) -> None:
        self.client: ClaudeSDKClient | None = None
        self.out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.pending: dict[str, asyncio.Future[bool]] = {}
        self.lock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.start_error: str | None = None

    # -- permission gate ------------------------------------------------- #
    #
    # This is a PreToolUse HOOK, not the can_use_tool callback. The callback
    # only fires when the permission flow "resolves to a prompt", so an
    # allowed_tools entry, an allow rule in settings, or a permission mode of
    # "auto"/"bypassPermissions" all silently skip it — and this machine's
    # user settings do set defaultMode: auto. A hook runs unconditionally,
    # which is the only way to guarantee the gate.

    async def _pre_tool_use(
        self, payload: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input") or {}
        log.debug("PreToolUse: %s", tool_name)
        reason = destructive_reason(tool_name, tool_input)
        if reason is None:
            return {}          # no opinion — normal permission flow continues

        request_id = uuid.uuid4().hex
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self.out.put({
            "type": "permission",
            "id": request_id,
            "reason": reason,
            "code": (tool_input.get("code") or "")[:4000],
        })
        try:
            approved = await future
        except asyncio.CancelledError:
            approved = False
        finally:
            self.pending.pop(request_id, None)

        if approved:
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "approved in the NIBBLER panel",
            }}
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "The person declined this destructive step in the NIBBLER panel. "
                "Do not retry it; propose a non-destructive alternative instead."
            ),
        }}

    def resolve_permission(self, request_id: str, approved: bool) -> bool:
        future = self.pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(approved)
        return True

    # -- lifecycle -------------------------------------------------------- #

    async def start(self) -> None:
        options = ClaudeAgentOptions(
            model="claude-opus-5",
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": SYSTEM_APPEND,
            },
            mcp_servers={
                "fusion": {
                    "type": "stdio",
                    "command": "uv",
                    "args": [
                        "run", "--frozen", "--no-sync",
                        "--directory", str(SERVER_DIR),
                        "mcp_server.py",
                    ],
                }
            },
            # Only the read-only tools are pre-approved. fusion_execute and
            # fusion_export are deliberately NOT listed: an allowed_tools entry
            # auto-approves a call *before* can_use_tool runs, so listing them
            # would silently disable the destructive gate entirely (the SDK
            # warns about exactly this). Leaving them out makes every call fall
            # through to the callback, which auto-allows the additive ones.
            allowed_tools=[
                "mcp__fusion__fusion_screenshot",
                "mcp__fusion__fusion_state",
                "mcp__fusion__fusion_execute",
                "mcp__fusion__fusion_export",
            ],
            # The hook is the gate — it runs before any allow rule or
            # permission mode is consulted, so pre-approving the tools above
            # only removes redundant prompting, it does not weaken the gate.
            hooks={
                "PreToolUse": [
                    HookMatcher(
                        # matcher=None means every tool. A name pattern like
                        # "mcp__fusion__fusion_execute|..." silently does NOT match
                        # MCP tools — verified empirically — and a matcher that
                        # quietly matches nothing is a gate that never fires.
                        matcher=None,
                        hooks=[self._pre_tool_use],
                        timeout=600,      # a person has to read the code and decide
                    )
                ]
            },
            # Load the user's own settings so the fusion-360 knowledge skill,
            # which install.sh links into ~/.claude/skills/, is available.
            setting_sources=["user"],
            env={"MCP_TOOL_TIMEOUT": "180000"},
        )
        try:
            client = ClaudeSDKClient(options=options)
            await client.connect()
        except Exception as exc:                          # noqa: BLE001
            self.start_error = repr(exc)
            log.exception("agent session failed to start")
            await self.out.put({"type": "error",
                                "message": f"agent failed to start: {exc}"})
            return
        self.client = client
        self.ready.set()
        log.info("agent session connected")

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None

    # -- one turn --------------------------------------------------------- #

    async def run_turn(self, prompt: str) -> None:
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=60)
        except asyncio.TimeoutError:
            await self.out.put({"type": "error",
                                "message": self.start_error or "agent did not start in time"})
            await self.out.put({"type": "turn_end"})
            return
        assert self.client is not None
        async with self.lock:
            await self.client.query(prompt)
            try:
                async for message in self.client.receive_response():
                    for event in _render(message):
                        await self.out.put(event)
            except Exception as exc:                      # noqa: BLE001
                log.exception("turn failed")
                await self.out.put({"type": "error", "message": str(exc)})
            await self.out.put({"type": "turn_end"})


def _render(message: Any) -> list[dict[str, Any]]:
    """Translate one SDK message into events the browser understands."""
    events: list[dict[str, Any]] = []
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock):
                if block.text.strip():
                    events.append({"type": "text", "text": block.text})
            elif isinstance(block, ThinkingBlock):
                events.append({"type": "thinking"})
            elif isinstance(block, ToolUseBlock):
                events.append({
                    "type": "tool",
                    "name": block.name.rsplit("__", 1)[-1],
                    "summary": _summarize_tool(block.name, block.input or {}),
                })
    elif isinstance(message, ResultMessage):
        events.append({"type": "result", "text": getattr(message, "result", "") or ""})
    return events


def _summarize_tool(name: str, args: dict[str, Any]) -> str:
    short = name.rsplit("__", 1)[-1]
    if short == "fusion_execute":
        code = (args.get("code") or "").strip().splitlines()
        head = code[0][:70] if code else ""
        return f"{len(code)} lines · {head}"
    if short == "fusion_screenshot":
        return str(args.get("view", "iso"))
    if short == "fusion_export":
        return f"{args.get('format', '')} {args.get('body_or_component', '') or '(whole design)'}"
    return ", ".join(f"{k}={v}" for k, v in list(args.items())[:2])[:80]


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------

def read_token() -> str | None:
    try:
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


async def handle_index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC / "index.html")


async def handle_send(request: web.Request) -> web.Response:
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return web.json_response({"ok": False, "error": "empty prompt"}, status=400)
    session: Session = request.app["session"]
    asyncio.create_task(session.run_turn(prompt))
    return web.json_response({"ok": True})


async def handle_permission(request: web.Request) -> web.Response:
    body = await request.json()
    session: Session = request.app["session"]
    ok = session.resolve_permission(body.get("id", ""), bool(body.get("allow")))
    return web.json_response({"ok": ok})


async def handle_interrupt(request: web.Request) -> web.Response:
    session: Session = request.app["session"]
    if session.client is not None:
        await session.client.interrupt()
    return web.json_response({"ok": True})


async def handle_events(request: web.Request) -> web.StreamResponse:
    """Server-sent events: the browser's single subscription to the session."""
    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )
    await response.prepare(request)
    session: Session = request.app["session"]
    try:
        while True:
            try:
                event = await asyncio.wait_for(session.out.get(), timeout=20)
            except asyncio.TimeoutError:
                await response.write(b": keepalive\n\n")   # keeps proxies/CEF honest
                continue
            payload = json.dumps(event).encode("utf-8")
            await response.write(b"data: " + payload + b"\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return response


async def handle_viewport(request: web.Request) -> web.Response:
    """Current viewport as a PNG, straight from the bridge.

    The chat shows this after every turn so the person sees the model without
    the agent having to spend a tool call on it.
    """
    token = read_token()
    if token is None:
        return web.json_response({"ok": False, "error": "no bridge token"}, status=503)
    view = request.query.get("view", "iso")
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=30.0) as client:
            reply = await client.post(
                f"{BRIDGE_URL}/screenshot",
                headers={"X-Fusion-Bridge-Token": token},
                json={"view": view, "width": 900, "height": 620},
            )
    except httpx.HTTPError as exc:
        return web.json_response({"ok": False, "error": repr(exc)}, status=503)
    if reply.status_code != 200:
        return web.json_response({"ok": False, "error": reply.text[:200]},
                                 status=reply.status_code)
    payload = reply.json()
    if not payload.get("ok"):
        return web.json_response({"ok": False, "error": payload.get("error")}, status=502)
    return web.Response(body=base64.b64decode(payload["png_base64"]),
                        content_type="image/png")


async def handle_health(request: web.Request) -> web.Response:
    session: Session = request.app["session"]
    return web.json_response({
        "ok": True,
        "agent": session.ready.is_set(),
        "agent_error": session.start_error,
        "bridge_token": read_token() is not None,
    })


# --------------------------------------------------------------------------

async def on_startup(app: web.Application) -> None:
    session = Session()
    app["session"] = session
    # Connect in the background so the port binds immediately — otherwise the
    # palette shows a blank page while the agent is still coming up, and a
    # hung connect() would mean the service never serves at all.
    app["starter"] = asyncio.create_task(session.start())


async def on_cleanup(app: web.Application) -> None:
    session: Session | None = app.get("session")
    if session is not None:
        await session.stop()


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/events", handle_events)
    app.router.add_get("/viewport", handle_viewport)
    app.router.add_post("/send", handle_send)
    app.router.add_post("/permission", handle_permission)
    app.router.add_post("/interrupt", handle_interrupt)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="NIBBLER chat service")
    parser.add_argument("--port", type=int, default=BIND_PORT)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        stream=sys.stderr,
    )
    if read_token() is None:
        log.warning("no bridge token at %s — run scripts/install.sh", TOKEN_PATH)

    web.run_app(build_app(), host=BIND_HOST, port=args.port, print=None)
    # Loopback only, deliberately: this service can run arbitrary Fusion code.


if __name__ == "__main__":
    main()
