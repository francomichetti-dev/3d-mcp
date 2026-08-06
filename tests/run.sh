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

# Run from agent/ so its venv is the one uv picks up: the tests import
# agent_service, which needs the SDK and aiohttp.
[ -d "${REPO_DIR}/agent/.venv" ] || {
    printf 'Setting up agent/.venv (first run)\n'
    uv sync --directory "${REPO_DIR}/agent" --quiet
}

status=0
for test_file in "${SCRIPT_DIR}"/test_*.py; do
    printf '\n=== %s ===\n' "$(basename "${test_file}")"
    uv run --frozen --no-sync --directory "${REPO_DIR}/agent" python "${test_file}" || status=1
done

printf '\n'
[ "${status}" -eq 0 ] && printf 'All suites passed.\n' || printf 'FAILURES — see above.\n' >&2
exit "${status}"
