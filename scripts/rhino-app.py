"""A standalone desktop window for driving Rhino through the broker.

Not a panel inside Rhino, deliberately. Rhino crashed five times during this
spike; a UI living inside it dies with it and takes the conversation along. This
is a separate process that reconnects when Rhino comes back, so the window
survives a crash that the CAD does not.

    broker (127.0.0.1:7656)  <--  this window
             ^
             |  poller (a UI timer inside Rhino)

Every failure mode seen today is handled explicitly rather than left to a
traceback: broker not running, Rhino closed, poller stopped, a job that times
out, a request that arrives while another is still running.

Run it with Rhino's own Python - nothing else needs installing:

    %USERPROFILE%\\.rhinocode\\py39-rh8\\python.exe rhino-app.py
"""

import base64
import json
import os
import threading
import time
import traceback
import urllib.error
import urllib.request

BROKER = os.environ.get("FUSION_BROKER_URL") or "http://127.0.0.1:7656"
TOKEN_PATH = os.path.join(os.path.expanduser("~"), ".fusion-mcp", "token")
AUTH_HEADER = "X-Fusion-Bridge-Token"


def _token():
    try:
        with open(TOKEN_PATH, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _call(method, path, body=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BROKER + path, data=data, method=method)
    request.add_header(AUTH_HEADER, _token())
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


class Api:
    """Everything the window can ask for. Exposed to JavaScript by pywebview.

    No method here raises: the window must never be left staring at a spinner
    because something threw. Each returns a dict the page can render, including
    for the failure cases.
    """

    def __init__(self):
        self._lock = threading.Lock()

    # -- status ---------------------------------------------------------- #

    def status(self):
        """Is the broker up, and is Rhino's poller checked in?"""
        if not _token():
            return {"state": "no-token",
                    "detail": f"no token at {TOKEN_PATH}"}
        try:
            health = _call("GET", "/health", timeout=5)
        except (urllib.error.URLError, OSError, ValueError):
            return {"state": "no-broker",
                    "detail": f"nothing answering on {BROKER}"}
        if not health.get("poller_connected"):
            return {"state": "no-rhino",
                    "detail": "broker is up, but Rhino's poller is not checked in",
                    "health": health}
        return {"state": "ready", "health": health}

    # -- jobs ------------------------------------------------------------ #

    def _submit(self, kind, payload, timeout=90):
        # Single-flight is enforced by the broker, but serialising here too
        # keeps the window from firing a second request the moment someone
        # double-clicks, which would only earn a confusing "busy".
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "error": "already working on something"}
        try:
            return _call("POST", "/submit",
                         {"kind": kind, "payload": payload}, timeout=timeout)
        except urllib.error.URLError as exc:
            return {"ok": False,
                    "error": f"cannot reach the broker - is it running? ({exc.reason})"}
        except OSError as exc:
            return {"ok": False, "error": f"connection failed: {exc}"}
        except ValueError:
            return {"ok": False, "error": "the broker sent something that was not JSON"}
        except Exception as exc:                             # noqa: BLE001
            return {"ok": False, "error": f"unexpected: {exc!r}"}
        finally:
            self._lock.release()

    def state(self):
        return self._submit("state", {}, timeout=30)

    def screenshot(self, view="perspective", width=1100, height=740):
        result = self._submit("screenshot",
                              {"view": view, "width": int(width),
                               "height": int(height)}, timeout=60)
        # Hand the page a data URI so it can render without a second request.
        if result.get("ok") and result.get("png_base64"):
            result["data_uri"] = "data:image/png;base64," + result["png_base64"]
            del result["png_base64"]           # keep the payload out of the DOM twice
        return result

    def execute(self, code):
        if not (code or "").strip():
            return {"ok": False, "error": "nothing to run"}
        return self._submit("execute", {"code": code}, timeout=90)


PAGE = r"""
<!doctype html><html><head><meta charset="utf-8"><title>Rhino</title><style>
:root{--bg:#23272b;--panel:#2c3136;--line:#3d434a;--fg:#e6e8ea;--dim:#98a0a8;
      --accent:#6cc0ff;--ok:#7ddc9a;--warn:#ffb454;--err:#ff6b6b;}
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--fg);display:flex;flex-direction:column;
     font:13px/1.5 -apple-system,"Segoe UI",sans-serif}
header{display:flex;align-items:center;gap:10px;padding:9px 12px;
       border-bottom:1px solid var(--line);flex:none}
.dot{width:9px;height:9px;border-radius:50%;background:var(--dim);flex:none}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.err{background:var(--err)}
.name{font-weight:600;letter-spacing:.03em}
#detail{color:var(--dim);font-size:12px;flex:1;overflow:hidden;text-overflow:ellipsis;
        white-space:nowrap}
button{background:transparent;border:1px solid var(--line);color:var(--dim);
       border-radius:5px;padding:4px 10px;font-size:12px;cursor:pointer}
button:hover:not(:disabled){color:var(--fg);border-color:var(--dim)}
button:disabled{opacity:.4;cursor:default}
#main{flex:1;display:flex;min-height:0}
#left{flex:1;display:flex;flex-direction:column;min-width:0;border-right:1px solid var(--line)}
#shot{flex:1;display:flex;align-items:center;justify-content:center;overflow:hidden;
      background:#1b1f22}
#shot img{max-width:100%;max-height:100%;display:block}
#shot .empty{color:var(--dim);font-style:italic}
#right{width:46%;display:flex;flex-direction:column;min-width:340px}
textarea{flex:1;background:var(--panel);color:var(--fg);border:0;
         border-bottom:1px solid var(--line);padding:10px;resize:none;
         font:12px/1.5 ui-monospace,Consolas,monospace}
textarea:focus{outline:none}
#out{height:38%;overflow:auto;padding:10px;font:12px/1.5 ui-monospace,Consolas,monospace;
     white-space:pre-wrap;word-break:break-word}
.err{color:var(--err)}.ok{color:var(--ok)}.dim{color:var(--dim)}
footer{display:flex;gap:6px;padding:8px;border-top:1px solid var(--line);flex:none;
       align-items:center}
select{background:var(--panel);color:var(--fg);border:1px solid var(--line);
       border-radius:5px;padding:4px 6px;font:inherit}
#run{background:var(--accent);color:#0d2233;border:0;font-weight:600;padding:6px 14px}
</style></head><body>
<header>
  <span class="dot" id="dot"></span><span class="name">Rhino</span>
  <span id="detail">connecting…</span>
  <button id="refresh">Refresh</button>
</header>
<div id="main">
  <div id="left"><div id="shot"><span class="empty">no capture yet</span></div></div>
  <div id="right">
    <textarea id="code" spellcheck="false" placeholder="Rhino Python — `Rhino`, `rs`, `doc` are in scope. Assign to `result` to send a value back.">import Rhino
doc = Rhino.RhinoDoc.ActiveDoc
result = {"objects": doc.Objects.Count, "units": str(doc.ModelUnitSystem)}</textarea>
    <div id="out"><span class="dim">output appears here</span></div>
  </div>
</div>
<footer>
  <select id="view">
    <option value="perspective">perspective</option>
    <option value="top">top</option>
    <option value="front">front</option>
    <option value="right">right</option>
    <option value="fit">fit</option>
  </select>
  <button id="capture">Capture</button>
  <button id="state">State</button>
  <span style="flex:1"></span>
  <button id="run">Run</button>
</footer>
<script>
const $ = (id) => document.getElementById(id);
let busy = false;

function setBusy(on){
  busy = on;
  ["run","capture","state","refresh"].forEach(id => $(id).disabled = on);
}
function out(text, cls){
  $("out").innerHTML = "";
  const span = document.createElement("span");
  if (cls) span.className = cls;
  span.textContent = text;
  $("out").appendChild(span);
}

// Status is polled rather than pushed: Rhino can vanish without warning, and a
// window that keeps claiming "ready" after it dies is worse than a slow one.
async function refresh(){
  let s;
  try { s = await window.pywebview.api.status(); }
  catch (e) { s = {state:"no-broker", detail:String(e)}; }
  const dot = $("dot"), detail = $("detail");
  dot.className = "dot";
  if (s.state === "ready"){
    dot.classList.add("ok");
    const h = s.health || {};
    detail.textContent = `connected · ${h.completed||0} jobs done`;
  } else if (s.state === "no-rhino"){
    dot.classList.add("warn");
    detail.textContent = "Rhino not connected — open Rhino, run ScriptEditor, then start the poller";
  } else if (s.state === "no-broker"){
    dot.classList.add("err");
    detail.textContent = "broker not running — start broker-service.ps1";
  } else {
    dot.classList.add("err");
    detail.textContent = s.detail || "not ready";
  }
  return s.state;
}

async function guard(fn){
  if (busy) return;
  setBusy(true);
  try { await fn(); }
  catch (e){ out("the window itself failed: " + e, "err"); }
  finally { setBusy(false); refresh(); }
}

$("run").onclick = () => guard(async () => {
  out("running…", "dim");
  const r = await window.pywebview.api.execute($("code").value);
  if (r.ok){
    let text = "";
    if (r.stdout) text += r.stdout;
    if (r.result !== undefined && r.result !== null)
      text += (text ? "\n" : "") + JSON.stringify(r.result, null, 2);
    out(text || "(no output)", "ok");
    // Anything that ran probably changed geometry; show it without being asked.
    const shot = await window.pywebview.api.screenshot($("view").value);
    if (shot.ok) showShot(shot.data_uri);
  } else {
    out(r.traceback || r.error || "failed", "err");
  }
});

$("capture").onclick = () => guard(async () => {
  out("capturing…", "dim");
  const r = await window.pywebview.api.screenshot($("view").value);
  if (r.ok){ showShot(r.data_uri); out(`${r.view} ${r.width}x${r.height}`, "dim"); }
  else out(r.error || "capture failed", "err");
});

$("state").onclick = () => guard(async () => {
  const r = await window.pywebview.api.state();
  out(r.ok ? JSON.stringify(r, null, 2) : (r.error || "failed"), r.ok ? "ok" : "err");
});

$("refresh").onclick = () => guard(async () => { await refresh(); });

function showShot(uri){
  const box = $("shot");
  box.innerHTML = "";
  const img = document.createElement("img");
  img.src = uri;
  box.appendChild(img);
}

refresh();
setInterval(() => { if (!busy) refresh(); }, 4000);
</script></body></html>
"""


def main():
    import webview

    api = Api()
    window = webview.create_window("Rhino — 3d-mcp", html=PAGE, js_api=api,
                                   width=1280, height=820, min_size=(900, 600))
    webview.start()
    return window


if __name__ == "__main__":
    try:
        main()
    except Exception:                                        # noqa: BLE001
        # A window that fails to open must say why somewhere findable, not
        # vanish - there is no console when this is double-clicked.
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "rhino-app-error.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(traceback.format_exc())
        raise
