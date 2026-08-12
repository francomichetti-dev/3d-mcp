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

import glob
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import traceback
import urllib.error
import urllib.request
import uuid

# Per-project memory is optional: the window is useful without it, and a
# machine that cannot import it should get a chat with one conversation
# rather than no chat at all.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import rhino_memory
except ImportError:
    rhino_memory = None

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


CONFIG_DIR = _state_dir()
CONFIG_PATH = os.path.join(CONFIG_DIR, "chat.json")
TOKEN_PATH = os.path.join(CONFIG_DIR, "token")
ATTACH_DIR = os.path.join(CONFIG_DIR, "attachments")
MCP_CONFIG_PATH = os.path.join(CONFIG_DIR, "rhino-mcp.json")

HERE = os.path.dirname(os.path.abspath(__file__))
MCP_SERVER = os.path.join(HERE, "rhino_mcp.py")

BROKER = (os.environ.get("ARGES_BROKER_URL")
          or os.environ.get("FUSION_BROKER_URL") or "http://127.0.0.1:7656")
AUTH_HEADER = "X-Arges-Bridge-Token"
# Sent alongside the current one, same value, so this works against a broker
# that has not been updated yet. Servers accept either; clients send both.
LEGACY_AUTH_HEADER = "X-Fusion-Bridge-Token"

# The same list the Fusion panel offers, duplicated because this file runs on
# Rhino's own Python and cannot import from the package. tests/test_consistency
# pins the two together: a model id that exists on one side and not the other
# fails when somebody switches, not at review.
MODELS = [
    ("claude-opus-5", "Opus 5"),
    ("claude-fable-5", "Fable 5"),
    ("claude-sonnet-5", "Sonnet 5"),
    ("claude-haiku-4-5-20251001", "Haiku 4.5"),
]
DEFAULT_MODEL = MODELS[0][0]
MODEL_IDS = frozenset(m for m, _ in MODELS)

# Mirrors the Fusion panel; see the note above about the duplication.
EFFORTS = [
    ("low", "Low"),
    ("medium", "Medium"),
    ("high", "High"),
    ("xhigh", "X-High"),
    ("max", "Max"),
]
DEFAULT_EFFORT = "high"
EFFORT_IDS = frozenset(e for e, _ in EFFORTS)
# Kept beside the token rather than in the window, so the choice survives
# closing it.
SETTINGS_PATH = os.path.join(CONFIG_DIR, "rhino-chat.json")


def read_settings():
    """The chosen model and effort, each falling back if unknown.

    Validated on read as well as on write: a value dropped from either list in
    an upgrade must not leave the window unable to start a turn.
    """
    stored = {}
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as handle:
            stored = json.load(handle) or {}
    except (OSError, ValueError):
        stored = {}
    model = stored.get("model")
    effort = stored.get("effort")
    return {
        "model": model if model in MODEL_IDS else DEFAULT_MODEL,
        "effort": effort if effort in EFFORT_IDS else DEFAULT_EFFORT,
    }


def write_settings(model=None, effort=None):
    """Persist whichever values are valid. Returns what is in force after."""
    current = read_settings()
    if model in MODEL_IDS:
        current["model"] = model
    if effort in EFFORT_IDS:
        current["effort"] = effort
    ensure_dirs()
    with open(SETTINGS_PATH, "w", encoding="utf-8") as handle:
        json.dump(current, handle)
    restrict(SETTINGS_PATH)
    return current


RHINO_TOOLS = ["mcp__rhino__rhino_execute", "mcp__rhino__rhino_state",
               "mcp__rhino__rhino_screenshot", "mcp__rhino__rhino_remember",
               "mcp__rhino__rhino_recall"]

MAX_ATTACH_BYTES = 20 * 1024 * 1024
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
VIDEO_SUFFIXES = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v")
# Videos may be big — a phone clip of a print running passes 20 MB fast — and
# only the extracted frames ever reach Claude, so the cap is its own, larger one.
MAX_VIDEO_BYTES = 500 * 1024 * 1024
VIDEO_FRAMES = 6                     # evenly spaced across the clip


# --------------------------------------------------------------------------
# Video: frames, and speech if the machine can transcribe it
#
# Claude cannot watch a video, but it CAN read frames as images and text as
# text. Both tools here are OPTIONAL and looked for at attach time rather than
# at startup: a machine with neither still runs the chat exactly as before,
# and installing ffmpeg while the window is open just works. Nothing is
# uploaded anywhere — Whisper runs locally.
# --------------------------------------------------------------------------


def find_ffmpeg():
    """ffmpeg if present, else None."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
        os.path.join(os.environ.get("ProgramData", ""),
                     "chocolatey", "bin", "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ]
    # winget installs under a versioned Packages directory. Links usually
    # covers it, but a fresh install is not on PATH for an already-running
    # process, which is exactly when somebody installs it: mid-attach.
    packages = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                            "Microsoft", "WinGet", "Packages")
    try:
        for entry in os.listdir(packages):
            if "ffmpeg" in entry.lower():
                for root, _dirs, files in os.walk(os.path.join(packages, entry)):
                    if "ffmpeg.exe" in files:
                        candidates.append(os.path.join(root, "ffmpeg.exe"))
    except OSError:
        pass
    for cand in candidates:
        if cand and os.path.isfile(cand):
            return cand
    return None


_whisper_model = None


def transcribe_video(ffmpeg, video, duration=0.0, on_progress=None):
    """What was said in the clip, as timestamped text — or None.

    Claude cannot hear, so speech in a video would otherwise be lost entirely.
    Failure is silent by design: no Whisper installed, no audio track, or a
    clip with nobody talking all end the same way, and the video still
    attaches as frames. The transcript is a bonus, never a gate.

    on_progress(fraction) is called as segments arrive — Whisper yields them
    in order, so seg.end / duration is honest progress rather than a guess.
    """
    global _whisper_model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None

    import tempfile

    wav = os.path.join(tempfile.gettempdir(),
                       "rhino-chat-%s.wav" % uuid.uuid4().hex[:8])
    try:
        # 16 kHz mono is what Whisper wants; anything richer is wasted bytes.
        result = subprocess.run(
            [ffmpeg, "-i", video, "-vn", "-ac", "1", "-ar", "16000", "-y", wav],
            capture_output=True, creationflags=NO_WINDOW)
        if result.returncode != 0 or not os.path.isfile(wav):
            return None                      # no audio track at all
        if _whisper_model is None:
            if on_progress:
                on_progress(0.0)             # about to block, loading weights
            # "small" understands Spanish well where "base" mangles it. Loaded
            # once per process — it is a few hundred MB of weights.
            _whisper_model = WhisperModel("small", device="cpu",
                                          compute_type="int8")
        segments, _info = _whisper_model.transcribe(wav, vad_filter=True)
        lines = []
        for seg in segments:
            if on_progress and duration > 0:
                on_progress(min(seg.end / duration, 1.0))
            text = seg.text.strip()
            if text:
                lines.append("[%02d:%02d] %s"
                             % (seg.start // 60, seg.start % 60, text))
        return "\n".join(lines) or None
    except Exception:                                        # noqa: BLE001
        return None
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


def video_duration(ffmpeg, video):
    """Length in seconds, or 0.0 when the file cannot be read."""
    probe = subprocess.run(
        [ffmpeg, "-i", video, "-hide_banner"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=NO_WINDOW)
    for line in (probe.stderr or "").splitlines():
        line = line.strip()
        if line.startswith("Duration:"):
            try:
                clock = line.split("Duration:")[1].split(",")[0].strip()
                hours, minutes, seconds = clock.split(":")
                return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            except (ValueError, IndexError):
                return 0.0
    return 0.0


def extract_frame_at(ffmpeg, video, target, moment):
    """One still at `moment` seconds. True when the file exists afterwards."""
    result = subprocess.run(
        [ffmpeg, "-ss", "%.2f" % moment, "-i", video, "-frames:v", "1",
         "-q:v", "3", "-y", target],
        capture_output=True, creationflags=NO_WINDOW)
    return result.returncode == 0 and os.path.isfile(target)


def thumb_of(path):
    """The frame as a data URI, so the window can preview it.

    Inline rather than file://, because the page is served from a string and
    local file URLs are not reliably reachable from it. Frames are 20-70 KB
    JPEGs, cheap enough to inline.
    """
    import base64

    try:
        with open(path, "rb") as handle:
            return ("data:image/jpeg;base64,"
                    + base64.b64encode(handle.read()).decode("ascii"))
    except OSError:
        return ""


def extract_frames(ffmpeg, video, out_dir, stamp, duration, on_step=None):
    """VIDEO_FRAMES stills, evenly spaced, each named with its timestamp.

    Six across the clip shows the progression — which is what "look at how
    this print is going" actually needs — without flooding the context with
    near-identical stills.
    """
    if duration <= 0:
        return [], "could not read the video (is it a valid file?)"
    frames = []
    for i in range(VIDEO_FRAMES):
        if on_step:
            on_step(i)
        # Nudged off the exact ends: second 0 is often black, and the last
        # frame is often cut mid-motion.
        moment = duration * (i + 0.5) / VIDEO_FRAMES
        name = "%s-frame%d-at-%ds.jpg" % (stamp, i + 1, int(moment))
        target = os.path.join(out_dir, name)
        if extract_frame_at(ffmpeg, video, target, moment):
            frames.append({"path": target, "at": int(moment)})
    if not frames:
        return [], "ffmpeg could not extract any frames"
    return frames, None

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

# Only shown when somebody attaches a video without ffmpeg installed, so it
# has to be the command for THEIR machine rather than the one the author used.
FFMPEG_HINT = {
    "windows": "winget install Gyan.FFmpeg",
    "macos": "brew install ffmpeg",
    "linux": "sudo apt install ffmpeg",
}[PLATFORM]

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

    # Claude Code also ships INSIDE Claude Desktop, under a versioned directory
    # in the packaged app's data. Observed on a real machine as
    # %LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude-code\
    # <version>\claude.exe, with nothing on PATH - so the window reported the
    # CLI missing on a machine that had it. Both the package id and the version
    # vary, hence the glob; the newest version wins.
    packages = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Packages")
    if packages and os.path.isdir(packages):
        pattern = os.path.join(packages, "Claude_*", "LocalCache", "Roaming",
                               "Claude", "claude-code", "*", "claude.exe")
        matches = [p for p in glob.glob(pattern) if os.path.isfile(p)]
        if matches:
            return max(matches, key=os.path.getmtime)
    return ""


def _server_env():
    # Both spellings: this spawns rhino_mcp.py out of the same folder, so it
    # understands ARGES_*, but a hand-written launcher may still be setting
    # the old names and passing them straight through costs nothing.
    env = {"ARGES_BROKER_URL": BROKER, "FUSION_BROKER_URL": BROKER}
    token = (os.environ.get("ARGES_BRIDGE_TOKEN")
             or os.environ.get("FUSION_BRIDGE_TOKEN"))
    if token:
        env["ARGES_BRIDGE_TOKEN"] = token
        env["FUSION_BRIDGE_TOKEN"] = token
    return env


def write_mcp_config():
    """Write the MCP config the CLI is pointed at, with this machine's paths."""
    ensure_dirs()
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


def restrict(path):
    """Make a file or directory readable only by this account.

    Attachments are whatever the person dragged in - drawings, briefs, photos of
    a workshop. They are not more public than the document they describe just
    because they passed through here.

    os.chmod does NOT restrict access on Windows; it only toggles the read-only
    attribute, so the path stays readable by every other account. Measured on a
    real machine earlier in this project: after chmod(0600) the ACL still listed
    SYSTEM, Administrators and the user, all inherited. icacls is what actually
    restricts it.

    The inheritance flags belong ONLY on a directory. (OI)(CI) are
    Object-Inherit/Container-Inherit: they say what a container passes to its
    children, and they are meaningless on a plain file. This code granted
    "(OI)(CI)F" unconditionally, and on a file that is not merely redundant --
    reported from a Windows machine, reproduced directly: icacls returns
    SUCCESS and writes an ACL with no usable grantee, then the very next write
    fails with PermissionError, for the same account that supposedly received
    Full Control. Two of the three call sites here are files, so the settings
    file became unwritable the moment it was first secured and every later
    save failed -- which is why a changed model or effort did not survive a
    restart on Windows while working perfectly on macOS.

    (The same report confirmed the grantee name is not the problem: USERNAME
    alone resolves fine even where icacls DISPLAYS a domain-qualified name.
    The flags were the cause.)
    """
    if os.name == "nt":
        user = os.environ.get("USERNAME")
        if not user:
            return False
        grant = f"{user}:(OI)(CI)F" if os.path.isdir(path) else f"{user}:F"
        try:
            done = subprocess.run(
                ["icacls", path, "/inheritance:r", "/grant:r", grant],
                capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW)
            return done.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.chmod(path, stat.S_IRWXU if os.path.isdir(path)
                 else stat.S_IRUSR | stat.S_IWUSR)
        return True
    except OSError:
        return False


def ensure_dirs():
    """Create the state directories with their permissions set, once."""
    for path, existed in ((CONFIG_DIR, os.path.isdir(CONFIG_DIR)),
                          (ATTACH_DIR, os.path.isdir(ATTACH_DIR))):
        os.makedirs(path, exist_ok=True)
        if not existed:
            restrict(path)


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_config(config):
    ensure_dirs()
    with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)


def _token():
    from_env = (os.environ.get("ARGES_BRIDGE_TOKEN")
                or os.environ.get("FUSION_BRIDGE_TOKEN"))
    if from_env:
        return from_env.strip()
    try:
        with open(TOKEN_PATH, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def read_transcript(session, limit=40):
    """Past messages of a session, oldest first, for repainting the window.

    Claude Code writes one JSON object per line under
    ~/.claude/projects/<cwd slug>/<session>.jsonl. The slug is the working
    directory with separators replaced, and this app always runs Claude with
    cwd=CONFIG_DIR, so the location is known rather than searched for.
    """
    slug = re.sub(r"[^A-Za-z0-9]", "-", CONFIG_DIR)
    path = os.path.join(HOME, ".claude", "projects", slug, session + ".jsonl")
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("type")
                if kind not in ("user", "assistant"):
                    continue
                content = (event.get("message") or {}).get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "\n".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
                else:
                    continue
                text = text.strip()
                # Tool-result turns arrive as role "user" with no prose. They
                # are plumbing, not something the person said.
                if not text or text.startswith("<"):
                    continue
                out.append({"who": "you" if kind == "user" else "claude",
                            "text": text})
    except OSError:
        return []
    return out[-limit:]


def submit_state(timeout=20):
    """Ask Rhino which document is open. The state dict, or None."""
    body = json.dumps({"kind": "state", "payload": {}}).encode()
    request = urllib.request.Request(BROKER + "/submit", data=body, method="POST")
    token = _token()
    request.add_header(AUTH_HEADER, token)
    request.add_header(LEGACY_AUTH_HEADER, token)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def broker_health(timeout=5):
    request = urllib.request.Request(BROKER + "/health")
    token = _token()
    request.add_header(AUTH_HEADER, token)
    request.add_header(LEGACY_AUTH_HEADER, token)
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
the traceback and fix it yourself rather than handing the error back.

You have memory of this project. A <project-memory> block arrives with each \
message: the model open now, its other saved versions, and what you recorded \
before. Rhino saves incrementally, so chair.3dm, chair001.3dm and chair_v2.3dm \
are the SAME project at different points — treat them as continuous work, and \
say so if the version moved rather than acting surprised.

Call rhino_remember when something is worth knowing next time: what they are \
building, a decision and why, a dimension that matters, what is left. Not \
every message, and never what reading the file would tell you. If they mention \
a different model, rhino_recall looks it up — answer from what is recorded, \
and say you have nothing on it rather than inventing.

A video arrives as frames in order plus a transcript of what was said. Read \
the frames as a sequence and treat the speech as instructions."""


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
        "mcp__rhino__rhino_remember": "saving that for next time",
        "mcp__rhino__rhino_recall": "checking what it knows",
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
        self._project = None        # which project the thread belongs to
        self._detected = None       # what Rhino last reported as open
        self._viewing = None        # a closed project being read, read-only

    def bind(self, window):
        self._window = window

    # -- projects -------------------------------------------------------- #

    def _detect_project(self):
        """Which project is open in Rhino now, or None if it cannot be asked."""
        if rhino_memory is None:
            return None
        try:
            out = submit_state()
        except Exception:                                    # noqa: BLE001
            return None
        if not out or not out.get("ok"):
            return None
        return rhino_memory.describe(out.get("path") or "",
                                     out.get("document") or "")

    def _use_project(self, info):
        """Point the conversation at this project's own thread."""
        if info is None or rhino_memory is None:
            return False
        if self._project and self._project["key"] == info["key"]:
            self._project = info                 # same project, newer version
            return False
        session, existed = rhino_memory.session_id(info["key"])
        if not existed and not self._config.get("migrated"):
            # Upgrading from the single global conversation: hand it to the
            # first project opened rather than stranding it, so everything
            # said before per-project memory existed is still there.
            legacy = self._config.get("session")
            if legacy and self._config.get("session_started"):
                session, existed = legacy, True
                rhino_memory.adopt_session(info["key"], legacy)
            self._config["migrated"] = True
            try:
                save_config(self._config)
            except OSError:
                pass
        self._session, self._started = session, existed
        self._project = info
        rhino_memory.touch(info)
        return True

    def _memory_preamble(self):
        """The project-memory block, or '' when there is nothing to say.

        Sent every turn rather than only the first. A resumed session already
        has it earlier in the thread, but the open FILE changes mid-session —
        people save incrementally as they work — and this is what tells Claude
        the version moved under it.
        """
        if rhino_memory is None or self._project is None:
            return ""
        try:
            block = rhino_memory.context_block(self._project)
        except Exception:                                    # noqa: BLE001
            return ""
        return "<project-memory>\n%s\n</project-memory>\n\n" % block if block else ""

    def _push(self, kind, text):
        if not self._window:
            return
        try:
            self._window.evaluate_js(
                "window.onAgent(%s, %s)" % (json.dumps(kind), json.dumps(text)))
        except Exception:                                    # noqa: BLE001
            pass

    def _progress(self, pct, note=""):
        """Drive the attachment progress bar. pct=None hides it.

        Safe to call from the API worker thread: evaluate_js marshals into the
        page, which is exactly why the window keeps painting while ffmpeg and
        Whisper grind away on this one.
        """
        if not self._window:
            return
        try:
            self._window.evaluate_js(
                "window.onAttachProgress(%s, %s)"
                % (json.dumps(pct), json.dumps(note)))
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

            # pywebview validates each filter against ^([\w ]+)\(...\)$ — the
            # description may contain ONLY word characters and spaces, so a
            # comma in it fails with "is not a valid file filter" at click
            # time, not at startup. "Images videos and documents", not
            # "Images, videos and documents".
            chosen = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True,
                file_types=("Images videos and documents (*.png;*.jpg;*.jpeg;"
                            "*.gif;*.webp;*.mp4;*.mov;*.avi;*.mkv;*.webm;*.m4v;"
                            "*.pdf;*.txt;*.md;*.csv;*.json)",
                            "All files (*.*)"))
        except Exception as exc:                             # noqa: BLE001
            return {"ok": False, "error": f"could not open the file picker: {exc}"}
        if not chosen:
            return {"ok": True, "added": []}

        ensure_dirs()
        added = []
        for source in chosen:
            try:
                size = os.path.getsize(source)
            except OSError:
                continue

            if source.lower().endswith(VIDEO_SUFFIXES):
                if size > MAX_VIDEO_BYTES:
                    self._progress(None)
                    return {"ok": False, "error":
                            f"{os.path.basename(source)} is larger than 500 MB"}
                ffmpeg = find_ffmpeg()
                if not ffmpeg:
                    self._progress(None)
                    return {"ok": False, "error": (
                        "videos need ffmpeg, which was not found — install it "
                        "with: " + FFMPEG_HINT)}
                base = os.path.basename(source)
                stamp = uuid.uuid4().hex[:8]
                # ffmpeg and Whisper both block this thread for real seconds,
                # so the window says what it is doing throughout. Silence here
                # reads as a hang, and the person cancels a working attach.
                self._progress(4, "reading %s…" % base)
                duration = video_duration(ffmpeg, source)
                frames, error = extract_frames(
                    ffmpeg, source, ATTACH_DIR, stamp, duration,
                    on_step=lambda i: self._progress(
                        8 + i * 9, "frame %d of %d…" % (i + 1, VIDEO_FRAMES)))
                if error:
                    self._progress(None)
                    return {"ok": False, "error": f"{base}: {error}"}
                for frame in frames:
                    # A frame is a picture of the person's workshop; it is no
                    # more public than the file it came from.
                    restrict(frame["path"])
                    added.append({
                        "name": "%s @ %ds" % (base, frame["at"]),
                        "path": frame["path"], "is_image": True,
                        "from_video": base, "video_path": source,
                        "at": frame["at"], "duration": duration,
                        "thumb": thumb_of(frame["path"])})
                self._progress(64, "listening for speech…")
                transcript = transcribe_video(
                    ffmpeg, source, duration,
                    on_progress=lambda f: self._progress(
                        68 + int(f * 28), "transcribing… %d%%" % int(f * 100)))
                if transcript:
                    tpath = os.path.join(ATTACH_DIR, "%s-audio.txt" % stamp)
                    try:
                        with open(tpath, "w", encoding="utf-8") as handle:
                            handle.write(transcript)
                        restrict(tpath)      # somebody's voice, written down
                        added.append({"name": "%s (audio)" % base,
                                      "path": tpath, "is_image": False,
                                      "from_video": base, "is_transcript": True,
                                      "text": transcript})
                    except OSError:
                        pass             # frames still attach; audio is bonus
                self._progress(100, "done")
                self._progress(None)
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
            # copy2 preserves the source's mode, which may be world-readable.
            restrict(target)
            added.append({"name": os.path.basename(source), "path": target,
                          "is_image": target.lower().endswith(IMAGE_SUFFIXES)})
        self._pending.extend(added)
        return {"ok": True, "added": added}

    def clear_attachments(self):
        self._pending = []
        return {"ok": True}

    def remove_attachment(self, path):
        """Drop one pending item.

        The ✕ on a chip has to reach here: removing it only from the page left
        the file still attached at send time, so a frame the person explicitly
        dropped was sent anyway.
        """
        self._pending = [i for i in self._pending if i.get("path") != path]
        return {"ok": True, "left": len(self._pending)}

    def reframe(self, path, delta):
        """Re-take one video frame `delta` seconds from where it was.

        The person saw the six previews and wants a different moment — the
        blurry one mid-pan, or two seconds later once the part is in shot.
        Only that frame is re-extracted; the others stay exactly as they are.
        """
        for item in self._pending:
            if (item.get("path") == path and item.get("video_path")
                    and not item.get("is_transcript")):
                ffmpeg = find_ffmpeg()
                if not ffmpeg:
                    return {"ok": False, "error": "ffmpeg is no longer available"}
                duration = item.get("duration") or 0
                top = max(duration - 0.5, 0.5)
                moment = min(max(item["at"] + delta, 0.0), top)
                target = os.path.join(
                    ATTACH_DIR,
                    "%s-at-%ds.jpg" % (uuid.uuid4().hex[:8], int(moment)))
                if not extract_frame_at(ffmpeg, item["video_path"],
                                        target, moment):
                    return {"ok": False, "error": "could not grab that frame"}
                restrict(target)
                try:
                    os.remove(item["path"])      # the old still is now junk
                except OSError:
                    pass
                item.update({
                    "path": target, "at": int(moment),
                    "name": "%s @ %ds" % (item["from_video"], int(moment)),
                    "thumb": thumb_of(target)})
                return {"ok": True, "old_path": path,
                        "item": {k: item[k] for k in
                                 ("name", "path", "at", "thumb", "from_video",
                                  "is_image")}}
        return {"ok": False, "error": "that frame is no longer attached"}

    # -- chat ----------------------------------------------------------- #

    @staticmethod
    def _label(info):
        title = info["title"]
        if info.get("version"):
            title += " · %s" % info["version"]
        return title

    def open_project(self, initial=False):
        """Track the model open in Rhino.

        `changed` is true only when Rhino actually moved to a different
        project, not merely because this was called again — the window polls
        it, so reporting a change every time would wipe the view out from
        under someone reading it, and fight with a sidebar selection.
        """
        info = self._detect_project()
        if info is None:
            return {"ok": True, "project": None, "changed": False,
                    "messages": [], "viewing": None}

        moved = self._detected is None or self._detected["key"] != info["key"]
        self._detected = info
        if moved or initial:
            self._use_project(info)
            self._viewing = info["key"]
            messages = read_transcript(self._session) if self._started else []
            return {"ok": True, "project": info["key"],
                    "title": self._label(info),
                    "changed": bool(moved and not initial),
                    "messages": messages, "viewing": info["key"]}
        # Same model as before: keep whatever the person is looking at.
        self._project = info                     # refresh the version label
        return {"ok": True, "project": info["key"], "title": self._label(info),
                "changed": False, "messages": None, "viewing": self._viewing}

    def list_projects(self):
        """Every project with a saved thread, newest first, for the sidebar."""
        if rhino_memory is None:
            return {"ok": True, "projects": [], "active": None}
        try:
            index = rhino_memory.load_index()
        except Exception:                                    # noqa: BLE001
            return {"ok": True, "projects": [], "active": None}
        rows = sorted(index.items(),
                      key=lambda kv: kv[1].get("last_seen", ""), reverse=True)
        projects = [{
            "key": key,
            "title": meta.get("title") or key,
            "last_seen": meta.get("last_seen", ""),
            "versions": len(meta.get("versions") or []),
            "cost_usd": meta.get("cost_usd") or 0.0,
        } for key, meta in rows]
        return {"ok": True, "projects": projects,
                "active": self._detected["key"] if self._detected else None}

    def view_project(self, key):
        """Show a project's past conversation without switching the work.

        Read-only on purpose: this window runs code in the document Rhino has
        OPEN, so continuing another project's thread would build geometry in
        the wrong file. Sending always goes to the open model.
        """
        if rhino_memory is None or not key:
            return {"ok": False, "error": "no memory"}
        try:
            session, existed = rhino_memory.session_id(key)
            meta = (rhino_memory.load_index().get(key) or {})
            notes = rhino_memory.read_notes(key)
        except Exception as exc:                             # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        messages = read_transcript(session) if existed else []
        active = self._detected["key"] if self._detected else None
        self._viewing = key
        return {"ok": True, "project": key,
                "title": meta.get("title") or key,
                "messages": messages, "notes": notes,
                "is_active": key == active,
                "active_title": (self._label(self._detected)
                                 if self._detected else None)}

    def reset(self):
        # Clearing the screen must not erase which project this is: the
        # conversation restarts, the project does not.
        if rhino_memory is not None and self._project is not None:
            self._session = rhino_memory.reset_session(self._project["key"])
            self._started = False
            return {"ok": True}
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

    def settings(self):
        """Both dropdowns' contents, plus what is selected now."""
        current = read_settings()
        return {
            "models": [{"id": m, "label": label} for m, label in MODELS],
            "efforts": [{"id": e, "label": label} for e, label in EFFORTS],
            "model": current["model"],
            "effort": current["effort"],
        }

    def set_settings(self, model, effort):
        """Persist a choice. Anything unrecognised leaves that value alone."""
        current = write_settings(model=model, effort=effort)
        return {"ok": True, **current}

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
            return self._memory_preamble() + message
        # Frames from one video are grouped and labelled as a sequence.
        # Listed flat they look like six unrelated photos, and the whole point
        # of a clip is that the order carries the information.
        videos = {}
        plain = []
        for item in self._pending:
            if item.get("from_video"):
                videos.setdefault(item["from_video"], []).append(item)
            else:
                plain.append(item)
        lines = [message.strip(), ""]
        if plain:
            lines.append("The person attached these files:")
            lines += ["  %s  ->  %s" % (i["name"], i["path"]) for i in plain]
        for video, items in videos.items():
            frames = [i for i in items if not i.get("is_transcript")]
            audio = [i for i in items if i.get("is_transcript")]
            lines.append("The person attached a video (%s). These are frames "
                         "taken from it at even intervals, in order — read "
                         "them as a sequence showing progression:" % video)
            lines += ["  %s  ->  %s" % (i["name"], i["path"]) for i in frames]
            for item in audio:
                lines.append("What they SAY in that video is transcribed here "
                             "with [mm:ss] timestamps matching the frames — "
                             "read it, it is usually instructions:")
                lines.append("  %s" % item["path"])
        lines += ["", "Read them before answering."]
        return self._memory_preamble() + "\n".join(lines)

    def _chat(self, message):
        claude = find_claude()
        if not claude:
            return {"ok": False, "error": (
                "Claude Code is not installed — Settings has the one-line "
                "install command")}

        config_path = write_mcp_config()
        prompt = self._build_prompt(message)
        self._pending = []

        settings = read_settings()
        args = [
            claude, "-p", prompt,
            # Explicit rather than inherited: without these the CLI picks its
            # own defaults, which are not necessarily what the window shows.
            # Read per invocation, so a change applies to the next message and
            # there is nothing to rebuild.
            "--model", settings["model"],
            "--effort", settings["effort"],
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

        # total_cost_usd is what THIS turn cost, so it accumulates onto the
        # project rather than replacing its total. A missed entry is not worth
        # failing a turn that otherwise worked.
        if rhino_memory is not None and self._project is not None:
            try:
                rhino_memory.add_cost(self._project["key"],
                                      final.get("total_cost_usd"))
            except Exception:                                # noqa: BLE001
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
      --accent:#6cc0ff;--ok:#7ddc9a;--warn:#ffb454;--err:#ff6b6b;
      /* The engine draws the open <select> list and the scrollbars itself,
         and without this it draws them for a LIGHT page — a white popup over
         the dark window. Same fix as the Fusion panel. */
      color-scheme:dark;}
/* The hidden attribute must always win over id rules that set display —
   learned in the Fusion panel, where an always-visible banner spun a wheel
   forever. Cheap insurance here for the same class of bug. */
[hidden]{display:none!important}
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
#shell{flex:1;min-height:0;display:flex}
main{flex:1;min-height:0;display:flex;flex-direction:column}
.view{flex:1;min-height:0;display:none;flex-direction:column}
.view.on{display:flex}
/* sidebar: one entry per project, newest first */
#side{width:212px;flex:none;border-right:1px solid var(--line);
      display:flex;flex-direction:column;overflow:hidden}
#side.hide{display:none}
#side-head{padding:11px 13px 7px;font-size:11px;letter-spacing:.06em;
           text-transform:uppercase;color:var(--dim);flex:none}
#plist{flex:1;overflow-y:auto;padding:0 7px 10px;display:flex;
       flex-direction:column;gap:2px}
.proj{background:transparent;border:0;color:var(--dim);text-align:left;
      border-radius:7px;padding:7px 9px;font:inherit;font-size:13px;
      cursor:pointer;display:flex;flex-direction:column;gap:2px;width:100%}
.proj:hover{background:var(--panel);color:var(--fg)}
.proj.on{background:var(--panel);color:var(--fg)}
.proj .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
          display:flex;align-items:center;gap:6px}
.proj .live{width:6px;height:6px;border-radius:50%;background:var(--ok);flex:none}
.proj .sub{font-size:11px;color:var(--dim);overflow:hidden;
           text-overflow:ellipsis;white-space:nowrap}
.proj .cost{font-size:10.5px;color:var(--accent);opacity:.85}
#pempty{color:var(--dim);font-size:12px;padding:6px 9px}
#burger{background:transparent;border:1px solid var(--line);color:var(--dim);
        border-radius:6px;width:30px;height:26px;font-size:13px;cursor:pointer;flex:none}
#burger:hover{color:var(--fg)}
/* shown while reading a project that is not the model Rhino has open */
#viewing{display:flex;align-items:center;gap:10px;padding:8px 13px;flex:none;
         background:var(--panel);border-bottom:1px solid var(--line);
         font-size:12.5px;color:var(--dim)}
#viewing b{color:var(--fg);font-weight:600}
#backlive{background:transparent;border:1px solid var(--line);color:var(--accent);
          border-radius:6px;padding:4px 10px;font:inherit;font-size:12px;
          cursor:pointer;margin-left:auto;flex:none}
/* attachment progress — the window must never look frozen while ffmpeg and
   Whisper work through a clip */
#prog{display:flex;flex-direction:column;gap:4px;padding:6px 14px 2px}
#prog-track{height:4px;background:var(--line);border-radius:2px;overflow:hidden}
#prog-fill{height:100%;width:0%;background:var(--accent);border-radius:2px;
           transition:width .25s ease}
#prog-note{font-size:11.5px;color:var(--dim)}
/* video frame previews, adjustable before sending */
.fcard{position:relative;flex:none}
.fcard img{width:96px;height:64px;object-fit:cover;border-radius:7px;
           border:1px solid var(--line);display:block}
.fcard .fat{position:absolute;left:4px;top:4px;background:rgba(0,0,0,.65);
            color:#fff;font-size:10.5px;padding:1px 5px;border-radius:4px}
.fcard .fbtns{position:absolute;inset:auto 0 0 0;display:flex;
              justify-content:space-between;padding:2px;opacity:0;
              transition:opacity .15s}
.fcard:hover .fbtns{opacity:1}
.fbtns button{background:rgba(0,0,0,.65);border:0;color:#fff;border-radius:4px;
              width:24px;height:20px;font-size:11px;cursor:pointer;line-height:1}
.fbtns button:hover{background:rgba(0,0,0,.85)}
.fcard.working img{opacity:.4}
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
#model{background:transparent;color:#8b8f94;border:1px solid #3a3d42;
  border-radius:6px;padding:7px 4px;font:inherit;font-size:11px;cursor:pointer;
  max-width:96px;align-self:flex-end}
#model:hover{color:#e6e8ea}
#model:disabled{opacity:.45;cursor:default}
#effort{background:transparent;color:#8b8f94;border:1px solid #3a3d42;
  border-radius:6px;padding:7px 4px;font:inherit;font-size:11px;cursor:pointer;
  max-width:82px;align-self:flex-end}
#effort:hover{color:#e6e8ea}
#effort:disabled{opacity:.45;cursor:default}
/* Pin the popup rows to the window's colours, on top of color-scheme. */
#model option,#effort option{background:var(--panel);color:var(--fg)}
#send{background:var(--accent);color:#0d2233;border:0;border-radius:9px;
      height:40px;padding:0 20px;font:600 14px inherit;cursor:pointer;flex:none}
#send:disabled{opacity:.45;cursor:default}
/* The working banner, as in the Fusion panel: the wheel says "running" and
   the Stop beside it — orange, the one colour for "make it stop" — is the
   answer to it. A cancelled turn ends orange "Stopped", never green "Done". */
#banner{display:flex;align-items:center;gap:8px;padding:6px 14px;
        font-size:12.5px;color:var(--dim);border-bottom:1px solid var(--line);
        background:var(--panel);flex:none}
#banner.done{color:var(--ok)}
#banner.stopped{color:var(--warn)}
#banner.done .spin,#banner.stopped .spin{display:none}
#banner.done #banner-stop,#banner.stopped #banner-stop{display:none}
#banner-stop{background:transparent;border:1px solid var(--warn);color:var(--warn);
  border-radius:5px;padding:2px 9px;font-size:11px;cursor:pointer;flex:none}
#banner-stop:hover{background:var(--warn);color:#3a2a10}
#banner-stop:disabled{opacity:.5;cursor:default}
.spin{width:11px;height:11px;flex:none;border-radius:50%;
  border:2px solid var(--line);border-top-color:var(--accent);
  animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion: reduce){
  .spin{animation:pulse 1.4s ease-in-out infinite}
  @keyframes pulse{50%{opacity:.35}}
}
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
<div id="shell">
<aside id="side">
  <div id="side-head">Projects</div>
  <div id="plist"><div id="pempty">No projects yet — save your model and
    start asking.</div></div>
</aside>
<main>
  <div class="view on" id="v-chat">
    <!-- Reading a project Rhino does not have open. Read-only on purpose:
         this window runs code in the OPEN document, so continuing another
         project's thread would build geometry in the wrong file. -->
    <div id="viewing" hidden>
      <span>You are reading <b id="v-name"></b>, but Rhino has
        <b id="live-name"></b> open — this is history only.</span>
      <button id="backlive" type="button">Back to open model</button>
    </div>
    <div id="banner" hidden>
      <span class="spin" aria-hidden="true"></span>
      <button id="banner-stop" type="button" title="Cancel the running prompt">Stop</button>
      <span id="banner-text"></span>
    </div>
    <div id="log"><div id="empty">
      <h2>Model by asking</h2>
      <p>“Make a 20&nbsp;cm cube on a new layer called Blocks”</p>
      <p>“What's in this document?”</p>
      <p>Attach a photo, sketch or video and say “build this”</p>
    </div></div>
    <!-- ffmpeg and Whisper block the API thread for real seconds. Silence
         there reads as a hang and the person kills a working attach. -->
    <div id="prog" hidden>
      <div id="prog-track"><div id="prog-fill"></div></div>
      <span id="prog-note"></span>
    </div>
    <div id="chips"></div>
    <form id="form">
      <button type="button" id="clip" title="Attach files, photos or videos">📎</button>
      <select id="model" title="Which model answers in this chat"></select>
      <select id="effort" title="How hard it thinks before answering"></select>
      <textarea id="box" rows="1" placeholder="Ask for something…"></textarea>
      <button id="send">Send</button>
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
</div>
<script>
const $ = (id) => document.getElementById(id);
let busy = false, attached = [];
let viewingKey = null, liveKey = null;

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
  if (kind === "tool") { bubble("tool", text + "…"); working(text); }
  else bubble("claude", text);
};

/* ---- the working banner ----
   As in the Fusion panel: the wheel says "running", the orange Stop beside it
   is the answer to it, and a cancelled turn ends "Stopped" — never the green
   "Done" of a build that actually finished. */
let stopping = false;
function working(label){
  const b = $("banner");
  b.classList.remove("done", "stopped");
  $("banner-text").textContent = label || "Working";
  $("banner-stop").disabled = false;
  b.hidden = false;
}
function finished(){
  const b = $("banner");
  b.classList.remove("stopped"); b.classList.add("done");
  $("banner-text").textContent = "Done";
  b.hidden = false;
}
function stoppedBanner(){
  const b = $("banner");
  b.classList.remove("done"); b.classList.add("stopped");
  $("banner-text").textContent = "Stopped";
  b.hidden = false;
}
function clearBanner(){
  const b = $("banner");
  b.hidden = true; b.classList.remove("done", "stopped");
}
$("banner-stop").onclick = async () => {
  // Feedback first: the click must change the screen before the server
  // answers, or a slow interrupt reads as a dead button.
  stopping = true;
  $("banner-stop").disabled = true;
  $("banner-text").textContent = "Stopping…";
  try { await window.pywebview.api.stop(); } catch (e) {}
};

/* ---- attachments ---- */
window.onAttachProgress = (pct, note) => {
  const bar = $("prog");
  if (pct === null){ bar.hidden = true; return; }
  bar.hidden = false;
  $("prog-fill").style.width = pct + "%";
  $("prog-note").textContent = note || "";
};

async function removeAttachment(a, i){
  // Tell Python too. Removing it only from the page left the file still
  // attached at send time, so a frame the person dropped was sent anyway.
  attached.splice(i, 1); drawChips();
  try { await window.pywebview.api.remove_attachment(a.path); } catch (e) {}
}

async function nudgeFrame(a, delta, card){
  if (busy || card.classList.contains("working")) return;
  card.classList.add("working");
  try {
    const r = await window.pywebview.api.reframe(a.path, delta);
    if (!r.ok) { bubble("err", r.error); return; }
    const idx = attached.findIndex(x => x.path === r.old_path);
    if (idx >= 0) attached[idx] = Object.assign(attached[idx], r.item);
    drawChips();
  } finally { card.classList.remove("working"); }
}

function drawChips(){
  $("chips").innerHTML = "";
  attached.forEach((a, i) => {
    if (a.thumb){
      // A video frame: show the picture itself, with ‹ › to re-take it a
      // couple of seconds either way and ✕ to drop it. The six evenly-spaced
      // stills often miss the moment that matters by a second or two.
      const card = document.createElement("div");
      card.className = "fcard";
      const img = document.createElement("img");
      img.src = a.thumb; img.title = a.name;
      const at = document.createElement("span");
      at.className = "fat"; at.textContent = "@" + a.at + "s";
      const btns = document.createElement("div");
      btns.className = "fbtns";
      [["‹", () => nudgeFrame(a, -2, card), "2s earlier"],
       ["✕", () => removeAttachment(a, i), "remove this frame"],
       ["›", () => nudgeFrame(a, 2, card), "2s later"]].forEach(([txt, fn, tip]) => {
        const b = document.createElement("button");
        b.type = "button"; b.textContent = txt; b.title = tip;
        b.onclick = (ev) => { ev.stopPropagation(); fn(); };
        btns.appendChild(b);
      });
      card.appendChild(img); card.appendChild(at); card.appendChild(btns);
      $("chips").appendChild(card);
      return;
    }
    const c = document.createElement("span");
    c.className = "chip";
    c.innerHTML = (a.is_transcript ? "🎙 " : a.from_video ? "🎬 "
                   : a.is_image ? "🖼 " : "📄 ") + "<b></b> ✕";
    c.querySelector("b").textContent = a.name;
    c.onclick = () => removeAttachment(a, i);
    $("chips").appendChild(c);
  });
}
async function syncAttachments(){
  if (attached.length === 0) await window.pywebview.api.clear_attachments();
}
$("clip").onclick = async () => {
  if (busy) return;
  $("clip").disabled = true;
  try {
    const r = await window.pywebview.api.attach();
    if (!r.ok) { bubble("err", r.error); return; }
    attached = attached.concat(r.added || []); drawChips();
    const spoken = (r.added || []).filter(a => a.is_transcript && a.text);
    // Show the transcript before sending: it is the one attachment the
    // person cannot check by looking at a thumbnail.
    spoken.forEach(a => bubble("tool", "heard in " + a.from_video + ":\n" + a.text));
  } finally {
    $("clip").disabled = false;
    window.onAttachProgress(null);   // belt and braces: never leave it stuck
  }
};

/* ---- send ---- */
$("form").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("box").value.trim();
  if ((!text && attached.length === 0) || busy) return;
  $("box").value = ""; $("box").style.height = "auto";
  bubble("you", text + (attached.length ? "\n\n📎 " + attached.map(a=>a.name).join(", ") : ""));
  attached = []; drawChips();
  // A modelling turn can run for minutes. The banner's Stop is the way out —
  // without one, a wedged turn leaves the window unusable with no recourse
  // but killing the process. The pickers freeze too: the CLI takes the flags
  // per invocation, so a mid-turn change would silently apply to a turn that
  // is not the one on screen.
  busy = true; stopping = false;
  $("send").disabled = true; $("clip").disabled = true;
  $("model").disabled = true; $("effort").disabled = true;
  working("Working");
  try {
    const r = await window.pywebview.api.chat(text);
    if (stopping) stoppedBanner();
    else if (r.ok) {
      finished();
      // What this turn cost, in the open. It runs on their own subscription,
      // so the honest thing is to show the number rather than hide it.
      if (r.cost > 0) bubble("tool", "$" + r.cost.toFixed(r.cost < 1 ? 3 : 2) + " this turn");
      loadProjects();               // refresh the sidebar's running total
    }
    else { bubble("err", r.error || "something went wrong"); clearBanner(); }
  } catch (e) { bubble("err", "the window failed: " + e); clearBanner(); }
  finally { busy = false; stopping = false;
            $("send").disabled = false; $("clip").disabled = false;
            $("model").disabled = false; $("effort").disabled = false;
            $("box").focus(); refresh(); }
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
  // Filled from Python so the page never hardcodes a list the CLI would
  // reject. Switching affects the NEXT message only — the CLI takes both flags
  // per invocation, so nothing in flight is disturbed and there is no session
  // to rebuild.
  const modelPicker = $('model');
  const effortPicker = $('effort');
  const settings = await window.pywebview.api.settings();
  const fill = (el, options, current) => {
    // loadSetup runs on every visit to the Settings tab; without clearing,
    // each visit appended the whole list again.
    el.innerHTML = "";
    options.forEach((o) => {
      const opt = document.createElement('option');
      opt.value = o.id; opt.textContent = o.label;
      el.appendChild(opt);
    });
    el.value = current;
  };
  fill(modelPicker, settings.models, settings.model);
  fill(effortPicker, settings.efforts, settings.effort);
  const applySettings = async () => {
    const out = await window.pywebview.api.set_settings(
      modelPicker.value, effortPicker.value);
    // Re-read from what was actually stored, so a rejected value snaps back
    // rather than leaving the page showing something that is not in force.
    modelPicker.value = out.model;
    effortPicker.value = out.effort;
    // bubble, not a phantom helper: this line used to call say(), which was
    // never defined — the setting stuck but the confirmation threw inside the
    // async handler and the person saw nothing change.
    bubble('tool', 'Now using ' + modelPicker.selectedOptions[0].textContent
        + ' at ' + effortPicker.selectedOptions[0].textContent
        + ' effort, from the next message.');
  };
  modelPicker.onchange = applySettings;
  effortPicker.onchange = applySettings;

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
/* ---- projects: one thread per model, listed in the sidebar ---- */
let liveProject = null;      // what Rhino has open
let viewing = null;          // what the log is currently showing
let liveLabel = null;        // the open model's name, with its version
const titles = {};           // key -> project name, for naming both sides

function paint(messages){
  $("log").innerHTML = "";
  (messages || []).forEach(m => bubble(m.who === "you" ? "you" : "claude", m.text));
}

/* Reading another project is history-only: this window runs code in the model
   Rhino has OPEN, so continuing a different thread would build geometry in the
   wrong file. Both names are shown, because "you are in the wrong chat" only
   helps if it says which one you are in and which one is live. */
function setViewingBanner(){
  const off = !!(viewing && liveProject && viewing !== liveProject);
  $("viewing").hidden = !off;
  if (off){
    $("v-name").textContent = titles[viewing] || "another project";
    $("live-name").textContent = titles[liveProject] || liveLabel || "the open model";
    $("backlive").textContent = "Go to " + (titles[liveProject] || "the open model");
  }
  $("box").disabled = off;
  $("send").disabled = off || busy;
  $("box").placeholder = off
    ? "You are reading " + (titles[viewing] || "another project") + " — switch to "
      + (titles[liveProject] || "the open model") + " to work"
    : "Ask for something…";
}

async function loadProjects(){
  let r; try { r = await window.pywebview.api.list_projects(); } catch (e) { return; }
  if (!r || !r.ok || !r.projects.length) return;
  const list = $("plist");
  list.innerHTML = "";
  r.projects.forEach(p => {
    titles[p.key] = p.title;
    const b = document.createElement("button");
    b.className = "proj" + (p.key === viewing ? " on" : "");
    b.dataset.key = p.key;
    const nm = document.createElement("span"); nm.className = "nm";
    if (p.key === r.active){
      const d = document.createElement("span"); d.className = "live";
      d.title = "open in Rhino now"; nm.appendChild(d);
    }
    nm.appendChild(document.createTextNode(p.title));
    const sub = document.createElement("span"); sub.className = "sub";
    sub.textContent = p.last_seen + (p.versions > 1 ? " · " + p.versions + " versions" : "");
    b.appendChild(nm); b.appendChild(sub);
    if (p.cost_usd > 0){
      const cost = document.createElement("span"); cost.className = "cost";
      cost.textContent = "$" + p.cost_usd.toFixed(p.cost_usd < 1 ? 3 : 2);
      b.appendChild(cost);
    }
    b.onclick = () => openProject(p.key);
    list.appendChild(b);
  });
  setViewingBanner();
}

async function openProject(key){
  if (busy) return;
  let r; try { r = await window.pywebview.api.view_project(key); } catch (e) { return; }
  if (!r || !r.ok) return;
  viewing = key;
  if (r.title) titles[key] = r.title;
  paint(r.messages);
  if (!(r.messages || []).length) bubble("tool", "no conversation saved for this one yet");
  document.querySelectorAll(".proj").forEach(
    el => el.classList.toggle("on", el.dataset.key === key));
  setViewingBanner();
}

$("backlive").onclick = () => { if (liveProject) openProject(liveProject); };

async function loadProject(initial){
  let r;
  try { r = await window.pywebview.api.open_project(initial === true); }
  catch (e) { return; }
  if (!r || !r.ok || !r.project) return;
  const moved = r.changed;
  liveProject = r.project;
  liveLabel = r.title;
  // messages is null when nothing changed — leave the screen alone, or the
  // poll wipes a conversation somebody is reading.
  if (r.messages !== null && r.messages !== undefined){
    viewing = r.project;
    paint(r.messages);
    if (moved) bubble("tool", "switched to " + r.title + " — this model has its own thread");
    else if (r.messages.length) bubble("tool", "picking up where you left off");
  }
  setViewingBanner();
  loadProjects();
}

refresh(); setInterval(() => { if (!busy) refresh(); }, 5000);
loadSetup(); loadProject(true);
setInterval(() => { if (!busy) loadProject(false); }, 6000);
$("box").focus();
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
