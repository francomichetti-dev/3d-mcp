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

import functools
import hashlib
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
    + list(REPO.glob("scripts/**/*.md")) + list(REPO.glob("skill/**/*.md"))
    + list(REPO.glob("tests/**/*.md"))
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

# The names themselves are stored as digests, not as strings.
#
# The first version of this check spelled them out, which put a collaborator's
# name and the filename of their CAD project back into the repository - inside
# the very test written to keep them out, and after a history rewrite had been
# run to remove them. A guard that has to contain what it forbids is the wrong
# shape. Hashing costs nothing here because these are exact terms, not classes
# of string; the structural patterns below stay as regexes because a private
# host address or an API key has no fixed value to hash.
FORBIDDEN_DIGESTS = {
    "e2f88324a7596528d94d3ab28eb3aaa8",
    "501b8c3f7cc1c285b8f9bb688d65c0f8",
    "e3a041c293621f940a636fbb2be503ee",
    "82838435bd539778a3e16d3224e89bbc",
}


@functools.lru_cache(maxsize=None)
def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()[:32]


@functools.lru_cache(maxsize=None)
def line_is_named(line):
    """Does this one line contain a forbidden name or filename?

    Cached on the line itself, which is what makes the history scan cheap.
    Every blob in the object store is a *version* of a file, so the same line
    is re-scanned once per commit that left it untouched - overwhelmingly the
    common case. Hashing it once instead collapses that: the digest work drops
    to the number of distinct lines the project has ever had, not the number of
    line-instances across all of history.
    """
    # Split on anything that is not a letter or digit. An earlier version
    # kept apostrophes, so a possessive ("<name>'s machine") hashed to a
    # different token and slipped straight through - found by planting it.
    words = re.findall(r"[a-z0-9]+", line.lower())
    shingles = words + [" ".join(pair) for pair in zip(words, words[1:])]
    return any(digest(s) in FORBIDDEN_DIGESTS for s in shingles)


def named_lines(text):
    """Line numbers in `text` carrying a forbidden name."""
    return [num for num, line in enumerate(text.splitlines(), 1)
            if line_is_named(line)]


# Every tracked file, not only the docs. The leak that actually happened was in
# commit messages and markdown, so this check was written for markdown - but
# nothing stops the next one landing in a test fixture or a comment, and the
# scan costs milliseconds either way.
#
# Untracked-but-not-ignored files are scanned too. They are exactly the set
# `git add -A` is about to commit, and a run of this suite before that commit
# is the whole point of the local gate - a new file was invisible here once,
# and the name it carried would only have failed in CI, after landing in
# history. (Found by planting the leak in a NEW file rather than an old one.)
TRACKED = []
for args in (["git", "ls-files"],
             ["git", "ls-files", "-o", "--exclude-standard"]):
    for name in subprocess.run(args, cwd=REPO, capture_output=True,
                               text=True).stdout.split():
        path = REPO / name
        try:
            TRACKED.append((name, path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue      # images and anything else not text
truthy("there are tracked files to scan", len(TRACKED) >= 20)

named = [f"{name}:{num}" for name, text in TRACKED for num in named_lines(text)]
check("no collaborator name or private filename anywhere in the repo", named, [])

# (regex, what it is, a lowercase literal every match must contain, a planted
# example that must be caught).
#
# The last two columns are what keep this honest. The marker makes the scan
# cheap - see private_hits - and the sample proves the regex and the marker
# actually work, because a guard nobody has watched fail is a guard nobody
# should trust. The samples are assembled from fragments at runtime so this
# file never contains the literal shapes it forbids; spelling one out here
# would trip the scan two functions down, which is the same mistake that put a
# collaborator's name back into the repo inside the check written to keep it
# out.
PRIVATE = [
    # Private mesh-VPN hostnames and addresses. These are not part of the
    # project - they came from a development machine and leaked once, which is
    # the whole reason the pattern exists. An earlier version required six hex
    # characters and missed the real hostname, which had five; found by
    # planting the leak rather than by reading the regex.
    (r"[\w-]+\.ts\.net", "a private VPN hostname", ".ts.net",
     "some-box" + ".ts" + ".net"),
    (r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b", "a private VPN address",
     "100.", "100." + "101.4.7"),
    (r"sk-ant-[A-Za-z0-9_-]{20,}", "an API key",
     "sk-ant-", "sk-" + "ant-" + "A" * 24),
    (r"/Users/(?!<)[a-z]+/(?:Documents|Desktop|Downloads)/", "someone's home path",
     "/users/", "/Users/" + "someone" + "/Documents/"),
    # One or two backslashes, because both forms turn up: a path pasted into a
    # doc has single ones, the same path inside a JSON or Python string literal
    # has doubled ones. This required doubled ones only, so for as long as it
    # existed it could not match a Windows path as anybody actually writes it -
    # on a project whose entire Rhino half runs on Windows. Nothing in the tree
    # or in history matches the corrected form; it was simply never doing its
    # job. The planted sample below is why it is not still like that.
    # ...and (?-i:) on the name, because the whole alternation runs case-blind
    # for the drive letter, which quietly turned [A-Z][a-z]+ into "any word".
    # That matched an already-anonymised `C:\Users\the tester\...` in two commit
    # messages - the guard firing on the word "the". Case-sensitive here is the
    # difference between "a capitalised account name" and "a word".
    (r"C:\\{1,2}Users\\{1,2}(?!<)(?-i:[A-Z][a-z]+)", "someone's Windows path",
     "c:\\users", "C:" + "\\Users\\" + "Someone" + "\\Desktop"),
    # A tailnet auth key is a live credential: it enrols a machine onto the
    # network. The setup script that used to live here printed the shape of one
    # as a prompt ("It looks like: tskey-auth-xxxx..."), so the placeholder has
    # to survive while a real key does not - hence the lookahead for a run of
    # x's that reaches the end of the token.
    (r"tskey-[a-z]+-(?!x+(?![\w-]))[\w-]{10,}", "a Tailscale auth key",
     "tskey-", "tskey-" + "auth-" + "k1B2c3D4e5F6g7"),
]

# One alternation instead of six passes, in front of it a plain substring test.
# The regexes are the expensive part - `[\w-]+\.ts\.net` backtracks over every
# run of word characters in the file - and almost no blob contains any of this,
# so the cheap test is the one that should run on almost every blob. It skips
# 459 of the 487 blobs in this repo's history outright, and the whole structural
# scan of every version of every file ever committed costs about a tenth of a
# second. A marker MUST be a literal that every match of its regex contains, or
# the scan will skip a real leak; the planted samples below are what enforces
# that, since a wrong marker makes its own sample undetectable.
MARKERS = tuple(marker for _, _, marker, _ in PRIVATE)
WHAT = {f"g{i}": what for i, (_, what, _, _) in enumerate(PRIVATE)}
PRIVATE_RE = re.compile(
    "|".join(f"(?P<g{i}>{entry[0]})" for i, entry in enumerate(PRIVATE)),
    re.I,
)


def private_hits(text, where):
    """[(what, "location")] for every structural leak in `text`."""
    low = text.lower()
    if not any(marker in low for marker in MARKERS):
        return []           # the overwhelmingly common case, at one memchr
    found = []
    for num, line in enumerate(text.splitlines(), 1):
        for match in PRIVATE_RE.finditer(line):
            found.append((WHAT[match.lastgroup], f"{where}:{num}"))
    return found


for _, what, _, sample in PRIVATE:
    caught = [kind for kind, _ in private_hits(sample, "planted")]
    truthy(f"the guard for {what} catches a planted one", what in caught)

by_kind = {what: [] for _, what, _, _ in PRIVATE}
for name, text in TRACKED:
    for what, where in private_hits(text, name):
        by_kind[what].append(where)
for _, what, _, _ in PRIVATE:
    check(f"no {what} anywhere in the repo", by_kind[what], [])

# ------------------------------------------------------- history ----------
# Everything above scans the files that are checked out. That is not where the
# leak lives: a string deleted from the tree is still in every blob that ever
# contained it, and `git log -p` or GitHub's object view will happily show it.
#
# This was found the hard way. Two things were sitting in pushed history while
# the working tree scanned clean: a collaborator's handle in a .cmd file that
# had since been deleted, and - from this very session - the project filename
# inside the older versions of the privacy check itself, before it moved to
# digests.
#
# This carried a baseline of six known-bad blobs while they waited for a
# history rewrite. The rewrite has run, the blobs are gone, and the exemption
# went with them - so this is now what it should be: no exceptions at all.
print("No personal data in git history")


def history_blobs():
    """Every text blob in the object store, read in one batch.

    One `git cat-file` per blob took long enough to be worth avoiding; the
    batch form streams all of them through a single process.
    """
    listing = subprocess.run(["git", "rev-list", "--objects", "--all"], cwd=REPO,
                             capture_output=True, text=True).stdout.splitlines()
    names = {}
    for entry in listing:
        parts = entry.split(maxsplit=1)
        if len(parts) == 2:
            names[parts[0]] = parts[1]
    if not names:
        return
    proc = subprocess.run(["git", "cat-file", "--batch"], cwd=REPO,
                          input="\n".join(names).encode(), capture_output=True)
    data, at = proc.stdout, 0
    while at < len(data):
        end = data.find(b"\n", at)
        if end == -1:
            break
        header = data[at:end].decode("ascii", "replace").split()
        at = end + 1
        if len(header) != 3 or header[1] != "blob":
            continue
        size = int(header[2])
        body, at = data[at:at + size], at + size + 1
        try:
            yield header[0], names.get(header[0], "?"), body.decode("utf-8")
        except UnicodeDecodeError:
            continue


# A shallow clone has one commit in it, so this whole section would pass
# without checking anything — which is worse than not having it, because the
# build goes green and nobody looks again. CI checks out with fetch-depth: 0
# for this reason; if that is ever dropped, this says so instead of shrugging.
shallow = subprocess.run(["git", "rev-parse", "--is-shallow-repository"], cwd=REPO,
                         capture_output=True, text=True).stdout.strip()
check("the clone is deep enough to scan", shallow, "false")

commit_count = subprocess.run(["git", "rev-list", "--count", "--all"], cwd=REPO,
                              capture_output=True, text=True).stdout.strip()
truthy(f"and has real history to scan (saw {commit_count} commits)",
       commit_count.isdigit() and int(commit_count) > 10)

# Both kinds of leak, over the same single pass. An earlier version ran only
# the name digests here and left the structural patterns - VPN address, API
# key, home path - scanning the working tree alone. That is exactly backwards
# for the thing they guard against: a name is typed once and noticed, while a
# pasted tailnet address or an absolute home path tends to arrive in a commit,
# get tidied out of the tree a day later, and stay in the object store forever.
# The tree scan would then be green while the leak was still one `git log -p`
# away. Found by planting a tailnet address in a file and deleting the file.
history_hits, history_private = [], []
for sha, name, text in history_blobs():
    named = named_lines(text)
    if named:
        history_hits.append(f"{name} ({sha[:8]}):{named[0]}")
    for what, where in private_hits(text, f"{name} ({sha[:8]})"):
        history_private.append(f"{what} at {where}")

check("no new personal data anywhere in git history", history_hits, [])
check("no private address, key or path anywhere in git history",
      history_private, [])

# Commit messages are their own store, and the original leak was in one.
#
# --all, not the current branch: a message is just as public on a pushed side
# branch, and this repo has had work sitting on one for weeks at a time.
messages = subprocess.run(["git", "log", "--all", "--format=%B"], cwd=REPO,
                          capture_output=True, text=True).stdout
message_hits = [line.strip()[:60] for line in messages.splitlines()
                if line_is_named(line)]
check("no personal data in any commit message", message_hits, [])
check("no private address, key or path in any commit message",
      [what for what, _ in private_hits(messages, "message")], [])


# The repo owner's own username is fine - it is the URL everyone clones from.
readme = (REPO / "README.md").read_text(encoding="utf-8")
truthy("the clone URL is present", "github.com/francomichetti-dev/3d-mcp" in readme)


# ------------------------------------------------- claims match reality ----
print("Claims match the code")

# The assertion count is quoted in the README. It goes stale the moment a test
# is added, and a wrong number undermines every other number on the page.
# The panel suite is JavaScript — it drives the panel's own script — so
# the count covers both languages rather than only the Python ones.
suites = sorted(list(REPO.glob("tests/test_*.py"))
                + list(REPO.glob("tests/test_*.js")))
quoted = re.search(r"\*\*(\d+) assertions across (\w+) suites\*\*", readme)
truthy("the README quotes an assertion count", quoted)
if quoted:
    words = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
             "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
             "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20}
    # An unrecognised word used to fall back to 0, which reads as "the README
    # is wrong" when the truth is that this table stopped at ten. Say which.
    said = words.get(quoted.group(2), f"unrecognised number word {quoted.group(2)!r}")
    check("the suite count matches the files on disk", said, len(suites))

# Every suite the README lists in its table must exist, and vice versa.
listed = set(re.findall(r"\| `(test_\w+\.(?:py|js))` \|", readme))
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
    # Run it twice at most. One CI run produced no count at all where every
    # other run on the same commit produced 69, so the nested start is not
    # perfectly reliable - most likely the suite's listener losing a port race
    # against its own earlier run. A genuine mismatch fails both attempts, so
    # the retry costs nothing but removes a flake from a check that is now
    # deliberately loud.
    for attempt in (1, 2):
        run = subprocess.run([sys.executable, str(suite)], cwd=REPO,
                             capture_output=True, text=True, timeout=300)
        got = re.search(r"^(\d+) passed", run.stdout, re.M)
        if got:
            if attempt == 2:
                print(f"  (note: {name} produced no count on the first attempt)")
            break
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


# ------------------------------------------------------------- images ------
# The README is the whole shop window on a public repo, and a broken image is
# worse there than a broken link: GitHub renders a torn icon in the middle of
# the page. Both directions are checked, because an image nobody references is
# just weight in the clone.
print("Images")
IMAGE_DIR = REPO / "docs" / "images"
on_disk = {p.name for p in IMAGE_DIR.glob("*.png")}
referenced = set(re.findall(r'docs/images/([\w.-]+\.png)', readme))

check("every image the README shows exists", referenced - on_disk, set())
check("and every image on disk is shown", on_disk - referenced, set())
truthy("there are images at all", len(on_disk) >= 3)

# Alt text is what a screen reader announces and what shows when the image
# fails to load, so a bare filename or an empty string is no use.
thin_alt = []
for src, attrs in re.findall(r'<img\s+src="(docs/images/[^"]+)"([^>]*)>', readme):
    alt = re.search(r'alt="([^"]*)"', attrs)
    if not alt or len(alt.group(1)) < 25:
        thin_alt.append(src.split("/")[-1])
check("every image has alt text worth reading", thin_alt, [])


# ----------------------------------------------------- knowledge skill -----
# The README calls this "more important than the bridge code", and it is the
# one component that fails SILENTLY: a malformed frontmatter means Claude Code
# never loads the skill, and the only symptom is a model that models slightly
# worse. Nothing else in the repo would notice.
print("The knowledge skill loads")
SKILL_DIR = REPO / "skill" / "fusion-360"
skill_md = SKILL_DIR / "SKILL.md"
truthy("SKILL.md exists", skill_md.exists())

skill_text = skill_md.read_text(encoding="utf-8") if skill_md.exists() else ""
front = re.match(r"^---\n(.*?)\n---\n", skill_text, re.S)
truthy("it opens with YAML frontmatter", front)
if front:
    fields = dict(re.findall(r"^(\w+):\s*(.+)$", front.group(1), re.M))
    # The name is how the skill is addressed; a mismatch with the directory is
    # the kind of thing that looks fine and simply never loads.
    check("the skill names itself after its directory",
          fields.get("name"), SKILL_DIR.name)
    truthy("it has a description", len(fields.get("description", "")) > 40)
    # The description is the ONLY thing Claude sees when deciding whether to
    # load it, so it has to name the tools it is about.
    for tool in ("fusion_execute", "fusion_screenshot", "fusion_state",
                 "fusion_export", "fusion_download", "fusion_save"):
        truthy(f"the description mentions {tool}", tool in fields.get("description", ""))

# The reference files are cited in backticks, not as markdown links, so the
# link checker above cannot see them. Both directions matter: a citation with
# no file sends Claude to read nothing, and a file nothing cites never loads.
cited = set(re.findall(r"`references/(\w+\.md)`", skill_text))
on_disk = {p.name for p in (SKILL_DIR / "references").glob("*.md")}
check("every reference the skill cites exists", cited - on_disk, set())
check("and every reference file is cited", on_disk - cited, set())
truthy("there are references to check", len(on_disk) >= 3)

# The README lists them by name in a brace expansion and quotes the skill's
# size; both go stale the moment a reference is added or SKILL.md grows.
listed_refs = re.search(r"`references/\{([\w,]+)\}\.md`", readme)
truthy("the README lists the reference files", listed_refs)
if listed_refs:
    check("and the list matches what is on disk",
          {n + ".md" for n in listed_refs.group(1).split(",")}, on_disk)

quoted_kb = re.search(r"`SKILL\.md`\s*\(~(\d+)\s*KB\)", readme)
truthy("the README quotes the skill's size", quoted_kb)
if quoted_kb and skill_md.exists():
    actual_kb = skill_md.stat().st_size / 1024
    check("and it is within a kilobyte of the real one",
          abs(actual_kb - int(quoted_kb.group(1))) < 1.0, True)


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
