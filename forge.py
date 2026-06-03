#!/usr/bin/env python3
"""
forge.py — SindriStudio Autonomous Development Orchestrator

Drives atomic OpenCode CLI sessions through the 4-step plan/implement/test/commit
cycle defined by the forge agent files, with Discord approval gates and full resume
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
    python forge.py --repo anvilml                         # run all phases for anvilml
    python forge.py --repo anvilml --task P1-A1            # run ONE task then exit (full cycle, full gates)
    python forge.py --repo anvilml --phase 2               # load phases 1+2 only, run from next unblocked task
    python forge.py --repo anvilml --phase 2 --task P2-B1  # run ONE specific task from phase 2 then exit
    python forge.py --repo anvilml --dry-run               # show what would run, no execution
    python forge.py --repo anvilml --list                  # show task DAG status and exit
    python forge.py --repo anvilml --list --phase 2        # show DAG for phases 1+2 only
    python forge.py --repo anvilml --reset-task P1-A3      # reset a task to unstarted (no git)
    python forge.py --repo anvilml --reset-task-git P1-A3  # reset task AND hard-reset repo to origin/<branch>

repos.json format (next to forge.py):
    {
      "anvilml": {
        "path": "/home/user/projects/AnvilML",
        "branch": "main",
        "github_url": "https://github.com/yourorg/AnvilML"
      },
      "bloomeryui": {
        "path": "/home/user/projects/BloomeryUI",
        "branch": "main",
        "github_url": "https://github.com/yourorg/BloomeryUI"
      }
    }
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

FORGE_DIR        = Path(__file__).parent.resolve()   # wherever forge.py lives
REPOS_FILE       = FORGE_DIR / "repos.json"
LOG_FILE         = FORGE_DIR / "forge.log"
OPENCODE_LOG_FILE   = FORGE_DIR / "opencode.log"
CONTEXT_LOG_FILE = FORGE_DIR / "context.log"
OPENCODE_SKIPPED_LOG_FILE = FORGE_DIR / "opencode-skipped.log"  # unhandled event types

# Resolved in main() after --repo is validated.
# Points to <repo>/.forge/state.json — scoped to the active repository.
STATE_FILE: Path = Path()  # placeholder; never used before main() sets it

# ─── Repository registry ──────────────────────────────────────────────────────
# repos.json maps logical project names to their configuration.
# Each entry must have:
#   path       — absolute filesystem path to the repository
#   branch     — working branch (e.g. "main", "develop")
#   github_url — GitHub remote URL (informational; not used by The Forge yet)
#
# Example repos.json:
# {
#   "anvilml": {
#     "path": "/home/user/projects/AnvilML",
#     "branch": "main",
#     "github_url": "https://github.com/yourorg/AnvilML"
#   }
# }

# Populated in main() after logging is ready.
# Maps project name -> { "path": Path, "branch": str, "github_url": str }
REPOS: dict = {}


def load_repos() -> dict:
    """
    Load repos.json and return a mapping of project name -> repo config dict.
    Each config dict has keys: path (Path), branch (str), github_url (str).
    Exits immediately if the file is missing, malformed, or any listed path
    does not exist on disk.
    """
    if not REPOS_FILE.exists():
        print(f"[FATAL] repos.json not found at {REPOS_FILE}", flush=True)
        print(f"[FATAL] Create repos.json next to forge.py. See docstring for format.", flush=True)
        sys.exit(1)
    try:
        raw = json.loads(REPOS_FILE.read_text())
    except json.JSONDecodeError as e:
        print(f"[FATAL] repos.json is not valid JSON: {e}", flush=True)
        sys.exit(1)
    if not isinstance(raw, dict) or not raw:
        print("[FATAL] repos.json must be a non-empty JSON object.", flush=True)
        sys.exit(1)

    resolved = {}
    errors   = []

    for name, entry in raw.items():
        # Support both old flat-string format and new object format
        if isinstance(entry, str):
            # Legacy: "anvilml": "/path/to/repo"
            raw_path   = entry
            branch     = "main"
            github_url = ""
        elif isinstance(entry, dict):
            raw_path   = entry.get("path", "")
            branch     = entry.get("branch", "main")
            github_url = entry.get("github_url", "")
        else:
            errors.append(f"  {name!r}: entry must be a string path or object with 'path' key")
            continue

        if not raw_path:
            errors.append(f"  {name!r}: missing 'path' field")
            continue

        p = Path(raw_path).resolve()
        if not p.exists():
            errors.append(f"  {name!r}: path does not exist: {p}")
        elif not p.is_dir():
            errors.append(f"  {name!r}: path is not a directory: {p}")
        else:
            resolved[name] = {
                "path":       p,
                "branch":     branch,
                "github_url": github_url,
            }

    if errors:
        print("[FATAL] repos.json contains errors:", flush=True)
        for e in errors:
            print(e, flush=True)
        sys.exit(1)

    return resolved


def resolve_project_path(project: str) -> Path:
    """Return the absolute repo Path for a project name or raise KeyError."""
    if project not in REPOS:
        registered = ", ".join(sorted(REPOS.keys())) or "(none)"
        raise KeyError(
            f"Project {project!r} is not registered in repos.json. "
            f"Registered: {registered}"
        )
    return REPOS[project]["path"]


def resolve_project_branch(project: str) -> str:
    """Return the configured working branch for a project or raise KeyError."""
    if project not in REPOS:
        registered = ", ".join(sorted(REPOS.keys())) or "(none)"
        raise KeyError(
            f"Project {project!r} is not registered in repos.json. "
            f"Registered: {registered}"
        )
    return REPOS[project]["branch"]


def resolve_project_tasks_dir(project: str) -> Path:
    """
    Return the path to the tasks directory for a project.
    Convention: <repo_root>/.forge/tasks/
    """
    return resolve_project_path(project) / ".forge" / "tasks"


def repo_reports_dir(project: str) -> Path:
    """<repo>/.forge/reports/ — see docs/FORGE_AGENT_RULES.md §10."""
    return resolve_project_path(project) / ".forge" / "reports"


def repo_state_dir(project: str) -> Path:
    """<repo>/.forge/state/ — see docs/FORGE_AGENT_RULES.md §10."""
    return resolve_project_path(project) / ".forge" / "state"


def repo_current_task_file(project: str) -> Path:
    return repo_state_dir(project) / "CURRENT_TASK.md"


def ensure_repo_forge_dirs(project: str) -> None:
    """Create .forge/reports/ and .forge/state/ inside the target repo if absent."""
    repo_reports_dir(project).mkdir(parents=True, exist_ok=True)
    repo_state_dir(project).mkdir(parents=True, exist_ok=True)


def write_current_task_file(task: dict, step: str, status: str) -> None:
    """
    Write .forge/state/CURRENT_TASK.md before invoking OpenCode.

    OpenCode reads this file at session start (agent §1) and verifies that
    the Task field matches the injected TASK_ID.  The Forge must write it before
    every OpenCode invocation so the identity check always passes.

    step   — "PLAN" or "IMPLEMENT"
    status — "IN_PROGRESS" when written by The Forge before OpenCode runs.
             OpenCode overwrites this with COMPLETE, PARTIAL, or BLOCKED at
             session end.  The Forge never reads back this file — it uses
             state.json exclusively.
    """
    ensure_repo_forge_dirs(task["project"])
    content = (
        f"Task: {task['id']}\n"
        f"Step: {step}\n"
        f"Status: {status}\n"
        f"Updated: {_ts()}\n"
    )
    repo_current_task_file(task["project"]).write_text(content)
    log(f"[{task['id']}] Wrote CURRENT_TASK.md  Step={step}  Status={status}")


# ─── Branch management ────────────────────────────────────────────────────────

def get_current_branch(repo_path: Path) -> Optional[str]:
    """Return the name of the currently checked-out branch, or None on error."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def ensure_on_branch(project: str) -> bool:
    """
    Verify the repo is on the branch configured in repos.json.
    If it is on a different branch, attempt to switch.

    Switch strategy:
      1. If the target branch exists locally  → git checkout <branch>
      2. If not local but exists on origin    → git checkout -b <branch> origin/<branch>
      3. If nowhere                           → fatal error, manual intervention required

    Returns True if the repo is (or was switched to) the correct branch.
    Returns False only on unrecoverable error; The Forge will stop the task.
    """
    try:
        repo_path  = resolve_project_path(project)
        target     = resolve_project_branch(project)
    except KeyError as e:
        log_err(f"[branch] {e}")
        return False

    current = get_current_branch(repo_path)
    if current is None:
        log_err(f"[branch] Could not determine current branch in {repo_path}")
        return False

    if current == target:
        return True  # already correct

    log_warn(
        f"[branch] {project}: on branch '{current}', "
        f"repos.json requires '{target}' — switching..."
    )

    # Check if target branch exists locally
    local_check = subprocess.run(
        ["git", "rev-parse", "--verify", target],
        cwd=repo_path, capture_output=True, text=True,
    )
    if local_check.returncode == 0:
        # Branch exists locally — just check out
        result = subprocess.run(
            ["git", "checkout", target],
            cwd=repo_path, capture_output=True, text=True,
        )
        if result.returncode == 0:
            log(f"[branch] {project}: switched to '{target}'")
            return True
        log_err(f"[branch] {project}: checkout '{target}' failed: {result.stderr.strip()}")
        return False

    # Branch does not exist locally — try to create from origin
    fetch = subprocess.run(
        ["git", "fetch", "origin"],
        cwd=repo_path, capture_output=True, text=True,
    )
    if fetch.returncode != 0:
        log_warn(f"[branch] {project}: fetch failed: {fetch.stderr.strip()}")

    remote_check = subprocess.run(
        ["git", "rev-parse", "--verify", f"origin/{target}"],
        cwd=repo_path, capture_output=True, text=True,
    )
    if remote_check.returncode == 0:
        result = subprocess.run(
            ["git", "checkout", "-b", target, f"origin/{target}"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if result.returncode == 0:
            log(f"[branch] {project}: created and switched to '{target}' from origin")
            return True
        log_err(f"[branch] {project}: checkout -b '{target}' failed: {result.stderr.strip()}")
        return False

    log_err(
        f"[branch] {project}: branch '{target}' does not exist locally or on origin. "
        f"Create it manually: git checkout -b {target}"
    )
    return False


# Discord — bot token and guild ID still come from environment (secrets)
DISCORD_BOT_TOKEN = os.environ.get("FORGE_DISCORD_TOKEN", "")
DISCORD_GUILD_ID  = os.environ.get("FORGE_DISCORD_GUILD_ID", "")

# Channel IDs — hardcoded, no env var fallback needed
# #forge-reports   : public broadcast, never polled
# #forge-approvals : owner-only, all approval requests go here
DISCORD_REPORTS_CHANNEL_ID   = "1509917708093886475"
DISCORD_APPROVALS_CHANNEL_ID = "1509917666889044068"

# Owner gate — only reactions from this Discord user ID are acted upon.
# User IDs are permanent; usernames can be changed.
FORGE_OWNER_ID = "334811986019745792"

# OpenCode
OPENCODE_BIN   = os.environ.get("FORGE_OPENCODE_BIN", "opencode")
OPENCODE_TIMEOUT = int(os.environ.get("FORGE_OPENCODE_TIMEOUT", str(60 * 120)))  # 120 min
OPENCODE_RETRIES = int(os.environ.get("FORGE_OPENCODE_RETRIES", "3"))
OPENCODE_RETRY_DELAY = int(os.environ.get("FORGE_OPENCODE_RETRY_DELAY", "60"))

# Context window size for the running model (tokens).
# Used to compute context usage percentage in context.log.
# Default: 262144 (256k) — matches Qwen3 35B A3B at 256k context.
OPENCODE_CONTEXT_WINDOW = int(os.environ.get("FORGE_CONTEXT_WINDOW", str(262144)))

# Model IDs — OpenCode provider/model format, passed via --model flag.
# llama-swap applies the correct sampling params server-side via setParamsByID.
# planning: used for STEP 1 (plan mode, all tasks) — forge-plan agent
# coding:   used for STEP 2-4 (act mode, all tasks) — forge-act agent
MODEL_PLANNING = os.environ.get("FORGE_MODEL_PLANNING", "llama.cpp/Qwen3.6-35B-A3B:planning")
MODEL_CODING   = os.environ.get("FORGE_MODEL_CODING",   "llama.cpp/Qwen3.6-35B-A3B:coding")

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

def load_tasks(project: Optional[str] = None, phase: Optional[int] = None) -> list[dict]:
    """
    Load task definitions from <repo>/.forge/tasks/tasks_phase<NNN>.json files
    and return a merged, deduplicated list.

    Resolution strategy:
      - If project is given: load tasks only from that project's .forge/tasks/ dir.
      - If project is None:  load tasks from ALL registered projects' .forge/tasks/ dirs.
      - If phase is given:   include only phase files with number <= phase.
      - If phase is None:    include all phase files found.

    Duplicate task IDs across any files are a fatal error.

    The caller receives one flat list ordered by (project_name, phase_number, file_order).
    Tasks never cross project borders — each task targets exactly one project.
    """
    import re as _re
    pattern = _re.compile(r"^tasks_phase(\d{3})\.json$", _re.IGNORECASE)

    projects_to_load = [project] if project else sorted(REPOS.keys())

    merged:   list[dict]       = []
    seen_ids: dict[str, Path]  = {}
    files_loaded: list[str]    = []

    for proj in projects_to_load:
        try:
            tasks_dir = resolve_project_tasks_dir(proj)
        except KeyError:
            continue  # project not in REPOS — already caught by schema validation

        if not tasks_dir.is_dir():
            log_warn(f"Tasks directory not found for {proj!r}: {tasks_dir} — skipping")
            continue

        found: list[tuple[int, Path]] = []
        for p in tasks_dir.iterdir():
            m = pattern.match(p.name)
            if m:
                found.append((int(m.group(1)), p))
        found.sort(key=lambda x: x[0])

        if phase is not None:
            candidates = [p for n, p in found if n <= phase]
        else:
            candidates = [p for _, p in found]

        for path in candidates:
            try:
                chunk: list[dict] = json.loads(path.read_text())
            except json.JSONDecodeError as e:
                log_err(f"Invalid JSON in {path}: {e}")
                sys.exit(1)
            if not isinstance(chunk, list):
                log_err(f"{path}: expected a JSON array, got {type(chunk).__name__}")
                sys.exit(1)
            for task in chunk:
                tid = task.get("id", "<missing>")
                if tid in seen_ids:
                    log_err(
                        f"Duplicate task ID {tid!r} found in both "
                        f"{seen_ids[tid].name} and {path.name}"
                    )
                    sys.exit(1)
                seen_ids[tid] = path
                merged.append(task)
            files_loaded.append(str(path.relative_to(FORGE_DIR.parent)
                                    if path.is_relative_to(FORGE_DIR.parent)
                                    else path))

    if not merged:
        log_err(
            "No task files found. Each project must have .forge/tasks/tasks_phase001.json "
            "(and subsequent phase files) inside its repository root."
        )
        sys.exit(1)

    log(f"Loaded {len(merged)} tasks from {len(files_loaded)} file(s): "
        f"{', '.join(Path(f).name for f in files_loaded)}")
    return merged


def build_dag(tasks: list[dict]) -> dict[str, dict]:
    return {t["id"]: t for t in tasks}


def validate_task_schema(task: dict) -> list[str]:
    """
    Validate a task dict against the required schema.
    Returns a list of error strings (empty = valid).
    Each task must have exactly one 'project' (not 'repos') referencing a
    registered entry in repos.json.  Multi-repo tasks are not permitted;
    they must be split into separate single-project tasks.
    """
    errors = []
    if "id" not in task:
        errors.append("missing required field 'id'")
    if "description" not in task:
        errors.append("missing required field 'description'")
    if "phase" not in task:
        errors.append("missing required field 'phase'")

    # 'repos' is the old field name — catch it and tell the author what to fix
    if "repos" in task and "project" not in task:
        errors.append(
            f"field 'repos' is no longer supported. "
            f"Rename it to 'project' and set a single project name string. "
            f"Multi-repo tasks must be split into separate tasks."
        )
    elif "project" not in task:
        errors.append("missing required field 'project' (string, e.g. 'anvilml')")
    else:
        project = task["project"]
        if not isinstance(project, str) or not project.strip():
            errors.append("field 'project' must be a non-empty string")
        elif project not in REPOS:
            registered = ", ".join(sorted(REPOS.keys())) or "(none)"
            errors.append(
                f"project {project!r} is not registered in repos.json. "
                f"Registered: {registered}"
            )
    return errors


def find_next_task(tasks: list[dict], state: dict) -> Optional[dict]:
    """Return the first unblocked task not yet completed or failed."""
    completed    = set(state["completed"])
    failed       = set(state["failed"])
    needs_review = set(state["needs_review"])
    blocked      = failed | needs_review

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
    completed    = set(state["completed"])
    failed       = set(state["failed"])
    needs_review = set(state["needs_review"])
    in_progress  = state.get("in_progress")

    print(f"\n{'Task':<12} {'Status':<14} {'Project':<12} {'Description'}")
    print("─" * 90)
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
        proj = task.get("project", "?")
        print(f"{tid:<12} {status:<14} {proj:<12} {task['description']}")
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

strong { font-weight: 600; }
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

    title_html  = f"<title>{title}</title>" if title else ""
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
        pdf_filename = re.sub(r"\.(md|txt|html)$", "", filename) + ".pdf"
        pdf_bytes    = _markdown_to_pdf(file_content, title=pdf_filename)

        try:
            headers = {"Authorization": self.headers["Authorization"]}
            if pdf_bytes:
                files = {"file": (pdf_filename, pdf_bytes, "application/pdf")}
                log(f"Discord send_file: sending PDF ({len(pdf_bytes)} bytes) as {pdf_filename}")
            else:
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

def get_recent_commits(repo_path: Path, branch: Optional[str] = None) -> list[str]:
    """
    Return commit one-liners for this task only.
    When branch is provided, returns commits in origin/<branch>..HEAD (unpushed only).
    Falls back to git log -5 if branch is not provided or origin ref is unavailable.
    """
    try:
        if branch:
            result = subprocess.run(
                ["git", "log", f"origin/{branch}..HEAD", "--oneline"],
                cwd=repo_path, capture_output=True, text=True,
            )
            if result.returncode == 0:
                lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
                if lines:
                    return lines
        # Fallback: last 5 commits
        result = subprocess.run(
            ["git", "log", "-5", "--oneline"],
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

def has_unpushed_commits(repo_path: Path, branch: str) -> bool:
    """Return True if the local branch is ahead of origin/<branch>."""
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", f"origin/{branch}..HEAD"],
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

def reset_repo_to_origin(repo_path: Path, repo_label: str, branch: str) -> bool:
    """
    Hard-reset repo to origin/<branch>, discarding all local commits
    and unstaged changes. Returns True on success.
    """
    log(f"[git] Resetting {repo_label} to origin/{branch}...")
    try:
        fetch = subprocess.run(
            ["git", "fetch", "origin", branch],
            cwd=repo_path, capture_output=True, text=True,
        )
        if fetch.returncode != 0:
            log_warn(f"[git] fetch failed in {repo_label}: {fetch.stderr}")

        reset = subprocess.run(
            ["git", "reset", "--hard", f"origin/{branch}"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if reset.returncode == 0:
            log(f"[git] {repo_label} reset to origin/{branch}: {reset.stdout.strip()}")
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

def revert_task_repo(task: dict) -> bool:
    """
    Reset the repository for this task to origin/<branch>, discarding all
    local commits and unstaged changes. Called before retrying a failed task.
    Returns True on success.
    """
    project = task["project"]
    try:
        path   = resolve_project_path(project)
        branch = resolve_project_branch(project)
    except KeyError as e:
        log_err(f"[git] revert_task_repo: {e}")
        return False

    dirty    = has_dirty_working_tree(path)
    unpushed = has_unpushed_commits(path, branch)

    if not dirty and not unpushed:
        log(f"[git] {project}: clean, nothing to reset")
        return True

    log_warn(f"[git] {project}: resetting to origin/{branch} "
             f"({'unpushed commits + ' if unpushed else ''}{'dirty tree' if dirty else ''})")
    ok = reset_repo_to_origin(path, project, branch)
    if ok and dirty:
        clean_repo_working_tree(path, project)
    return ok

def validate_commit_messages(task: dict) -> list[str]:
    """
    Check recent commits in the task's project repo against Conventional Commits.
    Returns a list of warning strings (empty = all good).
    Convention: type(scope): description   (docs/FORGE_AGENT_RULES.md §3.4)
    """
    VALID_TYPES  = {"feat", "fix", "chore", "docs", "test", "refactor"}
    VALID_SCOPES = {
        # Crate-level scopes
        "anvilml-core", "anvilml-ipc", "anvilml-hardware", "anvilml-registry",
        "anvilml-worker", "anvilml-scheduler", "anvilml-server",
        "py-worker",
        # Project-level scopes (used by The Forge for workspace-wide scaffold tasks)
        "anvilml", "bloomeryui", "sindristudio",
    }
    CONVENTIONAL_RE = re.compile(r"^(\w+)\(([^)]+)\):\s+\S")

    project = task.get("project", "")
    warnings = []
    try:
        path   = resolve_project_path(project)
        branch = resolve_project_branch(project)
    except KeyError:
        return [f"project {project!r} not in repos.json — cannot validate commits"]

    if not path.exists():
        return [f"project {project!r} path does not exist: {path}"]
    if not has_unpushed_commits(path, branch):
        return []

    # Validate only the single HEAD commit — The Forge's own commit for this task.
    # Validating all unpushed commits would surface noise from prior runs or
    # manual commits that are not the responsibility of the current task.
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=path, capture_output=True, text=True,
        )
        subjects = [l.strip() for l in result.stdout.splitlines() if l.strip()]
    except Exception as e:
        return [f"git log failed for {project!r}: {e}"]

    for subject in subjects:
        m = CONVENTIONAL_RE.match(subject)
        if not m:
            warnings.append(f"{project}: non-conventional commit: `{subject[:80]}`")
            continue
        ctype, scope = m.group(1), m.group(2)
        if ctype not in VALID_TYPES:
            warnings.append(
                f"{project}: unknown type `{ctype}` in: `{subject[:80]}`"
            )
        if scope not in VALID_SCOPES:
            warnings.append(
                f"{project}: unknown scope `{scope}` in: `{subject[:80]}` "
                f"— valid scopes: {', '.join(sorted(VALID_SCOPES))}"
            )

    return warnings


def collect_commit_info(task: dict) -> dict:
    """
    Collect commit info for this task from the project repo.
    Only includes commits not yet pushed to origin (origin/<branch>..HEAD).
    Returns {project_name: {commits: [...], changed_files: [...]}}
    """
    project = task.get("project", "")
    info = {}
    try:
        path   = resolve_project_path(project)
        branch = resolve_project_branch(project)
    except KeyError:
        return info
    if path.exists():
        info[project] = {
            "commits":       get_recent_commits(path, branch=branch),
            "changed_files": get_changed_files(path),
        }
    return info


def _forge_push(task: dict) -> bool:
    """
    Push the task's project repo to origin/<branch>.
    Called by The Forge after push approval.
    OpenCode stages files; The Forge commits and is the only actor that pushes.
    Returns True on success.
    """
    project = task["project"]
    try:
        path   = resolve_project_path(project)
        branch = resolve_project_branch(project)
    except KeyError as e:
        log_err(f"[git] _forge_push: {e}")
        return False

    if not has_unpushed_commits(path, branch):
        log(f"[git] {project}: nothing to push")
        return True

    log(f"[git] Pushing {project} to origin/{branch}...")
    try:
        result = subprocess.run(
            ["git", "push", "origin", branch],
            cwd=path, capture_output=True, text=True,
        )
        if result.returncode == 0:
            log(f"[git] {project}: pushed successfully")
            return True
        log_err(f"[git] {project}: push failed: {result.stderr.strip()}")
        return False
    except Exception as e:
        log_err(f"[git] {project}: push exception: {e}")
        return False


def _forge_commit(task: dict) -> Optional[str]:
    """
    Stage and commit everything in the task's project repo.

    Staged content:
      - All source/test/CI changes made by OpenCode
      - .forge/reports/<task_id>_plan.md
      - .forge/reports/<task_id>_implement.md
      - .forge/state/CURRENT_TASK.md

    The commit message is derived from the task description and uses
    Conventional Commits format.  The Forge is the sole author of this
    commit; OpenCode is not permitted to commit in the project repo during
    the ACT session (OpenCode only stages; The Forge commits and pushes).

    Returns the short commit hash on success, None on error or nothing-to-commit.
    """
    project   = task["project"]
    task_id   = task["id"]
    task_desc = task["description"]
    try:
        path = resolve_project_path(project)
    except KeyError as e:
        log_err(f"[git] _forge_commit: {e}")
        return None

    try:
        stage = subprocess.run(
            ["git", "add", "-A"],
            cwd=path, capture_output=True, text=True,
        )
        if stage.returncode != 0:
            log_err(f"[git] {project} git add -A failed: {stage.stderr}")
            return None

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path, capture_output=True, text=True,
        )
        if not status.stdout.strip():
            log(f"[git] {project}: nothing to commit for {task_id}")
            return None

        # Derive conventional commit type from task description
        desc_lower = task_desc.lower()
        if any(w in desc_lower for w in ("fix", "repair", "correct", "resolve")):
            commit_type = "fix"
        elif any(w in desc_lower for w in ("doc", "readme", "comment")):
            commit_type = "docs"
        elif any(w in desc_lower for w in ("refactor", "restructure")):
            commit_type = "refactor"
        elif any(w in desc_lower for w in ("test",)):
            commit_type = "test"
        else:
            commit_type = "feat"

        commit_msg = (
            f"{commit_type}({project}): {task_id} — {task_desc[:60]}\n\n"
            f"Task:        {task_id}\n"
            f"Description: {task_desc}\n"
            f"Phase:       {task.get('phase', '?')}\n"
            f"Reports:     .forge/reports/{task_id}_plan.md\n"
            f"             .forge/reports/{task_id}_implement.md\n"
            f"Committed by Forge orchestrator (not OpenCode)"
        )
        commit = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=path, capture_output=True, text=True,
        )
        if commit.returncode != 0:
            log_err(f"[git] {project} commit failed: {commit.stderr}")
            return None

        hash_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=path, capture_output=True, text=True,
        )
        return hash_result.stdout.strip()

    except Exception as e:
        log_err(f"[git] _forge_commit exception for {project}: {e}")
        return None

# ─── Disk report files ─────────────────────────────────────────────────────────
# Reports live inside the target repository under .forge/reports/.
# This keeps the reports version-controlled alongside the code they describe
# and matches docs/FORGE_AGENT_RULES.md §10.

def plan_report_path(task: dict) -> Path:
    """<repo>/.forge/reports/<TASK_ID>_plan.md"""
    return repo_reports_dir(task["project"]) / f"{task['id']}_plan.md"

def implement_report_path(task: dict) -> Path:
    """<repo>/.forge/reports/<TASK_ID>_implement.md"""
    return repo_reports_dir(task["project"]) / f"{task['id']}_implement.md"

def write_forge_plan_report(task: dict, plan_text: str, attempt: int) -> Path:
    """
    Ensure <repo>/.forge/reports/<TASK_ID>_plan.md exists on disk.

    If OpenCode wrote the file during the PLAN session it is left untouched.
    If OpenCode failed to write it The Forge writes a minimal valid report from
    whatever plan text was captured from stdout, so the Discord attachment
    and approval flow can still proceed.

    This file is permanent — it is never overwritten by the ACT session.
    The Forge stages and commits it as part of _forge_commit().
    Returns the report path.
    """
    ensure_repo_forge_dirs(task["project"])
    report_path = plan_report_path(task)

    if not report_path.exists():
        header = (
            f"# Plan Report: {task['id']}\n\n"
            f"| Field       | Value |\n"
            f"|-------------|-------|\n"
            f"| Task ID     | {task['id']} |\n"
            f"| Phase       | {task.get('phase', '?')} |\n"
            f"| Description | {task['description']} |\n"
            f"| Depends on  | {', '.join(task.get('prereqs', [])) or 'none'} |\n"
            f"| Project     | {task['project']} |\n"
            f"| Attempt     | {attempt} |\n\n"
            f"## Plan\n\n"
            f"{plan_text}\n"
        )
        report_path.write_text(header)
        log(f"[{task['id']}] Plan report written to "
            f"{report_path.relative_to(resolve_project_path(task['project']))}")
    else:
        log(f"[{task['id']}] Plan report already exists (written by OpenCode) — not overwriting")

    return report_path

def read_plan_report(task: dict) -> str:
    """Read <repo>/.forge/reports/<TASK_ID>_plan.md, or return empty string."""
    p = plan_report_path(task)
    return p.read_text() if p.exists() else ""

def read_implement_report(task: dict) -> str:
    """Read <repo>/.forge/reports/<TASK_ID>_implement.md, or return empty string."""
    p = implement_report_path(task)
    return p.read_text() if p.exists() else ""

def extract_plan_section(report_text: str, task_id: str) -> str:
    """Extract the ## Plan section from a plan report, or return the full text."""
    if not report_text:
        return f"[Plan report not yet written for {task_id}]"
    match = re.search(r"## Plan\n(.*?)(?=^##|\Z)", report_text, re.DOTALL | re.MULTILINE)
    if match:
        return match.group(0).strip()
    return report_text[:3000]

def _is_thinking_trace(report_text: str) -> bool:
    """
    Return True if the plan report is a thinking trace rather than a properly
    structured plan document.

    A valid plan must:
    1. Start with "# Plan Report:" as its first non-empty line.
    2. Contain ALL three mandatory section headers from docs/FORGE_AGENT_RULES.md (plan report format).

    Thinking traces typically start with first-person narration ("I'll", "Now",
    "Let me", etc.) and lack the required structural sections.
    """
    if not report_text or not report_text.strip():
        return True

    # Check the file starts with the required heading, not first-person narration
    first_line = report_text.lstrip().splitlines()[0].strip()
    if not first_line.startswith("# Plan Report:"):
        return True

    # All three structural sections must be present
    required = ["## Objective", "## Scope", "## Acceptance Criteria"]
    return not all(marker in report_text for marker in required)

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
    project = task.get("project", "(unknown)")

    header = f"**🔐 PLAN APPROVAL — `{tid}`**"
    if attempt > 1:
        header += f" *(revision {attempt})*"

    parts = [
        header,
        f"**{desc}**",
        f"Project: `{project}` · Prereqs: `{prereqs}`",
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
    tid  = task["id"]
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
    tid   = task["id"]
    desc  = task["description"]
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
    deadline      = time.monotonic() + timeout
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
                    f"{u.get('username', 'owner')}. OpenCode is proceeding."
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
                        f"OpenCode will revise the plan."
                    )
                else:
                    dc.send_message(
                        approvals_channel_id,
                        f"❌ **Rejection registered** — no feedback reply found.\n"
                        f"OpenCode will re-plan. Reply with feedback before reacting next time "
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

# ─── OpenCode subprocess ──────────────────────────────────────────────────────

# Agent file names (beside forge.py).  The Forge syncs these to
# ~/.config/opencode/agents/ at startup via ensure_opencode_agents().
AGENT_PLAN_NAME = "forge-plan"
AGENT_ACT_NAME  = "forge-act"

def _opencode_agents_dir() -> Path:
    """
    Return the platform-appropriate OpenCode global agents directory.
    Linux/macOS: ~/.config/opencode/agents/
    Windows:     %APPDATA%\\opencode\\agents\\
    Created if absent.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        d = base / "opencode" / "agents"
    else:
        d = Path.home() / ".config" / "opencode" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_opencode_agents() -> None:
    """
    Sync forge-plan.md and forge-act.md from the forge directory to
    ~/.config/opencode/agents/ (global OpenCode agent location).

    The forge directory is the single source of truth.  Files are only
    written when the hash differs, so re-runs are cheap.
    """
    agents_dir = _opencode_agents_dir()
    for name in (f"{AGENT_PLAN_NAME}.md", f"{AGENT_ACT_NAME}.md"):
        src = FORGE_DIR / name
        dst = agents_dir / name
        if not src.exists():
            log_warn(f"Agent file not found: {src} — OpenCode PLAN/ACT mode may fail")
            continue
        src_text = src.read_text(encoding="utf-8")
        if dst.exists() and dst.read_text(encoding="utf-8") == src_text:
            continue  # already up to date
        dst.write_text(src_text, encoding="utf-8")
        log(f"Synced agent file → {dst}")


def build_opencode_cmd(prompt: str, plan_mode: bool, cwd: Path,
                       model_id: Optional[str] = None) -> list[str]:
    """
    Build the opencode run command list.

    plan_mode=True  → uses the forge-plan agent (read-only, plan report only)
    plan_mode=False → uses the forge-act agent (full permissions, implementation)

    OpenCode's --dangerously-skip-permissions auto-approves all tool calls,
    equivalent to --auto-approve=true in prior tooling.  Permission scoping is enforced
    by the agent frontmatter, not by the CLI flag.
    """
    agent = AGENT_PLAN_NAME if plan_mode else AGENT_ACT_NAME
    cmd = [
        OPENCODE_BIN, "run",
        "--format", "json",
        "--dangerously-skip-permissions",
        "--dir", str(cwd),
        "--agent", agent,
    ]
    if model_id:
        cmd.extend(["--model", model_id])
    cmd.append(prompt)
    return cmd

def _update_context_display(task_id: str, mode: str, pct: float,
                             tokens_used: int, tokens_total: int) -> None:
    """
    Overwrite context.log with the current context usage status.
    This file is tailed in the fourth tmux pane for live monitoring.
    Color coding: green <50%, yellow 50–65%, red >=65%.
    """
    if pct >= 65:
        bar_char = "█"
        status   = "⚠  APPROACHING LIMIT"
        color    = "\033[91m"  # red
    elif pct >= 50:
        bar_char = "▓"
        status   = "◉  MONITOR"
        color    = "\033[93m"  # yellow
    else:
        bar_char = "░"
        status   = "●  OK"
        color    = "\033[92m"  # green

    reset     = "\033[0m"
    bar_width = 40
    filled    = int(bar_width * pct / 100)
    bar       = bar_char * filled + "·" * (bar_width - filled)

    content = (
        f"{color}{'─' * 54}{reset}\n"
        f"  Task    : {task_id}  [{mode}]\n"
        f"  Updated : {_ts()}\n"
        f"{'─' * 54}\n"
        f"\n"
        f"  {color}Context Usage:  {pct:.1f}%  {status}{reset}\n"
        f"\n"
        f"  [{color}{bar}{reset}]\n"
        f"  {tokens_used:,} / {tokens_total:,} tokens\n"
        f"\n"
        f"  Threshold : {color}65% = {int(tokens_total * 0.65):,} tokens{reset}\n"
        f"{'─' * 54}\n"
    )
    try:
        CONTEXT_LOG_FILE.write_text(content)
    except Exception:
        pass  # non-critical


def _summarise_command(cmd: str) -> str:
    """
    Return a short human-readable description of a shell command for opencode.log.
    Avoids truncating mid-token by recognising common patterns.
    """
    import re as _re
    s = cmd.strip()

    # Bare redirect: > /path/to/file  (agent writing a file with no command)
    m = _re.match(r'^>\s*(\S+)', s)
    if m:
        return f"write → {m.group(1).rsplit('/', 1)[-1]}"

    # cat > file (heredoc write)
    m = _re.match(r'cat\s*>\s*(\S+)', s)
    if m:
        return f"write → {m.group(1).rsplit('/', 1)[-1]}"

    # python3 -c "..." or python3 << 'EOF'  (inline python)
    if _re.match(r'python3?\s+(-c\s+|<<)', s):
        for line in s.splitlines():
            m = _re.search(r"open\([\"']([^\"']+)[\"']", line)
            if m:
                return f"python → {m.group(1).rsplit('/', 1)[-1]}"
        return "python inline"

    # python3 /path/to/script.py
    m = _re.match(r'python3?\s+(\S+\.py)', s)
    if m:
        return m.group(1).rsplit('/', 1)[-1]

    # cargo <subcommand> [args]
    m = _re.match(r'(cargo\s+\w+(?:\s+-p\s+\S+)?(?:\s+--\S+)*)', s)
    if m:
        return m.group(1)[:80]

    # git <subcommand> [args]
    m = _re.match(r'(git\s+\S+(?:\s+\S+){0,3})', s)
    if m:
        return m.group(1)[:60]

    # find / grep / pytest / tee — show as-is up to 80 chars
    if _re.match(r'(find|grep|pytest|tee|ls|cp|mv|rm|mkdir|touch)\s', s):
        return s[:80]

    # Default: first 80 chars
    return s[:80]


def _write_opencode_log(clf, event: dict, token_buf: list[str],
                        task_id: str = "", mode: str = "",
                        session_tokens: dict = None) -> None:
    """
    Write a human-readable line to opencode.log for a single OpenCode NDJSON event.
    Also maintains cumulative token counts and writes context usage to context.log.

    OpenCode --format json event types (from observed schema):
      step_start   — iteration begins (suppressed — noise with no user value)
      tool_use     — tool call + result combined; tool name, input, output, timing
      step_finish  — iteration ends; tokens, cost, reason
                     reason="tool-calls" -> suppressed (next tool call follows immediately)
                     reason="stop"       -> model finished; emit compact token summary
      text         — model prose output (narration, reasoning commentary, final answer)
      error        — session-level error from OpenCode or the provider

    Token tracking:
      step_finish carries per-step token counts. Cumulative input tokens are used
      to approximate context growth against OPENCODE_CONTEXT_WINDOW.

      session_tokens dict (mutated in-place by caller):
        "input_total"    — cumulative input tokens this session
        "output_total"   — cumulative output tokens this session
        "reasoning_total"— cumulative reasoning tokens (non-zero if model emits thinking blocks)
        "cache_read"     — cumulative cache read tokens
        "cache_write"    — cumulative cache write tokens
        "cost_total"     — cumulative cost (float, USD)
        "steps"          — number of step_finish events seen
        "_last_logged_pct" — last context % that triggered a threshold log line

    Log structure goal: readable narrative flow.
      - Model prose (text events) appears inline between tool calls
      - Tool calls show a call / result as a pair with no surrounding separators
      - Context % appears only when crossing 50% / 65% thresholds, and at session end
      - Errors are always visible with X prefix and also written to forge.log
    """
    if session_tokens is None:
        session_tokens = {}

    etype = event.get("type", "")

    # Timestamp: OpenCode uses Unix milliseconds in the "timestamp" field
    raw_ts = event.get("timestamp")
    if isinstance(raw_ts, (int, float)) and raw_ts > 1_000_000_000_000:
        ts = datetime.fromtimestamp(raw_ts / 1000, tz=timezone.utc).astimezone().strftime("%H:%M:%S")
    else:
        ts = _ts()[11:19]  # fallback: HH:MM:SS from forge clock

    def flush_tokens() -> None:
        if token_buf:
            text = "".join(t for t in token_buf if not t.startswith("\x00")).strip()
            if text:
                clf.write(f"  {text}\n")
                clf.flush()
            token_buf.clear()

    # step_start: suppressed — no user value, tool calls provide all structure
    if etype == "step_start":
        flush_tokens()

    # text: model prose, narration, reasoning commentary
    elif etype == "text":
        flush_tokens()
        part = event.get("part", {})
        text = part.get("text", "").strip()
        if text:
            clf.write("\n")
            for line in text.splitlines():
                clf.write(f"  {line}\n")
            clf.flush()

    # tool_use: call + result pair
    elif etype == "tool_use":
        flush_tokens()
        part      = event.get("part", {})
        tool_name = part.get("tool", "?")
        state     = part.get("state", {})
        inp       = state.get("input", {})
        out       = state.get("output", "")
        timing    = state.get("time", {})
        title     = state.get("title", "")

        t_start = timing.get("start", 0)
        t_end   = timing.get("end",   0)
        dur_ms  = (t_end - t_start) if (t_start and t_end) else 0
        dur_str = f" {dur_ms}ms" if dur_ms else ""

        if tool_name == "read":
            hint = f" {inp.get('filePath', '')}"
        elif tool_name in ("read_files", "readFiles"):
            paths = inp.get("paths", inp.get("files", []))
            hint  = f" {', '.join(str(p) for p in paths[:3])}" if isinstance(paths, list) else f" {paths}"
        elif tool_name in ("edit", "write", "create"):
            hint = f" {inp.get('filePath', inp.get('path', ''))}"
        elif tool_name in ("bash", "run_commands", "execute_command"):
            cmds    = inp.get("commands", [])
            raw_cmd = str(cmds[0]) if (isinstance(cmds, list) and cmds) else str(inp.get("command", inp.get("cmd", "")))
            hint    = f" $ {_summarise_command(raw_cmd)}"
        elif tool_name in ("glob", "grep", "search"):
            q    = inp.get("pattern", inp.get("query", inp.get("glob", "")))
            hint = f" {str(q)[:80]!r}"
        elif tool_name == "list":
            hint = f" {inp.get('path', '')}"
        else:
            hint = ""
            for key in ("filePath", "path", "command", "url", "query", "description"):
                if key in inp:
                    hint = f" {str(inp[key])[:80]}"
                    break

        clf.write(f"  [{ts}] {tool_name}{hint}{dur_str}\n")

        status_val = state.get("status", "")
        if status_val == "error" or (isinstance(out, str) and out.lower().startswith("error")):
            err_text = str(out)[:200] if isinstance(out, str) else str(state.get("error", ""))[:200]
            clf.write(f"       X {err_text}\n")
        elif tool_name in ("read", "read_files", "readFiles") and isinstance(out, str):
            lc = out.count("\n")
            clf.write(f"       + {title or 'file'} ({lc} lines)\n")
        elif tool_name in ("bash", "run_commands", "execute_command") and isinstance(out, str):
            first = next((l.strip() for l in out.splitlines() if l.strip()), "")
            clf.write(f"       + {first[:160]}\n" if first else "       +\n")
        elif isinstance(out, str) and out.strip():
            shown = " ".join(out.strip().split())[:120]
            clf.write(f"       + {shown}\n")
        else:
            clf.write(f"       +\n")
        clf.flush()

    # step_finish: token accounting; emit only on stop or threshold crossing
    elif etype == "step_finish":
        flush_tokens()
        part   = event.get("part", {})
        reason = part.get("reason", "?")
        toks   = part.get("tokens", {})
        cost   = part.get("cost", 0.0)

        inp_step = int(toks.get("input",     0))
        out_step = int(toks.get("output",    0))
        rsn_step = int(toks.get("reasoning", 0))
        cache    = toks.get("cache", {})
        cr_step  = int(cache.get("read",  0))
        cw_step  = int(cache.get("write", 0))

        session_tokens["input_total"]     = session_tokens.get("input_total",     0) + inp_step
        session_tokens["output_total"]    = session_tokens.get("output_total",    0) + out_step
        session_tokens["reasoning_total"] = session_tokens.get("reasoning_total", 0) + rsn_step
        session_tokens["cache_read"]      = session_tokens.get("cache_read",      0) + cr_step
        session_tokens["cache_write"]     = session_tokens.get("cache_write",     0) + cw_step
        session_tokens["cost_total"]      = session_tokens.get("cost_total",      0.0) + (cost or 0.0)
        session_tokens["steps"]           = session_tokens.get("steps",           0) + 1

        ctx_used  = session_tokens["input_total"]
        ctx_total = OPENCODE_CONTEXT_WINDOW
        pct       = (ctx_used / ctx_total) * 100.0 if ctx_total else 0.0
        prev_pct  = session_tokens.get("_last_logged_pct", 0.0)
        _update_context_display(task_id, mode, pct, ctx_used, ctx_total)

        if reason == "stop":
            rsn_str = f"  rsn={rsn_step:,}" if rsn_step else ""
            cr_str  = f"  cr={cr_step:,}"   if cr_step  else ""
            clf.write(
                f"\n  [{ts}] done"
                f"  in={session_tokens['input_total']:,}"
                f"  out={session_tokens['output_total']:,}"
                f"{rsn_str}{cr_str}"
                f"  ctx={pct:.1f}%\n"
            )
            session_tokens["_last_logged_pct"] = pct
        elif pct >= 65 and prev_pct < 65:
            clf.write(f"  [{ts}] CONTEXT {pct:.1f}% ({ctx_used:,}/{ctx_total:,}) -- APPROACHING LIMIT\n")
            session_tokens["_last_logged_pct"] = pct
        elif pct >= 50 and prev_pct < 50:
            clf.write(f"  [{ts}] context {pct:.1f}% ({ctx_used:,}/{ctx_total:,})\n")
            session_tokens["_last_logged_pct"] = pct
        # reason="tool-calls" below threshold: no output

        clf.flush()

    # error: always visible; propagated to forge.log
    elif etype == "error":
        flush_tokens()
        err_obj = event.get("error", {})
        if isinstance(err_obj, dict):
            name    = err_obj.get("name", "UnknownError")
            data    = err_obj.get("data", {})
            message = data.get("message", str(err_obj)) if isinstance(data, dict) else str(data)
        else:
            name    = "error"
            message = str(err_obj)
        clf.write(f"\n  [{ts}] ERROR {name}: {message[:240]}\n")
        clf.flush()
        log_err(f"[{task_id}] OpenCode session error -- {name}: {message[:160]}")

    # unhandled event types
    else:
        try:
            with open(OPENCODE_SKIPPED_LOG_FILE, "a") as skf:
                skf.write(f"[{ts}] type={etype!r} keys={list(event.keys())} "
                          f"raw={json.dumps(event)[:200]}\n")
        except Exception:
            pass


def run_opencode(
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
    Run OpenCode CLI with retry logic for llama.cpp failures.
    Returns (success: bool, output_text: str).

    model_id is passed via --model to select the llama-swap variant:
      openai-compatible/Qwen3.6-35B-A3B:planning — PLAN sessions (forge-plan agent)
      openai-compatible/Qwen3.6-35B-A3B:coding   — ACT sessions  (forge-act agent)

    OpenCode output is parsed and written to OPENCODE_LOG_FILE in human-readable form.
    Monitor live with: tail -f forge/opencode.log
    Context usage:    tail -f forge/context.log
    """
    cmd        = build_opencode_cmd(prompt, plan_mode, cwd, model_id=model_id)
    mode_label = "PLAN" if plan_mode else "ACT"
    model_label = model_id or "default"
    log(f"[{task_id}] Running OpenCode {mode_label} mode — model: {model_label} "
        f"(timeout {OPENCODE_TIMEOUT}s, attempt {attempt_number})")
    log(f"[{task_id}] OpenCode output → {OPENCODE_LOG_FILE}")

    full_output = ""

    for attempt in range(1, OPENCODE_RETRIES + 1):
        if attempt > 1:
            delay = OPENCODE_RETRY_DELAY * attempt
            msg = (f"⚠️ `{task_id}` OpenCode {mode_label} attempt {attempt}/{OPENCODE_RETRIES} "
                   f"— waiting {delay}s (llama.cpp may have crashed)")
            log_warn(msg)
            if dc and approvals_channel_id:
                dc.send_message(approvals_channel_id, msg)
            time.sleep(delay)

        text_output:    list[str] = []
        token_buf:      list[str] = []
        session_tokens: dict      = {}
        exit_code = -1

        with open(OPENCODE_LOG_FILE, "a") as clf:
            clf.write(
                f"\n{'─'*60}\n"
                f"[{_ts()}] [{task_id}] OpenCode {mode_label} — attempt {attempt}/{OPENCODE_RETRIES}\n"
                f"{'─'*60}\n"
            )
        _update_context_display(task_id, mode_label, 0.0, 0, OPENCODE_CONTEXT_WINDOW)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            with open(OPENCODE_LOG_FILE, "a") as clf:
                for line in proc.stdout:
                    raw = line.rstrip()
                    try:
                        event = json.loads(raw)
                        _write_opencode_log(clf, event, token_buf,
                                            task_id=task_id, mode=mode_label,
                                            session_tokens=session_tokens)
                        etype = event.get("type", "")
                        # Collect text output for plan extraction
                        if etype == "text":
                            part = event.get("part", {})
                            t = part.get("text", "")
                            if t:
                                text_output.append(t)
                    except json.JSONDecodeError:
                        if raw:
                            clf.write(f"{raw}\n")
                            clf.flush()
                            text_output.append(raw)

                if token_buf:
                    clf.write("  " + "".join(token_buf).strip() + "\n")
                    clf.flush()
                    token_buf.clear()

            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            exit_code = proc.returncode

        except subprocess.TimeoutExpired:
            proc.kill()
            log_err(f"[{task_id}] OpenCode {mode_label} timed out after {OPENCODE_TIMEOUT}s")
            exit_code = -1
        except FileNotFoundError:
            log_err(f"OpenCode binary not found: {OPENCODE_BIN}")
            log_err("Install with: npm install -g opencode-ai")
            sys.exit(1)

        # Write session token summary to opencode.log
        with open(OPENCODE_LOG_FILE, "a") as clf:
            clf.write(f"[{_ts()}] [{task_id}] OpenCode {mode_label} exited: {exit_code}\n")
            if session_tokens:
                clf.write(
                    f"[{_ts()}] [{task_id}] Session tokens — "
                    f"in={session_tokens.get('input_total', 0):,}  "
                    f"out={session_tokens.get('output_total', 0):,}  "
                    f"rsn={session_tokens.get('reasoning_total', 0):,}  "
                    f"cache_r={session_tokens.get('cache_read', 0):,}  "
                    f"cache_w={session_tokens.get('cache_write', 0):,}  "
                    f"cost=${session_tokens.get('cost_total', 0.0):.4f}  "
                    f"steps={session_tokens.get('steps', 0)}\n"
                )

        full_output = "\n".join(text_output)

        if exit_code == 0:
            log(f"[{task_id}] OpenCode {mode_label} completed successfully")
            return True, full_output

        log_err(f"[{task_id}] OpenCode {mode_label} exited with code {exit_code}")

    return False, full_output

# ─── Task prompt builders ─────────────────────────────────────────────────────

def build_task_prompt(task: dict, feedback: str = "") -> str:
    """
    Build the prompt injected into OpenCode for the PLAN session.

    Paths must match docs/FORGE_AGENT_RULES.md §10 exactly.
    The feedback parameter carries rejection notes from a prior plan attempt.
    """
    tid     = task["id"]
    desc    = task["description"]
    context = task.get("context", "")
    phase   = task.get("phase", "1")
    project = task["project"]

    prompt = (
        f"SindriStudio Task: {tid}\n"
        f"Description: {desc}\n"
        f"Phase: {phase}\n"
        f"Project: {project}\n\n"
    )

    if context:
        prompt += f"Context:\n{context}\n\n"

    if feedback:
        prompt += f"Revision feedback from project owner:\n{feedback}\n\n"

    phase_padded = phase.zfill(3)
    prompt += (
        f"Instructions — PLAN SESSION ONLY:\n"
        f"1. Read .forge/state/CURRENT_TASK.md and verify Task field matches {tid}.\n"
        f"   If it does not match: write a one-line error to\n"
        f"   .forge/reports/{tid}_plan.md and STOP immediately.\n"
        f"2. Read docs/ENVIRONMENT.md, docs/ARCHITECTURE.md, and\n"
        f"   docs/TASKS_PHASE{phase_padded}.md.\n"
        f"3. Write the plan report to .forge/reports/{tid}_plan.md.\n"
        f"   Use the exact section structure from docs/FORGE_AGENT_RULES.md (plan report format).\n"
        f"   Do not write anything to this file until the complete plan is\n"
        f"   ready. The first and only write must start with the exact line\n"
        f"   '# Plan Report: {tid}'. Writing narration, thinking, or reading\n"
        f"   progress to this file is a session failure.\n"
        f"   Write ONLY the plan report. No source code, no test files,\n"
        f"   no build commands.\n"
        f"4. Update .forge/state/CURRENT_TASK.md:\n"
        f"     Task: {tid}\n"
        f"     Step: PLAN\n"
        f"     Status: COMPLETE\n"
        f"     Updated: <ISO 8601 UTC timestamp>\n"
        f"5. STOP. Do not proceed to implementation.\n"
        f"   The Forge orchestrator handles approval and will resume in a new session.\n"
    )
    return prompt

def build_act_prompt(task: dict, approved_plan: str) -> str:
    """
    Build the prompt injected into OpenCode for the ACT (implementation) session.

    The approved plan is injected verbatim — OpenCode must implement strictly to it.
    Paths must match docs/FORGE_AGENT_RULES.md §10.
    """
    tid     = task["id"]
    desc    = task["description"]
    phase   = task.get("phase", "1")
    project = task["project"]

    return (
        f"SindriStudio Task: {tid}\n"
        f"Description: {desc}\n"
        f"Phase: {phase}\n"
        f"Project: {project}\n\n"
        f"The plan below has been APPROVED by the project owner.\n"
        f"Proceed directly to implementation. Do not re-plan.\n\n"
        f"APPROVED PLAN:\n{approved_plan}\n\n"
        f"Instructions — ACT SESSION:\n"
        f"1. IMPLEMENT: Write all source code, tests, and CI changes as specified\n"
        f"   in the approved plan. Scope is strictly limited to the plan's\n"
        f"   'In Scope' section. Do not add anything not listed there.\n"
        f"2. FORMAT: Run `cargo fmt --all` to format all Rust source files in-place.\n"
        f"   Do NOT use --check — format in-place. Fix any errors before proceeding.\n"
        f"3. LINT: Run `cargo clippy --workspace --features mock-hardware -- -D warnings`.\n"
        f"   Fix all warnings. Zero warnings required before proceeding.\n"
        f"4. WINDOWS CROSS-CHECK: Run\n"
        f"   `cargo check --target x86_64-pc-windows-gnu --workspace --features mock-hardware`.\n"
        f"   The windows-gnu target + mingw-w64 linker are installed locally, so this\n"
        f"   runs on this Linux host. It catches #[cfg(windows)]/#[cfg(unix)] and other\n"
        f"   platform-API mistakes that pass on Linux but break the native rust-windows\n"
        f"   CI job. Zero errors required before proceeding. A clean Linux build is NOT\n"
        f"   sufficient — this cross-check must also pass. Do not relax the 1.95.0\n"
        f"   toolchain pin to make it pass.\n"
        f"5. TEST: Run the full test suite for every affected crate/package.\n"
        f"   Fix all failures. Zero failures required before proceeding.\n"
        f"   Run the full workspace suite and fix any regressions.\n"
        f"6. CONFIG DRIFT GATE: Run\n"
        f"   `cargo test -p backend --features mock-hardware -- config_reference`.\n"
        f"   This asserts the committed ./anvilml.toml key-set matches ServerConfig::default()\n"
        f"   recursively. If this task added/renamed/removed any ServerConfig field (or a\n"
        f"   field on a nested config struct), you MUST have already updated ./anvilml.toml\n"
        f"   and docs/ENVIRONMENT.md §2 in this same task — see docs/FORGE_AGENT_RULES.md §5.8. Zero\n"
        f"   failures required before proceeding. Do NOT weaken or skip this test to pass;\n"
        f"   fix anvilml.toml instead. (Skip only if the config_reference test does not yet\n"
        f"   exist, i.e. before task P3-B2 has been implemented.)\n"
        f"7. STAGE: Run git add -A inside the project repo ({project}).\n"
        f"   Do NOT run git commit or git push — The Forge commits and pushes.\n"
        f"   Do NOT make any git operations outside the {project} repo.\n"
        f"8. REPORT: Write .forge/reports/{tid}_implement.md using the exact\n"
        f"   section structure from docs/FORGE_AGENT_RULES.md (implementation report format). Include verbatim\n"
        f"   test output (Linux, the windows-gnu cross-check, and the config drift gate).\n"
        f"   Write this ONLY after all tests pass and files are staged.\n"
        f"9. UPDATE STATE: Write .forge/state/CURRENT_TASK.md:\n"
        f"     Task: {tid}\n"
        f"     Step: IMPLEMENT\n"
        f"     Status: COMPLETE\n"
        f"     Updated: <ISO 8601 UTC timestamp>\n"
        f"10. STOP. The Forge will commit, seek push approval, and push.\n"
    )

def _fmt_duration(seconds: float) -> str:
    """Format a duration into XXs, XXm:YYs, or XXh:YYm:ZZs."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m:{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h:{m:02d}m:{s:02d}s"


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
    Execute one atomic task through the full plan→approve→act→commit→push cycle.

    Channel responsibilities:
      reports_channel_id   (#forge-reports)   — post plan/impl reports as PDF. NEVER polled.
      approvals_channel_id (#forge-approvals) — approval requests, polled for reactions.

    Each task targets exactly one project (task["project"]).  The Forge resolves
    the project path from repos.json, verifies the branch, runs OpenCode in that
    repo's working directory, and writes reports into that repo's .forge/reports/.

    Returns True if task completed successfully.
    """
    tid     = task["id"]
    project = task["project"]
    try:
        repo_path = resolve_project_path(project)
    except KeyError as e:
        log_err(f"[{tid}] {e}")
        state["failed"].append(tid)
        save_state(state)
        return False

    log(f"{'='*60}")
    log(f"[{tid}] Starting task: {task['description']}")
    log(f"[{tid}] Project: {project} → {repo_path}")

    # ── Branch guard: ensure repo is on the configured branch ────────────────
    if not dry_run:
        branch_ok = ensure_on_branch(project)
        if not branch_ok:
            msg = (f"❌ `{tid}` Branch switch failed for {project}. "
                   f"Check forge.log and switch manually.")
            log_err(msg)
            if dc and approvals_channel_id:
                dc.send_message(approvals_channel_id, msg)
            state["failed"].append(tid)
            save_state(state)
            return False

    # Ensure .forge/ dirs exist in the target repo before anything is written
    ensure_repo_forge_dirs(project)

    # ── Always announce task start to #forge-reports ─────────────────────────
    if dc and reports_channel_id:
        prereqs = ", ".join(task.get("prereqs", [])) or "none"
        dc.send_message(
            reports_channel_id,
            f"⚙️ **Task `{tid}` STARTED** — {task['description']}\n"
            f"Phase {task.get('phase', '?')} · Project: `{project}` · Prereqs: `{prereqs}`"
        )

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
    feedback     = ""
    t_plan_start = 0.0  # set when OpenCode PLAN runs; 0 if plan was already approved on resume
    t_plan_end   = 0.0
    t_act_start  = 0.0  # set when OpenCode ACT runs
    t_act_end    = 0.0

    while True:
        if state.get("plan_approved") and state.get("current_plan"):
            log(f"[{tid}] Plan already approved (resuming) — skipping plan phase")
            break

        log(f"[{tid}] 📋 Plan phase (attempt {plan_attempt})")

        prompt = build_task_prompt(task, feedback=feedback)

        # Write CURRENT_TASK.md so OpenCode's §1 identity check passes
        if not dry_run:
            write_current_task_file(task, step="PLAN", status="IN_PROGRESS")

        t_plan_start = time.monotonic()
        if dry_run:
            log(f"[{tid}] [DRY RUN] Would run OpenCode PLAN mode ({MODEL_PLANNING})")
            plan_text = f"[DRY RUN] Plan for {tid}"
        else:
            success, output = run_opencode(
                prompt, plan_mode=True, cwd=repo_path,
                task_id=tid, dc=dc,
                approvals_channel_id=approvals_channel_id,
                attempt_number=plan_attempt,
                model_id=MODEL_PLANNING,
            )
            if not success:
                msg = f"❌ `{tid}` OpenCode PLAN failed after {OPENCODE_RETRIES} attempts. Stopping."
                log_err(msg)
                if dc and approvals_channel_id:
                    dc.send_message(approvals_channel_id, msg)
                state["failed"].append(tid)
                state["in_progress"] = None
                save_state(state)
                return False

            report_text = read_plan_report(task)
            plan_text   = extract_plan_section(report_text, tid)

            if not report_text or plan_text.startswith("[Plan report not yet written"):
                if output.strip():
                    plan_text = output.strip()
                    log(f"[{tid}] Plan report file absent — using stdout-captured plan text")
                else:
                    log_warn(f"[{tid}] No plan text found in report file or stdout")
                    plan_text = (
                        f"# Plan Report: {tid}\n\n"
                        f"| Field | Value |\n|-------|-------|\n"
                        f"| Task ID | {tid} |\n"
                        f"| Description | {task['description']} |\n\n"
                        f"## Plan\n\n"
                        f"*OpenCode did not produce a readable plan. "
                        f"Review forge/opencode.log for session output.*\n"
                    )

            write_forge_plan_report(task, plan_text, plan_attempt)

            # Re-read after write so the thinking-trace check always operates
            # on the actual file content, not the pre-write stale read.
            report_text = read_plan_report(task) or report_text

            # ── Auto-detect thinking-trace; delete and retry without Discord ─
            if _is_thinking_trace(report_text):
                log_warn(f"[{tid}] Plan report is a thinking trace — "
                         f"deleting and retrying (attempt {plan_attempt})")
                if dc and approvals_channel_id:
                    dc.send_message(
                        approvals_channel_id,
                        f"🔄 `{tid}` Attempt {plan_attempt} produced a thinking "
                        f"trace instead of a plan. Auto-retrying — no action needed.",
                    )
                plan_report_path(task).unlink(missing_ok=True)
                feedback = (
                    f"The plan contains the thinking trace rather than the "
                    f"prescribed plan output. Write only the final plan report — "
                    f"no narration or commentary about what you are reading or doing. "
                    f"Start directly with '# Plan Report: {tid}'."
                )
                plan_attempt += 1
                state["plan_approved"] = False
                state["current_plan"]  = None
                save_state(state)
                if plan_attempt > 5:
                    msg = f"❌ `{tid}` Thinking-trace retry limit reached. Stopping."
                    log_err(msg)
                    if dc and approvals_channel_id:
                        dc.send_message(approvals_channel_id, msg)
                    state["failed"].append(tid)
                    state["in_progress"] = None
                    save_state(state)
                    return False
                continue

        t_plan_end = time.monotonic()  # approval wait NOT included
        state["current_plan"] = plan_text
        save_state(state)

        full_report = read_plan_report(task) or plan_text

        # ── Post plan report to #forge-reports as PDF attachment ─────────────
        if dc and reports_channel_id:
            if dry_run:
                dry_run_report_msg_id = dc.send_message(
                    reports_channel_id,
                    f"📋 **[DRY RUN] PLAN REPORT — `{tid}` (Phase {task.get('phase', '?')})**\n"
                    f"_{task['description']}_\n"
                    f"_No PDF generated in dry-run mode. Approval request in #forge-approvals._"
                )
                if dry_run_report_msg_id:
                    state["plan_report_message_id"] = dry_run_report_msg_id
                    save_state(state)
            else:
                caption      = format_report_caption(task, "PLAN")
                filename     = f"{tid}_plan.md"
                report_msg_id = dc.send_file(
                    reports_channel_id, caption, filename, full_report
                )
                if report_msg_id:
                    state["plan_report_message_id"] = report_msg_id
                    save_state(state)
                    log(f"[{tid}] Plan report attached to #forge-reports (msg {report_msg_id})")

        # ── Post approval request to #forge-approvals ─────────────────────────
        if dc and approvals_channel_id:
            approval_text   = format_plan_approval_request(task, plan_attempt, feedback)
            approval_msg_id = dc.send_message(approvals_channel_id, approval_text)
            if approval_msg_id:
                dc.add_reaction(approvals_channel_id, approval_msg_id, EMOJI_APPROVE)
                time.sleep(0.75)
                dc.add_reaction(approvals_channel_id, approval_msg_id, EMOJI_REJECT)
                state["plan_approval_message_id"] = approval_msg_id
                save_state(state)
                log(f"[{tid}] Plan approval request posted to #forge-approvals (msg {approval_msg_id})")

                if dry_run:
                    log(f"[{tid}] [DRY RUN] Waiting for real approval in #forge-approvals...")
                    approved, feedback = wait_for_approval(
                        dc, approvals_channel_id, approval_msg_id,
                        reports_channel_id=reports_channel_id,
                        report_message_id=state.get("plan_report_message_id"),
                    )
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
            break
        else:
            log(f"[{tid}] ❌ Plan rejected — feedback: {feedback!r}")
            plan_attempt += 1
            state["plan_approved"] = False
            state["current_plan"]  = None
            save_state(state)
            if plan_attempt > 5:
                msg = f"❌ `{tid}` Plan rejected {plan_attempt-1} times. Stopping."
                log_err(msg)
                if dc and approvals_channel_id:
                    dc.send_message(approvals_channel_id, msg)
                state["failed"].append(tid)
                state["in_progress"] = None
                save_state(state)
                return False

    # ── Phase 2: Act ─────────────────────────────────────────────────────────
    log(f"[{tid}] ⚙️  Act phase — model: {MODEL_CODING}")

    # Write CURRENT_TASK.md so OpenCode's §1 identity check passes
    if not dry_run:
        write_current_task_file(task, step="IMPLEMENT", status="IN_PROGRESS")

    t_act_start = time.monotonic()
    if dry_run:
        log(f"[{tid}] [DRY RUN] Would run OpenCode ACT mode ({MODEL_CODING})")
        act_success = True
    else:
        act_prompt  = build_act_prompt(task, state["current_plan"])
        act_success, _ = run_opencode(
            act_prompt, plan_mode=False, cwd=repo_path,
            task_id=tid, dc=dc,
            approvals_channel_id=approvals_channel_id,
            model_id=MODEL_CODING,
        )
    t_act_end = time.monotonic()  # push approval wait NOT included

    if not act_success:
        msg = f"❌ `{tid}` OpenCode ACT failed after {OPENCODE_RETRIES} attempts. Task marked failed."
        log_err(msg)
        if dc and approvals_channel_id:
            dc.send_message(approvals_channel_id, msg)
        state["failed"].append(tid)
        state["in_progress"] = None
        save_state(state)
        return False

    # ── Forge commits the project repo ────────────────────────────────────────
    if dry_run:
        log(f"[{tid}] [DRY RUN] Skipping git commit")
    else:
        log(f"[{tid}] Committing {project} repo...")
        commit_hash = _forge_commit(task)
        if not commit_hash:
            log_warn(f"[{tid}] Nothing committed in {project} — may be expected if OpenCode "
                     f"found no changes, or check forge/opencode.log for issues.")

    # ── Validate commit message format ────────────────────────────────────────
    if not dry_run:
        commit_warnings = validate_commit_messages(task)
        if commit_warnings:
            warn_text = "\n".join(f"  • {w}" for w in commit_warnings)
            msg = (
                f"⚠️ `{tid}` Commit message issues:\n{warn_text}\n\n"
                f"Review before approving push. The Forge will proceed if you approve."
            )
            log_warn(f"[{tid}] Commit warnings:\n{warn_text}")
            if dc and approvals_channel_id:
                dc.send_message(approvals_channel_id, msg)

    # ── Collect commit info for approval message ──────────────────────────────
    commit_info = collect_commit_info(task) if not dry_run else {}

    # ── Read implementation report and merge with plan report for PDF ─────────
    impl_report_text = read_implement_report(task)
    if not impl_report_text:
        log_warn(f"[{tid}] Implementation report not found at "
                 f"{implement_report_path(task).relative_to(repo_path)}")
        impl_report_text = (
            f"# Implementation Report: {tid}\n\n"
            f"*OpenCode did not write the implementation report. "
            f"Review forge/opencode.log for session output.*\n"
        )

    plan_report_text = read_plan_report(task)
    if plan_report_text:
        full_report_text = (
            f"{impl_report_text}\n\n---\n\n"
            f"# Approved Plan (for reference)\n\n{plan_report_text}"
        )
        log(f"[{tid}] Merged plan report into implementation PDF")
    else:
        log_warn(f"[{tid}] Plan report missing — PDF will not include it")
        full_report_text = impl_report_text

    # ── Post implementation report to #forge-reports as PDF attachment ────────
    if dc and reports_channel_id:
        if dry_run:
            dry_run_impl_msg_id = dc.send_message(
                reports_channel_id,
                f"📦 **[DRY RUN] IMPLEMENTATION REPORT — `{tid}` (Phase {task.get('phase', '?')})**\n"
                f"_{task['description']}_\n"
                f"_No PDF generated in dry-run mode. Push approval request in #forge-approvals._"
            )
            if dry_run_impl_msg_id:
                state["impl_report_message_id"] = dry_run_impl_msg_id
                save_state(state)
        else:
            caption     = format_implementation_caption(task, commit_info)
            filename    = f"{tid}_implement.md"
            impl_msg_id = dc.send_file(
                reports_channel_id, caption, filename, full_report_text
            )
            if impl_msg_id:
                state["impl_report_message_id"] = impl_msg_id
                save_state(state)
                log(f"[{tid}] Implementation report attached to #forge-reports (msg {impl_msg_id})")

    # ── Post push approval request to #forge-approvals (polled) ──────────────
    if dc and approvals_channel_id:
        approval_text   = format_push_approval_request(task, commit_info)
        approval_msg_id = dc.send_message(approvals_channel_id, approval_text)
        if approval_msg_id:
            dc.add_reaction(approvals_channel_id, approval_msg_id, EMOJI_APPROVE)
            time.sleep(0.75)
            dc.add_reaction(approvals_channel_id, approval_msg_id, EMOJI_REJECT)
            state["push_approval_message_id"] = approval_msg_id
            save_state(state)
            log(f"[{tid}] Push approval request posted to #forge-approvals (msg {approval_msg_id})")

            if dry_run:
                log(f"[{tid}] [DRY RUN] Waiting for real push approval in #forge-approvals...")
            push_approved, push_feedback = wait_for_approval(
                dc, approvals_channel_id, approval_msg_id,
                reports_channel_id=reports_channel_id,
                report_message_id=state.get("impl_report_message_id"),
            )
        else:
            log_warn(f"[{tid}] Failed to post push approval — auto-approving")
            push_approved, push_feedback = True, ""
    else:
        log_warn(f"[{tid}] Discord not configured — auto-approving push")
        push_approved, push_feedback = True, ""

    if not push_approved:
        msg = (f"🔍 `{tid}` Push rejected (feedback: {push_feedback!r}). "
               f"Commit is local. Task marked needs-review.")
        log_warn(msg)
        if dc and approvals_channel_id:
            dc.send_message(approvals_channel_id, msg)
        state["needs_review"].append(tid)
        state["in_progress"] = None
        save_state(state)
        return False

    log(f"[{tid}] ✅ Push approved — pushing {project}")

    # ── Forge pushes the project repo ─────────────────────────────────────────
    if dry_run:
        log(f"[{tid}] [DRY RUN] Skipping git push")
        push_ok = True
    else:
        push_ok = _forge_push(task)
    if not push_ok:
        msg = (f"⚠️ `{tid}` Push to {project} failed. "
               f"Commit is local. Use --reset-task or push manually.")
        log_err(msg)
        if dc and approvals_channel_id:
            dc.send_message(approvals_channel_id, msg)
        state["needs_review"].append(tid)
        state["in_progress"] = None
        save_state(state)
        return False

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

    for log_file in (OPENCODE_LOG_FILE, CONTEXT_LOG_FILE):
        try:
            log_file.write_text("")
        except Exception as e:
            log_warn(f"[{tid}] Could not purge {log_file.name}: {e}")

    if dc and reports_channel_id:
        plan_dur = _fmt_duration(t_plan_end - t_plan_start)
        act_dur  = _fmt_duration(t_act_end  - t_act_start)
        dc.send_message(
            reports_channel_id,
            f"✅ **Task `{tid}` COMPLETE** — {task['description']}\n"
            f"⏱ Planning: `{plan_dur}` · Implementation: `{act_dur}` _(approval wait excluded)_"
        )

    log(f"[{tid}] ✅ Task complete")
    return True

# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="SindriStudio Forge Orchestrator")
    parser.add_argument(
        "--repo", metavar="PROJECT", required=True,
        help="Repository to operate on (must match a key in repos.json, e.g. 'anvilml'). "
             "Required — The Forge always works on exactly one repository at a time.",
    )
    parser.add_argument(
        "--task", metavar="TASK_ID",
        help="Run exactly ONE specific task (full cycle + gates), then exit. "
             "Useful for testing The Forge itself.",
    )
    parser.add_argument(
        "--phase", type=int, metavar="N",
        help="Load task files for phase N and all prior phases only. "
             "If omitted, all phase files from the target repository are loaded.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would run without executing OpenCode or waiting for approvals.",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="Print the task DAG with current status, then exit.",
    )
    parser.add_argument(
        "--reset-task", metavar="TASK_ID",
        help="Reset a task to unstarted in state.json. Does NOT revert git.",
    )
    parser.add_argument(
        "--reset-task-git", metavar="TASK_ID",
        help="Reset task to unstarted AND hard-reset repo to origin/<branch>.",
    )
    args = parser.parse_args()

    log("=" * 60)
    log("Forge starting up")

    # ── Load and validate repository registry ────────────────────────────────
    global REPOS
    REPOS = load_repos()
    for name, cfg in REPOS.items():
        log(f"  {name}: path={cfg['path']}  branch={cfg['branch']}  "
            f"github={cfg['github_url'] or '(not set)'}")

    if not DISCORD_BOT_TOKEN:
        log_warn("FORGE_DISCORD_TOKEN not set — running without Discord notifications")
    if not DISCORD_GUILD_ID:
        log_warn("FORGE_DISCORD_GUILD_ID not set — Discord channel lookup disabled")

    # ── Sync OpenCode agent files to global agents directory ─────────────────
    ensure_opencode_agents()

    # ── Validate --repo against loaded registry ───────────────────────────────
    if args.repo not in REPOS:
        registered = ", ".join(sorted(REPOS.keys())) or "(none)"
        log_err(f"--repo {args.repo!r} is not registered in repos.json. "
                f"Registered: {registered}")
        sys.exit(1)
    log(f"Operating on repository: {args.repo} → {REPOS[args.repo]['path']}")

    # ── Scope state.json to the active repository's .forge/ directory ─────────
    # Each repository manages its own Forge state independently.
    # <repo>/.forge/state.json is created on first run; ensure the dir exists.
    global STATE_FILE
    ensure_repo_forge_dirs(args.repo)
    STATE_FILE = repo_state_dir(args.repo) / "state.json"
    log(f"State file: {STATE_FILE}")

    # ── Load tasks scoped to the single target repository ─────────────────────
    # repos.json may list many repositories; The Forge always works on exactly one.
    tasks = load_tasks(project=args.repo, phase=args.phase)
    state = load_state()

    # ── Validate all tasks against schema ─────────────────────────────────────
    schema_errors = []
    for t in tasks:
        errs = validate_task_schema(t)
        if errs:
            for e in errs:
                schema_errors.append(f"  {t.get('id', '?')}: {e}")
    if schema_errors:
        log_err("Task schema errors — fix before running:")
        for e in schema_errors:
            log_err(e)
        sys.exit(1)
    log(f"Loaded {len(tasks)} tasks — schema OK")

    if args.list:
        print_dag_status(tasks, state)
        return

    if args.reset_task or args.reset_task_git:
        tid  = args.reset_task or args.reset_task_git
        dag  = build_dag(tasks)
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
            branch = resolve_project_branch(task["project"])
            log(f"Reverting {task['project']} repo for {tid} to origin/{branch}...")
            ok = revert_task_repo(task)
            if ok:
                log("✅ Repo reverted successfully")
            else:
                log_err("⚠️  Repo could not be reverted — check manually")
        return

    # Set up Discord
    dc = get_discord()
    reports_channel_id   = DISCORD_REPORTS_CHANNEL_ID
    approvals_channel_id = DISCORD_APPROVALS_CHANNEL_ID
    if dc:
        log(f"Discord reports channel (broadcast):  {reports_channel_id}")
        log(f"Discord approvals channel (polled):   {approvals_channel_id}")
        log(f"Discord owner gate:                   {FORGE_OWNER_ID}")

    # ── Main loop ─────────────────────────────────────────────────────────────
    while True:
        tasks = load_tasks(project=args.repo, phase=args.phase)  # Reload — allows hot-editing task files
        state = load_state()

        # ── Resolve the task to execute ───────────────────────────────────────
        # --task pins a specific task for the entire run.  We bypass
        # find_next_task to avoid state.json's completed list causing the wrong
        # task to be selected (e.g. if P1-A1 is already completed, find_next_task
        # would skip it and return P2-A1 instead).
        if args.task:
            dag  = build_dag(tasks)
            task = dag.get(args.task)
            if not task:
                log_err(f"Task {args.task} not found in loaded task files")
                sys.exit(1)
        else:
            task = find_next_task(tasks, state)

        if task is None:
            all_ids      = {t["id"] for t in tasks}
            completed    = set(state["completed"])
            failed       = set(state["failed"])
            needs_review = set(state.get("needs_review", []))
            remaining    = all_ids - completed - failed - needs_review

            if not remaining:
                msg = "🎉 **All tasks complete!**"
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

        # ── Single-task mode: exit after the task completes (success or not) ──
        # --task runs exactly one task then exits cleanly regardless of outcome.
        # --dry-run also exits after one task (it never loops).
        if args.task or args.dry_run:
            if not success:
                log_err(f"Task {task['id']} did not complete successfully.")
                log_err(f"To retry: python forge.py --reset-task {task['id']} then rerun with --task")
            log("Forge exiting (single-task mode)")
            break

        if not success:
            log_err(f"Task {task['id']} failed. Stopping.")
            log_err(f"To retry with clean repo: python forge.py --reset-task-git {task['id']}")
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