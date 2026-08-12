"""The loader and the installer — no Fusion, no network, no writes outside tmp.

Covers the two pieces a new user hits first and which have no other safety net:
the add-in loader's provenance check (which refused to load itself through its
own symlink), and `arges install`.

    cd agent && uv run --frozen --no-sync python ../tests/test_install.py
"""

import importlib.util
import os
import platform
import shutil
import stat
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
ADDIN = REPO / "server/src/arges_mcp/addin/Arges"

sys.path.insert(0, str(HERE / "stubs"))
sys.path.insert(0, str(REPO / "server/src"))

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


def raises(label, fn, kind=Exception, contains=None):
    global PASS, FAIL
    try:
        fn()
    except kind as exc:
        if contains and contains not in str(exc):
            FAIL += 1
            print(f"  FAIL {label}: raised but message lacked {contains!r}: {exc}")
        else:
            PASS += 1
        return
    except Exception as exc:                              # noqa: BLE001
        FAIL += 1
        print(f"  FAIL {label}: raised {type(exc).__name__}, wanted {kind.__name__}")
        return
    FAIL += 1
    print(f"  FAIL {label}: did not raise")


# ------------------------------------------------------------- loader ----
print("Add-in loader")

spec = importlib.util.spec_from_file_location("_loader_probe", ADDIN / "Arges.py")
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)

truthy("exposes run/stop for Fusion", callable(loader.run) and callable(loader.stop))

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    # Reproduce the layout the installer creates: a real folder of per-file
    # symlinks. This is exactly the shape that broke the old abspath check.
    linked = tmp / "AddIns" / "Arges"
    linked.mkdir(parents=True)
    for source in ADDIN.iterdir():
        if source.is_file():
            os.symlink(source, linked / source.name)

    truthy("layout: folder is real, not a symlink", not linked.is_symlink())
    truthy("layout: files inside are symlinks",
           all(p.is_symlink() for p in linked.iterdir()))

    via_link = linked / "arges_impl.py"
    via_repo = ADDIN / "arges_impl.py"

    # The regression: abspath does not follow symlinks, so the same file reached
    # two ways compared unequal and the add-in refused to load itself.
    check("abspath sees them as different (the old bug)",
          os.path.abspath(via_link) == os.path.abspath(via_repo), False)
    check("realpath sees one file (the fix)",
          os.path.realpath(via_link) == os.path.realpath(via_repo), True)

    # and a genuinely foreign module must still be rejected
    check("a different file is still different",
          os.path.realpath(tmp / "elsewhere" / "arges_impl.py")
          == os.path.realpath(via_repo), False)

    # drive the real check: a module already registered under the linked path
    # must be accepted when the loader is reached through the repo path
    sys.modules.pop("arges_impl", None)
    fake = type(sys)("arges_impl")
    fake.__file__ = str(via_link)
    sys.modules["arges_impl"] = fake
    try:
        loader._IMPL_NAME = "arges_impl"
        loader.__file__ = str(ADDIN / "Arges.py")
        check("the same file via another path is accepted", loader._load_impl(), fake)

        fake.__file__ = "/somewhere/else/arges_impl.py"
        raises("a foreign module is refused", loader._load_impl,
               ImportError, "not this add-in")
    finally:
        sys.modules.pop("arges_impl", None)


# ---------------------------------------------------------- bootstrap ----
print("Installer")

from arges_mcp import bootstrap  # noqa: E402
from arges_mcp import cli  # noqa: E402

truthy("ships the add-in inside the package", bootstrap.bundled_addin().is_dir())
for required in ("Arges.py", "Arges.manifest", "arges_impl.py"):
    truthy(f"bundled: {required}", (bootstrap.bundled_addin() / required).is_file())

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    original_dir, original_token = bootstrap.STATE_DIR, bootstrap.TOKEN_PATH
    bootstrap.STATE_DIR = tmp / "state"
    bootstrap.TOKEN_PATH = bootstrap.STATE_DIR / "token"
    try:
        path, created = bootstrap.ensure_token()
        truthy("creates a token", created)
        token = path.read_text().strip()
        check("token is 64 hex chars", len(token), 64)
        truthy("token is hex", all(c in "0123456789abcdef" for c in token))
        check("token file is 0600", oct(stat.S_IMODE(os.stat(path).st_mode)), "0o600")
        check("state dir is 0700",
              oct(stat.S_IMODE(os.stat(bootstrap.STATE_DIR).st_mode)), "0o700")

        path, created = bootstrap.ensure_token()
        check("an existing token is kept", created, False)
        check("and unchanged", path.read_text().strip(), token)

        path, created = bootstrap.ensure_token(rotate=True)
        truthy("rotate replaces it", path.read_text().strip() != token)
        check("rotated token still 0600",
              oct(stat.S_IMODE(os.stat(path).st_mode)), "0o600")
    finally:
        bootstrap.STATE_DIR, bootstrap.TOKEN_PATH = original_dir, original_token


# ------------------------------------------------- the ~/.arges migration ---
# The state directory was ~/.fusion-mcp before the rename. Everything that
# reads it falls back to the old name; exactly one thing moves it, and that is
# `arges install`. What is asserted here is mostly what it must NOT do.
print("Migrating the pre-rename state directory")

with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp)
    saved = (bootstrap.STATE_DIR, bootstrap.LEGACY_STATE_DIR, bootstrap.TOKEN_PATH)
    try:
        bootstrap.STATE_DIR = home / ".arges"
        bootstrap.LEGACY_STATE_DIR = home / ".fusion-mcp"
        bootstrap.TOKEN_PATH = bootstrap.STATE_DIR / "token"

        check("nothing to migrate is not an error", bootstrap.migrate_state_dir(), False)

        bootstrap.LEGACY_STATE_DIR.mkdir(mode=0o700)
        (bootstrap.LEGACY_STATE_DIR / "token").write_text("deadbeef\n")
        (bootstrap.LEGACY_STATE_DIR / "chats.json").write_text("{}")

        truthy("an old directory alone is moved", bootstrap.migrate_state_dir())
        truthy("the old name is gone", not bootstrap.LEGACY_STATE_DIR.exists())
        check("the token came with it",
              (bootstrap.STATE_DIR / "token").read_text().strip(), "deadbeef")
        truthy("and so did the saved chats",
               (bootstrap.STATE_DIR / "chats.json").is_file())
        check("running it again does nothing", bootstrap.migrate_state_dir(), False)

        # Two directories is untidy; choosing between them is destructive, since
        # whichever loses holds a token something is still authenticating with.
        bootstrap.LEGACY_STATE_DIR.mkdir(mode=0o700)
        (bootstrap.LEGACY_STATE_DIR / "token").write_text("a different one\n")
        check("with both present it refuses", bootstrap.migrate_state_dir(), False)
        truthy("and leaves both alone",
               bootstrap.STATE_DIR.is_dir() and bootstrap.LEGACY_STATE_DIR.is_dir())
    finally:
        bootstrap.STATE_DIR, bootstrap.LEGACY_STATE_DIR, bootstrap.TOKEN_PATH = saved

# The one that matters. Redirecting STATE_DIR alone - which is exactly what the
# token test above does - used to leave LEGACY_STATE_DIR pointing at the real
# home, so the migration would move the operator's live ~/.fusion-mcp into a
# temp directory and delete it on cleanup: token, saved chats, attachments and
# all. The sibling check is what stops it, and this is what stops the sibling
# check being removed as redundant.
with tempfile.TemporaryDirectory() as tmp:
    saved = bootstrap.STATE_DIR
    try:
        bootstrap.STATE_DIR = Path(tmp) / "state"
        check("it refuses to move a directory from somewhere else entirely",
              bootstrap.migrate_state_dir(), False)
        truthy("so a redirected STATE_DIR cannot eat the real one",
               bootstrap.LEGACY_STATE_DIR.parent != bootstrap.STATE_DIR.parent)
    finally:
        bootstrap.STATE_DIR = saved

# install_addin() refuses to run anywhere but macOS, because it knows only
# where Fusion keeps add-ins there. That guard fires before anything else, so
# the behaviour worth asserting differs by platform - and asserting the macOS
# behaviour on Linux is what had CI red: the call raised, but with the platform
# message rather than the one the test was looking for.
if platform.system() == "Darwin":
    # the installer must never overwrite a checkout's symlink
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        addins = tmp / "AddIns"
        addins.mkdir()
        os.symlink(ADDIN, addins / "Arges")
        original = bootstrap.MACOS_ADDINS_DIR
        bootstrap.MACOS_ADDINS_DIR = addins
        try:
            raises("refuses to clobber a checkout symlink",
                   bootstrap.install_addin, RuntimeError, "symlink")
            truthy("and leaves it in place", (addins / "Arges").is_symlink())
        finally:
            bootstrap.MACOS_ADDINS_DIR = original

    # a clean install copies, and is idempotent
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        addins = tmp / "AddIns"
        addins.mkdir()
        original = bootstrap.MACOS_ADDINS_DIR
        bootstrap.MACOS_ADDINS_DIR = addins
        try:
            target, action = bootstrap.install_addin()
            check("installs", action, "installed")
            truthy("copied, not linked", target.is_dir() and not target.is_symlink())
            truthy("loader present", (target / "Arges.py").is_file())
            truthy("manifest present", (target / "Arges.manifest").is_file())
            truthy("no __pycache__ shipped", not (target / "__pycache__").exists())

            target, action = bootstrap.install_addin()
            check("second run is a no-op", action, "unchanged")

            (target / "arges_impl.py").write_text("# stale\n", encoding="utf-8")
            target, action = bootstrap.install_addin()
            check("a modified copy is refreshed", action, "updated")
            truthy("and restored to the real thing",
                   (target / "arges_impl.py").read_text() != "# stale\n")
        finally:
            bootstrap.MACOS_ADDINS_DIR = original
else:
    # Not macOS: refusing is the correct behaviour, and it should say why and
    # where to look rather than failing obscurely on a missing directory.
    print("Refusing to install off macOS")
    raises("refuses to install on an unsupported platform",
           bootstrap.install_addin, RuntimeError, "unsupported platform")
    raises("and names the platform it is on",
           bootstrap.install_addin, RuntimeError, platform.system())
    raises("and points at where the portability notes live",
           bootstrap.install_addin, RuntimeError, "CONTRIBUTING")


# ---------------------------------------------------------------- cli ----
print("CLI")

parser = cli.build_parser()
check("bare invocation serves (what an MCP client runs)",
      parser.parse_args([]).command, None)
check("serve is explicit too", parser.parse_args(["serve"]).command, "serve")
check("install", parser.parse_args(["install"]).command, "install")
truthy("install --rotate-token", parser.parse_args(["install", "--rotate-token"]).rotate_token)
check("uninstall", parser.parse_args(["uninstall"]).command, "uninstall")
truthy("uninstall --purge", parser.parse_args(["uninstall", "--purge"]).purge)
check("status", parser.parse_args(["status"]).command, "status")

def _reject_unknown():
    # argparse prints its usage to stderr on failure; swallow it so a passing
    # run stays readable.
    saved = sys.stderr
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
    try:
        parser.parse_args(["nonsense"])
    finally:
        sys.stderr.close()
        sys.stderr = saved


raises("an unknown subcommand is rejected", _reject_unknown, SystemExit)

# purge must never be able to aim outside HOME
truthy("purge is guarded to $HOME",
       "not inside" in (bootstrap.run_uninstall.__doc__ or "")
       or "home" in bootstrap.run_uninstall.__code__.co_names)


# ------------------------------------------------------ the only rm -rf ------
# scripts/uninstall.sh --purge is the single destructive command in this repo.
# It was covered only by the assertion above, which greps a DOCSTRING on a
# different function in a different language — that would pass with the shell
# guard deleted.
#
# The whole script cannot be run here: `pkill -f agent_service.py` is not
# HOME-scoped and would kill a live chat service on the developer's machine.
# So the real function text is lifted out of the file and driven directly. It
# is their code, not a copy of it — editing the guard changes what runs here.
print("The uninstaller's rm -rf")

import re as _re          # noqa: E402
import shutil as _shutil  # noqa: E402
import subprocess as _sp  # noqa: E402
import tempfile as _tf    # noqa: E402

_uninstall = (REPO / "scripts" / "uninstall.sh").read_text(encoding="utf-8")
_guard = _re.search(r"^purge_config_dir\(\) \{.*?^\}", _uninstall, _re.S | _re.M)
truthy("the guard function is still where the test expects it", _guard)


def purge(config_dir, home):
    """Run the real guard with these values. Returns (exit_code, still_exists)."""
    script = f"{_guard.group(0)}\nCONFIG_DIR={config_dir!r}\nHOME={home!r}\npurge_config_dir\n"
    done = _sp.run(["bash", "-c", script], capture_output=True, text=True)
    return done.returncode, os.path.isdir(config_dir)


if _guard:
    sandbox = _tf.mkdtemp(prefix="uninstall-guard-")
    try:
        # The ordinary case: a real config dir inside HOME goes.
        target = os.path.join(sandbox, ".arges")
        os.makedirs(target)
        open(os.path.join(target, "token"), "w").close()
        check("a config dir inside HOME is deleted", purge(target, sandbox), (0, False))

        # A nested path is still inside HOME and still fine.
        nested = os.path.join(sandbox, "a", "b", "c")
        os.makedirs(nested)
        check("a nested path inside HOME is deleted", purge(nested, sandbox), (0, False))

        # The ones that matter. Each of these would be a catastrophe.
        keep = os.path.join(sandbox, "keep-me")
        os.makedirs(keep, exist_ok=True)

        # CONFIG_DIR == HOME itself: `rm -rf $HOME`. The pattern requires at
        # least one character after the slash precisely to stop this.
        code, survived = purge(sandbox, sandbox)
        check("HOME itself is refused", (code, survived), (1, True))
        truthy("and nothing under it was touched", os.path.isdir(keep))

        # HOME with a trailing slash and nothing else.
        code, survived = purge(sandbox + "/", sandbox)
        check("HOME with a trailing slash is refused", code, 1)
        truthy("still untouched", os.path.isdir(keep))

        # A sibling directory whose name merely STARTS with HOME's — a plain
        # prefix check rather than a path check would delete this.
        sibling = sandbox + "-other"
        os.makedirs(os.path.join(sibling, "data"), exist_ok=True)
        code, survived = purge(os.path.join(sibling, "data"), sandbox)
        check("a path merely prefixed by HOME is refused", (code, survived), (1, True))

        # Outside HOME entirely.
        outside = _tf.mkdtemp(prefix="not-home-")
        code, survived = purge(outside, sandbox)
        check("a path outside HOME is refused", (code, survived), (1, True))
        _shutil.rmtree(outside, ignore_errors=True)

        # The root, which is what an empty HOME would collapse everything to.
        code, _ = purge("/", sandbox)
        check("the filesystem root is refused", code, 1)
        truthy("and the sandbox survived every refusal", os.path.isdir(keep))
        _shutil.rmtree(sibling, ignore_errors=True)
    finally:
        _shutil.rmtree(sandbox, ignore_errors=True)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
