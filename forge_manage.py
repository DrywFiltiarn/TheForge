#!/usr/bin/env python3
"""
forge_manage.py — The Forge management CLI

Provides status inspection and state management for The Forge task queue
without running the full orchestrator. Safe to use while forge.py is running.

Usage:
    python forge_manage.py --repo anvilml              # show full status
    python forge_manage.py --repo anvilml --unblock    # show only unblocked tasks
    python forge_manage.py --repo anvilml --fail P1-A3      # mark task failed
    python forge_manage.py --repo anvilml --reset P1-A3     # reset to unstarted
    python forge_manage.py --repo anvilml --complete P1-A3  # manually mark complete
    python forge_manage.py --repo anvilml --review P1-A3    # mark needs-review
    python forge_manage.py --repo anvilml --clear-failed    # reset all failed tasks
    python forge_manage.py --repo anvilml --clear-review    # reset all needs-review tasks

If only one repo is registered in repos.json, --repo may be omitted.
"""

import argparse
import json
import sys
from pathlib import Path

# Ensure the forge/ package is importable regardless of working directory
sys.path.insert(0, str(Path(__file__).parent))

# ── venv guard — must precede all package imports ────────────────────────────
# Prevents accidental direct execution outside The Forge's managed .venv.
# Always invoke via forge_manage.sh.
import sys as _sys, os as _os
_expected_venv = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".venv")
_in_venv       = _sys.prefix != _sys.base_prefix
_in_forge_venv = _os.path.abspath(_sys.prefix).startswith(_os.path.abspath(_expected_venv))
if not (_in_venv and _in_forge_venv):
    print("", flush=True)
    print("  Error: forge_manage.py must be run inside The Forge virtual environment.", flush=True)
    print("  Use the provided shell script instead:", flush=True)
    print("", flush=True)
    print("    ./forge_manage.sh --repo <project>", flush=True)
    print("", flush=True)
    print("  If .venv is missing, run: bash forge_setup.sh", flush=True)
    print("", flush=True)
    _sys.exit(1)
del _sys, _os, _expected_venv, _in_venv, _in_forge_venv
# ─────────────────────────────────────────────────────────────────────────────


import forge.forge_config as cfg
from forge.forge_repos import load_repos, REPOS, resolve_project_path
from forge.forge_log import _ts
from forge.forge_state import validate_task_graph

# ─── ANSI colours ─────────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"

# ─── State and task I/O ───────────────────────────────────────────────────────

def _load_state() -> dict:
    if cfg.STATE_FILE.exists():
        try:
            return json.loads(cfg.STATE_FILE.read_text())
        except Exception as e:
            print(f"{RED}[ERROR] Failed to load state: {e}{RESET}", flush=True)
            sys.exit(1)
    return {
        "completed": [], "in_progress": None, "failed": [],
        "needs_review": [], "plan_approved": False, "current_plan": None,
    }


def _save_state(state: dict) -> None:
    state["last_updated"] = _ts()
    tmp = cfg.STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(cfg.STATE_FILE)


def _load_tasks(project: str, phase: int = None) -> list[dict]:
    """Load all tasks for the project from .forge/tasks/tasks_phase<NNN>.json files."""
    import re
    pattern = re.compile(r"^tasks_phase(\d{3})\.json$", re.IGNORECASE)
    tasks_dir = resolve_project_path(project) / ".forge" / "tasks"

    if not tasks_dir.is_dir():
        print(f"{YELLOW}[WARN] No tasks directory found: {tasks_dir}{RESET}", flush=True)
        return []

    found = sorted(
        (int(m.group(1)), p)
        for p in tasks_dir.iterdir()
        if (m := pattern.match(p.name))
    )
    if phase is not None:
        found = [(n, p) for n, p in found if n <= phase]

    tasks = []
    seen  = set()
    for _, path in found:
        chunk = json.loads(path.read_text())
        for task in chunk:
            tid = task.get("id", "<missing>")
            if tid not in seen:
                seen.add(tid)
                tasks.append(task)
    return tasks

# ─── Display ──────────────────────────────────────────────────────────────────

def _status_line(tid: str, state: dict) -> tuple[str, str]:
    """Return (colour_code, label) for a task ID given current state."""
    completed    = set(state.get("completed", []))
    failed       = set(state.get("failed", []))
    needs_review = set(state.get("needs_review", []))
    in_progress  = state.get("in_progress")

    if tid in completed:
        return GREEN, "✅ complete"
    if tid == in_progress:
        sub = "act" if state.get("plan_approved", False) else "plan"
        return CYAN, f"⚙️  running ({sub})"
    if tid in failed:
        return RED, "❌ failed"
    if tid in needs_review:
        return YELLOW, "🔍 needs-review"
    return "", ""   # unresolved — caller handles blocked/unblocked


def print_status(tasks: list[dict], state: dict) -> None:
    completed    = set(state.get("completed", []))
    failed       = set(state.get("failed", []))
    needs_review = set(state.get("needs_review", []))
    in_progress  = state.get("in_progress")

    total     = len(tasks)
    n_done    = len(completed)
    n_failed  = len(failed)
    n_review  = len(needs_review)
    n_running = 1 if in_progress else 0
    n_pending = total - n_done - n_failed - n_review - n_running

    print(f"\n{BOLD}The Forge — Task Status{RESET}")
    print(f"{DIM}{'─' * 120}{RESET}")
    print(
        f"  Total: {BOLD}{total}{RESET}  "
        f"{GREEN}Complete: {n_done}{RESET}  "
        f"{CYAN}Running: {n_running}{RESET}  "
        f"{YELLOW}Pending: {n_pending}{RESET}  "
        f"{RED}Failed: {n_failed}{RESET}  "
        f"{YELLOW}Review: {n_review}{RESET}"
    )
    if state.get("last_updated"):
        print(f"  Last updated: {DIM}{state['last_updated']}{RESET}")

    print(f"\n{BOLD}{'  ID':<14} {'Status':<32} {'Project':<14} Description{RESET}")
    print(f"{DIM}{'─' * 120}{RESET}")

    current_phase = None
    for task in tasks:
        tid   = task["id"]
        phase = task.get("phase", "?")
        proj  = task.get("project", "?")
        desc  = task["description"]
        # if len(desc) > 44:
        #    desc = desc[:41] + "..."

        if phase != current_phase:
            current_phase = phase
            print(f"\n{DIM}  ── Phase {phase} ──{RESET}")

        colour, label = _status_line(tid, state)
        if not label:
            prereqs = set(task.get("prereqs", []))
            missing = prereqs - completed
            if missing:
                colour = DIM
                label  = f"⏸  blocked ({', '.join(sorted(missing))})"
            else:
                colour = YELLOW
                label  = f"▶  unblocked"

        padded_label = f"{colour}{label}{RESET}"
        # Account for ANSI escape sequences in column width calculation
        visible_len  = len(label)
        padding      = max(0, 20 - visible_len)
        print(f"  {tid:<12} {padded_label:<40} {proj:<14} {desc}")

        defers = task.get("defers_to", [])
        if defers:
            print(f"  {DIM}{'':<12} {'':<40} {'':<14} ⤷ defers to: {', '.join(defers)}{RESET}")

    print()


def show_unblocked(tasks: list[dict], state: dict) -> None:
    completed    = set(state.get("completed", []))
    failed       = set(state.get("failed", []))
    needs_review = set(state.get("needs_review", []))
    in_progress  = state.get("in_progress")

    print(f"\n{BOLD}Unblocked tasks (ready to run):{RESET}")
    found = False
    for task in tasks:
        tid = task["id"]
        if tid in completed or tid in failed or tid in needs_review:
            continue
        if tid == in_progress:
            print(f"  {CYAN}⚙️  {tid}{RESET} — {task['description']} {DIM}(running){RESET}")
            found = True
            continue
        prereqs = set(task.get("prereqs", []))
        if prereqs.issubset(completed):
            proj = task.get("project", "?")
            print(f"  {GREEN}{tid}{RESET} {DIM}[{proj}]{RESET} — {task['description']}")
            found = True

    if not found:
        print(f"  {DIM}None — all remaining tasks are blocked, failed, or in review.{RESET}")

    if failed or needs_review:
        print(f"\n{BOLD}Blocking entries:{RESET}")
        for tid in sorted(failed):
            print(f"  {RED}❌ {tid}{RESET} (failed) — unblock with --reset or --complete")
        for tid in sorted(needs_review):
            print(f"  {YELLOW}🔍 {tid}{RESET} (needs-review) — unblock with --reset or --complete")
    print()

# ─── State mutations ──────────────────────────────────────────────────────────

def _remove_from_lists(state: dict, tid: str, *lists: str) -> None:
    for lst in lists:
        if tid in state.get(lst, []):
            state[lst].remove(tid)


def cmd_fail(state: dict, tid: str) -> None:
    _remove_from_lists(state, tid, "completed", "needs_review")
    if tid not in state.setdefault("failed", []):
        state["failed"].append(tid)
    if state.get("in_progress") == tid:
        state["in_progress"] = None
    _save_state(state)
    print(f"{RED}Marked {tid} as failed.{RESET}")


def cmd_reset(state: dict, tid: str) -> None:
    _remove_from_lists(state, tid, "completed", "failed", "needs_review")
    if state.get("in_progress") == tid:
        state["in_progress"]  = None
        state["plan_approved"] = False
        state["current_plan"]  = None
    _save_state(state)
    print(f"{YELLOW}Reset {tid} to unstarted.{RESET}")


def cmd_complete(state: dict, tid: str) -> None:
    _remove_from_lists(state, tid, "failed", "needs_review")
    if tid not in state.setdefault("completed", []):
        state["completed"].append(tid)
    if state.get("in_progress") == tid:
        state["in_progress"] = None
    _save_state(state)
    print(f"{GREEN}Marked {tid} as complete.{RESET}")


def cmd_review(state: dict, tid: str) -> None:
    _remove_from_lists(state, tid, "completed", "failed")
    if tid not in state.setdefault("needs_review", []):
        state["needs_review"].append(tid)
    if state.get("in_progress") == tid:
        state["in_progress"] = None
    _save_state(state)
    print(f"{CYAN}Marked {tid} as needs-review.{RESET}")


def cmd_clear_failed(state: dict) -> None:
    n = len(state.get("failed", []))
    state["failed"] = []
    _save_state(state)
    print(f"{YELLOW}Cleared {n} failed task(s) — all reset to unstarted.{RESET}")


def cmd_clear_review(state: dict) -> None:
    n = len(state.get("needs_review", []))
    state["needs_review"] = []
    _save_state(state)
    print(f"{YELLOW}Cleared {n} needs-review task(s) — all reset to unstarted.{RESET}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="forge_manage",
        description="The Forge — management CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python forge_manage.py --repo anvilml\n"
            "  python forge_manage.py --repo anvilml --unblock\n"
            "  python forge_manage.py --repo anvilml --complete P4-A3\n"
            "  python forge_manage.py --repo anvilml --reset P4-A3\n"
            "  python forge_manage.py --repo anvilml --fail P4-A3\n"
            "  python forge_manage.py --repo anvilml --review P4-A3\n"
            "  python forge_manage.py --repo anvilml --clear-failed\n"
            "  python forge_manage.py --repo anvilml --clear-review\n"
        ),
    )
    parser.add_argument(
        "--repo", metavar="PROJECT",
        help="Project name as registered in repos.json. May be omitted if only one repo is registered.",
    )
    parser.add_argument("--phase", metavar="N", type=int,
                        help="Limit task view to phases 1..N.")
    parser.add_argument("--unblock", action="store_true",
                        help="Show only tasks that are ready to run.")
    parser.add_argument("--fail",     metavar="TASK_ID",
                        help="Mark a task as failed.")
    parser.add_argument("--reset",    metavar="TASK_ID",
                        help="Reset a task to unstarted (clears plan approval and current plan).")
    parser.add_argument("--complete", metavar="TASK_ID",
                        help="Manually mark a task complete (use after reviewing a push-rejected task).")
    parser.add_argument("--review",   metavar="TASK_ID",
                        help="Mark a task as needs-review (blocks dependents until resolved).")
    parser.add_argument("--clear-failed", action="store_true",
                        help="Reset all failed tasks to unstarted in one operation.")
    parser.add_argument("--clear-review", action="store_true",
                        help="Reset all needs-review tasks to unstarted in one operation.")
    args = parser.parse_args()

    # ── Resolve project ───────────────────────────────────────────────────────
    REPOS.update(load_repos())

    if args.repo:
        if args.repo not in REPOS:
            registered = ", ".join(sorted(REPOS.keys()))
            print(f"{RED}[ERROR] --repo {args.repo!r} not in repos.json. "
                  f"Registered: {registered}{RESET}", flush=True)
            sys.exit(1)
        active_project = args.repo
    else:
        if len(REPOS) == 1:
            active_project = next(iter(REPOS))
        else:
            registered = ", ".join(sorted(REPOS.keys()))
            print(f"{RED}[ERROR] Multiple repos registered — specify --repo. "
                  f"Options: {registered}{RESET}", flush=True)
            sys.exit(1)

    repo_path      = resolve_project_path(active_project)
    cfg.STATE_FILE = repo_path / ".forge" / "state" / "state.json"

    tasks = _load_tasks(active_project, phase=args.phase)
    state = _load_state()

    # Surface graph problems (bad prereqs/cycles/defers_to) as a warning —
    # this tool is also used to inspect and recover an already-broken state,
    # so it must not refuse to run; forge.py is the actual startup gate.
    graph_errors = validate_task_graph(tasks)
    if graph_errors:
        print(f"\n{RED}⚠ Task graph has {len(graph_errors)} problem(s) — "
              f"forge.py will refuse to start until these are fixed:{RESET}")
        for e in graph_errors:
            print(f"  {RED}• {e}{RESET}")
        print()

    # ── Mutations (mutually exclusive) ────────────────────────────────────────
    if args.fail:
        _assert_known(args.fail, tasks)
        cmd_fail(state, args.fail)

    elif args.reset:
        _assert_known(args.reset, tasks)
        cmd_reset(state, args.reset)

    elif args.complete:
        _assert_known(args.complete, tasks)
        cmd_complete(state, args.complete)

    elif args.review:
        _assert_known(args.review, tasks)
        cmd_review(state, args.review)

    elif args.clear_failed:
        cmd_clear_failed(state)

    elif args.clear_review:
        cmd_clear_review(state)

    # ── Display ───────────────────────────────────────────────────────────────
    # Reload state after any mutation so display reflects the change
    state = _load_state()

    if args.unblock:
        show_unblocked(tasks, state)
    else:
        print_status(tasks, state)


def _assert_known(tid: str, tasks: list[dict]) -> None:
    known = {t["id"] for t in tasks}
    if tid not in known:
        print(f"{RED}[ERROR] Task {tid!r} not found in loaded tasks.{RESET}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()