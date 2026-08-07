"""A stand-in for Rhino: claims jobs and answers with canned geometry results.

Lets the whole chain be exercised without a Windows machine or a CAD licence -
claude -> MCP server -> broker -> (this) -> back. What it cannot prove is
RhinoCommon itself; everything between Claude and Rhino's front door it can.
"""
import json, os, sys, threading, time, urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../../../../server/src"))
BROKER = os.environ.get("FUSION_BROKER_URL", "http://127.0.0.1:7699")
TOKEN = os.environ["FUSION_BRIDGE_TOKEN"]   # no relative path to break

# a 1x1 transparent png, so screenshot returns a real decodable image
PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
       "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")

def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BROKER + path, data=data, method=method)
    r.add_header("X-Fusion-Bridge-Token", TOKEN)
    if data: r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=30) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None

def run():
    while True:
        try:
            job = call("GET", "/claim?wait=5")
        except Exception:
            time.sleep(0.5); continue
        if not job:
            continue
        kind = job["kind"]
        if kind == "state":
            result = {"ok": True, "document": "(unsaved)", "units": "Millimeters",
                      "tolerance": 0.001, "objects": 0, "layers": ["Default"]}
        elif kind == "execute":
            code = job["payload"].get("code", "")
            result = {"ok": True, "result": {"ran": True, "chars": len(code)},
                      "stdout": "stub executed\n"}
        elif kind == "screenshot":
            result = {"ok": True, "view": job["payload"].get("view", "perspective"),
                      "width": 1200, "height": 800, "png_base64": PNG, "bytes": 70}
        else:
            result = {"ok": False, "error": "unknown kind"}
        try:
            call("POST", "/result", {"id": job["id"], "result": result})
        except Exception:
            pass

threading.Thread(target=run, daemon=True).start()
print("stub poller running", flush=True)
while True:
    time.sleep(5)
