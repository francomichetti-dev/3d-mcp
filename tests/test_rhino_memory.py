"""Per-project memory for the Rhino chat: identity, storage, and its promises.

The interesting part is `split_version`. Rhino saves incrementally and people
rename by hand, so one piece of work arrives as chair.3dm, chair001.3dm,
chair_v2.3dm and "chair - Copy.3dm". Getting that wrong is not cosmetic: too
loose and two different models share one conversation, too strict and every
incremental save starts a blank thread and the memory feature does nothing.

Also pinned here, because the module writes under ~/.fusion-mcp and SECURITY.md
makes a promise about that tree:

  * every write is restricted to this account
  * nothing in here ever raises — memory is an enhancement, and a chat that
    dies because a notes file is unwritable is worse than one with no memory

    python3 tests/test_rhino_memory.py
"""

import importlib.util
import os
import stat
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "scripts" / "rhino" / "rhino_memory.py"

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


spec = importlib.util.spec_from_file_location("rhino_memory", SOURCE)
memory = importlib.util.module_from_spec(spec)
sys.modules["rhino_memory"] = memory
spec.loader.exec_module(memory)

# Redirect the whole store into a temp dir. Nothing may touch the real one.
TMP = Path(tempfile.mkdtemp(prefix="rhino-memory-test-"))
memory.CONFIG_DIR = str(TMP)
memory.MEMORY_DIR = str(TMP / "memory")
memory.INDEX_PATH = str(TMP / "memory" / "index.json")


# ------------------------------------------------------------- identity ----
# The cases in split_version's own docstring, asserted rather than described.
print("One project, many filenames")

for stem, want_base, want_label in [
    ("caja herramientas", "caja herramientas", None),
    ("caja herramientas 008", "caja herramientas", "#8"),
    ("caja herramientas 008 para AI", "caja herramientas", "#8 para AI"),
    ("chair001", "chair", "#1"),
    ("chair_v2 - Copy", "chair", "v2"),
]:
    base, label = memory.split_version(stem)
    check(f"{stem!r} -> base", base, want_base)
    check(f"{stem!r} -> version", label, want_label)

# Rhino's own increment and hand-written versions must land on one key, or the
# conversation restarts every time somebody presses save.
key = memory.project_key
same = key("/models/chair.3dm")
check("an incremental save is the same project", key("/models/chair001.3dm"), same)
check("and a hand-written version too", key("/models/chair_v2.3dm"), same)
check("and a duplicate", key("/models/chair - Copy.3dm"), same)
check("and a final-named save", key("/models/chair_final.3dm"), same)

# The opposite error: two genuinely different models must NOT share a thread.
truthy("a different model is a different project",
       key("/models/lamp.3dm") != same)
truthy("same name in another folder stays separate",
       key("/other/chair.3dm") != same)
# "part2" is a part, not a version — two digits is not an increment.
truthy("a two-digit suffix is not treated as a version",
       key("/models/part2.3dm") != key("/models/part.3dm"))

check("nothing open has its own key", key(""), memory.UNSAVED_KEY)

info = memory.describe("/models/chair_v2 - Copy.3dm")
check("describe reports the project title", info["title"], "chair")
check("and the version it came from", info["version"], "v2")


# -------------------------------------------------------------- storage ----
print("What it stores, and who can read it")

session, existed = memory.session_id(same)
truthy("a project gets a session id", session)
check("created, not adopted, the first time", existed, False)
again, existed = memory.session_id(same)
check("the same id comes back next time", again, session)
check("and it is reported as existing", existed, True)
truthy("a different project gets a different thread",
       memory.session_id(key("/models/lamp.3dm"))[0] != session)

memory.touch(info)
memory.add_note(same, "the lid is 3 mm ply", version="v2")
notes = memory.read_notes(same)
truthy("a note is stored", "3 mm ply" in notes)
truthy("tagged with the version it was written on", "[v2]" in notes)

memory.add_cost(same, 0.12)
memory.add_cost(same, 0.0345)
check("per-turn costs accumulate rather than overwrite",
      memory.load_index()[same]["cost_usd"], 0.1545)
memory.add_cost(same, None)          # a turn with no cost figure
check("a missing cost is ignored, not counted",
      memory.load_index()[same]["cost_usd"], 0.1545)

block = memory.context_block(info)
truthy("the injected block names the project", "chair" in block)
truthy("and carries the notes", "3 mm ply" in block)
truthy("and tells the model how to save more", "rhino_remember" in block)

# SECURITY.md promises 0700 on this tree and 0600 on its files. The memory
# store is inside it and has to keep that promise itself rather than rely on
# the parent directory happening to block traversal.
if os.name != "nt":
    check("the index is owner-only",
          oct(os.stat(memory.INDEX_PATH).st_mode & 0o777), "0o600")
    check("the notes are owner-only",
          oct(os.stat(memory.notes_path(same)).st_mode & 0o777), "0o600")
    check("the session id is owner-only",
          oct(os.stat(os.path.join(memory.MEMORY_DIR, same, "session")).st_mode
              & 0o777), "0o600")
    check("the project directory is owner-only",
          oct(os.stat(os.path.join(memory.MEMORY_DIR, same)).st_mode & 0o777),
          "0o700")
else:
    source_text = SOURCE.read_text(encoding="utf-8")
    truthy("Windows restriction goes through icacls", "icacls" in source_text)
    # (OI)(CI) on a plain file yields an ACL with no usable grantee and the
    # next write fails; those flags are a directory's, and this bit us once.
    truthy("inheritance flags are directory-only",
           'if os.path.isdir(path) else f"{user}:F"' in source_text)


# ------------------------------------------------------------- it is soft --
# Memory is an enhancement. If the disk refuses, the chat still works.
print("A broken store degrades instead of raising")

# A path whose parent is a FILE, not a directory: makedirs fails with
# NotADirectoryError, a real OSError, which is what an unwritable disk
# actually looks like. (A null byte would raise ValueError instead — not a
# filesystem failure, and pretending otherwise would test nothing.)
blocker = TMP / "not-a-directory"
blocker.write_text("", encoding="utf-8")
memory.MEMORY_DIR = str(blocker / "memory")
memory.INDEX_PATH = str(blocker / "memory" / "index.json")
check("an unwritable index returns False, not an exception",
      memory.save_index({"a": 1}), False)
check("and reading one back is simply empty", memory.load_index(), {})
check("a note that cannot be written says so", memory.add_note("k", "x"), False)
try:
    memory.add_cost("k", 1.0)
    PASS += 1
except Exception as exc:                                  # noqa: BLE001
    FAIL += 1
    print(f"  FAIL add_cost raised on an unwritable store: {exc!r}")

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
