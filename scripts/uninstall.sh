#!/usr/bin/env bash
#
# fusion-mcp uninstaller.
#
#   scripts/uninstall.sh           remove add-in link + MCP registration, ask about ~/.fusion-mcp
#   scripts/uninstall.sh --purge   also delete ~/.fusion-mcp without asking
#
# Exported models in ~/Documents/fusion-mcp-exports/ are never touched.
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

ADDIN_SOURCE="${REPO_DIR}/addin/FusionBridge"
ADDINS_DIR="${HOME}/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns"
ADDIN_LINK="${ADDINS_DIR}/FusionBridge"

CONFIG_DIR="${HOME}/.fusion-mcp"
EXPORTS_DIR="${HOME}/Documents/fusion-mcp-exports"

MCP_NAME="fusion"

PURGE=0

info() { printf '  %s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }

usage() {
    cat <<'EOF'
Usage: uninstall.sh [--purge]

  --purge      Delete ~/.fusion-mcp (token + logs) without prompting.
  -h, --help   Show this help.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --purge) PURGE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 1 ;;
    esac
    shift
done

# --- add-in link -------------------------------------------------------------

step "Removing the FusionBridge add-in link"

if [ -L "${ADDIN_LINK}" ]; then
    current="$(readlink "${ADDIN_LINK}")"
    rm -f "${ADDIN_LINK}"
    info "removed symlink (was: ${current})"
elif [ -e "${ADDIN_LINK}" ]; then
    info "SKIPPED: ${ADDIN_LINK} is a real folder, not our symlink — remove it yourself if you want it gone"
else
    info "nothing to remove (${ADDIN_LINK} does not exist)"
fi

info "stop the add-in inside Fusion too: Tools → Add-Ins → FusionBridge → Stop"
info "add-in source stays in the repo: ${ADDIN_SOURCE}"

# --- MCP registration --------------------------------------------------------

step "Removing the MCP registration"

if command -v claude >/dev/null 2>&1; then
    if claude mcp get "${MCP_NAME}" >/dev/null 2>&1; then
        claude mcp remove "${MCP_NAME}" >/dev/null 2>&1 || true
        info "removed '${MCP_NAME}' from Claude Code"
    else
        info "'${MCP_NAME}' was not registered"
    fi
else
    info "claude CLI not found — skipping (run 'claude mcp remove ${MCP_NAME}' yourself)"
fi

# --- config dir --------------------------------------------------------------

step "Token and logs (${CONFIG_DIR})"

if [ ! -d "${CONFIG_DIR}" ]; then
    info "nothing to remove"
elif [ "${PURGE}" -eq 1 ]; then
    rm -rf "${CONFIG_DIR}"
    info "deleted ${CONFIG_DIR}"
elif [ -t 0 ]; then
    printf '  Delete %s (token, addin.log, server.log)? [y/N] ' "${CONFIG_DIR}"
    read -r reply
    case "${reply}" in
        [yY]|[yY][eE][sS]) rm -rf "${CONFIG_DIR}"; info "deleted ${CONFIG_DIR}" ;;
        *) info "kept ${CONFIG_DIR}" ;;
    esac
else
    info "kept ${CONFIG_DIR} (not a terminal — re-run with --purge to delete it)"
fi

printf '\nDone. Exports in %s were not touched.\n' "${EXPORTS_DIR}"
