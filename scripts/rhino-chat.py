"""Chat with Claude about the model open in Rhino.

Two screens: the conversation, and settings. Nothing else.

It drives the `claude` CLI, so it runs on the Claude subscription the person
already pays for. There is no API key to obtain, paste, protect, or bill
separately — that was the first version's mistake.

    this window ──▶ claude ──MCP──▶ rhino_mcp.py ──▶ broker ──▶ Rhino

The MCP config is passed inline with --mcp-config, so the app works as soon as
Claude Code is installed; `claude mcp add` is only needed to drive Rhino from a
terminal as well.

Runs on Rhino's own Python (3.9). The only dependency is pywebview.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import traceback
import urllib.error
import urllib.request
import uuid

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, ".fusion-mcp")
CONFIG_PATH = os.path.join(CONFIG_DIR, "chat.json")
TOKEN_PATH = os.path.join(CONFIG_DIR, "token")
ATTACH_DIR = os.path.join(CONFIG_DIR, "attachments")
MCP_CONFIG_PATH = os.path.join(CONFIG_DIR, "rhino-mcp.json")

HERE = os.path.dirname(os.path.abspath(__file__))
MCP_SERVER = os.path.join(HERE, "rhino_mcp.py")

BROKER = os.environ.get("FUSION_BROKER_URL") or "http://127.0.0.1:7656"
AUTH_HEADER = "X-Fusion-Bridge-Token"

RHINO_TOOLS = ["mcp__rhino__rhino_execute", "mcp__rhino__rhino_state",
               "mcp__rhino__rhino_screenshot"]

MAX_ATTACH_BYTES = 20 * 1024 * 1024
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Rhino 8 runs on macOS as well as Windows, so nothing user-facing may assume
# PowerShell or .cmd files. Everything platform-specific is resolved here once.
if os.name == "nt":
    PLATFORM = "windows"
elif sys.platform == "darwin":
    PLATFORM = "macos"
else:
    PLATFORM = "linux"

INSTALL_COMMAND = {
    "windows": "irm https://claude.ai/install.ps1 | iex",
    "macos": "curl -fsSL https://claude.ai/install.sh | bash",
    "linux": "curl -fsSL https://claude.ai/install.sh | bash",
}[PLATFORM]

TERMINAL_NAME = {"windows": "PowerShell", "macos": "Terminal",
                 "linux": "a terminal"}[PLATFORM]

# How the person starts the two background pieces on their platform.
START_STEPS = {
    "windows": ("Double-click <code>START-BROKER.cmd</code>, open Rhino and run "
                "the <code>ScriptEditor</code> command once, then double-click "
                "<code>START-POLLER.cmd</code>."),
    "macos": ("Run <code>./start-broker.sh</code>, open Rhino and run the "
              "<code>ScriptEditor</code> command once, then run "
              "<code>./start-poller.sh</code>."),
    "linux": ("Run <code>./start-broker.sh</code>, then start the poller from "
              "inside Rhino."),
}[PLATFORM]


# --------------------------------------------------------------------------
# Finding Claude Code
# --------------------------------------------------------------------------


def find_claude():
    """Locate the claude executable.

    PATH first, then the native installer's own location. On Windows that
    installer writes to %USERPROFILE%\\.local\\bin without updating PATH for an
    already-running process, so a fresh install is invisible to shutil.which
    until a new terminal is opened. Checking the path directly means the person
    does not have to know that.
    """
    found = shutil.which("claude")
    if found:
        return found
    candidates = [
        os.path.join(HOME, ".local", "bin", "claude.exe"),
        os.path.join(HOME, ".local", "bin", "claude"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "claude", "claude.exe"),
        os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return ""


def _server_env():
    env = {"FUSION_BROKER_URL": BROKER}
    token = os.environ.get("FUSION_BRIDGE_TOKEN")
    if token:
        env["FUSION_BRIDGE_TOKEN"] = token
    return env


def write_mcp_config():
    """Write the MCP config the CLI is pointed at, with this machine's paths."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    config = {"mcpServers": {"rhino": {
        "command": sys.executable,          # the Python running this window
        "args": [MCP_SERVER],
        # The MCP server is a separate process and does not inherit this
        # one's environment, so anything non-default has to be passed on.
        "env": _server_env(),
    }}}
    with open(MCP_CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
    return MCP_CONFIG_PATH


# --------------------------------------------------------------------------
# Config and status
# --------------------------------------------------------------------------


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_config(config):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)


def _token():
    from_env = os.environ.get("FUSION_BRIDGE_TOKEN")
    if from_env:
        return from_env.strip()
    try:
        with open(TOKEN_PATH, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def broker_health(timeout=5):
    request = urllib.request.Request(BROKER + "/health")
    request.add_header(AUTH_HEADER, _token())
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


SYSTEM = """You are modelling in Rhino 8 with the person you are talking to. \
They can see the Rhino viewport; you cannot, unless you call rhino_screenshot.

Work by doing. When they ask for something, build it with rhino_execute rather \
than describing how they could. Check what is open with rhino_state before \
assuming, and look at the result with rhino_screenshot before saying it is done.

Keep replies short — they are watching the model change in front of them, so a \
sentence about what you did is usually enough. No code dumps, and no lists of \
what you might do next unless they ask.

Check units and tolerance before building to scale. If something fails, read \
the traceback and fix it yourself rather than handing the error back."""


def _describe(tool):
    """What to show in the conversation for a tool call, or None to stay quiet.

    Claude Code has internal tools of its own (ToolSearch and friends) which it
    uses to find ours. Narrating those to someone modelling a chair is noise
    about our plumbing, so only the tools that touch their work are announced.
    """
    return {
        "mcp__rhino__rhino_execute": "running code in Rhino",
        "mcp__rhino__rhino_state": "reading the document",
        "mcp__rhino__rhino_screenshot": "looking at the viewport",
        "Read": "reading your attachment",
    }.get(tool)


# --------------------------------------------------------------------------
# The window's API
# --------------------------------------------------------------------------


class Api:
    """No method raises — the page always gets a dict it can render."""

    def __init__(self):
        self._lock = threading.Lock()
        self._config = load_config()
        self._window = None
        self._session = self._config.get("session") or str(uuid.uuid4())
        self._started = bool(self._config.get("session_started"))
        self._pending = []          # attachments queued for the next message
        self._proc = None

    def bind(self, window):
        self._window = window

    def _push(self, kind, text):
        if not self._window:
            return
        try:
            self._window.evaluate_js(
                "window.onAgent(%s, %s)" % (json.dumps(kind), json.dumps(text)))
        except Exception:                                    # noqa: BLE001
            pass

    # -- status --------------------------------------------------------- #

    def status(self):
        if not find_claude():
            return {"state": "no-claude"}
        try:
            health = broker_health()
        except (urllib.error.URLError, OSError, ValueError):
            return {"state": "no-broker"}
        if not health.get("poller_connected"):
            return {"state": "no-rhino"}
        return {"state": "ready"}

    def setup_info(self):
        """Everything Settings needs, with this machine's real paths filled in."""
        claude = find_claude()
        version = ""
        if claude:
            try:
                out = subprocess.run([claude, "--version"], capture_output=True,
                                     text=True, timeout=20, creationflags=NO_WINDOW)
                lines = (out.stdout or "").strip().splitlines()
                version = lines[0] if lines else ""
            except (OSError, subprocess.SubprocessError):
                version = ""
        return {
            "claude_found": bool(claude),
            "claude_path": claude,
            "claude_version": version,
            "platform": PLATFORM,
            "install_cmd": INSTALL_COMMAND,
            "terminal": TERMINAL_NAME,
            "start_steps": START_STEPS,
            # One line, paths already correct, pasteable as-is.
            "mcp_add": 'claude mcp add rhino -- "%s" "%s"' % (sys.executable, MCP_SERVER),
            # The whole of steps 3-5 as one sentence. Proven: the first user to
            # set this up pasted something like it and did nothing else.
            "ask_claude": ('Set up the Rhino bridge in "%s" — read SETUP.md '
                           'there and do what it says.' % HERE),
            "folder": HERE,
            "python_path": sys.executable,
            "server_path": MCP_SERVER,
            "attach_dir": ATTACH_DIR,
        }

    def recheck(self):
        """Settings' "I've done that" button — re-detect without a restart."""
        return {"setup": self.setup_info(), "status": self.status()}

    # -- attachments ---------------------------------------------------- #

    def attach(self):
        """Native file picker. Copies what is chosen somewhere Claude may read."""
        if not self._window:
            return {"ok": False, "error": "no window"}
        try:
            import webview

            chosen = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True,
                file_types=("Images and documents (*.png;*.jpg;*.jpeg;*.gif;"
                            "*.webp;*.pdf;*.txt;*.md;*.csv;*.json)",
                            "All files (*.*)"))
        except Exception as exc:                             # noqa: BLE001
            return {"ok": False, "error": f"could not open the file picker: {exc}"}
        if not chosen:
            return {"ok": True, "added": []}

        os.makedirs(ATTACH_DIR, exist_ok=True)
        added = []
        for source in chosen:
            try:
                size = os.path.getsize(source)
            except OSError:
                continue
            if size > MAX_ATTACH_BYTES:
                return {"ok": False,
                        "error": f"{os.path.basename(source)} is larger than 20 MB"}
            # Copied rather than referenced in place: the original may sit on a
            # removable drive or be moved, and Claude is only permitted to read
            # inside the attachments directory.
            target = os.path.join(
                ATTACH_DIR, "%s-%s" % (uuid.uuid4().hex[:8], os.path.basename(source)))
            try:
                shutil.copy2(source, target)
            except OSError as exc:
                return {"ok": False, "error": f"could not attach: {exc}"}
            added.append({"name": os.path.basename(source), "path": target,
                          "is_image": target.lower().endswith(IMAGE_SUFFIXES)})
        self._pending.extend(added)
        return {"ok": True, "added": added}

    def clear_attachments(self):
        self._pending = []
        return {"ok": True}

    # -- chat ----------------------------------------------------------- #

    def reset(self):
        self._session = str(uuid.uuid4())
        self._started = False
        self._pending = []
        self._config.update({"session": self._session, "session_started": False})
        try:
            save_config(self._config)
        except OSError:
            pass
        return {"ok": True}

    def stop(self):
        """Interrupt a running turn."""
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.kill()
                return {"ok": True}
            except OSError:
                pass
        return {"ok": False, "error": "nothing running"}

    def chat(self, message):
        if not (message or "").strip() and not self._pending:
            return {"ok": False, "error": "nothing to send"}
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "error": "still working on the previous message"}
        try:
            return self._chat(message)
        except Exception as exc:                             # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            self._proc = None
            self._lock.release()

    def _build_prompt(self, message):
        if not self._pending:
            return message
        lines = [message.strip(), "", "The person attached these files:"]
        for item in self._pending:
            lines.append("  %s  ->  %s" % (item["name"], item["path"]))
        lines.append("")
        lines.append("Read them before answering.")
        return "\n".join(lines)

    def _chat(self, message):
        claude = find_claude()
        if not claude:
            return {"ok": False, "error": (
                "Claude Code is not installed — Settings has the one-line "
                "install command")}

        config_path = write_mcp_config()
        prompt = self._build_prompt(message)
        self._pending = []

        args = [
            claude, "-p", prompt,
            # stream-json in print mode REQUIRES --verbose: without it the CLI
            # exits with "requires --verbose" and nothing runs at all.
            "--output-format", "stream-json", "--verbose",
            "--strict-mcp-config", "--mcp-config", config_path,
            "--append-system-prompt", SYSTEM,
            "--allowedTools", *RHINO_TOOLS,
            # Scoped rather than a blanket Read: the app should see what was
            # attached and nothing else on the disk.
            "Read(%s%s**)" % (ATTACH_DIR, os.sep),
        ]
        # A session id makes the conversation continuous. Resuming an id that
        # was never created is an error, so the first turn creates it instead.
        args += (["--resume", self._session] if self._started
                 else ["--session-id", self._session])

        try:
            self._proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                # text=True alone uses the LOCALE encoding, which is cp1252 on
                # a Western Windows install. Claude Code emits UTF-8, so the
                # first accented character or em-dash raised
                # UnicodeDecodeError and killed the turn. Measured on the
                # target machine, not hypothetical. errors="replace" means a
                # stray byte degrades one character instead of the reply.
                encoding="utf-8", errors="replace",
                bufsize=1, cwd=CONFIG_DIR, creationflags=NO_WINDOW)

            # FIX 2: drain stderr on a thread. Reading it only after stdout is
            # exhausted deadlocks if the child writes more than the pipe buffer
            # holds (~64 KB): the child blocks writing, so it never closes
            # stdout, so this never stops reading.
            collected = []

            def _drain(stream, into):
                try:
                    for chunk in stream:
                        into.append(chunk)
                except Exception:                            # noqa: BLE001
                    pass

            drainer = threading.Thread(
                target=_drain, args=(self._proc.stderr, collected), daemon=True)
            drainer.start()
        except OSError as exc:
            return {"ok": False, "error": f"could not start Claude Code: {exc}"}

        final, saw_text = None, False
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if block.get("type") == "text" and block.get("text", "").strip():
                        saw_text = True
                        self._push("text", block["text"])
                    elif block.get("type") == "tool_use":
                        told = _describe(block.get("name", ""))
                        if told:
                            self._push("tool", told)
            elif kind == "result":
                final = event

        self._proc.wait()
        drainer.join(timeout=5)
        stderr = "".join(collected).strip()

        if final is None:
            # No result event means the CLI itself failed - a bad flag, no
            # login, a crashed MCP server. Its stderr is the only useful thing
            # to show, and the login case is worth naming since it is the one
            # a new user actually hits.
            lowered = stderr.lower()
            if "log in" in lowered or "login" in lowered or "authenticate" in lowered:
                detail = "not signed in — run `claude` once in a terminal and sign in"
            else:
                detail = stderr.splitlines()[-1] if stderr else "no output"
            return {"ok": False, "error": f"Claude Code did not answer: {detail}"}

        self._started = True
        self._config.update({"session": self._session, "session_started": True})
        try:
            save_config(self._config)
        except OSError:
            pass

        if final.get("is_error"):
            return {"ok": False,
                    "error": final.get("result") or "Claude Code reported an error"}
        # A turn can end with the answer only in the result event; surface it
        # rather than leaving the window blank.
        if not saw_text and final.get("result"):
            self._push("text", final["result"])
        return {"ok": True, "cost": final.get("total_cost_usd"),
                "turns": final.get("num_turns")}


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
#log{flex:1;overflow-y:auto;padding:18px 16px;display:flex;flex-direction:column;gap:14px}
.msg{max-width:min(760px,92%);white-space:pre-wrap;word-break:break-word}
.msg.you{align-self:flex-end;background:var(--accent);color:#0d2233;
         padding:9px 13px;border-radius:14px 14px 3px 14px}
.msg.claude{align-self:flex-start}
.msg.tool{align-self:flex-start;color:var(--dim);font-size:12.5px;font-style:italic}
.msg.err{align-self:flex-start;color:var(--err)}
.msg img{max-width:100%;border-radius:8px;display:block;margin-top:4px}
#empty{margin:auto;text-align:center;color:var(--dim);max-width:430px}
#empty h2{font-size:16px;font-weight:600;color:var(--fg);margin:0 0 8px}
#empty p{margin:4px 0;font-size:13px}
#chips{display:flex;flex-wrap:wrap;gap:6px;padding:0 12px 8px}
#chips:empty{display:none}
.chip{background:var(--panel);border:1px solid var(--line);border-radius:14px;
      padding:3px 10px;font-size:12px;color:var(--dim);display:flex;gap:6px;align-items:center}
.chip b{color:var(--fg);font-weight:500}
form{display:flex;gap:8px;padding:12px;border-top:1px solid var(--line);flex:none;
     align-items:flex-end}
#clip{background:transparent;border:1px solid var(--line);color:var(--dim);
      border-radius:9px;width:40px;height:40px;font-size:17px;cursor:pointer;flex:none}
#clip:hover:not(:disabled){color:var(--fg)}
#box{flex:1;background:var(--panel);color:var(--fg);border:1px solid var(--line);
     border-radius:9px;padding:10px 12px;font:inherit;resize:none;max-height:150px}
#box:focus{outline:none;border-color:var(--dim)}
#send{background:var(--accent);color:#0d2233;border:0;border-radius:9px;
      height:40px;padding:0 20px;font:600 14px inherit;cursor:pointer;flex:none}
#send:disabled{opacity:.45;cursor:default}
#halt{background:transparent;border:1px solid var(--err);color:var(--err);
      border-radius:9px;height:40px;padding:0 16px;font:600 14px inherit;
      cursor:pointer;flex:none}
#settings{overflow-y:auto;padding:22px;gap:20px;display:flex;flex-direction:column}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
      padding:16px 18px;max-width:660px}
.card h3{margin:0 0 4px;font-size:14px}
.card p{margin:0 0 10px;color:var(--dim);font-size:12.5px}
ol{margin:0;padding-left:20px;font-size:13px}
ol li{margin-bottom:16px}
.cmd{display:flex;gap:6px;align-items:stretch;margin:7px 0}
.cmd code{flex:1;background:var(--bg);border:1px solid var(--line);border-radius:6px;
          padding:8px 10px;font:12px ui-monospace,Consolas,monospace;
          word-break:break-all;white-space:pre-wrap}
.copy{background:transparent;border:1px solid var(--line);color:var(--dim);
      border-radius:6px;padding:0 12px;font-size:12px;cursor:pointer;flex:none}
.copy:hover{color:var(--fg)}.copy.done{color:var(--ok);border-color:var(--ok)}
.status{font-size:12.5px;margin-top:6px}
.status.ok{color:var(--ok)}.status.err{color:var(--warn)}
.btn{background:var(--accent);color:#0d2233;border:0;border-radius:7px;
     padding:8px 16px;font:600 13px inherit;cursor:pointer;margin-top:6px}
a{color:var(--accent)}
small{color:var(--dim);font-size:12px}
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
      <p>Attach a photo or sketch and say “build this”</p>
    </div></div>
    <div id="chips"></div>
    <form id="form">
      <button type="button" id="clip" title="Attach files or photos">📎</button>
      <textarea id="box" rows="1" placeholder="Ask for something…"></textarea>
      <button id="send">Send</button>
      <button id="halt" style="display:none">Stop</button>
    </form>
  </div>

  <div class="view" id="v-set">
    <div id="settings">
      <div class="card">
        <h3>Setup — three steps, in order</h3>
        <p>This runs on your Claude subscription. There is no API key and
           nothing to pay for separately.</p>
        <ol>
          <li>
            <b>Install Claude Code.</b> Open <b id="term">a terminal</b> and paste:
            <div class="cmd"><code id="c-install"></code>
              <button class="copy" data-for="c-install">Copy</button></div>
            <small>Needs a Claude Pro, Max, or Team plan — the free plan does
            not include Claude Code.</small>
          </li>
          <li>
            <b>Sign in.</b> In the same window run <code>claude</code>, and
            follow the browser prompt. Close it once it says you are logged in.
            <div class="status" id="s-claude">checking…</div>
          </li>
          <li>
            <b>Ask Claude to do the rest.</b> Open Rhino, then type
            <code>claude</code> in that same window and paste this:
            <div class="cmd"><code id="c-ask"></code>
              <button class="copy" data-for="c-ask">Copy</button></div>
            <small>It connects the bridge, starts what needs starting, and
            tells you what it found. You should not have to run anything
            yourself.</small>
            <div class="status" id="s-rhino">checking…</div>
          </li>
        </ol>
        <button class="btn" id="recheck">Check again</button>
      </div>

      <div class="card">
        <h3>If you would rather do it by hand</h3>
        <p>Step 3 covers this — you only need what follows if Claude could not
           finish, or if you want <code>claude</code> in a terminal to drive
           Rhino as well. Paths are already correct for this machine.</p>
        <div class="cmd"><code id="c-mcp"></code>
          <button class="copy" data-for="c-mcp">Copy</button></div>
        <p style="margin-top:10px">Then start the broker and, with Rhino open
           and <code>ScriptEditor</code> run once, the poller:
           <span id="startsteps"></span></p>
      </div>

      <div class="card">
        <h3>If the dot is not green</h3>
        <p style="margin:0">
          <b>Red, “Claude Code not installed”</b> — do steps 1 and 2.<br><br>
          <b>Red, “broker not running”</b> — do step 3.<br><br>
          <b>Amber, “Rhino not connected”</b> — Rhino must be open, with
          <code>ScriptEditor</code> run once this session, then the poller
          started.<br><br>
          Restarting Rhino stops the poller, so repeat the last part after any
          Rhino restart. The conversation itself is kept.
        </p>
      </div>
    </div>
  </div>
</main>
<script>
const $ = (id) => document.getElementById(id);
let busy = false, attached = [];

function show(which){
  $("v-chat").classList.toggle("on", which === "chat");
  $("v-set").classList.toggle("on", which === "set");
  $("t-chat").classList.toggle("on", which === "chat");
  $("t-set").classList.toggle("on", which === "set");
}
$("t-chat").onclick = () => show("chat");
$("t-set").onclick  = () => { show("set"); loadSetup(); };

function bubble(cls, text){
  const e = $("empty"); if (e) e.remove();
  const el = document.createElement("div");
  el.className = "msg " + cls; el.textContent = text;
  $("log").appendChild(el); $("log").scrollTop = $("log").scrollHeight;
}
window.onAgent = (kind, text) => {
  if (kind === "tool") bubble("tool", text + "…");
  else bubble("claude", text);
};

/* ---- attachments ---- */
function drawChips(){
  $("chips").innerHTML = "";
  attached.forEach((a, i) => {
    const c = document.createElement("span");
    c.className = "chip";
    c.innerHTML = (a.is_image ? "🖼 " : "📄 ") + "<b></b> ✕";
    c.querySelector("b").textContent = a.name;
    c.onclick = () => { attached.splice(i,1); drawChips(); syncAttachments(); };
    $("chips").appendChild(c);
  });
}
async function syncAttachments(){
  if (attached.length === 0) await window.pywebview.api.clear_attachments();
}
$("clip").onclick = async () => {
  if (busy) return;
  const r = await window.pywebview.api.attach();
  if (!r.ok) { bubble("err", r.error); return; }
  attached = attached.concat(r.added || []); drawChips();
};

/* ---- send ---- */
$("form").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("box").value.trim();
  if ((!text && attached.length === 0) || busy) return;
  $("box").value = ""; $("box").style.height = "auto";
  bubble("you", text + (attached.length ? "\n\n📎 " + attached.map(a=>a.name).join(", ") : ""));
  attached = []; drawChips();
  // A modelling turn can run for minutes. Without a way out, a wedged turn
  // leaves the window unusable with no recourse but killing the process.
  busy = true; $("send").disabled = true; $("clip").disabled = true;
  $("send").style.display = "none"; $("halt").style.display = "";
  try {
    const r = await window.pywebview.api.chat(text);
    if (!r.ok) bubble("err", r.error || "something went wrong");
  } catch (e) { bubble("err", "the window failed: " + e); }
  finally { busy = false; $("send").disabled = false; $("clip").disabled = false;
            $("send").style.display = ""; $("halt").style.display = "none";
            $("box").focus(); refresh(); }
};
$("halt").onclick = async () => {
  const r = await window.pywebview.api.stop();
  if (r.ok) bubble("tool", "stopped");
};
$("box").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 150) + "px";
});
$("box").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("form").requestSubmit(); }
});

/* ---- settings ---- */
async function loadSetup(){
  const s = await window.pywebview.api.setup_info();
  $("c-install").textContent = s.install_cmd;
  $("term").textContent = s.terminal;
  $("startsteps").innerHTML = s.start_steps;
  $("c-mcp").textContent = s.mcp_add;
  $("c-ask").textContent = s.ask_claude;
  const el = $("s-claude");
  if (s.claude_found){
    el.className = "status ok";
    el.textContent = "✓ found" + (s.claude_version ? " — " + s.claude_version : "");
  } else {
    el.className = "status err";
    el.textContent = "not found yet — do step 1, then press Check again";
  }
  const st = await window.pywebview.api.status();
  const r = $("s-rhino");
  if (st.state === "ready"){ r.className = "status ok"; r.textContent = "✓ Rhino connected"; }
  else if (st.state === "no-rhino"){ r.className = "status err"; r.textContent = "broker up, Rhino not connected yet"; }
  else if (st.state === "no-broker"){ r.className = "status err"; r.textContent = "broker not running"; }
  else { r.className = "status err"; r.textContent = "waiting on step 1"; }
}
$("recheck").onclick = () => { loadSetup(); refresh(); };
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".copy"); if (!btn) return;
  const text = $(btn.dataset.for).textContent;
  try { await navigator.clipboard.writeText(text); }
  catch (err) {
    // Clipboard API can be blocked in an embedded webview; select the text so
    // it can still be copied by hand rather than leaving a dead button.
    const range = document.createRange(); range.selectNodeContents($(btn.dataset.for));
    const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
  }
  btn.textContent = "Copied"; btn.classList.add("done");
  setTimeout(() => { btn.textContent = "Copy"; btn.classList.remove("done"); }, 1600);
});

/* ---- status ---- */
async function refresh(){
  let s; try { s = await window.pywebview.api.status(); }
  catch (e) { s = {state:"no-broker"}; }
  const dot = $("dot"); dot.className = "dot";
  const m = {
    "ready":     ["ok",   "Rhino connected"],
    "no-rhino":  ["warn", "Rhino not connected — open Rhino, run ScriptEditor, start the poller"],
    "no-broker": ["err",  "broker not running — double-click START-BROKER.cmd"],
    "no-claude": ["err",  "Claude Code not installed — see Settings"],
  }[s.state] || ["err", "not ready"];
  dot.classList.add(m[0]); $("state").textContent = m[1];
}
refresh(); setInterval(() => { if (!busy) refresh(); }, 5000);
loadSetup(); $("box").focus();
</script></body></html>
"""


def main():
    import webview

    api = Api()
    window = webview.create_window("Rhino — Claude", html=PAGE, js_api=api,
                                   width=1080, height=780, min_size=(720, 540))
    api.bind(window)
    webview.start()


if __name__ == "__main__":
    try:
        main()
    except Exception:                                        # noqa: BLE001
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "rhino-chat-error.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(traceback.format_exc())
        raise
