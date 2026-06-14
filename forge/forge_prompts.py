"""
forge_prompts.py — Task prompt builders and report file helpers.

Prompts are intentionally project-agnostic. All build, format, lint, cross-check,
test, and gate commands are defined in each project's docs/ENVIRONMENT.md.
The prompts enforce sequence and exit-code contracts only; full behavioural
specification lives in agents/forge-plan.md and agents/forge-act.md.
"""

import re
from pathlib import Path
from typing import Optional

from . import forge_config as cfg
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
    return report_text


def _is_thinking_trace(report_text: str) -> bool:
    """
    Return True if the plan report looks like a raw thinking trace rather
    than a structured plan report.
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

def build_task_prompt(task: dict, feedback: Optional[str] = None) -> str:
    """
    Build the prompt injected into OpenCode for the PLAN session.

    This prompt is intentionally lean. Full behavioural specification —
    codebase inspection, MCP version verification, API shape confirmation,
    report section structure — lives in agents/forge-plan.md which OpenCode
    loads as the active agent for this session. The prompt enforces the
    read-order contract and the write-once constraint only.
    """
    tid          = task["id"]
    desc         = task["description"]
    context      = task.get("context", "")
    phase        = task.get("phase", "1")
    project      = task["project"]
    phase_padded = str(phase).zfill(3)

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

    prompt += (
        f"Instructions — PLAN SESSION ONLY:\n"
        f"1. Read .forge/state/CURRENT_TASK.md and verify Task field matches {tid}.\n"
        f"   If it does not match: write a one-line error to\n"
        f"   .forge/reports/{tid}_plan.md and STOP immediately.\n"
        f"2. Read, in order:\n"
        f"   a. docs/FORGE_AGENT_RULES.md\n"
        f"   b. docs/ENVIRONMENT.md\n"
        f"   c. docs/ARCHITECTURE.md\n"
        f"   d. docs/TASKS_PHASE{phase_padded}.md\n"
        f"   e. docs/<PROJECT>_DESIGN.md (check docs/ for the actual filename)\n"
        f"   Do not proceed past step 2 until all five are read.\n"
        f"3. Inspect the existing source files relevant to this task.\n"
        f"   Read the files listed in the task's Files Affected table, the\n"
        f"   lib.rs or mod.rs of any crate this task touches, adjacent test\n"
        f"   files, and the actual definitions of any types this task will\n"
        f"   consume or produce. See agents/forge-plan.md for the full\n"
        f"   inspection checklist.\n"
        f"4. For every external crate or package this task introduces or\n"
        f"   references by name: query the appropriate MCP tool to resolve\n"
        f"   the current version AND confirm the API shape (type names,\n"
        f"   method names, feature flags). Training-data memory is not a\n"
        f"   valid source for any version number or external API name.\n"
        f"   If a type named in the task context does not exist in the\n"
        f"   resolved version, check for a renamed equivalent; if none exists,\n"
        f"   write the plan with a BLOCKED status for that dependency.\n"
        f"   Do not write a lower version to make a missing type resolve.\n"
        f"5. Write the plan report to .forge/reports/{tid}_plan.md.\n"
        f"   The report must follow the exact section structure defined in\n"
        f"   agents/forge-plan.md (11 mandatory sections including\n"
        f"   ## Resolved Dependencies). The first and only write must start\n"
        f"   with the exact line '# Plan Report: {tid}'. Do not write\n"
        f"   narration, thinking, or reading progress to this file.\n"
        f"   Write ONLY the plan report. No source code, no test files,\n"
        f"   no build commands.\n"
        f"6. Verify the report:\n"
        f"   head -1 .forge/reports/{tid}_plan.md   # must be: # Plan Report: {tid}\n"
        f"   grep '^## ' .forge/reports/{tid}_plan.md  # must show 11 headings\n"
        f"   wc -l .forge/reports/{tid}_plan.md        # must be > 40\n"
        f"   If any check fails, write a corrective overwrite before continuing.\n"
        f"7. Update .forge/state/CURRENT_TASK.md:\n"
        f"     Task: {tid}\n"
        f"     Step: PLAN\n"
        f"     Status: COMPLETE\n"
        f"     Updated: <ISO 8601 UTC timestamp>\n"
        f"8. STOP. Do not proceed to implementation.\n"
        f"   The Forge orchestrator handles approval and will resume in a new session.\n"
    )
    return prompt


def build_act_prompt(task: dict, approved_plan: str) -> str:
    """
    Build the prompt injected into OpenCode for the ACT (implementation) session.
    The approved plan is injected verbatim — OpenCode must implement strictly to it.

    This prompt is intentionally lean. Full behavioural specification —
    codebase inspection, version floor rule, API verification, report section
    structure — lives in agents/forge-act.md which OpenCode loads as the active
    agent for this session. The prompt enforces the step sequence and the
    critical version floor rule only.
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
        f"1. RESOLVE DEPS: For every dependency this task adds or modifies,\n"
        f"   query the appropriate MCP tool before writing any code.\n"
        f"   VERSION FLOOR RULE: the version returned by MCP is the floor —\n"
        f"   you may not write a lower version into any manifest for any reason\n"
        f"   unless docs/ENVIRONMENT.md or docs/ANVILML_DESIGN.md explicitly\n"
        f"   names an older version with a technical justification.\n"
        f"   If an API type or method named in the approved plan does not exist\n"
        f"   in the MCP-resolved version: check for a renamed equivalent and use\n"
        f"   it (document in ## Deviations from Plan); if no equivalent exists,\n"
        f"   set Status=BLOCKED, document under ## Blockers, and STOP.\n"
        f"   Do not search older versions. Do not downgrade to make a missing\n"
        f"   API compile.\n"
        f"2. INSPECT: Read all files listed in the approved plan's Files Affected\n"
        f"   table that already exist on disk, the lib.rs/mod.rs of any crate\n"
        f"   this task touches, adjacent test files, and the actual definitions\n"
        f"   of any types you will call or return. See agents/forge-act.md for\n"
        f"   the full inspection checklist and the three defect categories this\n"
        f"   prevents.\n"
        f"3. IMPLEMENT: Write all source code, tests, and CI changes as specified\n"
        f"   in the approved plan. Scope is strictly limited to the plan's\n"
        f"   In Scope section. Follow the inline documentation, logging, error\n"
        f"   handling, and test isolation standards in agents/forge-act.md.\n"
        f"3a. TESTS.MD: Immediately after writing test files, update docs/TESTS.md\n"
        f"   with one entry per new or modified test using the format defined in\n"
        f"   ANVILML_DESIGN.md §16.1. Use the plan's Tests table as the starting\n"
        f"   point for preconditions, inputs, and expected output, then refine\n"
        f"   based on what was actually implemented. If docs/TESTS.md does not yet\n"
        f"   exist, create it with entries for this task's tests only. Do not\n"
        f"   defer this step — context is available now and will not be later.\n"
        f"   See FORGE_AGENT_RULES.md §5.10.\n"
        f"4. COMPILE CHECK: Run a fast compile check before the full test suite:\n"
        f"   cargo check --workspace --features mock-hardware   (Rust)\n"
        f"   python -m py_compile <new_files>                   (Python)\n"
        f"   Fix all compile errors before proceeding.\n"
        f"5. VERSION BUMP: For every crate or package whose source files were\n"
        f"   modified in step 3, increment the patch digit (Z in X.Y.Z) of its\n"
        f"   manifest [package] version by 1. Preserve X and Y exactly.\n"
        f"   The workspace release version is read-only — never modify it.\n"
        f"   See docs/ENVIRONMENT.md §12 for manifest locations.\n"
        f"6. FORMAT (pass 1): Run the project's formatter in-place as documented\n"
        f"   in docs/ENVIRONMENT.md. Fix the cause if it exits non-zero.\n"
        f"7. LINT: Run all linter passes as defined in docs/ENVIRONMENT.md.\n"
        f"   Zero warnings permitted. List pre-existing fixes in\n"
        f"   ## Deviations from Plan. Never document a warning and skip it.\n"
        f"8. PLATFORM CROSS-CHECK: Run every cross-check defined in\n"
        f"   docs/ENVIRONMENT.md. Zero errors required. Record verbatim output\n"
        f"   in ## Platform Cross-Check.\n"
        f"9. TEST: Run the full test suite for every affected module as documented\n"
        f"   in docs/ENVIRONMENT.md. Fix all failures. Zero failures required.\n"
        f"   Diagnose any failure that passes on retry — see agents/forge-act.md\n"
        f"   for the parallelism-induced vs true flakiness distinction.\n"
        f"10. PUBLIC API VERIFICATION: Run:\n"
        f"    git diff HEAD -- <modified_files> | grep '^+.*pub ' | head -40\n"
        f"    Confirm every new pub item matches the plan's Public API Surface\n"
        f"    table. Document additions or removals in ## Public API Delta.\n"
        f"11. PROJECT GATES: Run every mandatory post-test gate defined in\n"
        f"    docs/ENVIRONMENT.md. Zero failures required.\n"
        f"12. FORMAT (pass 2 — final gate): Run the formatter in check-only mode.\n"
        f"    Exit 0 required before staging. If non-zero: run formatter in-place\n"
        f"    (pass 3), re-run compile check, confirm no breakage. If compilation\n"
        f"    breaks after reformatting: set Status=BLOCKED, document under\n"
        f"    ## Blockers, STOP.\n"
        f"13. STAGE: Run git add -A inside the project repo ({project}).\n"
        f"    Do NOT run git commit or git push — The Forge commits and pushes.\n"
        f"14. REPORT: Write .forge/reports/{tid}_implement.md.\n"
        f"    The report must follow the exact section structure defined in\n"
        f"    agents/forge-act.md (includes ## Public API Delta). Include\n"
        f"    verbatim output for format gate, tests, cross-check, and all gates.\n"
        f"    Write this ONLY after all steps above are complete.\n"
        f"15. UPDATE STATE: Write .forge/state/CURRENT_TASK.md:\n"
        f"      Task: {tid}\n"
        f"      Step: IMPLEMENT\n"
        f"      Status: COMPLETE\n"
        f"      Updated: <ISO 8601 UTC timestamp>\n"
        f"16. STOP. The Forge will commit, seek push approval, and push.\n"
    )