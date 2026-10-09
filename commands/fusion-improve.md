---
description: Turn what the Fusion tool learned in use into improvements to the tool itself
argument-hint: "[--since YYYY-MM-DD]"
allowed-tools: Bash(uv run --frozen --no-sync --directory __REPO__/server arges experience:*)
---

Run this once and read its JSON:

    uv run --frozen --no-sync --directory __REPO__/server arges experience --json $ARGUMENTS

It summarises the experience log the Fusion MCP keeps by itself: every call's
outcome, recurring errors and whether a lesson covers them, how often each
lesson and fast command was used, helpers that keep being retyped, and the
slow paths. It holds names, counts and error signatures, never code.

Your job is to turn that into changes to this repository (`__REPO__`), so the
tool improves for everyone who installs it, not only on this machine. Propose
at most seven, most valuable first (what would save the most failed calls or
seconds next session), each as one short paragraph with the file it touches:

1. **A recurring error with no lesson** (`errors.unlearned`): what the lesson
   should say and where the fix belongs. Usually that is a line in
   `skill/fusion-360/references/gotchas.md`, which every session reads, rather
   than a lesson that only this install has. If the signature alone does not
   show the fix, say so instead of guessing.
2. **A lesson confirmed twice or more that is general**, not about one
   design: promote it into `references/gotchas.md` (quote the line to add).
3. **A learned fast command run often without failures**: promote it into
   `server/src/arges_mcp/fastcommands/builtin/` with a stand-in test in
   `tests/test_fastcommands.py`. Its source is in the state directory under
   `commands/<name>.py`; ask before reading it.
4. **A helper retyped three or more times** (`helpers`): a fast command to
   write, with its name and usage.
5. **A slow tool or command** (`slow`): where in `server/src/arges_mcp/` the
   time probably goes, and what measurement would confirm it.
6. **Recipes** stay recipes unless one is general enough for the skill.

Change nothing until the person picks which to apply. Then apply only those,
test-first, run `__REPO__/tests/run.sh`, and do not commit unless asked.

Do not read `experience.jsonl` itself: its code excerpts are the person's
design work. If a proposal needs one, say which and ask.

If the report says nothing has been recorded yet, say that in one line and
stop.
