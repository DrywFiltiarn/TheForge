"""
forge_prompts.py — Task prompt builders and report file helpers.

Prompts are intentionally project-agnostic. All build, format, lint, cross-check,
test, and gate commands are defined in each project's docs/ENVIRONMENT.md.
The prompts enforce sequence and exit-code contracts only.
"""

from pathlib import Path
from typing import Optional

from . import forge_config as cfg


# ─── Path helpers ─────────────────────────────────────────────────────────────

def plan_report_path(project: str, task_id: str) -> Path:
    """Return the expected path for a plan report inside the project repo."""
    from .forge_repos import resolve_project_path
    return resolve_project_path(project) / ".forge" / "reports" / f"{task_id}_plan.md"


def implement_report_path(project: str, task_id: str) -> Path:
    """Return the expected path for an implementation report inside the project repo."""
    from .forge_repos import resolve_project_path
    return resolve_project_path(project) / ".forge" / "reports" / f"{task_id}_implement.md"


def write_forge_plan_report(project: str, task_id: str, content: str) -> None:
    path = plan_report_path(project, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def read_plan_report(project: str, task_id: str) -> Optional[str]:
    path = plan_report_path(project, task_id)
    return path.read_text(encoding="utf-8") if path.exists() else None


def read_implement_report(project: str, task_id: str) -> Optional[str]:
    path = implement_report_path(project, task_id)
    return path.read_text(encoding="utf-8") if path.exists() else None


def extract_plan_section(plan_text: str, section: str) -> str:
    """
    Extract a named ## section from a plan report.
    Returns the section body (excluding the heading line) or empty string if absent.
    """
    lines = plan_text.splitlines()
    in_section = False
    body: list[str] = []
    heading = f"## {section}"
    for line in lines:
        if line.strip() == heading:
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                break
            body.append(line)
    return "\n".join(body).strip()


def _is_thinking_trace(text: str) -> bool:
    """
    Return True if the text looks like an OpenCode thinking trace rather than
    a real plan or implementation report.
    """
    lower = text.lower()
    indicators = [
        "let me think",
        "i need to",
        "i'll start by",
        "first, i",
        "okay, so",
        "alright,",
    ]
    return any(lower.startswith(ind) for ind in indicators)


# ─── Prompt builders ──────────────────────────────────────────────────────────

def build_task_prompt(task: dict, feedback: Optional[str] = None) -> str:
    """
    Build the prompt injected into OpenCode for the PLAN session.
    All project-specific details (toolchain, commands, etc.) come from
    docs/ENVIRONMENT.md which the agent reads at session start.
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

    This prompt is intentionally project-agnostic. All concrete build, format, lint,
    cross-check, test, and gate commands are defined in docs/ENVIRONMENT.md for the
    target project. The prompt enforces sequence and exit-code contracts only.
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
        f"Read docs/ENVIRONMENT.md before step 1. All build, format, lint,\n"
        f"cross-check, test, and gate commands for this project are defined there.\n"
        f"The steps below define the required sequence and exit-code contracts;\n"
        f"the specific commands come from docs/ENVIRONMENT.md.\n\n"
        f"1. IMPLEMENT: Write all source code, tests, and CI changes as specified\n"
        f"   in the approved plan. Scope is strictly limited to the plan's\n"
        f"   'In Scope' section. Do not add anything not listed there.\n"
        f"2. FORMAT (pass 1): Run the project's formatter in-place (not check-only\n"
        f"   mode) as documented in docs/ENVIRONMENT.md. If the formatter exits\n"
        f"   non-zero, fix the cause before proceeding.\n"
        f"3. LINT: Run all linter passes as defined in docs/ENVIRONMENT.md.\n"
        f"   Fix ALL warnings and errors. Zero warnings permitted.\n"
        f"   List any pre-existing fixes applied (not introduced by this task)\n"
        f"   in ## Deviations from Plan. Never document a warning and skip it.\n"
        f"4. PLATFORM CROSS-CHECK: Run every cross-check defined in\n"
        f"   docs/ENVIRONMENT.md (e.g. Windows target, browser bundle, alternate\n"
        f"   runtime). Zero errors required. Record verbatim output in\n"
        f"   ## Platform Cross-Check in the report.\n"
        f"5. TEST: Run the full test suite for every affected package/crate/module\n"
        f"   as documented in docs/ENVIRONMENT.md. Fix all failures.\n"
        f"   Zero failures required before proceeding.\n"
        f"   If a failure passes on retry, diagnose before continuing:\n"
        f"   (a) Parallelism-induced failures (database locked, port conflict, shared\n"
        f"       temp file, migration collision) are isolation defects — fix them by\n"
        f"       giving each test its own independent state (unique TempDir, unique\n"
        f"       port, unique in-memory fixture). Do NOT use serial test execution\n"
        f"       unless the resource is physically singular (e.g. a hardware device);\n"
        f"       if you must, justify it in ## Deviations from Plan.\n"
        f"   (b) True flakiness (timing, network) must be documented in ## Test\n"
        f"       Results with root cause identified; the final recorded run must\n"
        f"       show 0 failures.\n"
        f"6. PROJECT GATES: Run every mandatory post-test gate defined in\n"
        f"   docs/ENVIRONMENT.md (e.g. config drift check, schema validation,\n"
        f"   bundle size check, type coverage). Zero failures required.\n"
        f"   See docs/FORGE_AGENT_RULES.md §5.8 and §5.9.\n"
        f"7. FORMAT (pass 2 — final gate): Run the project's formatter in\n"
        f"   check-only mode as documented in docs/ENVIRONMENT.md.\n"
        f"   Exit 0 is required. If non-zero, formatting drift was introduced\n"
        f"   by lint or test fixes made after pass 1. Run the formatter in-place\n"
        f"   once more (pass 3), then immediately re-run the project's build or\n"
        f"   compile-check command to confirm the reformat did not break\n"
        f"   compilation. If compilation breaks after reformatting: document as a\n"
        f"   blocker in ## Blockers, set Status=BLOCKED, and STOP. Do not proceed\n"
        f"   to staging until format check exits 0 and build exits 0.\n"
        f"8. STAGE: Run git add -A inside the project repo ({project}).\n"
        f"   Do NOT run git commit or git push — The Forge commits and pushes.\n"
        f"9. REPORT: Write .forge/reports/{tid}_implement.md.\n"
        f"   See docs/FORGE_AGENT_RULES.md (implementation report format).\n"
        f"   Include verbatim output for format gate, tests, cross-check, and\n"
        f"   all project gates. Write this ONLY after all steps above are complete.\n"
        f"10. UPDATE STATE: Write .forge/state/CURRENT_TASK.md:\n"
        f"     Task: {tid}\n"
        f"     Step: IMPLEMENT\n"
        f"     Status: COMPLETE\n"
        f"     Updated: <ISO 8601 UTC timestamp>\n"
        f"11. STOP. The Forge will commit, seek push approval, and push.\n"
    )