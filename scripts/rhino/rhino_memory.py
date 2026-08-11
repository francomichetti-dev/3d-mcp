"""Per-project memory for the Rhino chat.

The chat had one global conversation. Reopening it looked empty, and two
different models shared one thread, so "the chair" meant whatever was said
last. This keys everything on the PROJECT instead.

Rhino saves incrementally: `chair.3dm`, `chair001.3dm`, `chair002.3dm`, and
people also write `chair_v2.3dm` or `chair - Copy.3dm` by hand. Those are the
same piece of work at different points, not different projects, so they must
share a thread. `project_key` strips those suffixes; `version_label` keeps what
was stripped, so Claude can still say "you were on v2, this is v3".

    ~/.fusion-mcp/memory/
        index.json          every project seen: key, title, path, last opened
        <key>/session       the Claude Code session id for this project
        <key>/notes.md      what the person is doing — injected every turn

Nothing here raises. Memory is an enhancement; if the disk is unwritable the
chat must still work, so every failure degrades to "no memory" rather than a
traceback in the person's face.
"""

import json
import os
import re
import stat
import subprocess
import time
import uuid

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, ".fusion-mcp")
MEMORY_DIR = os.path.join(CONFIG_DIR, "memory")
INDEX_PATH = os.path.join(MEMORY_DIR, "index.json")

# Keep the injected block small: it is prepended to EVERY turn, so it competes
# with the conversation for context. Notes beyond this are truncated oldest
# first by _trim, which keeps the newest entries — those describe where the
# work actually is.
MAX_NOTES_CHARS = 4000

UNSAVED_KEY = "_unsaved"
UNSAVED_TITLE = "Unsaved document"


# --------------------------------------------------------------------------
# Project identity
# --------------------------------------------------------------------------

# Hand-written versions: chair_v2, chair-v2, chair v2, chair_rev3 — and
# anything after them ("chair v2 for print") is a variant note, not a
# different project.
_HAND_VERSION = re.compile(
    r"^(?P<stem>.*?)[ _\-]+(?:v|ver|rev|version)[ _\-]?(?P<ver>\d+)(?P<rest>[ _\-].*)?$",
    re.IGNORECASE)
# A version as its own word, with an optional descriptor after it:
# "box 008", "box 008 for AI". Measured on real files — people number in the
# middle and keep writing. The project is what comes BEFORE the number.
_NUMBER_WORD = re.compile(
    r"^(?P<stem>.*?)[ _\-]+(?P<ver>\d{3,})(?P<rest>[ _\-].*)?$")
# Rhino's own incremental save glues digits on the end: chair.3dm ->
# chair001.3dm. Three or more digits only; "part2" is usually a different
# part, not a version.
_RHINO_INCREMENT = re.compile(r"^(?P<stem>.*?)(?P<ver>\d{3,})$")
# Windows/Mac duplication: "chair - Copy", "chair copy 2", "chair (1)".
_COPY_SUFFIX = re.compile(r"^(?P<stem>.*?)[ _\-]*(?:-\s*)?(?:copy|copia)(?:\s*\(?\d+\)?)?$",
                          re.IGNORECASE)
_PAREN_SUFFIX = re.compile(r"^(?P<stem>.*?)[ _\-]*\((?P<ver>\d+)\)$")
_FINAL_WORDS = re.compile(r"^(?P<stem>.*?)[ _\-]+(?:final|definitivo|def|nuevo|new|old|viejo)$",
                          re.IGNORECASE)
# The same words standing alone. A descriptor written after the version number
# ("008 para AI") is worth keeping in the label, but "Copy" is not a
# descriptor — it says how the file was made, not what the save is for.
_NOISE_ONLY = re.compile(
    r"^(?:copy|copia|final|definitivo|def|nuevo|new|old|viejo)"
    r"(?:\s*\(?\d+\)?)?$", re.IGNORECASE)


def split_version(stem):
    """Split a filename stem into (project-base, version-label-or-None).

    Measured against real files rather than guessed (renamed here, but the
    shapes are the ones that actually turned up on a working machine):

        caja herramientas.3dm                -> ("caja herramientas", None)
        caja herramientas 008.3dm            -> ("caja herramientas", "#8")
        caja herramientas 008 para AI.3dm    -> ("caja herramientas", "#8 para AI")
        chair001.3dm                         -> ("chair", "#1")
        chair_v2 - Copy.3dm                  -> ("chair", "v2")

    A descriptor written after the number ("para AI") is part of the version
    label, not the identity: it is the same box, saved again with a note about
    what that save is for. The examples are Spanish because the person this
    was measured against names files in Spanish — so do the copy/final
    suffixes below ("copia", "definitivo"), which is not decoration.
    """
    base = (stem or "").strip()
    label = None

    for pattern, mark in ((_HAND_VERSION, "v"), (_NUMBER_WORD, "#"),
                          (_RHINO_INCREMENT, "#")):
        match = pattern.match(base)
        if not match or not match.group("stem").strip(" _-"):
            continue
        number = match.group("ver").lstrip("0") or "0"
        label = mark + number
        try:
            rest = (match.group("rest") or "").strip(" _-")
        except IndexError:                   # _RHINO_INCREMENT has no `rest`
            rest = ""
        # "chair_v2 - Copy" must label as v2, not "v2 Copy" — the docstring
        # above says so and the code did not: the copy/final strippers below
        # only ever cleaned the base, so duplication noise that landed AFTER
        # the version number rode along in the label.
        if rest and not _NOISE_ONLY.match(rest):
            label += " " + rest
        base = match.group("stem").strip(" _-")
        break

    # Duplication and "final" suffixes carry no version of their own, so they
    # are stripped after: chair_v2 - Copy is still v2 of chair.
    for _ in range(3):                       # bounded; each pass strips one
        for pattern in (_COPY_SUFFIX, _PAREN_SUFFIX, _FINAL_WORDS):
            match = pattern.match(base)
            if match and match.group("stem").strip(" _-"):
                if label is None and pattern is _PAREN_SUFFIX:
                    label = "(%s)" % match.group("ver")
                base = match.group("stem").strip(" _-")
                break
        else:
            break

    return base or (stem or "").strip(), label


def _slug(text):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:60] or "project"


def project_key(doc_path, doc_name=""):
    """A stable id shared by every version of the same model.

    Keyed on folder + normalised stem, so two projects that happen to share a
    name in different folders stay separate.
    """
    if not doc_path:
        return UNSAVED_KEY
    folder, filename = os.path.split(doc_path)
    stem = os.path.splitext(filename)[0]
    base, _ = split_version(stem)
    folder_tag = _slug(os.path.basename(folder))[:20]
    return "%s__%s" % (_slug(base), folder_tag) if folder_tag else _slug(base)


def describe(doc_path, doc_name=""):
    """What this document is, as {key, title, version, path}."""
    if not doc_path:
        return {"key": UNSAVED_KEY, "title": UNSAVED_TITLE,
                "version": None, "path": ""}
    stem = os.path.splitext(os.path.basename(doc_path))[0]
    base, version = split_version(stem)
    return {"key": project_key(doc_path), "title": base or stem,
            "version": version, "path": doc_path}


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


# Deliberately duplicated from rhino-chat.py rather than imported: that file
# has a hyphen in its name and cannot be imported at all, and this module is
# also loaded by rhino_mcp.py, which runs on Rhino's bare interpreter. The
# Rhino half duplicates its shared constants for the same reason and
# tests/test_consistency.py is what stops the copies drifting.
#
# The (OI)(CI)-on-a-file trap is the one this project has already been bitten
# by: those are a FOLDER's inheritance flags, and on a plain file icacls
# reports success while writing an ACL with no usable grantee, after which the
# next write fails. Directory gets them, file does not.
def _secure(path):
    """Restrict a memory file or directory to this account. Never raises."""
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME")
            if not user:
                return False
            grant = f"{user}:(OI)(CI)F" if os.path.isdir(path) else f"{user}:F"
            done = subprocess.run(
                ["icacls", path, "/inheritance:r", "/grant:r", grant],
                capture_output=True, text=True, timeout=15)
            return done.returncode == 0
        os.chmod(path, stat.S_IRWXU if os.path.isdir(path)
                 else stat.S_IRUSR | stat.S_IWUSR)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _project_dir(key):
    # What a person is building, and the paths they build it in, is theirs.
    # SECURITY.md promises 0700 on this tree and 0600 on its files; the memory
    # store is inside that tree and has to keep the promise rather than lean
    # on the parent directory happening to block traversal.
    #
    # makedirs is guarded because this module promises never to raise, and
    # this was the one place that broke it: every note, session id and recall
    # goes through here, so an unwritable store took the chat down with it
    # instead of quietly running without memory. Returning the path anyway is
    # deliberate — _read and _write already degrade, so the callers land on
    # "no memory" rather than an exception.
    path = os.path.join(MEMORY_DIR, key)
    try:
        fresh = not os.path.isdir(path)
        os.makedirs(path, exist_ok=True)
        if fresh:
            _secure(path)
    except OSError:
        pass
    return path


def _read(path, default=""):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return default


def _write(path, text):
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        _secure(path)
        return True
    except OSError:
        return False


def load_index():
    try:
        with open(INDEX_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_index(index):
    try:
        fresh = not os.path.isdir(MEMORY_DIR)
        os.makedirs(MEMORY_DIR, exist_ok=True)
        if fresh:
            _secure(MEMORY_DIR)
        with open(INDEX_PATH, "w", encoding="utf-8") as handle:
            json.dump(index, handle, indent=2, ensure_ascii=False)
        # The index is the most revealing file here: every project title and
        # the full path of every model file the person has opened.
        _secure(INDEX_PATH)
        return True
    except OSError:
        return False


def add_cost(key, usd):
    """Accumulate a turn's $ cost onto this project's running total.

    `usd` is what the CLI's result event reports for that one turn
    (total_cost_usd) — a per-turn figure, so it is added, not overwritten.
    Silently ignored if key or usd is falsy: a turn with no project (nothing
    open in Rhino yet) or no cost figure should not raise.
    """
    if not key or not usd:
        return
    index = load_index()
    entry = index.get(key) or {}
    entry["cost_usd"] = round((entry.get("cost_usd") or 0.0) + float(usd), 4)
    index[key] = entry
    save_index(index)


def touch(info):
    """Record that this project was opened. Returns the index entry."""
    index = load_index()
    entry = index.get(info["key"]) or {}
    entry.update({
        "title": info["title"],
        "last_path": info["path"] or entry.get("last_path", ""),
        "last_seen": time.strftime("%Y-%m-%d %H:%M"),
    })
    versions = entry.get("versions") or []
    if info["path"]:
        name = os.path.basename(info["path"])
        if name not in versions:
            versions.append(name)
        entry["versions"] = versions[-12:]      # newest few are what matter
    index[info["key"]] = entry
    save_index(index)
    return entry


def session_id(key):
    """The Claude Code session for this project, created on first use.

    Stored per project so switching models switches conversations, and the
    thread for a model is still there weeks later.
    """
    path = os.path.join(_project_dir(key), "session")
    existing = _read(path).strip()
    if existing:
        return existing, True
    new = str(uuid.uuid4())
    _write(path, new)
    return new, False


def adopt_session(key, session):
    """Point a project at an existing conversation.

    Used once when upgrading from the old single global thread, so the history
    from before per-project memory existed carries into the first project.
    """
    return _write(os.path.join(_project_dir(key), "session"), session)


def mark_started(key):
    """Nothing to do — the id file IS the record. Kept for call-site clarity."""
    return True


def reset_session(key):
    """Start a fresh thread for this project, keeping its notes."""
    new = str(uuid.uuid4())
    _write(os.path.join(_project_dir(key), "session"), new)
    return new


def notes_path(key):
    return os.path.join(_project_dir(key), "notes.md")


def read_notes(key):
    return _read(notes_path(key)).strip()


def _trim(text):
    if len(text) <= MAX_NOTES_CHARS:
        return text
    # Drop whole entries from the top, never mid-sentence: a half-truncated
    # note reads as a fact with its qualifier missing.
    entries = text.split("\n- ")
    while entries and len("\n- ".join(entries)) > MAX_NOTES_CHARS:
        entries.pop(0)
    return "\n- ".join(entries) if entries else text[-MAX_NOTES_CHARS:]


def add_note(key, note, version=None):
    """Append one durable fact about this project."""
    note = (note or "").strip()
    if not note:
        return False
    stamp = time.strftime("%Y-%m-%d")
    tag = " [%s]" % version if version else ""
    existing = read_notes(key)
    body = "%s\n- %s%s: %s" % (existing, stamp, tag, note) if existing \
        else "- %s%s: %s" % (stamp, tag, note)
    return _write(notes_path(key), _trim(body.strip()) + "\n")


def other_projects(exclude_key, limit=12):
    """Recent projects, so a reference to another model can be understood."""
    index = load_index()
    rows = [(k, v) for k, v in index.items() if k != exclude_key]
    rows.sort(key=lambda kv: kv[1].get("last_seen", ""), reverse=True)
    return rows[:limit]


def context_block(info):
    """The memory text injected into a turn. Empty string when there is none."""
    key = info["key"]
    lines = []
    where = info["title"]
    if info.get("version"):
        where += " (version %s)" % info["version"]
    lines.append("Current project: %s" % where)
    if info.get("path"):
        lines.append("File: %s" % info["path"])

    entry = load_index().get(key) or {}
    versions = entry.get("versions") or []
    if len(versions) > 1:
        lines.append("Known versions of this project (same work, different "
                     "saves): %s" % ", ".join(versions[-8:]))

    notes = read_notes(key)
    if notes:
        lines.append("")
        lines.append("What you know about this project:")
        lines.append(notes)

    others = other_projects(key)
    if others:
        lines.append("")
        lines.append("Other projects you have worked on with this person "
                     "(mention only if they bring one up):")
        for other_key, meta in others:
            lines.append("  - %s (last %s)" % (meta.get("title", other_key),
                                               meta.get("last_seen", "?")))

    lines.append("")
    lines.append("Use rhino_remember to save anything worth knowing next "
                 "time: what they are building, decisions, dimensions, what is "
                 "left. Do not save trivia or anything already in the file.")
    return "\n".join(lines)
