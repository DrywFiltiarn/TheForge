"""
forge_repos.py — Repository registry, path resolution, branch management,
                 and per-repo .forge/ directory helpers.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .forge_log import log, log_err, log_warn, _ts

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
    if not cfg.REPOS_FILE.exists():
        print(f"[FATAL] repos.json not found at {cfg.REPOS_FILE}", flush=True)
        print(f"[FATAL] Create repos.json next to forge.py. See docstring for format.", flush=True)
        sys.exit(1)
    try:
        raw = json.loads(cfg.REPOS_FILE.read_text())
    except json.JSONDecodeError as e:
        print(f"[FATAL] repos.json is not valid JSON: {e}", flush=True)
        sys.exit(1)
    if not isinstance(raw, dict) or not raw:
        print("[FATAL] repos.json must be a non-empty JSON object.", flush=True)
        sys.exit(1)

    resolved = {}
    errors   = []

    for name, entry in raw.items():
        if isinstance(entry, str):
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
            resolved[name] = {"path": p, "branch": branch, "github_url": github_url}

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
    """Return the path to the tasks directory: <repo_root>/.forge/tasks/"""
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
    step   — "PLAN" or "IMPLEMENT"
    status — "IN_PROGRESS" before OpenCode runs; agent overwrites with
             COMPLETE, PARTIAL, or BLOCKED at session end.
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
    """
    Return the name of the currently checked-out branch, or None on error.
    Strips the 'heads/' prefix that some git versions prepend.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            if branch.startswith("heads/"):
                branch = branch[len("heads/"):]
            return branch
    except Exception:
        pass
    return None


def ensure_on_branch(project: str) -> bool:
    """
    Verify the repo is on the branch configured in repos.json and switch if not.
    Returns True if on (or switched to) the correct branch, False on failure.
    """
    try:
        repo_path = resolve_project_path(project)
        target    = resolve_project_branch(project)
    except KeyError as e:
        log_err(f"[branch] {e}")
        return False

    current = get_current_branch(repo_path)
    if current is None:
        log_err(f"[branch] Could not determine current branch in {repo_path}")
        return False

    if current == target:
        return True

    log_warn(
        f"[branch] {project}: on branch '{current}', "
        f"repos.json requires '{target}' — switching..."
    )

    local_check = subprocess.run(
        ["git", "rev-parse", "--verify", target],
        cwd=repo_path, capture_output=True, text=True,
    )
    if local_check.returncode == 0:
        result = subprocess.run(
            ["git", "checkout", target],
            cwd=repo_path, capture_output=True, text=True,
        )
        if result.returncode == 0:
            log(f"[branch] {project}: switched to '{target}'")
            return True
        log_err(f"[branch] {project}: checkout '{target}' failed: {result.stderr.strip()}")
        return False

    fetch = subprocess.run(
        ["git", "fetch", "origin"], cwd=repo_path, capture_output=True, text=True,
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


# ─── Forge documentation sync ─────────────────────────────────────────────────

#: Documents The Forge bundles in its own docs/ directory and expects to
#: find (and keep current) in each project repo's docs/ directory.
FORGE_MANAGED_DOCS = [
    "FORGE_AGENT_RULES.md",
    "FORGE_TASK_AUTHORING_SPEC.md",
]


def _sha256(path) -> str:
    """Return the SHA-256 hex digest of a file's contents."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def ensure_forge_docs(repo_path: "Path") -> bool:
    """
    Ensure FORGE_AGENT_RULES.md and FORGE_TASK_AUTHORING_SPEC.md are present
    and current in <repo>/docs/.

    Three cases per document:
      1. Missing from repo → copy from The Forge's own docs/ silently.
      2. Present but identical (same SHA-256) → no action.
      3. Present but different → warn the user, offer to update.

    Returns True if all documents are in sync (or were synced) after the call.
    Returns False only if the user declines an update — The Forge will proceed
    but has warned the user of potential malfunction.
    """
    # Hard-stop if The Forge's own docs/ directory is missing entirely
    if not cfg.DOCS_DIR.exists():
        print(flush=True)
        print("  " + "!" * 60, flush=True)
        print(f"  FATAL: The Forge docs/ directory not found:", flush=True)
        print(f"  {cfg.DOCS_DIR}", flush=True)
        print(f"", flush=True)
        print(f"  The Forge requires a docs/ directory alongside forge.py", flush=True)
        print(f"  containing:", flush=True)
        for _doc in FORGE_MANAGED_DOCS:
            print(f"    {_doc}", flush=True)
        print(f"", flush=True)
        print(f"  Restore these files from the Forge release package and", flush=True)
        print(f"  restart. The Forge cannot operate without them.", flush=True)
        print("  " + "!" * 60, flush=True)
        print(flush=True)
        sys.exit(1)

    repo_docs = repo_path / "docs"
    repo_docs.mkdir(parents=True, exist_ok=True)

    all_synced = True

    for filename in FORGE_MANAGED_DOCS:
        src = cfg.DOCS_DIR / filename
        dst = repo_docs / filename

        if not src.exists():
            print(flush=True)
            print("  " + "!" * 60, flush=True)
            print(f"  FATAL: bundled Forge document missing:", flush=True)
            print(f"  {src}", flush=True)
            print(f"", flush=True)
            print(f"  The Forge requires both of the following documents", flush=True)
            print(f"  in its own docs/ directory alongside forge.py:", flush=True)
            for _doc in FORGE_MANAGED_DOCS:
                print(f"    docs/{_doc}", flush=True)
            print(f"", flush=True)
            print(f"  These files are part of The Forge distribution.", flush=True)
            print(f"  Restore them from the Forge release package and", flush=True)
            print(f"  restart. The Forge cannot operate without them.", flush=True)
            print("  " + "!" * 60, flush=True)
            print(flush=True)
            sys.exit(1)

        if not dst.exists():
            # Case 1: missing — copy silently
            dst.write_bytes(src.read_bytes())
            log(f"Installed {filename} → {dst}")
            continue

        if _sha256(src) == _sha256(dst):
            # Case 2: identical — nothing to do
            continue

        # Case 3: present but out of sync — warn and offer update
        print(flush=True)
        print("  " + "!" * 60, flush=True)
        print(f"  WARNING: {filename} in this repo is out of sync", flush=True)
        print(f"  with the version bundled with The Forge.", flush=True)
        print(f"", flush=True)
        print(f"  Repo:   {dst}", flush=True)
        print(f"  Forge:  {src}", flush=True)
        print(f"", flush=True)
        print(f"  Running with an outdated {filename} may cause", flush=True)
        print(f"  OpenCode sessions to behave incorrectly or fail.", flush=True)
        print("  " + "!" * 60, flush=True)
        print(flush=True)

        try:
            answer = input(
                f"  Update {filename} to the bundled version? [Y/n] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"

        if answer in ("", "y", "yes"):
            dst.write_bytes(src.read_bytes())
            log(f"Updated {filename} → {dst}")
        else:
            log_warn(
                f"User declined update of {filename} — continuing with existing version. "
                f"Minor version differences are usually harmless; review the changelog "
                f"if The Forge behaves unexpectedly."
            )
            all_synced = False

    return all_synced