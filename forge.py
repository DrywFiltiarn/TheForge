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
REPO_ROOT = Path("/home/dryw/sandbox")  # SindriStudio root
STATE_FILE = FORGE_DIR / "state.json"
TASKS_FILE = FORGE_DIR / "tasks.json"
LOG_FILE       = FORGE_DIR / "forge.log"
CLINE_LOG_FILE = FORGE_DIR / "cline.log"

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

# Model IDs — llama-swap variants, selected via -M flag.
# Sampling parameters are applied server-side by llama-swap's setParamsByID.
# planning: used for STEP 1 (Cline plan mode) across all tasks
# coding:   used for STEP 2-4 (Cline act mode) across all tasks
MODEL_PLANNING = os.environ.get("FORGE_MODEL_PLANNING", "Qwen3.6-35B-A3B:planning")
MODEL_CODING   = os.environ.get("FORGE_MODEL_CODING",   "Qwen3.6-35B-A3B:coding")

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

# ─── PDF generation ───────────────────────────────────────────────────────────

PDF_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');

:root {
    --bg: #ffffff;
    --text: #1a1a2e;
    --muted: #4a5568;
    --accent: #2563eb;
    --border: #e2e8f0;
    --code-bg: #f1f5f9;
    --heading: #0f172a;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

@page {
    size: A4;
    margin: 24mm 20mm 24mm 20mm;
    @bottom-right {
        content: counter(page);
        font-size: 9pt;
        color: #94a3b8;
    }
}

body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    font-size: 10.5pt;
    line-height: 1.65;
    color: var(--text);
    background: var(--bg);
}

h1 {
    font-size: 20pt;
    font-weight: 600;
    color: var(--heading);
    margin-bottom: 6pt;
    padding-bottom: 8pt;
    border-bottom: 2px solid var(--accent);
}

h2 {
    font-size: 14pt;
    font-weight: 600;
    color: var(--heading);
    margin-top: 18pt;
    margin-bottom: 6pt;
    padding-bottom: 4pt;
    border-bottom: 1px solid var(--border);
}

h3 {
    font-size: 11pt;
    font-weight: 600;
    color: var(--accent);
    margin-top: 12pt;
    margin-bottom: 4pt;
    text-transform: uppercase;
    letter-spacing: 0.04em;
}

p { margin-bottom: 8pt; color: var(--text); }

ul, ol {
    margin-left: 16pt;
    margin-bottom: 8pt;
}

li { margin-bottom: 3pt; }

code {
    font-family: 'JetBrains Mono', 'Courier New', monospace;
    font-size: 9pt;
    background: var(--code-bg);
    padding: 1pt 4pt;
    border-radius: 3pt;
    color: #c7254e;
}

pre {
    background: var(--code-bg);
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
    border-radius: 4pt;
    padding: 10pt 12pt;
    margin: 8pt 0;
    overflow-x: auto;
    page-break-inside: avoid;
}

pre code {
    font-size: 8.5pt;
    background: none;
    padding: 0;
    color: var(--text);
}

blockquote {
    border-left: 3px solid var(--accent);
    margin: 8pt 0;
    padding: 6pt 12pt;
    background: #eff6ff;
    color: var(--muted);
}

table {
    width: 100%;
    border-collapse: collapse;
    margin: 10pt 0;
    font-size: 9.5pt;
}

th {
    background: var(--code-bg);
    font-weight: 600;
    padding: 5pt 8pt;
    border: 1px solid var(--border);
    text-align: left;
}

td {
    padding: 5pt 8pt;
    border: 1px solid var(--border);
}

tr:nth-child(even) td { background: #f8fafc; }

hr {
    border: none;
    border-top: 1px solid var(--border);
    margin: 14pt 0;
}

strong { font-weight: 600; color: var(--heading); }
em { color: var(--muted); }

.header-meta {
    font-size: 9pt;
    color: var(--muted);
    margin-bottom: 14pt;
}
"""

def _markdown_to_pdf(markdown_text: str, title: str = "") -> Optional[bytes]:
    """
    Convert a markdown string to PDF bytes using weasyprint.
    Returns bytes on success, None on failure (caller falls back to plain text).
    """
    try:
        import markdown as md_lib
        html_body = md_lib.markdown(
            markdown_text,
            extensions=["fenced_code", "tables", "codehilite", "toc", "nl2br"],
        )
    except ImportError:
        # markdown library not available — do minimal conversion
        import html as html_mod
        lines = markdown_text.splitlines()
        html_lines = []
        in_code = False
        for line in lines:
            if line.startswith("```"):
                if in_code:
                    html_lines.append("</code></pre>")
                    in_code = False
                else:
                    html_lines.append("<pre><code>")
                    in_code = True
            elif in_code:
                html_lines.append(html_mod.escape(line))
            elif line.startswith("### "):
                html_lines.append(f"<h3>{html_mod.escape(line[4:])}</h3>")
            elif line.startswith("## "):
                html_lines.append(f"<h2>{html_mod.escape(line[3:])}</h2>")
            elif line.startswith("# "):
                html_lines.append(f"<h1>{html_mod.escape(line[2:])}</h1>")
            elif line.startswith("- ") or line.startswith("* "):
                html_lines.append(f"<li>{html_mod.escape(line[2:])}</li>")
            elif line.strip() == "---":
                html_lines.append("<hr>")
            elif line.strip():
                html_lines.append(f"<p>{html_mod.escape(line)}</p>")
            else:
                html_lines.append("<br>")
        html_body = "\n".join(html_lines)

    title_html = f"<title>{title}</title>" if title else ""
    clean_title = re.sub(r"\.(pdf|md|txt)$", "", title) if title else "Report"

    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
{title_html}
<style>{PDF_CSS}</style>
</head>
<body>
<div class="header-meta">SindriStudio Forge · {clean_title}</div>
{html_body}
</body>
</html>"""

    try:
        from weasyprint import HTML, CSS
        pdf_bytes = HTML(string=full_html).write_pdf()
        return pdf_bytes
    except Exception as e:
        log_warn(f"weasyprint PDF generation failed: {e}")
        return None


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
        if len(content) <= 2000:
            payload["content"] = content
        else:
            payload["embeds"] = [{"description": content[:4096]}]
        if embeds:
            payload["embeds"] = embeds
        result = self._post(f"/channels/{channel_id}/messages", payload)
        return result["id"] if result else None

    def send_file(self, channel_id: str, caption: str,
                  filename: str, file_content: str) -> Optional[str]:
        """
        Convert markdown to PDF and post as a Discord attachment.
        PDF renders inline on iOS via QuickLook without requiring a download.
        filename should end in .pdf — the base name is used for the PDF title.
        Falls back to plain .txt attachment if PDF generation fails.
        Returns the message ID or None on failure.
        """
        # Ensure filename ends in .pdf
        pdf_filename = re.sub(r"\.(md|txt|html)$", "", filename) + ".pdf"

        pdf_bytes = _markdown_to_pdf(file_content, title=pdf_filename)

        try:
            headers = {"Authorization": self.headers["Authorization"]}
            if pdf_bytes:
                files = {"file": (pdf_filename, pdf_bytes, "application/pdf")}
                log(f"Discord send_file: sending PDF ({len(pdf_bytes)} bytes) as {pdf_filename}")
            else:
                # Fallback to plain text if PDF generation failed
                log_warn("PDF generation failed — sending as plain text")
                txt_filename = pdf_filename.replace(".pdf", ".txt")
                files = {"file": (txt_filename, file_content.encode("utf-8"), "text/plain; charset=utf-8")}

            data = {"content": caption[:2000]}
            r = requests.post(
                f"{self.BASE}/channels/{channel_id}/messages",
                headers=headers,
                data=data,
                files=files,
                timeout=30,
            )
            r.raise_for_status()
            return r.json()["id"]
        except Exception as e:
            log_warn(f"Discord send_file failed: {e} — falling back to inline message")
            return self.send_message(channel_id, f"{caption}\n\n```\n{file_content[:1800]}\n```")

    def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> bool:
        """Add a reaction. emoji can be raw Unicode, percent-encoded, or name:id.
        Handles 429 rate-limit with a single retry using the retry_after header."""
        try:
            encoded = _encode_emoji(emoji)
            url = f"{self.BASE}/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me"
            headers = {k: v for k, v in self.headers.items() if k != "Content-Type"}

            r = requests.put(url, headers=headers, timeout=10)

            if r.status_code == 429:
                retry_after = float(r.json().get("retry_after", 1.0))
                log_warn(f"Discord add_reaction rate-limited — retrying after {retry_after}s")
                time.sleep(retry_after + 0.1)
                r = requests.put(url, headers=headers, timeout=10)

            if r.status_code not in (200, 204):
                log_warn(f"Discord add_reaction HTTP {r.status_code} for emoji {encoded!r}"
                         f" — 403=missing permission, 400=unknown emoji, 429=rate limited")
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

def _forge_commit_root(task_id: str, task_desc: str) -> Optional[str]:
    """
    Commit and push the SindriStudio root repo.

    Stages everything in the root repo:
      - Updated submodule pointers (backend, frontend)
      - .cline/reports/{task_id}.md
      - .cline/state/CURRENT_TASK.md
      - Any other root-level files changed by Cline

    Returns the short commit hash on success, None if nothing to commit or on error.
    This function is always called by the Forge after a successful act phase —
    it is NOT delegated to Cline.
    """
    try:
        # Stage everything in the root repo
        stage = subprocess.run(
            ["git", "add", "-A"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        if stage.returncode != 0:
            log_err(f"[git] root git add -A failed: {stage.stderr}")
            return None

        # Check if there is actually anything to commit
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        if not status.stdout.strip():
            log(f"[git] Root repo: nothing to commit for {task_id}")
            return None

        # Commit
        commit_msg = (
            f"chore(root): update submodules and reports for {task_id}\n\n"
            f"Task: {task_id}\n"
            f"Description: {task_desc}\n"
            f"Committed by Forge orchestrator"
        )
        commit = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        if commit.returncode != 0:
            log_err(f"[git] root commit failed: {commit.stderr}")
            return None

        # Push
        push = subprocess.run(
            ["git", "push", "origin", "develop"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        if push.returncode != 0:
            log_err(f"[git] root push failed: {push.stderr}")
            # Commit succeeded but push failed — return hash with warning
            hash_result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )
            return f"{hash_result.stdout.strip()} (NOT PUSHED)"

        # Return short hash
        hash_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        return hash_result.stdout.strip()

    except Exception as e:
        log_err(f"[git] _forge_commit_root exception: {e}")
        return None

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

def _extract_section(report_text: str, heading: str) -> str:
    """Extract a named ## section from a markdown report. Returns '' if not found."""
    match = re.search(
        rf"^## {re.escape(heading)}\n(.*?)(?=^##|\Z)",
        report_text, re.DOTALL | re.MULTILINE
    )
    return match.group(1).strip() if match else ""

def _extract_subsection(report_text: str, heading: str) -> str:
    """Extract a named ### subsection from a markdown report."""
    match = re.search(
        rf"^### {re.escape(heading)}\n(.*?)(?=^###|^##|\Z)",
        report_text, re.DOTALL | re.MULTILINE
    )
    return match.group(1).strip() if match else ""

def _bullet_lines(text: str, max_lines: int = 6) -> str:
    """Return up to max_lines non-empty lines from text, each prefixed with a bullet."""
    lines = [l.strip("- •\t ") for l in text.splitlines() if l.strip("- •\t ")]
    return "\n".join(f"• {l}" for l in lines[:max_lines])

def format_report_caption(task: dict, section: str) -> str:
    """
    One-line caption posted above the attached .md file in #forge-reports.
    The full report is in the attachment — no extraction needed.
    """
    tid   = task["id"]
    desc  = task["description"]
    phase = task.get("phase", "?")
    icon  = "📋" if section == "PLAN" else "📦"
    gate  = "Approval request in #forge-approvals"
    return (
        f"{icon} **{section} REPORT — `{tid}` (Phase {phase})**\n"
        f"_{desc}_\n"
        f"_{gate}_"
    )

def format_plan_approval_request(task: dict, attempt: int,
                                  feedback: str = "") -> str:
    """
    Minimal approval request for #forge-approvals.
    Full plan is in the attached file in #forge-reports — nothing is extracted here.
    """
    tid     = task["id"]
    desc    = task["description"]
    prereqs = ", ".join(task.get("prereqs", [])) or "none"
    repos   = ", ".join(task.get("repos", ["root"]))

    header = f"**🔐 PLAN APPROVAL — `{tid}`**"
    if attempt > 1:
        header += f" *(revision {attempt})*"

    parts = [
        header,
        f"**{desc}**",
        f"Repos: `{repos}` · Prereqs: `{prereqs}`",
    ]

    if feedback:
        parts += ["", f"📝 _Revision feedback: {feedback}_"]

    parts += [
        "",
        f"_Full plan report attached in #forge-reports → search `{tid}`_",
        "",
        f"✅ approve · ❌ reject (reply with feedback then react)",
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

def format_implementation_caption(task: dict, commit_info: dict) -> str:
    """
    Caption posted above the attached implementation report .md file in #forge-reports.
    Shows commit hashes only — the full report is in the attachment.
    """
    tid  = task["id"]
    desc = task["description"]
    phase = task.get("phase", "?")

    parts = [
        f"**📦 IMPLEMENTATION REPORT — `{tid}` (Phase {phase})**",
        f"_{desc}_",
        f"_Push approval request in #forge-approvals_",
        "",
    ]

    for repo, info in commit_info.items():
        if info.get("commits"):
            parts.append(f"**{repo}:**")
            for c in info["commits"][:3]:
                parts.append(f"  `{c}`")
            parts.append("")

    return "\n".join(parts)

# ─── Approval flow ────────────────────────────────────────────────────────────

def wait_for_approval(
    dc: Optional["DiscordClient"],
    approvals_channel_id: str,
    message_id: str,
    timeout: int = APPROVAL_TIMEOUT,
    reports_channel_id: Optional[str] = None,
    report_message_id: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Poll for ✅ or ❌ reaction on message_id in #forge-approvals.
    Returns (approved: bool, feedback: str).

    Only reactions from FORGE_OWNER_ID are acted upon. Any reaction from
    a different user ID is logged and ignored — the poll continues.

    If reports_channel_id and report_message_id are provided, the outcome
    reaction is also added to the matching report in #forge-reports so the
    decision is visible to anyone reading that channel.
    """
    if dc is None:
        log_warn("Discord not configured — auto-approving")
        return True, ""

    log(f"Waiting for approval on message {message_id} (owner: {FORGE_OWNER_ID}, timeout {timeout}s)...")
    deadline = time.monotonic() + timeout
    last_reminder = time.monotonic()

    def mirror_reaction(emoji: str) -> None:
        """Add the outcome reaction to the report message in #forge-reports."""
        if reports_channel_id and report_message_id:
            dc.add_reaction(reports_channel_id, report_message_id, emoji)

    while time.monotonic() < deadline:
        time.sleep(APPROVAL_POLL_INTERVAL)

        # Check ✅ — only count if it's from the owner
        approvers = dc.get_reactions(approvals_channel_id, message_id, EMOJI_APPROVE)
        for u in approvers:
            if u.get("bot", False):
                continue
            if u.get("id") == FORGE_OWNER_ID:
                log(f"✅ Approved by owner ({u.get('username', 'unknown')})")
                mirror_reaction(EMOJI_APPROVE)
                dc.send_message(
                    approvals_channel_id,
                    f"✅ **Approval registered** — reaction picked up from "
                    f"{u.get('username', 'owner')}. Cline is proceeding."
                )
                return True, ""
            else:
                log_warn(f"⚠️  Ignoring ✅ from non-owner user {u.get('id')} ({u.get('username', '?')})")
                dc.send_message(
                    approvals_channel_id,
                    f"⚠️ Reaction from `{u.get('username', u.get('id', '?'))}` ignored — "
                    f"only the server owner can approve."
                )

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
                mirror_reaction(EMOJI_REJECT)
                if feedback:
                    dc.send_message(
                        approvals_channel_id,
                        f"❌ **Rejection registered** — feedback received: _{feedback}_\n"
                        f"Cline will revise the plan."
                    )
                else:
                    dc.send_message(
                        approvals_channel_id,
                        f"❌ **Rejection registered** — no feedback reply found.\n"
                        f"Cline will re-plan. Reply with feedback before reacting next time "
                        f"so the revision has direction."
                    )
                return False, feedback
            else:
                log_warn(f"⚠️  Ignoring ❌ from non-owner user {u.get('id')} ({u.get('username', '?')})")
                dc.send_message(
                    approvals_channel_id,
                    f"⚠️ Reaction from `{u.get('username', u.get('id', '?'))}` ignored — "
                    f"only the server owner can reject."
                )

        if time.monotonic() - last_reminder > 300:
            last_reminder = time.monotonic()
            log("Still waiting for owner approval in #forge-approvals...")

    log_warn(f"Approval timeout after {timeout}s")
    return False, "Approval timed out — no response received"

# ─── Cline subprocess ─────────────────────────────────────────────────────────

def build_cline_cmd(prompt: str, plan_mode: bool, cwd: Path,
                    model_id: Optional[str] = None) -> list[str]:
    cmd = [CLINE_BIN]
    if plan_mode:
        cmd.append("-p")
    cmd.extend([
        "--json",
        "--auto-approve=true",
        "--timeout", str(CLINE_TIMEOUT),
        "--cwd", str(cwd),
    ])
    if model_id:
        cmd.extend(["-M", model_id])
    cmd.append(prompt)
    return cmd

def _write_cline_log(clf, event: dict, token_buf: list[str]) -> None:
    """
    Write a human-readable line to cline.log for a single Cline NDJSON event.

    Token accumulation: content_start events carry individual tokens (~one word).
    We buffer them and flush as a complete paragraph when a non-token event
    arrives or when the buffer gets long enough, so tail -f shows readable prose
    rather than one JSON blob per word.

    Event type mapping:
      agent_event / content_start  → accumulate tokens, flush on paragraph break
      agent_event / content_block_stop → flush token buffer as a line
      tool_use                     → "  ▶ tool_name(params)"
      tool_result                  → "  ◀ result summary"
      system / info / error        → prefixed plain text
      anything else                → skip (noise)
    """
    etype = event.get("type", "")
    ts    = event.get("ts", "")[-8:-1] if event.get("ts") else ""  # HH:MM:SS

    def flush_tokens() -> None:
        if token_buf:
            text = "".join(token_buf).strip()
            if text:
                clf.write(f"  {text}\n")
                clf.flush()
            token_buf.clear()

    if etype == "agent_event":
        inner = event.get("event", {})
        itype = inner.get("type", "")

        if itype == "content_start":
            token = inner.get("text", "")
            token_buf.append(token)
            # Flush on sentence boundaries to keep lines reasonably short
            joined = "".join(token_buf)
            if len(joined) > 120 or joined.endswith(("\n", ". ", "! ", "? ")):
                flush_tokens()

        elif itype in ("content_block_stop", "message_stop"):
            flush_tokens()

        elif itype == "tool_use":
            flush_tokens()
            name   = inner.get("name", inner.get("tool", "?"))
            params = inner.get("input", inner.get("params", {}))
            # Show the most useful param without dumping the whole dict
            hint = ""
            for key in ("path", "command", "query", "url", "description", "content"):
                if key in params:
                    val = str(params[key])[:80]
                    hint = f" {key}={val!r}"
                    break
            clf.write(f"\n  ▶ {ts} {name}{hint}\n")
            clf.flush()

        elif itype == "tool_result":
            flush_tokens()
            content = inner.get("content", "")
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            summary = str(content).strip().replace("\n", " ")[:120]
            if summary:
                clf.write(f"  ◀ {summary}\n")
                clf.flush()

        elif itype == "message_start":
            flush_tokens()
            clf.write(f"\n  ── {ts} message ──\n")
            clf.flush()

    elif etype in ("system", "info"):
        flush_tokens()
        text = event.get("message", event.get("text", str(event)))[:200]
        clf.write(f"[{ts}] {text}\n")
        clf.flush()

    elif etype == "error":
        flush_tokens()
        text = event.get("message", event.get("error", str(event)))[:200]
        clf.write(f"[{ts}] ERROR: {text}\n")
        clf.flush()

    # All other event types (metadata, ping, etc.) are silently dropped


def run_cline(
    prompt: str,
    plan_mode: bool,
    cwd: Path,
    task_id: str,
    dc: Optional["DiscordClient"],
    approvals_channel_id: Optional[str],
    attempt_number: int = 1,
    model_id: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Run Cline CLI with retry logic for llama.cpp failures.
    Returns (success: bool, output_text: str).

    model_id is passed via -M to select the llama-swap variant:
      Qwen3.6-35B-A3B:planning — used for STEP 1 (plan mode, all tasks)
      Qwen3.6-35B-A3B:coding   — used for STEP 2-4 (act mode, all tasks)
    llama-swap applies the correct sampling params server-side via setParamsByID.

    Cline output is parsed and written to CLINE_LOG_FILE in human-readable form.
    Monitor live with: tail -f forge/cline.log
    """
    cmd = build_cline_cmd(prompt, plan_mode, cwd, model_id=model_id)
    mode_label = "PLAN" if plan_mode else "ACT"
    model_label = model_id or "default"
    log(f"[{task_id}] Running Cline {mode_label} mode — model: {model_label} "
        f"(timeout {CLINE_TIMEOUT}s, attempt {attempt_number})")
    log(f"[{task_id}] Cline output → {CLINE_LOG_FILE}")

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
        token_buf:   list[str] = []

        # Session header
        with open(CLINE_LOG_FILE, "a") as clf:
            clf.write(
                f"\n{'─'*60}\n"
                f"[{_ts()}] [{task_id}] Cline {mode_label} — attempt {attempt}/{CLINE_RETRIES}\n"
                f"{'─'*60}\n"
            )

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            with open(CLINE_LOG_FILE, "a") as clf:
                for line in proc.stdout:
                    raw = line.rstrip()
                    try:
                        event = json.loads(raw)
                        # Write human-readable form to cline.log
                        _write_cline_log(clf, event, token_buf)
                        # Extract text for internal use (Discord reports etc.)
                        if event.get("type") == "agent_event":
                            inner = event.get("event", {})
                            if inner.get("type") == "content_start":
                                text_output.append(inner.get("text", ""))
                        elif event.get("type") == "text":
                            text_output.append(event.get("text", ""))
                    except json.JSONDecodeError:
                        # Plain text — write as-is
                        if raw:
                            clf.write(f"{raw}\n")
                            clf.flush()
                            text_output.append(raw)

                # Flush any remaining tokens at end of stream
                if token_buf:
                    clf.write("  " + "".join(token_buf).strip() + "\n")
                    clf.flush()
                    token_buf.clear()

            proc.wait(timeout=30)
            exit_code = proc.returncode

        except subprocess.TimeoutExpired:
            proc.kill()
            log_err(f"[{task_id}] Cline {mode_label} timed out after {CLINE_TIMEOUT}s")
            exit_code = -1
        except FileNotFoundError:
            log_err(f"Cline binary not found: {CLINE_BIN}")
            log_err("Install with: npm install -g cline")
            sys.exit(1)

        with open(CLINE_LOG_FILE, "a") as clf:
            clf.write(f"[{_ts()}] [{task_id}] Cline {mode_label} exited: {exit_code}\n")

        full_output = "\n".join(text_output)

        if exit_code == 0:
            log(f"[{task_id}] Cline {mode_label} completed successfully")
            return True, full_output

        log_err(f"[{task_id}] Cline {mode_label} exited with code {exit_code}")

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

        # Notify reports channel that a new task is starting
        if dc and reports_channel_id and not dry_run:
            prereqs = ", ".join(task.get("prereqs", [])) or "none"
            dc.send_message(
                reports_channel_id,
                f"⚙️ **Task `{tid}` STARTED** — {task['description']}\n"
                f"Phase {task.get('phase', '?')} · Prereqs: `{prereqs}`"
            )

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
            log(f"[{tid}] [DRY RUN] Would run Cline PLAN mode ({MODEL_PLANNING})")
            plan_text = f"[DRY RUN] Plan for {tid}"
        else:
            success, output = run_cline(
                prompt, plan_mode=True, cwd=REPO_ROOT,
                task_id=tid, dc=dc,
                approvals_channel_id=approvals_channel_id,
                attempt_number=plan_attempt,
                model_id=MODEL_PLANNING,
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

        # Read full report once — sent as attachment to #forge-reports
        full_report = read_report_file(tid)

        # ── Post plan report to #forge-reports as .md attachment ────────────
        if dc and reports_channel_id and not dry_run:
            caption = format_report_caption(task, "PLAN")
            filename = f"{tid}-plan.md"
            report_msg_id = dc.send_file(
                reports_channel_id, caption, filename,
                full_report or plan_text
            )
            if report_msg_id:
                state["plan_report_message_id"] = report_msg_id
                save_state(state)
                log(f"[{tid}] Plan report attached to #forge-reports (msg {report_msg_id})")

        # ── Post approval request to #forge-approvals (polled for reactions) ─
        if dc and approvals_channel_id:
            approval_text = format_plan_approval_request(task, plan_attempt, feedback)
            approval_msg_id = dc.send_message(approvals_channel_id, approval_text)
            if approval_msg_id:
                dc.add_reaction(approvals_channel_id, approval_msg_id, EMOJI_APPROVE)  # ✅
                time.sleep(0.75)
                dc.add_reaction(approvals_channel_id, approval_msg_id, EMOJI_REJECT)   # ❌
                state["plan_approval_message_id"] = approval_msg_id
                save_state(state)
                log(f"[{tid}] Plan approval request posted to #forge-approvals (msg {approval_msg_id})")

                if dry_run:
                    log(f"[{tid}] [DRY RUN] Skipping approval wait")
                    approved, feedback = True, ""
                else:
                    approved, feedback = wait_for_approval(
                        dc, approvals_channel_id, approval_msg_id,
                        reports_channel_id=reports_channel_id,
                        report_message_id=state.get("plan_report_message_id"),
                    )
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

            # ── Snapshot approved plan before act phase ──────────────────────
            # Cline will overwrite .cline/reports/{tid}.md during STEP 4.
            # Save the full approved plan report now so the implementation PDF
            # can include it as a permanent record of decisions made.
            snapshot_path = REPO_ROOT / ".cline" / "reports" / f"{tid}-plan-snapshot.md"
            try:
                snapshot_content = read_report_file(tid) or plan_text
                snapshot_path.write_text(snapshot_content)
                log(f"[{tid}] Plan snapshot saved to {snapshot_path.name}")
            except Exception as e:
                log_warn(f"[{tid}] Could not save plan snapshot: {e}")
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
    log(f"[{tid}] ⚙️  Act phase — model: {MODEL_CODING}")

    if dry_run:
        log(f"[{tid}] [DRY RUN] Would run Cline ACT mode ({MODEL_CODING})")
        act_success = True
    else:
        act_prompt = build_act_prompt(task, state["current_plan"])
        act_success, _ = run_cline(
            act_prompt, plan_mode=False, cwd=REPO_ROOT,
            task_id=tid, dc=dc,
            approvals_channel_id=approvals_channel_id,
            model_id=MODEL_CODING,
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

    # ── Forge-owned root repo commit ──────────────────────────────────────────
    # Cline commits and pushes the submodules it worked on (AnvilML, BloomeryUI).
    # The Forge always commits the root repo itself to ensure:
    #   - submodule pointers are updated to the latest submodule commits
    #   - .cline/reports/{TASK-ID}.md is committed
    #   - .cline/state/CURRENT_TASK.md is committed
    # This runs unconditionally after every successful act phase.
    root_commit_hash = _forge_commit_root(tid, task["description"])
    if root_commit_hash:
        log(f"[{tid}] Root repo committed and pushed: {root_commit_hash}")
    else:
        log_warn(f"[{tid}] Root repo commit failed or had nothing to commit — check manually")
        if dc and approvals_channel_id:
            dc.send_message(
                approvals_channel_id,
                f"⚠️ `{tid}` Root repo commit/push failed. "
                f"Submodule refs and reports may not be on origin. Check manually."
            )

    # ── Collect git commit info ───────────────────────────────────────────────
    commit_info = collect_commit_info(task)

    # Read the finalized implementation report (Cline writes this in STEP 4)
    impl_report_text = read_report_file(tid)

    # Merge the saved plan snapshot into the implementation report so the PDF
    # is a complete record: approved plan decisions + implementation + test results.
    snapshot_path = REPO_ROOT / ".cline" / "reports" / f"{tid}-plan-snapshot.md"
    if snapshot_path.exists():
        plan_snapshot = snapshot_path.read_text()
        full_report_text = (
            f"{impl_report_text or ''}\n\n"
            f"---\n\n"
            f"# Approved Plan (recorded at approval)\n\n"
            f"{plan_snapshot}"
        )
        log(f"[{tid}] Merged plan snapshot into implementation report")
    else:
        log_warn(f"[{tid}] Plan snapshot not found — implementation report will not include plan")
        full_report_text = impl_report_text or f"# {tid}\nReport not found."

    # ── Post implementation report to #forge-reports as PDF attachment ────────
    if dc and reports_channel_id and not dry_run:
        caption = format_implementation_caption(task, commit_info)
        filename = f"{tid}-implementation.md"
        impl_msg_id = dc.send_file(
            reports_channel_id, caption, filename,
            full_report_text
        )
        if impl_msg_id:
            state["impl_report_message_id"] = impl_msg_id
            save_state(state)
            log(f"[{tid}] Implementation report attached to #forge-reports (msg {impl_msg_id})")

    # ── Post push approval request to #forge-approvals (polled) ──────────────
    if dc and approvals_channel_id:
        approval_text = format_push_approval_request(task, commit_info)
        approval_msg_id = dc.send_message(approvals_channel_id, approval_text)
        if approval_msg_id:
            dc.add_reaction(approvals_channel_id, approval_msg_id, EMOJI_APPROVE)
            time.sleep(0.75)
            dc.add_reaction(approvals_channel_id, approval_msg_id, EMOJI_REJECT)
            state["push_approval_message_id"] = approval_msg_id
            save_state(state)
            log(f"[{tid}] Push approval request posted to #forge-approvals (msg {approval_msg_id})")

            if dry_run:
                log(f"[{tid}] [DRY RUN] Skipping push approval wait")
                push_approved, push_feedback = True, ""
            else:
                push_approved, push_feedback = wait_for_approval(
                    dc, approvals_channel_id, approval_msg_id,
                    reports_channel_id=reports_channel_id,
                    report_message_id=state.get("impl_report_message_id"),
                )
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

    # Purge cline.log now that the task is complete — keeps disk usage low.
    # forge.log is retained (it's the permanent orchestration record).
    try:
        CLINE_LOG_FILE.write_text("")
        log(f"[{tid}] cline.log purged")
    except Exception as e:
        log_warn(f"[{tid}] Could not purge cline.log: {e}")

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