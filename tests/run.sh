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
capture_dir="$(mktemp -d)"
trap 'rm -rf "${capture_dir}"' EXIT

# The suites run concurrently: every one already isolates its state in its own
# temp directory and binds ports it probed for itself, which is what makes this
# safe. Serial took 14.5s and the slowest single suite 4.3s (measured), so the
# wall time is the slowest suite, not the sum. Output is captured per suite and
# printed in order afterwards, so a failure reads exactly as it did serially.
labels=()
pids=()

for test_file in "${SCRIPT_DIR}"/test_*.py; do
    # A test that imports agent_service needs the agent env; everything else
    # (bridge, installer, MCP server) runs under the server env.
    if grep -q "^import agent_service\|^import agent_service as" "${test_file}"; then
        project=agent
    else
        project=server
    fi
    label="$(basename "${test_file}")  [${project} env]"
    labels+=("${label}")
    uv run --frozen --no-sync --directory "${REPO_DIR}/${project}" python "${test_file}" \
        > "${capture_dir}/${#labels[@]}" 2>&1 &
    pids+=($!)
done

# The chat panel is JavaScript, so it runs under node rather than a venv. It is
# the product's face and had no coverage at all until a spinner that would not
# stop had to be diagnosed by reading the source.
have_node=0
if command -v node >/dev/null 2>&1; then
    have_node=1
    labels+=("test_panel.js  [node]")
    node "${SCRIPT_DIR}/test_panel.js" > "${capture_dir}/${#labels[@]}" 2>&1 &
    pids+=($!)
fi

for i in "${!pids[@]}"; do
    wait "${pids[$i]}" || status=1
    printf '\n=== %s ===\n' "${labels[$i]}"
    cat "${capture_dir}/$(( i + 1 ))"
    passed="$(grep -oE '^[0-9]+ passed' "${capture_dir}/$(( i + 1 ))" \
              | grep -oE '^[0-9]+' || true)"
    total=$(( total + ${passed:-0} ))
done

if [ "${have_node}" -eq 0 ]; then
    printf '\nERROR: node is required for the panel tests — install it or the\n' >&2
    printf 'panel ships unverified.\n' >&2
    status=1
fi

printf '\n'

# The README quotes this number, and a quoted number goes stale the moment a
# test is added. Only this script ever knows the real total, so this is the one
# place the claim can actually be checked rather than cross-referenced.
#
# The count is platform-dependent: the installer is macOS-only, so test_install
# skips its install assertions elsewhere and a full run is smaller off macOS.
# The documented number is therefore the macOS one, and only macOS can demand
# equality. Everywhere else a run that EXCEEDS the claim is still stale, and
# that is worth catching; one that falls short is just the expected skips.
if [ "${status}" -eq 0 ]; then
    claimed="$(grep -oE '\*\*[0-9]+ assertions across' "${REPO_DIR}/README.md" \
               | grep -oE '[0-9]+' || true)"
    if [ -n "${claimed}" ]; then
        if [ "$(uname -s)" = "Darwin" ] && [ "${claimed}" != "${total}" ]; then
            printf 'README says %s assertions; %s actually ran. Update it.\n' \
                "${claimed}" "${total}" >&2
            status=1
        elif [ "${total}" -gt "${claimed}" ]; then
            printf 'README says %s assertions; %s ran even with the macOS-only\n' \
                "${claimed}" "${total}" >&2
            printf 'ones skipped, so the README is out of date.\n' >&2
            status=1
        fi
    fi
fi

[ "${status}" -eq 0 ] && printf 'All %s assertions passed.\n' "${total}" \
                      || printf 'FAILURES — see above.\n' >&2
exit "${status}"
