#!/usr/bin/env bash
#
# Offline test suite. No Fusion, no network, no API key.
#
#   tests/run.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

command -v uv >/dev/null 2>&1 || {
    printf 'ERROR: uv is not on PATH — see https://docs.astral.sh/uv/\n' >&2
    exit 1
}

# Two environments, because the two halves have different dependencies: the
# chat service needs the Agent SDK and aiohttp, the MCP server needs fastmcp.
# Each test runs under whichever one it imports from.
for project in agent server; do
    [ -d "${REPO_DIR}/${project}/.venv" ] || {
        printf 'Setting up %s/.venv (first run)\n' "${project}"
        uv sync --directory "${REPO_DIR}/${project}" --quiet
    }
done

status=0
total=0
captured="$(mktemp)"
trap 'rm -f "${captured}"' EXIT

for test_file in "${SCRIPT_DIR}"/test_*.py; do
    # A test that imports agent_service needs the agent env; everything else
    # (bridge, installer, MCP server) runs under the server env.
    if grep -q "^import agent_service\|^import agent_service as" "${test_file}"; then
        project=agent
    else
        project=server
    fi
    printf '\n=== %s  [%s env] ===\n' "$(basename "${test_file}")" "${project}"
    uv run --frozen --no-sync --directory "${REPO_DIR}/${project}" python "${test_file}" \
        2>&1 | tee "${captured}" || status=1
    passed="$(grep -oE '^[0-9]+ passed' "${captured}" | grep -oE '^[0-9]+' || true)"
    total=$(( total + ${passed:-0} ))
done

printf '\n'

# The README quotes this number, and a quoted number goes stale the moment a
# test is added. Only this script ever knows the real total, so this is the one
# place the claim can actually be checked rather than cross-referenced.
if [ "${status}" -eq 0 ]; then
    claimed="$(grep -oE '\*\*[0-9]+ assertions across' "${REPO_DIR}/README.md" \
               | grep -oE '[0-9]+' || true)"
    if [ -n "${claimed}" ] && [ "${claimed}" != "${total}" ]; then
        printf 'README says %s assertions; %s actually ran. Update it.\n' \
            "${claimed}" "${total}" >&2
        status=1
    fi
fi

[ "${status}" -eq 0 ] && printf 'All %s assertions passed.\n' "${total}" \
                      || printf 'FAILURES — see above.\n' >&2
exit "${status}"
