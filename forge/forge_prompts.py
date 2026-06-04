"""
forge_prompts.py — Task prompt builders and disk report file helpers.
"""

import re
from pathlib import Path
from typing import Optional

from .forge_log import log, log_warn
from .forge_repos import repo_reports_dir


# ─── Report file paths ────────────────────────────────────────────────────────

def plan_report_path(task: dict) -> Path:
    return repo_reports_dir(task["project"]) / f"{task['id']}_plan.md"

def implement_report_path(task: dict) -> Path:
    return repo_reports_dir(task["project"]) / f"{task['id']}_implement.md"


# ─── Report file I/O ──────────────────────────────────────────────────────────

def write_forge_plan_report(task: dict, plan_text: str, attempt: int) -> Path:
    """
    Write the plan report to disk (used as a fallback when OpenCode did not
    write it directly). Never overwrites an existing report.
    """
    path = plan_report_path(task)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        log(f"[{task['id']}] Plan report already exists (written by OpenCode) — not overwriting")
        return path

    path.write_text(plan_text, encoding="utf-8")
    log(f"[{task['id']}] Wrote plan report → {path}")
    return path


def read_plan_report(task: dict) -> str:
    path = plan_report_path(task)
    return path.read_text(encoding="utf-8") if path.exists() else ""

def read_implement_report(task: dict) -> str:
    path = implement_report_path(task)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def extract_plan_section(report_text: str, task_id: str) -> str:
    """
    Extract the Approach section from a plan report.
    Falls back to the full text if the section is not found.
    """
    match = re.search(
        r"^## Approach\n(.*?)(?=^##|\Z)",
        report_text, re.DOTALL | re.MULTILINE
    )
    if match:
        return match.group(1).strip()
    # Fallback: return the full report text
    return report_text


def _is_thinking_trace(report_text: str) -> bool:
    """
    Return True if the plan report looks like a raw thinking trace rather
    than a structured plan report.  Used to detect the failure mode where
    the model emits its reasoning instead of the report.

    Signals:
    - Does not start with '# Plan Report:'
    - Contains thinking-trace markers: <think>, </think>, <|thinking|>
    - Is unusually long for a plan and lacks ## section headings
    """
    stripped = report_text.strip()
    if stripped.startswith("# Plan Report:"):
        return False
    thinking_markers = ["<think>", "</think>", "<|thinking|>", "<|/thinking|>"]
    if any(m in stripped for m in thinking_markers):
        return True
    if len(stripped) > 8000 and report_text.count("\n## ") < 3:
        return True
    return False


# ─── Prompt builders ──────────────────────────────────────────────────────────

def build_task_prompt(task: dict, feedback: str = "") -> str:
    """
    Build the prompt injected into OpenCode for the PLAN session.
    Paths must match docs/FORGE_AGENT_RULES.md §10 exactly.
    """
    tid     = task["id"]
    desc    = task["description"]
    context = task.get("context", "")
    phase   = task.get("phase", "1")
    project = task["project"]

    prompt = (
        f"Task: {tid}\n"
        f"Description: {desc}\n"
        f"Phase: {phase}\n"
        f"Project: {project}\n\n"
    )

    if context:
        prompt += f"Context:\n{context}\n\n"

    if feedback:
        prompt += f"Revision feedback from project owner:\n{feedback}\n\n"

    phase_padded = str(phase).zfill(3)
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
    """
    tid     = task["id"]
    desc    = task["description"]
    phase   = task.get("phase", "1")
    project = task["project"]

    return (
        f"Task: {tid}\n"
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
        f"4. PLATFORM CROSS-CHECK: Run all three checks in order. Zero errors required for each.\n"
        f"   a) `cargo check --target x86_64-pc-windows-gnu --workspace --features mock-hardware`\n"
        f"      (mock-hardware Windows-gnu cross-check — catches cfg-gated scaffold errors)\n"
        f"   b) `cargo check --bin anvilml`\n"
        f"      (real-hardware Linux native — exercises #[cfg(unix)] detection paths)\n"
        f"   c) `cargo check --bin anvilml --target x86_64-pc-windows-gnu`\n"
        f"      (real-hardware Windows-gnu cross-check — exercises #[cfg(windows)] detection paths)\n"
        f"   Record verbatim output of all three in ## Platform Cross-Check in the report.\n"
        f"5. TEST: Run the full test suite for every affected crate/package.\n"
        f"   Fix all failures. Zero failures required before proceeding.\n"
        f"6. CONFIG DRIFT GATE: Run\n"
        f"   `cargo test -p backend --features mock-hardware -- config_reference`.\n"
        f"   Zero failures required. See docs/FORGE_AGENT_RULES.md §5.8.\n"
        f"7. STAGE: Run git add -A inside the project repo ({project}).\n"
        f"   Do NOT run git commit or git push — The Forge commits and pushes.\n"
        f"8. REPORT: Write .forge/reports/{tid}_implement.md.\n"
        f"   See docs/FORGE_AGENT_RULES.md (implementation report format).\n"
        f"   Write this ONLY after all tests pass and files are staged.\n"
        f"9. UPDATE STATE: Write .forge/state/CURRENT_TASK.md:\n"
        f"     Task: {tid}\n"
        f"     Step: IMPLEMENT\n"
        f"     Status: COMPLETE\n"
        f"     Updated: <ISO 8601 UTC timestamp>\n"
        f"10. STOP. The Forge will commit, seek push approval, and push.\n"
    )