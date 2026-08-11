"""The Rhino chat window's logic, without opening a window.

This file had no tests at all. It is the piece a non-developer actually
touches — double-click, type, model — so its failures are the ones least
likely to be diagnosed by the person hitting them, and "claude is not
installed" on a machine that has it is indistinguishable from the truth.

`webview` is imported lazily inside the one function that opens a window, so
everything here can be exercised on a machine with no GUI, no Rhino and no
network.

Covered:

  * finding the `claude` CLI, including inside Claude Desktop, where it was
    genuinely missed on a real machine
  * the MCP config the window writes, which is what makes `claude mcp add`
    unnecessary for chat users
  * file permissions, which regressed once already when a `_restrict` call was
    deleted alongside an unrelated feature

    python3 tests/test_rhino_chat.py
"""

import importlib.util
import re
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "scripts" / "rhino" / "rhino-chat.py"

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


def load(env=None, call=None):
    """Import the window fresh under a chosen environment, and optionally call
    into it before the environment is put back.

    Two different times matter here. Module-level constants (HOME, CONFIG_DIR)
    read the environment at IMPORT time, while find_claude reads PATH and
    LOCALAPPDATA at CALL time. An earlier version of this helper restored the
    environment before the test called anything, so find_claude searched the
    developer's real machine and found the real CLI - the tests passed against
    the wrong thing entirely. Anything environment-sensitive must run inside.
    """
    saved = dict(os.environ)
    if env is not None:
        os.environ.clear()
        os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location("rhino_chat_under_test", SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return (module, call(module)) if call else module
    finally:
        os.environ.clear()
        os.environ.update(saved)


def found_claude(env):
    """find_claude() evaluated inside `env`."""
    return load(env, call=lambda m: m.find_claude())[1]


TMP = Path(tempfile.mkdtemp(prefix="rhino-chat-test-"))
EMPTY_PATH_DIR = TMP / "empty-path"
EMPTY_PATH_DIR.mkdir()


def base_env(home, localappdata=""):
    """An environment with nothing findable unless the test puts it there."""
    return {
        "PATH": str(EMPTY_PATH_DIR),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "LOCALAPPDATA": str(localappdata),
        "APPDATA": "",
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    }


# ------------------------------------------------------------ find_claude --
print("Finding the claude CLI")

home = TMP / "home-none"
home.mkdir()
check("nothing installed anywhere -> empty string",
      found_claude(base_env(home)), "")

# The native installer writes here without updating PATH for a running
# process, so a fresh install is invisible to `which` until a new terminal.
home = TMP / "home-local-bin"
(home / ".local" / "bin").mkdir(parents=True)
native = home / ".local" / "bin" / "claude"
native.write_text("#!/bin/sh\n", encoding="utf-8")
check("the native installer's path is found without PATH",
      found_claude(base_env(home)), str(native))

# The one this suite exists for. Claude Code ships inside Claude Desktop under
# a versioned directory, with nothing on PATH — observed on a real machine,
# where the window reported the CLI missing on a machine that had it.
home = TMP / "home-desktop"
home.mkdir()
lad = TMP / "localappdata-desktop"
desktop = (lad / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming"
           / "Claude" / "claude-code")
(desktop / "1.0.60").mkdir(parents=True)
older = desktop / "1.0.60" / "claude.exe"
older.write_text("old", encoding="utf-8")
check("claude inside Claude Desktop is found",
      found_claude(base_env(home, lad)), str(older))

# Two versions installed: the newest must win, not whichever glob returns first.
(desktop / "1.0.61").mkdir(parents=True)
newer = desktop / "1.0.61" / "claude.exe"
newer.write_text("new", encoding="utf-8")
os.utime(older, (1_000_000, 1_000_000))
os.utime(newer, (2_000_000, 2_000_000))
check("the newest Claude Desktop version wins",
      found_claude(base_env(home, lad)), str(newer))

# A Packages directory with no Claude in it must not match something else.
home = TMP / "home-other-pkg"
home.mkdir()
lad2 = TMP / "localappdata-other"
(lad2 / "Packages" / "SomethingElse_abc").mkdir(parents=True)
check("an unrelated package is not mistaken for claude",
      found_claude(base_env(home, lad2)), "")

# PATH still wins when it has an answer, so an explicit install is never
# shadowed by a stale copy inside Claude Desktop.
home = TMP / "home-path-wins"
home.mkdir()
on_path = EMPTY_PATH_DIR / ("claude.exe" if os.name == "nt" else "claude")
on_path.write_text("#!/bin/sh\n", encoding="utf-8")
on_path.chmod(0o755)
check("PATH takes precedence over Claude Desktop",
      found_claude(base_env(home, lad)), str(on_path))
on_path.unlink()


# ------------------------------------------------------------ MCP config ----
# The window passes this inline with --strict-mcp-config, which is why a chat
# user never has to run `claude mcp add`. If it stopped naming the server
# "rhino", the --allowedTools entries (mcp__rhino__*) would silently stop
# matching and every tool call would wait for an approval nobody can give.
print("The MCP config it writes")

home = TMP / "home-config"
home.mkdir()
module = load(base_env(home))
config_path = module.write_mcp_config()
truthy("it writes a file", os.path.isfile(config_path))

config = json.loads(Path(config_path).read_text(encoding="utf-8"))
check("the server is named rhino", sorted(config["mcpServers"]), ["rhino"])
entry = config["mcpServers"]["rhino"]
check("it runs the same Python running the window", entry["command"], sys.executable)
truthy("pointed at the MCP server", any("rhino_mcp.py" in a for a in entry["args"]))
truthy("with an absolute path",
       all(os.path.isabs(a) for a in entry["args"] if a.endswith(".py")))

# The tool names the window pre-approves must match what the server exposes.
# test_consistency.py pins those two lists together; this checks the prefix
# they are built with, which is what ties them to the config above.
allowed = [t for t in module.RHINO_TOOLS]
truthy("every allowed tool is namespaced to this server",
       all(t.startswith("mcp__rhino__") for t in allowed))
check("five tools, matching the server", len(allowed), 5)
truthy("including the memory pair, or the model is told to use tools the "
       "window forbids",
       {"mcp__rhino__rhino_remember", "mcp__rhino__rhino_recall"} <= set(allowed))


# ----------------------------------------------------------- permissions ----
# A `_restrict` call was once deleted along with an unrelated feature, leaving
# attachments world-readable while the UI said otherwise. chmod is meaningless
# on Windows, so the mode assertions only run where they mean something.
print("File permissions")

home = TMP / "home-perms"
home.mkdir()
module = load(base_env(home))
module.ensure_dirs()

truthy("the config directory exists", os.path.isdir(module.CONFIG_DIR))
truthy("the attachments directory exists", os.path.isdir(module.ATTACH_DIR))

if os.name != "nt":
    for label, path in [("config dir", module.CONFIG_DIR),
                        ("attachments dir", module.ATTACH_DIR)]:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        check(f"{label} is 0700", oct(mode), oct(0o700))

    # copy2 preserves the SOURCE's mode, so a world-readable original stays
    # world-readable unless it is re-restricted after copying. That is the
    # exact regression.
    loose = TMP / "world-readable.png"
    loose.write_bytes(b"\x89PNG\r\n")
    loose.chmod(0o644)
    restricted = Path(module.ATTACH_DIR) / "copied.png"
    import shutil as _shutil
    _shutil.copy2(loose, restricted)
    module.restrict(str(restricted))
    mode = stat.S_IMODE(os.stat(restricted).st_mode)
    check("a copied attachment ends up 0600", oct(mode), oct(0o600))
else:
    # icacls is what actually restricts on Windows; assert the code reaches for
    # it rather than trusting chmod, which only toggles the read-only bit.
    source_text = SOURCE.read_text(encoding="utf-8")
    truthy("Windows restriction goes through icacls", "icacls" in source_text)

# ---------------------------------------------------------- the window UI --
# The page is a string inside the module, and pywebview renders it with the
# same engines as any browser, so the lessons the Fusion panel paid for apply
# verbatim. No harness runs this page, so what the panel pins behaviourally is
# pinned statically here — presence of the guard, not its computed effect.
print("The window carries the panel's fixes")
page = SOURCE.read_text(encoding="utf-8")

# The engine draws the open <select> list itself; without this it is a white
# popup over a dark window (live on the Fusion panel, 2026-08-09).
truthy("the page declares itself dark to the engine", "color-scheme:dark" in page)
truthy("and pins the dropdown rows to the window colours",
       "#model option,#effort option{background:var(--panel)" in page)

# An id rule setting display outranks [hidden] without this.
truthy("the hidden attribute always wins", "[hidden]{display:none!important}" in page)

# Stop lives in the working banner beside the wheel, orange; the red form
# button it replaces is gone entirely.
truthy("the banner carries the stop button", 'id="banner-stop"' in page)
truthy("styled with the warn colour, like the elsewhere cancel",
       "#banner-stop{background:transparent;border:1px solid var(--warn)" in page)
check("the old red halt button is gone", "halt" in page, False)

# A cancelled turn must never read as a finished one.
truthy("a stopped turn has its own ending", '"Stopped"' in page)
truthy("distinct from the green Done", "#banner.stopped{color:var(--warn)}" in page)

# loadSetup runs on every Settings visit; filling without clearing duplicated
# the dropdown lists each time.
truthy("the pickers are cleared before filling", 'el.innerHTML = ""' in page)

# The settings confirmation called say('notice', ...), which was never
# defined: the value stuck, the notice threw inside the async handler, the
# person saw nothing. Matched as a call — the comment recording the bug is
# allowed to name it.
check("no call to the phantom say() helper", "say('" in page, False)

# Every element the script reaches for must exist in the markup. There is no
# harness for this page — a typo'd id is a TypeError at click time, in a
# window nobody is watching the console of.
print("The page and its script agree")
page = SOURCE.read_text(encoding="utf-8").split('PAGE = r"""', 1)[1]
ids = set(re.findall(r'id="([^"]+)"', page))
used = set(re.findall(r'\$\("([^"]+)"\)', page))
check("no element is referenced that the page does not define",
      sorted(used - ids), [])
truthy("and the script does reach for elements at all", len(used) > 20)


# ------------------------------------------------------------------ video --
# ffmpeg and Whisper are OPTIONAL. Someone with neither must still get a
# working chat — the whole feature degrades to "videos are not supported here"
# rather than breaking startup.
print("Video is optional, not required")
source_text = SOURCE.read_text(encoding="utf-8")
head = source_text.split("class Api", 1)[0]
check("faster_whisper is never imported at module scope",
      "\nfrom faster_whisper" in head or "\nimport faster_whisper" in head, False)
truthy("it is imported inside the function that needs it",
       "    from faster_whisper import WhisperModel" in source_text)
truthy("ffmpeg is looked for at attach time, not startup",
       "def find_ffmpeg" in source_text)
truthy("and the install hint is the one for THIS platform",
       "FFMPEG_HINT = {" in source_text and '"macos": "brew install ffmpeg"' in source_text)

# Frames and transcripts are pictures and speech from somebody's workshop.
# The plain-attachment path restricts what it copies; these must too.
frames = source_text.split("def attach", 1)[1].split("def clear_attachments", 1)[0]
truthy("extracted frames are restricted", 'restrict(frame["path"])' in frames)
truthy("and so is the transcript", "restrict(tpath)" in frames)
truthy("a re-taken frame is restricted too",
       "restrict(target)" in source_text.split("def reframe", 1)[1])

# The picker description may contain only word characters and spaces —
# pywebview rejects a comma at click time, not at startup.
picker = re.search(r'file_types=\("([^"]*)\(', source_text)
truthy("the file filter description has no comma", picker)
if picker:
    check("(pywebview rejects one at click time)", "," in picker.group(1), False)


# The update path ships beside the setup path, and the one security-relevant
# step in it — icacls on the EXISTING token, because the old install's chmod
# restricted nothing — must stay written down.
update = (SOURCE.parent / "UPDATE.md")
truthy("UPDATE.md ships next to SETUP.md", update.exists())
if update.exists():
    text = update.read_text(encoding="utf-8")
    truthy("and re-applies the token permissions", "icacls" in text)
    truthy("and forbids regenerating the token", "Do NOT regenerate" in text)

import shutil as _cleanup_shutil  # noqa: E402
_cleanup_shutil.rmtree(TMP, ignore_errors=True)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
