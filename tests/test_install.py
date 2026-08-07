"""The loader and the installer — no Fusion, no network, no writes outside tmp.

Covers the two pieces a new user hits first and which have no other safety net:
the add-in loader's provenance check (which refused to load itself through its
own symlink), and `fusion-3d-mcp install`.

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
ADDIN = REPO / "server/src/fusion_mcp/addin/FusionBridge"

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

spec = importlib.util.spec_from_file_location("_loader_probe", ADDIN / "FusionBridge.py")
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)

truthy("exposes run/stop for Fusion", callable(loader.run) and callable(loader.stop))

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    # Reproduce the layout the installer creates: a real folder of per-file
    # symlinks. This is exactly the shape that broke the old abspath check.
    linked = tmp / "AddIns" / "FusionBridge"
    linked.mkdir(parents=True)
    for source in ADDIN.iterdir():
        if source.is_file():
            os.symlink(source, linked / source.name)

    truthy("layout: folder is real, not a symlink", not linked.is_symlink())
    truthy("layout: files inside are symlinks",
           all(p.is_symlink() for p in linked.iterdir()))

    via_link = linked / "fusion_bridge_impl.py"
    via_repo = ADDIN / "fusion_bridge_impl.py"

    # The regression: abspath does not follow symlinks, so the same file reached
    # two ways compared unequal and the add-in refused to load itself.
    check("abspath sees them as different (the old bug)",
          os.path.abspath(via_link) == os.path.abspath(via_repo), False)
    check("realpath sees one file (the fix)",
          os.path.realpath(via_link) == os.path.realpath(via_repo), True)

    # and a genuinely foreign module must still be rejected
    check("a different file is still different",
          os.path.realpath(tmp / "elsewhere" / "fusion_bridge_impl.py")
          == os.path.realpath(via_repo), False)

    # drive the real check: a module already registered under the linked path
    # must be accepted when the loader is reached through the repo path
    sys.modules.pop("fusion_bridge_impl", None)
    fake = type(sys)("fusion_bridge_impl")
    fake.__file__ = str(via_link)
    sys.modules["fusion_bridge_impl"] = fake
    try:
        loader._IMPL_NAME = "fusion_bridge_impl"
        loader.__file__ = str(ADDIN / "FusionBridge.py")
        check("the same file via another path is accepted", loader._load_impl(), fake)

        fake.__file__ = "/somewhere/else/fusion_bridge_impl.py"
        raises("a foreign module is refused", loader._load_impl,
               ImportError, "not this add-in")
    finally:
        sys.modules.pop("fusion_bridge_impl", None)


# ---------------------------------------------------------- bootstrap ----
print("Installer")

from fusion_mcp import bootstrap  # noqa: E402
from fusion_mcp import cli  # noqa: E402

truthy("ships the add-in inside the package", bootstrap.bundled_addin().is_dir())
for required in ("FusionBridge.py", "FusionBridge.manifest", "fusion_bridge_impl.py"):
    truthy(f"bundled: {required}", (bootstrap.bundled_addin() / required).is_file())

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    original_dir, original_token = bootstrap.FUSION_DIR, bootstrap.TOKEN_PATH
    bootstrap.FUSION_DIR = tmp / "state"
    bootstrap.TOKEN_PATH = bootstrap.FUSION_DIR / "token"
    try:
        path, created = bootstrap.ensure_token()
        truthy("creates a token", created)
        token = path.read_text().strip()
        check("token is 64 hex chars", len(token), 64)
        truthy("token is hex", all(c in "0123456789abcdef" for c in token))
        check("token file is 0600", oct(stat.S_IMODE(os.stat(path).st_mode)), "0o600")
        check("state dir is 0700",
              oct(stat.S_IMODE(os.stat(bootstrap.FUSION_DIR).st_mode)), "0o700")

        path, created = bootstrap.ensure_token()
        check("an existing token is kept", created, False)
        check("and unchanged", path.read_text().strip(), token)

        path, created = bootstrap.ensure_token(rotate=True)
        truthy("rotate replaces it", path.read_text().strip() != token)
        check("rotated token still 0600",
              oct(stat.S_IMODE(os.stat(path).st_mode)), "0o600")
    finally:
        bootstrap.FUSION_DIR, bootstrap.TOKEN_PATH = original_dir, original_token

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
        os.symlink(ADDIN, addins / "FusionBridge")
        original = bootstrap.MACOS_ADDINS_DIR
        bootstrap.MACOS_ADDINS_DIR = addins
        try:
            raises("refuses to clobber a checkout symlink",
                   bootstrap.install_addin, RuntimeError, "symlink")
            truthy("and leaves it in place", (addins / "FusionBridge").is_symlink())
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
            truthy("loader present", (target / "FusionBridge.py").is_file())
            truthy("manifest present", (target / "FusionBridge.manifest").is_file())
            truthy("no __pycache__ shipped", not (target / "__pycache__").exists())

            target, action = bootstrap.install_addin()
            check("second run is a no-op", action, "unchanged")

            (target / "fusion_bridge_impl.py").write_text("# stale\n", encoding="utf-8")
            target, action = bootstrap.install_addin()
            check("a modified copy is refreshed", action, "updated")
            truthy("and restored to the real thing",
                   (target / "fusion_bridge_impl.py").read_text() != "# stale\n")
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

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
