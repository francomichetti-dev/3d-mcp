"""What both halves need to know about writing Fusion files.

The MCP server and the chat service are separate processes with separate virtual
environments, and both of them now write geometry: the server for Claude's tool
calls, the service for the panel's Save and Download buttons. The Fusion-side
code for that is a hundred lines of careful format handling, so it lives here
once and is imported by both rather than copied into each — a copy is a thing
that drifts, and the drift would show as one half exporting differently from the
other for reasons nobody can see.

Nothing here imports anything but the standard library. That is what lets the
chat service import it without the MCP server's dependencies.
"""

import json
import os
import re
import time
from pathlib import Path

# ----------------------------------------------------------------- formats --
# "f3d" is the Fusion archive: the whole parametric design in one local file. It
# is what "save" means here, because it is a plain local write. An expired
# subscription puts Fusion in read-only mode, and there Document.save() RETURNS
# TRUE and saves nothing — verified live, no new version, the document stays
# dirty — while exports keep working. So saving the state cannot go through
# Fusion's own save.
FORMATS = ("stl", "step", "3mf", "usd", "f3d")
SAVE_FORMAT = "f3d"

# The formats that can export a single body; the rest are component-level.
MESH_FORMATS = ("stl", "3mf")


# ------------------------------------------------------------ state, config --
STATE_DIR_NAME = ".arges"
LEGACY_STATE_DIR_NAME = ".fusion-mcp"


def state_dir():
    """`~/.arges`, unless only the pre-rename directory is there."""
    home = Path("~").expanduser()
    if not (home / STATE_DIR_NAME).is_dir() and (home / LEGACY_STATE_DIR_NAME).is_dir():
        return home / LEGACY_STATE_DIR_NAME
    return home / STATE_DIR_NAME


def config_path():
    return state_dir() / "config.json"


def default_save_dir():
    """Where files go when nobody has said otherwise.

    Downloads, because that is where a person looks for a file they asked a
    program to hand them. The environment variables are for an operator setting
    it at launch; the panel's config button writes the config file, which wins.
    """
    raw = (os.environ.get("ARGES_SAVE_DIR") or os.environ.get("ARGES_DOWNLOAD_DIR")
           or os.environ.get("FUSION_DOWNLOAD_DIR") or "~/Downloads")
    return Path(raw).expanduser()


DEFAULTS = {"save_dir": "", "format": "stl"}


def load_config():
    """The stored config with defaults filled in. Never raises.

    A missing or corrupt file is not worth failing a save over: it means "no
    preference expressed", and the defaults are exactly that.
    """
    out = dict(DEFAULTS)
    try:
        stored = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if isinstance(stored, dict):
        for key in DEFAULTS:
            value = stored.get(key)
            if isinstance(value, str) and value.strip():
                out[key] = value.strip()
    return out


def save_dir_from(config=None):
    config = load_config() if config is None else config
    raw = (config.get("save_dir") or "").strip()
    return Path(raw).expanduser() if raw else default_save_dir()


def format_from(config=None):
    config = load_config() if config is None else config
    fmt = (config.get("format") or "").strip().lower()
    return fmt if fmt in FORMATS else DEFAULTS["format"]


def write_config(save_dir=None, fmt=None):
    """Store a preference and return the config as it now stands.

    Validated before it is written, not when it is next used: a format the
    exporter cannot produce, or a directory that cannot be written, is a thing
    to say no to while somebody is looking at the dialog — not to discover later
    through a save that has nowhere to put its file.
    """
    config = load_config()
    if fmt is not None:
        wanted = fmt.strip().lower()
        if wanted not in FORMATS:
            raise ValueError(f"unknown format {fmt!r} — valid: {', '.join(FORMATS)}")
        config["format"] = wanted
    if save_dir is not None:
        raw = save_dir.strip()
        if raw:
            folder = Path(raw).expanduser()
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ValueError(f"cannot use {folder}: {exc}") from None
            if not os.access(folder, os.W_OK):
                raise ValueError(f"{folder} is not writable")
            config["save_dir"] = str(folder)
        else:
            config["save_dir"] = ""                  # back to the default
    path = config_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=1) + "\n", encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot write {path}: {exc}") from None
    return config


# ------------------------------------------------------------------ naming --
_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")


def slug(name):
    """Turn a component or file name into a safe filename stem.

    Dots are in the unsafe set, so no generated stem can read as a path
    component — which is what lets the callers accept a name from a model or a
    text field and build a path from it with no traversal check at all.
    """
    return _SLUG_RE.sub("_", name).strip("_-")[:60]


def output_path(folder, filename, fallback_name, fmt, overwrite=False):
    """Pick a file inside `folder`. The caller names the file, never the folder.

    The inverse of the MCP server's export-path handling, which takes a path and
    then has to prove it did not escape. Here there is nothing to escape from:
    the directory comes from configuration and only the stem comes from the
    caller, slugged, so "../../etc/passwd" is the inert name ".._.._etc_passwd"
    rather than a path.

    overwrite=False never replaces a file — an existing name gets -1, -2, … the
    way a browser download would, because a finished export is somebody's file.
    overwrite=True is for the autosave mirror, which is meant to be one file
    tracking the current state rather than a pile of them.
    """
    raw = (filename or "").strip()
    if raw.lower().endswith("." + fmt):
        raw = raw[: -(len(fmt) + 1)]
    stem = slug(raw)
    if not stem:
        stem = slug(fallback_name) or "design"
        if not overwrite:
            # Repeat exports of the same thing have to stay distinguishable.
            stem = stem + "_" + time.strftime("%Y%m%d-%H%M%S")

    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (stem + "." + fmt)
    if overwrite:
        return target
    for attempt in range(1, 1000):
        # lexists, not exists: a dangling symlink would otherwise be written
        # *through*, landing the file outside the folder entirely.
        if not os.path.lexists(target):
            return target
        target = folder / (stem + "-" + str(attempt) + "." + fmt)
    raise ValueError(f"{folder} already holds 999 files named {stem}*.{fmt}")


# -------------------------------------------------------- Fusion-side code --
#
# Parameters are embedded as a JSON document the snippet parses at runtime,
# never interpolated into the source. A component name containing a quote, a
# backslash or a newline is therefore inert data, not code.


def snippet(body, params):
    literal = json.dumps(json.dumps(params, ensure_ascii=True))
    return "import json as _fx_json\n_fx_params = _fx_json.loads(" + literal + ")\n" + body


EXPORT_BODY = '''
def _fx_export(_p):
    import os

    fmt = _p["format"]
    name = _p["name"]
    out_path = _p["path"]

    des = adsk.fusion.Design.cast(app.activeProduct)
    if des is None:
        return {"ok": False, "error": "no active Fusion design — open or create one and switch to the Design workspace"}

    root = des.rootComponent

    if not name:
        geom = root
        target = "whole design (root component)"
    else:
        bodies = []
        components = []
        occurrences = []
        for comp in des.allComponents:
            if comp.name == name:
                components.append((comp, "component '" + comp.name + "'"))
            for body in comp.bRepBodies:
                if body.name == name:
                    bodies.append((body, "body '" + body.name + "' in component '" + comp.name + "'"))
        for occ in root.allOccurrences:
            if occ.name == name:
                occurrences.append((occ, "occurrence '" + occ.name + "'"))

        if fmt in ("stl", "3mf"):
            pool = bodies + occurrences + components
        else:
            pool = occurrences + components
            if not pool and bodies:
                return {
                    "ok": False,
                    "error": fmt.upper() + " export is component-only; '" + name + "' is a body. Export the component or occurrence containing it, or use stl/3mf to export a single body.",
                    "candidates": [entry[1] for entry in bodies],
                }
        if not pool:
            return {"ok": False, "error": "nothing named '" + name + "' in this design — call fusion_state to see what exists"}
        if len(pool) > 1:
            return {
                "ok": False,
                "error": "'" + name + "' is ambiguous — pass an exact occurrence name such as 'Housing:1', or rename to something unique",
                "candidates": [entry[1] for entry in pool],
            }
        geom, target = pool[0]

    em = des.exportManager
    if fmt == "stl":
        opts = em.createSTLExportOptions(geom, out_path)
    elif fmt == "3mf":
        opts = em.createC3MFExportOptions(geom, out_path)
    elif fmt == "step":
        opts = em.createSTEPExportOptions(out_path, geom)
    elif fmt == "f3d":
        # The whole design, parametrically, in one local file: this is the
        # "save" that needs nothing from Fusion's cloud save.
        opts = em.createFusionArchiveExportOptions(out_path, geom)
    else:
        # createUSDExportOptions has taken its arguments in both orders across
        # Fusion releases; try one, fall back to the other rather than pinning
        # to a signature that a Fusion update can invalidate.
        try:
            opts = em.createUSDExportOptions(out_path, geom)
        except (TypeError, RuntimeError):
            opts = em.createUSDExportOptions(geom, out_path)

    try:
        opts.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementHigh
    except (AttributeError, RuntimeError):
        pass
    try:
        opts.filename = out_path
    except (AttributeError, RuntimeError):
        pass

    if not em.execute(opts):
        return {"ok": False, "error": "exportManager.execute() returned False for the " + fmt + " export of " + target}

    # Fusion may append its own extension rather than honouring the filename it
    # was given: a USD export to "part.usd" is actually written as
    # "part.usd.usdz" (a zip holding a .usdc). Checking only the requested path
    # would report a false failure for an export that succeeded.
    written = out_path
    if not os.path.exists(written):
        for suffix in (".usdz", ".usd", ".usdc", ".stl", ".step", ".stp", ".3mf", ".f3d"):
            if os.path.exists(out_path + suffix):
                written = out_path + suffix
                break

    size = os.path.getsize(written) if os.path.exists(written) else 0
    if size == 0:
        return {"ok": False, "error": "export reported success but no file was written to " + out_path}
    return {"ok": True, "format": fmt, "path": written, "bytes": size, "target": target}


result = _fx_export(_fx_params)
'''
