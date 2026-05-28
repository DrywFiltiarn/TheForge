#!/usr/bin/env bash
# forge.sh — Launch the Forge orchestrator with environment preloaded.
# Opens a tmux session with this layout:
#
#   ┌──────────────────────────────────────────────────────┐
#   │  forge.py (status strip — minimal height)            │
#   ├─────────────────┬────────────────┬───────────────────┤
#   │                 │                │                   │
#   │ tail forge.log  │ tail cline.log │  context.log      │
#   │ (orchestration) │ (cline output) │  (context usage)  │
#   │                 │                │                   │
#   └─────────────────┴────────────────┴───────────────────┘
#
# context.log is overwritten in real time by forge.py with the current
# context window usage percentage, token counts, and threshold indicator.
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
CONTEXT_LOG="$SCRIPT_DIR/context.log"

# Touch log files so tail -f doesn't error before forge writes anything
touch "$FORGE_LOG" "$CLINE_LOG"

# Initialise context.log with a waiting state
cat > "$CONTEXT_LOG" << 'CTXEOF'
──────────────────────────────────────────
  Context Monitor
  Waiting for Cline session to start...
──────────────────────────────────────────
CTXEOF

# Main status strip height in lines
MAIN_HEIGHT=4

# ── Layout builder ────────────────────────────────────────────────────────────
_build_layout() {
    local session="$1"

    # The session was created with the forge process in pane 0.
    # Split bottom area for the three log panes.

    # Bottom-left: forge.log
    tmux split-window -v -t "${session}:0.0" -l "$MAIN_HEIGHT" -b \
        "tail -f '$FORGE_LOG'"
    local forge_log_pane
    forge_log_pane=$(tmux display-message -p -t "${session}" "#{pane_id}")

    # Bottom-middle: cline.log (split forge.log pane horizontally)
    tmux split-window -h -t "${session}:0.0" \
        "tail -f '$CLINE_LOG'"

    # Bottom-right: context.log (watch -n1 re-reads the file every second
    # so the display updates even though it's not appended to)
    tmux split-window -h -t "${session}:0.1" \
        "watch -n1 -t cat '$CONTEXT_LOG'"

    # Even out the three bottom panes and pin the top strip
    tmux select-layout -t "$session" tiled
    tmux resize-pane -t "${session}:0.3" -y "$MAIN_HEIGHT"

    # Focus the forge process pane
    tmux select-pane -t "${session}:0.3"
}

# ── Launch ────────────────────────────────────────────────────────────────────
if [[ -n "${TMUX:-}" ]]; then
    MAIN_PANE=$(tmux display-message -p "#{pane_id}")
    CURRENT_WIN=$(tmux display-message -p "#{window_index}")

    # Split a strip at the top for forge process
    tmux split-window -v -t "$MAIN_PANE" -l "$MAIN_HEIGHT" -b \
        "source '$ENV_FILE' && python3 '$SCRIPT_DIR/forge.py' $*; echo '[forge exited — press any key]'; read -n1"
    FORGE_PANE=$(tmux display-message -p "#{pane_id}")

    # The original pane becomes forge.log
    tmux send-keys -t "$MAIN_PANE" "tail -f '$FORGE_LOG'" Enter

    # Split for cline.log
    tmux split-window -h -t "$MAIN_PANE" "tail -f '$CLINE_LOG'"

    # Split for context.log
    tmux split-window -h -t "$MAIN_PANE" "watch -n1 -t cat '$CONTEXT_LOG'"

    tmux select-pane -t "$FORGE_PANE"
else
    SESSION="forge"
    tmux kill-session -t "$SESSION" 2>/dev/null || true

    TERM_COLS=$(tput cols 2>/dev/null || echo 220)
    TERM_ROWS=$(tput lines 2>/dev/null || echo 50)

    # Create session with forge process as the first pane
    tmux new-session -d -s "$SESSION" \
        -x "$TERM_COLS" -y "$TERM_ROWS" \
        "source '$ENV_FILE' && python3 '$SCRIPT_DIR/forge.py' $*; echo '[forge exited — press any key]'; read -n1"

    # forge.log pane
    tmux split-window -v -t "$SESSION:0.0" -l "$MAIN_HEIGHT" -b \
        "tail -f '$FORGE_LOG'"

    # cline.log pane
    tmux split-window -h -t "$SESSION:0.0" \
        "tail -f '$CLINE_LOG'"

    # context.log pane — watch re-reads the file every second
    tmux split-window -h -t "$SESSION:0.1" \
        "watch -n1 -t cat '$CONTEXT_LOG'"

    # Tiled layout then pin the forge strip
    tmux select-layout -t "$SESSION" tiled
    tmux resize-pane -t "$SESSION:0.3" -y "$MAIN_HEIGHT"
    tmux select-pane -t "$SESSION:0.3"

    exec tmux attach-session -t "$SESSION"
fi