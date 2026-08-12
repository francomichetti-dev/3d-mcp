# Contributing

## Before anything else

Read [SECURITY.md](SECURITY.md). This project hands a language model arbitrary code execution inside
CAD software, on purpose. Changes near a bridge, the token, or Fusion's destructive-operation gate
need more care than their size suggests.

## Getting set up

The two halves are independent. You only need the one you are working on.

**Fusion** — macOS, Fusion, [`uv`](https://docs.astral.sh/uv/), `python3`, the `claude` CLI:

```sh
git clone https://github.com/francomichetti-dev/3d-mcp.git
cd 3d-mcp
scripts/install.sh
```

Then, once, in Fusion: **Utilities → Add-Ins → Arges → Run**. If it is not listed, restart
Fusion — it scans that folder only at launch.

**Rhino** — Rhino 8 and the `claude` CLI. No `uv`, no install script, and no Fusion: everything under
`scripts/rhino/` is deliberately dependency-free, because `rhino_mcp.py` runs on Rhino's own Python
(3.9, with nothing installed) and `rhino-poller.py` runs *inside* Rhino, where it cannot import from
this package at all. [`scripts/rhino/SETUP.md`](scripts/rhino/SETUP.md) is written to be handed to
Claude Code rather than followed by hand.

Keep that constraint in mind before adding an import: a dependency in either file does not fail at
review, it fails on somebody's machine at setup.

## Running the tests

```sh
tests/run.sh
```

No Fusion and no network needed — it stubs everything it touches. Anything you can cover here rather
than by hand, cover here.

## The edit–reload loop

`arges_impl.py` hot-reloads, so you do not need to restart Fusion:

```sh
curl -sS -X POST -H "X-Arges-Bridge-Token: $(cat ~/.arges/token)" \
  http://127.0.0.1:7654/reload
```

A syntax error is refused with 400 and the running bridge is left untouched. Changes to
`Arges.py` or the manifest still need **Stop/Run** in Fusion, because the loader itself is
what is being replaced.

For the chat panel: restart the service (`scripts/fusion-chat.sh --stop` then run it again). The
palette caches its page, so re-pointing it at the service is what forces a reload.

On the Rhino side there is no hot reload, and what you restart depends on the file:

| Changed | To pick it up |
| --- | --- |
| `rhino_mcp.py` | start a new chat — Claude Code spawns the MCP server per session |
| `rhino-poller.py` | run `rhino-poller-stop.py` in Rhino, then `rhino-poller.py` again |
| `broker.py` | restart the broker (`broker-service.ps1` on Windows, `start-broker.sh` otherwise) |
| `rhino-chat.py` | close and reopen the window |

`rhinocode script` prints nothing at all — not even tracebacks — so a script that failed looks
exactly like one that did nothing. The poller writes `rhino-poller.log` **next to itself** in
`scripts/rhino/`, not under `~/.arges`; that file is usually the only evidence you get.

## What good looks like here

- **Verify against the CAD, do not reason about it.** Most of the sharp edges in this codebase were
  found empirically and would not have been guessed: `viewOrientation` silently reverts, topology
  reads are stale within the same call, USD export appends its own extension, every unsaved document
  is named `Untitled`. On the Rhino side, `RhinoApp.Idle` never fires while Rhino is unfocused,
  `InvokeOnUiThread` deadlocks against itself, a background thread takes Rhino down with it, and
  `os.chmod(0o600)` does not restrict anything on Windows. Every one of those cost hours and none
  were predictable from the documentation. If you are asserting how an API behaves, show the probe
  output. [`docs/rhino-handover.md`](docs/rhino-handover.md) collects the Rhino ones.
- **Comments explain why, not what.** The existing ones record what was tried and what broke; keep
  that. A comment saying a pattern is deliberate is what stops the next person "fixing" it back.
- **Match the surrounding style.** Standard library where possible; the add-in is stdlib-only by
  necessity, since it runs inside Fusion's interpreter.
- **Loopback stays loopback.** A change that binds anything to a non-loopback address, weakens the
  token check, or adds an outbound call will not be merged.

## Layout

```
server/src/arges_mcp/          the published package (PyPI: arges-mcp)
    server.py                   the Fusion MCP server itself
    cli.py                      `arges-mcp` — bare invocation serves stdio
    bootstrap.py                `install` / `uninstall` / `status`
    broker.py                   the job queue the Rhino half polls
    addin/Arges/         the Fusion add-in, shipped inside the package
scripts/rhino/                  the Rhino half — dependency-free, not published
    rhino_mcp.py                the Rhino MCP server
    rhino-poller.py             the timer that runs inside Rhino
    rhino-chat.py               the standalone chat window
agent/                          the docked chat panel's service (repo only)
skill/fusion-360/               Fusion API knowledge, linked into ~/.claude/skills
```

`broker.py` sits in the package rather than in `scripts/rhino/` because it is ordinary server code
that runs outside Rhino; only the files that must survive Rhino's bare interpreter live under
`scripts/rhino/`. The constants those two sides share are duplicated on purpose — the auth header,
the token path, the ports, the screenshot bounds — and `tests/test_consistency.py` exists to stop
them drifting, since every one of those mismatches fails silently.

The add-in lives inside the package rather than at the repo root for a build reason: `uv build`
builds the wheel from the sdist, and a `force-include` cannot reach outside the sdist root — an
add-in above `server/` simply would not ship. One canonical copy also means the symlink a checkout
creates and the copy `arges install` makes are always the same code.

## Releasing

Tag-driven, and every version must agree:

```sh
# bump all three, then:
git tag v0.2.0 && git push --tags
```

- `server/pyproject.toml` → `version`
- `server.json` → `version` **and** `packages[0].version`
- `server/src/arges_mcp/__init__.py` → `__version__`

CI refuses the release if the tag and those disagree, or if `server/README.md` has lost its
`mcp-name:` marker — the registry uses that marker to verify the PyPI package is yours, so a release
without it would be rejected after the package was already published.

Publishing uses OIDC throughout: PyPI trusted publishing and `mcp-publisher login github-oidc`. No
tokens are stored in the repository.

## Reporting bugs

Say which CAD, what you asked for, and include the log — in both halves the log is usually the only
record that anything failed at all.

- **Fusion**: your version (`Help → About`), plus the relevant part of `~/.arges/addin.log`,
  `server.log` or `agent.log`. Fusion swallows add-in exceptions silently.
- **Rhino**: your version and OS, plus `scripts/rhino/rhino-poller.log`. `rhinocode` prints nothing,
  so a traceback exists only in that file.
