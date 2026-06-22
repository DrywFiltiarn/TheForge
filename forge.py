#!/usr/bin/env python3
"""
forge.py — SindriStudio Autonomous Development Orchestrator

Entry point. All logic lives in the forge/ package.

Directory layout:
    <this directory>/
      forge.py          — this entry point
      repos.json        — repository registry
      agents/           — OpenCode agent files (forge-plan.md, forge-act.md)
      docs/             — bundled Forge documents (synced to each repo's docs/ on startup)
                            FORGE_AGENT_RULES.md, FORGE_TASK_AUTHORING_SPEC.md
      logs/             — all runtime log output
      forge/            — implementation modules
      state.json        — runtime state (scoped per repo, set by main())

Usage:
    python forge.py --repo anvilml                         # run all phases
    python forge.py --repo anvilml --task P1-A1            # run ONE task
    python forge.py --repo anvilml --phase 2               # phases 1+2 only
    python forge.py --repo anvilml --phase 2 --task P2-B1  # specific task
    python forge.py --repo anvilml --dry-run               # no execution
    python forge.py --repo anvilml --list                  # show DAG status
    python forge.py --repo anvilml --reset-task P1-A3      # reset task
    python forge.py --repo anvilml --reset-task-git P1-A3  # reset + git
"""

import argparse
import sys
import traceback
from pathlib import Path

# Ensure the forge/ package is importable regardless of working directory
sys.path.insert(0, str(Path(__file__).parent))

# ── venv guard — must precede all package imports ────────────────────────────
# Prevents accidental direct execution outside The Forge's managed .venv.
# Always invoke via forge.sh or forge_monitor.sh.
import sys as _sys, os as _os
_expected_venv = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".venv")
_in_venv       = _sys.prefix != _sys.base_prefix
_in_forge_venv = _os.path.abspath(_sys.prefix).startswith(_os.path.abspath(_expected_venv))
if not (_in_venv and _in_forge_venv):
    print("", flush=True)
    print("  Error: forge.py must be run inside The Forge virtual environment.", flush=True)
    print("  Use the provided shell scripts instead:", flush=True)
    print("", flush=True)
    print("    ./forge.sh --repo <project>          # run The Forge", flush=True)
    print("    ./forge_monitor.sh --repo <project>  # run with tmux monitor", flush=True)
    print("", flush=True)
    print("  If .venv is missing, run: bash forge_setup.sh", flush=True)
    print("", flush=True)
    _sys.exit(1)
del _sys, _os, _expected_venv, _in_venv, _in_forge_venv
# ─────────────────────────────────────────────────────────────────────────────


import forge.forge_config as cfg
from forge.forge_log import log, log_err, log_warn
from forge.forge_repos import load_repos, REPOS, resolve_project_path, ensure_on_branch, ensure_forge_docs
from forge.forge_state import (
    load_state, save_state, load_tasks,
    validate_task_schema, validate_task_graph, find_next_task, print_dag_status,
    is_phase_closing_task,
    DEFAULT_STATE,
)
from forge.forge_git import revert_task_repo
from forge.forge_discord import get_discord
from forge.forge_opencode import ensure_opencode_agents
from forge.forge_runner import execute_task


def _ensure_runtime_dirs() -> None:
    """Create logs/ and agents/ directories beside forge.py if absent."""
    cfg.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    cfg.AGENTS_DIR.mkdir(parents=True, exist_ok=True)


def _validate_tasks_or_exit(tasks: list[dict]) -> None:
    """
    Run both per-task schema validation and whole-graph validation
    (prereq existence, cycles, defers_to existence + downstream
    positioning). Exits the process on any failure. Called at startup
    and again on every hot-reload of the task list, so an edit made to
    tasks_phase<NNN>.json between iterations cannot silently bypass
    validation. See docs/FORGE_TASK_AUTHORING_SPEC.md §5 and §12a.
    """
    schema_errors = []
    for task in tasks:
        errs = validate_task_schema(task)
        if errs:
            for e in errs:
                schema_errors.append(f"  {task.get('id', '?')}: {e}")
    if schema_errors:
        log_err("Task schema validation failed:")
        for e in schema_errors:
            log_err(e)
        sys.exit(1)

    graph_errors = validate_task_graph(tasks)
    if graph_errors:
        log_err("Task graph validation failed:")
        for e in graph_errors:
            log_err(f"  {e}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="forge",
        description="The Forge — Autonomous Development Orchestrator",
    )
    parser.add_argument(
        "--repo", metavar="PROJECT",
        help="Project name as registered in repos.json (e.g. anvilml)",
    )
    parser.add_argument(
        "--task", metavar="TASK_ID",
        help="Run exactly this task (full cycle, all gates) then exit.",
    )
    parser.add_argument(
        "--phase", metavar="N", type=int,
        help="Load tasks up to and including this phase number.",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="Print DAG status and exit without running anything.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would run without executing OpenCode or waiting for approvals.",
    )
    parser.add_argument(
        "--reset-task", metavar="TASK_ID",
        help="Reset a task to unstarted in state.json (no git changes).",
    )
    parser.add_argument(
        "--reset-task-git", metavar="TASK_ID",
        help="Reset a task to unstarted AND hard-reset the repo to origin/<branch>.",
    )
    args = parser.parse_args()

    # ── Bootstrap: directories, repos, logging ────────────────────────────────
    _ensure_runtime_dirs()

    REPOS.update(load_repos())
    if not REPOS:
        print("[FATAL] No repos loaded — check repos.json", flush=True)
        sys.exit(1)

    # Resolve the active project
    if args.repo:
        if args.repo not in REPOS:
            registered = ", ".join(sorted(REPOS.keys()))
            print(f"[FATAL] --repo {args.repo!r} not in repos.json. Registered: {registered}",
                  flush=True)
            sys.exit(1)
        active_project = args.repo
    else:
        if len(REPOS) == 1:
            active_project = next(iter(REPOS))
        else:
            registered = ", ".join(sorted(REPOS.keys()))
            print(f"[FATAL] Multiple repos registered — specify --repo. Options: {registered}",
                  flush=True)
            sys.exit(1)

    repo_path = resolve_project_path(active_project)
    cfg.STATE_FILE = repo_path / ".forge" / "state" / "state.json"
    cfg.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    # ── Sync Forge documentation to repo docs/ ──────────────────────────────
    ensure_forge_docs(repo_path)  # hard-stops only if bundled docs are missing

    log("=" * 60)
    log(f"The Forge — project: {active_project}")
    log(f"Repo: {repo_path}")
    log(f"State: {cfg.STATE_FILE}")
    log(f"Logs:  {cfg.LOGS_DIR}")

    if not cfg.DISCORD_BOT_TOKEN:
        log_warn("FORGE_DISCORD_TOKEN not set — running without Discord notifications")
    if not cfg.DISCORD_GUILD_ID:
        log_warn("FORGE_DISCORD_GUILD_ID not set — Discord channel lookup disabled")

    # ── Sync OpenCode agent files ──────────────────────────────────────────────
    ensure_opencode_agents()

    # ── Discord setup ─────────────────────────────────────────────────────────
    dc = get_discord()
    if dc:
        log(f"Discord reports channel (broadcast):  {cfg.DISCORD_REPORTS_CHANNEL_ID}")
        log(f"Discord approvals channel (polled):   {cfg.DISCORD_APPROVALS_CHANNEL_ID}")
        log(f"Discord owner gate:                   {cfg.FORGE_OWNER_ID}")
    reports_channel_id   = cfg.DISCORD_REPORTS_CHANNEL_ID   if dc else None
    approvals_channel_id = cfg.DISCORD_APPROVALS_CHANNEL_ID if dc else None

    # ── Load tasks and state ───────────────────────────────────────────────────
    tasks = load_tasks(project=active_project, phase=args.phase)

    # Validate task schemas and whole-graph properties (prereqs exist, no
    # cycles, defers_to targets exist and are verified downstream).
    _validate_tasks_or_exit(tasks)
    log(f"Loaded {len(tasks)} tasks — schema and graph OK")

    state = load_state()

    # ── --list ────────────────────────────────────────────────────────────────
    if args.list:
        print_dag_status(tasks, state)
        return

    # ── --reset-task ──────────────────────────────────────────────────────────
    if args.reset_task:
        tid = args.reset_task
        for lst in ("completed", "failed", "needs_review"):
            if tid in state.get(lst, []):
                state[lst].remove(tid)
        if state.get("in_progress") == tid:
            state["in_progress"] = None
            state["plan_approved"] = False
            state["current_plan"]  = None
        save_state(state)
        log(f"Reset task {tid!r} to unstarted in state.json")
        return

    # ── --reset-task-git ──────────────────────────────────────────────────────
    if args.reset_task_git:
        tid = args.reset_task_git
        task_obj = next((t for t in tasks if t["id"] == tid), None)
        if task_obj is None:
            log_err(f"Task {tid!r} not found in loaded tasks")
            sys.exit(1)
        if not revert_task_repo(task_obj):
            log_err(f"Git reset failed for task {tid!r}")
            sys.exit(1)
        for lst in ("completed", "failed", "needs_review"):
            if tid in state.get(lst, []):
                state[lst].remove(tid)
        if state.get("in_progress") == tid:
            state["in_progress"] = None
            state["plan_approved"] = False
            state["current_plan"]  = None
        save_state(state)
        log(f"Reset task {tid!r} and reverted repo to origin")
        return

    # ── Main execution loop ───────────────────────────────────────────────────
    target_task_id = args.task

    while True:
        # Hot-reload tasks on every iteration (allows edits between runs).
        # Re-validate every time — an edit made between iterations must not
        # silently bypass the schema/graph checks run at startup.
        tasks = load_tasks(project=active_project, phase=args.phase)
        _validate_tasks_or_exit(tasks)
        state = load_state()

        if target_task_id:
            task = next((t for t in tasks if t["id"] == target_task_id), None)
            if task is None:
                log_err(f"Task {target_task_id!r} not found")
                sys.exit(1)
        else:
            task = find_next_task(tasks, state)

        if task is None:
            completed = len(state["completed"])
            total     = len(tasks)
            if completed >= total:
                log(f"🎉 All {total} tasks complete!")
            else:
                remaining = total - completed - len(state["failed"]) - len(state["needs_review"])
                log(f"No unblocked tasks available. "
                    f"Completed: {completed}/{total}. "
                    f"Failed: {len(state['failed'])}. "
                    f"Needs review: {len(state['needs_review'])}. "
                    f"Remaining: {remaining}.")
            break

        if not ensure_on_branch(active_project):
            log_err(f"Cannot ensure correct branch for {active_project} — stopping")
            sys.exit(1)

        try:
            success = execute_task(
                task, state, dc,
                reports_channel_id=reports_channel_id,
                approvals_channel_id=approvals_channel_id,
                dry_run=args.dry_run,
                is_phase_closing=is_phase_closing_task(task, tasks),
            )
        except KeyboardInterrupt:
            log_warn("Interrupted by user — state saved, safe to resume")
            break
        except Exception as e:
            log_err(f"Unhandled exception in execute_task: {e}")
            traceback.print_exc()
            break

        if not success:
            log_err(f"Task {task['id']} failed — stopping")
            break

        if target_task_id:
            log(f"Task {target_task_id!r} complete — exiting (--task mode)")
            break


if __name__ == "__main__":
    main()