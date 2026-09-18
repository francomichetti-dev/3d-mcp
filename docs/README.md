# Docs

Background that does not belong in the README: how the Rhino side was arrived
at, what was tried and failed, and where the design ideas came from.

Most of this is a record of dead ends. That is deliberate — nearly every sharp
edge in this project was found by hitting it, and none of them were predictable
from the vendor documentation.

| | |
| --- | --- |
| [rhino-handover.md](rhino-handover.md) | what Claude Code needs to know to work on the Rhino half — the traps, the never-do list, and how to get a change back upstream. The copy that ships to a Rhino machine. |
| [rhino-windows-notes.md](rhino-windows-notes.md) | findings from the live Rhino 8 / Windows 11 spike: units, `RhinoCode.exe`, the PATH a non-interactive session gets, and the viewport-capture rule that inverts depending on which thread you are on. |
| [rhino-dead-ends.md](rhino-dead-ends.md) | the four architectures tried before the one that worked, and what disproved each. Read this before proposing a listener or a thread inside Rhino. |
| [troubleshooting.md](troubleshooting.md) | what each log is for, the symptom table, timeouts, reloading the add-in after an edit, and the duplicate-registration case. |
| [reference-notes.md](reference-notes.md) | read-only study of two existing Fusion MCP projects, done before any code was written. Mechanisms and design ideas only — **no code was copied**, and nothing third-party runs inside the CAD. |

The paths in `reference-notes.md` describe *other* projects' layouts, not this
one; `tests/test_docs.py` excludes it from the stale-path check for that reason.

## Screenshots

`images/` holds the README's screenshots. They are viewport captures taken
through the bridge itself.
