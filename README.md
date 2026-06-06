# The Forge — Autonomous Development Orchestrator

The Forge is a project-agnostic autonomous development orchestrator. It drives an OpenCode agent through a strict plan → approve → implement → approve → commit/push cycle, one atomic task at a time, with Discord approval gates and full resume capability after interruptions or model failures. It is not tied to any particular programming language or project type — the same orchestrator and agents work across Rust, Python, TypeScript, or any combination thereof. The current primary focus is Rust/Python/TypeScript projects, with support for additional stacks added as needed.

---

## How it works

The Forge agents (`forge-plan` and `forge-act`) are universal. They contain no project-specific knowledge. All knowledge about what to build and how to build it is provided through three documents that live in the target project's `docs/` directory and are read by the agents at the start of every session:

**`ENVIRONMENT.md`** is the agent's executable contract with the project's toolchain. It defines the exact commands for building, formatting, linting, testing, and any platform cross-checks or project-specific quality gates. Because every command the agent runs comes from this file, adapting The Forge to a new project or tech stack requires only updating this document — the agents and orchestrator are unchanged.

**`ARCHITECTURE.md`** is a navigational map of the repository. It describes the package or crate layout, the role and boundaries of each module or component, and the design principles that govern how they interact. Agents use this to understand where new code belongs, what existing code they can depend on, and what is intentionally out of scope for any given task.

**`<PROJECT>_DESIGN.md`** is the functional specification. It defines what the project does — its domain types, API contracts, IPC protocol, data model, and any other behavioural or interface requirements. This is the authoritative source an agent consults when it needs to know not just where to write code, but what that code must do and how it must behave relative to the rest of the system.

These three documents cross-reference each other and together give the agents everything they need to plan and implement any task correctly without out-of-band instructions.

### Forge-managed documents

Two additional documents are maintained by The Forge itself and automatically deployed into each project's `docs/` directory on startup:

**`FORGE_AGENT_RULES.md`** defines the operating rules that apply to all agents on all projects: task atomicity, git constraints, test and formatting requirements, version bumping, prohibited behaviours, and report formats. It is the universal behavioural contract that sits above any project-specific instruction.

**`FORGE_TASK_AUTHORING_SPEC.md`** is the authoring guide for task definitions — the `tasks_phase<NNN>.json` and `TASKS_PHASE<NNN>.md` files that describe the work to be done. It is written for both human authors and LLMs generating task content, and specifies the exact format, field semantics, sizing rules, and quality standards expected by the orchestrator.

Both documents are versioned inside The Forge. If a project copy is missing it is installed silently; if it is present but out of sync with the bundled version, the operator is prompted to update before the session continues.

---

## Discord channel roles

Two channels, clearly separated:

| Channel | Role | Who reads | The Forge behaviour |
|---|---|---|---|
| **#forge-reports** | Public broadcast | Anyone | Posts plan reports and implementation reports. **Never polled. Never acts on reactions here.** |
| **#forge-approvals** | Server-owner only | You | All approval requests. **Only channel the Forge polls for reactions.** |

### Workflow per task

1. OpenCode generates a plan → The Forge posts **plan report** to `#forge-reports`
2. The Forge posts **plan approval request** (with task ID) to `#forge-approvals`
3. You read the plan in `#forge-reports`, then approve/reject in `#forge-approvals`
4. On approve → OpenCode implements, tests, stages; The Forge commits and pushes
5. The Forge posts **implementation report** to `#forge-reports`
6. The Forge posts **push approval request** (with task ID) to `#forge-approvals`
7. You read the implementation report, then approve/reject in `#forge-approvals`

Each approval message in `#forge-approvals` contains the task ID and a note like
*"Full report is in #forge-reports — search for `P1-A3`"* so you can switch channels
to read before reacting.

---

## Structure

```
<forge-root>/
├── forge.py                          # Entry point — never run directly; use forge.sh
├── forge_manage.py                   # Management CLI — never run directly; use forge_manage.sh
├── forge.sh                          # Orchestrator launcher (activates .venv)
├── forge_manage.sh                   # Management CLI launcher (activates .venv)
├── forge_monitor.sh                  # tmux monitoring view launcher
├── forge_setup.sh                    # First-time setup: creates .venv, forge.env template
├── repos.json                        # Repository registry
├── forge.env                         # Local environment config (gitignored)
├── .venv/                            # Python virtual environment (auto-created by forge_setup.sh)
├── agents/                           # OpenCode agent markdown files (source of truth)
│   ├── forge-plan.md                 # PLAN sessions — read-only, plan report only
│   └── forge-act.md                  # ACT sessions — full implementation permissions
├── docs/                             # Bundled Forge documents (synced to each repo's docs/ on startup)
│   ├── FORGE_AGENT_RULES.md
│   └── FORGE_TASK_AUTHORING_SPEC.md
├── logs/                             # All runtime log output
│   ├── forge.log
│   ├── opencode.log
│   ├── context.log
│   ├── compaction.log
│   └── traces/                       # Per-task opencode.log archives (<TASK-ID>_opencode.log)
├── forge/                            # Implementation package (forge_config.py, forge_runner.py, …)
└── README.md                         # This file

<repo>/                               # Target repository (e.g. AnvilML)
├── docs/
│   ├── FORGE_AGENT_RULES.md          # Managed by The Forge — installed/updated on startup
│   ├── FORGE_TASK_AUTHORING_SPEC.md  # Managed by The Forge — installed/updated on startup
│   ├── ENVIRONMENT.md                # Project-owned: toolchain, commands, gates
│   ├── ARCHITECTURE.md               # Project-owned: module layout, component boundaries
│   ├── <PROJECT>_DESIGN.md           # Project-owned: functional spec and API contracts
│   └── TASKS_PHASE<NNN>.md           # Per-phase human-readable task narrative (one per phase)
└── .forge/
    ├── tasks/                        # Task JSON files (tasks_phase<NNN>.json) — authored per project
    ├── reports/                      # Plan and implementation reports written by OpenCode agents
    │   ├── <TASK-ID>_plan.md
    │   └── <TASK-ID>_implement.md
    └── state/
        ├── state.json                # Runtime state — auto-managed, never edit by hand
        └── CURRENT_TASK.md
```

Agent files (`forge-plan.md`, `forge-act.md`) are automatically synced from `agents/` to
`~/.config/opencode/agents/` by `forge.py` on startup.

Forge documents (`FORGE_AGENT_RULES.md`, `FORGE_TASK_AUTHORING_SPEC.md`) are automatically
synced from `docs/` to `<repo>/docs/` by `forge.py` on startup. Out-of-sync copies prompt
for an update.

---

## First-time setup

```bash
bash forge_setup.sh
```

This creates a `.venv/` directory beside `forge.py`, installs all Python
dependencies into it, and generates a `forge.env` template if one does not
exist.

Always run The Forge via the provided shell scripts — never invoke `forge.py` directly:

```bash
./forge.sh --repo anvilml
```

Then fill in `forge.env` and create the two Discord channels.

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
- `#forge-reports` — everyone can read, bot can write, members cannot write
- `#forge-approvals` — only you (server owner) can read and react; bot can write

### Get IDs
Enable Discord Developer Mode (Settings → Advanced), then:
- Right-click server icon → Copy Server ID
- Right-click your avatar → Copy User ID
- Right-click `#forge-reports` → Copy Channel ID
- Right-click `#forge-approvals` → Copy Channel ID

### forge.env
```bash
export FORGE_DISCORD_TOKEN=""
export FORGE_DISCORD_GUILD_ID=""

export FORGE_DISCORD_REPORTS_CHANNEL="forge-reports"
export FORGE_DISCORD_REPORTS_CHANNEL_ID=""
export FORGE_DISCORD_APPROVALS_CHANNEL="forge-approvals"
export FORGE_DISCORD_APPROVALS_CHANNEL_ID=""

export FORGE_DISCORD_OWNER_ID=""
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
          "name": "Qwen3.6-35B-A3B:planning",
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
./forge.sh --repo anvilml                    # run from next unblocked task
./forge.sh --repo anvilml --dry-run          # show what would run
./forge.sh --repo anvilml --task P1-A3       # run one specific task
./forge.sh --repo anvilml --list             # show DAG status and exit
./forge.sh --repo anvilml --phase 4          # load phases 1–4
./forge.sh --repo anvilml --reset-task P1-A3      # reset task state (no git)
./forge.sh --repo anvilml --reset-task-git P1-A3  # reset task state + git

# ── Full monitoring view (tmux) ───────────────────────────────────
./forge_monitor.sh --repo anvilml

# ── Management ────────────────────────────────────────────────────
./forge_manage.sh --repo anvilml             # full status table
./forge_manage.sh --repo anvilml --unblock   # ready-to-run only
```

**Resume after any interruption:** just re-run `./forge.sh --repo <project>`.
State is written to disk before every external action — nothing is lost.

---

## Monitoring a live session

```bash
tail -f logs/opencode.log    # agent tool calls, prose output, session summary
tail -f logs/context.log     # live context window usage (updates per step)
tail -f logs/forge.log       # orchestrator decisions, approvals, errors
tail -f logs/compaction.log  # OpenCode auto-compaction events
```

Or use the integrated tmux view, which arranges these streams automatically:

```bash
./forge_monitor.sh --repo anvilml
```

tmux layout:

```
┌─────────────────────────────────────────────────────────┐
│                                                         │
│  tail -f logs/opencode.log                              │
│  (OpenCode tool calls and output)                       │
│                                                         │
├───────────────────────┬─────────────────────────────────┤
│ forge.sh (status)     │ watch -n1 cat logs/context.log  │
│                       │ (context window usage %)        │
└───────────────────────┴─────────────────────────────────┘
```

Per-task `opencode.log` snapshots are archived to `logs/traces/<TASK-ID>_opencode.log`
after each completed task cycle.

---

## Task management

`forge_manage.sh` provides status inspection and state mutations without running
the full orchestrator. Safe to use while `forge.sh` is running.

```bash
./forge_manage.sh --repo anvilml                   # full status table
./forge_manage.sh --repo anvilml --unblock          # ready-to-run only
./forge_manage.sh --repo anvilml --phase 4          # limit view to phases 1–4
./forge_manage.sh --repo anvilml --complete P4-A3   # manually mark complete
./forge_manage.sh --repo anvilml --fail P4-A3       # mark as failed
./forge_manage.sh --repo anvilml --reset P4-A3      # reset to unstarted
./forge_manage.sh --repo anvilml --review P4-A3     # mark needs-review
./forge_manage.sh --repo anvilml --clear-failed     # reset all failed
./forge_manage.sh --repo anvilml --clear-review     # reset all needs-review
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
<repo>/.forge/reports/<TASK-ID>_plan.md         # written by forge-plan agent
<repo>/.forge/reports/<TASK-ID>_implement.md    # written by forge-act agent
```

The Forge reads these files to populate the Discord posts. Both are committed to the
repository as part of the post-approval commit, so the full build history is in git.

---

## Handling failures

### llama.cpp crash or timeout
Automatically retried up to `FORGE_OPENCODE_RETRIES` times (default: 3) with
increasing delays. A warning is posted to `#forge-approvals` on each retry.
If all retries fail, the task is marked `failed` and The Forge stops.

### Retry a failed task with clean repo
```bash
# Hard-reset repo to origin/<branch>, then retry
./forge.sh --repo anvilml --reset-task-git P1-A3

# Or reset state only (keep whatever the agent wrote locally, inspect first)
./forge.sh --repo anvilml --reset-task P1-A3
./forge.sh --repo anvilml --task P1-A3
```

`--reset-task-git` runs `git fetch origin <branch> && git reset --hard origin/<branch>`
on each repo the task touches, plus `git clean -fd` for untracked files.

---

## Configuration reference

All variables are optional; built-in defaults apply when unset.
Set them in `forge.env` — they are sourced by the shell scripts before launch.

### Discord

| Variable | Purpose | Default |
|---|---|---|
| `FORGE_DISCORD_TOKEN` | Bot token | (required for Discord) |
| `FORGE_DISCORD_GUILD_ID` | Server ID | (required for Discord) |
| `FORGE_DISCORD_REPORTS_CHANNEL` | Reports channel name | `forge-reports` |
| `FORGE_DISCORD_REPORTS_CHANNEL_ID` | Reports channel ID | (required for Discord) |
| `FORGE_DISCORD_APPROVALS_CHANNEL` | Approvals channel name | `forge-approvals` |
| `FORGE_DISCORD_APPROVALS_CHANNEL_ID` | Approvals channel ID | (required for Discord) |
| `FORGE_DISCORD_OWNER_ID` | Your Discord user ID (approval gate) | (required for Discord) |

### OpenCode

| Variable | Purpose | Default |
|---|---|---|
| `FORGE_OPENCODE_BIN` | Path to opencode binary | `opencode` |
| `FORGE_OPENCODE_TIMEOUT` | Max seconds per OpenCode session | `7200` (120 min) |
| `FORGE_OPENCODE_RETRIES` | Retry count on llama.cpp crash | `3` |
| `FORGE_OPENCODE_RETRY_DELAY` | Base seconds between retries | `60` |
| `FORGE_CONTEXT_WINDOW` | Model context window (tokens) | `262144` (256k) |

### Models

| Variable | Purpose | Default |
|---|---|---|
| `FORGE_MODEL_PLANNING` | Model ID for PLAN sessions | `llama.cpp/Qwen3.6-35B-A3B:planning` |
| `FORGE_MODEL_CODING` | Model ID for ACT sessions | `llama.cpp/Qwen3.6-35B-A3B:coding` |

### Approval polling

| Variable | Purpose | Default |
|---|---|---|
| `FORGE_POLL_INTERVAL` | Seconds between Discord reaction polls | `10` |
| `FORGE_APPROVAL_TIMEOUT` | Approval timeout in seconds | `86400` (24 h) |