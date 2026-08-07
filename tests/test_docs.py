"""The documentation makes checkable claims. This checks them.

Docs rot silently. A link breaks when a file moves, an assertion count goes
stale the moment a test is added, a command in the README stops matching the
script it describes — and none of that fails a build, it just quietly misleads
whoever is reading.

The privacy section exists because it already happened: a collaborator's name
and the filename of their CAD project were committed, and were only found by
looking. That took a history rewrite to undo. This makes the next one fail a
test instead.

    python3 tests/test_docs.py
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


MARKDOWN = sorted(
    p for p in list(REPO.glob("*.md")) + list(REPO.glob("docs/*.md"))
    + list(REPO.glob("scripts/**/*.md"))
    if ".git" not in p.parts
)


# ------------------------------------------------------------------ links --
# A moved file leaves a link pointing nowhere, and the person who follows it is
# the one who finds out.
print("Links resolve")
anchors = set()
for path in MARKDOWN:
    for head in re.findall(r"^#{1,6} (.+)$", path.read_text(encoding="utf-8"), re.M):
        slug = re.sub(r"[^a-z0-9 -]", "", head.lower()).replace(" ", "-")
        anchors.add((path.name, slug))

broken_files, broken_anchors = [], []
for path in MARKDOWN:
    text = path.read_text(encoding="utf-8")
    for target in re.findall(r"\]\(((?!https?:)(?!mailto:)[^)]+)\)", text):
        file_part, _, anchor = target.partition("#")
        if file_part:
            resolved = (path.parent / file_part).resolve()
            if not resolved.exists() and not (REPO / file_part).exists():
                broken_files.append(f"{path.name} -> {target}")
        elif anchor and (path.name, anchor) not in anchors:
            broken_anchors.append(f"{path.name} -> #{anchor}")

check("no link points at a missing file", broken_files, [])
check("no anchor points at a missing heading", broken_anchors, [])
truthy("there are links to check at all", len(MARKDOWN) >= 5)


# ------------------------------------------------------- privacy ----------
# A collaborator's name and their project filename were committed once. The
# cost was a history rewrite; the cost of catching it here is nothing.
print("No personal data")
PRIVATE = [
    (r"\bocta[vw]io\b", "a collaborator's name"),
    (r"<a project file>", "a collaborator's project file"),
    # Any *.ts.net host is a Tailscale machine. An earlier version of this
    # pattern required six hex characters and missed the real hostname, which
    # has five - found by planting the leak rather than by reading it.
    (r"[\w-]+\.ts\.net", "a Tailscale machine address"),
    (r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b", "a Tailscale IP"),
    (r"sk-ant-[A-Za-z0-9_-]{20,}", "an API key"),
    (r"/Users/(?!<)[a-z]+/(?:Documents|Desktop|Downloads)/", "someone's home path"),
    (r"C:\\\\Users\\\\(?!<)[A-Z][a-z]+\\\\", "someone's Windows path"),
]
for pattern, what in PRIVATE:
    hits = []
    for path in MARKDOWN:
        for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(pattern, line, re.I):
                hits.append(f"{path.relative_to(REPO)}:{num}")
    check(f"no {what} in the docs", hits, [])

# The repo owner's own username is fine - it is the URL everyone clones from.
readme = (REPO / "README.md").read_text(encoding="utf-8")
truthy("the clone URL is present", "github.com/francomichetti-dev/3d-mcp" in readme)


# ------------------------------------------------- claims match reality ----
print("Claims match the code")

# The assertion count is quoted in the README. It goes stale the moment a test
# is added, and a wrong number undermines every other number on the page.
suites = sorted(REPO.glob("tests/test_*.py"))
quoted = re.search(r"\*\*(\d+) assertions across (\w+) suites\*\*", readme)
truthy("the README quotes an assertion count", quoted)
if quoted:
    words = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10}
    said = words.get(quoted.group(2), 0)
    check("the suite count matches the files on disk", said, len(suites))

# Every suite the README lists in its table must exist, and vice versa.
listed = set(re.findall(r"\| `(test_\w+\.py)` \|", readme))
actual = {p.name for p in suites}
check("the README lists every suite", listed, actual)

# The count is quoted in more than one document, and only the README's was
# checked - so a handover doc sat at 375 long after the real number was 402.
# Any doc that states a whole-suite total must agree with the README's.
if quoted:
    disagreeing = []
    for path in MARKDOWN:
        for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # Only whole-suite claims; per-file claims are checked below.
            if re.search(r"`tests/test_\w+\.py`", line):
                continue
            for said in re.findall(r"(?:are|There are|all)\s+(\d+)\s+assertions", line):
                if said != quoted.group(1):
                    disagreeing.append(f"{path.relative_to(REPO)}:{num} says {said}")
    check("every doc quoting the total agrees with the README", disagreeing, [])

# A claim about ONE suite can be checked exactly, by running it. This is the
# only count in the docs that is verified rather than cross-referenced.
per_file = []
for path in MARKDOWN:
    text = path.read_text(encoding="utf-8")
    per_file += re.findall(r"(\d+) assertions in `tests/(test_\w+\.py)`", text)

for said, name in per_file:
    suite = REPO / "tests" / name
    if not suite.exists():
        check(f"{name} exists to be counted", False, True)
        continue
    if name == Path(__file__).name:          # would recurse
        continue
    run = subprocess.run([sys.executable, str(suite)], cwd=REPO,
                         capture_output=True, text=True, timeout=300)
    got = re.search(r"^(\d+) passed", run.stdout, re.M)
    # Exactly one assertion whether or not the suite could be run. An earlier
    # version skipped silently when it could not, which made this file's own
    # assertion count vary by environment - CI's macOS run came out one short
    # of the local one and the runner's equality check rejected the README.
    # A count check must not itself be uncountable.
    if got:
        actual = got.group(1)
    else:
        tail = (run.stderr or run.stdout or "").strip().splitlines()[-3:]
        actual = f"could not run {name}: {' | '.join(tail) or 'no output'}"
    check(f"the docs' count for {name} matches running it", actual, said)

# Commands the README tells people to run must exist.
for command in re.findall(r"`(scripts/[\w/.-]+\.(?:sh|ps1|cmd|py))`", readme):
    truthy(f"{command} exists", (REPO / command).exists())

# The Rhino setup instruction points at a real file.
if "SETUP.md" in readme:
    truthy("SETUP.md exists where the README says",
           (REPO / "scripts" / "rhino" / "SETUP.md").exists())


# --------------------------------------------------- security doc ---------
# A security document that omits half the system implies coverage it does not
# have, which is worse than saying nothing.
print("The security doc covers both halves")
security = (REPO / "SECURITY.md").read_text(encoding="utf-8")
for term, why in [("fusion_execute", "the Fusion execute tool"),
                  ("rhino_execute", "the Rhino execute tool"),
                  ("127.0.0.1", "the loopback binding"),
                  ("token", "the shared secret"),
                  ("icacls", "Windows permissions, where chmod does nothing")]:
    truthy(f"it mentions {why}", term.lower() in security.lower())

# The asymmetry between the two CADs is the thing a user most needs to know.
truthy("it states that Rhino has no destructive gate",
       "no such gate" in security.lower() or "stops for nothing" in security.lower())


# ------------------------------------------------------------ freshness ----
print("Nothing points at deleted files")
tracked = set(subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True,
                             text=True).stdout.split())
# reference-notes.md documents the layout of OTHER projects for comparison, so
# its paths are deliberately not ours and must not be checked against this repo.
DESCRIBES_OTHERS = {"reference-notes.md"}

stale = []
for path in MARKDOWN:
    if path.name in DESCRIBES_OTHERS:
        continue
    for ref in re.findall(r"`((?:scripts|server|agent|tests)/[\w/.-]+)`",
                          path.read_text(encoding="utf-8")):
        # A log is written at runtime, so it is absent from a fresh checkout by
        # definition - pointing someone at one is correct, not stale.
        if ref.endswith(".log"):
            continue
        if ref not in tracked and not (REPO / ref).exists():
            stale.append(f"{path.relative_to(REPO)} -> {ref}")
check("no doc references a path that no longer exists", stale, [])

# The reverse of a broken link, and just as bad: a document nothing points at.
# Two accumulated here unnoticed - one of them a superseded plan whose code
# sketches would crash Rhino if anyone found and followed them.
print("Nothing is orphaned")
orphans = []
for path in sorted(REPO.glob("docs/*.md")):
    others = [p for p in MARKDOWN if p != path]
    if not any(path.name in p.read_text(encoding="utf-8") for p in others):
        orphans.append(str(path.relative_to(REPO)))
check("every doc under docs/ is linked from somewhere", orphans, [])

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
