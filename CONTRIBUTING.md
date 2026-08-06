# Contributing

## Before anything else

Read [SECURITY.md](SECURITY.md). This project hands a language model arbitrary code execution inside
Fusion, on purpose. Changes near the bridge, the token, or the destructive-operation gate need more
care than their size suggests.

## Getting set up

```sh
git clone https://github.com/francomichetti-dev/3d-mcp.git
cd 3d-mcp
scripts/install.sh
```

Then, once, in Fusion: **Utilities → Add-Ins → FusionBridge → Run**. If it is not listed, restart
Fusion — it scans that folder only at launch.

Requires macOS, Fusion, [`uv`](https://docs.astral.sh/uv/), `python3` and the `claude` CLI.

## Running the tests

```sh
tests/run.sh
```

No Fusion and no network needed — it stubs everything it touches. Anything you can cover here rather
than by hand, cover here.

## The edit–reload loop

`fusion_bridge_impl.py` hot-reloads, so you do not need to restart Fusion:

```sh
curl -sS -X POST -H "X-Fusion-Bridge-Token: $(cat ~/.fusion-mcp/token)" \
  http://127.0.0.1:7654/reload
```

A syntax error is refused with 400 and the running bridge is left untouched. Changes to
`FusionBridge.py` or the manifest still need **Stop/Run** in Fusion, because the loader itself is
what is being replaced.

For the chat panel: restart the service (`scripts/fusion-chat.sh --stop` then run it again). The
palette caches its page, so re-pointing it at the service is what forces a reload.

## What good looks like here

- **Verify against Fusion, do not reason about it.** Most of the sharp edges in this codebase were
  found empirically and would not have been guessed: `viewOrientation` silently reverts, topology
  reads are stale within the same call, USD export appends its own extension, every unsaved document
  is named `Untitled`. If you are asserting how the API behaves, show the probe output.
- **Comments explain why, not what.** The existing ones record what was tried and what broke; keep
  that. A comment saying a pattern is deliberate is what stops the next person "fixing" it back.
- **Match the surrounding style.** Standard library where possible; the add-in is stdlib-only by
  necessity, since it runs inside Fusion's interpreter.
- **Loopback stays loopback.** A change that binds anything to a non-loopback address, weakens the
  token check, or adds an outbound call will not be merged.

## Layout

```
server/src/fusion_mcp/          the published package (PyPI: fusion-3d-mcp)
    server.py                   the MCP server itself
    cli.py                      `fusion-3d-mcp` — bare invocation serves stdio
    bootstrap.py                `install` / `uninstall` / `status`
    addin/FusionBridge/         the Fusion add-in, shipped inside the package
agent/                          the docked chat panel's service (repo only)
skill/fusion-360/               Fusion API knowledge, linked into ~/.claude/skills
```

The add-in lives inside the package rather than at the repo root for a build reason: `uv build`
builds the wheel from the sdist, and a `force-include` cannot reach outside the sdist root — an
add-in above `server/` simply would not ship. One canonical copy also means the symlink a checkout
creates and the copy `fusion-3d-mcp install` makes are always the same code.

## Releasing

Tag-driven, and every version must agree:

```sh
# bump all three, then:
git tag v0.2.0 && git push --tags
```

- `server/pyproject.toml` → `version`
- `server.json` → `version` **and** `packages[0].version`
- `server/src/fusion_mcp/__init__.py` → `__version__`

CI refuses the release if the tag and those disagree, or if `server/README.md` has lost its
`mcp-name:` marker — the registry uses that marker to verify the PyPI package is yours, so a release
without it would be rejected after the package was already published.

Publishing uses OIDC throughout: PyPI trusted publishing and `mcp-publisher login github-oidc`. No
tokens are stored in the repository.

## Reporting bugs

Include your Fusion version (`Help → About`), what you asked for, and the relevant part of
`~/.fusion-mcp/addin.log`, `server.log` or `agent.log`. Fusion swallows add-in exceptions silently,
so those logs are usually the only record that something failed.
