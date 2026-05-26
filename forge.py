#!/usr/bin/env python3
"""
forge.py — SindriStudio Autonomous Development Orchestrator

Drives atomic Cline CLI sessions through the 4-step plan/implement/test/commit
cycle defined in .clinerules, with Discord approval gates and full resume
capability after any interruption or llama.cpp failure.

Discord channel roles:
  #forge-reports   — PUBLIC broadcast only. Plan reports and implementation
                     reports are posted here for anyone to read. The Forge
                     NEVER polls reactions here and NEVER acts on anything here.
  #forge-approvals — SERVER-OWNER only. All approval requests go here.
                     The Forge ONLY polls reactions in this channel.
                     Each approval message includes the task ID so you can
                     cross-reference with the matching report in #forge-reports.

Usage:
    python forge.py                    # run from next unblocked task
    python forge.py --task P1-A3       # force-start a specific task
    python forge.py --dry-run          # show what would run, no execution
    python forge.py --list             # show task DAG status and exit
    python forge.py --reset-task P1-A3 # reset a task to unstarted
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote

import requests  # pip install requests

# Emoji constants — raw Unicode, encoded at call sites via _encode_emoji()
EMOJI_APPROVE = "✅"
EMOJI_REJECT  = "❌"

def _encode_emoji(emoji: str) -> str:
    """
    Normalise an emoji for use in a Discord reaction URL path segment.
    Decodes any existing percent-encoding first to avoid double-encoding,
    then re-encodes the raw Unicode character to percent-encoded UTF-8.
    Custom guild emoji in name:id format is passed through unchanged.
    """
    if "%" in emoji:
        emoji = unquote(emoji)
    if ":" in emoji:
        return emoji
    return quote(emoji, safe="")

# ─── Configuration ────────────────────────────────────────────────────────────

FORGE_DIR = Path(__file__).parent.resolve()
REPO_ROOT = FORGE_DIR.parent  # SindriStudio root
STATE_FILE = FORGE_DIR / "state.json"
TASKS_FILE = FORGE_DIR / "tasks.json"
LOG_FILE = FORGE_DIR / "forge.log"

# Discord — bot token and guild ID still come from environment (secrets)
DISCORD_BOT_TOKEN = os.environ.get("FORGE_DISCORD_TOKEN", "")
DISCORD_GUILD_ID  = os.environ.get("FORGE_DISCORD_GUILD_ID", "")

# Channel IDs — hardcoded, no env var fallback needed
# #forge-reports   : public broadcast, never polled
# #forge-approvals : owner-only, all approval requests go here
DISCORD_REPORTS_CHANNEL_ID   = "1508515907952054323"
DISCORD_APPROVALS_CHANNEL_ID = "1508488060298334229"

# Owner gate — only reactions from this Discord user ID are acted upon.
# User IDs are permanent; usernames can be changed.
FORGE_OWNER_ID = "334811986019745792"

# Cline
CLINE_BIN = os.environ.get("FORGE_CLINE_BIN", "cline")
CLINE_TIMEOUT = int(os.environ.get("FORGE_CLINE_TIMEOUT", str(60 * 90)))  # 90 min
CLINE_RETRIES = int(os.environ.get("FORGE_CLINE_RETRIES", "3"))
CLINE_RETRY_DELAY = int(os.environ.get("FORGE_CLINE_RETRY_DELAY", "60"))

# Approval
APPROVAL_POLL_INTERVAL = int(os.environ.get("FORGE_POLL_INTERVAL", "10"))
APPROVAL_TIMEOUT = int(os.environ.get("FORGE_APPROVAL_TIMEOUT", str(60 * 60 * 24)))  # 24h

APPROVE_EMOJI = "✅"
REJECT_EMOJI = "❌"

# ─── Logging ──────────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def log(msg: str, level: str = "INFO") -> None:
    line = f"[{_ts()}] [{level}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def log_err(msg: str) -> None:
    log(msg, "ERROR")

def log_warn(msg: str) -> None:
    log(msg, "WARN")

# ─── State management ─────────────────────────────────────────────────────────

DEFAULT_STATE = {
    "completed": [],
    "in_progress": None,
    "plan_approved": False,
    "current_plan": None,
    "failed": [],
    "needs_review": [],
    "last_updated": None,
    # IDs of the approval request messages in #forge-approvals (polled for reactions)
    "plan_approval_message_id": None,
    "push_approval_message_id": None,
    # IDs of the report messages in #forge-reports (broadcast only, never polled)
    "plan_report_message_id": None,
    "impl_report_message_id": None,
}

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as e:
            log_err(f"Failed to load state: {e} — using default")
    return dict(DEFAULT_STATE)

def save_state(state: dict) -> None:
    state["last_updated"] = _ts()
    # Atomic write
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)

# ─── Task DAG ─────────────────────────────────────────────────────────────────

def load_tasks() -> list[dict]:
    if not TASKS_FILE.exists():
        log_err(f"tasks.json not found at {TASKS_FILE}")
        sys.exit(1)
    return json.loads(TASKS_FILE.read_text())

def build_dag(tasks: list[dict]) -> dict[str, dict]:
    return {t["id"]: t for t in tasks}

def find_next_task(tasks: list[dict], state: dict) -> Optional[dict]:
    """Return the first unblocked task not yet completed or failed."""
    completed = set(state["completed"])
    failed = set(state["failed"])
    needs_review = set(state["needs_review"])
    blocked = failed | needs_review

    for task in tasks:
        tid = task["id"]
        if tid in completed or tid in blocked:
            continue
        if tid == state.get("in_progress"):
            # Resume in-progress task
            return task
        prereqs = set(task.get("prereqs", []))
        if prereqs.issubset(completed):
            return task
    return None

def print_dag_status(tasks: list[dict], state: dict) -> None:
    completed = set(state["completed"])
    failed = set(state["failed"])
    needs_review = set(state["needs_review"])
    in_progress = state.get("in_progress")

    print(f"\n{'Task':<12} {'Status':<14} {'Description'}")
    print("─" * 80)
    for task in tasks:
        tid = task["id"]
        if tid in completed:
            status = "✅ complete"
        elif tid == in_progress:
            status = "⚙️  in progress"
        elif tid in failed:
            status = "❌ failed"
        elif tid in needs_review:
            status = "🔍 needs review"
        else:
            prereqs = set(task.get("prereqs", []))
            if prereqs.issubset(completed):
                status = "⬜ unblocked"
            else:
                status = "⏸  blocked"
        print(f"{tid:<12} {status:<14} {task['description']}")
    print()

# ─── Discord client ───────────────────────────────────────────────────────────

class DiscordClient:
    BASE = "https://discord.com/api/v10"

    def __init__(self, token: str):
        self.headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}

    def _get(self, path: str) -> Optional[dict]:
        try:
            r = requests.get(f"{self.BASE}{path}", headers=self.headers, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log_warn(f"Discord GET {path} failed: {e}")
            return None

    def _post(self, path: str, payload: dict) -> Optional[dict]:
        try:
            r = requests.post(f"{self.BASE}{path}", headers=self.headers,
                              json=payload, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log_warn(f"Discord POST {path} failed: {e}")
            return None

    def get_channel_id(self, guild_id: str, channel_name: str) -> Optional[str]:
        channels = self._get(f"/guilds/{guild_id}/channels")
        if not channels:
            return None
        for ch in channels:
            if ch.get("name") == channel_name:
                return ch["id"]
        log_warn(f"Discord channel #{channel_name} not found in guild {guild_id}")
        return None

    def send_message(self, channel_id: str, content: str,
                     embeds: Optional[list] = None) -> Optional[str]:
        payload: dict = {}
        # Discord messages: content max 2000 chars, embed description max 4096
        if len(content) <= 2000:
            payload["content"] = content
        else:
            # Overflow into embed
            payload["embeds"] = [{"description": content[:4096]}]

        if embeds:
            payload["embeds"] = embeds

        result = self._post(f"/channels/{channel_id}/messages", payload)
        return result["id"] if result else None

    def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> bool:
        """Add a reaction. emoji can be raw Unicode, percent-encoded, or name:id."""
        try:
            encoded = _encode_emoji(emoji)
            r = requests.put(
                f"{self.BASE}/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me",
                headers={k: v for k, v in self.headers.items() if k != "Content-Type"},
                timeout=10,
            )
            if r.status_code not in (200, 204):
                log_warn(f"Discord add_reaction HTTP {r.status_code} for emoji {encoded!r}"
                         f" — 403=missing permission, 400=unknown emoji")
            return r.status_code in (200, 204)
        except Exception as e:
            log_warn(f"Discord add_reaction failed: {e}")
            return False

    def get_reactions(self, channel_id: str, message_id: str, emoji: str) -> list[dict]:
        result = self._get(
            f"/channels/{channel_id}/messages/{message_id}/reactions/{_encode_emoji(emoji)}"
        )
        return result if isinstance(result, list) else []

    def get_message(self, channel_id: str, message_id: str) -> Optional[dict]:
        return self._get(f"/channels/{channel_id}/messages/{message_id}")

    def get_recent_messages(self, channel_id: str, after_id: str,
                            limit: int = 10) -> list[dict]:
        result = self._get(
            f"/channels/{channel_id}/messages?after={after_id}&limit={limit}"
        )
        return result if isinstance(result, list) else []


def get_discord() -> Optional["DiscordClient"]:
    if not DISCORD_BOT_TOKEN:
        log_warn("FORGE_DISCORD_TOKEN not set — Discord notifications disabled")
        return None
    return DiscordClient(DISCORD_BOT_TOKEN)

# ─── Git helpers ──────────────────────────────────────────────────────────────

def get_recent_commits(repo_path: Path, count: int = 5) -> list[str]:
    """Return the last N commit one-liners from a repo."""
    try:
        result = subprocess.run(
            ["git", "log", f"-{count}", "--oneline"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if result.returncode == 0:
            return [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
    except Exception as e:
        log_warn(f"git log failed in {repo_path}: {e}")
    return []

def get_changed_files(repo_path: Path) -> list[str]:
    """Return list of files changed in HEAD commit."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if result.returncode == 0:
            return [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
    except Exception:
        pass
    return []

def has_unpushed_commits(repo_path: Path) -> bool:
    """Return True if the local branch is ahead of origin/develop."""
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "origin/develop..HEAD"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if result.returncode == 0:
            count = int(result.stdout.strip() or "0")
            return count > 0
    except Exception:
        pass
    return False

def has_dirty_working_tree(repo_path: Path) -> bool:
    """Return True if there are uncommitted changes."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path, capture_output=True, text=True,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False

def reset_repo_to_origin(repo_path: Path, repo_label: str) -> bool:
    """
    Hard-reset repo to origin/develop, discarding all local commits
    and unstaged changes. Returns True on success.
    """
    log(f"[git] Resetting {repo_label} to origin/develop...")
    try:
        # Fetch first to ensure origin/develop is current
        fetch = subprocess.run(
            ["git", "fetch", "origin", "develop"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if fetch.returncode != 0:
            log_warn(f"[git] fetch failed in {repo_label}: {fetch.stderr}")

        reset = subprocess.run(
            ["git", "reset", "--hard", "origin/develop"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if reset.returncode == 0:
            log(f"[git] {repo_label} reset to origin/develop: {reset.stdout.strip()}")
            return True
        else:
            log_err(f"[git] reset failed in {repo_label}: {reset.stderr}")
            return False
    except Exception as e:
        log_err(f"[git] reset exception in {repo_label}: {e}")
        return False

def clean_repo_working_tree(repo_path: Path, repo_label: str) -> bool:
    """Remove untracked files and directories (git clean -fd)."""
    try:
        result = subprocess.run(
            ["git", "clean", "-fd"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if result.returncode == 0:
            log(f"[git] {repo_label} working tree cleaned")
            return True
        log_warn(f"[git] clean failed in {repo_label}: {result.stderr}")
        return False
    except Exception as e:
        log_warn(f"[git] clean exception in {repo_label}: {e}")
        return False

def revert_task_repos(task: dict) -> bool:
    """
    Reset all repos touched by this task to origin/develop.
    Called before retrying a failed task to ensure a clean codebase.
    Returns True if all resets succeeded.
    """
    repos = task.get("repos", ["root"])
    repo_paths = {
        "root": REPO_ROOT,
        "anvilml": REPO_ROOT / "backend",
        "bloomeryui": REPO_ROOT / "frontend",
    }

    all_ok = True
    for repo_key in repos:
        path = repo_paths.get(repo_key)
        if not path or not path.exists():
            log_warn(f"[git] repo path for '{repo_key}' not found, skipping")
            continue

        dirty = has_dirty_working_tree(path)
        unpushed = has_unpushed_commits(path)

        if not dirty and not unpushed:
            log(f"[git] {repo_key}: clean, nothing to reset")
            continue

        if unpushed:
            log_warn(f"[git] {repo_key}: has {' unpushed commits' if unpushed else ''}"
                     f"{' and dirty tree' if dirty else ''} — resetting")

        ok = reset_repo_to_origin(path, repo_key)
        if ok and dirty:
            clean_repo_working_tree(path, repo_key)
        all_ok = all_ok and ok

    return all_ok

def collect_commit_info(task: dict) -> dict:
    """Collect commit info from all repos the task touches."""
    info = {}
    repos = task.get("repos", ["root"])
    repo_paths = {
        "root": REPO_ROOT,
        "anvilml": REPO_ROOT / "backend",
        "bloomeryui": REPO_ROOT / "frontend",
    }
    for repo_key in repos:
        path = repo_paths.get(repo_key)
        if path and path.exists():
            commits = get_recent_commits(path)
            changed = get_changed_files(path)
            info[repo_key] = {"commits": commits, "changed_files": changed}
    return info

# ─── Disk report files ────────────────────────────────────────────────────────

def write_forge_plan_report(task: dict, plan_text: str, attempt: int) -> Path:
    """
    Write the plan report for a task to .cline/reports/{TASK-ID}.md.
    Cline also writes to this path in STEP 1 — this ensures the plan
    is on disk even if we're reading from a previous session's output.
    The file is committed to git as part of STEP 4's 'git add -A'.
    Returns the report path.
    """
    reports_dir = REPO_ROOT / ".cline" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{task['id']}.md"

    # Only write the forge-level header if the file doesn't already exist
    # (Cline writes the full report in STEP 1; we don't overwrite it)
    if not report_path.exists():
        header = (
            f"# Task Report: {task['id']}\n"
            f"{task['description']}\n"
            f"Phase: {task.get('phase', '?')}\n"
            f"Forge plan attempt: {attempt}\n\n"
            f"## Plan\n\n"
            f"{plan_text}\n"
        )
        report_path.write_text(header)
        log(f"[{task['id']}] Plan report written to {report_path}")
    else:
        log(f"[{task['id']}] Plan report already exists at {report_path} (written by Cline)")

    return report_path

def read_report_file(task_id: str) -> str:
    """Read the full report file for a task, or return empty string."""
    report_path = REPO_ROOT / ".cline" / "reports" / f"{task_id}.md"
    if report_path.exists():
        return report_path.read_text()
    return ""

def extract_plan_section(report_text: str, task_id: str) -> str:
    """Extract the ## Plan section from a task report."""
    if not report_text:
        return f"[Plan report not yet written for {task_id}]"

    match = re.search(r"## Plan\n(.*?)(?=^##|\Z)", report_text, re.DOTALL | re.MULTILINE)
    if match:
        return match.group(0).strip()

    # Return first 3000 chars of whatever the report contains
    return report_text[:3000]

# ─── Discord message formatting ───────────────────────────────────────────────

def format_report_broadcast(task: dict, section: str, content: str) -> str:
    """
    Format a message for #forge-reports (broadcast, no approval).
    section: 'PLAN' or 'IMPLEMENTATION'
    """
    tid = task["id"]
    desc = task["description"]
    phase = task.get("phase", "?")

    header = f"**📋 {section} REPORT — Task `{tid}` (Phase {phase})**"
    body = content[:3800] if len(content) > 3800 else content

    return (
        f"{header}\n"
        f"**{desc}**\n"
        f"*See #forge-approvals for the matching approval request.*\n\n"
        f"```markdown\n{body}\n```"
    )

def format_plan_approval_request(task: dict, plan_text: str, attempt: int,
                                  feedback: str = "") -> str:
    """
    Format an approval request for #forge-approvals.
    Includes a clear reference to the task ID for cross-referencing
    with the plan report posted in #forge-reports.
    """
    tid = task["id"]
    desc = task["description"]
    prereqs = ", ".join(task.get("prereqs", [])) or "none"
    repos = ", ".join(task.get("repos", ["root"]))

    header = f"**🔐 PLAN APPROVAL REQUEST — Task `{tid}`**"
    if attempt > 1:
        header += f" *(Revision {attempt})*"

    summary = plan_text[:1200] if len(plan_text) > 1200 else plan_text

    parts = [
        header,
        f"",
        f"**Description:** {desc}",
        f"**Prerequisites:** {prereqs}",
        f"**Repos:** {repos}",
        f"",
        f"*Full plan report is in #forge-reports — search for `{tid}`.*",
    ]

    if feedback:
        parts += ["", f"**📝 Revision feedback applied:** {feedback}"]

    parts += [
        "",
        f"**Plan summary:**",
        f"```markdown",
        summary,
        f"```",
        "",
        f"✅ **React to approve** — Cline will proceed to implementation.",
        f"❌ **React to reject** — Reply with feedback, then react ❌.",
    ]

    return "\n".join(parts)

def format_push_approval_request(task: dict, commit_info: dict) -> str:
    """
    Format a push approval request for #forge-approvals.
    Includes task ID for cross-referencing with #forge-reports.
    """
    tid = task["id"]
    desc = task["description"]

    parts = [
        f"**🔐 PUSH APPROVAL REQUEST — Task `{tid}`**",
        f"",
        f"**Description:** {desc}",
        f"*Full implementation report is in #forge-reports — search for `{tid}`.*",
        f"",
    ]

    for repo, info in commit_info.items():
        if info.get("commits"):
            parts.append(f"**{repo} commits:**")
            for c in info["commits"][:3]:
                parts.append(f"  `{c}`")
        if info.get("changed_files"):
            files = info["changed_files"][:8]
            parts.append(f"**{repo} files:** {', '.join(files)}")
        parts.append("")

    parts += [
        f"✅ **React to confirm** — task marked complete.",
        f"❌ **React to reject** — task marked needs-review, no further action.",
    ]

    return "\n".join(parts)

def format_implementation_report_broadcast(task: dict, commit_info: dict,
                                            report_text: str) -> str:
    """Format the implementation report for #forge-reports (broadcast, no approval)."""
    tid = task["id"]
    desc = task["description"]

    parts = [
        f"**📋 IMPLEMENTATION REPORT — Task `{tid}`**",
        f"**{desc}**",
        f"*Push approval request is in #forge-approvals.*",
        f"",
    ]

    for repo, info in commit_info.items():
        if info.get("commits"):
            parts.append(f"**{repo} commits:**")
            for c in info["commits"][:3]:
                parts.append(f"  `{c}`")
        if info.get("changed_files"):
            files = info["changed_files"][:10]
            parts.append(f"**{repo} files changed:** {', '.join(files)}")
        parts.append("")

    if report_text:
        # Extract the test results section for the broadcast
        test_match = re.search(
            r"## Test Results\n(.*?)(?=^##|\Z)", report_text, re.DOTALL | re.MULTILINE
        )
        if test_match:
            excerpt = test_match.group(0)[:1200]
            parts += [f"**Test results excerpt:**", f"```", excerpt, f"```"]

    return "\n".join(parts)

# ─── Approval flow ────────────────────────────────────────────────────────────

def wait_for_approval(
    dc: Optional["DiscordClient"],
    approvals_channel_id: str,
    message_id: str,
    timeout: int = APPROVAL_TIMEOUT,
) -> tuple[bool, str]:
    """
    Poll for ✅ or ❌ reaction on message_id in #forge-approvals.
    Returns (approved: bool, feedback: str).

    Only reactions from FORGE_OWNER_ID are acted upon. Any reaction from
    a different user ID is logged and ignored — the poll continues.
    """
    if dc is None:
        log_warn("Discord not configured — auto-approving")
        return True, ""

    log(f"Waiting for approval on message {message_id} (owner: {FORGE_OWNER_ID}, timeout {timeout}s)...")
    deadline = time.monotonic() + timeout
    last_reminder = time.monotonic()

    while time.monotonic() < deadline:
        time.sleep(APPROVAL_POLL_INTERVAL)

        # Check ✅ — only count if it's from the owner
        approvers = dc.get_reactions(approvals_channel_id, message_id, EMOJI_APPROVE)
        for u in approvers:
            if u.get("bot", False):
                continue
            if u.get("id") == FORGE_OWNER_ID:
                log(f"✅ Approved by owner ({u.get('username', 'unknown')})")
                return True, ""
            else:
                log_warn(f"⚠️  Ignoring ✅ from non-owner user {u.get('id')} ({u.get('username', '?')})")

        # Check ❌ — only count if it's from the owner
        rejectors = dc.get_reactions(approvals_channel_id, message_id, EMOJI_REJECT)
        for u in rejectors:
            if u.get("bot", False):
                continue
            if u.get("id") == FORGE_OWNER_ID:
                log(f"❌ Rejected by owner ({u.get('username', 'unknown')})")
                # Look for a reply with feedback (messages after the approval request)
                feedback = ""
                recent = dc.get_recent_messages(approvals_channel_id, message_id, limit=5)
                for msg in sorted(recent, key=lambda m: m.get("id", "0")):
                    author = msg.get("author", {})
                    if not author.get("bot", False) and author.get("id") == FORGE_OWNER_ID:
                        feedback = msg.get("content", "").strip()
                        break
                return False, feedback
            else:
                log_warn(f"⚠️  Ignoring ❌ from non-owner user {u.get('id')} ({u.get('username', '?')})")

        if time.monotonic() - last_reminder > 300:
            last_reminder = time.monotonic()
            log("Still waiting for owner approval in #forge-approvals...")

    log_warn(f"Approval timeout after {timeout}s")
    return False, "Approval timed out — no response received"

# ─── Cline subprocess ─────────────────────────────────────────────────────────

def build_cline_cmd(prompt: str, plan_mode: bool, cwd: Path) -> list[str]:
    cmd = [CLINE_BIN]
    if plan_mode:
        cmd.append("-p")
    cmd.extend([
        "--json",
        "--auto-approve=true",
        "--timeout", str(CLINE_TIMEOUT),
        "--cwd", str(cwd),
    ])
    cmd.append(prompt)
    return cmd

def run_cline(
    prompt: str,
    plan_mode: bool,
    cwd: Path,
    task_id: str,
    dc: Optional["DiscordClient"],
    approvals_channel_id: Optional[str],
    attempt_number: int = 1,
) -> tuple[bool, str]:
    """
    Run Cline CLI with retry logic for llama.cpp failures.
    Returns (success: bool, output_text: str).
    Notifies #forge-approvals (not #forge-reports) on retry — approvals channel
    is owner-only and the right place for operational alerts.
    """
    cmd = build_cline_cmd(prompt, plan_mode, cwd)
    mode_label = "PLAN" if plan_mode else "ACT"
    log(f"[{task_id}] Running Cline {mode_label} mode (timeout {CLINE_TIMEOUT}s, attempt {attempt_number})")

    for attempt in range(1, CLINE_RETRIES + 1):
        if attempt > 1:
            delay = CLINE_RETRY_DELAY * attempt
            msg = (f"⚠️ `{task_id}` Cline {mode_label} attempt {attempt}/{CLINE_RETRIES} "
                   f"— waiting {delay}s (llama.cpp may have crashed)")
            log_warn(msg)
            if dc and approvals_channel_id:
                dc.send_message(approvals_channel_id, msg)
            time.sleep(delay)

        text_output: list[str] = []

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            for line in proc.stdout:
                line = line.rstrip()
                try:
                    event = json.loads(line)
                    if event.get("type") == "agent_event" and event.get("event", {}).get("text"):
                        text_output.append(event["event"]["text"])
                    elif event.get("type") == "text":
                        text_output.append(event.get("text", ""))
                except json.JSONDecodeError:
                    text_output.append(line)

            _, stderr = proc.communicate(timeout=30)
            exit_code = proc.returncode

        except subprocess.TimeoutExpired:
            proc.kill()
            log_err(f"[{task_id}] Cline {mode_label} timed out after {CLINE_TIMEOUT}s")
            exit_code = -1
            stderr = "process timed out"
        except FileNotFoundError:
            log_err(f"Cline binary not found: {CLINE_BIN}")
            log_err("Install with: npm install -g cline")
            sys.exit(1)

        full_output = "\n".join(text_output)

        if exit_code == 0:
            log(f"[{task_id}] Cline {mode_label} completed successfully")
            return True, full_output

        log_err(f"[{task_id}] Cline {mode_label} exited with code {exit_code}")
        if stderr:
            log_err(f"[{task_id}] stderr: {stderr[-500:]}")

    return False, full_output

# ─── Task prompt builders ─────────────────────────────────────────────────────

def build_task_prompt(task: dict) -> str:
    """Build the prompt injected into Cline for the plan phase."""
    tid = task["id"]
    desc = task["description"]
    context = task.get("context", "")
    phase = task.get("phase", "1")

    prompt = f"SindriStudio Task: {tid}\nDescription: {desc}\nPhase: {phase}\n\n"

    if context:
        prompt += f"Context:\n{context}\n\n"

    prompt += (
        f"Instructions:\n"
        f"1. Read .cline/state/CURRENT_TASK.md first\n"
        f"2. Read docs/ENVIRONMENT.md and docs/ARCHITECTURE.md\n"
        f"3. Read docs/TASKS_PHASE{phase}.md and find task {tid}\n"
        f"4. Execute STEP 1 — PLAN only:\n"
        f"   - Write .cline/reports/{tid}.md plan section\n"
        f"   - Update .cline/state/CURRENT_TASK.md Step = 1-PLAN Status = COMPLETE\n"
        f"   - Do NOT write any application or test code\n"
        f"5. After the plan is written: STOP. Do not proceed to STEP 2.\n"
        f"   The Forge orchestrator will resume this session after plan approval.\n"
    )
    return prompt

def build_act_prompt(task: dict, approved_plan: str) -> str:
    """Build the Act phase prompt with the approved plan injected."""
    tid = task["id"]
    desc = task["description"]
    phase = task.get("phase", "1")

    return (
        f"SindriStudio Task: {tid}\nDescription: {desc}\nPhase: {phase}\n\n"
        f"The plan below has been APPROVED by the project owner.\n"
        f"Proceed directly to STEP 2 — IMPLEMENT. Do not re-plan.\n\n"
        f"APPROVED PLAN:\n{approved_plan}\n\n"
        f"Instructions:\n"
        f"1. STEP 2 — IMPLEMENT: Write all code and tests as specified in the plan\n"
        f"2. STEP 3 — TEST: Run all tests, fix any failures, zero failures required\n"
        f"3. STEP 4 — COMMIT: git commit + push, finalize .cline/reports/{tid}.md,\n"
        f"   update .cline/state/CURRENT_TASK.md, call new_task, STOP\n"
    )

# ─── Task execution ───────────────────────────────────────────────────────────

def execute_task(
    task: dict,
    state: dict,
    dc: Optional["DiscordClient"],
    reports_channel_id: Optional[str],    # #forge-reports — broadcast only
    approvals_channel_id: Optional[str],  # #forge-approvals — approval polling
    dry_run: bool = False,
) -> bool:
    """
    Execute one atomic task through the full plan→approve→act→approve cycle.

    Channel responsibilities:
      reports_channel_id   (#forge-reports)   — post plan report, post impl report. NEVER polled.
      approvals_channel_id (#forge-approvals) — post approval requests, poll reactions, send alerts.

    Returns True if task completed successfully.
    """
    tid = task["id"]
    log(f"{'='*60}")
    log(f"[{tid}] Starting task: {task['description']}")

    # ── State: mark in progress ──────────────────────────────────────────────
    if state.get("in_progress") != tid:
        state["in_progress"] = tid
        state["plan_approved"] = False
        state["current_plan"] = None
        state["plan_approval_message_id"] = None
        state["push_approval_message_id"] = None
        state["plan_report_message_id"] = None
        state["impl_report_message_id"] = None
        save_state(state)

    # ── Phase 1: Plan ────────────────────────────────────────────────────────
    plan_attempt = 1
    feedback = ""

    while True:
        if state.get("plan_approved") and state.get("current_plan"):
            log(f"[{tid}] Plan already approved (resuming) — skipping plan phase")
            break

        log(f"[{tid}] 📋 Plan phase (attempt {plan_attempt})")

        prompt = build_task_prompt(task)
        if feedback:
            prompt += f"\n\nREVISION FEEDBACK from project owner:\n{feedback}\n\nRevise your plan accordingly."

        if dry_run:
            log(f"[{tid}] [DRY RUN] Would run Cline PLAN mode")
            plan_text = f"[DRY RUN] Plan for {tid}"
        else:
            success, output = run_cline(
                prompt, plan_mode=True, cwd=REPO_ROOT,
                task_id=tid, dc=dc,
                approvals_channel_id=approvals_channel_id,
                attempt_number=plan_attempt,
            )
            if not success:
                msg = f"❌ `{tid}` Cline PLAN failed after {CLINE_RETRIES} attempts. Stopping."
                log_err(msg)
                if dc and approvals_channel_id:
                    dc.send_message(approvals_channel_id, msg)
                state["failed"].append(tid)
                state["in_progress"] = None
                save_state(state)
                return False

            # Read the plan from the report file Cline wrote (the authoritative source)
            report_text = read_report_file(tid)
            plan_text = extract_plan_section(report_text, tid)

            # Ensure the report file exists on disk (write if Cline didn't)
            write_forge_plan_report(task, plan_text, plan_attempt)

        state["current_plan"] = plan_text
        save_state(state)

        # ── Post plan report to #forge-reports (broadcast, no reactions) ────
        if dc and reports_channel_id and not dry_run:
            full_report = read_report_file(tid)
            broadcast_text = format_report_broadcast(task, "PLAN", full_report or plan_text)
            report_msg_id = dc.send_message(reports_channel_id, broadcast_text)
            if report_msg_id:
                state["plan_report_message_id"] = report_msg_id
                save_state(state)
                log(f"[{tid}] Plan report posted to #forge-reports (msg {report_msg_id})")

        # ── Post approval request to #forge-approvals (polled for reactions) ─
        if dc and approvals_channel_id:
            approval_text = format_plan_approval_request(task, plan_text, plan_attempt, feedback)
            approval_msg_id = dc.send_message(approvals_channel_id, approval_text)
            if approval_msg_id:
                dc.add_reaction(approvals_channel_id, approval_msg_id, EMOJI_APPROVE)  # ✅
                dc.add_reaction(approvals_channel_id, approval_msg_id, EMOJI_REJECT)  # ❌
                state["plan_approval_message_id"] = approval_msg_id
                save_state(state)
                log(f"[{tid}] Plan approval request posted to #forge-approvals (msg {approval_msg_id})")

                if dry_run:
                    log(f"[{tid}] [DRY RUN] Skipping approval wait")
                    approved, feedback = True, ""
                else:
                    approved, feedback = wait_for_approval(dc, approvals_channel_id, approval_msg_id)
            else:
                log_warn(f"[{tid}] Failed to post to #forge-approvals — auto-approving plan")
                approved, feedback = True, ""
        else:
            log_warn(f"[{tid}] Discord not configured — auto-approving plan")
            approved, feedback = True, ""

        if approved:
            state["plan_approved"] = True
            save_state(state)
            log(f"[{tid}] ✅ Plan approved")
            break
        else:
            log(f"[{tid}] ❌ Plan rejected — feedback: {feedback!r}")
            plan_attempt += 1
            state["plan_approved"] = False
            state["current_plan"] = None
            save_state(state)
            if plan_attempt > 5:
                msg = f"❌ `{tid}` Plan rejected {plan_attempt-1} times without approval. Stopping."
                log_err(msg)
                if dc and approvals_channel_id:
                    dc.send_message(approvals_channel_id, msg)
                state["failed"].append(tid)
                state["in_progress"] = None
                save_state(state)
                return False

    # ── Phase 2: Act ─────────────────────────────────────────────────────────
    log(f"[{tid}] ⚙️  Act phase (implement + test + commit)")

    if dry_run:
        log(f"[{tid}] [DRY RUN] Would run Cline ACT mode")
        act_success = True
    else:
        act_prompt = build_act_prompt(task, state["current_plan"])
        act_success, _ = run_cline(
            act_prompt, plan_mode=False, cwd=REPO_ROOT,
            task_id=tid, dc=dc,
            approvals_channel_id=approvals_channel_id,
        )

    if not act_success:
        msg = f"❌ `{tid}` Cline ACT failed after {CLINE_RETRIES} attempts. Task marked failed."
        log_err(msg)
        if dc and approvals_channel_id:
            dc.send_message(approvals_channel_id, msg)
        state["failed"].append(tid)
        state["in_progress"] = None
        save_state(state)
        return False

    # ── Collect git commit info ───────────────────────────────────────────────
    commit_info = collect_commit_info(task)

    # Read the finalized report file (Cline fills in Implementation + Test Results in STEP 4)
    report_text = read_report_file(tid)

    # ── Post implementation report to #forge-reports (broadcast, no reactions) ─
    if dc and reports_channel_id and not dry_run:
        broadcast_text = format_implementation_report_broadcast(task, commit_info, report_text)
        impl_msg_id = dc.send_message(reports_channel_id, broadcast_text)
        if impl_msg_id:
            state["impl_report_message_id"] = impl_msg_id
            save_state(state)
            log(f"[{tid}] Implementation report posted to #forge-reports (msg {impl_msg_id})")

    # ── Post push approval request to #forge-approvals (polled) ──────────────
    if dc and approvals_channel_id:
        approval_text = format_push_approval_request(task, commit_info)
        approval_msg_id = dc.send_message(approvals_channel_id, approval_text)
        if approval_msg_id:
            dc.add_reaction(approvals_channel_id, approval_msg_id, EMOJI_APPROVE)
            dc.add_reaction(approvals_channel_id, approval_msg_id, EMOJI_REJECT)
            state["push_approval_message_id"] = approval_msg_id
            save_state(state)
            log(f"[{tid}] Push approval request posted to #forge-approvals (msg {approval_msg_id})")

            if dry_run:
                log(f"[{tid}] [DRY RUN] Skipping push approval wait")
                push_approved, push_feedback = True, ""
            else:
                push_approved, push_feedback = wait_for_approval(dc, approvals_channel_id, approval_msg_id)
        else:
            log_warn(f"[{tid}] Failed to post push approval to #forge-approvals — auto-approving")
            push_approved, push_feedback = True, ""
    else:
        log_warn(f"[{tid}] Discord not configured — auto-approving push")
        push_approved, push_feedback = True, ""

    if not push_approved:
        # Cline already committed and pushed in STEP 4 — mark for owner review
        msg = (f"🔍 `{tid}` Push rejected (feedback: {push_feedback!r}). "
               f"Commits are on origin/develop. Task marked needs-review.")
        log_warn(msg)
        if dc and approvals_channel_id:
            dc.send_message(approvals_channel_id, msg)
        state["needs_review"].append(tid)
        state["in_progress"] = None
        save_state(state)
        return False

    log(f"[{tid}] ✅ Push approved")

    # ── Mark complete ────────────────────────────────────────────────────────
    state["completed"].append(tid)
    state["in_progress"] = None
    state["plan_approved"] = False
    state["current_plan"] = None
    state["plan_approval_message_id"] = None
    state["push_approval_message_id"] = None
    state["plan_report_message_id"] = None
    state["impl_report_message_id"] = None
    save_state(state)

    if dc and reports_channel_id:
        dc.send_message(
            reports_channel_id,
            f"✅ **Task `{tid}` COMPLETE** — {task['description']}"
        )

    log(f"[{tid}] ✅ Task complete")
    return True

# ─── Main loop ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="SindriStudio Forge Orchestrator")
    parser.add_argument("--task", help="Force-start a specific task ID")
    parser.add_argument("--dry-run", action="store_true", help="Show plan only, no execution")
    parser.add_argument("--list", action="store_true", help="Show DAG status and exit")
    parser.add_argument("--reset-task", metavar="TASK_ID",
                        help="Reset a task to unstarted (does NOT revert git)")
    parser.add_argument("--reset-task-git", metavar="TASK_ID",
                        help="Reset task to unstarted AND hard-reset repos to origin/develop")
    args = parser.parse_args()

    log("=" * 60)
    log("Forge starting up")

    if not DISCORD_BOT_TOKEN:
        log_warn("FORGE_DISCORD_TOKEN not set — running without Discord notifications")
    if not DISCORD_GUILD_ID:
        log_warn("FORGE_DISCORD_GUILD_ID not set — Discord channel lookup will fail")

    tasks = load_tasks()
    state = load_state()

    if args.list:
        print_dag_status(tasks, state)
        return

    if args.reset_task or args.reset_task_git:
        tid = args.reset_task or args.reset_task_git
        dag = build_dag(tasks)
        task = dag.get(tid)

        for lst in ["completed", "failed", "needs_review"]:
            if tid in state.get(lst, []):
                state[lst].remove(tid)
        if state.get("in_progress") == tid:
            state["in_progress"] = None
            state["plan_approved"] = False
            state["current_plan"] = None
        save_state(state)
        log(f"Task {tid} reset to unstarted in state.json")

        if args.reset_task_git and task:
            log(f"Reverting repos for {tid} to origin/develop...")
            ok = revert_task_repos(task)
            if ok:
                log(f"✅ Repos reverted successfully")
            else:
                log_err(f"⚠️  Some repos could not be reverted — check manually")
        return

    # Set up Discord — channel IDs are hardcoded, only token needed from env
    dc = get_discord()
    reports_channel_id   = DISCORD_REPORTS_CHANNEL_ID
    approvals_channel_id = DISCORD_APPROVALS_CHANNEL_ID
    if dc:
        log(f"Discord reports channel (broadcast):  {reports_channel_id}")
        log(f"Discord approvals channel (polled):   {approvals_channel_id}")
        log(f"Discord owner gate:                   {FORGE_OWNER_ID}")

    # If --task specified, force that task as in_progress
    if args.task:
        dag = build_dag(tasks)
        task = dag.get(args.task)
        if not task:
            log_err(f"Task {args.task} not found in tasks.json")
            sys.exit(1)
        state["in_progress"] = args.task
        state["plan_approved"] = False
        state["current_plan"] = None
        save_state(state)

    # Main loop
    while True:
        tasks = load_tasks()  # Reload each iteration — allows hot-editing tasks.json
        state = load_state()

        task = find_next_task(tasks, state)

        if task is None:
            all_ids = {t["id"] for t in tasks}
            completed = set(state["completed"])
            failed = set(state["failed"])
            needs_review = set(state.get("needs_review", []))
            remaining = all_ids - completed - failed - needs_review

            if not remaining:
                msg = "🎉 **All tasks complete!** SindriStudio Phase 1+2 build is done."
                log(msg)
                if dc and reports_channel_id:
                    dc.send_message(reports_channel_id, msg)
            else:
                log(f"No unblocked tasks. Remaining: {remaining}")
                log("Possible causes: failed tasks blocking prereqs. Use --list for details.")
            break

        success = execute_task(
            task=task,
            state=state,
            dc=dc,
            reports_channel_id=reports_channel_id,
            approvals_channel_id=approvals_channel_id,
            dry_run=args.dry_run,
        )

        if args.dry_run or args.task:
            break

        if not success:
            # On failure: check if repos need reverting before next run
            log_err(f"Task {task['id']} failed. Stopping.")
            log_err(f"To retry with clean repos: python forge.py --reset-task-git {task['id']}")
            log_err(f"To retry without reverting: python forge.py --reset-task {task['id']}")
            sys.exit(1)

        time.sleep(2)

    log("Forge exiting")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\nForge interrupted (Ctrl+C). State saved. Resume with: python forge.py")
        sys.exit(0)
    except Exception as e:
        log_err(f"Unexpected error: {e}")
        log_err(traceback.format_exc())
        sys.exit(1)