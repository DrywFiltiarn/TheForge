"""
forge_runner.py — execute_task(): the full plan→approve→act→commit→push cycle
                  for a single atomic task.
"""

import time
import traceback
from pathlib import Path
from typing import Optional

from . import forge_config as cfg
from .forge_log import log, log_err, log_warn, _fmt_duration
from .forge_state import save_state
from .forge_repos import (
    ensure_repo_forge_dirs, write_current_task_file,
    resolve_project_path, resolve_project_branch,
    ensure_on_branch,
)
from .forge_git import (
    forge_commit, forge_push,
    collect_commit_info, validate_commit_messages, has_dirty_working_tree,
    has_unpushed_commits, revert_task_repo,
)
from .forge_discord import (
    DiscordClient, wait_for_approval,
    format_report_caption, format_plan_approval_request,
    format_push_approval_request, format_implementation_caption,
    _discord_escape,
)
from .forge_opencode import run_opencode
from .forge_prompts import (
    plan_report_path, implement_report_path,
    write_forge_plan_report, read_plan_report, read_implement_report,
    extract_plan_section, _is_thinking_trace,
    build_task_prompt, build_act_prompt,
)

def execute_task(
    task: dict,
    state: dict,
    dc: Optional[DiscordClient],
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
            f"⚙️ **Task `{tid}` STARTED** — {_discord_escape(task['description'])}\n"
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
            log(f"[{tid}] [DRY RUN] Would run OpenCode PLAN mode ({cfg.MODEL_PLANNING})")
            plan_text = f"[DRY RUN] Plan for {tid}"
        else:
            success, output = run_opencode(
                prompt, plan_mode=True, cwd=repo_path,
                task_id=tid, dc=dc,
                approvals_channel_id=approvals_channel_id,
                attempt_number=plan_attempt,
                model_id=cfg.MODEL_PLANNING,
            )
            if not success:
                msg = f"❌ `{tid}` OpenCode PLAN failed after {cfg.OPENCODE_RETRIES} attempts. Stopping."
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
                plan_dur_cap = _fmt_duration(t_plan_end - t_plan_start) if t_plan_end > 0 else ""
                caption      = format_report_caption(task, "PLAN", dur=plan_dur_cap)
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
            plan_dur_str    = _fmt_duration(t_plan_end - t_plan_start) if t_plan_end > 0 else ""
            approval_text   = format_plan_approval_request(task, plan_attempt, feedback, plan_dur=plan_dur_str)
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
    log(f"[{tid}] ⚙️  Act phase — model: {cfg.MODEL_CODING}")

    # Write CURRENT_TASK.md so OpenCode's §1 identity check passes
    if not dry_run:
        write_current_task_file(task, step="IMPLEMENT", status="IN_PROGRESS")

    t_act_start = time.monotonic()
    if dry_run:
        log(f"[{tid}] [DRY RUN] Would run OpenCode ACT mode ({cfg.MODEL_CODING})")
        act_success = True
    else:
        act_prompt  = build_act_prompt(task, state["current_plan"])
        act_success, _ = run_opencode(
            act_prompt, plan_mode=False, cwd=repo_path,
            task_id=tid, dc=dc,
            approvals_channel_id=approvals_channel_id,
            model_id=cfg.MODEL_CODING,
        )
    t_act_end = time.monotonic()  # push approval wait NOT included

    if not act_success:
        msg = f"❌ `{tid}` OpenCode ACT failed after {cfg.OPENCODE_RETRIES} attempts. Task marked failed."
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
        commit_hash = forge_commit(task)
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
            act_dur_str  = _fmt_duration(t_act_end - t_act_start) if t_act_end > 0 else ""
            caption     = format_implementation_caption(task, commit_info, act_dur=act_dur_str)
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
        act_dur_str     = _fmt_duration(t_act_end - t_act_start) if t_act_end > 0 else ""
        approval_text   = format_push_approval_request(task, commit_info, act_dur=act_dur_str)
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
        push_ok = forge_push(task)
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

    for log_file in (cfg.OPENCODE_LOG_FILE, cfg.CONTEXT_LOG_FILE):
        try:
            log_file.write_text("")
        except Exception as e:
            log_warn(f"[{tid}] Could not purge {log_file.name}: {e}")

    if dc and reports_channel_id:
        plan_dur = _fmt_duration(t_plan_end - t_plan_start) if t_plan_end > 0 else "—"
        act_dur  = _fmt_duration(t_act_end  - t_act_start)  if t_act_end  > 0 else "—"
        dc.send_message(
            reports_channel_id,
            f"✅ **Task `{tid}` COMPLETE** — {_discord_escape(task['description'])}\n"
            f"⏱ Planning: `{plan_dur}` · Implementation: `{act_dur}` _(approval wait excluded)_"
        )

    log(f"[{tid}] ✅ Task complete")
    return True
