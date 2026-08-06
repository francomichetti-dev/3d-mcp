---
description: Open the NIBBLER chat panel inside Fusion 360
argument-hint: "[--stop | --status]"
allowed-tools: Bash(__REPO__/scripts/nibbler.sh:*)
---

Run `__REPO__/scripts/nibbler.sh $ARGUMENTS` once and report the result.

The script is idempotent: it starts the agent service if it isn't already up
and opens (or focuses) the docked panel in Fusion. Do not run it more than once
per invocation, and do not try to work around a failure by starting the service
by hand — the script's error text already says what to fix.

Report in one or two lines:
- On success: that the panel is open, and whether the agent is ready or still
  connecting.
- On failure: the script's own error message verbatim. The two common causes
  are Fusion not running (or the FusionBridge add-in not started) and a missing
  install — both are named explicitly in the output.

Do not offer to model anything yourself afterwards. The point of the panel is
that the user types into it directly; the CAD conversation happens there, not
in this session.
