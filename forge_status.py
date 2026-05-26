#!/usr/bin/env python3
"""
forge_status.py — Forge status and management CLI

Provides interactive management of the Forge task queue without
running the full orchestrator. Safe to use while forge.py is running.

Usage:
    python forge/forge_status.py             # show full status
    python forge/forge_status.py --fail P1-A3  # mark task as failed
    python forge/forge_status.py --reset P1-A3 # reset task to unstarted
    python forge/forge_status.py --complete P1-A3 # manually mark complete
    python forge/forge_status.py --unblock    # list what's currently blocking
"""

import argparse
import json
from pathlib import Path

FORGE_DIR = Path(__file__).parent.resolve()
STATE_FILE = FORGE_DIR / "state.json"
TASKS_FILE = FORGE_DIR / "tasks.json"

RESET  = "\033[0m"
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
DIM    = "\033[2m"
BOLD   = "\033[1m"

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"completed": [], "in_progress": None, "failed": [], "needs_review": []}

def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)

def load_tasks() -> list[dict]:
    return json.loads(TASKS_FILE.read_text())

def print_status(tasks: list[dict], state: dict) -> None:
    completed   = set(state.get("completed", []))
    failed      = set(state.get("failed", []))
    needs_review = set(state.get("needs_review", []))
    in_progress = state.get("in_progress")

    total     = len(tasks)
    n_done    = len(completed)
    n_failed  = len(failed)
    n_review  = len(needs_review)
    n_pending = total - n_done - n_failed - n_review

    print(f"\n{BOLD}SindriStudio Forge — Task Status{RESET}")
    print(f"{DIM}{'─'*60}{RESET}")
    print(f"  Total: {total}  "
          f"{GREEN}Complete: {n_done}{RESET}  "
          f"{YELLOW}Pending: {n_pending}{RESET}  "
          f"{RED}Failed: {n_failed}{RESET}  "
          f"{CYAN}Review: {n_review}{RESET}")

    if state.get("last_updated"):
        print(f"  Last updated: {state['last_updated']}")

    print(f"\n{BOLD}{'ID':<12} {'Status':<16} Description{RESET}")
    print(f"{DIM}{'─'*80}{RESET}")

    phase = None
    for task in tasks:
        tid = task["id"]
        p = task.get("phase", "?")
        if p != phase:
            phase = p
            print(f"\n{DIM}  ── Phase {phase} ──{RESET}")

        if tid in completed:
            status = f"{GREEN}✅ complete{RESET}"
        elif tid == in_progress:
            step = state.get("plan_approved", False)
            sub = "act" if step else "plan"
            status = f"{CYAN}⚙️  running ({sub}){RESET}"
        elif tid in failed:
            status = f"{RED}❌ failed{RESET}"
        elif tid in needs_review:
            status = f"{YELLOW}🔍 needs review{RESET}"
        else:
            prereqs = set(task.get("prereqs", []))
            if prereqs.issubset(completed):
                status = f"{YELLOW}⬜ unblocked{RESET}"
            else:
                missing = prereqs - completed
                status = f"{DIM}⏸  blocked ({','.join(sorted(missing))}){RESET}"

        desc = task["description"]
        if len(desc) > 48:
            desc = desc[:45] + "..."
        print(f"  {tid:<12} {status:<25} {desc}")

    print()

def show_unblocked(tasks: list[dict], state: dict) -> None:
    completed = set(state.get("completed", []))
    failed    = set(state.get("failed", []))
    needs_review = set(state.get("needs_review", []))
    in_progress = state.get("in_progress")

    print(f"\n{BOLD}Unblocked tasks (ready to run):{RESET}")
    found = False
    for task in tasks:
        tid = task["id"]
        if tid in completed or tid in failed or tid in needs_review:
            continue
        if tid == in_progress:
            continue
        prereqs = set(task.get("prereqs", []))
        if prereqs.issubset(completed):
            print(f"  {GREEN}{tid}{RESET} — {task['description']}")
            found = True
    if not found:
        print(f"  {DIM}None (check failed/needs_review tasks){RESET}")

    if failed or needs_review:
        print(f"\n{BOLD}Blocked by:{RESET}")
        for tid in sorted(failed):
            print(f"  {RED}❌ {tid}{RESET} (failed) — dependent tasks are blocked")
        for tid in sorted(needs_review):
            print(f"  {YELLOW}🔍 {tid}{RESET} (needs review) — dependent tasks are blocked")
    print()

def main() -> None:
    parser = argparse.ArgumentParser(description="Forge status and management")
    parser.add_argument("--fail",     metavar="TASK_ID", help="Mark task as failed")
    parser.add_argument("--reset",    metavar="TASK_ID", help="Reset task to unstarted")
    parser.add_argument("--complete", metavar="TASK_ID", help="Manually mark task complete")
    parser.add_argument("--review",   metavar="TASK_ID", help="Mark task as needs-review")
    parser.add_argument("--unblock",  action="store_true", help="Show unblocked tasks")
    args = parser.parse_args()

    tasks = load_tasks()
    state = load_state()

    if args.fail:
        tid = args.fail
        for lst in ["completed", "needs_review"]:
            if tid in state.get(lst, []):
                state[lst].remove(tid)
        if tid not in state.get("failed", []):
            state.setdefault("failed", []).append(tid)
        if state.get("in_progress") == tid:
            state["in_progress"] = None
        save_state(state)
        print(f"{RED}Marked {tid} as failed{RESET}")

    elif args.reset:
        tid = args.reset
        for lst in ["completed", "failed", "needs_review"]:
            if tid in state.get(lst, []):
                state[lst].remove(tid)
        if state.get("in_progress") == tid:
            state["in_progress"] = None
            state["plan_approved"] = False
            state["current_plan"] = None
        save_state(state)
        print(f"{YELLOW}Reset {tid} to unstarted{RESET}")

    elif args.complete:
        tid = args.complete
        for lst in ["failed", "needs_review"]:
            if tid in state.get(lst, []):
                state[lst].remove(tid)
        if tid not in state.get("completed", []):
            state.setdefault("completed", []).append(tid)
        if state.get("in_progress") == tid:
            state["in_progress"] = None
        save_state(state)
        print(f"{GREEN}Marked {tid} as complete{RESET}")

    elif args.review:
        tid = args.review
        for lst in ["completed", "failed"]:
            if tid in state.get(lst, []):
                state[lst].remove(tid)
        if tid not in state.get("needs_review", []):
            state.setdefault("needs_review", []).append(tid)
        if state.get("in_progress") == tid:
            state["in_progress"] = None
        save_state(state)
        print(f"{CYAN}Marked {tid} as needs-review{RESET}")

    elif args.unblock:
        show_unblocked(tasks, state)
        return

    # Always show status
    print_status(tasks, state)

if __name__ == "__main__":
    main()
