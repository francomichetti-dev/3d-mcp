# End-to-end harness — no Windows, no CAD licence required

`stub_poller.py` stands in for Rhino: it claims jobs from the broker and answers
with canned results, so the whole chain can be exercised on any machine.

    claude ──MCP──▶ rhino_mcp.py ──HTTP──▶ broker ──▶ stub_poller

What this proves, and what it does not: everything between Claude and Rhino's
front door is covered — MCP protocol, tool discovery, argument passing, image
blocks, error propagation. RhinoCommon itself is not; only a real Rhino can
prove that.

## Automatically

`tests/test_e2e_chain.py` runs this whole chain as part of `tests/run.sh` — it
starts a broker, this stub and the MCP server, and drives the MCP protocol
directly. Nothing to set up, and it runs in CI.

That covers the joins. What it deliberately does not cover is the *model*: it
speaks MCP itself rather than asking Claude to. For that, use the manual
procedure below — it is the only way to check that Claude can actually see a
screenshot, and no amount of protocol testing substitutes for it.

## By hand, with real Claude Code

```sh
# 1. broker on a test port
cd server && FUSION_BROKER_PORT=7699 uv run --frozen --no-sync python -c "
import sys, time, pathlib; sys.path.insert(0, 'src')
from fusion_mcp import broker
broker.TOKEN_PATH = pathlib.Path('<token file>')
broker.ALLOWED_HOSTS = frozenset(('127.0.0.1:7699','localhost:7699'))
broker.serve(port=7699); time.sleep(600)"

# 2. the stand-in for Rhino
python3 tests/e2e/stub_poller.py

# 3. drive it with real Claude Code
claude -p "read the document state" --strict-mcp-config --mcp-config mcp.json \
  --allowedTools "mcp__rhino__rhino_state" --output-format json
```

Measured results the first time this ran:

* `rhino_state` + `rhino_execute` — Claude reported back the exact values the
  stub returned ("Millimeters, tolerance 0.001, Default layer"), 4 turns
* `rhino_screenshot` — Claude SAW the image and described it accurately as a
  uniform square with no geometry, which is what a 1x1 PNG is. The vision loop
  survives the MCP boundary.
