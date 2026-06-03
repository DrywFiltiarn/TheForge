#!/usr/bin/env bash
# forge_setup.sh — One-time setup for The Forge
# Run from the forge directory: bash forge_setup.sh
set -euo pipefail

FORGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$FORGE_DIR/.venv"
cd "$FORGE_DIR"

echo "=== The Forge — Setup ==="
echo ""

# ── Python version check ──────────────────────────────────────────────────────
echo "Checking Python..."
if ! command -v python3 &>/dev/null; then
    echo "  ✗ python3 not found — required (3.9+)"
    exit 1
fi
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 9 ]]; }; then
    echo "  ✗ Python $PY_VER found but 3.9+ is required"
    exit 1
fi
echo "  ✓ Python $PY_VER"

# ── Virtual environment ───────────────────────────────────────────────────────
echo ""
echo "Setting up Python virtual environment..."
if [[ -d "$VENV_DIR" ]]; then
    echo "  .venv already exists — checking it is functional..."
    if "$VENV_DIR/bin/python3" -c "import sys" 2>/dev/null; then
        echo "  ✓ .venv OK"
    else
        echo "  .venv appears broken — recreating..."
        rm -rf "$VENV_DIR"
        python3 -m venv "$VENV_DIR"
        echo "  ✓ .venv recreated"
    fi
else
    python3 -m venv "$VENV_DIR"
    echo "  ✓ .venv created at $VENV_DIR"
fi

VENV_PY="$VENV_DIR/bin/python3"
VENV_PIP="$VENV_DIR/bin/pip"

# ── Python dependencies ───────────────────────────────────────────────────────
echo ""
echo "Installing Python dependencies into .venv..."
"$VENV_PIP" install --quiet --upgrade pip
"$VENV_PIP" install --quiet requests weasyprint markdown
echo "  ✓ requests   (Discord API)"
echo "  ✓ weasyprint  (PDF generation for report attachments)"
echo "  ✓ markdown    (Markdown → HTML conversion)"

# zoneinfo is stdlib in Python 3.9+; backports needed only for 3.8
if [[ "$PY_MINOR" -lt 9 ]]; then
    "$VENV_PIP" install --quiet backports.zoneinfo
    echo "  ✓ backports.zoneinfo (Python < 3.9 timezone support)"
fi

echo ""
echo "  To activate the venv manually:"
echo "    source $VENV_DIR/bin/activate"
echo ""
echo "  The Forge must be run via the provided shell scripts:"
echo "    ./forge.sh --repo <project>"
echo "    ./forge_monitor.sh --repo <project>  # with tmux monitor"

# ── OpenCode CLI check ────────────────────────────────────────────────────────
echo ""
echo "Checking OpenCode CLI..."
if command -v opencode &>/dev/null; then
    OC_VER=$(opencode --version 2>/dev/null || echo "unknown")
    echo "  ✓ opencode found: $OC_VER"
else
    echo "  ✗ opencode not found"
    echo "    Install with: npm install -g opencode-ai"
fi

# ── Git check ─────────────────────────────────────────────────────────────────
echo ""
echo "Checking git..."
if command -v git &>/dev/null; then
    GIT_VER=$(git --version)
    echo "  ✓ $GIT_VER"
else
    echo "  ✗ git not found — required"
    exit 1
fi

# ── forge.env ─────────────────────────────────────────────────────────────────
echo ""
if [[ -f "$FORGE_DIR/forge.env" ]]; then
    echo "forge.env found — skipping template creation"
else
    echo "Creating forge.env template..."
    cat > "$FORGE_DIR/forge.env" << 'ENV_EOF'
# The Forge — environment configuration
# Fill in your values, then source before running:
#   source forge.env
#   ./forge.sh --repo <project>

# ── Discord (required for approval gates) ─────────────────────────────────────
export FORGE_DISCORD_TOKEN=""
export FORGE_DISCORD_GUILD_ID=""

# Discord channel names (must exist in your server)
export FORGE_DISCORD_APPROVALS_CHANNEL="forge-approvals"
export FORGE_DISCORD_REPORTS_CHANNEL="forge-reports"

# ── OpenCode ──────────────────────────────────────────────────────────────────
# export FORGE_OPENCODE_BIN="opencode"           # path to opencode binary (default: opencode)
# export FORGE_OPENCODE_TIMEOUT="7200"           # max seconds per session (default: 120 min)
# export FORGE_OPENCODE_RETRIES="3"              # retries on llama.cpp crash (default: 3)
# export FORGE_OPENCODE_RETRY_DELAY="60"         # base seconds between retries (default: 60)

# ── Models ────────────────────────────────────────────────────────────────────
# export FORGE_MODEL_PLANNING="llama.cpp/Qwen3.6-35B-A3B:planning"
# export FORGE_MODEL_CODING="llama.cpp/Qwen3.6-35B-A3B:coding"
# export FORGE_CONTEXT_WINDOW="262144"           # model context window in tokens (default: 256k)

# ── Approval polling ──────────────────────────────────────────────────────────
# export FORGE_POLL_INTERVAL="10"                # seconds between Discord polls (default: 10)
# export FORGE_APPROVAL_TIMEOUT="86400"          # approval timeout in seconds (default: 24h)
ENV_EOF
    echo "  ✓ Created forge.env — fill in FORGE_DISCORD_TOKEN and FORGE_DISCORD_GUILD_ID"
fi

# ── Check required env vars ───────────────────────────────────────────────────
echo ""
echo "Checking environment variables..."
# Source forge.env if present so we can validate it
if [[ -f "$FORGE_DIR/forge.env" ]]; then
    # shellcheck source=/dev/null
    source "$FORGE_DIR/forge.env"
fi

MISSING=0
for VAR in FORGE_DISCORD_TOKEN FORGE_DISCORD_GUILD_ID; do
    if [[ -z "${!VAR:-}" ]]; then
        echo "  ✗ $VAR not set"
        MISSING=$((MISSING + 1))
    else
        echo "  ✓ $VAR set"
    fi
done

if [[ $MISSING -gt 0 ]]; then
    echo ""
    echo "  ⚠  $MISSING variable(s) unset in forge.env."
    echo "     The Forge will run but Discord notifications will be disabled."
fi

# ── Discord bot setup instructions ────────────────────────────────────────────
echo ""
echo "=== Discord Bot Setup ==="
echo ""
echo "1. https://discord.com/developers/applications → New Application"
echo "2. Bot → Add Bot → copy TOKEN → set as FORGE_DISCORD_TOKEN"
echo "3. Bot → Privileged Gateway Intents → enable Message Content Intent"
echo "4. OAuth2 → URL Generator → Scopes: bot"
echo "   Permissions: Send Messages, Add Reactions, Read Message History, View Channels"
echo "5. Open the generated URL to invite the bot to your server"
echo "6. Create two text channels:"
echo "   #forge-approvals  (owner-only — approval requests and reactions)"
echo "   #forge-reports    (broadcast — plan and implementation reports)"
echo "7. Server ID: right-click server icon → Copy Server ID"
echo "   (Enable Developer Mode: Discord Settings → Advanced)"

# ── OpenCode configuration instructions ───────────────────────────────────────
echo ""
echo "=== OpenCode Configuration ==="
echo ""
echo "Edit ~/.config/opencode/opencode.json to configure your local model endpoint."
echo "See README.md for the full opencode.json template."

# ── .gitignore ────────────────────────────────────────────────────────────────
if [[ -f "$FORGE_DIR/.gitignore" ]]; then
    # Ensure .venv is ignored if .gitignore exists
    if ! grep -q "^\.venv" "$FORGE_DIR/.gitignore" 2>/dev/null; then
        echo "" >> "$FORGE_DIR/.gitignore"
        echo "# Python virtual environment" >> "$FORGE_DIR/.gitignore"
        echo ".venv/" >> "$FORGE_DIR/.gitignore"
        echo ""
        echo "Added .venv/ to .gitignore"
    fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "=== Setup complete ==="
echo ""
echo "Usage:"
echo "  source forge.env"
echo "  ./forge.sh --repo <project>                 # run The Forge"
echo "  ./forge.sh --repo <project> --dry-run"
echo "  ./forge.sh --repo <project> --list          # task status"
echo "  ./forge_monitor.sh --repo <project>         # tmux monitoring view"
echo "  ./forge_manage.sh --repo <project>          # management CLI"