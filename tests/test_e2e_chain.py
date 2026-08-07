"""The whole Rhino chain, end to end, with no CAD installed.

    claude  ──MCP/stdio──▶  rhino_mcp.py  ──HTTP──▶  broker  ◀──  poller

Every other suite tests one link with the next one stubbed. This runs three
real processes against each other and drives them the way Claude Code does, so
it covers the joins - the parts no unit test can see:

  * the MCP server and the broker agreeing on the job envelope
  * the token travelling all the way through
  * a poller's canned answer arriving back as a valid MCP result
  * a PNG surviving base64 -> broker -> MCP image block

The README already claimed this was possible ("tests/e2e/ additionally stands
in for Rhino"). It was possible, but nothing did it, so the join was never
actually exercised - and the stub carried a path that walked six directories
above the repo, which nobody had noticed because nothing ran it.

Rhino itself is still out of scope: RhinoCommon is what the stub replaces.

    python3 tests/test_e2e_chain.py
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server" / "src"))

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


from arges_mcp import broker as bk  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="e2e-chain-"))
TOKEN = "e" * 64
(TMP / "token").write_text(TOKEN, encoding="utf-8")
bk.TOKEN_PATH = TMP / "token"

with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    PORT = probe.getsockname()[1]
bk.ALLOWED_HOSTS = frozenset((f"127.0.0.1:{PORT}", f"localhost:{PORT}"))
BROKER_URL = f"http://127.0.0.1:{PORT}"

server, broker = bk.serve(bk.Broker(), port=PORT)

env = dict(os.environ)
env["FUSION_BROKER_URL"] = BROKER_URL
env["FUSION_BRIDGE_TOKEN"] = TOKEN

poller = subprocess.Popen([sys.executable, str(REPO / "tests" / "e2e" / "stub_poller.py")],
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
mcp = subprocess.Popen([sys.executable, str(REPO / "scripts" / "rhino" / "rhino_mcp.py")],
                       env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)

_id = [0]


def rpc(method, params=None, expect_reply=True):
    _id[0] += 1
    message = {"jsonrpc": "2.0", "id": _id[0], "method": method}
    if params is not None:
        message["params"] = params
    mcp.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
    mcp.stdin.flush()
    if not expect_reply:
        return None
    line = mcp.stdout.readline()
    return json.loads(line.decode("utf-8")) if line else None


def tool(name, arguments=None):
    return rpc("tools/call", {"name": name, "arguments": arguments or {}})["result"]


try:
    # The poller has to check in before anything can be submitted: the broker
    # fails a submit fast when no CAD is connected, rather than making the
    # caller wait out the full timeout for something that is not there.
    print("The poller connects")
    connected = False
    for _ in range(100):
        if broker.poller_connected():
            connected = True
            break
        time.sleep(0.1)
    truthy("the stub poller reaches the broker", connected)

    print("The chain answers")
    reply = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    check("the MCP server initializes", reply["result"]["serverInfo"]["name"], "rhino")

    # rhino_state, all the way to the poller and back.
    result = tool("rhino_state")
    check("state is not an error", result["isError"], False)
    state = json.loads(result["content"][0]["text"])
    check("the poller's document reaches Claude", state["document"], "(unsaved)")
    check("and its units", state["units"], "Millimeters")

    # rhino_execute: the code must arrive intact at the far end. The stub
    # reports the length it received, which is the cheapest possible proof
    # that the payload was not mangled in transit.
    code = "import rhinoscriptsyntax as rs\nrs.AddSphere((0, 0, 0), 12.5)\n"
    result = tool("rhino_execute", {"code": code})
    check("execute is not an error", result["isError"], False)
    text = result["content"][0]["text"]
    truthy("the poller's stdout comes back", "stub executed" in text)
    truthy("and the code arrived intact", str(len(code)) in text)

    # A screenshot has to survive base64 through two hops and arrive as a real
    # image block, not a path and not text.
    result = tool("rhino_screenshot", {"view": "top"})
    block = result["content"][0]
    check("a screenshot returns an image block", block["type"], "image")
    check("with a media type", block["mimeType"], "image/png")
    truthy("and decodable data", block["data"].startswith("iVBORw0KGgo"))

    print("Single flight holds across the chain")
    # The broker is single-flight. Two submits cannot overlap here because the
    # MCP server is synchronous, so what is checked is that a second call after
    # the first completes still works - the slot is released, not leaked.
    result = tool("rhino_state")
    check("a second call after the first still works", result["isError"], False)

    print("Failure travels back as a result")
    result = tool("rhino_execute", {"code": "   "})
    check("empty code is refused as a result, not a crash", result["isError"], True)
    result = tool("rhino_state")
    check("and the chain still works afterwards", result["isError"], False)

finally:
    for process in (mcp, poller):
        try:
            if process is mcp and process.stdin:
                process.stdin.close()
            process.terminate()
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    server.shutdown()
    server.server_close()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
