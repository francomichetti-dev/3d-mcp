<!-- mcp-name: io.github.francomichetti-dev/3d-mcp -->

# arges-mcp

**Model in Autodesk Fusion by prompting.** An MCP server that lets a model write Fusion API Python,
run it inside a live Fusion session, *look at the result through viewport screenshots*, correct
itself, and export print-ready files.

The screenshot loop is the point. A fixed set of "create box / create hole" tools can never cover
the Fusion API, and a model writing CAD code blind gets things subtly wrong — an extrude in the
wrong direction, a profile that grabbed the wrong region — with no exception raised. Arbitrary API
access plus eyes turns that into a design → look → correct loop.

> **Read this before installing.** `fusion_execute` runs arbitrary Python inside your Fusion session
> with your privileges. It is not a sandbox. The only boundary is that the bridge binds `127.0.0.1`
> and requires a token generated at install — **never expose it to a network**. Generated code can
> also mangle an open design, so work in a scratch Fusion project while you get a feel for it.
> Full threat model: [SECURITY.md](https://github.com/francomichetti-dev/3d-mcp/blob/main/SECURITY.md).

## Install

Two halves have to agree: this server, and a small add-in that runs *inside* Fusion. A Python
package cannot reach into another application, so installing the add-in is an explicit step:

```sh
uvx arges install
```

That copies the add-in into Fusion's add-ins folder and creates a bridge token at `~/.arges/`
(`0600`, inside a `0700` directory). Then, once, in Fusion:

> **Utilities → Add-Ins → select Arges → Run**

If Arges is not listed, restart Fusion — it scans that folder only at launch. It auto-starts
on later launches.

Check it:

```sh
uvx arges status
```

## Point your MCP client at it

```json
{
  "mcpServers": {
    "fusion": {
      "command": "uvx",
      "args": ["arges-mcp"]
    }
  }
}
```

Or with Claude Code:

```sh
claude mcp add fusion -- uvx arges-mcp
```

## Tools

| Tool | What it does |
| --- | --- |
| `fusion_execute` | Runs Python inside Fusion with `adsk`, `app`, `ui`, `design` injected. The namespace persists across calls. |
| `fusion_screenshot` | Viewport PNG (`front`, `top`, `right`, `iso`, `fit`) returned as a real image, not base64 text. |
| `fusion_export` | STL / STEP / 3MF / USD / F3D into `~/Documents/arges-exports/`. |
| `fusion_download` | The same into the configured save folder (Downloads by default), never overwriting. |
| `fusion_save` | The design as one `.f3d` in that folder. Runs automatically after every successful `fusion_execute`; `ARGES_AUTOSAVE=0` turns that off. |
| `fusion_state` | Document, units, design type, timeline count, parameters, top-level bodies and components. |

A failing script is a **normal result**, not a tool error — the traceback comes back verbatim so the
model can read it and fix its own code.

Two settings, both read per call so neither needs a restart. `ARGES_AUTOSAVE=0` stops the automatic
save; `ARGES_SAVE_DIR` sets where saves and downloads go when no folder has been chosen in the chat
panel, which writes `config.json` in the state directory and wins over both.

## Upgrading

Upgrading the package does not upgrade the add-in, because the add-in was copied into Fusion. Re-run
`uvx arges install` after an upgrade. If you forget, the version check between server and
bridge will say so rather than misbehaving quietly.

## Requirements

macOS with Autodesk Fusion installed and launched at least once, and Python 3.11+.

Any MCP client works - this server imports nothing Claude-specific. The server and add-in are plain
Python; what is macOS-specific is only knowing where Fusion keeps its add-ins.

## More

Source, the docked chat panel, the Fusion API knowledge skill, and the full documentation:
**https://github.com/francomichetti-dev/3d-mcp**

MIT licensed.
