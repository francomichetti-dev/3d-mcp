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

# Memory is optional on purpose. This server runs on Rhino's own interpreter
# from whatever directory the MCP client launches it in, so the import is made
# to work by path rather than assumed — and if it still fails, the modelling
# tools carry on without it instead of the whole server refusing to start.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import rhino_memory
except ImportError:
    rhino_memory = None

BROKER = (os.environ.get("ARGES_BROKER_URL")
          or os.environ.get("FUSION_BROKER_URL") or "http://127.0.0.1:7656")

HOME = os.path.expanduser("~")
STATE_DIR_NAME = ".arges"
# Pre-rename directory. Preferred order is new-then-old and nothing here moves
# anything: the Rhino half ships as a zip and updates on its own schedule, so a
# machine routinely runs one half newer than the other. Only `arges install`
# migrates. See server/src/arges_mcp/server.py.
LEGACY_STATE_DIR_NAME = ".fusion-mcp"


def _state_dir():
    current = os.path.join(HOME, STATE_DIR_NAME)
    legacy = os.path.join(HOME, LEGACY_STATE_DIR_NAME)
    if not os.path.isdir(current) and os.path.isdir(legacy):
        return legacy
    return current


TOKEN_PATH = os.path.join(_state_dir(), "token")
AUTH_HEADER = "X-Arges-Bridge-Token"
# Sent alongside the current one, same value, so this works against a broker
# that has not been updated yet. Servers accept either; clients send both.
LEGACY_AUTH_HEADER = "X-Fusion-Bridge-Token"

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "rhino", "version": "0.1.0"}


# --------------------------------------------------------------------------
# The bridge
# --------------------------------------------------------------------------


def _token():
    # Env first, mirroring ARGES_BROKER_URL. Lets the server be pointed at a
    # test broker without touching the real token file, and covers installs
    # where the token does not live under HOME.
    from_env = (os.environ.get("ARGES_BRIDGE_TOKEN")
                or os.environ.get("FUSION_BRIDGE_TOKEN"))
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
    token = _token()
    request.add_header(AUTH_HEADER, token)
    request.add_header(LEGACY_AUTH_HEADER, token)
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

SCREENSHOT_VIEWS = ("perspective", "top", "front", "right", "fit")

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
            "After changing geometry, LOOK at it before saying it is done: "
            "pass screenshot=\"perspective\" (or another view) and the picture "
            "comes back with the result in the same call — one round trip "
            "instead of two. The capture runs only if the code succeeded, and "
            "a capture problem never fails the call. Use rhino_screenshot only "
            "for a second angle or a look without running code."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string",
                         "description": "Python to run inside Rhino."},
                "screenshot": {
                    "type": "string",
                    "enum": list(SCREENSHOT_VIEWS),
                    "description": ("Also capture this view once the code "
                                    "succeeds and return the image with the "
                                    "result."),
                },
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
    {
        "name": "rhino_remember",
        "description": (
            "Save something worth knowing the next time this project is "
            "opened — what they are building, a decision and why, a dimension "
            "that matters, or what is still left to do.\n\n"
            "Memory is per PROJECT, and Rhino's incremental saves "
            "(chair.3dm, chair001.3dm, chair_v2.3dm) are treated as the same "
            "project, so a note written on one version is there on the next.\n\n"
            "Call this when something is decided or finished, not for every "
            "message. One fact per call, written so it still makes sense in a "
            "month. Do not save what reading the file would tell you."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "note": {"type": "string",
                         "description": "One durable fact, in the person's language."},
            },
            "required": ["note"],
        },
    },
    {
        "name": "rhino_recall",
        "description": (
            "Look up what you know about a project. With no argument it "
            "lists every project seen; with `project` it returns that "
            "project's notes.\n\n"
            "Use it when the person refers to a different model than the one "
            "open — \"like the lamp I did\" — so you can answer from what was "
            "actually recorded instead of guessing. The open project's memory "
            "is already given to you each turn; you do not need this for that."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "Project name to look up. Omit to list all."},
            },
        },
    },
]


def _current_project():
    """Identify the open document. Returns an info dict, or None if unreachable."""
    if rhino_memory is None:
        return None
    state = submit("state", {}, timeout=30)
    if not state.get("ok"):
        return None
    return rhino_memory.describe(state.get("path") or "",
                                 state.get("document") or "")


def call_tool(name, arguments):
    """Run one tool. Returns MCP content blocks."""
    if name == "rhino_execute":
        code = (arguments or {}).get("code") or ""
        if not code.strip():
            return _text("no code given", error=True)
        view = (arguments or {}).get("screenshot")
        # An unknown view is refused BEFORE the code runs. Refusing it after
        # would leave the geometry changed under a call that reported failure,
        # which is the worst possible reading of an error.
        if view is not None and view not in SCREENSHOT_VIEWS:
            return _text("unknown screenshot view '%s' — valid views: %s. "
                         "The code was NOT run."
                         % (view, ", ".join(SCREENSHOT_VIEWS)), error=True)
        out = submit("execute", {"code": code})
        if not out.get("ok"):
            # A failing script wants its traceback read, not photographed.
            return _text(out.get("traceback") or out.get("error") or "failed",
                         error=True)
        parts = []
        if out.get("stdout"):
            parts.append(out["stdout"].rstrip())
        if out.get("result") is not None:
            parts.append("result = " + json.dumps(out["result"], default=str))
        text = "\n".join(parts) if parts else "done (no output)"
        if view is None:
            return _text(text)
        shot = submit("screenshot",
                      {"view": view, "width": 1200, "height": 800}, timeout=90)
        if not shot.get("ok"):
            # The code SUCCEEDED — an error here would read as "the script
            # failed" and push the model into re-running code that already
            # changed the document.
            return _text(text + "\n(the code ran fine, but the screenshot "
                         "failed: %s)" % (shot.get("error") or "capture failed"))
        return {"content": [{"type": "text", "text": text},
                            {"type": "image", "data": shot["png_base64"],
                             "mimeType": "image/png"}],
                "isError": False}

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

    if name == "rhino_remember":
        if rhino_memory is None:
            return _text("memory is not available", error=True)
        note = ((arguments or {}).get("note") or "").strip()
        if not note:
            return _text("nothing to remember", error=True)
        info = _current_project()
        if info is None:
            return _text("cannot tell which project is open — is Rhino "
                         "connected?", error=True)
        rhino_memory.touch(info)
        if not rhino_memory.add_note(info["key"], note, info.get("version")):
            return _text("could not write the note", error=True)
        return _text("remembered for %s" % info["title"])

    if name == "rhino_recall":
        if rhino_memory is None:
            return _text("memory is not available", error=True)
        wanted = ((arguments or {}).get("project") or "").strip()
        index = rhino_memory.load_index()
        if not wanted:
            if not index:
                return _text("no projects recorded yet")
            rows = sorted(index.items(),
                          key=lambda kv: kv[1].get("last_seen", ""), reverse=True)
            return _text("\n".join(
                "%s — last %s" % (meta.get("title", key), meta.get("last_seen", "?"))
                for key, meta in rows))
        lowered = wanted.lower()
        # Match on the title people actually say, not the internal key.
        for key, meta in index.items():
            title = (meta.get("title") or "").lower()
            if lowered == title or lowered in title or title in lowered:
                notes = rhino_memory.read_notes(key)
                head = "%s (last %s)" % (meta.get("title", key),
                                         meta.get("last_seen", "?"))
                versions = meta.get("versions") or []
                if versions:
                    head += "\nversions: %s" % ", ".join(versions[-8:])
                return _text(head + ("\n\n" + notes if notes else
                                     "\n\n(no notes saved for it)"))
        return _text("nothing recorded for '%s'" % wanted)

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
    # stdout carries the protocol, so nothing else may ever be printed there -
    # a stray print corrupts the stream and the client drops the server with no
    # useful message.
    #
    # Force UTF-8 on both directions. A piped child on Windows gets the locale
    # encoding, measured as cp1252 on the target machine, while MCP is UTF-8
    # JSON throughout. Being a single-byte codec, cp1252 does not fail loudly:
    # it silently turns an em-dash into three characters, so Spanish code
    # comments and accented layer names come back as mojibake - and the bytes
    # 0x81/0x8d/0x8f/0x90/0x9d are undefined in it and raise outright.
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass                                              # already UTF-8
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
            try:
                # ensure_ascii keeps every byte on the wire in the 7-bit range,
                # so the transport cannot be broken by content even if some
                # layer between here and the client is not UTF-8 clean.
                stdout.write(json.dumps(response, ensure_ascii=True) + "\n")
                stdout.flush()
            except (BrokenPipeError, ValueError):
                # The client went away mid-write. Nothing to report it to.
                break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
