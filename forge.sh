#!/usr/bin/env bash
# forge.sh — Run The Forge orchestrator with venv activated.
#
# Use this script rather than invoking forge.py directly.
# For the full tmux monitoring view, use forge_monitor.sh instead.
#
# Usage:
#   ./forge.sh --repo anvilml
#   ./forge.sh --repo anvilml --task P4-A3
#   ./forge.sh --repo anvilml --phase 4 --dry-run
#   ./forge.sh --repo anvilml --list
#   ./forge.sh --repo anvilml --reset-task P4-A3
#   ./forge.sh --repo anvilml --reset-task-git P4-A3

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python3"
ENV_FILE="$SCRIPT_DIR/forge.env"

# ── Pre-flight checks ─────────────────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
    echo "Error: forge.env not found at $ENV_FILE"
    echo "Run forge_setup.sh first."
    exit 1
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Error: .venv not found or not executable at $VENV_PYTHON"
    echo "Run forge_setup.sh to create it."
    exit 1
fi

# ── Activate environment and run ─────────────────────────────────────────────
# shellcheck source=/dev/null
source "$ENV_FILE"
source "$SCRIPT_DIR/.venv/bin/activate"

exec "$VENV_PYTHON" "$SCRIPT_DIR/forge.py" "$@"