"""Entry point for the `arges-mcp` command.

Bare invocation serves MCP over stdio, because that is how an MCP client starts
it — `uvx arges-mcp` with no arguments has to be the server and nothing
else. Anything printed to stdout by the other subcommands would corrupt the
JSON-RPC stream, so they exist only when named explicitly.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arges-mcp",
        description="Model in Autodesk Fusion by prompting. "
                    "Run with no arguments to serve MCP over stdio.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="serve MCP over stdio (the default)")

    install = sub.add_parser(
        "install",
        help="copy the Arges add-in into Fusion and create the bridge token",
    )
    install.add_argument(
        "--rotate-token", action="store_true",
        help="replace the existing token instead of keeping it",
    )

    uninstall = sub.add_parser("uninstall", help="remove the add-in")
    uninstall.add_argument(
        "--purge", action="store_true",
        help="also delete ~/.fusion-mcp (token, logs, saved chats)",
    )

    sub.add_parser("status", help="report whether the add-in and bridge are up")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command in (None, "serve"):
        # Imported lazily: this pulls in fastmcp and httpx, which the install and
        # status paths have no use for — and which should not be able to break
        # `install` if the environment is half-built.
        from .server import main as serve

        serve()
        return 0

    from . import bootstrap

    if args.command == "install":
        return bootstrap.run_install(rotate_token=args.rotate_token)
    if args.command == "uninstall":
        return bootstrap.run_uninstall(purge=args.purge)
    if args.command == "status":
        return bootstrap.run_status()

    raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    sys.exit(main())
