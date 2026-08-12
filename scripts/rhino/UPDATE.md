# Updating an existing install — instructions for Claude Code

You are Claude Code on the Windows machine where the Rhino bridge is already
installed and working. The folder this file sits in is the NEW version,
arriving as a zip. Your job is to replace the old install with it without
losing anything the person has set up. Do the work yourself; do not hand the
person commands unless a step genuinely needs them (closing Rhino, approving
a cleanup).

## What must survive the update

- `%USERPROFILE%\.fusion-mcp\` — the token and `rhino-chat.json` settings.
  Do NOT regenerate the token: the broker and the poller share it, and a new
  one would orphan whichever side you did not restart.

  **This folder keeps its old name, and that is correct.** The project was
  renamed to `arges`, and a fresh install now uses `%USERPROFILE%\.arges\`.
  Every part of the new code looks for `.arges` first and falls back to
  `.fusion-mcp` when that is the only one present, so this machine keeps
  working untouched. The one thing that migrates is `arges install`, which is
  the macOS Fusion installer and does not run here. **Do not move or rename
  this folder** to tidy it up: nothing requires it, and doing it while the
  broker is running separates the two halves from their shared token.
- The conversation history and any exports. Nothing here touches them; keep it
  that way.

## Steps, in order

1. **Find the old install.** The broker runs as a scheduled task — read its
   action path (`schtasks /query /tn <task> /v`, or search for
   `broker-service.ps1`). That path's folder is the old `scripts/rhino`.
   `START-BROKER.cmd` on the Desktop, if present, points at the same place.

2. **Stop what is running, gently.**
   - Chat window: ask the person to close it if it is open.
   - Poller: run `rhino-poller-stop.py` through `rhinocode` if Rhino is open
     (remember: `ScriptEditor` must have been run once this Rhino session
     before `rhinocode` can see the instance). If Rhino is closed, the poller
     is already stopped.
   - Broker: end the scheduled task (`schtasks /end /tn <task>`).

3. **Replace the files.** Copy everything from this folder over the old one,
   same location, so the scheduled task's path stays valid. Overwrite; do not
   merge by hand.

4. **Re-apply the token permissions — this matters.** The old installer used
   `os.chmod(0o600)`, which restricts nothing on Windows (it only toggles the
   read-only attribute). The new code does it properly, but the EXISTING
   files need it once:

       icacls "%USERPROFILE%\.fusion-mcp" /inheritance:r /grant:r "%USERNAME%":(OI)(CI)F
       icacls "%USERPROFILE%\.fusion-mcp\token" /inheritance:r /grant:r "%USERNAME%":F

   The flags differ on purpose: `(OI)(CI)` is what a FOLDER passes to its
   children, and on a plain FILE it produces an ACL with no usable grantee —
   icacls still reports success, and the next write to that file fails with
   PermissionError. Directory gets the flags, file does not.

5. **Start everything again.** Broker task first; then, with Rhino open and
   `ScriptEditor` run once, the poller; then the chat window
   (`RHINO-CHAT.cmd`).

6. **Verify before saying it works.**
   - The window's dot is green and says Rhino is connected.
   - A trivial prompt round-trips ("what's in this document?").
   - The model dropdown lists five entries including **Fable 5**, and picking
     one confirms itself in the chat.
   - During a turn, an orange **Stop** appears beside the spinner; afterwards
     the banner reads a green **Done**.

7. **One-time housekeeping, agreed with the repo owner:** run
   `cleanup-downloads.ps1` (in this folder) against the Downloads folder.
   List what it would remove and get a yes from the person at the machine
   BEFORE running it — it deletes files, and agreement from another machine
   is not consent from this one.

## New in this version: memory, video, and cost

The window now keeps **one conversation per project**, where Rhino's
incremental saves are the same project — `chair.3dm`, `chair001.3dm`,
`chair_v2.3dm` and `chair - Copy.3dm` all share a thread. The first project
opened inherits the old single global conversation, so nothing said before is
lost. Claude can save notes with `rhino_remember` and look them up with
`rhino_recall`; the sidebar lists every project with what it has cost so far.

Videos can be attached: six frames spread across the clip, plus a local
transcript of anything spoken. **Both are optional** — see the table in
`SETUP.md`. Without ffmpeg, videos are refused with a message naming the
install command and everything else works exactly as before; without
`faster-whisper`, frames still attach and only the transcript is missing.

Memory lives in `%USERPROFILE%\.fusion-mcp\memory\` on this machine (see
above — the folder keeps its old name). It is restricted to the
account like the token is, and it holds project names, file paths and notes —
if the person shares that machine, it is worth telling them it exists.

## What changed since the version you are replacing

Worth telling the person, briefly: model and effort selection in the window
(including Fable 5); Stop now sits beside the working spinner and a cancelled
turn reads "Stopped" instead of pretending it finished; the dropdowns match
the dark theme instead of opening white; `rhino_execute` can return a
screenshot with the result in one call, so build steps are faster; the
settings confirmation actually appears now; the dropdowns no longer duplicate
after visiting Settings; and the token file is genuinely restricted on
Windows.

## Unchanged rules

Everything in `SETUP.md` and `docs/` still holds: loopback only, never expose
a port, never delete geometry by walking the document, and read
`rhino-poller.log` when something looks silent — `rhinocode` prints nothing,
ever.
