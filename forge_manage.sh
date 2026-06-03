#!/usr/bin/env bash
# forge_manage.sh — Run The Forge management CLI with venv activated.
#
# Use this script rather than invoking forge_manage.py directly.
#
# Usage:
#   ./forge_manage.sh --repo anvilml
#   ./forge_manage.sh --repo anvilml --unblock
#   ./forge_manage.sh --repo anvilml --complete P4-A3
#   ./forge_manage.sh --repo anvilml --fail P4-A3
#   ./forge_manage.sh --repo anvilml --reset P4-A3
#   ./forge_manage.sh --repo anvilml --review P4-A3
#   ./forge_manage.sh --repo anvilml --clear-failed
#   ./forge_manage.sh --repo anvilml --clear-review
#   ./forge_manage.sh --repo anvilml --phase 4

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

exec "$VENV_PYTHON" "$SCRIPT_DIR/forge_manage.py" "$@"