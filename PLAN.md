# Plan: a tool that gets better with use

Each modelling session should leave the next one faster and less error-prone.
Today it forgets everything: `server.log` holds 538 `fusion_execute` calls with
their sizes and timings and not one outcome, so nobody can tell which calls
failed, why, or what fixed them. The same helpers get retyped from scratch every
session. In the Claude Code transcripts on the development machine, a
`set user parameter` helper was written six times and the camera-placement lines
eight times, and none of that work carried over to the next session.

Three mechanisms, in order of dependency:

1. **Experience log**: the server records every call (outcome, error
   signature, timing, the helpers it defined). The server writes it, so the
   model does not have to remember to.
2. **Lessons**: the fix for an error is recorded once and comes back
   automatically the next time the same error appears, in any session.
3. **Fast commands**: proven code is saved under a name and run in one call
   (`fusion_run("bbox")`) instead of being retyped. A few ship built in. The
   model adds more when the log shows it keeps writing the same helper.

On top of these sits a report (`arges experience`) and a Claude Code command
that turns what accumulated into changes to the skill and the built-ins. That
is how the MCP itself improves, not only one person's install.

Everything stays local: `~/.arges/`, 0600, no network, `ARGES_EXPERIENCE=0`
turns the log off.

## Design

- `server/src/arges_mcp/experience.py`: standard library only, so the chat
  service can import it as it already imports `common.py`.
  - pure: `error_signature`, `function_fingerprints`, `summarize`
  - storage: `ExperienceLog` (append-only JSONL, rotated)
- `server/src/arges_mcp/lessons.py`: `Lessons`, one JSON file written
  atomically under a lock, since several MCP server processes share it.
  Recall is by exact error signature only: a looser match would bring back
  lessons about other errors, and a misleading lesson is worse than none.
- `server/src/arges_mcp/fastcommands/`: the command library. Built-ins ship in
  `builtin/`, and learned commands live in `~/.arges/commands/`, in the same
  format: one `.py` file per command, a docstring, and `def run(p)`. A command
  runs as data inside the snippet mechanism `common.snippet` already uses, so
  arguments are never interpolated into source.
- `server.py` only wires these in. No new logic lives in the tool functions.

## Checklist

Each step: failing test first, then code, then docs. The full suite stays
green. Stop and ask at every gate.

- [x] **0. Review**: findings reported in the session. The live registration
      fix (re-run `scripts/install.sh`) is outside the repo.
- [x] **1. Experience log**: `experience.py` (signature, fingerprints, log),
      `fusion_execute` records every call, new suite `tests/test_experience.py`,
      README and SECURITY.md say what is stored.
      **Gate.**
- [x] **2. Lessons**: `fusion_learn` tool; a failing call returns the lessons
      recorded for its error signature, or a one-line nudge to record one once
      fixed; `fusion_state` carries the most-confirmed general lessons. Skill
      section on when to record a lesson. **Gate.**
- [x] **3. Fast commands**: `fusion_run`, `fusion_command_save`, the
      library, and built-ins drawn from the mined repeats (`bbox`, `inspect`,
      `params`, `sketch_axes`, `profiles`). Read-only commands skip the autosave.
      Built-ins verified live against Fusion on read-only calls. **Gate.**
- [x] **3b. Found in use, fixed out of order** (2026-10-08):
      - the model picker lists what the installed CLI offers, and the
        panel runs that CLI rather than the SDK's frozen copy
      - saving never overwrites a file this tool did not write (the
        autosave had written an empty "Untitled" design over a downloaded
        Untitled.f3d)
      - `fusion_import` finds a CAD file by name in the usual folders and
        imports it in one call
- [x] **3b'. Replay test** (2026-10-08): the session that lost the motor,
      re-run through the live panel in a scratch design. 80 s, 11 tool calls,
      no Bash, and no file in Downloads changed (the save went to
      Untitled-1.f3d). The original session took over ten minutes and
      overwrote the motor. The replay also found two gaps, both fixed and
      verified live: open tabs are now listed with their body names and size,
      so the motor tab is recognisable; and copied bodies keep their names.
- [x] **3b''. Codex review** (2026-10-08): a read-only Codex session
      (gpt-6.1-sol) reviewed the branch and reported 11 findings. All held up
      when checked. A Codex lane in its own worktree fixed the two lock races
      (lessons, experience log), and nine were fixed here, each test-first.
      The source-hash gate binding was verified live (`gated=yes`). Follow-up
      from the user's session: a second import of the same source is refused
      unless `allow_duplicate=true`.
- [x] **4b. Learn from successes** (2026-10-08, Franco's request: "record
      success and quick resolution also"): recipes (`fusion_learn(when=...)`)
      matched against each request and attached to it in the panel, or
      returned by `fusion_state(query=...)`; a nudge on a success after two or
      more failures; built-ins `selection` and `remove` (Codex lane), verified
      live. The first real recipe: "delete the one I selected".
- [x] **3c. Import from the panel**: drop a CAD file on the panel and it is
      imported at once, with a line in the chat saying what arrived, and the
      model told on its next turn. **Gate.**
      (2026-10-09) Codex lane, reviewed and live-tested through the deployed
      service in a throwaway document: an STL dropped was in the design in
      0.75 s, measured 36 x 36 x 80.5 mm; dropping it again was refused by the
      duplicate guard with a readable message. Reviewing it found an older
      hole: any open web page could POST to the service with the right Host.
      Foreign origins are now refused (403 live).
- [x] **Second Codex review** (2026-10-09): ten findings on the six commits
      after the first review, all confirmed. Recipes matched unrelated
      requests and negations; recalled notes arrived unmarked inside the
      person's message; `remove` could take every instance of a shared body or
      the wrong one of two same-named occurrences; the duplicate guard missed
      nested imports, renumbered tabs, copies of copies and symlinks; reads
      reset the failure streak. Fixed here and in two Codex lanes, each
      verified live where Fusion was involved. The live probes also found that
      a nested removal's timeline item points at the parent occurrence, so
      `remove` gained `undo` (gotcha 13).
- [x] **4. Repetition hints** (Codex lane `codex/hints`, reviewed and merged): a successful call whose helper function has
      been defined three or more times before gets a one-time hint to save it
      as a fast command. **Gate.**
- [x] **5. Panel parity**: new tools allowed, the destructive gate reads a
      fast command's *source* (not just its name), and `SYSTEM_APPEND` teaches
      the combined execute+screenshot, lessons and fast commands. **Gate.**
      Done along the way in steps 2 to 4b, and checked on 2026-10-09: the
      allowed tools are pinned to the server's by `test_consistency.py`, the
      gate resolves `fusion_run` to its source and stamps its hash
      (`gated=yes` live), and `SYSTEM_APPEND` covers screenshots, lessons,
      recipes, fast commands and `fusion_import`.
- [x] **6. The improvement loop**: `arges experience [--json]` report, plus a
      `/fusion-improve` command that reads it and proposes promotions (lessons
      into `references/gotchas.md`, learned commands into built-ins, slow paths
      in the server), changing nothing without asking. **Gate.**
      (2026-10-09) The report is a Codex lane (`report.py`, pure, no code or
      arguments in it; lesson text is, so it can be promoted). The installer
      now writes every file in `commands/`, so `/fusion-improve` ships the way
      `/fusion-chat` does. First real run: 105 calls over 14 sessions, one
      recurring error already covered by a lesson, and the delete-selection
      recipe proposed for the skill.
- [ ] **7. Later**: the same for the Rhino half (dependency-free, so it needs
      its own copy, pinned by `tests/test_consistency.py`); skip the autosave
      when the design did not change, decided by measured cost from the log.
      (2026-10-09) The autosave skip was decided by measurement, and the answer
      is no. On the real design a fingerprint costs about as much as the save,
      and it cannot see a moved sketch point. See FOLLOWUP.md. Rhino parity is
      still open.
