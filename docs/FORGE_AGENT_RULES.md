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
| 5.7 | **PLATFORM CROSS-CHECK** — if `docs/ENVIRONMENT.md` specifies one or more secondary target platforms or runtimes (e.g. Windows cross-compilation, a browser bundle check, an alternate CPU architecture), run every cross-check command listed there before writing the report. Record verbatim output in `## Platform Cross-Check`. A passing primary-platform build is NOT sufficient evidence of correctness if cross-checks are required. |
| 5.8 | **PROJECT-SPECIFIC GATES** — `docs/ENVIRONMENT.md` may define mandatory post-test gates (e.g. config surface sync tests, schema drift checks, bundle size checks, type coverage thresholds). Run every gate listed there before staging. A task may NOT be marked COMPLETE while any documented gate fails. |
| 5.9 | **FORMAT FINAL GATE** — Immediately before `git add -A`, run the project's formatter in check-only mode as documented in `docs/ENVIRONMENT.md`. Exit 0 is required. If non-zero: run the formatter in-place, then immediately re-run the project's build or compile-check command to verify compilation still passes. If compilation fails after reformatting, set Status=BLOCKED and STOP — do not stage. A task that reaches `git add -A` with a non-zero format check is a pipeline defect. |

---

## 6. Dependency Version Resolution

MCP tools for dependency version resolution are available and MUST be used before writing
any version number, feature flag, or API call — in both PLAN and ACT sessions.
The available tools depend on what is configured in `~/.config/opencode/opencode.json`.
Common mappings:

| Stack          | MCP tool       | Covers                                         |
|----------------|----------------|------------------------------------------------|
| Rust           | `rust-docs`    | crates.io versions, feature flags, API shape   |
| Python         | `pypi-query`   | PyPI releases, correct package names           |
| Node/TypeScript| check opencode.json — an npm MCP may be configured | npm package versions |

Use the tool appropriate for the project's language stack. If no MCP tool covers a required
dependency type, document the gap in `## Blockers` and fall back to the lockfile version.

**Rules:**
- 6.1 In PLAN sessions: verify every dependency named in the task context before writing
  the plan.
- 6.2 In ACT sessions: query before writing or accepting any dependency version —
  including versions already written in the approved plan. **ACT is authoritative over
  PLAN on version numbers.** If the MCP lookup returns a version that differs from what
  the plan specified, use the MCP result, not the plan's version. Record every lookup,
  the plan's version, and the resolved version in `## Resolved Dependencies`. If the
  resolved version is semver-incompatible with existing code, document the incompatibility
  under `## Blockers` and stop.
- 6.3 Do NOT use lookup results to introduce any dependency not already declared in the
  project's dependency manifests (`Cargo.toml`, `package.json`, `requirements*.txt`,
  `pyproject.toml`, etc.). If a looked-up API reveals an impossible dependency
  combination, document under `## Dependency Notes`, set `Status=BLOCKED`, and STOP.
- 6.4 If an MCP server is unavailable, fall back to the most recent version in the
  project's lockfile (`Cargo.lock`, `package-lock.json`, `yarn.lock`,
  `requirements*.txt`, etc.) and document the fallback in `## Blockers`.
- 6.5 Follow the dependency declaration convention already established in the project's
  existing manifests. Do not introduce inline version strings where the project uses a
  workspace or monorepo root manifest. If the correct convention is unclear, read the
  existing manifests before adding any dependency.

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
| 9.3 | **Pre-existing warnings** (present before this task's changes, surfaced by `cargo clippy` or the compiler) MUST be fixed via the most minimal correct solution, even if the affected file is outside the task's originally listed scope. Never document a warning and skip it — a skipped warning persists indefinitely. Fix it, list the file and the change under `## Deviations from Plan`, and continue. |
| 9.4 | **Pre-existing errors** in files this task does not otherwise touch are blockers: document under `## Blockers` and STOP. If the error is in a file this task already modifies, fix it as part of the normal test-fix loop (rule 9.2) and note it under `## Deviations from Plan`. |
| 9.5 | **Test failures that pass on retry** must be diagnosed before proceeding — never documented and accepted as flakiness without investigation. (a) **Parallelism-induced failures** (database locked, port conflict, shared temp file, migration collision, shared global state) are deterministic isolation defects, not flakiness. Fix the test isolation so each test owns its own independent state (unique `tempfile::TempDir`, unique port, unique in-memory fixture). `#[serial]` or `--test-threads=1` is only permitted when the shared resource is physically singular and cannot be instantiated per-test (e.g. a hardware device); if used, justify it in `## Deviations from Plan`. (b) **True flakiness** (timing, network, external service) must be documented in `## Test Results` with the root cause identified; the final recorded run must show 0 failures. |
| 9.6 | **Environment-variable test isolation** — any test that calls `std::env::set_var` (Rust), `os.environ[...] =` (Python), or equivalent process-global env mutation MUST: (1) capture the pre-existing value of every variable it sets before mutating it; (2) restore every variable to its original value — or remove it if it was absent — as the **unconditional last statements** of the test body, outside any conditional or assertion block so teardown always runs even on panic or early return; (3) if the test uses `#[serial]` or equivalent serialisation, also add the matching `set_var` setup at the top of the test body rather than relying on inherited env state from a prior test — every serialised test must be fully self-contained. When writing a new test that mutates env vars, include the teardown in the initial implementation; do not treat teardown as a follow-up task. When modifying an existing test that mutates env vars, add teardown in the same change. Failure to follow this rule is an isolation defect under §9.5(a) and must be fixed before writing the implementation report. |

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

## 11. Logging Standards

Every task that adds or modifies code **must** include appropriate logging. Logging is
not optional and is not deferred to a later task. The agent must apply these rules when
writing new code and must fix missing or incorrect logging in any file it already modifies
for another reason — even if the logging gap was pre-existing and not introduced by this task.

### 11.1 General instrumentation obligation

Every function or code path **added or modified** by a task must be assessed for
observability before the task is marked COMPLETE. For each non-trivial code path, ask:

1. **Would an operator need to know this ran?** If yes → INFO (lifecycle event) or
   DEBUG (routine operation).
2. **Would an operator need to know what it decided?** If a branch is taken, a value
   is selected, or a fallback is used → DEBUG with the relevant fields.
3. **Would an operator need to know why it failed or was skipped?** If work is
   discarded, retried, or falls back silently → at minimum WARN with context.

Code that silently succeeds or silently discards work without any log call is a defect
unless the function is a pure data transformation with no side effects and no decision
points (e.g. a type conversion or a sort). The lists in §11.3 and §11.5 are minimum
guaranteed points for known subsystems — not an exhaustive inventory. New code paths
not covered by those lists are subject to this obligation independently.

When in doubt: instrument at DEBUG. A DEBUG call costs nothing at the default INFO
level and is invaluable during diagnosis.

### 11.2 Level assignment

| Level   | Use for |
|---------|---------|
| `ERROR` | Unrecoverable failures that cause an operation or subsystem to abort. Always include `error=` field. |
| `WARN`  | Recoverable anomalies — execution continues but something unexpected occurred. See §11.4 for field discipline. |
| `INFO`  | Operational lifecycle events unconditionally visible at the default log level. See §11.3 for mandatory points. |
| `DEBUG` | Detailed internal state useful for diagnosis but too noisy for production. See §11.5 for mandatory points. |
| `TRACE` | Per-iteration or per-byte detail. Use sparingly; only for hot paths where DEBUG is still too noisy. |

INFO is the default level in production. DEBUG and TRACE are off by default and require
an explicit filter override. Do not demote mandatory INFO events to DEBUG to reduce
noise — fix the message instead.

### 11.3 Mandatory INFO log points

The following classes of event MUST always be logged at INFO. If a task touches the
relevant subsystem and the log call is absent, add it:

- **Database initialisation** — file created for the first time, each migration applied,
  all-migrations-up-to-date (no-op), each seed file applied or skipped
- **Server lifecycle** — bind address on successful listen, graceful shutdown initiated
- **Worker lifecycle** — worker spawned, worker reached Ready state, worker respawned
  after unexpected exit (include exit code or signal)
- **Hardware detection** — each device detected on startup (name, index, type, VRAM)
- **Model registry** — scan completed (include count)
- **Provisioning** — provisioning started (include reason), provisioning completed
  (include duration)

The project's `docs/ENVIRONMENT.md §9` lists the exact required fields for each
mandatory log point. Use those field names — do not invent alternatives.

### 11.4 WARN field discipline

Include the `error=` field in a WARN message **only when it adds information beyond
what the other structured fields already convey**.

- A "file not found" OS error on a `path=` field that already names the missing file
  is **redundant** — omit `error=`. Write the message to indicate the condition
  (e.g. `"scanner: skipping missing path"`).
- An unexpected OS error (permission denied, I/O error, network timeout) on a named
  path or resource is **not redundant** — include `error=`.
- When in doubt: if reading the log line without `error=` still tells the operator
  exactly what went wrong and what was affected, omit it.

### 11.5 Mandatory DEBUG log points

The following classes of event MUST exist at DEBUG level. If a task touches the
relevant subsystem and the log call is absent, add it:

- **IPC** — each message sent to a worker (`worker_id=`, `message_type=`); each event
  received from a worker (`worker_id=`, `event_type=`)
- **Job scheduler** — job dispatched (`job_id=`, `worker_id=`); job state transition
  (`job_id=`, `from=`, `to=`)
- **Model scanner** — each file examined, whether accepted or skipped (`path=`;
  `reason=` if skipped)
- **Hardware detection** — fallback path used when primary enumeration is unavailable
  (`fallback=` naming the method used)

### 11.6 Instrumentation

- Apply `#[tracing::instrument]` (Rust) or equivalent to async functions that represent
  a meaningful unit of work: migration runner, seed loader, worker spawn, job dispatch,
  model scan.
- Span names must be lowercase `snake_case` matching the function or subsystem name.
- Do not instrument tight inner loops or per-packet/per-frame functions.
- Span fields must use structured notation so that log aggregators can index them:
  `tracing::info!(addr = %addr, "listening")` not `tracing::info!("listening on {addr}")`.

### 11.7 Plan and report obligations

**PLAN sessions:** if a task adds, modifies, or touches a subsystem listed in §11.3 or
§11.5, the plan's Approach section must explicitly list the log calls to be added or
verified. Do not leave logging as an implicit side effect.

**ACT sessions:** after implementing, scan every file changed by this task for missing
mandatory log points (§11.3 and §11.5) and apply §11.1 to all new code paths. Add any
that are absent. Record them in `## Files Changed`. Do not mark a task COMPLETE if a
mandatory INFO log point is absent in a subsystem the task touches.

---

## 12. Crate Version Bumping

Every task that modifies source files inside a crate **must** increment that crate's
patch version before staging. This rule applies to all projects regardless of language
stack; see `docs/ENVIRONMENT.md §10` for the project-specific version file locations
and commands.

### 12.1 What triggers a bump

A crate (or package) version must be bumped when **any** of the following are modified
within it during this task:

- Source files (`src/**`, `lib/**`, `worker/**`, equivalent per stack)
- Test files (`tests/**`, `__tests__/**`, equivalent)
- Build scripts (`build.rs`, equivalent)

A bump is **not** required when only the following are modified:

- `Cargo.toml` / `package.json` / `pyproject.toml` dependency entries (no source change)
- Documentation files (`docs/**`, `*.md`, `*.txt`)
- CI workflow files (`.github/**`)
- The crate's own manifest version field (i.e. the bump itself)

### 12.2 Which version field to bump

Bump the patch digit only — the `Z` in `X.Y.Z`:

- `X` (major) and `Y` (minor) are **read-only for agents**. Preserve them exactly as found.
- `Z` (patch) increments by 1 from the value found at the start of this session.
- The workspace release version (`[workspace.package] version` in root `Cargo.toml`,
  or any equivalent top-level product version field) is **always read-only for agents**
  and must never be modified.

### 12.3 When to apply the bump

Apply during the IMPLEMENT step, immediately after writing source changes for that crate.
Do not defer. If multiple crates are modified in one task, bump each independently as you
finish modifying it.

### 12.4 How to apply the bump

Read the current version from the crate's manifest `[package]` section, compute `Z + 1`,
write it back. Target only the `version = "X.Y.Z"` line in `[package]` — do not touch
version strings in dependency declarations or lock files.

For Rust (`Cargo.toml`):
```toml
# Before
[package]
version = "0.1.4"

# After — only Z changes, X.Y preserved exactly
[package]
version = "0.1.5"
```

Do not edit `Cargo.lock` manually — cargo regenerates it on the next build.
Cross-crate path dependencies in this workspace do not carry version pins, so no cascade
update to other crates' `Cargo.toml` files is needed.

### 12.5 Plan and report obligations

**PLAN sessions:** For every crate listed in `## Files Affected` whose source files are
modified, add a row to the Files Affected table:

| Modify | `crates/<name>/Cargo.toml` | Bump patch version `X.Y.Z → X.Y.(Z+1)` |

**ACT sessions:** After implementing, verify the version was bumped for every modified
crate. Record each bump in `## Files Changed`. Do not mark a task COMPLETE if a crate's
source files were modified but its version was not bumped.

---

## 13. Prohibited Behaviours

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

## 14. Phase Numbering Reference

Phase numbers are zero-padded to three digits in filenames (`001`, `002` …) and displayed
as plain integers in task IDs (`P1-A3`, not `P001-A3`). The canonical mapping is in
`docs/PHASES.md` — read it, do not rely on this file's examples.