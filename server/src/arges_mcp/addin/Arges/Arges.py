"""Arges add-in loader.

Deliberately thin and dependency-free: all logic lives in ``arges_impl``
so it can be hot-reloaded via ``POST /reload``.  The only job here is to make
sure a failure to even import that module still leaves a trace — otherwise a
bootstrap crash produces no log, no listener and no symptom at all.
"""

import importlib.util
import os
import sys
import traceback
from datetime import datetime

def _state_dir():
    """~/.arges, or the pre-rename ~/.fusion-mcp when only that one exists."""
    home = os.path.expanduser("~")
    current = os.path.join(home, ".arges")
    legacy = os.path.join(home, ".fusion-mcp")
    if not os.path.isdir(current) and os.path.isdir(legacy):
        return legacy
    return current


_STATE_DIR = _state_dir()
_LOG_PATH = os.path.join(_STATE_DIR, "addin.log")

# All Fusion add-ins share one interpreter and one sys.modules, so the impl is
# loaded from an explicit path under a namespaced key rather than by putting our
# directory on sys.path — that would let a generic module name collide with
# another add-in's in either direction.
_IMPL_NAME = "arges_impl"

_impl = None
_alert_shown = False


def _log(message):
    """Append to ~/.arges/addin.log without importing anything fallible."""
    line = "%s %-5s %s\n" % (datetime.now().isoformat(timespec="seconds"), "ERROR", message)
    try:
        os.makedirs(_STATE_DIR, mode=0o700, exist_ok=True)
        fd = os.open(_LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8", "replace"))
        finally:
            os.close(fd)
    except Exception:
        pass


def _bootstrap_failure(stage, detail):
    global _alert_shown
    _log("loader: %s failed:\n%s" % (stage, detail))
    if _alert_shown:
        return
    _alert_shown = True
    # adsk is only importable inside Fusion, and a bootstrap crash can happen
    # before it is available at all.
    try:
        import adsk.core

        app = adsk.core.Application.get()
        if app is not None and app.userInterface is not None:
            app.userInterface.messageBox(
                "Arges failed to start (%s).\n"
                "Details: ~/.arges/addin.log" % stage,
                "Arges",
            )
    except Exception:
        pass


def _load_impl():
    """Import the implementation module from this add-in's own directory."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, _IMPL_NAME + ".py")
    existing = sys.modules.get(_IMPL_NAME)
    if existing is not None:
        # Whichever add-in loads a given name first wins it for the whole
        # process, so trusting the key without checking where it came from would
        # leave exactly the collision this namespaced name is meant to prevent.
        #
        # Compared by realpath, not abspath: a checkout install symlinks this
        # add-in into Fusion's AddIns folder, and abspath does not follow
        # symlinks — so the same file reached through the link and through the
        # repo produced two different strings and the add-in refused to load
        # itself.  realpath collapses both to one identity while still catching
        # a genuinely different module that grabbed the name.
        origin = getattr(existing, "__file__", "") or ""
        if origin and os.path.realpath(origin) == os.path.realpath(path):
            return existing
        raise ImportError(
            "sys.modules[%r] belongs to %r, not this add-in" % (_IMPL_NAME, origin or None)
        )
    spec = importlib.util.spec_from_file_location(_IMPL_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError("could not load %s from %s" % (_IMPL_NAME, path))
    module = importlib.util.module_from_spec(spec)
    # Registered before exec_module so the module's own importlib.reload() path
    # (sys.modules[__name__]) resolves to itself.
    sys.modules[_IMPL_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_IMPL_NAME, None)
        raise
    return module


def run(context):
    global _impl
    try:
        module = _load_impl()
        _impl = module
        module.run(context)
    except Exception:
        _bootstrap_failure("import/run", traceback.format_exc())


def stop(context):
    global _impl
    try:
        if _impl is not None:
            _impl.stop(context)
    except Exception:
        _log("loader: stop failed:\n%s" % traceback.format_exc())
    finally:
        _impl = None
