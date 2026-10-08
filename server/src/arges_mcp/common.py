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
import tempfile
import time
from pathlib import Path

try:
    import fcntl
except ImportError:                 # Windows: no flock. The Fusion half is
    fcntl = None                    # verified on macOS only; this keeps a
                                    # Windows import working, unlocked.

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


# ------------------------------------------------------------- importing --
#
# Asked to import a motor from Downloads, a model once spent its turn on two
# tool lookups, three Bash searches through Downloads and Fusion's cache, and
# two hand-written importManager attempts. Finding the file is host-side work
# that needs no Fusion and no shell, so it lives here, and the import itself is
# one snippet (IMPORT_BODY, below).

IMPORT_FORMATS = {
    ".f3d": "f3d", ".f3z": "f3d",
    ".step": "step", ".stp": "step",
    ".iges": "iges", ".igs": "iges",
    ".sat": "sat", ".smt": "smt",
    ".stl": "mesh", ".obj": "mesh", ".3mf": "mesh",
}
# The units a mesh file is read in (STL carries none); IMPORT_BODY maps each to
# Fusion's MeshUnits enum.
MESH_UNITS = ("mm", "cm", "m", "in", "ft")
IMPORT_LIST_LIMIT = 30


def import_format(name):
    """The importer a file needs ("f3d", "step", "iges", "sat", "smt", "mesh"), or None."""
    return IMPORT_FORMATS.get(Path(str(name)).suffix.lower())


def import_roots(extra=()):
    """Where a file to import is looked for: the save folder, then the usual places.

    Top level only, on purpose. A file somebody means to import was put
    somewhere they can name, and walking whole trees is slow and returns
    things nobody meant.
    """
    home = Path("~").expanduser()
    candidates = [save_dir_from(), home / "Downloads", home / "Desktop", home / "Documents",
                  *[Path(p) for p in extra], state_dir() / "imports"]
    out, seen = [], set()
    for folder in candidates:
        try:
            real = folder.resolve()
        except OSError:
            continue
        if real not in seen and folder.is_dir():
            seen.add(real)
            out.append(folder)
    return out


def importable_files(roots, limit=IMPORT_LIST_LIMIT):
    """Importable files at the top of each root, newest first."""
    found = []
    for folder in roots:
        try:
            entries = list(Path(folder).iterdir())
        except OSError:
            continue
        for path in entries:
            fmt = import_format(path.name)
            if fmt is None or path.name.startswith("."):
                continue
            try:
                if not path.is_file():
                    continue
                st = path.stat()
            except OSError:
                continue
            found.append({"name": path.name, "path": str(path), "format": fmt,
                          "bytes": st.st_size, "mtime": st.st_mtime,
                          "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                          "folder": str(folder)})
    found.sort(key=lambda f: f["mtime"], reverse=True)
    for entry in found:
        entry.pop("mtime")
    return found[:limit]


def resolve_import(spec, roots, home=None):
    """The file `spec` names: a path, a ~ path, or a bare name found in `roots`.

    A bare name matches with or without its extension and in any case, newest
    first, because "import the motor from Downloads" is how people say it.
    ValueError, naming what is there, when nothing matches.
    """
    raw = str(spec or "").strip()
    if not raw:
        raise ValueError("name a file to import, or call with no file to list them")
    if raw.startswith("~"):
        base = Path(home) if home else Path("~").expanduser()
        path = base / raw[2:] if raw.startswith("~/") else Path(raw).expanduser()
    elif os.path.isabs(raw) or os.sep in raw:
        path = Path(raw)
    else:
        path = None
    if path is not None:
        # Absolute before anything else: a relative path means one thing here
        # and another in Fusion, whose working directory is its own.
        path = Path(os.path.abspath(path))
        if path.is_dir():
            raise ValueError(f"{path} is a folder, not a file")
        if not path.is_file():
            raise ValueError(f"no file at {path}")
        if import_format(path.name) is None:
            raise ValueError(f"cannot import {path.suffix or 'that'} files; importable: "
                             + ", ".join(sorted(IMPORT_FORMATS)))
        return path
    wanted = raw.lower()
    candidates = importable_files(roots, limit=10_000)
    for entry in candidates:
        name = entry["name"].lower()
        if name == wanted or Path(name).stem == wanted:
            return Path(entry["path"])
    recent = ", ".join(e["name"] for e in candidates[:8]) or "none"
    raise ValueError(f"no importable file named {raw!r} in "
                     + ", ".join(str(r) for r in roots) + f". Most recent there: {recent}")


# -------------------------------------------------------------- the mirror --
#
# A save is the design written to one .f3d per document, overwritten each time:
# a mirror of the current state. It used to be "<folder>/<document name>.f3d",
# and on 2026-10-08 that destroyed a real file. The person had downloaded a
# motor as Untitled.f3d into Downloads (the default folder), asked for it to be
# imported, and the first successful call wrote the open, empty "Untitled"
# design over it. Every unsaved Fusion document is called Untitled, so the name
# could never have been enough on its own.
#
# So the mirror keeps a ledger of what it wrote and replaces a file only if the
# ledger shows this tool wrote it, for the same document, and nobody has
# changed it since. Anything else gets the next free name.

LEDGER_NAME = "saves.json"
# The add-in stamps this attribute to give an unsaved document a stable chat
# identity. The mirror reads the same one, so a document has one identity
# throughout. tests/test_consistency.py pins both to the add-in's constants.
DOC_ATTR_GROUP = "FusionChat"
DOC_ATTR_NAME = "chatKey"
MAX_LEDGER_FILES = 500


def _ledger_path():
    return state_dir() / LEDGER_NAME


class _LedgerLock:
    """Exclusive across processes for one read-modify-write of the ledger.

    Choosing a path, reserving it and recording a save all go through this:
    Claude Code's MCP server and every panel session share one ledger, and two
    of them picking the same unwritten path before either had exported was a
    way to overwrite a mirror (found in review).
    """

    def __enter__(self):
        lock_path = Path(str(_ledger_path()) + ".lock")
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        if fcntl is not None:
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def _load_ledger():
    """The ledger, or an empty one. Corrupt reads as empty, which is the safe
    direction: nothing looks like the tool's, so nothing is overwritten."""
    try:
        data = json.loads(_ledger_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = {}
    docs = data.get("docs") if isinstance(data.get("docs"), dict) else {}
    files = data.get("files") if isinstance(data.get("files"), dict) else {}
    return {"version": 1, "docs": docs, "files": files}


def _save_ledger(ledger):
    # Bounded: drop records of files that no longer exist once it grows.
    if len(ledger["files"]) > MAX_LEDGER_FILES:
        for stale in [p for p in ledger["files"] if not os.path.lexists(p)]:
            ledger["files"].pop(stale, None)
            if len(ledger["files"]) <= MAX_LEDGER_FILES:
                break
    path = _ledger_path()
    tmp_fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".saves-")
    with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=1)
    os.replace(tmp, path)


def _signature(path):
    st = os.stat(path)
    return [st.st_size, st.st_mtime_ns]


def _is_ours(ledger, path, key):
    """Did this tool write `path` for this document, with nothing changed since?

    A document with no identity owns nothing. Treating "" as an owner let one
    unidentified document overwrite another's mirror (found in review).
    """
    if not key:
        return False
    record = ledger["files"].get(str(path))
    if not isinstance(record, dict) or record.get("doc") != key:
        return False
    if not os.path.lexists(path):
        return True               # ours, and deleted since: free to write again
    if os.path.islink(path):
        return False
    try:
        return _signature(path) == record.get("sig")
    except OSError:
        return False


def _reserve(ledger, path, key):
    """Create `path` empty, exclusively, and record it as this document's.

    The empty file is what makes the reservation visible to every other
    process, the next one choosing a name included. A save that then fails
    gives it back with release_mirror.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        os.close(fd)
    except FileExistsError:
        pass                      # ours already (a mirror deleted and re-picked)
    ledger["files"][str(path)] = {"doc": key or "", "sig": _signature(path),
                                  "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if key:
        ledger["docs"][key] = str(path)


def mirror_path(folder, document_name, document_key):
    """Where a document's saved state goes. Never a file this tool did not write.

    The same document gets the same file every time, so the mirror tracks the
    current state rather than piling up copies. A file the person put there, a
    mirror they edited by hand, and another document's mirror are all left
    alone. The state goes to the next free name instead (tower-1.f3d), which is
    reserved before this returns.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    key = document_key or ""
    with _LedgerLock():
        ledger = _load_ledger()
        known = ledger["docs"].get(key) if key else None
        if known and Path(known).parent == folder and _is_ours(ledger, known, key):
            if not os.path.lexists(known):
                _reserve(ledger, Path(known), key)
                _save_ledger(ledger)
            return Path(known)
        stem = slug(document_name or "") or "design"
        for attempt in range(0, 1000):
            target = folder / (stem + ("-" + str(attempt) if attempt else "") + "." + SAVE_FORMAT)
            if _is_ours(ledger, target, key):
                return target
            if not os.path.lexists(target):
                _reserve(ledger, target, key)
                _save_ledger(ledger)
                return target
    raise ValueError(f"{folder} already holds 999 files named {stem}*.{SAVE_FORMAT}")


def record_mirror(path, document_key):
    """Note that the tool just wrote `path` for this document. Never raises.

    Called after a successful write, so the signature is the file as the tool
    left it. Any later change by somebody else no longer matches it.
    """
    try:
        with _LedgerLock():
            ledger = _load_ledger()
            key = document_key or ""
            ledger["files"][str(path)] = {"doc": key, "sig": _signature(path),
                                          "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            if key:
                ledger["docs"][key] = str(path)
            _save_ledger(ledger)
    except (OSError, ValueError, TypeError):
        pass


def release_mirror(path, document_key):
    """Give back a reservation whose save failed. Never raises.

    Only an empty file still matching the reservation is removed: a failed
    save must not leave an empty Untitled-1.f3d in somebody's Downloads, and a
    real save must never be deleted by this.
    """
    try:
        with _LedgerLock():
            ledger = _load_ledger()
            if (_is_ours(ledger, path, document_key) and os.path.lexists(path)
                    and os.path.getsize(path) == 0):
                os.unlink(path)
                ledger["files"].pop(str(path), None)
                _save_ledger(ledger)
    except (OSError, ValueError, TypeError):
        pass


# -------------------------------------------------------- Fusion-side code --
#
# Parameters are embedded as a JSON document the snippet parses at runtime,
# never interpolated into the source. A component name containing a quote, a
# backslash or a newline is therefore inert data, not code.


def snippet(body, params):
    literal = json.dumps(json.dumps(params, ensure_ascii=True))
    return "import json as _fx_json\n_fx_params = _fx_json.loads(" + literal + ")\n" + body


# The open document's name and identity, resolved the way the add-in resolves a
# chat's key: the stamped attribute first, then the saved file's id, and only
# then a fresh key stamped into the document. A save happens after a change, so
# the document is modified already and the stamp costs nothing.
IDENTITY_BODY = '''
def _fx_identity(_p):
    doc = app.activeDocument
    des = adsk.fusion.Design.cast(app.activeProduct)
    key = None
    if des is not None:
        try:
            attr = des.attributes.itemByName(_p["group"], _p["attr"])
            if attr is not None and attr.value:
                key = attr.value
        except Exception:
            key = None
    if key is None and doc is not None:
        try:
            if doc.dataFile is not None:
                key = "file:" + doc.dataFile.id
        except Exception:
            key = None
    if key is None and des is not None:
        import uuid
        stamped = "doc:" + uuid.uuid4().hex
        try:
            des.attributes.add(_p["group"], _p["attr"], stamped)
            key = stamped
        except Exception:
            key = None
    return {"name": doc.name if doc is not None else "", "key": key}


result = _fx_identity(_fx_params)
'''

IDENTITY_PARAMS = {"group": DOC_ATTR_GROUP, "attr": DOC_ATTR_NAME}


# What a refused import says. Measured on Fusion 2704 with an expired
# subscription (2026-10-08): importManager refuses archives and STEP files into
# an unsaved design, importToNewDocument refuses a local file "while expired",
# and TemporaryBRepManager.createFromFile rejects a STEP. Copying bodies from
# another open document and adding a mesh with meshBodies.add both still work,
# so the message points at those. It names no file, so its error signature is
# the same for every file and one lesson can cover them all.
IMPORT_REFUSED = (
    "Fusion refused this import ({reason}). It refuses imports into an unsaved design, and on an "
    "expired subscription it refuses archive and STEP imports outright. What still works: if the "
    "design is open in another Fusion tab, fusion_import(from_document=...) copies its bodies "
    "(fusion_import() lists the open tabs); mesh files (STL, OBJ, 3MF) always import."
)

def import_params(path, fmt, name="", units="mm"):
    """What IMPORT_BODY is given: the file, how to read it, and what to say if refused."""
    return {"path": str(path), "format": fmt, "name": name, "units": units,
            "refused": IMPORT_REFUSED}


# A new component holding whatever `add` puts in it: inside a base feature when
# the design keeps history (bodies and meshes need one there), directly when not.
_NEW_COMPONENT = '''
def _fx_new_component(des, name, add):
    root = des.rootComponent
    occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    if name:
        occ.component.name = name
    try:
        if des.designType == adsk.fusion.DesignTypes.ParametricDesignType:
            base = occ.component.features.baseFeatures.add()
            base.startEdit()
            try:
                add(occ.component, base)
            finally:
                # An edit left open would swallow whatever the next call does.
                base.finishEdit()
        else:
            add(occ.component, None)
    except Exception:
        # Nothing half-made is left behind: the failure is the result.
        try:
            occ.deleteMe()
        except Exception:
            pass
        raise
    return occ
'''

# Brings one file into the open design as a new component. Failures raise, as
# any failing code does, so they come back as a traceback with lessons and the
# autosave does not write a half-imported design. No size is reported: read in
# the same call, a new occurrence's box is stale (measured: all zeros), so the
# server measures it in a call of its own (MEASURE_BODY).
IMPORT_BODY = _NEW_COMPONENT + '''
def _fx_import(_p):
    import os

    path, fmt, name = _p["path"], _p["format"], _p["name"]
    des = adsk.fusion.Design.cast(app.activeProduct)
    if des is None:
        raise RuntimeError("no active Fusion design to import into: open or create one first")
    if not os.path.isfile(path):
        raise RuntimeError("the file to import is gone: " + path)
    root = des.rootComponent

    def report(method, occurrences):
        return {"ok": True, "file": path, "format": fmt, "method": method,
                "occurrences": [o.name for o in occurrences]}

    if fmt == "mesh":
        units = getattr(adsk.fusion.MeshUnits, {
            "mm": "MillimeterMeshUnit", "cm": "CentimeterMeshUnit", "m": "MeterMeshUnit",
            "in": "InchMeshUnit", "ft": "FootMeshUnit"}[_p.get("units") or "mm"])

        def add_mesh(comp, base):
            if base is not None:
                comp.meshBodies.add(path, units, base)
            else:
                comp.meshBodies.add(path, units)
        return report("mesh", [_fx_new_component(des, name, add_mesh)])

    im = app.importManager
    before = set(o.name for o in root.occurrences)
    occurrences, reason = [], None
    try:
        make = {"f3d": getattr(im, "createFusionArchiveImportOptions", None),
                "step": getattr(im, "createSTEPImportOptions", None),
                "iges": getattr(im, "createIGESImportOptions", None),
                "sat": getattr(im, "createSATImportOptions", None),
                "smt": getattr(im, "createSMTImportOptions", None)}.get(fmt)
        if make is None:
            raise RuntimeError("this Fusion has no " + fmt + " importer")
        options = make(path)
        to_target2 = getattr(im, "importToTarget2", None)
        if callable(to_target2):
            got = to_target2(options, root)
            occurrences = [got.item(i) for i in range(got.count)] if got else []
        elif not im.importToTarget(options, root):
            raise RuntimeError("importToTarget returned False")
        if not occurrences:
            occurrences = [o for o in root.occurrences if o.name not in before]
        if not occurrences:
            # Reported as success with nothing to show for it, and autosaved,
            # until a review caught it.
            raise RuntimeError("Fusion reported the import done but nothing arrived")
    except Exception as exc:
        reason = str(exc)
    if reason is None:
        if name and len(occurrences) == 1:
            occurrences[0].component.name = name
        return report("importManager", occurrences)

    if fmt in ("step", "iges", "sat", "smt"):
        # Solids can also be read straight from the file, without importManager.
        try:
            solids = adsk.fusion.TemporaryBRepManager.get().createFromFile(path)
            bodies = [solids.item(i) for i in range(solids.count)]
            if bodies:
                def add_bodies(comp, base):
                    for body in bodies:
                        if base is not None:
                            comp.bRepBodies.add(body, base)
                        else:
                            comp.bRepBodies.add(body)
                return report("createFromFile", [_fx_new_component(des, name, add_bodies)])
        except Exception as exc:
            reason = reason + "; reading it directly: " + str(exc)
    raise RuntimeError(_p["refused"].replace("{reason}", " ".join(reason.split())))


result = _fx_import(_fx_params)
'''

# The other open documents, each with a reference that tells same-named ones
# apart ("Untitled #2"): every unsaved document is called Untitled.
_DOCUMENT_REFS = '''
def _fx_document_refs():
    docs = list(app.documents)
    counts, seen, refs = {}, {}, []
    for d in docs:
        counts[d.name] = counts.get(d.name, 0) + 1
    for d in docs:
        seen[d.name] = seen.get(d.name, 0) + 1
        refs.append((d.name if counts[d.name] == 1 else d.name + " #" + str(seen[d.name]), d))
    return refs
'''

OPEN_DOCUMENTS_BODY = _DOCUMENT_REFS + '''
def _fx_open_documents(_p):
    active = app.activeDocument
    out = []
    for ref, d in _fx_document_refs():
        if d == active:
            continue
        des = adsk.fusion.Design.cast(d.products.itemByProductType("DesignProductType"))
        if des is None:
            continue
        root = des.rootComponent
        bodies = list(root.bRepBodies) + [b for o in root.allOccurrences for b in o.bRepBodies]
        # Names and size, because a reference alone does not say which tab is
        # which: every unsaved tab is called Untitled, and a model shown
        # "Untitled #2, 1 body" did not recognise the motor it was looking for.
        size = None
        if bodies:
            boxes = [b.boundingBox for b in bodies]
            lo = [min(getattr(bb.minPoint, a) for bb in boxes) for a in "xyz"]
            hi = [max(getattr(bb.maxPoint, a) for bb in boxes) for a in "xyz"]
            size = [round((h - l) * 10, 3) + 0.0 for l, h in zip(lo, hi)]
        out.append({"ref": ref, "bodies": len(bodies), "body_names": [b.name for b in bodies[:8]],
                    "size_mm": size, "saved": d.dataFile is not None})
    return {"documents": out}


result = _fx_open_documents(_fx_params)
'''

# Copies another open document's solid bodies into a new component: geometry
# without history. On an expired subscription this is the one way to bring in
# a design that only exists as an open tab or as an .f3d Fusion will not import.
COPY_BODY = _NEW_COMPONENT + _DOCUMENT_REFS + '''
def _fx_copy(_p):
    ref, name = _p["document"], _p["name"]
    des = adsk.fusion.Design.cast(app.activeProduct)
    if des is None:
        raise RuntimeError("no active Fusion design to copy into: open or create one first")
    active = app.activeDocument
    refs = _fx_document_refs()
    found = [d for r, d in refs if r == ref]
    if not found:
        same = [r for r, d in refs if r.split(" #")[0] == ref]
        if len(same) > 1:
            raise RuntimeError(repr(ref) + " is open more than once; pass one of: " + ", ".join(same))
        raise RuntimeError("no open document " + repr(ref) + "; open: "
                           + ", ".join(r for r, d in refs if d != active))
    source = found[0]
    if source == active:
        raise RuntimeError(repr(ref) + " is the design being imported into; it cannot be copied into itself")
    sroot = adsk.fusion.Design.cast(source.products.itemByProductType("DesignProductType")).rootComponent
    bodies = list(sroot.bRepBodies) + [b for o in sroot.allOccurrences for b in o.bRepBodies]
    if not bodies:
        raise RuntimeError(repr(ref) + " has no solid bodies to copy")
    tbm = adsk.fusion.TemporaryBRepManager.get()
    copies = [(b.name, tbm.copy(b)) for b in bodies]

    def add_copies(comp, base):
        for _, body in copies:
            if base is not None:
                comp.bRepBodies.add(body, base)
            else:
                comp.bRepBodies.add(body)
    occ = _fx_new_component(des, name, add_copies)
    # A copy arrives as Body1, Body2..., and the names are what the organising
    # that usually follows an import works from. They are set only now: a
    # name set inside the base feature's edit does not survive finishEdit
    # (verified on 2704). The result reads them back rather than trusting them.
    for (source_name, _), body in zip(copies, occ.component.bRepBodies):
        try:
            body.name = source_name
        except Exception:
            pass
    return {"ok": True, "from_document": ref, "method": "copied bodies (no history)",
            "occurrences": [occ.name], "copied": len(copies),
            "bodies": [b.name for b in occ.component.bRepBodies]}


result = _fx_copy(_fx_params)
'''

# Run in a call of its own after an import, when the new geometry reads true.
MEASURE_BODY = '''
def _fx_measure(_p):
    des = adsk.fusion.Design.cast(app.activeProduct)
    wanted = set(_p["occurrences"])
    out = {}
    for o in des.rootComponent.occurrences:
        if o.name in wanted:
            bb = o.boundingBox
            out[o.name] = {
                "bodies": o.bRepBodies.count,
                "meshes": getattr(o.component.meshBodies, "count", 0),
                "size_mm": [round((h - l) * 10, 3) + 0.0 for l, h in (
                    (bb.minPoint.x, bb.maxPoint.x), (bb.minPoint.y, bb.maxPoint.y),
                    (bb.minPoint.z, bb.maxPoint.z))],
            }
    return out


result = _fx_measure(_fx_params)
'''


EXPORT_BODY = '''
def _fx_export(_p):
    import os

    fmt = _p["format"]
    name = _p["name"]
    out_path = _p["path"]

    des = adsk.fusion.Design.cast(app.activeProduct)
    if des is None:
        return {"ok": False, "error": "no active Fusion design — open or create one and switch to the Design workspace"}

    # A save picks its path from the document's identity in one call and
    # exports in the next. A tab switch in between once meant the new tab was
    # written into the old tab's mirror (found in review), so the export
    # checks, on Fusion's main thread, that it is still the same document.
    expected = _p.get("expect_key")
    if expected:
        key = None
        try:
            attr = des.attributes.itemByName(__DOC_ATTR_GROUP__, __DOC_ATTR_NAME__)
            key = attr.value if attr is not None and attr.value else None
        except Exception:
            key = None
        if key is None:
            try:
                if app.activeDocument.dataFile is not None:
                    key = "file:" + app.activeDocument.dataFile.id
            except Exception:
                key = None
        if key != expected:
            return {"ok": False, "error": "the open document changed between choosing where to save it and saving; nothing was written"}

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
'''.replace(
    "__DOC_ATTR_GROUP__", repr(DOC_ATTR_GROUP)).replace("__DOC_ATTR_NAME__", repr(DOC_ATTR_NAME))
