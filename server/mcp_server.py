"""fusion-mcp — MCP server (stdio) bridging Claude Code to the FusionBridge add-in.

Transport: MCP over stdio. Everything this process talks to is on loopback:
POST/GET against http://127.0.0.1:7654 with a shared token, nothing else.

STDIO DISCIPLINE: stdout belongs to the JSON-RPC framing. Nothing in this file
may write to it — no print(), no banner. All diagnostics go to stderr and to
~/.fusion-mcp/server.log.

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
from fastmcp.utilities.types import Image

# --------------------------------------------------------------------------
# Pinned protocol constants — the add-in must match these exactly.
# --------------------------------------------------------------------------

BRIDGE_PROTOCOL_VERSION = "1"
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 7654
BRIDGE_BASE_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"
AUTH_HEADER = "X-Fusion-Bridge-Token"
VERSION_HEADER = "X-Bridge-Version"

# 75 s: longer than the add-in's 60 s marshal wait and its 70 s socket timeout,
# so a slow model operation times out at the bridge (which knows what happened)
# and never at this layer.
HTTP_READ_TIMEOUT = 75.0
HTTP_CONNECT_TIMEOUT = 10.0

FUSION_DIR = Path("~/.fusion-mcp").expanduser()
TOKEN_PATH = FUSION_DIR / "token"
SERVER_LOG = FUSION_DIR / "server.log"
EXPORT_DIR = Path("~/Documents/fusion-mcp-exports").expanduser()

SCREENSHOT_MAX_WIDTH = 1920
SCREENSHOT_MAX_HEIGHT = 1440
SCREENSHOT_MIN_SIDE = 64
STATE_ENTRY_CAP = 50
LOG_MAX_BYTES = 5 * 1024 * 1024

# Mirrors the add-in's body cap. Enforced here as well because the add-in
# answers 413 *before* draining the socket and closes the connection, so a
# too-large POST is reset mid-write and httpx raises WriteError /
# RemoteProtocolError instead of ever handing us the 413.
MAX_BODY_BYTES = 5 * 1024 * 1024

VIEWS = ("front", "top", "right", "iso", "fit")
FORMATS = ("stl", "step", "3mf", "usd")

# --------------------------------------------------------------------------
# Operator-facing messages (kept in one place so they stay consistent).
# --------------------------------------------------------------------------

MSG_BRIDGE_DOWN = (
    "Fusion not running or FusionBridge add-in not enabled — check "
    "Utilities → Add-Ins (select FusionBridge → Run). The bridge listens on "
    f"{BRIDGE_BASE_URL}."
)
MSG_TIMEOUT = (
    "Fusion did not answer within the timeout — code may still be executing; "
    "do not resend; check fusion_state/screenshot."
)
MSG_NO_TOKEN = (
    f"Bridge token not found or empty at {TOKEN_PATH} — run scripts/install.sh "
    "to create it, then restart the FusionBridge add-in."
)
MSG_BAD_TOKEN = (
    f"Bridge rejected the token (401). The add-in re-reads {TOKEN_PATH} on every "
    "request, so restarting it changes nothing: either that file is unreadable "
    "from Fusion's process, or this server is holding an older cached value. "
    "Check ~/.fusion-mcp/addin.log, then re-run scripts/install.sh (or "
    "scripts/install.sh --rotate-token) and retry."
)
MSG_TOO_LARGE = (
    "Request body too large (413) — the bridge caps bodies at 5 MB. "
    "Split the work across several calls."
)
MSG_NOT_BRIDGE = (
    f"Something is listening on {BRIDGE_BASE_URL} but it is not FusionBridge "
    f"(no {VERSION_HEADER} header) — another process is holding port "
    f"{BRIDGE_PORT}. Free the port and restart the add-in."
)

log = logging.getLogger("fusion-mcp")
log.addHandler(logging.NullHandler())

mcp = FastMCP(
    name="fusion",
    instructions=(
        "Drive Autodesk Fusion 360 running on this Mac. fusion_execute runs "
        "Python inside the live Fusion session, fusion_screenshot shows you the "
        "viewport, fusion_state reports what is open, fusion_export writes "
        "print-ready files. Check fusion_state before assuming anything about "
        "the document, and screenshot after every geometry change."
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
            f"FusionBridge add-in is v{seen}, server expects "
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
            f"FusionBridge is stopping or reloading (503): {_detail(response)} "
            "Retry in a moment; if it persists, run the add-in from Utilities → "
            "Add-Ins (select FusionBridge → Run)."
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

    headers = {AUTH_HEADER: _read_token(), "Accept": "application/json"}
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


def _snippet(body: str, params: dict[str, Any]) -> str:
    literal = json.dumps(json.dumps(params, ensure_ascii=True))
    return f"import json as _fx_json\n_fx_params = _fx_json.loads({literal})\n{body}"


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


_EXPORT_BODY = '''
def _fx_export(_p):
    import os

    fmt = _p["format"]
    name = _p["name"]
    out_path = _p["path"]

    des = adsk.fusion.Design.cast(app.activeProduct)
    if des is None:
        return {"ok": False, "error": "no active Fusion design — open or create one and switch to the Design workspace"}

    root = des.rootComponent

    if not name:
        geom = root
        target = "whole design (root component)"
    else:
        bodies = []
        components = []
        occurrences = []
        for comp in des.allComponents:
            if comp.name == name:
                components.append((comp, "component '" + comp.name + "'"))
            for body in comp.bRepBodies:
                if body.name == name:
                    bodies.append((body, "body '" + body.name + "' in component '" + comp.name + "'"))
        for occ in root.allOccurrences:
            if occ.name == name:
                occurrences.append((occ, "occurrence '" + occ.name + "'"))

        if fmt in ("stl", "3mf"):
            pool = bodies + occurrences + components
        else:
            pool = occurrences + components
            if not pool and bodies:
                return {
                    "ok": False,
                    "error": fmt.upper() + " export is component-only; '" + name + "' is a body. Export the component or occurrence containing it, or use stl/3mf to export a single body.",
                    "candidates": [entry[1] for entry in bodies],
                }
        if not pool:
            return {"ok": False, "error": "nothing named '" + name + "' in this design — call fusion_state to see what exists"}
        if len(pool) > 1:
            return {
                "ok": False,
                "error": "'" + name + "' is ambiguous — pass an exact occurrence name such as 'Housing:1', or rename to something unique",
                "candidates": [entry[1] for entry in pool],
            }
        geom, target = pool[0]

    em = des.exportManager
    if fmt == "stl":
        opts = em.createSTLExportOptions(geom, out_path)
    elif fmt == "3mf":
        opts = em.createC3MFExportOptions(geom, out_path)
    elif fmt == "step":
        opts = em.createSTEPExportOptions(out_path, geom)
    else:
        # createUSDExportOptions has taken its arguments in both orders across
        # Fusion releases; try one, fall back to the other rather than pinning
        # to a signature that a Fusion update can invalidate.
        try:
            opts = em.createUSDExportOptions(out_path, geom)
        except (TypeError, RuntimeError):
            opts = em.createUSDExportOptions(geom, out_path)

    try:
        opts.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementHigh
    except (AttributeError, RuntimeError):
        pass
    try:
        opts.filename = out_path
    except (AttributeError, RuntimeError):
        pass

    if not em.execute(opts):
        return {"ok": False, "error": "exportManager.execute() returned False for the " + fmt + " export of " + target}

    # Fusion may append its own extension rather than honouring the filename it
    # was given: a USD export to "part.usd" is actually written as
    # "part.usd.usdz" (a zip holding a .usdc). Checking only the requested path
    # would report a false failure for an export that succeeded.
    written = out_path
    if not os.path.exists(written):
        for suffix in (".usdz", ".usd", ".usdc", ".stl", ".step", ".stp", ".3mf"):
            if os.path.exists(out_path + suffix):
                written = out_path + suffix
                break

    size = os.path.getsize(written) if os.path.exists(written) else 0
    if size == 0:
        return {"ok": False, "error": "export reported success but no file was written to " + out_path}
    return {"ok": True, "format": fmt, "path": written, "bytes": size, "target": target}


result = _fx_export(_fx_params)
'''


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

_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _slug(name: str) -> str:
    """Turn a component name into a safe filename stem (dots included in the
    unsafe set, so no generated stem can read as a path component)."""
    return _SLUG_RE.sub("_", name).strip("_-")[:60]


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
# Tools.
# --------------------------------------------------------------------------


@mcp.tool
def fusion_execute(
    code: str, reset: bool = False, allow_no_design: bool = False
) -> dict[str, Any]:
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

    AFTER ANY GEOMETRY CHANGE, CALL fusion_screenshot AND VERIFY the result
    before continuing. Do not chain several modeling steps blind.

    NEVER call ui.messageBox, adsk.doEvents(), or create/execute UI commands —
    a modal dialog deadlocks the bridge. Never write unbounded loops: the code
    runs on Fusion's main thread and cannot be cancelled; the 60 s timeout only
    abandons the wait. Split long operations across several calls.

    Returns {"ok": true, "result": ..., "stdout": ...}; or
    {"ok": false, "traceback": ..., "stdout": ...} when your code raised — a
    failing script is a normal result, read the traceback and fix the code; or
    {"ok": false, "error": ...} with NO traceback and NO stdout, which means the
    code never ran. The usual cause is "no active Fusion design".
    Always check `error` when `traceback` is absent.

    NO DOCUMENT OPEN: pass allow_no_design=true to run anyway, with `design`
    injected as None, and create one yourself — this is the only way out of that
    state, since the guard would otherwise block the very call that fixes it:
        doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    Every later call then sees the new design normally. Use it ONLY to bootstrap
    a document; leave it false otherwise so the clear error keeps protecting you.
    """
    if not isinstance(code, str) or not code.strip():
        raise ToolError("`code` must be a non-empty Python source string.")
    log.info(
        "fusion_execute: %d chars, reset=%s, allow_no_design=%s",
        len(code), reset, allow_no_design,
    )
    log.debug("fusion_execute code: %s", code[:2000])
    return _execute(code, reset=reset, allow_no_design=allow_no_design)


@mcp.tool
def fusion_screenshot(
    view: Literal["front", "top", "right", "iso", "fit"] = "iso",
    width: int = 1200,
    height: int = 800,
) -> Image:
    """Capture the Fusion viewport as a PNG image.

    Call this after every geometry change and look at what came back — this is
    the only way to catch geometry that went somewhere unexpected.

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
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ToolError(f"Bridge returned undecodable image data: {exc}") from None

    log.info("fusion_screenshot: view=%s %dx%d -> %d bytes", view, width, height, len(data))
    return Image(data=data, format="png")


@mcp.tool
def fusion_export(
    format: Literal["stl", "step", "3mf", "usd"],
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
      - A name that matches more than one thing returns an error listing the
        candidates — retry with an exact occurrence name.

    Where it goes: `path` is optional and confined to ~/Documents/fusion-mcp-exports/
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
            f"FusionBridge add-in reports bridge_version {seen!r}, server expects "
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
    """Log to stderr always, and to ~/.fusion-mcp/server.log when possible.

    Never to stdout: that stream carries the JSON-RPC framing.
    """
    level = logging.DEBUG if os.environ.get("FUSION_MCP_DEBUG") else logging.INFO
    log.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    log.addHandler(stderr_handler)

    try:
        FUSION_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkdir(mode=...) is a no-op on a directory that already exists, so
        # repair the mode unconditionally.
        os.chmod(FUSION_DIR, 0o700)
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
    log.info("fusion-mcp server starting (bridge protocol v%s)", BRIDGE_PROTOCOL_VERSION)
    # show_banner=False keeps startup chatter out of the transport entirely.
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
