# Forge — SindriStudio Autonomous Development Orchestrator

The Forge drives OpenCode through the SindriStudio build plan one atomic task at a time,
with Discord approval gates and full resume capability after interruptions or llama.cpp failures.

---

## Discord channel roles

Two channels, clearly separated:

| Channel | Role | Who reads | Forge behaviour |
|---|---|---|---|
| **#forge-reports** | Public broadcast | Anyone | Posts plan reports and implementation reports. **Never polled. Never acts on reactions here.** |
| **#forge-approvals** | Server-owner only | You | All approval requests. **Only channel the Forge polls for reactions.** |

### Workflow per task

1. OpenCode generates a plan → Forge posts **plan report** to `#forge-reports`
2. Forge posts **plan approval request** (with task ID) to `#forge-approvals`
3. You read the plan in `#forge-reports`, then approve/reject in `#forge-approvals`
4. On approve → OpenCode implements, tests, stages; Forge commits and pushes
5. Forge posts **implementation report** to `#forge-reports`
6. Forge posts **push approval request** (with task ID) to `#forge-approvals`
7. You read the implementation report, then approve/reject in `#forge-approvals`

Each approval message in `#forge-approvals` contains the task ID and a note like
*"Full report is in #forge-reports — search for `P1-A3`"* so you can switch channels
to read before reacting.

---

## Structure

```
forge/
├── forge.py              # Entry point — run this
├── forge_manage.py       # Management CLI — status, state mutations
├── repos.json            # Repository registry
├── forge.env             # Local environment config (gitignored)
├── .venv/                # Python virtual environment (auto-created by forge_setup.sh)
├── forge-plan.md         # OpenCode agent — PLAN sessions (read-only permissions)
├── forge-act.md          # OpenCode agent — ACT sessions (implementation permissions)
├── state.json            # Runtime state — auto-managed, never edit by hand
└── README.md             # This file

AnvilML/docs/
├── FORGE_AGENT_RULES.md  # Agent operating rules
└── ...
```

Agent files (`forge-plan.md`, `forge-act.md`) are automatically synced to
`~/.config/opencode/agents/` by `forge.py` on startup.

---

## First-time setup

```bash
bash forge/forge_setup.sh
```

This creates a `.venv/` directory beside `forge.py`, installs all Python
dependencies into it, and generates a `forge.env` template if one does not
exist. Always run The Forge using the venv Python:

```bash
./forge/forge.sh --repo anvilml
```

Then fill in `forge/forge.env` and create the two Discord channels.

---

## Discord setup

### Create the bot
1. https://discord.com/developers/applications → New Application
2. Bot → Add Bot → copy the token → set as `FORGE_DISCORD_TOKEN`
3. Enable **Message Content Intent** under Privileged Gateway Intents
4. OAuth2 → URL Generator → Scopes: `bot` → Permissions:
   Send Messages, Add Reactions, Read Message History, View Channels
5. Invite the bot to your server via the generated URL

### Create channels
- `#forge-reports` — set permissions so everyone can read, bot can write, members cannot write
- `#forge-approvals` — set permissions so only you (server owner) can read and react; bot can write

### Get IDs
Right-click server icon → Copy Server ID (requires Developer Mode in Discord Settings → Advanced).

### forge.env
```bash
export FORGE_DISCORD_TOKEN="your-bot-token"
export FORGE_DISCORD_GUILD_ID="your-server-id"
export FORGE_DISCORD_REPORTS_CHANNEL="forge-reports"
export FORGE_DISCORD_APPROVALS_CHANNEL="forge-approvals"
```

---

## OpenCode configuration (llama.cpp)

OpenCode reads provider and model configuration from `~/.config/opencode/opencode.json`.
The relevant section for a local llama.cpp/llama-swap server:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "llama.cpp": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "llama-server (local)",
      "options": {
        "baseURL": "http://172.26.160.1:8080/v1"
      },
      "models": {
        "Qwen3.6-35B-A3B:planning": {
          "name": "Qwen3.6-35B-A3B:coding",
          "limit": { "context": 262144, "output": 65536 }
        },
        "Qwen3.6-35B-A3B:coding": {
          "name": "Qwen3.6-35B-A3B:coding",
          "limit": { "context": 262144, "output": 65536 }
        }
      }
    }
  },
  "mcp": {
    "rust-docs": {
      "type": "local",
      "command": ["node", "/home/dryw/mcp-rust-docs/index.js"],
      "enabled": true
    },
    "pypi-query": {
      "type": "local",
      "command": ["uvx", "--from", "pypi-query-mcp-server", "pypi-query-mcp"],
      "enabled": true,
      "environment": {
        "PYPI_INDEX_URL": "https://pypi.org/simple/",
        "CACHE_TTL": "3600"
      }
    }
  }
}
```

Model selection is controlled via environment variables (see Configuration reference below).

---

## Running

```bash
# ── Orchestrator ──────────────────────────────────────────────────
./forge/forge.sh --repo anvilml                    # run from next unblocked task
./forge/forge.sh --repo anvilml --dry-run          # show what would run
./forge/forge.sh --repo anvilml --task P1-A3       # run one specific task
./forge/forge.sh --repo anvilml --list             # show DAG status and exit
./forge/forge.sh --repo anvilml --phase 4          # load phases 1–4
./forge/forge.sh --repo anvilml --reset-task P1-A3      # reset (no git)
./forge/forge.sh --repo anvilml --reset-task-git P1-A3  # reset + git

# ── Full monitoring view (tmux) ───────────────────────────────────
./forge/forge_monitor.sh --repo anvilml

# ── Management ────────────────────────────────────────────────────
./forge/forge_manage.sh --repo anvilml             # full status table
./forge/forge_manage.sh --repo anvilml --unblock   # ready-to-run only
```

**Resume after any interruption:** just re-run `./forge/forge.sh --repo <project>`.
State is written to disk before every external action — nothing is lost.

---

## Task management

`forge_manage.py` provides status inspection and state mutations without running
the full orchestrator. Safe to use while `forge.py` is running.

```bash
./forge/forge_manage.sh --repo anvilml                   # full status table
./forge/forge_manage.sh --repo anvilml --unblock          # ready-to-run only
./forge/forge_manage.sh --repo anvilml --phase 4          # limit to phases 1–4
./forge/forge_manage.sh --repo anvilml --complete P4-A3   # manually mark complete
./forge/forge_manage.sh --repo anvilml --fail P4-A3       # mark as failed
./forge/forge_manage.sh --repo anvilml --reset P4-A3      # reset to unstarted
./forge/forge_manage.sh --repo anvilml --review P4-A3     # mark needs-review
./forge/forge_manage.sh --repo anvilml --clear-failed     # reset all failed
./forge/forge_manage.sh --repo anvilml --clear-review     # reset all needs-review
```

### When to use each command

| Command | When to use |
|---------|-------------|
| `--complete` | Push was rejected but you reviewed the implementation and it's acceptable. Marks the task done and unblocks dependents. |
| `--fail` | Explicitly mark a task failed so The Forge stops treating it as in-progress. |
| `--reset` | Re-run a task from scratch — clears plan approval and current plan. No git changes. |
| `--review` | Flag a task for manual inspection. Blocks all dependent tasks until resolved via `--complete` or `--reset`. |
| `--clear-failed` | Bulk-reset all failed tasks to unstarted after investigating root cause. |
| `--clear-review` | Bulk-reset all needs-review tasks after a review pass. |
| `--unblock` | Quick scan of what's ready to run and what's currently blocking progress. |

---

## Report files on disk

Every task produces two markdown reports inside the target repository:

```
AnvilML/.forge/reports/<TASK-ID>_plan.md         # written by forge-plan agent
AnvilML/.forge/reports/<TASK-ID>_implement.md    # written by forge-act agent
```

The Forge reads these files to populate the Discord posts. Both are committed to the
repository as part of the post-approval commit, so the full build history is in git.

---

## Monitoring a live session

```bash
tail -f forge/logs/opencode.log    # agent tool calls, prose output, session summary
tail -f forge/logs/context.log     # live context window usage (updates per step)
tail -f forge/logs/forge.log       # orchestrator decisions, approvals, errors
```

---

## Handling failures

### llama.cpp crash or timeout
Automatically retried up to `FORGE_OPENCODE_RETRIES` times (default: 3) with
increasing delays. A warning is posted to `#forge-approvals` on each retry.
If all retries fail, the task is marked `failed` and the Forge stops.

### Retry a failed task with clean repos
```bash
# Hard-reset all touched repos to origin/develop, then retry
./forge/forge.sh --reset-task-git P1-A3

# Or reset state only (keep whatever the agent wrote locally, inspect first)
./forge/forge.sh --reset-task P1-A3
./forge/forge.sh --task P1-A3
```

`--reset-task-git` runs `git fetch origin develop && git reset --hard origin/develop`
on each repo the task touches, plus `git clean -fd` for untracked files. This
ensures the retry starts from a known-good codebase with no half-written code.

### When to use --reset-task-git vs --reset-task

| Situation | Command |
|---|---|
| Agent failed before writing any files | `--reset-task` is enough |
| Agent wrote partial files, no commits | `--reset-task-git` (cleans working tree) |
| Agent made local commits, not pushed | `--reset-task-git` (resets to origin) |
| Agent pushed but push approval rejected | neither — task is `needs_review`; inspect manually |

### Task needs review (push rejected)
```bash
./forge/forge_manage.sh --repo anvilml   # see what's in review

# After reviewing git log and deciding it's acceptable:
./forge/forge_manage.sh --repo anvilml --complete P1-A3

# After deciding it needs a full redo:
./forge/forge.sh --reset-task-git P1-A3
./forge/forge.sh --task P1-A3
```

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `FORGE_DISCORD_TOKEN` | — | Discord bot token (required) |
| `FORGE_DISCORD_GUILD_ID` | — | Discord server ID (required) |
| `FORGE_DISCORD_REPORTS_CHANNEL` | `forge-reports` | Broadcast channel (plan + impl reports) |
| `FORGE_DISCORD_APPROVALS_CHANNEL` | `forge-approvals` | Approval channel (polled for reactions) |
| `FORGE_OPENCODE_BIN` | `opencode` | Path to OpenCode binary |
| `FORGE_OPENCODE_TIMEOUT` | `7200` | Max seconds per OpenCode session (120 min) |
| `FORGE_OPENCODE_RETRIES` | `3` | Retry attempts on OpenCode failure |
| `FORGE_OPENCODE_RETRY_DELAY` | `60` | Base seconds between retries (×attempt number) |
| `FORGE_CONTEXT_WINDOW` | `262144` | Model context window size in tokens (256k) |
| `FORGE_MODEL_PLANNING` | `llama.cpp/Qwen3.6-35B-A3B:planning` | Model for PLAN sessions |
| `FORGE_MODEL_CODING` | `llama.cpp/Qwen3.6-35B-A3B:coding` | Model for ACT sessions |
| `FORGE_POLL_INTERVAL` | `10` | Seconds between Discord reaction polls |
| `FORGE_APPROVAL_TIMEOUT` | `86400` | Max seconds to wait for approval (24h) |