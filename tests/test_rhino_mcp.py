"""The Rhino MCP server — protocol, tools, and the failure paths.

This is the piece Claude Code talks to, and it was hand-verified rather than
tested. Everything else in the repo has a suite; a protocol server that has
only ever been checked by driving it manually is one edit away from silently
breaking, and the symptom would be "Claude can't see Rhino" with no error
anyone can read.

Covers what is cheap to get wrong and expensive to notice:

  * notifications must get NO reply (answering one is a protocol violation)
  * a failing tool is a RESULT, not a crash - Claude should adapt, not have the
    session torn down
  * stdout carries the protocol, so nothing else may ever be written there
  * UTF-8, which cost a real bug on Windows twice today

    python3 tests/test_rhino_mcp.py
"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "scripts" / "rhino" / "rhino_mcp.py"

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {label}\n       got  {got!r}\n       want {want!r}")


def truthy(label, got):
    check(label, bool(got), True)


# --------------------------------------------------------------------------
# A fake broker, so the tools have something to talk to without Rhino.
# --------------------------------------------------------------------------

TOKEN = "t" * 40
PNG_1PX = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
           "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")

_seen = {"tokens": [], "jobs": []}
_mode = {"reply": "ok"}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):                                       # noqa: N802
        _seen["tokens"].append(self.headers.get("X-Fusion-Bridge-Token"))
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _seen["jobs"].append(body)

        if _mode["reply"] == "401":
            self.send_response(401)
            self.end_headers()
            return
        if _mode["reply"] == "garbage":
            payload = b"not json at all"
        else:
            kind = body["kind"]
            if kind == "state":
                out = {"ok": True, "document": "cajón.3dm", "units": "Millimeters",
                       "objects": 3, "layers": ["Default", "año"]}
            elif kind == "execute":
                if _mode["reply"] == "boom":
                    out = {"ok": False, "traceback": "ZeroDivisionError: division by zero"}
                else:
                    out = {"ok": True, "result": {"hecho": True},
                           "stdout": "listo — 20°\n"}
            elif kind == "screenshot":
                if _mode["reply"] == "shot-fail":
                    out = {"ok": False, "error": "la vista no existe"}
                else:
                    out = {"ok": True, "view": body["payload"].get("view"),
                           "width": 1200, "height": 800, "png_base64": PNG_1PX}
            else:
                out = {"ok": False, "error": "unknown kind"}
            payload = json.dumps(out).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


import socket  # noqa: E402

with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    PORT = probe.getsockname()[1]

_broker = HTTPServer(("127.0.0.1", PORT), _Handler)
threading.Thread(target=_broker.serve_forever, daemon=True).start()
time.sleep(0.2)


# --------------------------------------------------------------------------
# Driving the server the way Claude Code does: one JSON line at a time.
# --------------------------------------------------------------------------


class Server:
    def __init__(self, token=TOKEN):
        env = dict(os.environ)
        env["FUSION_BROKER_URL"] = f"http://127.0.0.1:{PORT}"
        env["FUSION_BRIDGE_TOKEN"] = token
        # HOME is redirected so a token file on the developer's machine can
        # never leak into a test run and make a failure look like a pass.
        env["HOME"] = str(REPO / "tests" / "_nonexistent_home")
        env["USERPROFILE"] = env["HOME"]
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self._id = 0

    def send(self, message, expect_reply=True):
        self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        if not expect_reply:
            return None
        line = self.proc.stdout.readline()
        return json.loads(line.decode("utf-8")) if line else None

    def call(self, name, arguments=None):
        self._id += 1
        reply = self.send({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments or {}}})
        return reply["result"]

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
        return self.proc.returncode


# ----------------------------------------------------------------- protocol --
print("The handshake")
s = Server()
reply = s.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
check("initialize answers with its id", reply["id"], 1)
check("names itself", reply["result"]["serverInfo"]["name"], "rhino")
truthy("declares a protocol version", reply["result"]["protocolVersion"])
truthy("advertises tools", "tools" in reply["result"]["capabilities"])

# The one that is easy to get wrong: a notification has no id and must never be
# answered. A stray reply desynchronises the stream and strict clients drop the
# server without saying why.
s.send({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_reply=False)
reply = s.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
check("a notification gets no reply, so ping still lines up", reply["id"], 2)
check("ping returns an empty result", reply["result"], {})

reply = s.send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
tools = reply["result"]["tools"]
check("three tools", sorted(t["name"] for t in tools),
      ["rhino_execute", "rhino_screenshot", "rhino_state"])
truthy("every tool has a schema", all("inputSchema" in t for t in tools))
truthy("every tool has a description", all(len(t.get("description", "")) > 40 for t in tools))
# The description is what Claude reads to decide whether to call it, so it must
# say WHEN, not only what.
execute = next(t for t in tools if t["name"] == "rhino_execute")
truthy("execute says when to use it", "whenever" in execute["description"].lower())
truthy("execute names what is in scope", "rhinoscriptsyntax" in execute["description"])

reply = s.send({"jsonrpc": "2.0", "id": 4, "method": "frobnicate"})
check("unknown method is a JSON-RPC error", reply["error"]["code"], -32601)
check("clean exit", s.close(), 0)


# -------------------------------------------------------------------- tools --
print("Tools against a working broker")
s = Server()
s.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

result = s.call("rhino_state")
check("state is not an error", result["isError"], False)
state = json.loads(result["content"][0]["text"])
check("state passes the document through", state["document"], "cajón.3dm")
check("and non-ASCII layer names survive", state["layers"][1], "año")

result = s.call("rhino_execute", {"code": "result = 1"})
text = result["content"][0]["text"]
check("execute is not an error", result["isError"], False)
truthy("stdout is surfaced", "listo" in text)
truthy("non-ASCII in stdout survives", "—" in text and "20°" in text)
truthy("the returned value is surfaced", "hecho" in text)

result = s.call("rhino_screenshot", {"view": "top"})
block = result["content"][0]
check("screenshot returns an image block, not a path", block["type"], "image")
check("with a media type", block["mimeType"], "image/png")
check("and the image data", block["data"], PNG_1PX)
check("the requested view is passed through", _seen["jobs"][-1]["payload"]["view"], "top")

# Execute + screenshot in one call — parity with the Fusion server. The
# expensive half of look-then-correct is the extra model turn between the two
# calls, not the capture itself.
jobs_before = len(_seen["jobs"])
result = s.call("rhino_execute", {"code": "result = 1", "screenshot": "front"})
check("combined call is not an error", result["isError"], False)
check("two blocks come back", len(result["content"]), 2)
check("the first is the text result", result["content"][0]["type"], "text")
truthy("still carrying the output", "hecho" in result["content"][0]["text"])
check("the second is the picture", result["content"][1]["type"], "image")
check("of the requested view", _seen["jobs"][-1]["payload"]["view"], "front")
check("from exactly two jobs, execute then screenshot",
      [j["kind"] for j in _seen["jobs"][jobs_before:]], ["execute", "screenshot"])

check("the token is sent on every call", set(_seen["tokens"]), {TOKEN})
check("clean exit", s.close(), 0)


# ------------------------------------------------------------ failure paths --
# A failing tool must come back as a RESULT. Raising would end the session, and
# Claude can do something useful with an error it can read.
print("When things go wrong")
s = Server()
s.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

result = s.call("rhino_execute", {"code": "   "})
check("empty code is refused", result["isError"], True)
truthy("and says so", "no code" in result["content"][0]["text"])

result = s.call("nope", {})
check("an unknown tool is an error result, not a crash", result["isError"], True)
truthy("naming the tool", "nope" in result["content"][0]["text"])

_mode["reply"] = "boom"
result = s.call("rhino_execute", {"code": "1/0"})
check("a Rhino traceback comes back as an error result", result["isError"], True)
truthy("with the traceback intact",
       "ZeroDivisionError" in result["content"][0]["text"])

# The combined call's three failure semantics. A failing script is not
# photographed — its traceback is the story and the geometry may be
# half-changed:
jobs_before = len(_seen["jobs"])
result = s.call("rhino_execute", {"code": "1/0", "screenshot": "top"})
check("a failing script with a screenshot asked is the error alone",
      result["isError"], True)
check("and was never photographed",
      [j["kind"] for j in _seen["jobs"][jobs_before:]], ["execute"])

# A capture failure after a successful run must not read as a script failure,
# or the model re-runs code that already changed the document:
_mode["reply"] = "shot-fail"
result = s.call("rhino_execute", {"code": "result = 1", "screenshot": "top"})
check("a capture failure does not fail the call", result["isError"], False)
truthy("the result text still arrives", "hecho" in result["content"][0]["text"])
truthy("with the capture problem attached",
       "screenshot failed" in result["content"][0]["text"])

# And an unknown view is refused BEFORE the code runs — "iso" is the Fusion
# name, exactly the mistake a model that knows both CADs will make:
_mode["reply"] = "ok"
jobs_before = len(_seen["jobs"])
result = s.call("rhino_execute", {"code": "result = 1", "screenshot": "iso"})
check("an unknown view is refused", result["isError"], True)
truthy("saying the code did not run", "NOT run" in result["content"][0]["text"])
check("with nothing sent to the broker", len(_seen["jobs"]), jobs_before)

_mode["reply"] = "401"
result = s.call("rhino_state")
check("a rejected token is an error result", result["isError"], True)
truthy("that says what to do", "token" in result["content"][0]["text"].lower())

_mode["reply"] = "garbage"
result = s.call("rhino_state")
check("a broker talking nonsense does not crash the server", result["isError"], True)

_mode["reply"] = "ok"
result = s.call("rhino_state")
check("and it recovers afterwards", result["isError"], False)
check("clean exit", s.close(), 0)


# ------------------------------------------------------- broker unreachable --
print("With no broker at all")
env = dict(os.environ)
env["FUSION_BROKER_URL"] = "http://127.0.0.1:1"          # nothing listens here
env["FUSION_BRIDGE_TOKEN"] = TOKEN
proc = subprocess.Popen([sys.executable, str(SERVER)], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {}}).encode() + b"\n")
proc.stdin.flush()
proc.stdout.readline()
proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "rhino_state", "arguments": {}}}).encode() + b"\n")
proc.stdin.flush()
reply = json.loads(proc.stdout.readline().decode())
check("unreachable broker is an error result", reply["result"]["isError"], True)
message = reply["result"]["content"][0]["text"]
truthy("that tells the person what to start", "broker" in message.lower())
truthy("and mentions the poller", "poller" in message.lower())
proc.stdin.close()
check("clean exit", proc.wait(timeout=10), 0)


# ------------------------------------------------------------ stream hygiene --
# stdout carries the protocol. Anything else written there corrupts the stream
# and the client drops the server with no useful message.
print("Stream hygiene")
s = Server()
s.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
s.call("rhino_state")
s.call("rhino_execute", {"code": "result = 1"})
s.proc.stdin.close()
remainder = s.proc.stdout.read().decode("utf-8", "replace").strip()
check("nothing extra is left on stdout", remainder, "")
stderr = s.proc.stderr.read().decode("utf-8", "replace")
truthy("no traceback leaked to stderr", "Traceback" not in stderr)
check("clean exit", s.proc.wait(timeout=10), 0)


# ------------------------------------------------------------------ garbage --
print("Malformed input")
s = Server()
s.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
# A line that is not JSON is skipped rather than being fatal - one bad frame
# should not end a modelling session.
s.proc.stdin.write(b"this is not json\n")
s.proc.stdin.flush()
s.proc.stdin.write(b"\n")                                     # and a blank line
s.proc.stdin.flush()
reply = s.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
check("garbage lines are skipped, the session continues", reply["id"], 2)
check("clean exit", s.close(), 0)

_broker.shutdown()
print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
