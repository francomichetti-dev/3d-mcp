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
import subprocess
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
FUSION_SERVER = "server/src/arges_mcp/server.py"
FUSION_ADDIN = "server/src/arges_mcp/addin/FusionBridge/fusion_bridge_impl.py"
BROKER = "server/src/arges_mcp/broker.py"
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


# ------------------------------------------------------- connection cap -----
# SECURITY.md states one number for both listeners. The broker went without a
# cap entirely for a while, under a document that said it had one; keeping the
# two values pinned together is what stops the claim drifting from either side.
print("Connection cap")
caps = {}
for rel in [FUSION_ADDIN, BROKER]:
    found = re.search(r'MAX_CONCURRENT_CONNECTIONS\s*=\s*(\d+)', read(rel))
    if found:
        caps[Path(rel).name] = int(found.group(1))

check("both listeners declare a cap", sorted(caps), ["broker.py", "fusion_bridge_impl.py"])
check("and it is the same number", len(set(caps.values())), 1)
check("which is what SECURITY.md says", set(caps.values()), {8})

security_doc = read("SECURITY.md")
truthy("SECURITY.md quotes that number",
       "%d concurrent connections" % (list(caps.values()) or [0])[0] in security_doc)

# The grace period before refusing. Both listeners release a slot on the
# handler thread after the client has moved on, so refusing the instant the cap
# is reached turns ordinary sequential traffic into spurious 503s. If the two
# sides disagree, the same client behaviour succeeds against one CAD and fails
# against the other, which is the hardest kind of bug to believe.
graces = {}
for rel in [FUSION_ADDIN, BROKER]:
    found = re.search(r'SLOT_GRACE_S\s*=\s*([\d.]+)', read(rel))
    if found:
        graces[Path(rel).name] = float(found.group(1))

check("both listeners declare a grace period", sorted(graces),
      ["broker.py", "fusion_bridge_impl.py"])
check("and it is the same", len(set(graces.values())), 1)
truthy("and it is long enough to absorb cleanup, short enough to still refuse",
       all(0.1 <= g <= 2.0 for g in graces.values()))


# ------------------------------------------------------- no outbound calls --
# SECURITY.md: "The bridge, the MCP server and the chat service make no
# outbound calls at all. There is no telemetry." That is the kind of promise
# that stays true only until someone adds a version check, a crash reporter or
# a docs fetch without thinking of it as a network call.
#
# An allowlist rather than a ban, so a genuinely needed URL is a deliberate
# edit to this list with a reason, not a silent addition.
print("No outbound calls")
ALLOWED_URLS = {
    # The install command the Rhino window SHOWS the user to copy. It is a
    # display string; nothing here ever fetches it.
    "https://claude.ai/install.sh",
    "https://claude.ai/install.ps1",
}

# OUR code only. An earlier version of this scan globbed the tree and swept
# server/.venv, then reported a few hundred URLs from third-party packages -
# a check that noisy is a check nobody reads.
NOT_OURS = (".venv", "site-packages", "__pycache__", "node_modules")

found_urls = set()
sources = sorted(list((REPO / "server" / "src").rglob("*.py"))
                 + list((REPO / "agent").rglob("*.py"))
                 + list((REPO / "scripts").rglob("*.py")))
sources = [p for p in sources if not any(part in NOT_OURS for part in p.parts)]
truthy("the scan found our sources", len(sources) >= 5)
for source in sources:
    for url in re.findall(r"https?://[a-zA-Z0-9./_-]+",
                          source.read_text(encoding="utf-8")):
        if not url.startswith(("http://127.0.0.1", "http://localhost")):
            found_urls.add(url)

check("no URL outside loopback and the allowlist", found_urls - ALLOWED_URLS, set())
truthy("and the loopback URLs are actually there, so the scan is looking",
       any("127.0.0.1" in read(rel) for rel in ALL_SPEAKERS))

# The install strings must stay strings. If one ever reaches a fetch, this is
# the file where that shows up.
chat = read(RHINO_CHAT)
for url in ALLOWED_URLS:
    if url in chat:
        for opener in ("urlopen(%s" % url, 'urlopen("%s' % url):
            check(f"{url} is never fetched", opener in chat, False)


# ------------------------------------------------------------- version ------
# Four places declare the version and all four must agree. Until now that was
# checked only by release.yml, which runs on a tag push — so a bump that
# touched three of the four would sit broken until release day, which is the
# worst possible moment to find out: the workflow's own comment notes that a
# registry entry pointing at a version PyPI does not have is the failure being
# guarded against.
print("Version")
import json as _json  # noqa: E402

versions = {}
found = re.search(r'^version\s*=\s*"([^"]+)"', read("server/pyproject.toml"), re.M)
if found:
    versions["server/pyproject.toml"] = found.group(1)
found = re.search(r'__version__\s*=\s*"([^"]+)"', read("server/src/arges_mcp/__init__.py"))
if found:
    versions["__init__.py"] = found.group(1)
try:
    manifest = _json.loads(read("server.json"))
    versions["server.json"] = manifest["version"]
    versions["server.json packages[0]"] = manifest["packages"][0]["version"]
except (ValueError, KeyError, IndexError):
    manifest = {}

check("all four version declarations were found", len(versions), 4)
check("and they agree", len(set(versions.values())), 1)
truthy("the version looks like a version",
       all(re.match(r"^\d+\.\d+\.\d+", v) for v in versions.values()))

# The registry verifies PyPI ownership through this marker. Losing it fails the
# release AFTER the package has already been published, which is unrecoverable
# for that version number.
server_readme = read("server/README.md")
marker = re.search(r"<!--\s*mcp-name:\s*(\S+)\s*-->", server_readme)
truthy("server/README.md carries the mcp-name marker", marker)
if marker and manifest:
    check("and it matches the name in server.json", marker.group(1), manifest.get("name"))


# -------------------------------------------------------- line endings ------
# Both directions cost real time on this project.
#
# cmd.exe mis-parses a batch file whose lines end in LF alone, which is why
# RHINO-CHAT.cmd — the thing a non-developer double-clicks — ships CRLF. And
# bash fails on a CRLF script with "$'\r': command not found", an error that
# names the carriage return in a way nobody recognises.
#
# The bytes in the git blob are what a Windows user receives, so that is what
# is checked, not the working copy: a contributor on macOS whose editor
# rewrites the endings would commit LF and break the launcher for someone else
# entirely, with the file still looking fine on their own machine.
print("Line endings")


def blob_bytes(rel):
    done = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=REPO, capture_output=True)
    return done.stdout if done.returncode == 0 else None


tracked_files = subprocess.run(["git", "ls-files"], cwd=REPO,
                               capture_output=True, text=True).stdout.split()

cmd_files = [f for f in tracked_files if f.endswith(".cmd")]
truthy("there is a .cmd to check", cmd_files)
for rel in cmd_files:
    raw = blob_bytes(rel)
    if raw is None:
        continue
    bare_lf = raw.count(b"\n") - raw.count(b"\r\n")
    check(f"{Path(rel).name} is CRLF in git, as cmd.exe needs", bare_lf, 0)

sh_files = [f for f in tracked_files if f.endswith(".sh")]
truthy("there are shell scripts to check", sh_files)
for rel in sh_files:
    raw = blob_bytes(rel)
    if raw is None:
        continue
    check(f"{Path(rel).name} has no CR, as bash needs", raw.count(b"\r\n"), 0)

# And the rules are declared, so git enforces them on everyone's checkout
# rather than relying on whoever edits next having the right settings.
attributes = read(".gitattributes")
truthy(".gitattributes exists", attributes)
truthy("it pins .cmd", re.search(r"^\*\.cmd\s+-text", attributes, re.M))
truthy("it pins .sh to LF", re.search(r"^\*\.sh\s+text eol=lf", attributes, re.M))


# ------------------------------------------------------------ packaging -----
# release.yml opens the built wheel and fails if the add-in is not inside it,
# because a package that installs without the add-in is a dead end: there is
# nothing for Fusion to run and no way to get it. That check needs a build, so
# it only happens on a tag.
#
# The structural reason the add-in ships is cheap to check without building:
# it lives INSIDE the packaged tree. CONTRIBUTING explains why it has to —
# hatch's force-include cannot reach outside the sdist root, so an add-in at
# the repo root would silently not ship. Move it and the wheel guard fails on
# release day; this fails immediately.
print("Packaging")
PACKAGED_ROOT = REPO / "server" / "src" / "arges_mcp"
ADDIN_FILES = ["FusionBridge.py", "FusionBridge.manifest", "fusion_bridge_impl.py"]
for name in ADDIN_FILES:
    truthy(f"{name} is inside the packaged tree",
           (PACKAGED_ROOT / "addin" / "FusionBridge" / name).is_file())

pyproject = read("server/pyproject.toml")
truthy("the wheel packages src/arges_mcp",
       re.search(r'packages\s*=\s*\[\s*"src/arges_mcp"', pyproject))

# The manifest is the file Fusion reads to find the add-in at all, and it is
# the one a broad exclude would take first, being the only non-.py file.
excludes = re.search(r'exclude\s*=\s*\[([^\]]*)\]', pyproject)
truthy("the build declares its excludes", excludes)
if excludes:
    patterns = re.findall(r'"([^"]+)"', excludes.group(1))
    check("and excludes only build litter", sorted(patterns),
          ["**/*.pyc", "**/__pycache__"])


# -------------------------------------------------------- host pinning ------
# Three listeners, three chances to forget. The chat service went without this
# until it was audited: it binds loopback, which stops another machine but not
# a browser whose DNS has been rebound to 127.0.0.1 — and it forwards to the
# bridge with the token, so reaching it is as good as having the token.
print("Host pinning")
AGENT_SERVICE = "agent/agent_service.py"
for rel in [FUSION_ADDIN, BROKER, AGENT_SERVICE]:
    text = read(rel)
    truthy(f"{Path(rel).name} checks the Host header",
           "ALLOWED_HOSTS" in text or "allowed_hosts" in text)
    truthy(f"{Path(rel).name} accepts only loopback names",
           '127.0.0.1:' in text and 'localhost:' in text)

# The chat service applies it as middleware rather than per handler, because a
# per-handler check is one new route away from being incomplete.
truthy("the chat service pins Host as middleware",
       "middlewares=[pin_host]" in read(AGENT_SERVICE))

# Now that the service refuses a mismatched Host, the URL the palette is
# pointed at is load-bearing: the browser derives the Host header from it. Move
# it to a hostname the service does not list and the panel 403s itself.
CHAT_PANEL = "server/src/arges_mcp/addin/FusionBridge/chat_panel.py"
service_url = re.search(r'SERVICE_URL\s*=\s*"(http://[^"%]+)', read(CHAT_PANEL))
truthy("the palette declares its service URL", service_url)
if service_url:
    truthy("and it uses a host the service allows",
           service_url.group(1).startswith(("http://127.0.0.1", "http://localhost")))


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
