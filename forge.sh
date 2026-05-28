#!/usr/bin/env bash
# forge.sh — Launch the Forge orchestrator with environment preloaded.
# Opens a tmux session with this layout:
#
#   ┌─────────────────────────────────────────────────────┐
#   │  forge.py (status strip — minimal height)           │
#   ├──────────────────────────┬──────────────────────────┤
#   │                          │                          │
#   │   tail -f forge.log      │   tail -f cline.log      │
#   │                          │                          │
#   │                          │                          │
#   └──────────────────────────┴──────────────────────────┘
#
# If already inside a tmux session, splits the current window.
# If outside tmux, creates a new session named "forge".

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

FORGE_LOG="$SCRIPT_DIR/forge.log"
CLINE_LOG="$SCRIPT_DIR/cline.log"

# Touch log files so tail -f doesn't error before forge writes anything
touch "$FORGE_LOG" "$CLINE_LOG"

# ── Layout constants ──────────────────────────────────────────────────────────
# Main status strip height in lines — just enough to see the current task line
MAIN_HEIGHT=4

# ── Launch ────────────────────────────────────────────────────────────────────
if [[ -n "${TMUX:-}" ]]; then
    # Already inside tmux — reshape current window
    MAIN_PANE=$(tmux display-message -p "#{pane_id}")
    WIN=$(tmux display-message -p "#{window_id}")

    # Split bottom portion for logs (takes up most of the height)
    tmux split-window -v -t "$MAIN_PANE" -l "$MAIN_HEIGHT" -b \
        "source '$ENV_FILE' && python3 '$SCRIPT_DIR/forge.py' $*; echo '[forge exited — press any key]'; read -n1"
    FORGE_PANE=$(tmux display-message -p "#{pane_id}")

    # The original pane becomes forge.log (bottom-left)
    tmux send-keys -t "$MAIN_PANE" "tail -f '$FORGE_LOG'" Enter

    # Split forge.log pane right for cline.log (bottom-right)
    tmux split-window -h -t "$MAIN_PANE" "tail -f '$CLINE_LOG'"

    # Focus the forge process pane
    tmux select-pane -t "$FORGE_PANE"

else
    # Outside tmux — create new session
    SESSION="forge"

    # Kill existing forge session if present
    tmux kill-session -t "$SESSION" 2>/dev/null || true

    # Get terminal dimensions for sizing
    TERM_COLS=$(tput cols 2>/dev/null || echo 220)
    TERM_ROWS=$(tput lines 2>/dev/null || echo 50)

    # Create session: first pane is the forge process (top strip)
    tmux new-session -d -s "$SESSION" \
        -x "$TERM_COLS" -y "$TERM_ROWS" \
        "source '$ENV_FILE' && python3 '$SCRIPT_DIR/forge.py' $*; echo '[forge exited — press any key]'; read -n1"

    # Split a large bottom area for the logs
    tmux split-window -v -t "$SESSION:0.0" -l "$MAIN_HEIGHT" -b \
        "tail -f '$FORGE_LOG'"

    # Split that log pane in half horizontally for cline.log
    tmux split-window -h -t "$SESSION:0.0" \
        "tail -f '$CLINE_LOG'"

    # Resize the forge process strip to exactly MAIN_HEIGHT lines
    tmux resize-pane -t "$SESSION:0.2" -y "$MAIN_HEIGHT"

    # Give log panes equal width
    tmux select-layout -t "$SESSION" tiled
    tmux resize-pane -t "$SESSION:0.2" -y "$MAIN_HEIGHT"

    # Focus the forge process pane
    tmux select-pane -t "$SESSION:0.2"

    # Attach
    exec tmux attach-session -t "$SESSION"
fi