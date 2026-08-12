"""The Fusion Chat palette — a docked webview inside Fusion.

The palette points at the agent service on 127.0.0.1:7655 and talks to it
DIRECTLY over HTTP. It deliberately does not route through this add-in's
Python: the add-in's main thread is what serves bridge calls, so a chat
request that went palette → add-in → agent → bridge would deadlock on the
first Fusion tool call.

Stdlib only — this runs inside Fusion's embedded interpreter.
"""

import os
import subprocess
import traceback
import urllib.error
import urllib.request

import adsk.core

PALETTE_ID = "FusionChatPalette"
PALETTE_NAME = "Fusion Chat"
CMD_ID = "FusionChatShow"
CMD_NAME = "Fusion Chat"
CMD_TOOLTIP = "Model by prompting — opens the Fusion Chat panel"

SERVICE_URL = "http://127.0.0.1:%d/" % int(
    os.environ.get("ARGES_CHAT_PORT") or os.environ.get("FUSION_CHAT_PORT") or 7655)
HEALTH_URL = SERVICE_URL + "health"

# Populated by install(); torn down by uninstall().
_handlers = []          # Fusion holds command handlers weakly — keep them alive
_service = None         # subprocess.Popen for the agent service, if we spawned it


def _log(message, level="INFO"):
    try:
        from . import arges_impl          # noqa: F401  (never a package)
    except Exception:
        pass
    try:
        import sys
        impl = sys.modules.get("arges_impl")
        if impl is not None:
            impl._log("panel: " + message, level)
    except Exception:
        pass


# Fusion launched from Finder inherits a minimal PATH (/usr/bin:/bin:/usr/sbin:
# /sbin) with no Homebrew, so `uv` is not resolvable by name from inside the
# add-in even though it works in a terminal. Resolve it by absolute path.
_UV_CANDIDATES = (
    "/opt/homebrew/bin/uv",
    "/usr/local/bin/uv",
    os.path.expanduser("~/.local/bin/uv"),
    os.path.expanduser("~/.cargo/bin/uv"),
)

_EXTRA_PATH = "/opt/homebrew/bin:/usr/local/bin:" + os.path.expanduser("~/.local/bin")


def _find_uv():
    import shutil

    found = shutil.which("uv")
    if found:
        return found
    for candidate in _UV_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _service_alive(timeout=1.0):
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _start_service(repo_dir):
    """Launch the agent service detached, if it is not already up.

    Spawned rather than required-to-be-running so the panel works from a cold
    Fusion start. Failure is non-fatal: the palette still opens and its own
    health check explains what is wrong.
    """
    global _service
    if _service_alive():
        return "already running"
    agent_dir = os.path.join(repo_dir, "agent")
    if not os.path.isdir(agent_dir):
        return "agent/ not found at %s" % agent_dir

    uv = _find_uv()
    if uv is None:
        _log("uv not found on any known path; cannot start the agent service", "ERROR")
        return "uv not found — run scripts/fusion-chat.sh from a terminal instead"

    # Fusion embeds its own CPython and exports PYTHONHOME/PYTHONPATH to point
    # at it. Inherited by the subprocess, those make the venv's interpreter
    # load Fusion's stdlib and die instantly with
    #   ModuleNotFoundError: No module named 'encodings'
    # Scrub every PYTHON* variable so the child bootstraps from its own prefix.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env["PATH"] = _EXTRA_PATH + ":" + env.get("PATH", "")

    home = os.path.expanduser("~")
    state = os.path.join(home, ".arges")
    if not os.path.isdir(state) and os.path.isdir(os.path.join(home, ".fusion-mcp")):
        state = os.path.join(home, ".fusion-mcp")   # pre-rename install
    log_path = os.path.join(state, "agent.log")
    try:
        handle = open(log_path, "ab")
    except OSError:
        handle = subprocess.DEVNULL

    try:
        _service = subprocess.Popen(
            [uv, "run", "--frozen", "--no-sync",
             "--directory", agent_dir, "agent_service.py"],
            stdout=handle,
            stderr=handle,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,     # survives Fusion reloading the add-in
        )
        return "spawned pid %d" % _service.pid
    except Exception:
        _log("could not spawn the agent service:\n" + traceback.format_exc(), "ERROR")
        return "spawn failed — see addin.log"


def _show_palette(ui):
    palette = ui.palettes.itemById(PALETTE_ID)
    if palette is None:
        palette = ui.palettes.add(
            PALETTE_ID, PALETTE_NAME, SERVICE_URL,
            True,    # isVisible
            True,    # showCloseButton
            True,    # isResizable
            420, 620,
        )
        try:
            palette.dockingState = adsk.core.PaletteDockingStates.PaletteDockStateRight
        except Exception:
            pass    # docking is a preference, not a requirement
    palette.isVisible = True
    return palette


class _ShowHandler(adsk.core.CommandCreatedEventHandler):
    """Opens the panel. The command does its work on creation — there is no
    dialog to build, so no CommandInputs and no execute handler."""

    def __init__(self, repo_dir):
        super().__init__()
        self._repo_dir = repo_dir

    def notify(self, args):
        app = adsk.core.Application.get()
        ui = app.userInterface
        try:
            status = _start_service(self._repo_dir)
            _log("panel opened (%s)" % status)
            _show_palette(ui)
            try:
                args.command.isAutoExecute = True
                args.command.isExecutedWhenPreEmpted = False
            except Exception:
                pass
        except Exception:
            _log("failed to open the panel:\n" + traceback.format_exc(), "ERROR")
            ui.messageBox("Fusion Chat could not open:\n" + traceback.format_exc(),
                          "Fusion Chat")


def install(repo_dir):
    """Add the toolbar button. Safe to call repeatedly."""
    app = adsk.core.Application.get()
    ui = app.userInterface

    definition = ui.commandDefinitions.itemById(CMD_ID)
    if definition is None:
        definition = ui.commandDefinitions.addButtonDefinition(
            CMD_ID, CMD_NAME, CMD_TOOLTIP)

    handler = _ShowHandler(repo_dir)
    definition.commandCreated.add(handler)
    _handlers.append(handler)          # Fusion holds this weakly; see module docstring

    # UTILITIES tab on current builds, TOOLS on older ones — try both.
    panel = None
    for tab_id in ("ToolsTab", "UtilitiesTab"):
        tab = ui.allToolbarTabs.itemById(tab_id)
        if tab is None:
            continue
        for panel_id in ("SolidScriptsAddinsPanel", "UtilityPanel", "ToolsAddinsPanel"):
            panel = tab.toolbarPanels.itemById(panel_id)
            if panel is not None:
                break
        if panel is not None:
            break

    if panel is not None and panel.controls.itemById(CMD_ID) is None:
        panel.controls.addCommand(definition)
    return definition


def uninstall():
    """Remove the button and palette, and stop a service we spawned."""
    global _service
    app = adsk.core.Application.get()
    ui = app.userInterface

    palette = ui.palettes.itemById(PALETTE_ID)
    if palette is not None:
        try:
            palette.deleteMe()
        except Exception:
            pass

    for tab_id in ("ToolsTab", "UtilitiesTab"):
        tab = ui.allToolbarTabs.itemById(tab_id)
        if tab is None:
            continue
        for panel_id in ("SolidScriptsAddinsPanel", "UtilityPanel", "ToolsAddinsPanel"):
            p = tab.toolbarPanels.itemById(panel_id)
            if p is None:
                continue
            control = p.controls.itemById(CMD_ID)
            if control is not None:
                try:
                    control.deleteMe()
                except Exception:
                    pass

    definition = ui.commandDefinitions.itemById(CMD_ID)
    if definition is not None:
        try:
            definition.deleteMe()
        except Exception:
            pass

    del _handlers[:]

    if _service is not None and _service.poll() is None:
        # Only ours to stop — a service the user started stays up.
        _stop_service(_service)
    _service = None


def _stop_service(service):
    """Stop the whole spawned tree, without holding up Fusion's quit.

    _service is the `uv run` wrapper, and it was started with
    start_new_session=True, so terminate() signals the wrapper alone — if uv
    does not forward it, the Python child survives as an orphan still holding
    port 7655 and still polling a bridge that no longer exists. Signalling the
    process GROUP reaches both.

    Every wait here is deliberately short: this runs on Fusion's main thread
    while it is quitting, so seconds spent here are seconds the application
    appears to hang.
    """
    import signal
    import time

    try:
        group = os.getpgid(service.pid)
    except OSError:
        group = None

    try:
        if group is not None:
            os.killpg(group, signal.SIGTERM)
        else:
            service.terminate()
    except OSError:
        return          # already gone

    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        if service.poll() is not None:
            return
        time.sleep(0.05)

    # Refused to go quietly. Kill it outright and do not wait — an orphan on
    # the port is worse than a hard kill, and a hung quit is worse than both.
    try:
        if group is not None:
            os.killpg(group, signal.SIGKILL)
        else:
            service.kill()
    except OSError:
        pass
