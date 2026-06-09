"""
forge_git.py — All git operations: commits, push, reset, status queries,
               commit validation, and report-file collection.
"""

import re
import subprocess
from pathlib import Path
from typing import Optional

from .forge_log import log, log_err, log_warn
from .forge_repos import (
    resolve_project_path,
    resolve_project_branch,
)


def get_recent_commits(repo_path: Path, branch: Optional[str] = None) -> list[str]:
    """
    Return commit one-liners for unpushed commits on branch.
    Falls back to the last 5 commits if branch is not provided.
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
            return int(result.stdout.strip() or "0") > 0
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
    """Hard-reset repo to origin/<branch>, discarding all local commits."""
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
    """Reset the repository for this task to origin/<branch>."""
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
    """
    VALID_TYPES  = {"feat", "fix", "chore", "docs", "test", "refactor"}
    VALID_SCOPES = {
        "anvilml-core", "anvilml-ipc", "anvilml-hardware", "anvilml-registry",
        "anvilml-worker", "anvilml-scheduler", "anvilml-server",
        "py-worker",
        "anvilml-testui",
        "anvilml", "bloomeryui", "sindristudio",
    }
    CONVENTIONAL_RE = re.compile(r"^(\w+)\(([^)]+)\):\s+\S")

    project  = task.get("project", "")
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
            warnings.append(f"{project}: unknown type `{ctype}` in: `{subject[:80]}`")
        if scope not in VALID_SCOPES:
            warnings.append(
                f"{project}: unknown scope `{scope}` in: `{subject[:80]}` "
                f"— valid scopes: {', '.join(sorted(VALID_SCOPES))}"
            )
    return warnings


def collect_commit_info(task: dict) -> dict:
    """Collect unpushed commit info for the task's project repo."""
    project = task.get("project", "")
    info    = {}
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


def forge_push(task: dict) -> bool:
    """
    Push the task's project repo to origin/<branch>.
    The Forge is the sole actor that pushes — OpenCode never pushes.
    """
    project = task["project"]
    try:
        path   = resolve_project_path(project)
        branch = resolve_project_branch(project)
    except KeyError as e:
        log_err(f"[git] forge_push: {e}")
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


def forge_commit(task: dict) -> Optional[str]:
    """
    Stage and commit everything in the task's project repo.
    The Forge is the sole author of all commits — OpenCode only stages.
    Returns the short commit hash on success, None on error or nothing-to-commit.
    """
    project   = task["project"]
    task_id   = task["id"]
    task_desc = task["description"]
    try:
        path = resolve_project_path(project)
    except KeyError as e:
        log_err(f"[git] forge_commit: {e}")
        return None

    try:
        stage = subprocess.run(
            ["git", "add", "-A"], cwd=path, capture_output=True, text=True,
        )
        if stage.returncode != 0:
            log_err(f"[git] {project} git add -A failed: {stage.stderr}")
            return None

        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=path, capture_output=True, text=True,
        )
        if not status.stdout.strip():
            log(f"[git] {project}: nothing to commit for {task_id}")
            return None

        desc_lower = task_desc.lower()
        if any(w in desc_lower for w in ("fix", "repair", "correct", "resolve")):
            commit_type = "fix"
        elif any(w in desc_lower for w in ("doc", "readme", "comment")):
            commit_type = "docs"
        elif any(w in desc_lower for w in ("refactor", "restructure")):
            commit_type = "refactor"
        elif "test" in desc_lower:
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
        log_err(f"[git] forge_commit exception for {project}: {e}")
        return None
