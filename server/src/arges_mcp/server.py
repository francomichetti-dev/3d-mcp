"""arges — MCP server (stdio) bridging Claude Code to the Arges add-in.

Transport: MCP over stdio. Everything this process talks to is on loopback:
POST/GET against http://127.0.0.1:7654 with a shared token, nothing else.

STDIO DISCIPLINE: stdout belongs to the JSON-RPC framing. Nothing in this file
may write to it — no print(), no banner. All diagnostics go to stderr and to
~/.arges/server.log.

HTTP client choice: httpx. It is already in the dependency closure (fastmcp
ships a client built on it), it lets us split connect and read timeouts, and its
exception taxonomy (ConnectError vs ReadTimeout) maps one-to-one onto the two
very different messages the operator needs — urllib collapses both into
URLError/socket.timeout and would force string sniffing.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from . import common
from fastmcp.utilities.types import Image

# --------------------------------------------------------------------------
# Pinned protocol constants — the add-in must match these exactly.
# --------------------------------------------------------------------------

BRIDGE_PROTOCOL_VERSION = "1"
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 7654
BRIDGE_BASE_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"
AUTH_HEADER = "X-Arges-Bridge-Token"
# Sent alongside the current one, same value, for as long as a pre-rename
# add-in might be installed. Clients send both and servers accept either, which
# is what makes the rename safe in any upgrade order: the add-in is copied into
# Fusion's folder by `arges install` and this package upgrades independently, so
# there is no order we get to assume. Sending one header and guessing wrong
# fails as "invalid token" — the message that sends you looking at the token
# rather than at the header. Drop this once no old add-in can still be out there.
LEGACY_AUTH_HEADER = "X-Fusion-Bridge-Token"
VERSION_HEADER = "X-Bridge-Version"

# 75 s: longer than the add-in's 60 s marshal wait and its 70 s socket timeout,
# so a slow model operation times out at the bridge (which knows what happened)
# and never at this layer.
HTTP_READ_TIMEOUT = 75.0
HTTP_CONNECT_TIMEOUT = 10.0

STATE_DIR_NAME = ".arges"
# What installs made before the rename from fusion-mcp. Nothing here ever moves
# a directory: the Rhino half ships as a zip and updates on its own schedule, so
# the two halves of a live install are routinely different versions. A reader
# that insisted on the new name would turn that ordinary state into "invalid
# token", which sends you looking at the token rather than at the path.
# `arges install` is the one place that migrates — see bootstrap.py.
LEGACY_STATE_DIR_NAME = ".fusion-mcp"
EXPORT_DIR_NAME = "arges-exports"
LEGACY_EXPORT_DIR_NAME = "fusion-mcp-exports"


def _preferred(parent: Path, current: str, legacy: str) -> Path:
    """`parent/current`, unless only the pre-rename name is there."""
    if not (parent / current).is_dir() and (parent / legacy).is_dir():
        return parent / legacy
    return parent / current


STATE_DIR = _preferred(Path("~").expanduser(), STATE_DIR_NAME, LEGACY_STATE_DIR_NAME)
TOKEN_PATH = STATE_DIR / "token"
SERVER_LOG = STATE_DIR / "server.log"
EXPORT_DIR = _preferred(Path("~/Documents").expanduser(),
                        EXPORT_DIR_NAME, LEGACY_EXPORT_DIR_NAME)


SCREENSHOT_MAX_WIDTH = 1920
SCREENSHOT_MAX_HEIGHT = 1440
# Ceiling on one capture's PNG bytes. Base64 inflates this by 4/3, so 512 KB
# here is about 683 KB on the wire — dozens of captures fit in one message
# with room to spare. Anything larger is recaptured smaller rather than sent.
MAX_SCREENSHOT_BYTES = 512 * 1024
SCREENSHOT_SHRINK_ATTEMPTS = 2
SCREENSHOT_MIN_SIDE = 64
STATE_ENTRY_CAP = 50
LOG_MAX_BYTES = 5 * 1024 * 1024

# Mirrors the add-in's body cap. Enforced here as well because the add-in
# answers 413 *before* draining the socket and closes the connection, so a
# too-large POST is reset mid-write and httpx raises WriteError /
# RemoteProtocolError instead of ever handing us the 413.
MAX_BODY_BYTES = 5 * 1024 * 1024

VIEWS = ("front", "top", "right", "iso", "fit")
# Including "f3d", the Fusion archive — see common.py for why saving goes
# through an export rather than through Fusion's own save.
FORMATS = common.FORMATS

# --------------------------------------------------------------------------
# Operator-facing messages (kept in one place so they stay consistent).
# --------------------------------------------------------------------------

MSG_BRIDGE_DOWN = (
    "Fusion not running or Arges add-in not enabled — check "
    "Utilities → Add-Ins (select Arges → Run). The bridge listens on "
    f"{BRIDGE_BASE_URL}."
)
MSG_TIMEOUT = (
    "Fusion did not answer within the timeout — code may still be executing; "
    "do not resend; check fusion_state/screenshot."
)
MSG_NO_TOKEN = (
    f"Bridge token not found or empty at {TOKEN_PATH} — run 'arges install' "
    "(or scripts/install.sh from a checkout) to create it, then restart the "
    "Arges add-in."
)
MSG_BAD_TOKEN = (
    f"Bridge rejected the token (401). The add-in re-reads {TOKEN_PATH} on every "
    "request, so restarting it changes nothing: either that file is unreadable "
    "from Fusion's process, or this server is holding an older cached value. "
    "Check ~/.arges/addin.log, then re-run 'arges install "
    "--rotate-token' (or scripts/install.sh --rotate-token) and retry."
)
MSG_TOO_LARGE = (
    "Request body too large (413) — the bridge caps bodies at 5 MB. "
    "Split the work across several calls."
)
MSG_NOT_BRIDGE = (
    f"Something is listening on {BRIDGE_BASE_URL} but it is not Arges "
    f"(no {VERSION_HEADER} header) — another process is holding port "
    f"{BRIDGE_PORT}. Free the port and restart the add-in."
)

log = logging.getLogger("arges")
log.addHandler(logging.NullHandler())

mcp = FastMCP(
    name="fusion",
    instructions=(
        "Drive Autodesk Fusion 360 running on this Mac. fusion_execute runs "
        "Python inside the live Fusion session, fusion_screenshot shows you the "
        "viewport, fusion_state reports what is open, fusion_export writes "
        "print-ready files into the exports folder, and fusion_download writes "
        "one into the person's own save folder when they want the file in hand. "
        "The design saves itself to that folder after every successful "
        "fusion_execute, so never save by hand and never ask whether to save; "
        "fusion_save is only for an explicit \"save it now\". "
        "Check fusion_state before assuming anything about "
        "the document, and verify every geometry change visually: "
        "fusion_execute(screenshot=\"iso\") returns the picture together with "
        "the result in one call; fusion_screenshot is the standalone look."
    ),
)


# --------------------------------------------------------------------------
# Token — re-read per request, mtime-cached.
# --------------------------------------------------------------------------

_token_lock = threading.Lock()
_token_cache: tuple[int, int, str] | None = None  # (mtime_ns, size, token)


def _read_token() -> str:
    """Return the shared token, re-reading only when the file changed."""
    global _token_cache
    try:
        stat = TOKEN_PATH.stat()
    except OSError:
        raise ToolError(MSG_NO_TOKEN) from None

    key = (stat.st_mtime_ns, stat.st_size)
    with _token_lock:
        if _token_cache is not None and (_token_cache[0], _token_cache[1]) == key:
            return _token_cache[2]
        try:
            token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            raise ToolError(MSG_NO_TOKEN) from None
        if not token:
            raise ToolError(MSG_NO_TOKEN)
        _token_cache = (key[0], key[1], token)
        return token


# --------------------------------------------------------------------------
# HTTP transport to the add-in.
# --------------------------------------------------------------------------

_client_lock = threading.Lock()
_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    """Lazily build the HTTP client. Constructing it opens no connection, so
    importing or starting this server never touches the bridge."""
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                base_url=BRIDGE_BASE_URL,
                timeout=httpx.Timeout(
                    HTTP_READ_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT
                ),
                # trust_env=False: an HTTP_PROXY/ALL_PROXY variable in the
                # environment must never be able to route a loopback request
                # off this machine.
                trust_env=False,
                follow_redirects=False,
            )
        return _client


def _check_version(response: httpx.Response) -> None:
    seen = response.headers.get(VERSION_HEADER)
    if seen is None:
        raise ToolError(MSG_NOT_BRIDGE)
    if seen != BRIDGE_PROTOCOL_VERSION:
        raise ToolError(
            f"Arges add-in is v{seen}, server expects "
            f"v{BRIDGE_PROTOCOL_VERSION} — restart the add-in "
            "(Utilities → Add-Ins → stop/run)."
        )


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500].strip()
    if isinstance(body, dict):
        for key in ("error", "traceback", "message"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value[:500]
    return str(body)[:500]


def _raise_for_status(response: httpx.Response, path: str) -> None:
    status = response.status_code
    if status == 200:
        return
    if status == 401:
        raise ToolError(MSG_BAD_TOKEN)
    if status == 403:
        raise ToolError(
            "Bridge rejected the request Host header (403) — it only accepts "
            f"{BRIDGE_HOST}:{BRIDGE_PORT} or localhost:{BRIDGE_PORT}."
        )
    if status == 404:
        raise ToolError(
            f"Bridge has no endpoint {path} (404) — the running add-in is older "
            "than this server; restart it (Utilities → Add-Ins → stop/run)."
        )
    if status == 409:
        raise ToolError(
            "A previous Fusion execution is still running (409). Wait for it to "
            "finish — do not resend. Fusion's main thread cannot be "
            "interrupted; use fusion_screenshot to see where it got to."
        )
    if status == 413:
        # Backstop: _bridge_request rejects oversized payloads before they are
        # sent, so this only fires if the add-in's cap is tighter than ours.
        raise ToolError(MSG_TOO_LARGE)
    if status == 400:
        raise ToolError(f"Bridge rejected the request (400): {_detail(response)}")
    if status == 504:
        raise ToolError(MSG_TIMEOUT)
    if status == 503:
        # 503 covers a reload in flight as well as a genuine stop, and a reload
        # clears in a moment — so lead with the retry and keep the add-in advice
        # as the fallback rather than sending the user to a dialog they may not
        # need.
        raise ToolError(
            f"Arges is stopping or reloading (503): {_detail(response)} "
            "Retry in a moment; if it persists, run the add-in from Utilities → "
            "Add-Ins (select Arges → Run)."
        )
    raise ToolError(f"Bridge returned HTTP {status}: {_detail(response)}")


def _bridge_request(
    method: Literal["GET", "POST"], path: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Perform one authenticated loopback request and return the parsed body.

    Every failure that is not "your Fusion code raised" surfaces as ToolError
    with an actionable message.
    """
    body_bytes: bytes | None = None
    if method == "POST":
        # Same compact encoding httpx uses for `json=`, so this is the size that
        # would actually go on the wire.
        body_bytes = json.dumps(
            payload or {}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(body_bytes) > MAX_BODY_BYTES:
            log.warning(
                "refusing oversized %s body: %d bytes > %d",
                path,
                len(body_bytes),
                MAX_BODY_BYTES,
            )
            raise ToolError(MSG_TOO_LARGE)

    token = _read_token()
    headers = {AUTH_HEADER: token, LEGACY_AUTH_HEADER: token,
               "Accept": "application/json"}
    client = _get_client()
    started = time.monotonic()
    try:
        if method == "GET":
            response = client.get(path, headers=headers)
        else:
            response = client.post(path, headers=headers, json=payload or {})
    except (httpx.ConnectError, httpx.ConnectTimeout):
        log.warning("bridge unreachable for %s %s", method, path)
        raise ToolError(MSG_BRIDGE_DOWN) from None
    except httpx.ReadTimeout:
        log.warning("bridge read timeout for %s %s", method, path)
        raise ToolError(MSG_TIMEOUT) from None
    except httpx.HTTPError as exc:
        log.warning("bridge transport error for %s %s: %r", method, path, exc)
        raise ToolError(f"Bridge request failed: {exc!r}") from None

    log.info(
        "%s %s -> %s in %.2fs (%d bytes)",
        method,
        path,
        response.status_code,
        time.monotonic() - started,
        len(response.content),
    )
    _check_version(response)
    _raise_for_status(response, path)
    try:
        body = response.json()
    except ValueError:
        raise ToolError(
            f"Bridge returned a non-JSON body for {path}: {response.text[:300]!r}"
        ) from None
    if not isinstance(body, dict):
        raise ToolError(f"Bridge returned an unexpected JSON body for {path}.")
    return body


def _execute(
    code: str, reset: bool = False, allow_no_design: bool = False
) -> dict[str, Any]:
    return _bridge_request(
        "POST",
        "/execute",
        {"code": code, "reset": reset, "allow_no_design": allow_no_design},
    )


# --------------------------------------------------------------------------
# Generated snippets.
#
# Parameters are embedded as a JSON document that the snippet parses at
# runtime, never interpolated into the source. A component name containing a
# quote, a backslash or a newline is therefore inert data, not code.
# --------------------------------------------------------------------------


_snippet = common.snippet


def _run_snippet(body: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run a generated snippet and unwrap its `result` dict.

    A Fusion-side raise comes back as the bridge's {ok: false, traceback}
    envelope and is passed through untouched — same contract as fusion_execute.
    """
    payload = _execute(_snippet(body, params))
    result = payload.get("result")
    if payload.get("ok") and isinstance(result, dict):
        return result
    return payload


_EXPORT_BODY = common.EXPORT_BODY




_STATE_BODY = '''
def _fx_state(_p):
    cap = _p["cap"]

    des = adsk.fusion.Design.cast(app.activeProduct)
    if des is None:
        return {"ok": False, "error": "no active Fusion design — open or create one and switch to the Design workspace"}

    root = des.rootComponent

    try:
        parametric = des.designType == adsk.fusion.DesignTypes.ParametricDesignType
    except (AttributeError, RuntimeError):
        parametric = True
    try:
        timeline_count = des.timeline.count
    except (AttributeError, RuntimeError):
        timeline_count = None

    params_total = des.userParameters.count
    params = []
    for i in range(min(params_total, cap)):
        p = des.userParameters.item(i)
        params.append({"name": p.name, "expression": p.expression, "value": p.value})
    if params_total > cap:
        params.append({"name": "... truncated, " + str(params_total - cap) + " more", "expression": "", "value": None})

    occ_total = root.occurrences.count
    occurrences = []
    for i in range(min(occ_total, cap)):
        occ = root.occurrences.item(i)
        occurrences.append(occ.name + " (" + str(occ.component.bRepBodies.count) + " bodies)")
    if occ_total > cap:
        occurrences.append("... truncated, " + str(occ_total - cap) + " more")

    body_total = root.bRepBodies.count
    bodies = []
    for i in range(min(body_total, cap)):
        bodies.append(root.bRepBodies.item(i).name)
    if body_total > cap:
        bodies.append("... truncated, " + str(body_total - cap) + " more")

    doc = app.activeDocument
    return {
        "ok": True,
        "document": doc.name,
        "saved": doc.isSaved,
        "designType": "parametric" if parametric else "direct",
        "units": des.unitsManager.defaultLengthUnits,
        "timeline_count": timeline_count,
        "user_parameters": params,
        "root_occurrences": occurrences,
        "root_bodies": bodies,
        "counts": {
            "user_parameters": params_total,
            "root_occurrences": occ_total,
            "root_bodies": body_total,
            "all_components": des.allComponents.count,
        },
    }


result = _fx_state(_fx_params)
'''


# --------------------------------------------------------------------------
# Export path handling.
# --------------------------------------------------------------------------

# Both live in common.py now, because the chat service needs them too. Aliased
# rather than renamed at every call site: these names are what the tests and the
# rest of this file already say.
_slug = common.slug


def _export_root() -> Path:
    return Path(os.path.realpath(EXPORT_DIR))


def _resolve_export_path(path: str, fmt: str, name: str) -> Path:
    """Map the caller's path onto a real path confined to the export dir."""
    root = _export_root()
    raw = path.strip()

    if not raw:
        stem = _slug(name) or "design"
        target = root / f"{stem}_{time.strftime('%Y%m%d-%H%M%S')}.{fmt}"
    else:
        candidate = Path(os.path.expanduser(raw))
        if not candidate.is_absolute():
            candidate = root / candidate
        target = Path(os.path.realpath(candidate))

    if target.suffix.lower() != f".{fmt}":
        target = target.with_name(f"{target.name}.{fmt}")

    try:
        target.relative_to(root)
    except ValueError:
        raise ToolError(
            f"Export path must stay under {EXPORT_DIR} (resolved to {target}). "
            "Use fusion_execute directly if you need an exotic destination."
        ) from None
    if target == root or target.is_dir():
        raise ToolError(f"Export path {target} is a directory, not a file name.")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ToolError(f"Cannot create export directory {target.parent}: {exc}") from None
    return target




# --------------------------------------------------------------------------
# Saving the state.
#
# Not through Fusion's own save. An expired subscription puts Fusion in
# read-only mode, and there Document.save() returns True and saves nothing —
# verified live on a real document: no new version, the document still dirty,
# the window title reading "Read Only". A save that silently does nothing is
# worse than no save, so what "save" means here is a Fusion archive (.f3d)
# written to a folder you choose: the whole parametric design, in one local
# file, produced by the export manager, which keeps working when saving does
# not. Fusion's own ⌘S is untouched and still yours to use.
# --------------------------------------------------------------------------


def _autosave_enabled() -> bool:
    """Whether to save after every successful change. On unless turned off.

    Read per call rather than captured at import, so the operator's answer does
    not need a restart to change. Anything unrecognised counts as on: the
    failure that matters here is losing work, not saving too often.
    """
    raw = (os.environ.get("ARGES_AUTOSAVE")
           or os.environ.get("FUSION_AUTOSAVE") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _document_name() -> str:
    """The open document's name, or "" if the bridge cannot say.

    From /health, which answers off Fusion's main thread, so asking costs
    nothing worth counting. It is asked at all because in this codebase the
    host resolves every path and the Fusion-side snippet only writes to the one
    it is handed — which needs the document's name here rather than there.
    """
    try:
        health = _bridge_request("GET", "/health")
    except Exception:                                     # noqa: BLE001
        return ""
    name = health.get("document")
    return name if isinstance(name, str) else ""


def _write_state_file() -> dict[str, Any]:
    """Write the whole design to the configured folder as one .f3d.

    One file per document, overwritten. This is a mirror of the current state,
    not a pile of snapshots — Fusion's own version history is the place for
    those, and a new 3 MB archive per modelling step would fill a disk by
    lunchtime.
    """
    folder = common.save_dir_from()
    target = common.output_path(folder, "", _document_name() or "design",
                                common.SAVE_FORMAT, overwrite=True)
    outcome = _run_snippet(
        _EXPORT_BODY,
        {"format": common.SAVE_FORMAT, "name": "", "path": str(target)},
    )
    if outcome.get("ok"):
        outcome["saved"] = True
    return outcome


def _autosave(result: dict[str, Any]) -> None:
    """Save the state after a successful change. Never raises.

    Here rather than left to the model: "and save it" after every step is the
    one instruction nobody wants to have to repeat, and a model that forgets
    loses work without saying so.

    A failed save must not turn a change that worked into a tool error. The
    geometry is already in the document; an error raised here would read as
    "the script failed" and push the model into running the same code twice.
    The outcome rides along under "autosave" instead, exactly as a failed
    screenshot does.
    """
    if not _autosave_enabled():
        return
    try:
        outcome = _write_state_file()
    except Exception as exc:                              # noqa: BLE001
        outcome = {"ok": False, "saved": False, "error": (
            f"the change succeeded, but saving the state failed: {exc}")}
    result["autosave"] = outcome


# --------------------------------------------------------------------------
# Tools.
# --------------------------------------------------------------------------


@mcp.tool
def fusion_execute(
    code: str,
    reset: bool = False,
    allow_no_design: bool = False,
    screenshot: Literal["front", "top", "right", "iso", "fit"] | None = None,
) -> Any:
    """Run Python inside the live Fusion 360 session.

    UNITS: the API's internal length unit is CENTIMETERS regardless of the
    document's display units — 20 mm = 2.0. Angles are radians. Prefer explicit
    strings where a ValueInput is accepted: ValueInput.createByString("20 mm").

    INJECTED NAMES (do not re-import or re-resolve them): `adsk` with
    `adsk.core` and `adsk.fusion` already imported, `app`, `ui`, and `design`
    (already cast to adsk.fusion.Design and re-resolved for every call). The
    namespace PERSISTS between calls, so entities you stored in a variable
    earlier are still there; pass reset=true to clear it.

    RESULT: assign to a variable named `result` to send a value back; anything
    you print() is captured and returned as `stdout`. Both are capped at 64 KB.

    VERIFY EVERY GEOMETRY CHANGE: pass screenshot="iso" (or another view) and
    the viewport image arrives together with the result — one call instead of
    two. LOOK at the picture before continuing; never chain modeling steps
    blind. The capture runs only when your code succeeded, and a capture
    problem never fails the call: the result gains a "screenshot_error" note
    instead. Reach for fusion_screenshot only when you want a second angle, a
    custom size, or a look without running any code.

    NEVER call ui.messageBox, adsk.doEvents(), or create/execute UI commands —
    a modal dialog deadlocks the bridge. Never write unbounded loops: the code
    runs on Fusion's main thread and cannot be cancelled; the 60 s timeout only
    abandons the wait. Split long operations across several calls.

    Returns {"ok": true, "result": ..., "stdout": ...}; or
    {"ok": false, "traceback": ..., "stdout": ...} when your code raised — a
    failing script is a normal result, read the traceback and fix the code; or
    {"ok": false, "error": ...} with NO traceback and NO stdout, which means the
    code never ran. The usual cause is "no active Fusion design".
    Always check `error` when `traceback` is absent. With screenshot set, that
    same JSON is the first content block and the image is the second.

    SAVING IS AUTOMATIC: after every successful call the design is written to
    the person's save folder as a Fusion archive, and the result carries
    "autosave" — {"saved": true, "path": ..., "bytes": ...} — saying where it
    went. Do NOT call doc.save() yourself and do not ask whether to save; it is
    done. (Fusion's own save is deliberately not used: on an expired
    subscription it reports success and saves nothing.) A save that fails leaves
    "autosave" with an `error` and does NOT make the call fail — your geometry
    is still there, so never re-run the code because of it.

    NO DOCUMENT OPEN: pass allow_no_design=true to run anyway, with `design`
    injected as None, and create one yourself — this is the only way out of that
    state, since the guard would otherwise block the very call that fixes it:
        doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    Every later call then sees the new design normally. Use it ONLY to bootstrap
    a document; leave it false otherwise so the clear error keeps protecting you.
    """
    if not isinstance(code, str) or not code.strip():
        raise ToolError("`code` must be a non-empty Python source string.")
    # An unknown view must be refused BEFORE the code runs: rejecting it after
    # would leave the geometry changed and the caller believing nothing
    # happened, which is the worst possible reading of an error.
    if screenshot is not None and screenshot not in VIEWS:
        raise ToolError(
            f"Unknown screenshot view {screenshot!r} — valid views: "
            f"{', '.join(VIEWS)}. The code was NOT run."
        )
    log.info(
        "fusion_execute: %d chars, reset=%s, allow_no_design=%s, screenshot=%s",
        len(code), reset, allow_no_design, screenshot,
    )
    log.debug("fusion_execute code: %s", code[:2000])
    result = _execute(code, reset=reset, allow_no_design=allow_no_design)
    # Before the screenshot, so both return shapes carry the outcome, and so a
    # slow cloud save cannot leave a window where the picture is newer than the
    # saved document.
    if result.get("ok"):
        _autosave(result)
    if screenshot is None or not result.get("ok"):
        # A failed script wants its traceback read, not photographed; the
        # geometry may be half-changed and the next step is fixing the code.
        return result
    try:
        data = _bounded_capture(screenshot, 1200, 800)
    except Exception as exc:                              # noqa: BLE001
        # The code SUCCEEDED — reporting the capture as a tool error would
        # read as "the script failed" and push the model into re-running it.
        result["screenshot_error"] = (
            f"the code ran fine, but the screenshot failed: {exc}"
        )
        return result
    return [result, Image(data=data, format="png")]


@mcp.tool
def fusion_screenshot(
    view: Literal["front", "top", "right", "iso", "fit"] = "iso",
    width: int = 1200,
    height: int = 800,
) -> Image:
    """Capture the Fusion viewport as a PNG image.

    For routine verification after running code, prefer fusion_execute's
    screenshot parameter — the same picture, one round trip. Call this when you
    want a look without executing anything, another angle, or a custom size.

    Views: "front", "top", "right", "iso" (isometric from top-right), and "fit"
    (keep the current camera orientation, just frame everything). Every view
    fits the model in the viewport before capturing.

    Size defaults to 1200x800. Width must be 64..1920 and height 64..1440;
    values outside that range are REJECTED with an error, never clamped, so
    pass a size inside the range rather than relying on a fallback.
    """
    if view not in VIEWS:
        raise ToolError(f"Unknown view {view!r} — valid views: {', '.join(VIEWS)}.")
    if not SCREENSHOT_MIN_SIDE <= width <= SCREENSHOT_MAX_WIDTH:
        raise ToolError(
            f"width must be between {SCREENSHOT_MIN_SIDE} and {SCREENSHOT_MAX_WIDTH}."
        )
    if not SCREENSHOT_MIN_SIDE <= height <= SCREENSHOT_MAX_HEIGHT:
        raise ToolError(
            f"height must be between {SCREENSHOT_MIN_SIDE} and {SCREENSHOT_MAX_HEIGHT}."
        )
    return Image(data=_bounded_capture(view, width, height), format="png")


def _bounded_capture(view: str, width: int, height: int) -> bytes:
    """Capture, then shrink and recapture until the PNG fits the budget.

    An image travels to the model as base64 inside a single protocol message,
    and every transport between here and there bounds how long one message
    may be. A dense viewport is what pushes it: measured against a live
    Fusion, an empty scene is around 200 KB at 1200x800 while a detailed
    model at 1920x1440 reaches 631 KB of base64 — and several captures in one
    turn is ordinary, since the whole method is look-then-correct.

    So the size is bounded here rather than hoped about. Shrinking and
    recapturing costs a second and keeps the picture; the alternative is a
    turn that dies with the geometry half-built, which is what used to happen.
    """
    data = _capture(view, width, height)
    for _ in range(SCREENSHOT_SHRINK_ATTEMPTS):
        if len(data) <= MAX_SCREENSHOT_BYTES:
            break
        # Bytes scale roughly with area, so take the square root to land near
        # the budget in one step instead of creeping toward it.
        scale = (MAX_SCREENSHOT_BYTES / len(data)) ** 0.5
        smaller_w = max(SCREENSHOT_MIN_SIDE, int(width * scale))
        smaller_h = max(SCREENSHOT_MIN_SIDE, int(height * scale))
        if (smaller_w, smaller_h) == (width, height):
            break                      # already as small as it is allowed to go
        log.info("fusion_screenshot: %d bytes over budget, retrying at %dx%d",
                 len(data), smaller_w, smaller_h)
        width, height = smaller_w, smaller_h
        data = _capture(view, width, height)

    log.info("fusion_screenshot: view=%s %dx%d -> %d bytes", view, width, height, len(data))
    return data


def _capture(view: str, width: int, height: int) -> bytes:
    """One capture through the bridge, returned as raw PNG bytes."""
    payload = _bridge_request(
        "POST", "/screenshot", {"view": view, "width": width, "height": height}
    )
    if not payload.get("ok"):
        raise ToolError(
            "Screenshot failed: "
            + str(payload.get("error") or payload.get("traceback") or payload)
        )

    encoded = payload.get("png_base64")
    if not isinstance(encoded, str) or not encoded:
        raise ToolError("Bridge returned no image data for the screenshot.")
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ToolError(f"Bridge returned undecodable image data: {exc}") from None


@mcp.tool
def fusion_export(
    format: Literal["stl", "step", "3mf", "usd", "f3d"],
    body_or_component: str = "",
    path: str = "",
) -> dict[str, Any]:
    """Export geometry from the open design to a file on this Mac.

    What gets exported:
      - Leave `body_or_component` empty to export the whole design (root
        component) — works for every format.
      - "stl" and "3mf" are mesh formats and accept a body name, an occurrence
        name ("Housing:1") or a component name. Mesh refinement is set to high.
      - "step" is COMPONENT-ONLY: passing a body name returns an error naming
        the components that contain it. Export the component instead.
      - "usd" is component-only too (it is the interchange format for
        rendering and DCC tools).
      - "f3d" is the Fusion archive: the whole parametric design in one file,
        component-level, and the only format that keeps the history. It is what
        fusion_save writes.
      - A name that matches more than one thing returns an error listing the
        candidates — retry with an exact occurrence name.

    Where it goes: `path` is optional and confined to ~/Documents/arges-exports/
    (relative paths resolve inside it). Leave it empty for a timestamped
    filename. The extension is added to match the format.

    Returns {"ok": true, "path", "bytes", "target"} or {"ok": false, "error",
    "candidates"}.
    """
    if format not in FORMATS:
        raise ToolError(f"Unknown format {format!r} — valid: {', '.join(FORMATS)}.")

    name = body_or_component.strip()
    target = _resolve_export_path(path, format, name)
    log.info("fusion_export: format=%s name=%r -> %s", format, name, target)

    return _run_snippet(
        _EXPORT_BODY, {"format": format, "name": name, "path": str(target)}
    )


@mcp.tool
def fusion_download(
    format: str = "",
    body_or_component: str = "",
    filename: str = "",
) -> dict[str, Any]:
    """Write a file of the open design into the person's own download folder.

    Use this when they want the file itself — "download the STL", "give me the
    STEP", "send it to my printer software". It goes to the folder configured in
    the chat panel (Downloads unless they changed it), so the file lands where
    they already look for files rather than in a directory they have to be told
    about. fusion_export is the other one: it keeps a copy in
    ~/Documents/arges-exports/ instead.

    `format` defaults to whatever they configured (stl unless changed), so
    leaving it empty does the expected thing. Pass one of stl, step, 3mf, usd,
    f3d to override for this file only.

    What gets exported:
      - Leave `body_or_component` empty for the whole design (root component).
      - stl and 3mf accept a body name, an occurrence name ("Housing:1") or a
        component name; step, usd and f3d are component-level and refuse a body,
        naming the components that contain it.
      - A name matching more than one thing returns an error listing the
        candidates; retry with an exact occurrence name.

    `filename` is a NAME, not a path: the folder is configuration, not something
    a caller picks, and the extension is added to match the format. Leave it
    empty to name the file after the document. Nothing is ever overwritten — a
    name already in the folder gets -1, -2, … appended.

    Returns {"ok": true, "path", "bytes", "target"} — `path` is the real file on
    disk, ready to hand to the person — or {"ok": false, "error", "candidates"}.
    """
    config = common.load_config()
    fmt = (format or "").strip().lower() or common.format_from(config)
    if fmt not in FORMATS:
        raise ToolError(f"Unknown format {fmt!r} — valid: {', '.join(FORMATS)}.")

    name = body_or_component.strip()
    try:
        target = common.output_path(common.save_dir_from(config), filename,
                                    name or _document_name() or "design", fmt)
    except (OSError, ValueError) as exc:
        raise ToolError(str(exc)) from None
    log.info("fusion_download: format=%s name=%r -> %s", fmt, name, target)

    # The same Fusion-side export the exports folder gets; only the destination
    # differs. Two copies of the body/occurrence/component lookup would be two
    # things to keep in step.
    return _run_snippet(
        _EXPORT_BODY, {"format": fmt, "name": name, "path": str(target)}
    )


@mcp.tool
def fusion_save() -> dict[str, Any]:
    """Save the current state of the design to a file you keep.

    Call it when the person says "save" — and note that it is normally already
    done: every successful fusion_execute saves automatically, so this is for an
    explicit ask, or after something the automatic save reported as failed.

    It writes the whole design as a Fusion archive (.f3d) into the configured
    folder, one file per document, overwritten each time — a mirror of the
    current state. That is deliberately NOT Fusion's own save: on an expired
    subscription Fusion goes read-only, where Document.save() reports success
    and saves nothing, while this keeps working. It does not replace Fusion's
    cloud version history when that is available.

    Returns {"ok": true, "saved": true, "path", "bytes"} or
    {"ok": false, "error"}.
    """
    try:
        return _write_state_file()
    except (OSError, ValueError) as exc:
        raise ToolError(f"Cannot write the state file: {exc}") from None


@mcp.tool
def fusion_state() -> dict[str, Any]:
    """Report what is currently open in Fusion — call this before assuming anything.

    Returns the document name and saved flag, designType ("parametric" or
    "direct"), the document's display length units, the timeline item count,
    user parameters (name / expression / value in cm), and a DEPTH-1 listing of
    the root component's occurrences ("Housing:1 (3 bodies)") and root bodies,
    plus total counts. Each list is capped at 50 entries with a
    "... truncated, N more" marker.

    This is deliberately shallow. For anything deeper — nested components, face
    and edge queries, bounding boxes, measurements — use fusion_execute.
    """
    health = _bridge_request("GET", "/health")
    seen = health.get("bridge_version")
    if seen != BRIDGE_PROTOCOL_VERSION:
        raise ToolError(
            f"Arges add-in reports bridge_version {seen!r}, server expects "
            f"{BRIDGE_PROTOCOL_VERSION!r} — restart the add-in "
            "(Utilities → Add-Ins → stop/run)."
        )

    state = _run_snippet(_STATE_BODY, {"cap": STATE_ENTRY_CAP})
    state.setdefault("app_version", health.get("app_version"))
    state.setdefault("busy", health.get("busy"))
    state["bridge_version"] = BRIDGE_PROTOCOL_VERSION
    return state


# --------------------------------------------------------------------------
# Startup.
# --------------------------------------------------------------------------


class _SecureRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that creates every file it opens 0600.

    logging's own _open() uses builtin open(), so a one-shot chmod after
    construction is lost at the first rollover: the fresh server.log lands at
    whatever umask allows (0644 in practice). server.log carries the Fusion
    Python Claude sent — design data — so the mode has to hold across rollovers.
    """

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, self.mode.replace("b", "") or "a", encoding=self.encoding)


def _configure_logging() -> None:
    """Log to stderr always, and to ~/.arges/server.log when possible.

    Never to stdout: that stream carries the JSON-RPC framing.
    """
    level = logging.DEBUG if os.environ.get("ARGES_MCP_DEBUG") or os.environ.get("FUSION_MCP_DEBUG") else logging.INFO
    log.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    log.addHandler(stderr_handler)

    try:
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkdir(mode=...) is a no-op on a directory that already exists, so
        # repair the mode unconditionally.
        os.chmod(STATE_DIR, 0o700)
        file_handler = _SecureRotatingFileHandler(
            SERVER_LOG, maxBytes=LOG_MAX_BYTES, backupCount=2, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)
    except OSError as exc:
        log.warning("file logging disabled (%s): %s", SERVER_LOG, exc)


def main() -> None:
    _configure_logging()
    try:
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("could not create export directory %s: %s", EXPORT_DIR, exc)
    log.info("arges server starting (bridge protocol v%s)", BRIDGE_PROTOCOL_VERSION)
    # show_banner=False keeps startup chatter out of the transport entirely.
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
