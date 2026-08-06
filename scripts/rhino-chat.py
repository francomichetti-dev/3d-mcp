"""Chat with Claude about the model open in Rhino.

Two screens, deliberately: the conversation, and settings. Nothing else. The
CAD is Rhino's job and the viewport is already on screen — this window is for
saying what you want and watching it happen.

    you ──▶ Claude ──▶ tools ──▶ broker ──▶ poller (a UI timer in Rhino)

Claude gets three tools: run Python in the live session, read the document, and
look at the viewport. The screenshot comes back as an image, so it can see what
it built and correct itself rather than working blind.

Runs on Rhino's own Python. Nothing to install but the SDK:

    %USERPROFILE%\\.rhinocode\\py39-rh8\\python.exe -m pip install anthropic pywebview
    %USERPROFILE%\\.rhinocode\\py39-rh8\\python.exe rhino-chat.py
"""

import json
import os
import stat
import subprocess
import sys
import threading
import traceback
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# Config — the API key lives beside the bridge token, with the same 0600.
# --------------------------------------------------------------------------

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, ".fusion-mcp")
CONFIG_PATH = os.path.join(CONFIG_DIR, "chat.json")
TOKEN_PATH = os.path.join(CONFIG_DIR, "token")

BROKER = os.environ.get("FUSION_BROKER_URL") or "http://127.0.0.1:7656"
AUTH_HEADER = "X-Fusion-Bridge-Token"

DEFAULT_MODEL = "claude-opus-5"
MODELS = [
    ("claude-opus-5", "Opus 5 — most capable, best for hard modelling"),
    ("claude-sonnet-5", "Sonnet 5 — faster and cheaper, very capable"),
    ("claude-haiku-4-5", "Haiku 4.5 — fastest, for simple edits"),
]


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    data.setdefault("model", DEFAULT_MODEL)
    data.setdefault("api_key", "")
    return data


def _restrict(path):
    """Make the file readable only by this account.

    os.chmod(0600) does NOT do this on Windows - measured: after chmod the ACL
    still read `NT AUTHORITY\\SYSTEM:(I)(F)`, `BUILTIN\\Administrators:(I)(F)`,
    `<user>:(I)(F)`, all inherited. chmod there only toggles the read-only
    attribute, so an API key written this way stays readable by every other
    account on the machine. icacls is what actually restricts it: drop
    inherited ACEs, then grant this user alone.

    An administrator can still take ownership and read it. That is true of
    root on POSIX too, so the honest claim is "other users cannot read it",
    not "nobody can".
    """
    if os.name == "nt":
        user = os.environ.get("USERNAME") or ""
        if not user:
            return False
        try:
            done = subprocess.run(
                ["icacls", path, "/inheritance:r", "/grant:r", f"{user}:F"],
                capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return done.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        return True
    except OSError:
        return False


def save_config(config):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
    return _restrict(CONFIG_PATH)


# --------------------------------------------------------------------------
# The broker — the only way into Rhino.
# --------------------------------------------------------------------------


def _token():
    try:
        with open(TOKEN_PATH, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def broker(method, path, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BROKER + path, data=data, method=method)
    request.add_header(AUTH_HEADER, _token())
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def submit(kind, payload, timeout=120):
    """Send a job to Rhino. Returns a dict; never raises."""
    try:
        return broker("POST", "/submit", {"kind": kind, "payload": payload},
                      timeout=timeout)
    except urllib.error.URLError as exc:
        return {"ok": False, "error": (
            "Rhino is not reachable — the broker is not running. "
            f"Start it, then try again. ({exc.reason})")}
    except OSError as exc:
        return {"ok": False, "error": f"connection failed: {exc}"}
    except ValueError:
        return {"ok": False, "error": "the broker replied with something that was not JSON"}


# --------------------------------------------------------------------------
# The tools Claude gets. Schemas are generated from these signatures and
# docstrings, so the docstring IS the tool description Claude reads - it says
# when to reach for the tool, not just what it does.
# --------------------------------------------------------------------------

# Set by the window so tools can report activity as they run.
_notify = None


def _say(kind, text):
    if _notify:
        _notify(kind, text)


def make_tools():
    from anthropic import beta_tool

    @beta_tool
    def rhino_execute(code: str) -> str:
        """Run Python inside the live Rhino session and return the result.

        This is how you build and modify geometry. Call it whenever the person
        asks for something to be created, changed, measured, or deleted.

        `Rhino` (RhinoCommon), `rs` (rhinoscriptsyntax), `doc` (the active
        document) and `scriptcontext` are already in scope. Assign to `result`
        to send a value back. Variables persist between calls, so you can build
        something up across several steps.

        After changing geometry, call rhino_screenshot to check the result
        looks right before telling the person it is done.

        Args:
            code: Python to execute in Rhino.
        """
        _say("tool", "running code in Rhino")
        out = submit("execute", {"code": code})
        if not out.get("ok"):
            return "FAILED\n" + (out.get("traceback") or out.get("error") or "unknown error")
        parts = []
        if out.get("stdout"):
            parts.append(out["stdout"].rstrip())
        if out.get("result") is not None:
            parts.append("result = " + json.dumps(out["result"], default=str))
        return "\n".join(parts) if parts else "done (no output)"

    @beta_tool
    def rhino_state() -> str:
        """Read the current Rhino document: units, tolerance, object count, layers.

        Call this before making assumptions about what is open — especially at
        the start of a conversation, or when the person refers to something
        that already exists.
        """
        _say("tool", "reading the document")
        out = submit("state", {}, timeout=30)
        if not out.get("ok"):
            return "FAILED: " + (out.get("error") or "unknown error")
        return json.dumps(out, indent=2, default=str)

    @beta_tool
    def rhino_screenshot(view: str = "perspective") -> list:
        """Look at the Rhino viewport. Returns the image so you can see the model.

        Use this to check your own work after building something, and to
        understand what the person is referring to when they describe what they
        can see. Prefer looking over guessing.

        Args:
            view: perspective, top, front, right, or fit.
        """
        _say("tool", f"looking at the {view} view")
        out = submit("screenshot", {"view": view, "width": 1200, "height": 800},
                     timeout=90)
        if not out.get("ok"):
            return [{"type": "text",
                     "text": "FAILED: " + (out.get("error") or "unknown error")}]
        # Show it in the conversation too - the person should see what Claude saw.
        _say("image", "data:image/png;base64," + out["png_base64"])
        return [{
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png",
                       "data": out["png_base64"]},
        }]

    return [rhino_execute, rhino_state, rhino_screenshot]


SYSTEM = """You are modelling in Rhino 8 alongside the person you are talking \
to. They can see the Rhino viewport; you cannot, unless you take a screenshot.

Work by doing, not by explaining. When they ask for something, build it with \
rhino_execute rather than describing how they could. Check the document with \
rhino_state when you need to know what is already there, and look at the result \
with rhino_screenshot before you say something is finished.

Keep replies short. They are watching the model change in front of them, so a \
sentence on what you did is usually enough — no summaries of code you just ran, \
no lists of what you might do next unless they ask.

Rhino units and tolerance matter: check them before building to scale. If \
something fails, read the traceback and fix it yourself rather than handing the \
error back."""


# --------------------------------------------------------------------------
# The window's API
# --------------------------------------------------------------------------


class Api:
    """No method raises — the page always gets a dict it can render."""

    def __init__(self):
        self._lock = threading.Lock()
        self._config = load_config()
        self._history = []          # the conversation, in API shape
        self._window = None

    def bind(self, window):
        self._window = window

    # -- notifications to the page ------------------------------------- #

    def _push(self, kind, text):
        if not self._window:
            return
        try:
            self._window.evaluate_js(
                "window.onAgent(%s, %s)" % (json.dumps(kind), json.dumps(text)))
        except Exception:                                    # noqa: BLE001
            pass

    # -- settings ------------------------------------------------------- #

    def get_settings(self):
        key = self._config.get("api_key") or ""
        return {
            "has_key": bool(key),
            "key_hint": (key[:7] + "…" + key[-4:]) if len(key) > 15 else "",
            "model": self._config.get("model", DEFAULT_MODEL),
            "models": [{"id": m, "label": label} for m, label in MODELS],
            "config_path": CONFIG_PATH,
        }

    def save_settings(self, api_key, model):
        if api_key and not api_key.startswith("sk-"):
            return {"ok": False,
                    "error": "that does not look like an API key — they start with 'sk-'"}
        if api_key:
            self._config["api_key"] = api_key.strip()
        if model:
            self._config["model"] = model
        try:
            restricted = save_config(self._config)
        except OSError as exc:
            return {"ok": False, "error": f"could not save: {exc}"}
        # Say so if the key landed on disk without its permissions locked down,
        # rather than letting the UI keep claiming it is protected.
        return {"ok": True, "restricted": restricted}

    def test_key(self):
        """Prove the key works with the smallest possible real call."""
        key = self._config.get("api_key")
        if not key:
            return {"ok": False, "error": "no API key saved yet"}
        try:
            import anthropic
        except ImportError:
            return {"ok": False, "error": (
                "the anthropic package is not installed — see the setup steps below")}
        try:
            client = anthropic.Anthropic(api_key=key)
            client.messages.create(
                model=self._config.get("model", DEFAULT_MODEL),
                max_tokens=16,
                messages=[{"role": "user", "content": "Reply with: ok"}],
            )
            return {"ok": True, "detail": "key works"}
        except anthropic.AuthenticationError:
            return {"ok": False, "error": "the API key was rejected — check it and re-paste"}
        except anthropic.NotFoundError:
            return {"ok": False, "error": (
                f"this key cannot use {self._config.get('model')} — pick another model")}
        except anthropic.RateLimitError:
            return {"ok": False, "error": "rate limited — the key is valid, try again shortly"}
        except anthropic.APIConnectionError:
            return {"ok": False, "error": "no internet connection to the API"}
        except Exception as exc:                             # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # -- status --------------------------------------------------------- #

    def status(self):
        if not self._config.get("api_key"):
            return {"state": "no-key"}
        try:
            health = broker("GET", "/health", timeout=5)
        except (urllib.error.URLError, OSError, ValueError):
            return {"state": "no-broker"}
        if not health.get("poller_connected"):
            return {"state": "no-rhino"}
        return {"state": "ready"}

    # -- chat ----------------------------------------------------------- #

    def reset(self):
        self._history = []
        return {"ok": True}

    def chat(self, message):
        if not (message or "").strip():
            return {"ok": False, "error": "nothing to send"}
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "error": "still working on the previous message"}
        try:
            return self._chat(message)
        except Exception as exc:                             # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            self._lock.release()

    def _chat(self, message):
        key = self._config.get("api_key")
        if not key:
            return {"ok": False, "error": "add your API key in Settings first"}
        try:
            import anthropic
        except ImportError:
            return {"ok": False, "error": (
                "the anthropic package is not installed — see Settings for the command")}

        global _notify
        _notify = self._push

        client = anthropic.Anthropic(api_key=key)
        self._history.append({"role": "user", "content": message})

        try:
            runner = client.beta.messages.tool_runner(
                model=self._config.get("model", DEFAULT_MODEL),
                max_tokens=16000,
                system=SYSTEM,
                tools=make_tools(),
                messages=self._history,
            )
            final = None
            for reply in runner:
                final = reply
                # Surface Claude's words as they arrive, not just at the end -
                # a modelling turn can run through several tool calls.
                for block in reply.content:
                    if block.type == "text" and block.text.strip():
                        self._push("text", block.text)

                # Mirror the history as we go. The runner keeps its own copy and
                # does NOT expose it (no .messages attribute - checked on
                # anthropic 0.120.2), so this is the only way to carry the tool
                # calls into the next turn. Keeping just the final text instead
                # would silently drop them and Claude would redo work it had
                # already done. generate_tool_call_response() is cached, so the
                # tools still execute exactly once.
                self._history.append({"role": "assistant", "content": reply.content})
                tool_reply = runner.generate_tool_call_response()
                if tool_reply is not None:
                    self._history.append(tool_reply)
        except anthropic.AuthenticationError:
            return {"ok": False, "error": "the API key was rejected — check it in Settings"}
        except anthropic.RateLimitError:
            return {"ok": False, "error": "rate limited — wait a moment and try again"}
        except anthropic.APIConnectionError:
            return {"ok": False, "error": "lost the connection to the API"}
        except anthropic.APIStatusError as exc:
            return {"ok": False, "error": f"API error {exc.status_code}: {exc.message}"}
        finally:
            _notify = None

        if final is not None and final.stop_reason == "refusal":
            return {"ok": False, "error": "Claude declined that request"}
        if final is not None and final.stop_reason == "max_tokens":
            # Silently truncating reads as a mysteriously short answer, so name
            # it and say what to do about it.
            return {"ok": False, "error": (
                "the reply hit the length limit and was cut off — ask for it in "
                "smaller steps")}
        return {"ok": True, "turns": len(self._history)}


PAGE = r"""
<!doctype html><html><head><meta charset="utf-8"><title>Rhino Chat</title><style>
:root{--bg:#1e2226;--panel:#272c31;--line:#3a4046;--fg:#e8eaec;--dim:#949ca4;
      --accent:#6cc0ff;--ok:#7ddc9a;--warn:#ffb454;--err:#ff6b6b;}
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--fg);display:flex;flex-direction:column;
     font:14px/1.6 -apple-system,"Segoe UI",sans-serif}
header{display:flex;align-items:center;gap:10px;padding:10px 14px;
       border-bottom:1px solid var(--line);flex:none}
.dot{width:9px;height:9px;border-radius:50%;background:var(--dim);flex:none}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.err{background:var(--err)}
#state{color:var(--dim);font-size:12.5px;flex:1;overflow:hidden;
       text-overflow:ellipsis;white-space:nowrap}
.tab{background:transparent;border:1px solid var(--line);color:var(--dim);
     border-radius:6px;padding:4px 12px;font-size:12.5px;cursor:pointer}
.tab:hover{color:var(--fg)}.tab.on{color:var(--fg);border-color:var(--dim)}
main{flex:1;min-height:0;display:flex;flex-direction:column}
.view{flex:1;min-height:0;display:none;flex-direction:column}
.view.on{display:flex}

/* chat */
#log{flex:1;overflow-y:auto;padding:18px 16px;display:flex;flex-direction:column;gap:14px}
.msg{max-width:min(760px,92%);white-space:pre-wrap;word-break:break-word}
.msg.you{align-self:flex-end;background:var(--accent);color:#0d2233;
         padding:9px 13px;border-radius:14px 14px 3px 14px}
.msg.claude{align-self:flex-start}
.msg.tool{align-self:flex-start;color:var(--dim);font-size:12.5px;font-style:italic}
.msg.err{align-self:flex-start;color:var(--err)}
.msg img{max-width:100%;border-radius:8px;display:block;margin-top:4px}
#empty{margin:auto;text-align:center;color:var(--dim);max-width:420px}
#empty h2{font-size:16px;font-weight:600;color:var(--fg);margin:0 0 8px}
#empty p{margin:4px 0;font-size:13px}
form{display:flex;gap:8px;padding:12px;border-top:1px solid var(--line);flex:none}
#box{flex:1;background:var(--panel);color:var(--fg);border:1px solid var(--line);
     border-radius:9px;padding:10px 12px;font:inherit;resize:none;max-height:150px}
#box:focus{outline:none;border-color:var(--dim)}
#send{background:var(--accent);color:#0d2233;border:0;border-radius:9px;
      padding:0 20px;font:600 14px inherit;cursor:pointer}
#send:disabled{opacity:.45;cursor:default}

/* settings */
#settings{overflow-y:auto;padding:22px;gap:22px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
      padding:16px 18px;max-width:640px}
.card h3{margin:0 0 4px;font-size:14px}
.card p{margin:0 0 12px;color:var(--dim);font-size:12.5px}
label{display:block;font-size:12.5px;color:var(--dim);margin:10px 0 5px}
input,select{width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--line);
             border-radius:7px;padding:9px 11px;font:inherit}
input:focus,select:focus{outline:none;border-color:var(--accent)}
.btn{background:var(--accent);color:#0d2233;border:0;border-radius:7px;
     padding:8px 16px;font:600 13px inherit;cursor:pointer;margin-top:12px}
.btn.ghost{background:transparent;border:1px solid var(--line);color:var(--dim);
           font-weight:400;margin-left:6px}
.note{margin-top:10px;font-size:12.5px}
.note.ok{color:var(--ok)}.note.err{color:var(--err)}
ol{margin:0;padding-left:20px;font-size:13px}
ol li{margin-bottom:12px}
code{background:var(--bg);border:1px solid var(--line);border-radius:5px;
     padding:2px 6px;font:12px ui-monospace,Consolas,monospace;
     word-break:break-all;display:inline-block}
a{color:var(--accent)}
</style></head><body>
<header>
  <span class="dot" id="dot"></span><span id="state">…</span>
  <button class="tab on" id="t-chat">Chat</button>
  <button class="tab" id="t-set">Settings</button>
</header>
<main>
  <div class="view on" id="v-chat">
    <div id="log"><div id="empty">
      <h2>Model by asking</h2>
      <p>“Make a 20&nbsp;cm cube on a new layer called Blocks”</p>
      <p>“What's in this document?”</p>
      <p>“The chair legs look too thin — thicken them”</p>
    </div></div>
    <form id="form">
      <textarea id="box" rows="1" placeholder="Ask for something…"></textarea>
      <button id="send">Send</button>
    </form>
  </div>

  <div class="view" id="v-set">
    <div id="settings">
      <div class="card">
        <h3>Anthropic API key</h3>
        <p>Stored on this computer only, at <span id="cfgpath"></span>, locked
           so other accounts on this machine cannot read it. It is never sent
           anywhere except Anthropic.</p>
        <label for="key">API key</label>
        <input id="key" type="password" placeholder="sk-ant-…" autocomplete="off">
        <label for="model">Model</label>
        <select id="model"></select>
        <button class="btn" id="save">Save</button>
        <button class="btn ghost" id="test">Test connection</button>
        <div class="note" id="note"></div>
      </div>

      <div class="card">
        <h3>Setup — in this order</h3>
        <p>Each step is one you can check before moving on.</p>
        <ol>
          <li><b>Install the two packages.</b> Open PowerShell and run:<br>
            <code id="pipcmd"></code><br>
            <span style="color:var(--dim)">Uses Rhino's own Python, so there is
            nothing else to install.</span></li>
          <li><b>Get an API key.</b> Sign in at
            <a href="https://console.anthropic.com/settings/keys">console.anthropic.com</a>,
            create a key, and paste it above. This is an Anthropic API account
            and is billed per use — a Claude.ai or Claude Code subscription is
            a different thing and its login will not work here.</li>
          <li><b>Press Test connection.</b> It makes one tiny real request. If
            it fails it says why, so fix that before going on.</li>
          <li><b>Start the broker</b> — double-click <code>START-BROKER.cmd</code>.
            The dot above turns amber: the broker is up, Rhino is not connected
            yet.</li>
          <li><b>Open Rhino</b> and type <code>ScriptEditor</code> once. This
            loads Rhino's Python. The very first time it takes about a minute —
            it is not frozen.</li>
          <li><b>Start the poller</b> — double-click
            <code>START-POLLER.cmd</code>. The dot turns green. You can chat.</li>
        </ol>
      </div>

      <div class="card">
        <h3>If the dot is not green</h3>
        <p style="margin:0">
          <b>Amber</b> — the broker is running but Rhino is not connected. Rhino
          must be open, <code>ScriptEditor</code> run once this session, and the
          poller started (steps 5 and 6).<br><br>
          <b>Red, “broker not running”</b> — do step 4.<br><br>
          <b>Red, “no API key”</b> — do steps 1–3.<br><br>
          Rhino restarting stops the poller. Re-run step 5 and 6 after any
          Rhino restart; the chat itself keeps its conversation.
        </p>
      </div>
    </div>
  </div>
</main>
<script>
const $ = (id) => document.getElementById(id);
let busy = false;

/* ---- tabs ---- */
function show(which){
  $("v-chat").classList.toggle("on", which === "chat");
  $("v-set").classList.toggle("on", which === "set");
  $("t-chat").classList.toggle("on", which === "chat");
  $("t-set").classList.toggle("on", which === "set");
}
$("t-chat").onclick = () => show("chat");
$("t-set").onclick  = () => { show("set"); loadSettings(); };

/* ---- chat ---- */
function bubble(cls, text){
  const empty = $("empty"); if (empty) empty.remove();
  const el = document.createElement("div");
  el.className = "msg " + cls;
  el.textContent = text;
  $("log").appendChild(el);
  $("log").scrollTop = $("log").scrollHeight;
  return el;
}
function image(uri){
  const empty = $("empty"); if (empty) empty.remove();
  const el = document.createElement("div");
  el.className = "msg claude";
  const img = document.createElement("img");
  img.src = uri;
  el.appendChild(img);
  $("log").appendChild(el);
  $("log").scrollTop = $("log").scrollHeight;
}

// Called from Python while a turn is running, so tool use and Claude's words
// appear as they happen rather than all at the end.
window.onAgent = (kind, text) => {
  if (kind === "image") image(text);
  else if (kind === "tool") bubble("tool", text + "…");
  else bubble("claude", text);
};

$("form").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("box").value.trim();
  if (!text || busy) return;
  $("box").value = ""; $("box").style.height = "auto";
  bubble("you", text);
  busy = true; $("send").disabled = true;
  try {
    const r = await window.pywebview.api.chat(text);
    if (!r.ok) bubble("err", r.error || "something went wrong");
  } catch (e) {
    bubble("err", "the window failed: " + e);
  } finally {
    busy = false; $("send").disabled = false; $("box").focus(); refresh();
  }
};

$("box").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 150) + "px";
});
$("box").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("form").requestSubmit(); }
});

/* ---- settings ---- */
async function loadSettings(){
  const s = await window.pywebview.api.get_settings();
  $("cfgpath").textContent = s.config_path;
  $("pipcmd").textContent =
    '& "$env:USERPROFILE\\.rhinocode\\py39-rh8\\python.exe" -m pip install anthropic pywebview';
  $("key").placeholder = s.has_key ? s.key_hint + "  (saved)" : "sk-ant-…";
  const sel = $("model"); sel.innerHTML = "";
  for (const m of s.models){
    const o = document.createElement("option");
    o.value = m.id; o.textContent = m.label; o.selected = m.id === s.model;
    sel.appendChild(o);
  }
}
function note(text, cls){
  $("note").className = "note " + (cls || "");
  $("note").textContent = text;
}
$("save").onclick = async () => {
  const r = await window.pywebview.api.save_settings($("key").value, $("model").value);
  if (r.ok){
    $("key").value = "";
    note(r.restricted ? "Saved."
                      : "Saved, but the file permissions could not be locked down — "
                        + "anyone with an account on this PC could read the key.",
         r.restricted ? "ok" : "err");
    loadSettings(); refresh();
  }
  else note(r.error, "err");
};
$("test").onclick = async () => {
  note("testing…");
  const r = await window.pywebview.api.test_key();
  note(r.ok ? "Connected — your key works." : r.error, r.ok ? "ok" : "err");
  refresh();
};

/* ---- status ---- */
async function refresh(){
  let s; try { s = await window.pywebview.api.status(); }
  catch (e) { s = {state:"no-broker"}; }
  const dot = $("dot"); dot.className = "dot";
  const msg = {
    "ready":     ["ok",   "Rhino connected"],
    "no-rhino":  ["warn", "Rhino not connected — open Rhino, run ScriptEditor, start the poller"],
    "no-broker": ["err",  "broker not running — see Settings, step 4"],
    "no-key":    ["err",  "no API key — add one in Settings"],
  }[s.state] || ["err", "not ready"];
  dot.classList.add(msg[0]);
  $("state").textContent = msg[1];
}
refresh();
setInterval(() => { if (!busy) refresh(); }, 5000);
loadSettings();
$("box").focus();
</script></body></html>
"""


def main():
    import webview

    api = Api()
    window = webview.create_window("Rhino — Claude", html=PAGE, js_api=api,
                                   width=1080, height=760, min_size=(720, 520))
    api.bind(window)
    webview.start()


if __name__ == "__main__":
    try:
        main()
    except Exception:                                        # noqa: BLE001
        # No console when this is double-clicked, so a failure to open must
        # leave a note somewhere findable rather than vanishing.
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "rhino-chat-error.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(traceback.format_exc())
        raise
