#!/usr/bin/env bash
# forge.sh — Launch the Forge orchestrator with environment preloaded.
#
# Layout
#
# ┌─────────────────────────────────────────────────────────┐
# │                                                         │
# │ tail -f '$CLINE_LOG'                                    │
# │ (cline output)                                          │
# │                                                         │
# ├───────────────────────┬─────────────────────────────────┤
# │ ${forge_cmd} (status) │ watch -n1 -t cat '$CONTEXT_LOG' │  
# │                       │ (context usage %)               │
# └───────────────────────┴─────────────────────────────────┘
#
# Bottom row is fixed at BOTTOM_HEIGHT lines — sized to show the full
# context display without scrolling. Top row gets all remaining height.
#
# Usage:
#   ./forge.sh --repo anvilml
#   ./forge.sh --repo anvilml --task P1-A1 --dry-run

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
source ".venv/bin/activate"

SESSION_NAME="cline-workspace"
FORGE_LOG="$SCRIPT_DIR/forge.log"
CLINE_LOG="$SCRIPT_DIR/cline.log"
CONTEXT_LOG="$SCRIPT_DIR/context.log"

# Build a properly quoted argument string to forward to forge.py
FORGE_ARGS=""
for arg in "$@"; do
    FORGE_ARGS="$FORGE_ARGS $(printf '%q' "$arg")"
done

FORGE_CMD="source '${ENV_FILE}' && python3 '${SCRIPT_DIR}/forge.py'${FORGE_ARGS}; echo '[forge exited — press any key]'; read -n1"

# Touch log files so tail -f doesn't error before forge writes anything
touch "$FORGE_LOG" "$CLINE_LOG"

# Initialise context.log with a waiting state
cat > "$CONTEXT_LOG" << 'CTXEOF'
──────────────────────────────────────────
  Context Monitor
  Waiting for Cline session to start...
──────────────────────────────────────────
CTXEOF

# Get current terminal dimensions to prevent tmux from scaling panes proportionally on attach
TERM_COLS=$(tput cols 2>/dev/null || echo 80)
TERM_LINES=$(tput lines 2>/dev/null || echo 24)

# Check if the tmux session exists; if not, create it
if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then

  # 1. Create a new detached session with exact terminal dimensions.
  # Passing the command directly at the end hides the shell prompt.
  tmux new-session -d -x "$TERM_COLS" -y "$TERM_LINES" -s "$SESSION_NAME" "tail -f '$CLINE_LOG'"

  # 2. Split vertically. Bottom pane is locked to 12 rows.
  # We wrap FORGE_CMD in bash -c because it uses 'source' and multiple chained commands.
  tmux split-window -v -l 20 -t "$SESSION_NAME:0.0" "bash -c \"$FORGE_CMD\""

  # 3. Split horizontally. The newly created right pane is locked to 60 columns.
  tmux split-window -h -l 60 -t "$SESSION_NAME:0.1" "watch -n1 -t cat '$CONTEXT_LOG'"

  # 4. Return focus to the top pane
  tmux select-pane -t "$SESSION_NAME:0.0"
fi

# Attach to the session
tmux attach-session -t "$SESSION_NAME"
