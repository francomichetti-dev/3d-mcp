"""Install the FusionBridge add-in and mint the bridge token.

The repo has scripts/install.sh, which symlinks the add-in out of the checkout
so an edit is live immediately. That is the right thing for development and the
wrong thing for a published package: installed with uvx, this code lives in a
cache directory that is disposable and may be garbage-collected, so a symlink
into it would rot. Here the add-in is **copied**.

The consequence is worth stating plainly: upgrading the package does not upgrade
the add-in. Re-run `fusion-3d-mcp install` after an upgrade, and the version
check between server and bridge will tell you if you forget.
"""

from __future__ import annotations

import filecmp
import os
import platform
import secrets
import shutil
import sys
from pathlib import Path

FUSION_DIR = Path("~/.fusion-mcp").expanduser()
TOKEN_PATH = FUSION_DIR / "token"
EXPORT_DIR = Path("~/Documents/fusion-mcp-exports").expanduser()

ADDIN_NAME = "FusionBridge"
# Fusion's per-user add-in directory. macOS only for now; the add-in itself is
# plain Python and portable, this path is not.
MACOS_ADDINS_DIR = Path(
    "~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns"
).expanduser()

MANUAL_STEP = """
One manual step remains — Fusion cannot enable an add-in from outside:

    Fusion  ->  Utilities  ->  Add-Ins  ->  FusionBridge  ->  Run

If FusionBridge is not in that list, restart Fusion: it scans its add-ins
folder only at launch, so anything installed while it was running is invisible
until you relaunch. It auto-starts on later launches after that.
"""


def bundled_addin() -> Path:
    """The add-in shipped inside this package."""
    return Path(__file__).resolve().parent / "addin" / ADDIN_NAME


def addins_dir() -> Path:
    if platform.system() != "Darwin":
        raise RuntimeError(
            f"unsupported platform {platform.system()!r}: this installer only knows "
            "where Fusion keeps add-ins on macOS. The add-in is portable — see "
            "CONTRIBUTING.md in the repository."
        )
    return MACOS_ADDINS_DIR


def ensure_token(rotate: bool = False) -> tuple[Path, bool]:
    """Create the shared secret if absent. Returns (path, created)."""
    FUSION_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(FUSION_DIR, 0o700)

    existing = ""
    if TOKEN_PATH.is_file() and not rotate:
        try:
            existing = TOKEN_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            existing = ""
    if existing:
        os.chmod(TOKEN_PATH, 0o600)
        return TOKEN_PATH, False

    # Written 0600 from the start rather than chmodded afterwards, so the secret
    # is never briefly world-readable.
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(secrets.token_hex(32) + "\n")
    return TOKEN_PATH, True


def install_addin() -> tuple[Path, str]:
    """Copy the bundled add-in into Fusion's add-ins folder."""
    source = bundled_addin()
    if not source.is_dir():
        raise RuntimeError(
            f"the add-in is missing from this package (looked in {source}). "
            "This is a packaging bug — please report it."
        )

    target_dir = addins_dir()
    if not target_dir.parent.exists():
        raise RuntimeError(
            f"{target_dir.parent} does not exist — install Autodesk Fusion and "
            "launch it once, then run this again."
        )
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / ADDIN_NAME

    if target.is_symlink():
        # A checkout's scripts/install.sh puts a symlink here on purpose. Silently
        # replacing it with a copy would strand the developer wondering why their
        # edits stopped taking effect.
        raise RuntimeError(
            f"{target} is a symlink, which means it was installed from a checkout "
            "by scripts/install.sh. Leaving it alone. Remove it first if you want "
            "the packaged copy instead."
        )

    action = "installed"
    if target.exists():
        action = "unchanged" if _same_tree(source, target) else "updated"
        if action == "updated":
            shutil.rmtree(target)
    if action != "unchanged":
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return target, action


def _same_tree(left: Path, right: Path) -> bool:
    comparison = filecmp.dircmp(left, right, ignore=["__pycache__"])
    if comparison.left_only or comparison.right_only or comparison.diff_files:
        return False
    return all(
        _same_tree(left / name, right / name) for name in comparison.common_dirs
    )


def run_install(rotate_token: bool = False) -> int:
    # flush: stdout is block-buffered when piped, so without this the header
    # lands after an error written to stderr and the output reads backwards.
    print("Installing the FusionBridge add-in\n", flush=True)
    try:
        target, action = install_addin()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"  add-in {action}: {target}")

    token_path, created = ensure_token(rotate=rotate_token)
    print(f"  token {'created' if created else 'kept'}: {token_path} (0600)")
    if created and not rotate_token:
        pass
    elif rotate_token:
        print("  the add-in re-reads the token per request — no restart needed")

    try:
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        print(f"  exports go to: {EXPORT_DIR}")
    except OSError as exc:
        print(f"  could not create {EXPORT_DIR}: {exc}")

    print(MANUAL_STEP)
    print("Then point your MCP client at:  fusion-3d-mcp")
    return 0


def run_uninstall(purge: bool = False) -> int:
    print("Removing the FusionBridge add-in\n")
    target = MACOS_ADDINS_DIR / ADDIN_NAME
    if target.is_symlink():
        print(f"  SKIPPED: {target} is a symlink from a checkout — "
              "use that checkout's scripts/uninstall.sh")
    elif target.is_dir():
        shutil.rmtree(target)
        print(f"  removed {target}")
    else:
        print(f"  nothing to remove at {target}")

    print("  stop it inside Fusion too: Utilities -> Add-Ins -> FusionBridge -> Stop")

    if purge:
        # Guarded because this is the one destructive path here, and an empty
        # HOME would otherwise aim it at the filesystem root.
        resolved = FUSION_DIR.resolve()
        home = Path.home().resolve()
        if resolved == home or home not in resolved.parents:
            print(f"  REFUSING to delete {resolved} — not inside {home}", file=sys.stderr)
            return 1
        if resolved.exists():
            shutil.rmtree(resolved)
            print(f"  deleted {resolved} (token, logs, saved chats)")
    else:
        print(f"  kept {FUSION_DIR} (token, logs, chats) — pass --purge to delete it")

    print(f"\nExports in {EXPORT_DIR} were not touched.")
    return 0


def run_status() -> int:
    import json
    import urllib.error
    import urllib.request

    print("Fusion MCP status\n")

    target = MACOS_ADDINS_DIR / ADDIN_NAME
    if target.is_symlink():
        print(f"  add-in: symlink from a checkout -> {os.readlink(target)}")
    elif target.is_dir():
        print(f"  add-in: installed at {target}")
    else:
        print(f"  add-in: NOT installed (expected {target})")

    token = ""
    if TOKEN_PATH.is_file():
        try:
            token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    print(f"  token:  {'present' if token else 'MISSING — run: fusion-3d-mcp install'}")

    if not token:
        return 1

    request = urllib.request.Request(
        "http://127.0.0.1:7654/health", headers={"X-Fusion-Bridge-Token": token}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        print(f"  bridge: up — Fusion {payload.get('app_version')}, "
              f"protocol v{payload.get('bridge_version')}, "
              f"document {payload.get('document')!r}")
        return 0
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"  bridge: not answering on 127.0.0.1:7654 ({exc})")
        print("          open Fusion, then Utilities -> Add-Ins -> FusionBridge -> Run")
        return 1
