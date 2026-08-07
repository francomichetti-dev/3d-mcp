"""An MCP server that gives Claude Code control of Rhino. No dependencies.

Claude Code speaks MCP over stdin/stdout, so registering this once means every
`claude` session — terminal, IDE, or the chat window — can drive Rhino. It runs
on the person's Claude subscription, so there is no API key to obtain, paste,
or bill separately.

    claude  ──MCP/stdio──▶  this  ──HTTP──▶  broker  ──▶  poller inside Rhino

Written against the protocol rather than a framework on purpose. FastMCP needs
Python 3.10+ and Rhino ships 3.9.10, so a framework would mean installing a
second Python just to run a server that forwards four JSON messages. This has
no dependencies at all, which also means there is nothing to install and
nothing to keep up to date — the whole setup is one `claude mcp add` line.

Register it (one line, paths already correct for this machine):

    claude mcp add rhino -- <rhino-python> <path-to-this-file>
"""

import json
import os
import sys
import urllib.error
import urllib.request

BROKER = os.environ.get("FUSION_BROKER_URL") or "http://127.0.0.1:7656"
TOKEN_PATH = os.path.join(os.path.expanduser("~"), ".fusion-mcp", "token")
AUTH_HEADER = "X-Fusion-Bridge-Token"

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "rhino", "version": "0.1.0"}


# --------------------------------------------------------------------------
# The bridge
# --------------------------------------------------------------------------


def _token():
    # Env first, mirroring FUSION_BROKER_URL. Lets the server be pointed at a
    # test broker without touching the real token file, and covers installs
    # where the token does not live under HOME.
    from_env = os.environ.get("FUSION_BRIDGE_TOKEN")
    if from_env:
        return from_env.strip()
    try:
        with open(TOKEN_PATH, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def submit(kind, payload, timeout=120):
    """Hand a job to Rhino through the broker. Never raises."""
    body = json.dumps({"kind": kind, "payload": payload}).encode()
    request = urllib.request.Request(BROKER + "/submit", data=body, method="POST")
    request.add_header(AUTH_HEADER, _token())
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return {"ok": False, "error": (
                "the broker rejected the token — reinstall to regenerate it")}
        return {"ok": False, "error": f"broker returned HTTP {exc.code}"}
    except urllib.error.URLError:
        return {"ok": False, "error": (
            "Rhino is not reachable. Check that the broker is running "
            "(START-BROKER.cmd) and that Rhino has the poller started.")}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"broker call failed: {exc}"}


# --------------------------------------------------------------------------
# Tools. The description is what Claude reads to decide whether to call the
# tool, so each says WHEN to use it, not only what it does.
# --------------------------------------------------------------------------

TOOLS = [
    {
        "name": "rhino_execute",
        "description": (
            "Run Python inside the live Rhino 8 session and return its output. "
            "This is how you create, modify, measure, or delete geometry — use "
            "it whenever the person asks for a change to the model.\n\n"
            "`Rhino` (RhinoCommon), `rs` (rhinoscriptsyntax), `doc` (the active "
            "document) and `scriptcontext` are already in scope. Assign to "
            "`result` to return a value. Variables persist between calls, so "
            "you can build something up over several steps.\n\n"
            "After changing geometry, call rhino_screenshot to check it looks "
            "right before saying it is done."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string",
                         "description": "Python to run inside Rhino."},
            },
            "required": ["code"],
        },
    },
    {
        "name": "rhino_state",
        "description": (
            "Read the open Rhino document: name, units, tolerance, object "
            "count, and layer names. Call this before assuming what is open — "
            "especially at the start of a task, or when the person refers to "
            "something that already exists."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "rhino_screenshot",
        "description": (
            "Look at the Rhino viewport. Returns the image, so use it to check "
            "your own work after building something and to see what the person "
            "is describing. Prefer looking over guessing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "view": {
                    "type": "string",
                    "enum": ["perspective", "top", "front", "right", "fit"],
                    "description": "Which view to capture. Defaults to perspective.",
                },
            },
        },
    },
]


def call_tool(name, arguments):
    """Run one tool. Returns MCP content blocks."""
    if name == "rhino_execute":
        code = (arguments or {}).get("code") or ""
        if not code.strip():
            return _text("no code given", error=True)
        out = submit("execute", {"code": code})
        if not out.get("ok"):
            return _text(out.get("traceback") or out.get("error") or "failed",
                         error=True)
        parts = []
        if out.get("stdout"):
            parts.append(out["stdout"].rstrip())
        if out.get("result") is not None:
            parts.append("result = " + json.dumps(out["result"], default=str))
        return _text("\n".join(parts) if parts else "done (no output)")

    if name == "rhino_state":
        out = submit("state", {}, timeout=30)
        if not out.get("ok"):
            return _text(out.get("error") or "failed", error=True)
        return _text(json.dumps(out, indent=2, default=str))

    if name == "rhino_screenshot":
        view = (arguments or {}).get("view") or "perspective"
        out = submit("screenshot",
                     {"view": view, "width": 1200, "height": 800}, timeout=90)
        if not out.get("ok"):
            return _text(out.get("error") or "capture failed", error=True)
        # An image block, not a file path - the point is for Claude to SEE the
        # model and correct itself, which a path it cannot open does not give.
        return {"content": [{"type": "image",
                             "data": out["png_base64"],
                             "mimeType": "image/png"}],
                "isError": False}

    return _text(f"unknown tool '{name}'", error=True)


def _text(text, error=False):
    return {"content": [{"type": "text", "text": text}], "isError": error}


# --------------------------------------------------------------------------
# The protocol. JSON-RPC 2.0, one message per line, over stdin/stdout.
# --------------------------------------------------------------------------


def handle(message):
    """Return a response dict, or None for notifications (which get no reply)."""
    method = message.get("method")
    ident = message.get("id")

    # Notifications have no id and must never be answered - replying to one is
    # a protocol violation that some clients treat as fatal.
    if ident is None:
        return None

    if method == "initialize":
        return _ok(ident, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })

    if method == "tools/list":
        return _ok(ident, {"tools": TOOLS})

    if method == "tools/call":
        params = message.get("params") or {}
        try:
            result = call_tool(params.get("name"), params.get("arguments"))
        except Exception as exc:                             # noqa: BLE001
            # A failing tool is a result, not a transport error: Claude should
            # see what went wrong and try something else, not have the session
            # torn down.
            result = _text(f"{type(exc).__name__}: {exc}", error=True)
        return _ok(ident, result)

    if method == "ping":
        return _ok(ident, {})

    return {"jsonrpc": "2.0", "id": ident,
            "error": {"code": -32601, "message": f"unknown method '{method}'"}}


def _ok(ident, result):
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def main():
    # Binary-safe line IO. stdout carries the protocol, so nothing else may
    # ever be printed there - a stray print corrupts the stream and the client
    # drops the server with no useful message.
    stdin = sys.stdin
    stdout = sys.stdout

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue                                          # not our problem
        try:
            response = handle(message)
        except Exception as exc:                              # noqa: BLE001
            response = {"jsonrpc": "2.0", "id": message.get("id"),
                        "error": {"code": -32603, "message": str(exc)}}
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
