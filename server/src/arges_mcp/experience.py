"""What the tool remembers between sessions.

Every call leaves one line in ~/.arges/experience.jsonl: whether it worked, the
error if it did not, how long it took, and the helper functions it defined. A
session used to end and take all of that with it. server.log recorded hundreds
of calls with their sizes and timings and not one outcome, so nothing could say
which mistakes recur or which code keeps being retyped. This log is the raw
material for the lessons and fast commands built on top of it.

The server writes it, not the model. The record is complete because nothing
has to remember to make it.

Two properties matter more than anything else here:

- It is harmless. A log that cannot be written returns False and changes
  nothing the caller sees. Losing a line of history is cheap; turning a call
  that worked into an error would push the model into running it twice.
- It stays local. The file holds the code that ran, which is design data, so
  it is 0600 inside the 0700 state directory, never leaves the machine, and
  ARGES_EXPERIENCE=0 turns it off.

Standard library only, so the chat service can import it the way it already
imports common.py.
"""

import ast
import copy
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

from . import common

LOG_NAME = "experience.jsonl"
# One rotation's worth kept, as with server.log: about ten thousand calls,
# which is months of modelling, before the oldest half is dropped.
MAX_LOG_BYTES = 5 * 1024 * 1024
# The excerpt is for recognising repeated work and for reading what failed. A
# whole 200-line script adds nothing the first few thousand characters do not
# already show, and would let a few long calls crowd out the history.
CODE_EXCERPT_CHARS = 4000
MAX_SIGNATURE_CHARS = 200
# Below this many statements a helper is quicker to retype than to look up, so
# it is not worth remembering as a candidate for a fast command.
MIN_HELPER_STATEMENTS = 3

# One id per server process. Claude Code starts one per session and the chat
# panel one per design, so this groups calls the way a person thinks of them:
# a failure and the call that fixed it share a session.
SESSION = uuid.uuid4().hex[:8]


def enabled():
    """On unless clearly turned off, read per call (same contract as autosave).

    An unrecognised value counts as on: a typo should not silently stop the
    tool from learning.
    """
    raw = (os.environ.get("ARGES_EXPERIENCE")
           or os.environ.get("FUSION_EXPERIENCE") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


# ---------------------------------------------------------- signatures ----
_HEX = re.compile(r"0x[0-9a-fA-F]+")
# A number standing alone, not one inside an identifier: `sk2` and `item0` are
# names, and a lesson about `sk2` should not also match `sk7`.
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])")
_SPACE = re.compile(r"\s+")


def error_signature(text):
    """The part of an error that identifies the mistake, stable across runs.

    The final line of a traceback is the exception, and it is the only line
    that stays the same when the same mistake recurs: the frames above it name
    line numbers in code that is different every time. Numbers and addresses
    in the message vary too ("RuntimeError: 3 : invalid argument" against
    "5 :"), so they become placeholders. Names stay: they are usually what the
    mistake was about.
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    sig = _HEX.sub("0x?", lines[-1])
    sig = _NUMBER.sub("N", sig)
    return _SPACE.sub(" ", sig).strip()[:MAX_SIGNATURE_CHARS]


# -------------------------------------------------------- fingerprints ----
class _Shape(ast.NodeTransformer):
    """Erase what varies between retypings of one helper.

    Its name, its parameter names, its docstring and its literal values all
    change between sessions. Its structure does not, and that is what the
    fingerprint keeps. Parameters become positional placeholders everywhere
    they are used, so `setp(n, e)` and `set_param(name, expr)` match. Other
    names in the body are kept, since erasing every name would make unrelated
    helpers collide.
    """

    def __init__(self):
        self.params = {}

    def visit_FunctionDef(self, node):
        node.name = "_"
        args = node.args
        for i, arg in enumerate(args.posonlyargs + args.args + args.kwonlyargs
                                + [a for a in (args.vararg, args.kwarg) if a]):
            self.params.setdefault(arg.arg, "_a%d" % i)
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0], "value", None), ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
        self.generic_visit(node)
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_arg(self, node):
        node.arg = self.params.get(node.arg, node.arg)
        node.annotation = None
        return node

    def visit_Name(self, node):
        node.id = self.params.get(node.id, node.id)
        return node

    def visit_Constant(self, node):
        value = node.value
        # 10 and 25.4 are the same kind of thing here: a number somebody tuned.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return ast.Constant(value="num")
        return ast.Constant(value=type(value).__name__)


def function_fingerprints(code):
    """{name: shape hash} for each top-level helper worth remembering.

    Never raises: code that does not parse defines nothing anyone could reuse.
    """
    try:
        tree = ast.parse(code or "")
    except (SyntaxError, ValueError):
        return {}
    out = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        statements = sum(isinstance(n, ast.stmt) for n in ast.walk(node)) - 1
        if statements < MIN_HELPER_STATEMENTS:
            continue
        shape = _Shape().visit(copy.deepcopy(node))
        dumped = ast.dump(shape, annotate_fields=False, include_attributes=False)
        out[node.name] = hashlib.sha256(dumped.encode("utf-8")).hexdigest()[:12]
    return out


# -------------------------------------------------------------- records ----
def call_record(tool, code, outcome, seconds, **extra):
    """One line of the log, built from a call and what the bridge answered.

    The three bridge envelopes map onto two flags. `ran` separates "your code
    raised" (a traceback) from "your code never ran" (an error alone, usually
    no open design), because they call for different fixes.
    """
    outcome = outcome if isinstance(outcome, dict) else {}
    ok = bool(outcome.get("ok"))
    ran = ok or "traceback" in outcome
    failure = "" if ok else (outcome.get("traceback") or outcome.get("error") or "")
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "session": SESSION,
        "tool": tool,
        "ok": ok,
        "ran": ran,
        "sig": error_signature(failure),
        "ms": int(round(seconds * 1000)),
        "chars": len(code or ""),
        "code": (code or "")[:CODE_EXCERPT_CHARS],
        "defs": function_fingerprints(code),
    }
    record.update(extra)
    return record


# ------------------------------------------------------------------ log ----
class ExperienceLog:
    """An append-only JSONL file, rotated once it reaches `max_bytes`.

    Several processes append to it at once: Claude Code's MCP server and one
    per design in the chat panel. Each record goes out in a single write() on
    an O_APPEND descriptor, so lines from different processes cannot
    interleave mid-line.
    """

    def __init__(self, path, max_bytes=MAX_LOG_BYTES):
        self.path = Path(path)
        self.max_bytes = max_bytes

    @property
    def rotated(self):
        return Path(str(self.path) + ".1")

    def append(self, record):
        """Write one record. Returns False rather than raising, always."""
        try:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"),
                              default=str) + "\n"
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._rotate_if_full()
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
            return True
        except (OSError, TypeError, ValueError):
            return False

    def _rotate_if_full(self):
        try:
            if self.path.stat().st_size >= self.max_bytes:
                os.replace(self.path, self.rotated)
        except FileNotFoundError:
            pass

    def read(self):
        """Every record, oldest first, across the rotation. Bad lines skipped."""
        records = []
        for path in (self.rotated, self.path):
            try:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            value = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(value, dict):
                            records.append(value)
            except OSError:
                continue
        return records


def default_log():
    """The log in the state directory, resolved per call like everything else
    there, so a test that redirects the state directory redirects this too."""
    return ExperienceLog(common.state_dir() / LOG_NAME)


def record_call(record, log=None):
    """Append `record` if the log is on. Never raises."""
    if not enabled():
        return False
    return (log or default_log()).append(record)
