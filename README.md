# Forge — SindriStudio Autonomous Development Orchestrator

The Forge drives Cline through the SindriStudio build plan one atomic task at a time,
with Discord approval gates and full resume capability after interruptions or llama.cpp failures.

---

## Discord channel roles

Two channels, clearly separated:

| Channel | Role | Who reads | Forge behaviour |
|---|---|---|---|
| **#forge-reports** | Public broadcast | Anyone | Posts plan reports and implementation reports. **Never polled. Never acts on reactions here.** |
| **#forge-approvals** | Server-owner only | You | All approval requests. **Only channel the Forge polls for reactions.** |

### Workflow per task

1. Cline generates a plan → Forge posts **plan report** to `#forge-reports`
2. Forge posts **plan approval request** (with task ID) to `#forge-approvals`
3. You read the plan in `#forge-reports`, then approve/reject in `#forge-approvals`
4. On approve → Cline implements, tests, commits, pushes
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
├── forge.py           # Main orchestrator
├── forge_status.py    # Status and management CLI
├── forge_setup.sh     # One-time setup script
├── forge.env          # Your local environment config (gitignored)
├── discord_mcp.py     # Optional MCP server for Cline↔Discord integration
├── .clinerules        # Cline session rules (copy to SindriStudio root)
├── tasks.json         # Task DAG — Phase 1 (39 tasks) and Phase 2 (23 tasks)
├── state.json         # Runtime state — auto-managed, never edit by hand
└── README.md          # This file
```

---

## First-time setup

```bash
bash forge/forge_setup.sh
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

## Cline configuration (llama.cpp)

```bash
cline config set act-mode-api-provider openai-compatible
cline config set act-mode-openai-base-url http://localhost:11434/v1
cline config set act-mode-openai-api-key dummy
cline config set act-mode-openai-model-id qwen3

cline config set plan-mode-api-provider openai-compatible
cline config set plan-mode-openai-base-url http://localhost:11434/v1
cline config set plan-mode-openai-api-key dummy
cline config set plan-mode-openai-model-id qwen3

cline config set auto-approval-settings.enabled true
```

---

## Running

```bash
source forge/forge.env

python forge/forge_status.py              # check task status
python forge/forge.py                     # run from next unblocked task
python forge/forge.py --dry-run           # test without executing Cline
python forge/forge.py --task P1-A3        # force a specific task
python forge/forge.py --list              # show DAG status and exit
```

**Resume after any interruption:** just re-run `python forge/forge.py`.
State is written to disk before every external action — nothing is lost.

---

## Report files on disk

Every task produces a markdown report at:
```
SindriStudio/.cline/reports/{TASK-ID}.md
```

This file is written by Cline in STEP 1 (plan section) and finalized in STEP 4
(implementation, test results, commit hashes). It is committed to the root repo
as part of STEP 4's `git add -A`, so the full build history is in git.

The Forge reads this file to populate the Discord posts — it is the authoritative
source for both the plan report and the implementation report.

---

## Handling failures

### llama.cpp crash or timeout
Automatically retried up to `FORGE_CLINE_RETRIES` times (default: 3) with
increasing delays. A warning is posted to `#forge-approvals` on each retry.
If all retries fail, the task is marked `failed` and the Forge stops.

### Retry a failed task with clean repos
```bash
# Hard-reset all touched repos to origin/develop, then retry
python forge/forge.py --reset-task-git P1-A3

# Or reset state only (keep whatever Cline wrote locally, inspect first)
python forge/forge.py --reset-task P1-A3
python forge/forge.py --task P1-A3
```

`--reset-task-git` runs `git fetch origin develop && git reset --hard origin/develop`
on each repo the task touches, plus `git clean -fd` for untracked files. This
ensures the retry starts from a known-good codebase with no half-written code.

### When to use --reset-task-git vs --reset-task

| Situation | Command |
|---|---|
| Cline failed before writing any files | `--reset-task` is enough |
| Cline wrote partial files, no commits | `--reset-task-git` (cleans working tree) |
| Cline made local commits, not pushed | `--reset-task-git` (resets to origin) |
| Cline pushed but push approval rejected | neither — task is `needs_review`; inspect manually |

### Task needs review (push rejected)
```bash
python forge/forge_status.py              # see what's in review

# After reviewing git log and deciding it's acceptable:
python forge/forge_status.py --complete P1-A3

# After deciding it needs a full redo:
python forge/forge.py --reset-task-git P1-A3
python forge/forge.py --task P1-A3
```

---

## Optional: Discord MCP for Cline

`discord_mcp.py` lets Cline post to Discord from within its own session.
Register with Cline:
```bash
cline mcp add discord \
  --command "python forge/discord_mcp.py" \
  --env FORGE_DISCORD_TOKEN=$FORGE_DISCORD_TOKEN \
  --env FORGE_DISCORD_GUILD_ID=$FORGE_DISCORD_GUILD_ID
```

Tools: `discord_send_message`, `discord_send_embed`, `discord_add_reaction`,
`discord_check_approval`, `discord_list_channels`, `discord_get_channel_id`.

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `FORGE_DISCORD_TOKEN` | — | Discord bot token (required) |
| `FORGE_DISCORD_GUILD_ID` | — | Discord server ID (required) |
| `FORGE_DISCORD_REPORTS_CHANNEL` | `forge-reports` | Broadcast channel (plan + impl reports) |
| `FORGE_DISCORD_APPROVALS_CHANNEL` | `forge-approvals` | Approval channel (polled for reactions) |
| `FORGE_CLINE_BIN` | `cline` | Path to Cline binary |
| `FORGE_CLINE_TIMEOUT` | `5400` | Max seconds per Cline session (90 min) |
| `FORGE_CLINE_RETRIES` | `3` | Retry attempts on Cline failure |
| `FORGE_CLINE_RETRY_DELAY` | `60` | Base seconds between retries (×attempt number) |
| `FORGE_POLL_INTERVAL` | `10` | Seconds between Discord reaction polls |
| `FORGE_APPROVAL_TIMEOUT` | `86400` | Max seconds to wait for approval (24h) |
