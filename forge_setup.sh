#!/usr/bin/env bash
# forge_setup.sh — Install dependencies and validate environment for Forge
# Run once before first use: bash forge/forge_setup.sh
set -euo pipefail

FORGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$FORGE_DIR"

echo "=== Forge Setup ==="
echo ""

# ── Python deps ──────────────────────────────────────────────────────────────
echo "Installing Python dependencies..."
pip install requests --quiet
echo "  ✓ requests"

# ── Cline CLI check ───────────────────────────────────────────────────────────
echo ""
echo "Checking Cline CLI..."
if command -v cline &>/dev/null; then
    CLINE_VER=$(cline --version 2>/dev/null || echo "unknown")
    echo "  ✓ cline found: $CLINE_VER"
else
    echo "  ✗ cline not found"
    echo "    Install with: npm install -g cline"
    echo "    Then configure: cline auth -p openai-compatible -k dummy"
    echo "    With base URL:  cline config set act-mode-openai-base-url http://localhost:11434/v1"
fi

# ── Git check ─────────────────────────────────────────────────────────────────
echo ""
echo "Checking git..."
if command -v git &>/dev/null; then
    GIT_VER=$(git --version)
    echo "  ✓ $GIT_VER"
else
    echo "  ✗ git not found — required"
fi

# ── Environment variables ─────────────────────────────────────────────────────
echo ""
echo "Environment variables (check forge.env):"
if [[ -f "$FORGE_DIR/forge.env" ]]; then
    echo "  forge.env found"
    # shellcheck source=/dev/null
    source "$FORGE_DIR/forge.env"
else
    echo "  forge.env not found — creating template..."
    cat > "$FORGE_DIR/forge.env" << 'EOF'
# Forge environment configuration
# Copy this file and fill in your values.
# Source before running: source forge/forge.env

# Discord bot token (required for approval gates)
export FORGE_DISCORD_TOKEN=""

# Discord guild (server) ID — right-click server icon → Copy Server ID
export FORGE_DISCORD_GUILD_ID=""

# Discord channel names (must exist in your server)
export FORGE_DISCORD_APPROVALS_CHANNEL="forge-approvals"
export FORGE_DISCORD_REPORTS_CHANNEL="forge-reports"

# Cline binary path (default: cline from PATH)
# export FORGE_CLINE_BIN="cline"

# Cline session timeout in seconds (default: 5400 = 90 minutes)
# export FORGE_CLINE_TIMEOUT="5400"

# Number of Cline retries on llama.cpp failure (default: 3)
# export FORGE_CLINE_RETRIES="3"

# Seconds between retry attempts (default: 60, multiplied by attempt number)
# export FORGE_CLINE_RETRY_DELAY="60"

# Approval poll interval in seconds (default: 10)
# export FORGE_POLL_INTERVAL="10"

# Approval timeout in seconds (default: 86400 = 24 hours)
# export FORGE_APPROVAL_TIMEOUT="86400"
EOF
    echo "  Created forge/forge.env — fill in your values"
fi

echo ""
echo "Checking required env vars..."
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
    echo "⚠️  $MISSING required variable(s) missing."
    echo "   Edit forge/forge.env and source it: source forge/forge.env"
    echo "   Forge will still run but Discord notifications will be disabled."
fi

# ── Discord bot instructions ──────────────────────────────────────────────────
echo ""
echo "=== Discord Bot Setup ==="
echo ""
echo "1. Go to https://discord.com/developers/applications"
echo "2. Create a new application (e.g. 'Forge')"
echo "3. Go to Bot → Add Bot → copy the TOKEN → set as FORGE_DISCORD_TOKEN"
echo "4. Enable 'Message Content Intent' under Bot → Privileged Gateway Intents"
echo "5. Go to OAuth2 → URL Generator:"
echo "   Scopes: bot"
echo "   Bot Permissions: Send Messages, Add Reactions, Read Message History, View Channels"
echo "6. Open the generated URL in a browser to invite the bot to your server"
echo "7. Create two text channels in your server:"
echo "   #forge-approvals  (server-owner only — approval requests polled here)
   #forge-reports   (public broadcast — plan and implementation reports)"
echo "8. Get your Server ID: right-click the server icon → Copy Server ID"
echo "   (Enable Developer Mode in Discord Settings → Advanced first)"
echo ""

# ── Cline configuration ───────────────────────────────────────────────────────
echo "=== Cline Configuration for llama.cpp ==="
echo ""
echo "Configure Cline to use your local llama.cpp endpoint:"
echo ""
echo "  cline config set act-mode-api-provider openai-compatible"
echo "  cline config set act-mode-openai-base-url http://localhost:11434/v1"
echo "  cline config set act-mode-openai-api-key dummy"
echo "  cline config set act-mode-openai-model-id qwen3"
echo ""
echo "  cline config set plan-mode-api-provider openai-compatible"
echo "  cline config set plan-mode-openai-base-url http://localhost:11434/v1"
echo "  cline config set plan-mode-openai-api-key dummy"
echo "  cline config set plan-mode-openai-model-id qwen3"
echo ""
echo "  # Disable auto-approve in Cline config (Forge manages approval externally)"
echo "  cline config set auto-approval-settings.enabled false"
echo ""

echo "=== Setup complete ==="
echo ""
echo "Usage:"
echo "  source forge/forge.env"
echo "  python forge/forge.py --list        # see task status"
echo "  python forge/forge.py               # run from next unblocked task"
echo "  python forge/forge.py --dry-run     # test without executing Cline"
echo "  python forge/forge.py --task P1-A1  # force a specific task"
