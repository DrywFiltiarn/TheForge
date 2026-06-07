"""
forge_state.py — State persistence, task DAG loading, validation, and scheduling.
"""

import json
import sys
from pathlib import Path
from typing import Optional

from . import forge_config as cfg
from .forge_log import log, log_err, log_warn, _ts

# Populated by main() after --repo validation.
# Imported directly by callers that need the raw path.
from .forge_config import STATE_FILE

# ─── Default state ────────────────────────────────────────────────────────────

DEFAULT_STATE: dict = {
    "completed":               [],
    "in_progress":             None,
    "plan_approved":           False,
    "current_plan":            None,
    "failed":                  [],
    "needs_review":            [],
    "last_updated":            None,
    "plan_approval_message_id": None,
    "push_approval_message_id": None,
    "plan_report_message_id":   None,
    "impl_report_message_id":   None,
}


def load_state() -> dict:
    if cfg.STATE_FILE.exists():
        try:
            return json.loads(cfg.STATE_FILE.read_text())
        except Exception as e:
            log_err(f"Failed to load state: {e} — using default")
    return dict(DEFAULT_STATE)


def save_state(state: dict) -> None:
    state["last_updated"] = _ts()
    tmp = cfg.STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(cfg.STATE_FILE)


# ─── Task DAG ─────────────────────────────────────────────────────────────────

def load_tasks(project: Optional[str] = None, phase: Optional[int] = None) -> list[dict]:
    """
    Load task definitions from <repo>/.forge/tasks/tasks_phase<NNN>.json files.
    project=None loads from all registered projects.
    phase=None loads all phase files; phase=N loads phases 1..N only.
    Duplicate task IDs across any files are a fatal error.
    """
    import re as _re
    from .forge_repos import REPOS, resolve_project_tasks_dir

    pattern = _re.compile(r"^tasks_phase(\d{3})\.json$", _re.IGNORECASE)
    projects_to_load = [project] if project else sorted(REPOS.keys())

    merged:       list[dict]      = []
    seen_ids:     dict[str, Path] = {}
    files_loaded: list[str]       = []

    for proj in projects_to_load:
        try:
            tasks_dir = resolve_project_tasks_dir(proj)
        except KeyError:
            continue

        if not tasks_dir.is_dir():
            log_warn(f"Tasks directory not found for {proj!r}: {tasks_dir} — skipping")
            continue

        found: list[tuple[int, Path]] = []
        for p in tasks_dir.iterdir():
            m = pattern.match(p.name)
            if m:
                found.append((int(m.group(1)), p))
        found.sort(key=lambda x: x[0])

        candidates = [p for n, p in found if n <= phase] if phase is not None \
                     else [p for _, p in found]

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
            files_loaded.append(
                str(path.relative_to(cfg.FORGE_DIR.parent)
                    if path.is_relative_to(cfg.FORGE_DIR.parent)
                    else path)
            )

    if not merged:
        log_err(
            "No task files found. Each project must have .forge/tasks/tasks_phase001.json "
            "(and subsequent phase files) inside its repository root."
        )
        sys.exit(1)

    log(f"Loaded {len(merged)} tasks — schema OK")
    log(f"Loaded {len(merged)} tasks from {len(files_loaded)} file(s): "
        f"{', '.join(Path(f).name for f in files_loaded)}")
    return merged


def build_dag(tasks: list[dict]) -> dict[str, dict]:
    return {t["id"]: t for t in tasks}


def validate_task_schema(task: dict) -> list[str]:
    """
    Validate a task dict against the required schema.
    Returns a list of error strings (empty = valid).
    """
    from .forge_repos import REPOS
    errors = []
    if "id" not in task:
        errors.append("missing required field 'id'")
    if "description" not in task:
        errors.append("missing required field 'description'")
    if "phase" not in task:
        errors.append("missing required field 'phase'")

    if "repos" in task and "project" not in task:
        errors.append(
            "field 'repos' is no longer supported. "
            "Rename it to 'project' and set a single project name string."
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
            status = "▶ unblocked" if prereqs.issubset(completed) else "⏸  blocked"
        proj = task.get("project", "?")
        print(f"{tid:<12} {status:<14} {proj:<12} {task['description']}")
    print()
