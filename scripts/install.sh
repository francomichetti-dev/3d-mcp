#!/usr/bin/env bash
#
# fusion-mcp installer — 100% local, no network calls.
#
#   scripts/install.sh                 install / repair
#   scripts/install.sh --rotate-token  install and replace the bridge token
#
set -euo pipefail
umask 077

# set -u catches an unset HOME but not an empty one — and an empty HOME would
# collapse every path below to the filesystem root (mkdir/symlink under /).
: "${HOME:?HOME must be set to a non-empty path}"
[ -d "${HOME}" ] || { printf 'ERROR: HOME (%s) is not a directory\n' "${HOME}" >&2; exit 1; }

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

ADDIN_SOURCE="${REPO_DIR}/addin/FusionBridge"
ADDINS_DIR="${HOME}/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns"
ADDIN_LINK="${ADDINS_DIR}/FusionBridge"

SKILL_SOURCE="${REPO_DIR}/skill/fusion-360"
SKILLS_DIR="${HOME}/.claude/skills"
SKILL_LINK="${SKILLS_DIR}/fusion-360"

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

if [ -L "${CONFIG_DIR}" ]; then
    die "${CONFIG_DIR} is a symlink.
Refusing to create the token under a redirected path. Move it aside, then re-run."
fi

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
elif [ -L "${TOKEN_FILE}" ]; then
    # -e/chmod both follow symlinks: a symlinked token would chmod 0600 onto an
    # arbitrary target and make the add-in read that file's contents as the
    # bridge token. Same rule as the add-in link below — never follow, never delete.
    die "${TOKEN_FILE} is a symlink, not a token file.
Refusing to chmod or read through it. Move it aside, then re-run this script."
elif [ -f "${TOKEN_FILE}" ]; then
    chmod 600 "${TOKEN_FILE}"
    info "existing token preserved"
elif [ -e "${TOKEN_FILE}" ]; then
    die "${TOKEN_FILE} exists but is not a regular file.
Refusing to touch it. Move it aside, then re-run this script."
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

# --- knowledge skill ---------------------------------------------------------

# User-scoped, not a repo CLAUDE.md: CAD requests get prompted from arbitrary
# directories, and a repo-local file would only load inside this repo.
step "Linking the Fusion knowledge skill"

mkdir -p "${SKILLS_DIR}"

if [ -L "${SKILL_LINK}" ]; then
    current="$(readlink "${SKILL_LINK}")"
    if [ "${current}" = "${SKILL_SOURCE}" ]; then
        info "already linked"
    else
        rm -f "${SKILL_LINK}"
        ln -s "${SKILL_SOURCE}" "${SKILL_LINK}"
        info "replaced stale symlink (was: ${current})"
    fi
elif [ -e "${SKILL_LINK}" ]; then
    die "a real file or folder already exists at:
  ${SKILL_LINK}
Refusing to delete it. Move it aside, then re-run this script."
else
    ln -s "${SKILL_SOURCE}" "${SKILL_LINK}"
    info "linked ${SKILL_LINK} -> ${SKILL_SOURCE}"
fi

# --- /nibbler slash command --------------------------------------------------

# Generated rather than symlinked: the command needs this checkout's absolute
# path baked in so `/nibbler` works from any directory.
step "Installing the /nibbler slash command"

COMMANDS_DIR="${HOME}/.claude/commands"
if [ -L "${COMMANDS_DIR}" ]; then
    die "${COMMANDS_DIR} is a symlink. Refusing to write through it."
fi
mkdir -p "${COMMANDS_DIR}"

if [ -f "${REPO_DIR}/commands/nibbler.md" ]; then
    sed "s#__REPO__#${REPO_DIR}#g" "${REPO_DIR}/commands/nibbler.md" \
        > "${COMMANDS_DIR}/nibbler.md"
    info "installed ${COMMANDS_DIR}/nibbler.md"
    info "type /nibbler in any Claude Code session to open the panel"
else
    info "commands/nibbler.md not found — skipping"
fi

# --- exports dir -------------------------------------------------------------

step "Preparing the exports directory"
mkdir -p "${EXPORTS_DIR}"
info "${EXPORTS_DIR}"

# --- server environment ------------------------------------------------------

step "Building the MCP server environment (server/.venv)"

# The ONE intentional, user-initiated network step in this project: uv resolves
# and downloads the server's dependencies here, now. Every later session start
# runs with --frozen --no-sync, so it uses this .venv and never touches an
# index again — that is what keeps "zero external calls from our code" true by
# construction rather than by hope.
info "this step downloads dependencies (the only network access in the install)"
uv sync --directory "${REPO_DIR}/server" \
    || die "uv sync failed in ${REPO_DIR}/server — check your network and re-run."
info "environment ready: ${REPO_DIR}/server/.venv"

# --- chat agent environment --------------------------------------------------

step "Building the chat agent environment (agent/.venv)"

if [ -d "${REPO_DIR}/agent" ]; then
    uv sync --directory "${REPO_DIR}/agent" \
        || die "uv sync failed in ${REPO_DIR}/agent — check your network and re-run."
    info "environment ready: ${REPO_DIR}/agent/.venv"
    info "the NIBBLER panel starts this service on demand from inside Fusion"
else
    info "no agent/ directory — skipping the chat panel"
fi

# --- MCP registration --------------------------------------------------------

step "Registering the MCP server with Claude Code (user scope)"

if claude mcp get "${MCP_NAME}" >/dev/null 2>&1; then
    # No -s: remove from whichever scope holds it, so a stale local-scope entry
    # cannot shadow the user-scope one we are about to add.
    claude mcp remove "${MCP_NAME}" >/dev/null 2>&1 || true
    info "removed previous registration"
fi

# --frozen --no-sync: start from the .venv built above without re-resolving or
# re-checking the lockfile, so a routine session start contacts no package index.
claude mcp add "${MCP_NAME}" -s user -- \
    uv run --frozen --no-sync --directory "${REPO_DIR}/server" mcp_server.py
info "registered as '${MCP_NAME}' (absolute path baked in, offline start)"

# --- manual step -------------------------------------------------------------

cat <<EOF

Files are in place. One manual step is left, inside Fusion:

  Tools → Add-Ins → select FusionBridge → Run
  (auto-starts on later launches — runOnStartup is in the manifest)

The verification command, if you ever need it by hand:

  curl -fsS -H "X-Fusion-Bridge-Token: \$(cat ${TOKEN_FILE})" ${BRIDGE_URL}

A healthy bridge replies with JSON containing "ok": true and "bridge_version": "1".
EOF

# --- verification ------------------------------------------------------------

step "Verifying the bridge (${BRIDGE_URL}, up to 60s — do the step above now)"

verified=0
mismatch=""

if ! command -v curl >/dev/null 2>&1; then
    info "curl not found — skipping automatic verification"
else
    token="$(cat "${TOKEN_FILE}")"
    attempt=0
    while [ "${attempt}" -lt 30 ]; do
        attempt=$((attempt + 1))
        if reply="$(curl -fsS --max-time 5 -H "X-Fusion-Bridge-Token: ${token}" "${BRIDGE_URL}" 2>/dev/null)"; then
            case "${reply}" in
                *'"bridge_version": "1"'*|*'"bridge_version":"1"'*|\
                *'"bridge_version": 1'*|*'"bridge_version":1'*)
                    verified=1
                    break
                    ;;
                *)
                    mismatch="${reply}"
                    break
                    ;;
            esac
        fi
        sleep 2
    done
    unset token
fi

if [ "${verified}" -eq 1 ]; then
    cat <<EOF

Install complete and VERIFIED — the bridge answered on 127.0.0.1:7654
with bridge_version 1, using the token in ${TOKEN_FILE}.

Open a new Claude Code session anywhere and the 'fusion' tools are available.
EOF
elif [ -n "${mismatch}" ]; then
    cat <<EOF

Install finished, but the bridge is NOT verified: it answered with an
unexpected protocol version.

  ${mismatch}

This server expects bridge_version 1. Restart the add-in inside Fusion
(Tools → Add-Ins → FusionBridge → Stop, then Run) so it picks up the
current code, then re-run the curl command above.
Details: ${CONFIG_DIR}/addin.log
EOF
else
    cat <<EOF

Install finished, but the bridge is NOT verified — nothing answered on
127.0.0.1:7654 within 60s. That is expected if Fusion is not open yet.

  1. Open Fusion, then: Tools → Add-Ins → select FusionBridge → Run
  2. Re-run the curl command printed above (or just re-run this script)

If it still does not answer, the reason is in ${CONFIG_DIR}/addin.log.
EOF
fi
