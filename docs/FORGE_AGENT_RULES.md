# FORGE_AGENT_RULES.md — Forge Agent Operating Rules

**Read by:** OpenCode forge-plan and forge-act agents at the start of every session.
**Authoritative for:** task atomicity, git rules, test/CI requirements, context window
management, error handling, file/path conventions, and prohibited behaviours.

This document is project-agnostic. Project-specific build commands, test runners,
platform targets, config sync requirements, and technology stack details are defined
in the project's own `docs/ARCHITECTURE.md`, `docs/ENVIRONMENT.md`, and
`docs/<PROJECT>_DESIGN.md`. Read those documents before writing any code or plan.


---

## 1. Identity and Role

The agent is an implementation agent. It does not make project-level decisions.
It executes exactly what The Forge assigns: plan OR implement, never both in one session.
The Forge owns git, Discord, and all approval gates.

**Permitted output types:**
- PLAN session → exactly one markdown report file at `.forge/reports/<TASK_ID>_plan.md`, then STOP
- ACT session → source code, tests, CI updates, one report file, local git stages, then STOP

**The agent MUST NEVER:**
- Commit or push to any repository — git is exclusively The Forge's domain
- Send messages to Discord
- Edit `forge.py`, `state.json`, or any file under `.forge/tasks/`
- Delete or rename report files already written
- Exceed the scope of the current task as defined in the task context

---

## 2. Task Identification

Every session begins with a structured header injected by The Forge:

```
Task: <TASK_ID>
Description: <description>
Phase: <NNN>
Project: <name>
```

- **TASK_ID format:** `P<phase>-<letter><number>` e.g. `P1-A3`, `P4-B7`
- **Phase numbering:** 001–999; maps to a named phase in `docs/PHASES.md`
- **Project:** logical name registered in `repos.json` (e.g. `anvilml`, `bloomeryui`)
- Each task targets exactly **one** project. Multi-repo work is split into separate tasks.

---

## 3. Git Rules

These rules are absolute. Violations break the pipeline and may corrupt repository state.

| Rule | Requirement |
|------|-------------|
| 3.1 | Do NOT commit. `git commit` is exclusively executed by The Forge. Stage only: `git add -A`. |
| 3.2 | Do NOT push. `git push` is exclusively executed by The Forge after push approval. |
| 3.3 | Do NOT perform any git operation outside the task's project repo. |
| 3.4 | Commit messages are authored by The Forge in Conventional Commits format: `<type>(<project>): <task_id> — <description>`. |
| 3.5 | Do not amend, rebase, or force-push any commit. |
| 3.6 | Do not create, delete, or rename branches. All work is on the project's configured working branch as set in `repos.json`. The Forge verifies and switches to the correct branch before invoking the agent. |
| 3.7 | Do not modify `.gitmodules` or any CI workflow file unless explicitly listed in the task's "Files Affected" table. |

---

## 4. Task Atomicity Rules

Tasks are intentionally small. Implement exactly the task defined — no more, no less.

| Rule | Requirement |
|------|-------------|
| 4.1 | Do not implement functionality not listed in the plan's "In Scope" section, even if it would be "helpful" or "obviously needed". |
| 4.2 | Do not refactor code outside the files listed in "Files Affected" unless a failing test in those files requires it. |
| 4.3 | Do not upgrade dependencies unless the task explicitly requires it. |
| 4.4 | Do not modify unrelated tests. Do not delete tests. |
| 4.5 | If a prerequisite task's output is missing or incomplete, STOP and write the blocker under `## Blockers` in the report. Do not attempt to compensate. |

---

## 5. Test and CI Requirements

| Rule | Requirement |
|------|-------------|
| 5.1 | Every task that writes source code MUST include tests. No exceptions. |
| 5.2 | The test suite for the affected module/package/crate must exit 0 before writing the implementation report. Use the test runner appropriate for the project's language stack as documented in `docs/ENVIRONMENT.md`. |
| 5.3 | The full project test suite must exit 0 before writing the report. Regressions caused by this task must be fixed. |
| 5.4 | **Test file naming:** Follow the conventions established in `docs/ENVIRONMENT.md` for the project's language stack. When no convention is documented: place unit tests adjacent to the code under test; place integration tests in a `tests/` directory. |
| 5.5 | When CI workflow files are modified: preserve all existing jobs, add new job/step only if the plan specifies it, do not disable or skip any existing test job. |
| 5.6 | If tests fail after implementation, fix the failures before writing the report. Test-fix is part of the ACT session. Do NOT write the implementation report with known failures. |
| 5.7 | **PLATFORM CROSS-CHECK** — if `docs/ENVIRONMENT.md` specifies a secondary target platform (e.g. Windows, a specific CPU architecture, a browser runtime), run the documented cross-check command before writing the report. Record the result in `## Test Results` alongside the primary platform output. A passing primary-platform build is NOT sufficient evidence of correctness if a cross-check is required. |
| 5.8 | **PROJECT-SPECIFIC GATES** — `docs/ENVIRONMENT.md` may define mandatory post-test gates (e.g. config surface sync tests, schema drift checks, bundle size checks). Run every gate listed there before staging. A task may NOT be marked COMPLETE while any documented gate fails. |

---

## 6. Dependency Version Resolution

MCP tools are available for version and API lookups. Which tools apply depends on the
project's language stack — check `docs/ENVIRONMENT.md` for the authoritative list.

Common tools:

| Tool | Use for |
|------|---------|
| `rust-docs` | Rust crates — current stable version, feature flags, API shape |
| `pypi-query` | Python packages — current release, correct PyPI package name |

Additional tools may be configured for other stacks (npm packages, Go modules, etc.).
The available MCP tools are listed in `~/.config/opencode/opencode.json`.

**Rules:**
- 6.1 In PLAN sessions: verify every dependency named in the task context before writing the plan.
- 6.2 In ACT sessions: use to resolve compiler or runtime errors from API mismatches. Record the lookup and result in `## Resolved Dependencies` in the report.
- 6.3 Do NOT use lookup results to introduce any dependency not already present in the project's dependency manifests (e.g. `Cargo.toml`, `package.json`, `requirements*.txt`, `pyproject.toml`). If a looked-up API reveals an impossible dependency combination, document under `## Dependency Notes`, set `Status=BLOCKED`, and STOP.
- 6.4 If an MCP server is unavailable, fall back to the most recent version visible in the project's lockfile (`Cargo.lock`, `package-lock.json`, `yarn.lock`, `requirements*.txt`) and document the fallback in `## Blockers`.

---

## 7. Context Window Management

| Threshold | Action |
|-----------|--------|
| 50% | Continue normally. No output about context usage. |
| 65% | STOP accumulating new context. Finish the current file or function, run tests, stage changes (`git add -A`), write a partial implementation report with a `## Continuation` section listing exactly what remains. Update `.forge/state/CURRENT_TASK.md` with `Status=PARTIAL`. STOP — The Forge will resume in a fresh session. |

- Do NOT compress or summarise prior content to extend the session. A clean partial is always preferable to degraded output.
- Do NOT hallucinate file contents or API signatures when context is high. If uncertain about a symbol, re-read the relevant file even at token cost. Wrong assumptions compound.
- The Forge will detect `Status=PARTIAL` and resume the ACT session with the partial report injected as context. Do not attempt to detect or handle resumption yourself — the injected header will say `RESUME SESSION`.

---

## 8. Output Structure Discipline

Report structure is fixed regardless of task complexity. The model must not abbreviate
or drop sections, regardless of how simple the task appears.

**Patterns to avoid:**
- Omitting `## Files Changed` because `## Commit Log` is present (or vice versa) — both are always required
- Writing a prose summary instead of the header table
- Collapsing `### In Scope` / `### Out of Scope` into a single paragraph
- Skipping `## Risks and Mitigations` with "no risks identified" — write the table with at least one row; if genuinely none apply, write `Risk="None identified"`, `Mitigation="n/a"`
- Writing `## Test Results` as a summary sentence rather than verbatim test runner output

**Write method — bash heredoc only:** Always write the plan or implementation report using
a bash heredoc with a single-quoted delimiter. Never use the `write` tool for report files.
The `write` tool corrupts technical identifiers (hex values, CamelCase names, numeric suffixes
like `bf16`/`fp16`, section signs `§`) in long strings. The bash heredoc is immune:

```bash
cat << 'ENDPLAN' > .forge/reports/<TASK_ID>_plan.md
# Plan Report: <TASK_ID>
...complete content...
ENDPLAN
```

**Single write rule:** Write the complete finished document in one heredoc. Do not write
interim notes, progress updates, or partial drafts to the report file. The report must not
exist until it is complete and ready.

**Correction exception:** If after writing you verify the file contains corrupted content
(garbled identifiers, missing sections, wrong first line), a single corrective overwrite is
permitted using the same bash heredoc method. No more than two writes total per file per
session. If corruption persists after two attempts, set `Status=BLOCKED` and stop.

A report that does not begin with `# Plan Report: <TASK_ID>` or
`# Implementation Report: <TASK_ID>` is malformed and constitutes a session failure.

**Pre-Stop Verification (use exactly this):**
```bash
head -1 .forge/reports/<TASK_ID>_plan.md        # must print: # Plan Report: <TASK_ID>
grep "^## " .forge/reports/<TASK_ID>_plan.md    # must show all section headings
wc -l .forge/reports/<TASK_ID>_plan.md          # must be >30 lines
```
Do not write Python verification scripts. These three commands are sufficient.

---

## 9. Error Handling and Stopping

| Rule | Requirement |
|------|-------------|
| 9.1 | If an unrecoverable error is encountered: (a) write a `## Blockers` section to the in-progress report; (b) update `.forge/state/CURRENT_TASK.md` with `Status=BLOCKED`; (c) STOP immediately. Do not guess, retry indefinitely, or continue with an unsanctioned workaround. |
| 9.2 | Build failures within the task's scope (caused by code written in this session) MUST be fixed before writing the report. They are not blockers; they are part of the test-fix loop. |
| 9.3 | **Pre-existing warnings** (present before this task's changes, surfaced by `cargo clippy` or the compiler) MUST be fixed via the most minimal correct solution, even if the affected file is outside the task's originally listed scope. Never document a warning and skip it — a skipped warning persists indefinitely and will never be resolved by coincidence. Fix it, list the file and the change under `## Deviations from Plan`, and continue. |
| 9.4 | **Pre-existing errors** in files this task does not otherwise touch are blockers: document under `## Blockers` and STOP. If the error is in a file this task already modifies, fix it as part of the normal test-fix loop (rule 9.2) and note it under `## Deviations from Plan`. |
| 9.5 | **Test failures that pass on retry** must be diagnosed before proceeding — never documented and accepted as flakiness without investigation. (a) **Parallelism-induced failures** (database locked, port conflict, shared temp file, migration collision, shared global state) are deterministic isolation defects, not flakiness. Fix the test isolation so each test owns its own independent state (unique `tempfile::TempDir`, unique port, unique in-memory fixture). `#[serial]` or `--test-threads=1` is only permitted when the shared resource is physically singular and cannot be instantiated per-test (e.g. a hardware device); if used, justify it in `## Deviations from Plan`. (b) **True flakiness** (timing, network, external service) must be documented in `## Test Results` with the root cause identified; the final recorded run must show 0 failures. |

---

## 10. File and Path Conventions

| Convention | Detail |
|------------|--------|
| Report files | `.forge/reports/<TASK_ID>_plan.md` (PLAN session), `.forge/reports/<TASK_ID>_implement.md` (ACT session). Paths relative to project repo root. `.forge/` is dot-prefixed (hidden), version-controlled, committed by The Forge. |
| State file | `.forge/state/CURRENT_TASK.md` — update at end of every session. Format: `Task: <TASK_ID>`, `Step: <PLAN\|IMPLEMENT>`, `Status: <COMPLETE\|PARTIAL\|BLOCKED>`, `Updated: <ISO 8601 UTC>` |
| Phase task docs | `docs/TASKS_PHASE<NNN>.md` — read, do not modify. |
| Task JSON files | `.forge/tasks/tasks_phase<NNN>.json` — read, do not modify. |
| Project scope | The task's `project` field names the single repository. Do NOT read or write files outside that repository. |
| Root files | Do not create files at the repository root unless explicitly listed in the plan's "Files Affected" table. |
| `.forge/` vs `forge/` | `.forge/` is the dot-prefixed directory inside the project repo (hidden). The Forge orchestrator's own `forge/` package directory is a separate thing and lives outside the project repo entirely. Never confuse the two. |

---

## 11. Prohibited Behaviours

The following are unconditional prohibitions regardless of task context:

- No `git push`, `git push --force`, or any remote write operation
- No modifications to `forge.py`, `state.json`, or any file under `.forge/tasks/`
- No modifications to files outside the single project repo named in the task's `project` field
- No use of environment variables, secrets, or API keys not already present in the repository's documented configuration
- No network calls to external services except via configured MCP tools
- No interactive prompts — all tool invocations must be non-interactive (use `-y`, `--yes`, `--non-interactive` flags where applicable)
- No spawning of background processes or daemons that outlive the session
- No modifications to `.env` files or secrets unless the task explicitly lists a specific `.env.example` change in "Files Affected"

---

## 12. Phase Numbering Reference

Phase numbers are zero-padded to three digits in filenames (`001`, `002` …) and displayed
as plain integers in task IDs (`P1-A3`, not `P001-A3`). The canonical mapping is in
`docs/PHASES.md` — read it, do not rely on this file's examples.