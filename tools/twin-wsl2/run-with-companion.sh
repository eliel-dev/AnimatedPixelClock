#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BRIDGE="$ROOT/tools/twin-wsl2/http_bridge.py"
RUNNER="$ROOT/tools/twin-wsl2/run.sh"

if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is required to bind the Companion HTTP bridge to port 80." >&2
    exit 1
fi

sudo -v
sudo -n python3 "$BRIDGE" &
bridge_pid=$!

cleanup() {
    local status=$?
    trap - EXIT
    if kill -0 "$bridge_pid" 2>/dev/null; then
        kill -TERM "$bridge_pid" 2>/dev/null || true
    fi
    wait "$bridge_pid" 2>/dev/null || true
    exit "$status"
}
trap cleanup EXIT

for _ in {1..20}; do
    if ! kill -0 "$bridge_pid" 2>/dev/null; then
        wait "$bridge_pid" || true
        echo "The HTTP bridge failed to start; check whether port 80 is already in use." >&2
        exit 1
    fi
    sleep 0.1
done

"$RUNNER" "$@"
