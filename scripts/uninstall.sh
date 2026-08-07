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

# set -u catches an unset HOME but not an empty one — and an empty HOME would
# collapse every path below to the filesystem root (rm -rf /.fusion-mcp, and an
# ADDIN_LINK under /Library outside this user's account).
: "${HOME:?HOME must be set to a non-empty path}"
[ -d "${HOME}" ] || { printf 'ERROR: HOME (%s) is not a directory\n' "${HOME}" >&2; exit 1; }

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

ADDIN_SOURCE="${REPO_DIR}/server/src/arges_mcp/addin/Arges"
ADDINS_DIR="${HOME}/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns"
ADDIN_LINK="${ADDINS_DIR}/Arges"

SKILL_LINK="${HOME}/.claude/skills/fusion-360"

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

step "Removing the Arges add-in link"

if [ -L "${ADDIN_LINK}" ]; then
    # The old layout: the whole folder was one symlink.
    current="$(readlink "${ADDIN_LINK}")"
    rm -f "${ADDIN_LINK}"
    info "removed symlink (was: ${current})"
elif [ -d "${ADDIN_LINK}" ]; then
    # The current layout: a real folder of per-file symlinks into the checkout.
    # Removed only if EVERY entry is such a link — anything else means a real
    # add-in is living there and it is not ours to delete.
    ours=1
    found=0
    for entry in "${ADDIN_LINK}"/* "${ADDIN_LINK}"/.[!.]*; do
        [ -e "${entry}" ] || [ -L "${entry}" ] || continue
        found=1
        if [ ! -L "${entry}" ]; then ours=0; break; fi
        case "$(readlink "${entry}")" in
            "${ADDIN_SOURCE}"/*) ;;
            *) ours=0; break ;;
        esac
    done
    if [ "${ours}" -eq 1 ] && [ "${found}" -eq 1 ]; then
        rm -rf "${ADDIN_LINK}"
        info "removed ${ADDIN_LINK} (links into this checkout)"
    else
        info "SKIPPED: ${ADDIN_LINK} holds files that are not ours — remove it yourself if you want it gone"
    fi
elif [ -e "${ADDIN_LINK}" ]; then
    info "SKIPPED: ${ADDIN_LINK} is not a folder we recognise — remove it yourself if you want it gone"
else
    info "nothing to remove (${ADDIN_LINK} does not exist)"
fi

info "stop the add-in inside Fusion too: Tools → Add-Ins → Arges → Stop"
info "add-in source stays in the repo: ${ADDIN_SOURCE}"

# --- knowledge skill ---------------------------------------------------------

step "Removing the Fusion knowledge skill link"

if [ -L "${SKILL_LINK}" ]; then
    current="$(readlink "${SKILL_LINK}")"
    rm -f "${SKILL_LINK}"
    info "removed symlink (was: ${current})"
elif [ -e "${SKILL_LINK}" ]; then
    info "SKIPPED: ${SKILL_LINK} is a real folder, not our symlink — remove it yourself if you want it gone"
else
    info "nothing to remove (${SKILL_LINK} does not exist)"
fi

# --- /fusion-chat slash command ----------------------------------------------

step "Removing the /fusion-chat slash command"

COMMAND_FILE="${HOME}/.claude/commands/fusion-chat.md"

if [ -L "${COMMAND_FILE}" ]; then
    info "SKIPPED: ${COMMAND_FILE} is a symlink — not ours, remove it yourself"
elif [ -f "${COMMAND_FILE}" ]; then
    # install.sh generates this file with our checkout's path baked in. Only
    # delete a file that still points at this repo, so a hand-written command
    # of the same name survives.
    if grep -qF "${REPO_DIR}/scripts/fusion-chat.sh" "${COMMAND_FILE}" 2>/dev/null; then
        rm -f "${COMMAND_FILE}"
        info "removed ${COMMAND_FILE}"
    else
        info "SKIPPED: ${COMMAND_FILE} does not point at ${REPO_DIR} — left alone"
    fi
else
    info "nothing to remove (${COMMAND_FILE} does not exist)"
fi

# --- agent service -----------------------------------------------------------

step "Stopping the chat agent service"

if pkill -f "agent_service.py" >/dev/null 2>&1; then
    info "stopped"
else
    info "was not running"
fi

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

# Belt and braces around the only destructive command in this script: refuse to
# recurse-delete anything that is not strictly below $HOME.
purge_config_dir() {
    case "${CONFIG_DIR}" in
        "${HOME}"/?*) ;;
        *) printf '\nERROR: refusing to delete %s — not inside %s\n' "${CONFIG_DIR}" "${HOME}" >&2
           exit 1 ;;
    esac
    rm -rf "${CONFIG_DIR}"
}

step "Token, logs and saved chats (${CONFIG_DIR})"

if [ ! -d "${CONFIG_DIR}" ]; then
    info "nothing to remove"
elif [ "${PURGE}" -eq 1 ]; then
    purge_config_dir
    info "deleted ${CONFIG_DIR}"
elif [ -t 0 ]; then
    printf '  Delete %s (token, logs, and every design chat)? [y/N] ' "${CONFIG_DIR}"
    read -r reply
    case "${reply}" in
        [yY]|[yY][eE][sS]) purge_config_dir; info "deleted ${CONFIG_DIR}" ;;
        *) info "kept ${CONFIG_DIR}" ;;
    esac
else
    info "kept ${CONFIG_DIR} (not a terminal — re-run with --purge to delete it)"
fi

printf '\nDone. Exports in %s were not touched.\n' "${EXPORTS_DIR}"
