"""Fusion Chat service — a local agent loop that drives Fusion by prompt.

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
import contextlib
import hashlib
import json
import logging
import os
import re
import sys
import time
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
BIND_PORT = int(os.environ.get("FUSION_CHAT_PORT") or 7655)

BRIDGE_URL = "http://127.0.0.1:7654"
TOKEN_PATH = Path("~/.fusion-mcp/token").expanduser()

STATIC = Path(__file__).resolve().parent / "static"

# One chat per design, persisted so reopening a design tomorrow reopens its
# conversation. Only a pointer to the SDK's own session plus what the panel
# needs to redraw — the model's real context lives in the SDK session store.
CHATS_PATH = Path("~/.fusion-mcp/chats.json").expanduser()

# Attached images are kept on disk, not in chats.json: the transcript stores a
# reference so the panel can redraw a conversation, while the bytes themselves
# stay out of a file that is read and rewritten constantly.
ATTACH_DIR = Path("~/.fusion-mcp/attachments").expanduser()
ALLOWED_IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
# Anthropic rejects images over 5 MB. The panel downscales before uploading, so
# reaching this means something genuinely oversized arrived.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES_PER_MESSAGE = 8
# id shape: <16 hex of the design key>/<32 hex>.<ext> — matched exactly when
# serving, so a crafted id cannot walk out of the attachments directory.
ATTACHMENT_ID = re.compile(r"^[0-9a-f]{16}/[0-9a-f]{32}\.(?:png|jpg|gif|webp)$")

DOC_POLL_SECONDS = 1.0
# Per-viewer backlog before the oldest events are dropped. A panel that stops
# reading must not be able to stall a turn or grow memory without bound.
EVENT_QUEUE_LIMIT = 1000
# Upper bound on remembered designs, evicted least-recently-touched first.
MAX_DESIGNS = 50
# Enough to redraw a conversation without letting the file grow forever.
MAX_TRANSCRIPT_EVENTS = 400
# Events worth replaying when the panel switches back to a design. Excludes
# turn_end, thinking and permission prompts, which only mean something live.
PERSISTED_EVENTS = frozenset(("user", "text", "tool", "error", "notice"))

log = logging.getLogger("fusion-chat")


# --------------------------------------------------------------------------
# Destructive-operation gate
#
# Additive work runs immediately so the chat feels conversational; anything
# that can destroy existing geometry stops and asks.  Fusion's undo does not
# cover everything a script does, so this is the only real safety net.
# --------------------------------------------------------------------------

DESTRUCTIVE_PATTERNS: list[tuple[str, str]] = [
    # Deliberately any .close(, not documents.<something>.close(: the two forms
    # that actually get written — app.activeDocument.close(False) and
    # app.documents.item(0).close(False) — both slipped through the narrower
    # pattern, and closing a document throws away everything unsaved in it.
    # A false positive here costs one approval click; a miss costs the design.
    (r"\.close\s*\(", "closes a document, discarding anything unsaved in it"),
    (r"\.saveAs\s*\(|\.save\s*\(", "writes over a saved document"),
]

# Deliberately NOT gated, at the owner's request: deleteMe, remove/removeAll,
# deleteAllAfterMarker, markerPosition, designType, combineFeatures and
# Cut/Intersect operations. Modelling is subtractive — cuts and combines fire
# constantly in ordinary work — and every one of these is recoverable through
# the timeline or undo. The two above are not recoverable by any means, which
# is the whole reason they stay.
#
# To gate deletes again, move the patterns back into the list above:
#   (r"\.deleteMe\s*\(",        "deletes a body, feature, sketch or component"),
#   (r"deleteAllAfterMarker",   "deletes every timeline feature after the marker"),
#   (r"\.markerPosition\s*=",   "rolls the timeline back over existing work"),
#   (r"\.designType\s*=",       "switches parametric/direct mode, erasing the timeline"),
#   (r"combineFeatures",        "boolean-combines bodies"),
#   (r"CutFeatureOperation|IntersectFeatureOperation", "cuts against existing geometry"),
#   (r"\.remove\s*\(|removeAll\s*\(", "removes entities from the design"),


def design_folder(key: str) -> Path:
    """Attachments live under a hash of the design key.

    The key itself is unusable as a path component: a saved design's key is a
    URN full of colons, and an attacker-shaped key must not be able to steer
    where bytes land.
    """
    return ATTACH_DIR / hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def save_attachment(key: str, name: str, media_type: str, raw: bytes) -> dict[str, Any]:
    """Persist one image and return the reference the transcript keeps."""
    folder = design_folder(key)
    # mkdir(parents=True) applies `mode` only to the leaf, so the intermediate
    # attachments/ directory would be left at the process umask.
    ATTACH_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ATTACH_DIR, 0o700)
    folder.mkdir(mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    filename = uuid.uuid4().hex + ALLOWED_IMAGE_TYPES[media_type]
    path = folder / filename
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    return {
        "id": f"{folder.name}/{filename}",
        "name": name[:120] or filename,
        "media_type": media_type,
    }


def forget_attachments(key: str) -> None:
    folder = design_folder(key)
    try:
        for child in folder.iterdir():
            child.unlink()
        folder.rmdir()
    except OSError:
        pass            # never existed, or already gone


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
# Persistence
#
# What survives a restart is a pointer, not a conversation: the SDK keeps the
# real context in its own session store, and ClaudeAgentOptions.resume reopens
# it. Here we keep only what the panel needs to redraw, plus the compressed
# core context a closed design leaves behind.
# --------------------------------------------------------------------------


class Store:
    """chats.json, kept at 0600 and written atomically."""

    def __init__(self, path: Path = CHATS_PATH) -> None:
        self.path = path
        self.data: dict[str, Any] = {"version": 1, "designs": {}}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return                      # absent or corrupt: start clean
        if isinstance(raw, dict) and isinstance(raw.get("designs"), dict):
            self.data = raw

    def prune(self) -> None:
        """Keep the newest MAX_DESIGNS designs.

        An unsaved document that is closed and discarded takes its stamped key
        with it, so its entry can never be reached again. Rather than guess at
        close time whether a document is being thrown away — the user may still
        answer "save" to Fusion's prompt, which keeps the key valid — the file
        is simply bounded by least-recently-touched.
        """
        designs = self.data["designs"]
        excess = len(designs) - MAX_DESIGNS
        if excess <= 0:
            return
        oldest = sorted(designs.items(), key=lambda kv: kv[1].get("updated") or 0)
        for key, _ in oldest[:excess]:
            designs.pop(key, None)
            # Otherwise the images outlive the only record that referenced them
            # and nothing would ever delete them.
            forget_attachments(key)
        log.info("pruned %d old design chat(s)", excess)

    def save(self) -> None:
        self.prune()
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            os.chmod(tmp, 0o600)
            # Atomic: a crash mid-write must not leave a truncated file that
            # would silently lose every design's chat on the next start.
            os.replace(tmp, self.path)
        except OSError:
            log.exception("could not write %s", self.path)

    def entry(self, key: str) -> dict[str, Any]:
        return self.data["designs"].setdefault(key, {})

    def get(self, key: str) -> dict[str, Any]:
        return self.data["designs"].get(key, {})

    def update(self, key: str, **fields: Any) -> None:
        entry = self.entry(key)
        entry.update(fields)
        entry["updated"] = time.time()
        self.save()

    def forget(self, key: str) -> None:
        self.data["designs"].pop(key, None)
        forget_attachments(key)
        self.save()


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

This conversation belongs to ONE design. Everything you built here is in that
design, and the person may have other designs open in other tabs with their own
separate chats — never assume work you did elsewhere exists here.
"""

CORE_CONTEXT_PREAMBLE = """
This design was closed and reopened, so the earlier conversation is gone. What
survived is the core context below. Treat it as established fact about the
design, and verify against fusion_state rather than trusting it blindly.

--- core context ---
%s
--- end core context ---
"""


class Session:
    """One design's chat: a ClaudeSDKClient plus what the panel needs to redraw.

    Sessions are per-document and long-lived. They push into a queue owned by
    the registry rather than one of their own, because the panel holds a single
    SSE stream and every event is tagged with the design it came from.
    """

    def __init__(self, key: str, name: str | None, registry: Registry) -> None:
        self.key = key
        self.name = name
        self.registry = registry
        self.client: ClaudeSDKClient | None = None
        self.pending: dict[str, asyncio.Future[bool]] = {}
        self.lock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.start_error: str | None = None
        self.busy = False
        # Captured from the SDK's own messages so the conversation can be
        # resumed after a restart.
        self.sdk_session_id: str | None = None

    async def emit(self, event: dict[str, Any]) -> None:
        """Tag an event with this design and record it for later replay."""
        event = dict(event, doc=self.key)
        if event["type"] in PERSISTED_EVENTS:
            self.registry.append_transcript(self.key, event)
        self.registry.publish(event)

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
        await self.emit({
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
                "permissionDecisionReason": "approved in the Fusion Chat panel",
            }}
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "The person declined this destructive step in the Fusion Chat panel. "
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

    def _options(self, resume: str | None) -> ClaudeAgentOptions:
        stored = self.registry.store.get(self.key)
        append = SYSTEM_APPEND
        core = stored.get("core_context")
        if core:
            append = append + CORE_CONTEXT_PREAMBLE % core
        return ClaudeAgentOptions(
            model="claude-opus-5",
            # Pinned rather than inherited: the SDK keys its session store by
            # working directory, so a resume only finds the conversation again
            # if this is the same every time the service starts.
            cwd=str(REPO),
            resume=resume,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": append,
            },
            mcp_servers={
                "fusion": {
                    "type": "stdio",
                    "command": "uv",
                    "args": [
                        "run", "--frozen", "--no-sync",
                        "--directory", str(SERVER_DIR),
                        "fusion-3d-mcp",
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
    async def start(self) -> None:
        resume = self.registry.store.get(self.key).get("session_id")
        for attempt in (resume, None):
            try:
                client = ClaudeSDKClient(options=self._options(attempt))
                await client.connect()
            except Exception as exc:                      # noqa: BLE001
                if attempt is not None:
                    # The stored session is gone or unreadable — expected after
                    # the SDK's own history is cleared. Start fresh rather than
                    # leaving this design permanently unable to chat.
                    log.warning("resume of %s failed (%s); starting fresh", self.key, exc)
                    self.registry.store.update(self.key, session_id=None)
                    continue
                self.start_error = repr(exc)
                log.exception("agent session failed to start for %s", self.key)
                await self.emit({"type": "error",
                                 "message": f"agent failed to start: {exc}"})
                return
            self.client = client
            self.ready.set()
            log.info("agent session connected for %s (resumed=%s)",
                     self.key, bool(attempt))
            return

    async def stop(self) -> None:
        self.ready.clear()
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:                             # noqa: BLE001
                log.exception("disconnect failed for %s", self.key)
            self.client = None

    async def interrupt(self) -> None:
        if self.client is not None and self.busy:
            try:
                await self.client.interrupt()
            except Exception:                             # noqa: BLE001
                log.exception("interrupt failed for %s", self.key)

    # -- one turn --------------------------------------------------------- #

    async def _send(self, prompt: str, images: list[dict[str, Any]]) -> None:
        """Hand the message to the SDK, with images inline when there are any.

        Verified against the SDK: a message dict yielded from an async iterable
        is written to the CLI verbatim, so standard Anthropic image blocks reach
        the model directly — no tool call, and the images land in the session
        itself, which is what lets a resumed conversation still see them.
        """
        assert self.client is not None
        if not images:
            await self.client.query(prompt)
            return

        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image["media_type"],
                    "data": image["b64"],
                },
            }
            for image in images
        ]
        if prompt:
            content.append({"type": "text", "text": prompt})

        async def stream():
            yield {
                "type": "user",
                "message": {"role": "user", "content": content},
                "parent_tool_use_id": None,
            }

        await self.client.query(stream())

    async def run_turn(self, prompt: str,
                       images: list[dict[str, Any]] | None = None) -> None:
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=60)
        except asyncio.TimeoutError:
            await self.emit({"type": "error",
                             "message": self.start_error or "agent did not start in time"})
            self.registry.publish({"type": "turn_end", "doc": self.key})
            return
        assert self.client is not None
        async with self.lock:
            self.busy = True
            # Fence the bridge to this design for the whole turn. The document
            # watcher also interrupts on a tab switch, but it only polls once a
            # second; this refuses a tool call that is already in flight.
            await self.registry.pin(self.key)
            try:
                await self._send(prompt, images or [])
                async for message in self.client.receive_response():
                    self._capture_session_id(message)
                    for event in _render(message):
                        await self.emit(event)
            except Exception as exc:                      # noqa: BLE001
                log.exception("turn failed for %s", self.key)
                await self.emit({"type": "error", "message": str(exc)})
            finally:
                self.busy = False
                await self.registry.pin(None)
                self.registry.publish({"type": "turn_end", "doc": self.key})

    def _capture_session_id(self, message: Any) -> None:
        session_id = getattr(message, "session_id", None)
        if session_id and session_id != self.sdk_session_id:
            self.sdk_session_id = session_id
            self.registry.store.update(
                self.key, session_id=session_id, name=self.name)


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
# The registry: one session per design, and the document watcher
# --------------------------------------------------------------------------

COMPRESS_PROMPT = """This design has just been closed in Fusion, so this
conversation is over. Write the CORE CONTEXT a future conversation about this
same design would need, and nothing else.

Keep only what is expensive or impossible to rediscover by looking at the design:
- what it is for, and the intent behind it
- key dimensions and user parameters, and why they are what they are
- names of bodies, components and sketches that carry meaning
- decisions the person made, constraints they stated, approaches they rejected
- anything unfinished or deliberately left for later

Drop entirely: tool mechanics, code, tracebacks, retries, apologies, exploration
that went nowhere, and anything a fusion_state call would reveal anyway.

Under 250 words, terse bullets. No preamble and no sign-off — output the core
context only."""


class Registry:
    """Every design's chat, plus the watcher that follows Fusion's active tab."""

    def __init__(self) -> None:
        self.store = Store()
        self.sessions: dict[str, Session] = {}
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.current: dict[str, Any] | None = None
        self.bridge_ok: bool | None = None
        self.http = httpx.AsyncClient(trust_env=False, timeout=10.0)
        self._dirty = False
        self._compressing: set[str] = set()

    # -- event fan-out ----------------------------------------------------- #
    #
    # One queue per subscriber, not one shared queue. asyncio.Queue hands each
    # item to exactly ONE getter, so a shared queue silently splits the stream
    # between viewers: with the palette open in Fusion and the same page open
    # anywhere else, each would receive roughly half the conversation. A palette
    # reload that leaves the old connection briefly alive does the same thing.

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=EVENT_QUEUE_LIMIT)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.subscribers.discard(queue)

    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A viewer that stopped reading must not stall the turn or grow
                # without bound. Drop its oldest event and keep the newest —
                # a stalled panel is better off current than complete.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

    # -- bridge ----------------------------------------------------------- #

    async def _bridge(self, method: str, path: str, **kw: Any) -> dict[str, Any] | None:
        token = read_token()
        if token is None:
            return None
        try:
            reply = await self.http.request(
                method, f"{BRIDGE_URL}{path}",
                headers={"X-Fusion-Bridge-Token": token}, **kw)
        except httpx.HTTPError:
            return None
        if reply.status_code != 200:
            return None
        try:
            return reply.json()
        except ValueError:
            return None

    async def pin(self, key: str | None) -> None:
        await self._bridge("POST", "/pin", json={"key": key})

    async def resolve_active(self) -> dict[str, Any] | None:
        """The active design, stamping an identity if it does not have one yet.

        Only reached when the person actually sends a message: merely looking at
        a design must never mark it modified.
        """
        payload = await self._bridge("POST", "/document", json={})
        return (payload or {}).get("active")

    # -- transcripts ------------------------------------------------------- #

    def append_transcript(self, key: str, event: dict[str, Any]) -> None:
        transcript = self.store.entry(key).setdefault("transcript", [])
        transcript.append(event)
        del transcript[:-MAX_TRANSCRIPT_EVENTS]
        # Flushed by the watcher rather than here: a busy turn emits dozens of
        # events and a file write per event would be pointless churn.
        self._dirty = True

    def flush(self) -> None:
        if self._dirty:
            self._dirty = False
            self.store.save()

    def snapshot(self, key: str | None) -> dict[str, Any]:
        entry = self.store.get(key) if key else {}
        session = self.sessions.get(key) if key else None
        return {
            "transcript": entry.get("transcript", []),
            "core_context": entry.get("core_context"),
            "busy": bool(session and session.busy),
        }

    # -- sessions ---------------------------------------------------------- #

    def session_for(self, key: str, name: str | None) -> Session:
        session = self.sessions.get(key)
        if session is None:
            session = Session(key, name, self)
            self.sessions[key] = session
            self.store.update(key, name=name)
            asyncio.create_task(session.start())
            log.info("new chat session for %s (%s)", key, name)
        elif name and session.name != name:
            session.name = name
            self.store.update(key, name=name)
        return session

    # -- the watcher ------------------------------------------------------- #

    async def watch(self) -> None:
        """Follow Fusion's active document and react to closes.

        Polls the bridge's cached /document, which never touches Fusion's main
        thread, so this cannot queue behind a long modelling job or slow one
        down.
        """
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:                             # noqa: BLE001
                log.exception("document watcher tick failed")
            await asyncio.sleep(DOC_POLL_SECONDS)

    async def _tick(self) -> None:
        payload = await self._bridge("GET", "/document")
        self.flush()
        if payload is None:
            if self.bridge_ok is not False:
                self.bridge_ok = False
                self.publish({"type": "bridge", "ok": False})
            return
        if self.bridge_ok is not True:
            self.bridge_ok = True
            self.publish({"type": "bridge", "ok": True})

        for key in payload.get("closed") or []:
            asyncio.create_task(self.compress(key))

        await self.set_active(payload.get("active"))

    async def set_active(self, active: dict[str, Any] | None) -> bool:
        """Adopt `active` as the design on screen, announcing a real change.

        Shared with /send so that stamping an identity on first message emits
        the switch *before* the message itself. Letting the watcher discover it
        a beat later would redraw the panel and wipe the message just sent.
        """
        if not self._changed(active):
            return False
        previous = self.current
        self.current = active
        await self._on_switch(previous, active)
        return True

    def _changed(self, active: dict[str, Any] | None) -> bool:
        def shape(d: dict[str, Any] | None) -> tuple:
            d = d or {}
            return (d.get("key"), d.get("name"), d.get("saved"), d.get("design"))
        return shape(active) != shape(self.current)

    async def _on_switch(self, previous: dict[str, Any] | None,
                         active: dict[str, Any] | None) -> None:
        previous_key = (previous or {}).get("key")
        session = self.sessions.get(previous_key) if previous_key else None
        if session is not None and session.busy:
            # fusion_execute always acts on whatever document is active, so a
            # turn that outlived the switch would edit the design just moved to.
            # The bridge refuses it too; this is what makes it visible.
            await session.interrupt()
            await session.emit({
                "type": "notice",
                "text": "Stopped — you switched to another design while this was running.",
            })
            self.publish({"type": "turn_end", "doc": previous_key})

        key = (active or {}).get("key")
        self.publish({
            "type": "document",
            "key": key,
            "name": (active or {}).get("name"),
            "saved": bool((active or {}).get("saved")),
            "design": bool((active or {}).get("design")),
            **self.snapshot(key),
        })

    # -- compression on close ---------------------------------------------- #

    async def compress(self, key: str) -> None:
        """Boil a closed design's conversation down to core context.

        The full session is discarded afterwards: reopening the design starts a
        fresh conversation seeded with the summary, which keeps chats.json and
        the model's context small no matter how long a design has been worked on.
        """
        if key in self._compressing:
            return
        self._compressing.add(key)
        try:
            session = self.sessions.pop(key, None)
            if session is not None:
                await session.interrupt()
                await session.stop()

            session_id = self.store.get(key).get("session_id")
            if not session_id:
                return                  # never chatted about, or already compressed

            summary = await self._summarize(session_id)
            if not summary:
                log.warning("compression produced nothing for %s; keeping the session", key)
                return
            self.store.update(
                key,
                core_context=summary,
                session_id=None,
                transcript=[{
                    "type": "notice", "doc": key,
                    "text": "Design closed — this conversation was compressed to core context.",
                }],
            )
            # The transcript that referenced them is gone, and whatever mattered
            # about them is in the summary now.
            forget_attachments(key)
            log.info("compressed chat for %s (%d chars)", key, len(summary))
        except Exception:                                 # noqa: BLE001
            log.exception("compression failed for %s", key)
        finally:
            self._compressing.discard(key)

    async def _summarize(self, session_id: str) -> str:
        """One forked turn over the closed conversation, with no Fusion tools.

        Forked so the original session is never mutated, and given no MCP
        servers at all — the design is gone, so any tool call would act on
        whatever document happens to be open now.
        """
        options = ClaudeAgentOptions(
            model="claude-opus-5",
            cwd=str(REPO),
            resume=session_id,
            fork_session=True,
            max_turns=1,
            setting_sources=[],
        )
        parts: list[str] = []
        client = ClaudeSDKClient(options=options)
        await client.connect()
        try:
            await client.query(COMPRESS_PROMPT)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
        finally:
            await client.disconnect()
        return "\n".join(p.strip() for p in parts if p.strip()).strip()

    async def close(self) -> None:
        for session in list(self.sessions.values()):
            await session.stop()
        self.sessions.clear()
        self.flush()
        await self.http.aclose()


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


def _decode_images(raw_images: Any) -> tuple[list[dict[str, Any]], str | None]:
    """Validate what the panel uploaded. Returns (images, error)."""
    if not raw_images:
        return [], None
    if not isinstance(raw_images, list):
        return [], "attachments must be a list"
    if len(raw_images) > MAX_IMAGES_PER_MESSAGE:
        return [], f"at most {MAX_IMAGES_PER_MESSAGE} images per message"

    images: list[dict[str, Any]] = []
    for item in raw_images:
        if not isinstance(item, dict):
            return [], "malformed attachment"
        media_type = item.get("media_type")
        if media_type not in ALLOWED_IMAGE_TYPES:
            return [], f"unsupported image type {media_type!r}"
        try:
            # validate=True so stray characters are rejected rather than
            # silently skipped into a corrupt image the model cannot read.
            raw = base64.b64decode(item.get("data") or "", validate=True)
        except (ValueError, TypeError):
            return [], "attachment was not valid base64"
        if not raw:
            return [], "empty attachment"
        if len(raw) > MAX_IMAGE_BYTES:
            return [], (f"{item.get('name') or 'image'} is "
                        f"{len(raw) // (1024 * 1024)} MB — the limit is "
                        f"{MAX_IMAGE_BYTES // (1024 * 1024)} MB")
        images.append({"name": str(item.get("name") or "image"),
                       "media_type": media_type, "raw": raw})
    return images, None


async def handle_send(request: web.Request) -> web.Response:
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    images, error = _decode_images(body.get("images"))
    if error is not None:
        return web.json_response({"ok": False, "error": error}, status=400)
    # An image on its own is a perfectly good message — "make this" with a
    # reference photo needs no prose.
    if not prompt and not images:
        return web.json_response({"ok": False, "error": "empty prompt"}, status=400)
    registry: Registry = request.app["registry"]

    # Resolved per message rather than trusted from the watcher, so the turn is
    # always aimed at the design that is active right now — and this is where an
    # unsaved design gets its identity stamped.
    active = await registry.resolve_active()
    key = (active or {}).get("key")
    if not key:
        # Distinguish "Fusion is not there" from "Fusion is there but empty".
        # Both used to say "open or create a design", which reads as nonsense
        # when Fusion is not even running — the first thing a new user hits.
        if registry.bridge_ok is False:
            error = ("Fusion is not reachable — open Fusion, then "
                     "Utilities → Add-Ins → FusionBridge → Run.")
        else:
            error = "No design is open in Fusion — open or create one first."
        return web.json_response({"ok": False, "error": error}, status=409)

    await registry.set_active(active)
    session = registry.session_for(key, active.get("name"))

    # Saved before the turn so the transcript can redraw them later; only the
    # reference goes into chats.json, never the bytes.
    stored = [save_attachment(key, i["name"], i["media_type"], i["raw"]) for i in images]
    for image, ref in zip(images, stored):
        image["b64"] = base64.b64encode(image.pop("raw")).decode("ascii")
        image["id"] = ref["id"]

    event: dict[str, Any] = {"type": "user", "text": prompt}
    if stored:
        event["images"] = [{"id": r["id"], "name": r["name"]} for r in stored]
    await session.emit(event)
    asyncio.create_task(session.run_turn(prompt, images))
    return web.json_response({"ok": True, "doc": key, "images": len(stored)})


async def handle_attachment(request: web.Request) -> web.StreamResponse:
    """Serve a stored attachment back to the panel."""
    attachment_id = request.match_info["folder"] + "/" + request.match_info["name"]
    # Matched against an exact shape rather than sanitised: the id is generated
    # here and never user-supplied, so anything that does not match is hostile.
    if not ATTACHMENT_ID.match(attachment_id):
        return web.Response(status=404)
    path = ATTACH_DIR / attachment_id
    if not path.is_file():
        return web.Response(status=404)
    return web.FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


async def handle_documents(request: web.Request) -> web.Response:
    """What the panel needs on load: the active design and its transcript."""
    registry: Registry = request.app["registry"]
    active = registry.current or {}
    key = active.get("key")
    return web.json_response({
        "ok": True,
        "key": key,
        "name": active.get("name"),
        "saved": bool(active.get("saved")),
        "design": bool(active.get("design")),
        **registry.snapshot(key),
    })


async def handle_permission(request: web.Request) -> web.Response:
    body = await request.json()
    registry: Registry = request.app["registry"]
    request_id = body.get("id", "")
    approved = bool(body.get("allow"))
    for session in registry.sessions.values():
        if session.resolve_permission(request_id, approved):
            return web.json_response({"ok": True})
    return web.json_response({"ok": False})


async def handle_interrupt(request: web.Request) -> web.Response:
    registry: Registry = request.app["registry"]
    key = (registry.current or {}).get("key")
    session = registry.sessions.get(key) if key else None
    if session is not None:
        await session.interrupt()
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
    registry: Registry = request.app["registry"]
    queue = registry.subscribe()
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20)
            except asyncio.TimeoutError:
                await response.write(b": keepalive\n\n")   # keeps proxies/CEF honest
                continue
            payload = json.dumps(event).encode("utf-8")
            await response.write(b"data: " + payload + b"\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        registry.unsubscribe(queue)
    return response


async def handle_viewport(request: web.Request) -> web.Response:
    """Current viewport as a PNG, straight from the bridge.

    Requested by the panel's Screenshot button. Deliberately not fired at the
    end of a turn: the panel is docked beside the viewport, so repeating the
    finished result as an image only pushes the conversation off screen.
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
    registry: Registry = request.app["registry"]
    key = (registry.current or {}).get("key")
    session = registry.sessions.get(key) if key else None
    # Sessions are created on a design's first message, so "agent" means "ready
    # to accept one", not "a client is connected". A design nobody has chatted
    # with yet has no session and needs none — reporting that as not-ready would
    # leave --status saying "still connecting" forever.
    return web.json_response({
        "ok": True,
        "agent": session.ready.is_set() if session is not None else True,
        "agent_error": session.start_error if session is not None else None,
        "bridge_token": read_token() is not None,
        "document": (registry.current or {}).get("name"),
        "session": session is not None,
        "designs": len(registry.sessions),
    })


# --------------------------------------------------------------------------

async def on_startup(app: web.Application) -> None:
    registry = Registry()
    app["registry"] = registry
    # Watch in the background so the port binds immediately — otherwise the
    # palette shows a blank page while the first poll is in flight, and a slow
    # bridge would mean the service never serves at all.
    app["watcher"] = asyncio.create_task(registry.watch())


async def on_cleanup(app: web.Application) -> None:
    watcher: asyncio.Task | None = app.get("watcher")
    if watcher is not None:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
    registry: Registry | None = app.get("registry")
    if registry is not None:
        await registry.close()


def allowed_hosts(port: int) -> frozenset[str]:
    return frozenset((f"127.0.0.1:{port}", f"localhost:{port}"))


@web.middleware
async def pin_host(request: web.Request, handler):
    """Refuse any request whose Host is not our own loopback address.

    This service is a confused deputy without it. It holds the bridge token and
    forwards to the bridge, so an unauthenticated caller reaching *here* gets
    authenticated arbitrary code execution inside Fusion for free.

    Loopback binding alone does not stop a browser. A page on some site can
    have its DNS rebound to 127.0.0.1 and then POST here, and two measured
    details make that a real request rather than a theoretical one:

      * aiohttp's request.json() ignores Content-Type entirely — a body sent as
        text/plain parses exactly the same as application/json. Verified
        against this version of aiohttp, all three content types parsed.
      * text/plain and form-encoding are CORS "simple request" types, so the
        browser sends no preflight. The attacker cannot read the reply, but by
        then the geometry has already been changed.

    The Fusion listener and the Rhino broker have pinned Host since they were
    written; this service is the one that did not, and it is the one that can
    drive Fusion without a token.
    """
    if request.headers.get("Host", "") not in request.app["allowed_hosts"]:
        return web.json_response(
            {"ok": False, "error": "invalid Host header"}, status=403)
    return await handler(request)


def build_app(port: int = BIND_PORT) -> web.Application:
    app = web.Application(middlewares=[pin_host])
    app["allowed_hosts"] = allowed_hosts(port)
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/events", handle_events)
    app.router.add_get("/viewport", handle_viewport)
    app.router.add_get("/document", handle_documents)
    app.router.add_get("/attachment/{folder}/{name}", handle_attachment)
    app.router.add_post("/send", handle_send)
    app.router.add_post("/permission", handle_permission)
    app.router.add_post("/interrupt", handle_interrupt)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Fusion Chat service")
    parser.add_argument("--port", type=int, default=BIND_PORT)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        stream=sys.stderr,
    )
    # The document watcher polls the bridge once a second for as long as the
    # panel is open. At INFO that is a line per second — around 86k lines a day
    # of "GET /document 200", which would bury everything worth reading.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    if read_token() is None:
        log.warning("no bridge token at %s — run scripts/install.sh", TOKEN_PATH)

    web.run_app(build_app(args.port), host=BIND_HOST, port=args.port, print=None)
    # Loopback only, deliberately: this service can run arbitrary Fusion code.


if __name__ == "__main__":
    main()
