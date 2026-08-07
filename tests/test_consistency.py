"""Constants that are duplicated on purpose must not drift apart.

The Rhino half is deliberately dependency-free: `rhino_mcp.py` runs on Rhino's
own Python with nothing installed, and `rhino-poller.py` runs *inside* Rhino,
where it cannot import from this package at all. So the obvious fix for the
duplication below — a shared module — is not available, and buying it would
cost the one-line setup that makes the Rhino side easy to install.

The duplication is therefore accepted, and this is what makes it safe. Every
constant here is one where a mismatch fails *silently*: change the auth header
in one file and requests are rejected with no clue why; change the token
directory and one half writes where the other never looks; let two services
share a port and whichever starts second dies.

    python3 tests/test_consistency.py
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

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


def read(rel):
    path = REPO / rel
    return path.read_text(encoding="utf-8") if path.exists() else ""


# Everything that speaks the bridge protocol, on both sides.
FUSION_SERVER = "server/src/fusion_mcp/server.py"
FUSION_ADDIN = "server/src/fusion_mcp/addin/FusionBridge/fusion_bridge_impl.py"
BROKER = "server/src/fusion_mcp/broker.py"
RHINO_MCP = "scripts/rhino/rhino_mcp.py"
RHINO_POLLER = "scripts/rhino/rhino-poller.py"
RHINO_CHAT = "scripts/rhino/rhino-chat.py"

ALL_SPEAKERS = [FUSION_SERVER, FUSION_ADDIN, BROKER, RHINO_MCP, RHINO_POLLER, RHINO_CHAT]


# ------------------------------------------------------------ auth header --
# A mismatch here means every request is rejected as unauthenticated, with
# nothing in any log saying the header name is the reason.
print("The auth header")
headers = {}
for rel in ALL_SPEAKERS:
    found = re.search(r'AUTH_HEADER\s*=\s*["\']([^"\']+)["\']', read(rel))
    if found:
        headers[rel] = found.group(1)

check("every component declares one", len(headers), len(ALL_SPEAKERS))
check("and they are all the same", len(set(headers.values())), 1)
check("it is the documented name", set(headers.values()), {"X-Fusion-Bridge-Token"})


# ------------------------------------------------------------- token path --
# One half writing the token where the other never reads it fails as "invalid
# token", which sends you looking at the token rather than at the path.
print("Where the token lives")
dirs = set()
for rel in ALL_SPEAKERS:
    text = read(rel)
    if ".fusion-mcp" in text:
        dirs.add(".fusion-mcp")
    other = re.findall(r'["\']\.([a-z0-9-]+)["\']\s*,\s*["\']token["\']', text)
    dirs.update("." + o for o in other)
check("everyone agrees on the directory", dirs, {".fusion-mcp"})

# The file is named either as its own path segment or inside a joined path,
# so match the name rather than one spelling of it.
for rel in [BROKER, RHINO_MCP, RHINO_POLLER, RHINO_CHAT]:
    text = read(rel)
    truthy(f"{Path(rel).name} names the token file",
           '"token"' in text or "/token" in text)


# ------------------------------------------------------------------ ports --
# The Fusion add-in's listener and the Rhino broker run at the same time on a
# machine with both installed. Sharing a port means whichever starts second
# fails to bind, and the symptom is "the bridge is not running".
print("Ports")


def port_of(rel, name):
    found = re.search(name + r'\s*=\s*(?:int\([^)]*\)\s*or\s*)?(\d{4})', read(rel))
    if found:
        return int(found.group(1))
    found = re.search(r'127\.0\.0\.1:(\d{4})', read(rel))
    return int(found.group(1)) if found else None


fusion_port = port_of(FUSION_ADDIN, "BIND_PORT")
broker_port = port_of(BROKER, "BROKER_PORT")
truthy("the Fusion add-in declares a port", fusion_port)
truthy("the broker declares a port", broker_port)
check("and they do not collide", fusion_port == broker_port, False)

# Everything on the Rhino side must point at the broker, not at Fusion's port.
for rel in [RHINO_MCP, RHINO_POLLER, RHINO_CHAT]:
    found = re.search(r'127\.0\.0\.1:(\d{4})', read(rel))
    check(f"{Path(rel).name} points at the broker", int(found.group(1)) if found else None,
          broker_port)


# ------------------------------------------------- screenshot bounds --------
# Both CADs reject out-of-range sizes rather than clamping, so that a capture
# never silently differs from what was asked for. The bounds should match, or
# the same prompt behaves differently depending on which CAD is open.
print("Screenshot bounds")
# Declared as a tuple on one side and three named constants on the other. The
# VALUES are what must agree; how each file spells them is its own business.
def screenshot_bounds(rel):
    text = read(rel)
    tup = re.search(r'MIN_SIDE,\s*MAX_WIDTH,\s*MAX_HEIGHT\s*=\s*(\d+),\s*(\d+),\s*(\d+)',
                    text)
    if tup:
        return tuple(int(g) for g in tup.groups())
    named = {}
    for key in ("MIN_SIDE", "MAX_WIDTH", "MAX_HEIGHT"):
        hit = re.search(r'SCREENSHOT_' + key + r'\s*=\s*(\d+)', text)
        if hit:
            named[key] = int(hit.group(1))
    if len(named) == 3:
        return (named["MIN_SIDE"], named["MAX_WIDTH"], named["MAX_HEIGHT"])
    return None


bounds = {rel: screenshot_bounds(rel) for rel in [FUSION_SERVER, RHINO_POLLER]}
truthy("the Fusion server declares bounds", bounds[FUSION_SERVER])
truthy("the Rhino poller declares bounds", bounds[RHINO_POLLER])
check("and the numbers agree", bounds[FUSION_SERVER], bounds[RHINO_POLLER])
check("which are the documented ones", bounds[RHINO_POLLER], (64, 1920, 1440))


# ------------------------------------------------------------ tool naming --
# The chat window auto-approves tools by name. A rename on one side without the
# other means the tool silently stops being permitted, and Claude Code then
# waits for an approval that the window never offers.
print("Tool names")
mcp_tools = set(re.findall(r'"name":\s*"(rhino_[a-z_]+)"', read(RHINO_MCP)))
allowed = set(re.findall(r'"mcp__rhino__(rhino_[a-z_]+)"', read(RHINO_CHAT)))
check("the MCP server exposes three tools", len(mcp_tools), 3)
check("and the chat window allows exactly those", allowed, mcp_tools)

handlers = set(re.findall(r'^HANDLERS\s*=|"(execute|state|screenshot)":', read(RHINO_POLLER),
                          re.M))
for kind in ["execute", "state", "screenshot"]:
    truthy(f"the poller handles '{kind}'", f'"{kind}"' in read(RHINO_POLLER))
    truthy(f"and the MCP server can ask for '{kind}'",
           f'submit("{kind}"' in read(RHINO_MCP))


# ---------------------------------------------------------------- loopback --
# The one security property that must never regress: nothing binds to anything
# but loopback, on either side.
print("Loopback only")
for rel in [FUSION_ADDIN, BROKER]:
    text = read(rel)
    truthy(f"{Path(rel).name} binds loopback", '127.0.0.1' in text)
    check(f"{Path(rel).name} never binds 0.0.0.0", "0.0.0.0" in text, False)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
