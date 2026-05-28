#!/usr/bin/env bash
# forge-status.sh — Launch the Forge status CLI with environment preloaded
# Place this anywhere convenient and make executable: chmod +x forge-status.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Load environment ──────────────────────────────────────────────────────────
ENV_FILE="$SCRIPT_DIR/forge.env"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "Error: forge.env not found at $ENV_FILE"
    echo "Run forge_setup.sh first to create it."
    exit 1
fi
# shellcheck source=/dev/null
source "$ENV_FILE"

# ── Launch ────────────────────────────────────────────────────────────────────
exec python3 "$SCRIPT_DIR/forge_status.py" "$@"