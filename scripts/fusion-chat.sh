#!/usr/bin/env bash
#
# Open the Fusion Chat panel inside Fusion.
#
#   scripts/fusion-chat.sh          start the agent service (if needed) and show the panel
#   scripts/fusion-chat.sh --stop   hide the panel and stop the service
#   scripts/fusion-chat.sh --status report what is and isn't running
#
# Idempotent: safe to run repeatedly. Opens the palette through the bridge, so
# it never requires stopping and re-running the add-in.
#
set -euo pipefail

: "${HOME:?HOME must be set to a non-empty path}"
[ -d "${HOME}" ] || { printf 'ERROR: HOME (%s) is not a directory\n' "${HOME}" >&2; exit 1; }

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

AGENT_DIR="${REPO_DIR}/agent"
# ~/.arges, or the pre-rename ~/.fusion-mcp when only that exists.
STATE_DIR="${HOME}/.arges"
if [[ ! -d "${STATE_DIR}" && -d "${HOME}/.fusion-mcp" ]]; then
  STATE_DIR="${HOME}/.fusion-mcp"
fi
TOKEN_FILE="${STATE_DIR}/token"
BRIDGE="http://127.0.0.1:7654"
PORT="${ARGES_CHAT_PORT:-${FUSION_CHAT_PORT:-7655}}"
SERVICE="http://127.0.0.1:${PORT}"
LOG="${STATE_DIR}/agent.log"

PALETTE_ID="FusionChatPalette"

info() { printf '  %s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

service_up() { curl -fsS --max-time 2 "${SERVICE}/health" >/dev/null 2>&1; }

bridge_exec() {
    # $1 = python source. Encodes as JSON so quotes/newlines survive intact.
    local token payload
    token="$(cat "${TOKEN_FILE}")"
    payload="$(TARGET_CODE="$1" python3 -c 'import json,os; print(json.dumps({"code": os.environ["TARGET_CODE"]}))')"
    curl -fsS --max-time 70 \
        -H "X-Arges-Bridge-Token: ${token}" \
        -H "Content-Type: application/json" \
        -d "${payload}" "${BRIDGE}/execute"
}

# Heredocs are read into a variable rather than used inside $( ) — bash
# mis-parses a heredoc within command substitution when the body contains an
# apostrophe, treating it as an opening quote.
show_palette() {
    local code
    IFS= read -r -d '' code <<PY || true
pal = ui.palettes.itemById("${PALETTE_ID}")
if pal is None:
    pal = ui.palettes.add("${PALETTE_ID}", "Fusion Chat", "${SERVICE}/", True, True, True, 420, 620)
    try:
        pal.dockingState = adsk.core.PaletteDockingStates.PaletteDockStateRight
    except Exception:
        pass
else:
    # Re-point an existing palette at the current port and reload it, so a
    # stale error page from an earlier run does not persist.
    try:
        pal.htmlFileURL = "${SERVICE}/"
    except Exception:
        pass
pal.isVisible = True
result = {"visible": pal.isVisible}
PY
    bridge_exec "${code}"
}

hide_palette() {
    local code
    IFS= read -r -d '' code <<PY || true
pal = ui.palettes.itemById("${PALETTE_ID}")
if pal is not None:
    pal.isVisible = False
result = {"hidden": True}
PY
    bridge_exec "${code}" >/dev/null 2>&1 || true
}

# --- subcommands -------------------------------------------------------------

case "${1:-}" in
    --stop)
        step "Stopping Fusion Chat"
        hide_palette
        info "panel hidden"
        if pkill -f "agent_service.py" >/dev/null 2>&1; then
            info "agent service stopped"
        else
            info "agent service was not running"
        fi
        exit 0
        ;;
    --status)
        printf 'bridge:  '; curl -fsS --max-time 2 -H "X-Fusion-Bridge-Token: $(cat "${TOKEN_FILE}" 2>/dev/null)" \
            "${BRIDGE}/health" 2>/dev/null || printf 'unreachable (Fusion closed or add-in not running)'
        printf '\nagent:   '; curl -fsS --max-time 2 "${SERVICE}/health" 2>/dev/null || printf 'not running'
        printf '\n'
        exit 0
        ;;
    -h|--help)
        sed -n '3,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
esac

# --- preflight ---------------------------------------------------------------

[ -f "${TOKEN_FILE}" ] || die "no bridge token at ${TOKEN_FILE} — run scripts/install.sh first."
[ -d "${AGENT_DIR}" ] || die "no agent/ directory at ${AGENT_DIR}."

if ! curl -fsS --max-time 3 -H "X-Arges-Bridge-Token: $(cat "${TOKEN_FILE}")" "${BRIDGE}/health" >/dev/null 2>&1; then
    die "Fusion's bridge is not answering on ${BRIDGE}.
  Open Fusion, then Utilities → Add-Ins → Arges → Run.
  If it was already running, check ${STATE_DIR}/addin.log."
fi

# --- agent service -----------------------------------------------------------

if service_up; then
    step "Agent service already running on port ${PORT}"
else
    step "Starting the agent service"
    [ -d "${AGENT_DIR}/.venv" ] || die "agent/.venv is missing — run scripts/install.sh first."
    # Detached so it outlives this shell (and the Claude Code session that ran it).
    nohup uv run --frozen --no-sync --directory "${AGENT_DIR}" agent_service.py \
        >>"${LOG}" 2>&1 </dev/null &
    disown || true

    for _ in $(seq 1 40); do
        sleep 1
        service_up && break
    done
    service_up || die "the agent service did not come up within 40s — see ${LOG}"
    info "listening on ${SERVICE}"
fi

# --- panel -------------------------------------------------------------------

step "Opening the panel in Fusion"
show_palette >/dev/null || die "could not open the palette — see ${STATE_DIR}/addin.log"
info "Fusion Chat is docked on the right of the Fusion window."

# The service binds its port before the agent client finishes connecting, so a
# health check taken the instant the port opens still reports agent:false. Give
# it a few seconds rather than telling the user to wait for something that has
# almost certainly already happened.
agent_ready=""
for _ in $(seq 1 10); do
    agent_state="$(curl -fsS --max-time 2 "${SERVICE}/health" 2>/dev/null || echo '{}')"
    case "${agent_state}" in
        *'"agent": true'*) agent_ready=1; break ;;
    esac
    sleep 1
done

if [ -n "${agent_ready}" ]; then
    info "agent ready — type in the panel to start modelling"
else
    info "agent still connecting — the panel will say when it's ready"
fi
printf '\n'
