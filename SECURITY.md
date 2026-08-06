# Security

Read this before installing. This project is not a sandbox, and it is not trying to be one.

## What it actually does

`fusion_execute` runs **arbitrary Python inside your live Fusion session**, with the full `adsk`
API and your user's privileges. That is the entire design: a fixed set of "create box / create hole"
tools can never cover the Fusion API, so the model is given the API itself.

Consequences, stated plainly:

- Anything that can reach the bridge can run code as you — read and write your files, not just your
  CAD model.
- Generated code can destroy work in the open document. Fusion's timeline covers most of it; it does
  not cover everything, and it does not cover a document that gets closed without saving.
- The chat panel auto-runs geometry operations, including deletes. Only `.close(` and `.save`/
  `.saveAs` stop to ask, because those are the two that nothing can undo.

**Work in a scratch Fusion project while you are getting a feel for it.**

## The boundary

There is exactly one, and it is the reason the above is tolerable:

| Control | What it is |
| --- | --- |
| Bind address | `127.0.0.1` only, never `0.0.0.0`. Not reachable from your LAN. |
| Host header | Requests must carry a `Host` of `127.0.0.1:7654` or `localhost:7654`, so a browser on another site cannot drive it via DNS rebinding. |
| Token | 64 hex chars generated at install, sent as `X-Fusion-Bridge-Token`, compared with `hmac.compare_digest`. Stored `0600` in `~/.fusion-mcp/`, which is `0700`. |
| Fail closed | No token file, or an empty one, and the listener refuses to start. |
| Request cap | Body limited to 5 MB; at most 8 concurrent connections; one job on Fusion's main thread at a time. |
| Logs | Never contain the token. |

**Do not expose this to a network.** Not to your LAN, not through a tunnel, not "just for testing".
There is no authentication model beyond a shared secret on loopback, and it is not built to have one.

## What leaves your machine

The bridge, the MCP server and the chat service make **no outbound calls at all**. There is no
telemetry.

The one exception is the deliberate one: the chat panel runs the Claude Agent SDK, which talks to
Anthropic. Your prompts, the code the model writes, viewport screenshots it takes, and any images
you attach go to the API as part of that conversation. If that is not acceptable for a given design,
use the MCP tools from your own Claude Code session instead, or do not use the panel for it.

Chats are stored locally in `~/.fusion-mcp/chats.json` and attachments in
`~/.fusion-mcp/attachments/` (`0600` in a `0700` directory). `scripts/uninstall.sh --purge` deletes
all of it.

## Reporting a vulnerability

Open a GitHub issue for anything already public. For something exploitable that is not, use GitHub's
**private vulnerability reporting** on this repository rather than a public issue.

Things worth reporting: any way to reach the bridge without the token, from another origin, or from
off the machine; any path that escapes the exports or attachments directories; anything that logs
the token.

Things that are not vulnerabilities: that `fusion_execute` runs arbitrary code (that is the feature),
and that a user who runs the installer can then drive their own Fusion.
