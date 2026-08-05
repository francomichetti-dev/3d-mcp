#!/usr/bin/env bash
#
# fusion-mcp installer — 100% local, no network calls.
#
#   scripts/install.sh                 install / repair
#   scripts/install.sh --rotate-token  install and replace the bridge token
#
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

ADDIN_SOURCE="${REPO_DIR}/addin/FusionBridge"
ADDINS_DIR="${HOME}/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns"
ADDIN_LINK="${ADDINS_DIR}/FusionBridge"

CONFIG_DIR="${HOME}/.fusion-mcp"
TOKEN_FILE="${CONFIG_DIR}/token"
EXPORTS_DIR="${HOME}/Documents/fusion-mcp-exports"

MCP_NAME="fusion"
BRIDGE_URL="http://127.0.0.1:7654/health"

ROTATE_TOKEN=0

info()  { printf '  %s\n' "$*"; }
step()  { printf '\n==> %s\n' "$*"; }
die()   { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: install.sh [--rotate-token]

  --rotate-token   Generate a new bridge token, replacing the existing one.
                   The add-in re-reads the token file per request, so Fusion
                   does not need restarting.
  -h, --help       Show this help.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --rotate-token) ROTATE_TOKEN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown argument: $1" ;;
    esac
    shift
done

# --- prerequisites -----------------------------------------------------------

step "Checking prerequisites"

command -v python3 >/dev/null 2>&1 || die "python3 not found (needed to generate the token)."
command -v uv      >/dev/null 2>&1 || die "uv not found — install it (https://docs.astral.sh/uv/) and re-run."
command -v claude  >/dev/null 2>&1 || die "claude CLI not found — install Claude Code and re-run."

[ -d "${ADDIN_SOURCE}" ] || die "add-in source missing: ${ADDIN_SOURCE}"
[ -f "${REPO_DIR}/server/mcp_server.py" ] || die "MCP server missing: ${REPO_DIR}/server/mcp_server.py"

if [ ! -d "${ADDINS_DIR}" ]; then
    die "Fusion AddIns directory not found:
  ${ADDINS_DIR}
Install and launch Autodesk Fusion at least once, then re-run this script."
fi

info "repo:    ${REPO_DIR}"
info "add-ins: ${ADDINS_DIR}"

# --- config dir + token ------------------------------------------------------

step "Preparing ${CONFIG_DIR}"

# shellcheck disable=SC2174  # -m covers creation; the chmod covers a pre-existing dir
mkdir -p -m 700 "${CONFIG_DIR}"
chmod 700 "${CONFIG_DIR}"

# Atomic creation: O_CREAT|O_EXCL means two concurrent installs can never
# hand out different tokens, and the file is never briefly world-readable.
new_token() {
    python3 -c 'import os,secrets,sys; fd=os.open(sys.argv[1], os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600); os.write(fd, secrets.token_hex(32).encode()); os.close(fd)' "$1"
}

if [ "${ROTATE_TOKEN}" -eq 1 ]; then
    tmp_token="${TOKEN_FILE}.new.$$"
    rm -f "${tmp_token}"
    new_token "${tmp_token}"
    mv -f "${tmp_token}" "${TOKEN_FILE}"
    info "token rotated (the add-in picks it up on the next request)"
elif [ -e "${TOKEN_FILE}" ]; then
    chmod 600 "${TOKEN_FILE}"
    info "existing token preserved"
else
    new_token "${TOKEN_FILE}"
    info "token created (${TOKEN_FILE}, mode 0600)"
fi

# --- add-in symlink ----------------------------------------------------------

step "Linking the FusionBridge add-in"

if [ -L "${ADDIN_LINK}" ]; then
    current="$(readlink "${ADDIN_LINK}")"
    if [ "${current}" = "${ADDIN_SOURCE}" ]; then
        info "already linked"
    else
        rm -f "${ADDIN_LINK}"
        ln -s "${ADDIN_SOURCE}" "${ADDIN_LINK}"
        info "replaced stale symlink (was: ${current})"
    fi
elif [ -e "${ADDIN_LINK}" ]; then
    die "a real file or folder already exists at:
  ${ADDIN_LINK}
Refusing to delete it. Move it aside, then re-run this script."
else
    ln -s "${ADDIN_SOURCE}" "${ADDIN_LINK}"
    info "linked ${ADDIN_LINK} -> ${ADDIN_SOURCE}"
fi

# --- exports dir -------------------------------------------------------------

step "Preparing the exports directory"
mkdir -p "${EXPORTS_DIR}"
info "${EXPORTS_DIR}"

# --- MCP registration --------------------------------------------------------

step "Registering the MCP server with Claude Code (user scope)"

if claude mcp get "${MCP_NAME}" >/dev/null 2>&1; then
    # No -s: remove from whichever scope holds it, so a stale local-scope entry
    # cannot shadow the user-scope one we are about to add.
    claude mcp remove "${MCP_NAME}" >/dev/null 2>&1 || true
    info "removed previous registration"
fi

claude mcp add "${MCP_NAME}" -s user -- uv run --directory "${REPO_DIR}/server" mcp_server.py
info "registered as '${MCP_NAME}' (absolute path baked in)"

# --- manual step -------------------------------------------------------------

cat <<EOF

Install complete. One manual step is left, inside Fusion:

  Tools → Add-Ins → select FusionBridge → Run
  (auto-starts on later launches — runOnStartup is in the manifest)

Then verify the bridge is answering:

  curl -sS -H "X-Fusion-Bridge-Token: \$(cat ${TOKEN_FILE})" ${BRIDGE_URL}

A healthy bridge replies with JSON containing "ok": true and "bridge_version": "1".
If it does not, check ${CONFIG_DIR}/addin.log.
EOF
