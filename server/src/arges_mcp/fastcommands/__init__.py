"""Fast commands: proven code saved under a name and run in one call.

The same helpers were retyped in session after session. In the Claude Code
transcripts on the development machine, a set-user-parameter helper was
written six times, the camera-placement lines eight times, and a
timeline-walking loop seven. Each retyping costs tokens and a chance to get it
subtly wrong. A fast command is that code written once, checked, and then
called by name: fusion_run("bbox") instead of twenty lines of bounding-box
arithmetic.

A command is one Python file: a docstring whose first line says what it does
and whose rest says how to call it, an optional `READONLY = True`, and a
`def run(p)` that receives the call's arguments as a dict and returns
something JSON can carry. It runs inside Fusion with the same injected names
as fusion_execute (`adsk`, `app`, `ui`, `design`).

Built-ins ship in builtin/, next to this file. Learned commands, saved by the
model with fusion_command_save, live in ~/.arges/commands/ in the same
format, so a learned command that proves itself can be promoted into the
built-ins by copying the file.

Two things are deliberate:

- Metadata is read with ast, never by importing. Loading a command must not
  run it, and a broken learned file must not take the library down with it.
- A command runs in a namespace of its own, with its source and arguments
  both passed as data through the snippet mechanism every generated call
  uses. An argument is never interpolated into code, and a command's own
  names (`run`, `READONLY`, its helpers) never leak into the persistent
  namespace that fusion_execute shares.

Standard library only: the chat service imports this to gate a command by its
source, the same way it gates fusion_execute by its code.
"""

import ast
import hashlib
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .. import common

BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"
USER_DIR_NAME = "commands"
# A plain identifier: it becomes a filename and a traceback label, and must
# never be able to become a path.
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
# A command is a helper, not a program. Past this it is a script, and a script
# belongs in fusion_execute, where it can be read and fixed in context.
MAX_SOURCE_BYTES = 20_000
MAX_DESCRIPTION_CHARS = 200
MAX_USAGE_CHARS = 1200


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    usage: str
    readonly: bool
    origin: str          # "builtin" or "learned"
    source: str
    path: Path | None = None


def parse(source, name, origin="learned", path=None):
    """Read a command's metadata without running it. ValueError if unusable."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"the code does not parse (line {exc.lineno}: {exc.msg})") from None
    doc = ast.get_docstring(tree, clean=True) or ""
    description, _, usage = doc.partition("\n")
    if not description.strip():
        raise ValueError("a command needs a docstring whose first line is its description")

    readonly = False
    has_run = False
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "READONLY" for t in node.targets):
            if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, bool)):
                raise ValueError("READONLY must be the literal True or False")
            readonly = node.value.value
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            args = node.args
            positional = args.posonlyargs + args.args
            if len(positional) == 1 and not args.kwonlyargs and not args.vararg:
                has_run = True
    if not has_run:
        raise ValueError("a command must define run(p) at top level, taking one "
                         "argument: the dict of arguments it is called with")
    return Command(name=name, description=description.strip(), usage=usage.strip(),
                   readonly=readonly, origin=origin, source=source, path=path)


def _docstring(description, usage):
    text = description.strip() + ("\n\n" + usage.strip() if usage.strip() else "")
    text = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return '"""' + text + '\n"""\n'


class Library:
    def __init__(self, builtin_dir=BUILTIN_DIR, user_dir=None):
        self.builtin_dir = Path(builtin_dir)
        self.user_dir = Path(user_dir) if user_dir else None

    # ------------------------------------------------------------ reading --
    def _read_dir(self, folder, origin):
        commands = []
        try:
            paths = sorted(folder.glob("*.py"))
        except OSError:
            return commands
        for path in paths:
            name = path.stem
            if not NAME_RE.match(name):
                continue
            try:
                commands.append(parse(path.read_text(encoding="utf-8"), name, origin, path))
            except (OSError, UnicodeError, ValueError):
                continue          # a broken file is skipped, never fatal
        return commands

    def builtins(self):
        return self._read_dir(self.builtin_dir, "builtin")

    def learned(self):
        if self.user_dir is None or not self.user_dir.is_dir():
            return []
        taken = {c.name for c in self.builtins()}
        return [c for c in self._read_dir(self.user_dir, "learned") if c.name not in taken]

    def all(self):
        return self.builtins() + self.learned()

    def get(self, name):
        for command in self.all():
            if command.name == name:
                return command
        return None

    # ------------------------------------------------------------ writing --
    def _check_name(self, name):
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ValueError(
                f"{name!r} is not a command name: use 2-40 lowercase letters, digits "
                "and underscores, starting with a letter (e.g. hole_grid)")
        if name in {c.name for c in self.builtins()}:
            raise ValueError(f"{name!r} is a built-in command and cannot be replaced; "
                             "pick another name")

    def save(self, name, description, code, usage="", readonly=False):
        """Validate and store a learned command. Returns (command, replaced)."""
        if self.user_dir is None:
            raise ValueError("no folder configured for learned commands")
        self._check_name(name)
        description = " ".join(str(description or "").split())
        if not description:
            raise ValueError("a command needs a one-line description of what it does")
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise ValueError(f"keep the description under {MAX_DESCRIPTION_CHARS} characters")
        usage = str(usage or "").strip()[:MAX_USAGE_CHARS]
        code = str(code or "")
        if len(code.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValueError(f"the code is too large for a command (over {MAX_SOURCE_BYTES} "
                             "bytes); keep commands to a helper, not a whole build")
        try:
            body = ast.parse(code).body
        except SyntaxError as exc:
            raise ValueError(f"the code does not parse (line {exc.lineno}: {exc.msg})") from None
        if any(isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "READONLY" for t in n.targets)
               for n in body):
            raise ValueError("do not set READONLY in the code; pass readonly=true instead")

        source = (_docstring(description, usage)
                  + f"READONLY = {bool(readonly)}\n"
                  + f"# Saved by fusion_command_save on {time.strftime('%Y-%m-%d %H:%M')}.\n\n"
                  + code.rstrip() + "\n")
        command = parse(source, name, "learned", self.user_dir / f"{name}.py")

        self.user_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = self.user_dir / f"{name}.py"
        replaced = target.exists()
        if replaced:
            # One previous version kept: a "better" command that turns out
            # worse should cost a rename to undo, not a rewrite.
            os.replace(target, self.user_dir / f"{name}.py.prev")
        fd, tmp = tempfile.mkstemp(dir=self.user_dir, prefix=".command-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(source)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return command, replaced

    def remove(self, name):
        """Remove a learned command, keeping it as <name>.py.prev. True if found."""
        if name in {c.name for c in self.builtins()}:
            raise ValueError(f"{name!r} is a built-in command and cannot be removed")
        if self.user_dir is None or not NAME_RE.match(str(name)):
            return False
        target = self.user_dir / f"{name}.py"
        if not target.is_file():
            return False
        os.replace(target, self.user_dir / f"{name}.py.prev")
        return True


def source_sha(source):
    """The hash the panel's gate stamps on a fast-command call, so the server
    runs exactly the source that was checked and not a later replacement."""
    return hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:16]


def default_library():
    """Built-ins plus the learned commands in the state directory, per call."""
    return Library(user_dir=common.state_dir() / USER_DIR_NAME)


def listing(commands, usage=True):
    """What a caller needs to choose and call a command, and nothing else."""
    out = []
    for c in commands:
        entry = {"name": c.name, "description": c.description,
                 "readonly": c.readonly, "origin": c.origin}
        if usage:
            entry["usage"] = c.usage
        out.append(entry)
    return out


# The wrapper that runs a command inside Fusion. Its source and arguments are
# both data, parsed from JSON at runtime. The command gets a namespace of its
# own holding only the injected names, and compiles under its own filename, so
# a traceback says which command failed.
INVOKE_BODY = '''
def _fx_command(_p):
    ns = {"adsk": adsk, "app": app, "ui": ui, "design": design,
          "__name__": "arges_command_" + _p["name"]}
    exec(compile(_p["source"], "<command:" + _p["name"] + ">", "exec"), ns)
    return ns["run"](_p["args"])


result = _fx_command(_fx_params)
'''


def invocation(command, args):
    """The code that runs `command` with `args` inside Fusion."""
    return common.snippet(INVOKE_BODY, {"name": command.name, "source": command.source,
                                        "args": args if args is not None else {}})
