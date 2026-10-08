"""Lessons: a fix recorded once comes back the next time its error does.

The experience log remembers what happened. A lesson records what to do about
it. The model records one with fusion_learn after it fixes a failure, keyed by
the error's signature. When a later call in any session fails with the same
signature, the server attaches the lesson to the result, before the model has
spent a turn rediscovering the fix. Lessons with no error are for the mistakes
that raise nothing (a mirrored sketch, a profile that grabbed the wrong
region); fusion_state shows those at the start of a session.

Matching is on the exact signature only. A looser match, such as ignoring the
quoted names, would make "AttributeError: 'X' object has no attribute 'y'"
recall a lesson about every other AttributeError ever recorded. This project
holds that a wrong pattern is worse than a missing one, so a lesson that
misleads is worse than no lesson. The same principle is why a lesson can be
forgotten.

One JSON file, ~/.arges/lessons.json, shared by every MCP server process on the
machine: Claude Code's, and one per design in the chat panel. Each write
re-reads the file under an exclusive lock, so no process writes back a stale
copy over another's lesson.

Standard library only, like experience.py.
"""

import hashlib
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

from . import common
from .experience import error_signature

STORE_NAME = "lessons.json"
# Long enough for a fix and its reason, short enough that three of them riding
# on a failed call cost less than the turn they save.
MAX_LESSON_CHARS = 600
# A personal store, not a knowledge base. Past this the least-confirmed go
# first. The ones that keep being confirmed are the ones worth promoting into
# the skill, which is where a lesson goes to outlive one machine.
MAX_LESSONS = 300
MAX_TAGS = 5
RECALL_LIMIT = 3
GENERAL_LIMIT = 5

_TAG = re.compile(r"[^a-z0-9_-]+")
_WORD = re.compile(r"[a-z0-9]+")
# A recipe keeps a few phrasings of what it answers, not every one ever used.
MAX_WHEN = 5
MAX_WHEN_CHARS = 200
# Words that say nothing about what was asked. Kept short: a missing stopword
# costs a little ranking noise, a wrong one hides a real match.
_STOP = frozenset("""the and for with this that these those what which who you your our its
are was were has have had can could should would will just like into from then than them
they their there here make need want please one some all any not but how also very
""".split())


def _normal(text):
    """What counts as "the same lesson": case and spacing do not."""
    return " ".join(str(text).lower().split())


def _tags(raw):
    items = raw.split(",") if isinstance(raw, str) else list(raw or ())
    out = []
    for item in items:
        tag = _TAG.sub("", str(item).strip().lower())
        if tag and tag not in out:
            out.append(tag)
    return out[:MAX_TAGS]


# Words people use for the same thing. The case that made this necessary: the
# person said "delete the one I selected", and the recipe that answers it says
# "remove", so the two shared only "select". Keys and values are stems.
_SAME = {"delet": "remov", "eras": "remov", "erase": "remov",
         "highlight": "select", "pick": "select", "chosen": "select"}


def _stems(text):
    """Words reduced far enough that "selected", "selection" and "select" meet.

    Deliberately crude: strip one common suffix, then a trailing e, so
    "delete", "deleted" and "deleting" all become "delet". A stemmer library
    would be better and would also be a dependency; this is enough to recognise
    the same request said twice.
    """
    out = set()
    for word in _WORD.findall(str(text or "").lower()):
        if len(word) < 3 or word in _STOP:
            continue
        for suffix in ("ation", "ions", "ion", "ing", "ed", "es", "s"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                word = word[: -len(suffix)]
                break
        if word.endswith("e") and len(word) > 4:
            word = word[:-1]
        out.add(_SAME.get(word, word))
    return out


def _when(raw):
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for item in items:
        text = " ".join(str(item or "").split())[:MAX_WHEN_CHARS]
        if text and text not in out:
            out.append(text)
    return out[:MAX_WHEN]


def _rank(lesson):
    return (-int(lesson.get("confirmed") or 0), -float(lesson.get("updated_at") or 0))


class Lessons:
    def __init__(self, path, max_lessons=MAX_LESSONS):
        self.path = Path(path)
        self.max_lessons = max_lessons

    # ------------------------------------------------------------ reading --
    def _load(self, quarantine=False):
        """The stored list. A corrupt file reads as empty.

        Only a writer re-reading under the lock may move it aside. Otherwise
        a reader that read corrupt bytes could rename a writer's valid
        replacement out of the way (found in review).
        Keeping the bad file preserves somebody's accumulated work instead
        of overwriting the only copy when a bad byte stops it parsing.
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        try:
            data = json.loads(raw)
            lessons = data.get("lessons") if isinstance(data, dict) else None
            if not isinstance(lessons, list):
                raise ValueError("no lessons list")
        except ValueError:
            if quarantine:
                aside = Path(str(self.path) + ".corrupt-" + time.strftime("%Y%m%d-%H%M%S"))
                try:
                    os.replace(self.path, aside)
                except OSError:
                    pass
            return []
        return [item for item in lessons if isinstance(item, dict) and item.get("lesson")]

    def all(self):
        """Every lesson. Never raises: unreadable means none."""
        try:
            return self._load()
        except OSError:
            return []

    def for_error(self, text, limit=RECALL_LIMIT):
        """The lessons recorded for this error, best-confirmed first."""
        sig = error_signature(text)
        if not sig:
            return []
        hits = [item for item in self.all() if item.get("sig") == sig]
        return sorted(hits, key=_rank)[:limit]

    def matching(self, text, limit=RECALL_LIMIT):
        """Lessons that fit a request, by the words it shares with them.

        What a recipe says it answers (`when`) counts double: those phrasings
        were written to be matched against requests like this one. Two shared
        words at least, or a single word would recall half the store.
        """
        wanted = _stems(text)
        if not wanted:
            return []
        scored = []
        for item in self.all():
            when = wanted & _stems(" ".join(item.get("when") or []))
            said = wanted & _stems(item.get("lesson"))
            if len(when | said) < 2:
                continue
            scored.append((2 * len(when) + len(said), item))
        scored.sort(key=lambda pair: (-pair[0],) + _rank(pair[1]))
        return [item for _, item in scored[:limit]]

    def general(self, limit=GENERAL_LIMIT):
        """Lessons with no error to trigger them, best-confirmed first."""
        return sorted((item for item in self.all() if not item.get("sig")),
                      key=_rank)[:limit]

    # ------------------------------------------------------------ writing --
    def add(self, lesson, error="", tags=(), when=""):
        """Record a lesson, or confirm it if it is already there.

        `when` is what the lesson answers, in the words a request would use. A
        success recorded again with other words confirms it and keeps the new
        phrasing too.

        Raises ValueError for an empty lesson and OSError if the store cannot be
        written. Unlike recall, adding is something the model asked for, so a
        failure has to reach it rather than read as success.
        """
        text = " ".join(str(lesson or "").split())[:MAX_LESSON_CHARS]
        if not text:
            raise ValueError("a lesson needs some text: what fixed it, and why")
        sig = error_signature(error)
        lesson_id = hashlib.sha256(
            (sig + "\n" + _normal(text)).encode("utf-8")).hexdigest()[:10]
        now = time.time()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._locked():
            lessons = self._load(quarantine=True)
            for item in lessons:
                if item.get("id") == lesson_id:
                    item["confirmed"] = int(item.get("confirmed") or 0) + 1
                    item["updated"], item["updated_at"] = stamp, now
                    item["tags"] = _tags(list(item.get("tags") or []) + _tags(tags))
                    item["when"] = _when(list(item.get("when") or []) + _when(when))
                    self._write(lessons)
                    return {"id": lesson_id, "new": False, "sig": sig, "when": item["when"],
                            "confirmed": item["confirmed"], "total": len(lessons)}
            lessons.append({"id": lesson_id, "lesson": text, "sig": sig,
                            "tags": _tags(tags), "when": _when(when), "confirmed": 1,
                            "created": stamp, "updated": stamp, "updated_at": now})
            while len(lessons) > self.max_lessons:
                # Evict the least confirmed, oldest first, and never the one
                # just recorded: reporting "new" for a lesson that was dropped
                # on the way in would be a lie.
                worst = min(lessons[:-1], key=lambda item: (
                    int(item.get("confirmed") or 0), float(item.get("updated_at") or 0)))
                lessons.remove(worst)
            self._write(lessons)
            return {"id": lesson_id, "new": True, "sig": sig, "when": _when(when),
                    "confirmed": 1, "total": len(lessons)}

    def forget(self, lesson_id):
        """Remove a lesson. True if it was there."""
        with self._locked():
            lessons = self._load(quarantine=True)
            kept = [item for item in lessons if item.get("id") != lesson_id]
            if len(kept) == len(lessons):
                return False
            self._write(kept)
            return True

    def _locked(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return _FileLock(Path(str(self.path) + ".lock"))

    def _write(self, lessons):
        """Atomically: a reader in another process sees the old file or the new
        one, never half of one. mkstemp creates it 0600."""
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".lessons-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "lessons": lessons}, fh,
                          ensure_ascii=False, indent=1)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class _FileLock:
    """An exclusive flock held for one read-modify-write."""

    def __init__(self, path):
        self.path = path
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        if fcntl is not None:
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def default_store():
    """The store in the state directory, resolved per call."""
    return Lessons(common.state_dir() / STORE_NAME)


def brief(lesson):
    """What rides on a tool result: enough to act on and to correct."""
    out = {"id": lesson.get("id"), "lesson": lesson.get("lesson"),
           "confirmed": lesson.get("confirmed", 1)}
    if lesson.get("when"):
        out["when"] = lesson["when"]
    return out
