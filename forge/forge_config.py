"""
forge_config.py — All constants, paths, and environment-derived configuration.

This module has NO imports from other forge_* modules. Everything else imports from here.
"""

import os
from pathlib import Path

# ─── Directory layout ─────────────────────────────────────────────────────────
# The forge/ package lives one level below the forge.py entry point.
# FORGE_DIR is the directory containing forge.py (the parent of this package).
#
#   <forge_dir>/
#     forge.py            — entry point
#     repos.json          — repository registry
#     agents/             — OpenCode agent markdown files
#     logs/               — all runtime log files
#     forge/              — this package (modules)
#     state.json          — runtime state (per-repo, set by main())

FORGE_DIR    = Path(__file__).parent.parent.resolve()  # parent of the forge/ package
AGENTS_DIR   = FORGE_DIR / "agents"
DOCS_DIR     = FORGE_DIR / "docs"         # bundled Forge documentation
LOGS_DIR     = FORGE_DIR / "logs"

REPOS_FILE              = FORGE_DIR / "repos.json"
LOG_FILE                = LOGS_DIR  / "forge.log"
OPENCODE_LOG_FILE       = LOGS_DIR  / "opencode.log"
CONTEXT_LOG_FILE        = LOGS_DIR  / "context.log"
OPENCODE_SKIPPED_LOG_FILE = LOGS_DIR / "opencode-skipped.log"
COMPACTION_LOG_FILE     = LOGS_DIR  / "compaction.log"

# Resolved in main() after --repo is validated.
# Points to <repo>/.forge/state.json — scoped to the active repository.
STATE_FILE: Path = Path()  # placeholder; set by main()

# ─── Discord ──────────────────────────────────────────────────────────────────

DISCORD_BOT_TOKEN            = os.environ.get("FORGE_DISCORD_TOKEN", "")
DISCORD_GUILD_ID             = os.environ.get("FORGE_DISCORD_GUILD_ID", "")
DISCORD_REPORTS_CHANNEL_ID   = "1509917708093886475"
DISCORD_APPROVALS_CHANNEL_ID = "1509917666889044068"
FORGE_OWNER_ID               = "334811986019745792"

APPROVE_EMOJI = "✅"
REJECT_EMOJI  = "❌"

# ─── OpenCode ─────────────────────────────────────────────────────────────────

OPENCODE_BIN          = os.environ.get("FORGE_OPENCODE_BIN",          "opencode")
OPENCODE_TIMEOUT      = int(os.environ.get("FORGE_OPENCODE_TIMEOUT",  str(60 * 120)))  # 120 min
OPENCODE_RETRIES      = int(os.environ.get("FORGE_OPENCODE_RETRIES",  "3"))
OPENCODE_RETRY_DELAY  = int(os.environ.get("FORGE_OPENCODE_RETRY_DELAY", "60"))
OPENCODE_CONTEXT_WINDOW = int(os.environ.get("FORGE_CONTEXT_WINDOW",  str(262144)))   # 256k

MODEL_PLANNING = os.environ.get("FORGE_MODEL_PLANNING", "llama.cpp/Qwen3.6-35B-A3B:planning")
MODEL_CODING   = os.environ.get("FORGE_MODEL_CODING",   "llama.cpp/Qwen3.6-35B-A3B:coding")

AGENT_PLAN_NAME = "forge-plan"
AGENT_ACT_NAME  = "forge-act"

# ─── Approval ─────────────────────────────────────────────────────────────────

APPROVAL_POLL_INTERVAL = int(os.environ.get("FORGE_POLL_INTERVAL",      "10"))
APPROVAL_TIMEOUT       = int(os.environ.get("FORGE_APPROVAL_TIMEOUT",   str(60 * 60 * 24)))  # 24h

# ─── Emoji encoding ───────────────────────────────────────────────────────────

EMOJI_APPROVE = "✅"
EMOJI_REJECT  = "❌"