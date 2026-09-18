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
FUSION_ADDIN = "server/src/arges_mcp/addin/Arges/arges_impl.py"
BROKER = "server/src/arges_mcp/broker.py"
RHINO_MCP = "scripts/rhino/rhino_mcp.py"
RHINO_POLLER = "scripts/rhino/rhino-poller.py"
RHINO_CHAT = "scripts/rhino/rhino-chat.py"

AGENT_SERVICE = "agent/agent_service.py"
FUSION_COMMON = "server/src/arges_mcp/common.py"
ORB_ENGINE = "agent/static/thinking-orb-engine.js"

# The chat service belongs here: it POSTs to the add-in with the same token and
# the same header as everyone else. It was missing only because it used to spell
# the header inline instead of declaring a constant, so the sweep never saw it.
ALL_SPEAKERS = [FUSION_SERVER, FUSION_ADDIN, BROKER, RHINO_MCP, RHINO_POLLER,
                RHINO_CHAT, AGENT_SERVICE]


# ------------------------------------------------------------ auth header --
# A mismatch here means every request is rejected as unauthenticated, with
# nothing in any log saying the header name is the reason.
print("The auth header")


def declared(rel, name):
    """The string a module assigns to `name` at module level.

    Anchored: LEGACY_AUTH_HEADER *contains* AUTH_HEADER, so an unanchored
    search finds whichever happens to come first in the file and would pass
    while reading the wrong constant.
    """
    found = re.search(r'^' + name + r'\s*=\s*["\']([^"\']+)["\']', read(rel), re.M)
    return found.group(1) if found else None


headers = {rel: declared(rel, "AUTH_HEADER") for rel in ALL_SPEAKERS}
headers = {rel: value for rel, value in headers.items() if value}
legacy_headers = {rel: declared(rel, "LEGACY_AUTH_HEADER") for rel in ALL_SPEAKERS}
legacy_headers = {rel: value for rel, value in legacy_headers.items() if value}

check("every component declares one", len(headers), len(ALL_SPEAKERS))
check("and they are all the same", len(set(headers.values())), 1)
check("it is the documented name", set(headers.values()), {"X-Arges-Bridge-Token"})

# The rename survives any upgrade order only because everyone still knows the
# old name: clients send both headers, servers accept either. The pieces are
# upgraded by different commands — the add-in by `arges install`, the MCP server
# with the package, the Rhino half by unzipping — so there is no order to
# assume. Letting this rot away one file at a time reintroduces a 401 that
# reads as a bad token and sends you looking at the wrong thing entirely.
# Removing it is a deliberate later step, once no pre-rename install can remain.
check("every component still knows the pre-rename name",
      len(legacy_headers), len(ALL_SPEAKERS))
check("and agrees on what it was", set(legacy_headers.values()),
      {"X-Fusion-Bridge-Token"})


# ------------------------------------------------------------- token path --
# One half writing the token where the other never reads it fails as "invalid
# token", which sends you looking at the token rather than at the path.
print("Where the token lives")
state_dirs = {rel: declared(rel, "STATE_DIR_NAME") for rel in ALL_SPEAKERS}
state_dirs = {rel: value for rel, value in state_dirs.items() if value}
legacy_dirs = {rel: declared(rel, "LEGACY_STATE_DIR_NAME") for rel in ALL_SPEAKERS}
legacy_dirs = {rel: value for rel, value in legacy_dirs.items() if value}

check("every component declares one", len(state_dirs), len(ALL_SPEAKERS))
check("everyone agrees on the directory", set(state_dirs.values()), {".arges"})

# Same reasoning as the header: every reader falls back to the pre-rename
# directory when it is the only one present, so a machine that has updated one
# half and not the other still finds its token. Only `arges install` migrates.
check("every component still knows the pre-rename one",
      len(legacy_dirs), len(ALL_SPEAKERS))
check("and agrees on what it was", set(legacy_dirs.values()), {".fusion-mcp"})

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


def assignment(text, name):
    """The right-hand side of `name = ...`, however many lines it spans.

    Bracket-counted rather than line-based. These defaults sit behind two
    chained environment lookups now and wrap across lines, which a regex over a
    single line silently stops matching — reporting "no port declared" for a
    file that declares one perfectly well.
    """
    found = re.search(r"^" + name + r"\s*=\s*", text, re.M)
    if not found:
        return ""
    depth, out = 0, []
    for char in text[found.end():]:
        if char == "\n" and depth == 0:
            break
        depth += char in "([{"
        depth -= char in ")]}"
        out.append(char)
    return "".join(out)


def port_of(rel, name):
    text = read(rel)
    # The last literal in the expression is the default: the env lookups in
    # front of it carry no number of their own.
    numbers = re.findall(r"\b(\d{4})\b", assignment(text, name))
    if numbers:
        return int(numbers[-1])
    found = re.search(r'127\.0\.0\.1:(\d{4})', text)
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
check("the MCP server exposes five tools", len(mcp_tools), 5)
# The pairing is the point: a tool the server exposes but the window does not
# allow is invisible from the chat, and the model is told to use it anyway.
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

check("both listeners declare a cap", sorted(caps), ["arges_impl.py", "broker.py"])
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
      ["arges_impl.py", "broker.py"])
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
ADDIN_FILES = ["Arges.py", "Arges.manifest", "arges_impl.py"]
for name in ADDIN_FILES:
    truthy(f"{name} is inside the packaged tree",
           (PACKAGED_ROOT / "addin" / "Arges" / name).is_file())

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


# ------------------------------------------------------------- models -------
# Both chats offer a model dropdown, and the two lists are duplicated because
# the Rhino window runs on Rhino's own Python and cannot import from the
# package. A model present on one side and not the other fails at the moment
# somebody switches — not at review, and not on the machine that changed it.
print("The model lists")


def listed(rel, name):
    block = re.search(name + r" = \[(.*?)\]", read(rel), re.S)
    return re.findall(r'\("([^"]+)",\s*"([^"]+)"\)', block.group(1)) if block else []


def model_ids(rel):
    return listed(rel, "MODELS")


fusion_models = model_ids("agent/agent_service.py")
rhino_models = model_ids(RHINO_CHAT)
truthy("the Fusion panel offers models", fusion_models)
truthy("the Rhino window offers models", rhino_models)
check("and they are the same list, in the same order", fusion_models, rhino_models)
truthy("every id looks like a real model", all(m.startswith("claude-") for m, _ in fusion_models))
truthy("every entry has a label short enough for a dropdown",
       all(0 < len(label) <= 16 for _, label in fusion_models))

# The default is the first entry on both sides, so "best" means the same thing
# in each window.
for rel in ["agent/agent_service.py", RHINO_CHAT]:
    found = re.search(r"DEFAULT_MODEL = MODELS\[0\]\[0\]", read(rel))
    truthy(f"{Path(rel).name} defaults to the first entry", found)

# Rhino passes it per invocation; Fusion bakes it into the session options.
truthy("the Rhino window actually passes --model",
       '"--model", settings["model"]' in read(RHINO_CHAT))
truthy("the Fusion session reads the selection rather than a constant",
       "model=self.registry.model()" in read("agent/agent_service.py"))

# Effort sits beside the model in both windows and drifts the same way.
fusion_efforts = listed("agent/agent_service.py", "EFFORTS")
rhino_efforts = listed(RHINO_CHAT, "EFFORTS")
truthy("both windows offer effort levels", fusion_efforts and rhino_efforts)
check("and the same ones, in the same order", fusion_efforts, rhino_efforts)
# The SDK accepts exactly these five; anything else is refused at connect time,
# which surfaces as a session that will not start rather than as a bad answer.
check("matching the levels the SDK accepts",
      [e for e, _ in fusion_efforts], ["low", "medium", "high", "xhigh", "max"])
for rel in ["agent/agent_service.py", RHINO_CHAT]:
    truthy(f"{Path(rel).name} names an explicit effort default",
           re.search(r'DEFAULT_EFFORT = "(\w+)"', read(rel)))
check("and both default to the same level",
      re.search(r'DEFAULT_EFFORT = "(\w+)"', read("agent/agent_service.py")).group(1),
      re.search(r'DEFAULT_EFFORT = "(\w+)"', read(RHINO_CHAT)).group(1))
truthy("the Rhino window passes --effort",
       '"--effort", settings["effort"]' in read(RHINO_CHAT))
truthy("the Fusion session passes effort too",
       "effort=self.registry.effort()" in read("agent/agent_service.py"))


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
CHAT_PANEL = "server/src/arges_mcp/addin/Arges/chat_panel.py"
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


# ------------------------------------------------------------ spawn names ---
# Every binary a spawn site or a doc names must be one the package installs.
#
# The failure this pins is invisible at the scene of the crime: the rename
# changed the console script to `arges` and the chat agent kept spawning
# `arges-mcp` - registration succeeded, the session started, and the model
# simply had no Fusion tools, reporting "bridge disconnected" while the bridge
# was answering /health. The fix then missed that install.sh REGISTERS the
# same wrong name with Claude Code and both READMEs teach it, so every fresh
# install re-created the exact failure. Nothing held the names to the one
# place that decides them, [project.scripts]; now this does.
print("Spawned binaries exist")

pyproject = read("server/pyproject.toml")
scripts_block = re.search(r"\[project\.scripts\](.*?)(?:\n\[|\Z)", pyproject, re.S)
truthy("pyproject declares console scripts", scripts_block)
SCRIPTS = set(re.findall(r"^([A-Za-z0-9_-]+)\s*=",
                         scripts_block.group(1), re.M)) if scripts_block else set()
package = re.search(r'^name\s*=\s*"([^"]+)"', pyproject, re.M)
truthy("`uvx <package>` can work: a script named after the package exists",
       package and package.group(1) in SCRIPTS)

spawn = re.search(r'"--directory",\s*str\(SERVER_DIR\),\s*"([A-Za-z0-9_-]+)"',
                  read(AGENT_SERVICE))
truthy("the chat agent spawns an installed script",
       spawn and spawn.group(1) in SCRIPTS)

# The sweep: any arges-ish token in the installer or the user-facing docs must
# be an installed script. Module and folder names use underscores or capitals
# (arges_mcp, Arges) and are excluded by construction.
# Directory names are arges-ish too and are not commands. Read from the module
# that declares them rather than listed here, so renaming one cannot leave a
# stale exemption behind that quietly swallows a real broken binary name.
NOT_BINARIES = set(re.findall(
    r'^(?:EXPORT_DIR_NAME|LEGACY_EXPORT_DIR_NAME)\s*=\s*"([^"]+)"',
    read("server/src/arges_mcp/bootstrap.py"), re.M))
truthy("the export directory names are discoverable", NOT_BINARIES)

for rel in ["scripts/install.sh", "README.md", "server/README.md"]:
    tokens = {t for t in re.findall(r"\barges[a-z0-9-]*\b", read(rel))
              if "_" not in t}
    unknown = sorted(tokens - SCRIPTS - NOT_BINARIES)
    check(f"{rel} names only installed binaries", unknown, [])

# ------------------------------------------------ one copy of the export code --
# The MCP server and the chat service both write Fusion files now — the server
# for the model's tool calls, the service for the panel's Save and Download
# buttons — and they are separate processes with separate virtual environments.
# The Fusion-side code is shared through common.py rather than copied, which
# holds only as long as two things stay true.
print("The shared export code")

common_text = read(FUSION_COMMON)
truthy("there is a shared module at all", len(common_text) > 500)

# 1. It must import nothing but the standard library. The chat service's
#    environment has no fastmcp, so a third-party import here does not fail a
#    test — it stops the panel from starting, with a traceback about a package
#    nobody was thinking about.
STDLIB_OK = {"json", "os", "re", "time", "pathlib", "typing", "dataclasses",
             "shutil", "tempfile", "hashlib", "datetime", "math"}
imported = set(re.findall(r"^(?:import|from)\s+([A-Za-z_][\w.]*)", common_text, re.M))
outside = {name for name in imported if name.split(".")[0] not in STDLIB_OK}
check("the shared module imports nothing but the standard library", outside, set())

# 2. Neither half may keep its own copy. A second copy would drift, and the
#    symptom is the panel exporting differently from the model.
truthy("the Fusion-side export code lives in the shared module",
       "createSTLExportOptions" in common_text)
for rel in [FUSION_SERVER, AGENT_SERVICE]:
    text = read(rel)
    name = Path(rel).name
    check(f"{name} keeps no second copy of it",
          "createSTLExportOptions" in text, False)
    truthy(f"{name} imports the shared module", "common" in text)

# 3. The format list is shared too, or the panel offers a format the exporter
#    cannot produce.
check("the MCP server does not redeclare the formats",
      bool(re.search(r'^FORMATS\s*=\s*\("', read(FUSION_SERVER), re.M)), False)
truthy("and the panel asks the service for them",
       "formats" in read("agent/static/index.html"))


# ---------------------------------------------------- the vendored orb engine --
# The banner's orb animation is somebody else's code, vendored as a file rather
# than depended on: the panel has no build step and the service makes no
# outbound calls. Two things have to stay true about that.
print("The vendored orb engine")

engine = read(ORB_ENGINE)
truthy("the engine is vendored in the repo", len(engine) > 5000)

# 1. Its licence travels with it. MIT requires the notice be kept, and a
#    vendored file is exactly where that is easy to lose.
truthy("it carries the MIT notice", "MIT License" in engine)
truthy("and names the copyright holder", "Jakub Antalik" in engine)
truthy("and says where it came from and at what version",
       "Libraries.dev" in engine and "thinking-orbs 0.3.1" in engine)

# 2. Every orb state the service names must be one the engine implements. This
#    is the drift the vendoring invites: upstream renames a state, the file is
#    re-vendored, and the banner quietly falls back to the default forever.
service = read(AGENT_SERVICE)
# Only the mapping's own block — a regex over the whole file collects every
# other dict in it and reports half the module as a missing orb state.
block = re.search(r"^ORB_STATES: dict\[str, str\] = \{(.*?)^\}", service, re.S | re.M)
truthy("the orb mapping is where this test expects it", block)
states = set(re.findall(r':\s*"(\w+)"', block.group(1) if block else ""))
states |= set(re.findall(r'^ORB_(?:DEFAULT|THINKING) = "(\w+)"', service, re.M))
truthy("the service names some orb states", len(states) >= 5)
# The engine is minified, so its states are bare object keys, not quoted
# strings: match the word rather than a spelling of it.
missing = sorted(s for s in states
                 if not re.search(r"\b%s\b" % re.escape(s), engine))
check("every orb state the service names exists in the engine", missing, [])
# And the check has to be able to fail, or it is decoration.
truthy("a state the engine does not have would be caught",
       not re.search(r"\bhovering\b", engine))

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
