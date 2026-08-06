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

## Reporting bugs

Include your Fusion version (`Help → About`), what you asked for, and the relevant part of
`~/.fusion-mcp/addin.log`, `server.log` or `agent.log`. Fusion swallows add-in exceptions silently,
so those logs are usually the only record that something failed.
