# Security

Read this before installing. This project is not a sandbox, and it is not trying to be one.

## What it actually does

`fusion_execute` runs **arbitrary Python inside your live Fusion session**, with the full `adsk`
API and your user's privileges. `rhino_execute` does the same inside Rhino, with RhinoCommon. That
is the entire design: a fixed set of "create box / create hole" tools can never cover a CAD API, so
the model is given the API itself.

Consequences, stated plainly:

- Anything that can reach the bridge can run code as you — read and write your files, not just your
  CAD model.
- Generated code can destroy work in the open document. Fusion's timeline covers most of it; it does
  not cover everything, and it does not cover a document that gets closed without saving.
- The chat panel auto-runs geometry operations, including deletes. On the Fusion side, only
  `.close(` and `.save`/`.saveAs` stop to ask, because those are the two that nothing can undo.
- **The Rhino side has no such gate.** Nothing stops to ask, and Rhino's undo does not cover a
  document that gets closed without saving. A plausible-sounding instruction — "clear the scene so
  I can start fresh" — empties whatever file is open. This is not hypothetical: during development
  a cleanup script written for an assumed-empty scratch document was one working service away from
  deleting 56 objects of someone's real work.

**Work in a scratch document while you are getting a feel for it.** On Rhino, check what is open
before asking for anything destructive — `rhino_state` names the file and counts the objects.

## The boundary

There is exactly one, and it is the reason the above is tolerable:

| Control | What it is |
| --- | --- |
| Bind address | `127.0.0.1` only, never `0.0.0.0`. Not reachable from your LAN. Both the Fusion listener (`7654`) and the Rhino broker (`7656`). |
| Host header | Requests must carry a `Host` matching the service's own loopback address and port, so a browser on another site cannot drive it via DNS rebinding. On all three listeners: the Fusion bridge (`7654`), the chat service (`7655`) and the Rhino broker (`7656`). The chat service matters most and had it last — it holds the token and forwards to the bridge, so anything reaching it gets authenticated code execution without needing the token at all. |
| Token | 64 hex chars generated at install, sent as `X-Fusion-Bridge-Token`, compared with `hmac.compare_digest`. Stored `0600` in `~/.fusion-mcp/`, which is `0700`. |
| Fail closed | No token file, an empty one, or one that is only whitespace, and nothing is served. The Fusion add-in refuses to start and says so; the broker starts but answers every request `503`. Emptiness is checked explicitly, because `compare_digest(b"", b"")` is true — a truncated token file must not authorise a caller who presents nothing. |
| Request cap | Body limited to 5 MB and at most 8 concurrent connections — on the Fusion listener and the Rhino broker alike, a connection past the cap is refused with `503` rather than given a thread. One job runs in the CAD at a time on both sides. |
| Rhino direction | Rhino is never listened to *on*. The poller inside Rhino makes outbound requests to the broker and nothing accepts connections inside the CAD process. |
| Logs | Never contain the token. |

**Do not expose this to a network.** Not to your LAN, not through a tunnel, not "just for testing".
There is no authentication model beyond a shared secret on loopback, and it is not built to have one.

## What leaves your machine

The bridge, the MCP server and the chat service make **no outbound calls at all**. There is no
telemetry.

The deliberate exception is the chat: the Fusion panel runs the Claude Agent SDK and the Rhino
window drives the `claude` CLI. Both talk to Anthropic. Your prompts, the code the model writes,
viewport screenshots it takes, and any files you attach go to the API as part of that conversation.
If that is not acceptable for a given design, use the MCP tools from your own Claude Code session
instead, or do not use the chat for it.

Neither needs an API key — both run on the Claude subscription you already have, so there is no
second credential to store or leak.

**What the Rhino window grants Claude Code.** It does not hand over a general-purpose agent. Each
turn is launched with `--strict-mcp-config`, so only this project's MCP server is loaded and any
other MCP servers you have configured are ignored; `--allowedTools` naming exactly the three
`rhino_*` tools; and a `Read` scoped to the attachments directory alone. Anything else Claude Code
can normally do — writing files, running shell commands, fetching URLs — is not pre-approved, and in
a non-interactive run an unapproved tool is refused rather than queued for a prompt that no one will
answer.

Chats are stored locally in `~/.fusion-mcp/chats.json` and attachments in
`~/.fusion-mcp/attachments/` — files `0600` inside a `0700` directory, and each copied attachment
re-restricted after copying, since `copy2` preserves whatever mode the original had.

On Windows that is done with an explicit ACL (`icacls /inheritance:r /grant:r`), not `chmod`.
`os.chmod` there only toggles the read-only attribute and does not restrict access at all: measured
on a real machine, a file "locked" with `chmod(0600)` still listed SYSTEM, Administrators and the
user with full control, all inherited.

`scripts/uninstall.sh --purge` deletes all of it.

## Reporting a vulnerability

Open a GitHub issue for anything already public. For something exploitable that is not, use GitHub's
**private vulnerability reporting** on this repository rather than a public issue.

Things worth reporting: any way to reach the bridge or the broker without the token, from another
origin, or from off the machine; any path that escapes the exports or attachments directories;
anything that logs the token; any way to widen what the Rhino window pre-approves beyond the three
`rhino_*` tools and the scoped attachment read.

Things that are not vulnerabilities: that `fusion_execute` runs arbitrary code (that is the feature),
and that a user who runs the installer can then drive their own Fusion.
