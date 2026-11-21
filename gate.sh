#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "=== Gate run at $(TZ=SGT-8 date '+%Y-%m-%d %H:%M:%S SGT') ==="
du -sh "$SCRIPT_DIR/.git" || true
git -C "$SCRIPT_DIR" count-objects -vH || true
exec python "$SCRIPT_DIR/gate_runner.py" "$@"
