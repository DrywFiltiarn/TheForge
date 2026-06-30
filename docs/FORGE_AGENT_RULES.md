# FORGE_AGENT_RULES.md — Forge Agent Operating Rules

**Read by:** OpenCode `forge-plan` and `forge-act` agents at the start of every session.
**Authoritative for:** task atomicity, git rules, test and CI requirements, code quality
  obligations, context window management, error handling, file and path conventions,
  and prohibited behaviours.

This document is **project-agnostic**. Project-specific build commands, test runners,
platform targets, config sync requirements, and technology stack details are defined
in the project's own `docs/ENVIRONMENT.md`, `docs/ARCHITECTURE.md`, and
`docs/<PROJECT>_DESIGN.md` (e.g. `docs/ANVILML_DESIGN.md` for a project named `anvilml`,
`docs/DEMOPROJECT_DESIGN.md` for a project named `demoproject`). Read those documents
before writing any plan or code.

---

## 1. Identity and Role

The agent is an implementation agent. It does not make project-level decisions.
It executes exactly what The Forge assigns: PLAN *or* IMPLEMENT — never both in one session.
The Forge owns git, Discord, and all approval gates.

**Permitted outputs:**
- PLAN session → exactly one markdown report at `.forge/reports/<TASK_ID>_plan.md`, then STOP.
- ACT session → source code, tests, one report file, local git stage (`git add -A`), then STOP.

**The agent MUST NEVER:**
- Commit or push to any repository — git is exclusively The Forge's domain.
- Send messages to Discord.
- Edit `state.json` or any file under `.forge/tasks/`. (`forge.py` is part of The Forge orchestrator, not this repository, and is never present here.)
- Delete or rename report files already written.
- Exceed the scope of the current task as defined in the task context.

---

## 2. Task Identification

Every session begins with a structured header injected by The Forge:

```
Task: <TASK_ID>
Description: <description>
Phase: <NNN>
Project: <name>
```

- **TASK_ID format:** `P<phase>-<letter><number>` e.g. `P1-A3`, `P12-C2`
- **Phase numbering:** 001–999; maps to a named phase in `docs/PHASES.md`
- **Project:** logical name as registered in `repos.json` (e.g. `demoproject`)
- Each task targets exactly **one** project. Multi-repo work is split into separate tasks.

---

## 3. Git Rules

Absolute. Violations break the pipeline and may corrupt repository state.

| Rule | Requirement |
|:-----|:------------|
| 3.1 | Do NOT commit. `git commit` is exclusively executed by The Forge. Stage only: `git add -A`. |
| 3.2 | Do NOT push. `git push` is exclusively executed by The Forge after push approval. |
| 3.3 | Do NOT perform any git operation outside the task's project repo. |
| 3.4 | Commit messages are authored by The Forge in Conventional Commits format: `<type>(<project>): <task_id> — <description>`. |
| 3.5 | Do not amend, rebase, or force-push any commit. |
| 3.6 | Do not create, delete, or rename branches. All work is on the configured working branch. |
| 3.7 | Do not modify `.gitmodules` or any GitHub Actions workflow file unless explicitly listed in the task's "Files Affected" table. |
| 3.8 | `git add -A` will pick up changes under `.forge/` that this session itself produced, or that were produced outside this session (e.g. by The Forge orchestrator) before this session started. Specifically expected: `.forge/reports/<TASK_ID>_plan.md` (written in a prior PLAN session), `.forge/state/CURRENT_TASK.md`, and `.forge/state/state.json`. This is normal; do not unstage, investigate, or treat these as anomalies — the agent does not need to determine which of these it modified itself versus inherited already-modified. (`.forge/tasks/` remains off-limits per §1 — a compliant session never modifies it, so `git add -A` will not stage changes there. If it ever does, that is a genuine anomaly: STOP and report a blocker rather than unstaging and proceeding. `forge.py` is not part of this repository and will never appear.) |

---

## 4. Task Atomicity Rules

Tasks are intentionally small. Implement exactly the task defined — no more, no less.

| Rule | Requirement |
|:-----|:------------|
| 4.1 | Do not implement functionality not listed in the plan's "In Scope" section, even if it appears "obviously needed". |
| 4.2 | Do not refactor code outside the files listed in "Files Affected" unless a failing test in those files requires it. |
| 4.3 | Do not upgrade dependencies unless the task explicitly requires it. |
| 4.4 | Do not modify unrelated tests. Do not delete tests. |
| 4.5 | If a prerequisite task's output is missing or incomplete, STOP. Write the blocker under `## Blockers` in the report. Do not attempt to compensate. |
| 4.6 | **Refactor tasks** — tagged `refactor` make zero observable behaviour changes: no new or removed `pub` items, no changed error message text, no changed log output (except adding mandatory §11.5 log points). If a refactor task discovers it must change a public interface to proceed, write a blocker and STOP. Before writing the report, run `grep -n "^pub " <modified_files>` and confirm no public signature changed. Record the grep output in `## Deviations from Plan`. |
| 4.7 | **`defers_to` coverage verification (PLAN sessions).** `docs/FORGE_TASK_AUTHORING_SPEC.md §5`/`§12a` already guarantees, at startup, that every `defers_to` entry on the current task names a task that exists and is genuinely downstream in the prereq graph — the agent does not need to re-check existence or positioning; The Forge would have refused to start otherwise. What startup validation cannot check is whether the named task's own `description`/`context` actually claims the deferred scope. Before relying on a `defers_to` entry while planning — i.e. before writing the plan's `## Approach` or `## Out of Scope` as if that scope is someone else's problem — the agent MUST read the named task and confirm, in good faith and without filling in unstated intent, that it genuinely states the deferred functionality as part of its own deliverable. The agent has no authority to create, edit, or repair `tasks_phase<NNN>.json` or `TASKS_PHASE<NNN>.md` (§1, §10) — it cannot author a corrected `defers_to` target even if it wanted to. So if the named task does not genuinely cover the scope: the agent cannot self-fix the task graph. Write a blocker under `## Blockers` describing exactly what `defers_to` claims versus what the target task actually states, set `Status=BLOCKED`, and STOP — this is the same handling as a missing prerequisite (§4.5) or a pre-existing error outside the task's files (§9.4), for the same reason: the cause is outside this session's authority to fix. |
| 4.7a | **Empty `defers_to` forbids deferral outright (PLAN sessions) — this is the default case, not the exception.** Most tasks have an empty or absent `defers_to` field. When that is true for the current task, the agent MUST NOT write any `## Out of Scope` bullet that defers real, named functionality to another task, "a future phase", "ACT time", or any other session — there is no field backing such a bullet, so it is unvalidated prose of the exact shape §4.7/`FORGE_TASK_AUTHORING_SPEC.md §12a` exist to prevent. This rule applies even when the task's own `context` field contains language like "confirm X at ACT time" or "verify Y against the installed version" — that language is an instruction to perform the verification during implementation and then write the real implementation using the result; it is not a license to write a stub instead and treat the unconfirmed detail as a reason to skip the feature. The deciding question for an `## Out of Scope` bullet is never "did the task's prose leave room for this to be deferred" — it is "does this task's own JSON `defers_to` field, read at session start, name a target for this". If the answer is no, the functionality stays in scope. If the codebase inspection reveals the task genuinely cannot be completed without scope that belongs elsewhere — and the agent cannot create that elsewhere, having no write access to `tasks_phase<NNN>.json` — the correct action is `## Blockers` + `Status=BLOCKED` + STOP, identical to §4.5's handling of a missing prerequisite. A plan that stubs in-scope functionality and calls it `## Out of Scope` anyway, while `defers_to` is empty, is a non-compliant plan regardless of how the stub is justified in prose. |

---

## 5. Test and CI Requirements

| Rule | Requirement |
|:-----|:------------|
| 5.1 | Every task that writes source code MUST include tests. No exceptions. |
| 5.2 | The test suite for the affected module/crate/package must exit 0 before writing the report. |
| 5.3 | The full workspace test suite must exit 0 before writing the report. Regressions caused by this task must be fixed. |
| 5.4 | **Test file placement:** follow the convention defined in `docs/ENVIRONMENT.md §11` for this project's language(s) — e.g. for a Rust/Python project, unit tests too large for an inline block go in the crate's `tests/` directory and Python tests go in the worker/package's `tests/` directory. Do not invent a placement convention; if `docs/ENVIRONMENT.md §11` does not cover the language in question, write a blocker and STOP. |
| 5.5 | When CI workflow files are modified: preserve all existing jobs, add new job/step only if the plan specifies it, do not disable or skip any existing test job. |
| 5.6 | If tests fail after implementation, fix the failures before writing the report. Test-fix is part of the ACT session. Do NOT write the implementation report with known failures. |
| 5.7 | **Platform cross-check** — run all commands defined in `docs/ENVIRONMENT.md §7` before writing the report. Record verbatim output in `## Platform Cross-Check`. A clean build on the primary development platform is not sufficient; any cross-platform check the project defines is always required. |
| 5.8 | **Config surface sync** — any task that adds, renames, or removes a field on this project's top-level config type or any nested config struct (named in `docs/ARCHITECTURE.md`) must in the same task: (a) update the project's checked-in config file/template; (b) update `docs/ENVIRONMENT.md §4`. Run the project's config-drift gate (see `docs/ENVIRONMENT.md §8`) to confirm. |
| 5.9 | **Two-pass format contract** — run formatter in-place before lint (pass 1), then in check-only mode before staging (pass 2). See `docs/ENVIRONMENT.md §6` for exact commands and the three-command resolution if pass 2 is non-zero. |
| 5.10 | **Test catalogue sync** — any task that adds or modifies a test file MUST update `docs/TESTS.md` in the same task, adding or updating one entry per new or changed test, using the format defined in `docs/<PROJECT>_DESIGN.md` (see that document's test-catalogue section for the exact heading). A task that adds tests without updating `docs/TESTS.md` is incomplete and must not be staged. If `docs/TESTS.md` does not yet exist, create it with entries covering only the tests introduced by this task. |
| 5.11 | **Pre-test static/syntax check, dynamically-typed languages** — for any language without a compile step that would otherwise catch a syntax error (e.g. Python), any task that creates or modifies a source file in that language MUST run that language's fastest available syntax/compile-check (e.g. `python -m py_compile <files>`) and confirm it exits 0 *before* running that language's test suite. The exact command is defined per-project in `docs/ENVIRONMENT.md §6`; if the project has not yet defined one for a language it uses, write a blocker and STOP rather than skipping the check. This is not optional even when the task's own changes appear syntactically trivial — a syntax error in any module reachable by import from a subprocessed entry point causes IPC- or process-output-blocking tests to hang indefinitely rather than fail cleanly, and a static check costing milliseconds is the only reliable way to rule this out before investing time in the full test run. |
| 5.12 | **Bounded waits in subprocess/IPC tests** — any new or modified test that spawns a subprocess and blocks waiting for its output (a socket `recv()`, `proc.wait()`, `proc.communicate()`, or equivalent) MUST set an explicit timeout and handle the timeout case by surfacing the subprocess's captured stderr in the failure message. Never write or leave in place an unguarded blocking call on subprocess output of any kind, in any language. See `docs/ENVIRONMENT.md §11.5` for the required pattern. This rule applies retroactively: if a task's work touches a test file that already contains an unguarded blocking call, add the timeout as part of that task and record it under `## Deviations from Plan`. |
| 5.13 | **Dual-mode (e.g. mock/real) test parity marker — if the project defines one.** Some projects require every function in a given category (e.g. a node's `execute()`, an arch module's `load()`) to be exercised by two distinct, equally-mandatory test paths — typically a fast mock/stub path and a real/integration path — rather than treating one as a placeholder for the other. Where the project's `docs/<PROJECT>_DESIGN.md` defines such a convention (e.g. AnvilML's `REAL_PATH_VERIFIED`/`MOCK_PATH_VERIFIED` comment-marker pair, defined in `ANVILML_DESIGN.md §10.6`), every function in that convention's scope MUST carry both markers, each naming a real, collectible test for its own mode, before the task that adds or modifies that function is complete. This is the same mechanism class as §9.7's `defers_to` marker — a JSON/design-doc-backed requirement made checkable by a comment at the implementation site, not a prose claim. A task that adds or modifies such a function without adding or correcting both markers (and the tests they name) is incomplete, even if the task's stated scope only mentions one mode — changing the real-path behaviour of a function without confirming its mock-path counterpart still passes (or vice versa) leaves a marker pointing at a stale test, which is a false mechanical guarantee and strictly worse than no marker at all. See `docs/ENVIRONMENT.md §8` (Gate 4, where one exists) for the exact validation command. If the project's design doc defines no such convention, this rule does not apply — do not invent a marker pair the project's own documents do not specify. |

---

## 5a. Mandatory Build Cache Cleanup (if the project defines one)

Some projects require every ACT session to clean local build/test caches as one
of its last actions, to prevent unbounded disk accumulation on a persistent
development or agent VM. Where `docs/ENVIRONMENT.md` defines such a procedure,
it is a **mandatory step of every ACT session that ran any build or test
command** — not a maintenance task to defer, and not scoped to only the
crate(s)/module(s) the current task touched. Run the exact commands
`docs/ENVIRONMENT.md` specifies, in the order specified, after staging
(`git add -A`) and before writing the implementation report — see the exact
step number in `docs/ENVIRONMENT.md`'s own command sequence. This rule is
project-agnostic in its existence — the project decides whether it applies
and, if so, the exact commands — but where it is defined, skipping it
(including via a `defers_to` entry pointing the cleanup at a future task) is
non-compliant in the same way skipping any other mandatory ACT step is
non-compliant (§9.1).

This rule does not apply to CI runners — they are ephemeral and discard their
filesystem at job end. It applies to local/agent ACT sessions only, where state
persists across many sessions over the life of the project.

---

## 6. Dependency Version Resolution

Use the project's MCP tools (`rust-docs`, `npm-search`, or equivalent) to look up the
current stable version of any new dependency before writing any version string.

| Rule | Requirement |
|:-----|:------------|
| 6.1 | In PLAN sessions: verify every dependency named in the task context before writing the plan. |
| 6.2 | In ACT sessions: query before writing or accepting any dependency version, including versions already written in the approved plan. **ACT is authoritative over PLAN on version numbers.** If the MCP result differs from the plan, use the MCP result. Record every lookup in `## Resolved Dependencies`. |
| 6.3 | Do NOT introduce a dependency not already declared in the project's dependency manifests. If a dependency is needed but absent, write a blocker and STOP. |
| 6.4 | If an MCP server is unavailable, fall back to the most recent version in the project's lockfile and document the fallback in `## Resolved Dependencies`. |
| 6.5 | Follow the dependency declaration convention already established in the project's manifests. Do not introduce inline version strings where the project uses workspace dependencies. |

---

## 7. Context Window Management

| Threshold | Action |
|:----------|:-------|
| 50% | Continue normally. No output about context usage. |
| 65% | STOP accumulating new context. Finish the current file or function, run tests, stage with `git add -A`, write a partial implementation report with a `## Continuation` section listing exactly what remains. Update `.forge/state/CURRENT_TASK.md` with `Status=PARTIAL`. STOP — The Forge will resume in a fresh session. |

- Do NOT compress or summarise prior content to extend the session. A clean partial is always preferable to degraded output.
- Do NOT hallucinate file contents or API signatures when context is high. If uncertain about a symbol, re-read the relevant file even at token cost. Wrong assumptions compound.
- The Forge will detect `Status=PARTIAL` and resume the ACT session with the partial report injected as context. Do not attempt to detect or handle resumption yourself — the injected header will say `RESUME SESSION`.

---

## 8. Output Structure Discipline

Report structure is fixed regardless of task complexity. Never abbreviate or drop sections.

**Patterns to avoid:**
- Omitting `## Files Changed` because `## Commit Log` is present (or vice versa) — both are always required.
- Writing a prose summary instead of the required header table.
- Collapsing `### In Scope` / `### Out of Scope` into a single paragraph.
- Skipping `## Risks and Mitigations` with "no risks identified" — write the table with at least one row; if genuinely none apply, write `Risk="None identified"`, `Mitigation="n/a"`.
- Writing `## Test Results` as a summary sentence rather than verbatim test runner output.

**Write method — bash heredoc only.** Always write plan and implementation reports using
a bash heredoc with a single-quoted delimiter. Never use a write tool for report files.
The write tool corrupts technical identifiers (hex values, CamelCase names, numeric
suffixes like `bf16`/`fp16`, section signs `§`) in long strings:

```bash
cat << 'ENDPLAN' > .forge/reports/<TASK_ID>_plan.md
# Plan Report: <TASK_ID>
...complete content...
ENDPLAN
```

**Single write rule:** Write the complete finished document in one heredoc. Do not write
interim notes, progress updates, or partial drafts to the report file. The report must
not exist until it is complete and ready.

**Correction exception:** if after writing you verify the file contains corrupted content,
a single corrective overwrite is permitted using the same heredoc method. No more than
two writes total per file per session. If corruption persists after two attempts, set
`Status=BLOCKED` and STOP.

A report that does not begin with `# Plan Report: <TASK_ID>` or
`# Implementation Report: <TASK_ID>` is malformed and constitutes a session failure.

**Pre-Stop Verification (use exactly these three commands):**
```bash
# For plan reports:
head -1 .forge/reports/<TASK_ID>_plan.md        # must print: # Plan Report: <TASK_ID>
grep "^## " .forge/reports/<TASK_ID>_plan.md    # must show 12 section headings
wc -l .forge/reports/<TASK_ID>_plan.md          # must be > 40 lines

# For implementation reports:
head -1 .forge/reports/<TASK_ID>_implement.md        # must print: # Implementation Report: <TASK_ID>
grep "^## " .forge/reports/<TASK_ID>_implement.md    # must show 11 section headings
wc -l .forge/reports/<TASK_ID>_implement.md          # must be > 40 lines
```
The exact required sections for each report type are defined in §16 and §17 below.

---

## 9. Error Handling and Stopping

| Rule | Requirement |
|:-----|:------------|
| 9.1 | If an unrecoverable error is encountered: (a) write a `## Blockers` section to the in-progress report; (b) update `.forge/state/CURRENT_TASK.md` with `Status=BLOCKED`; (c) STOP immediately. Do not guess, retry indefinitely, or continue with an unsanctioned workaround. |
| 9.2 | Build failures within the task's scope (caused by code written in this session) MUST be fixed before writing the report. They are not blockers; they are part of the test-fix loop. |
| 9.3 | **Pre-existing warnings** (present before this task's changes, surfaced by `cargo clippy` or the compiler) MUST be fixed via the most minimal correct solution, even if the affected file is outside the task's original scope. Never document a warning and skip it — a skipped warning persists indefinitely. Fix it, list the file and change under `## Deviations from Plan`, and continue. |
| 9.4 | **Pre-existing errors** in files this task does not otherwise touch are blockers: document under `## Blockers` and STOP. If the error is in a file this task already modifies, fix it as part of the normal test-fix loop (rule 9.2) and note it under `## Deviations from Plan`. |
| 9.5 | **Test failures that pass on retry** must be diagnosed before proceeding — never accepted as flakiness without investigation. (a) Parallelism-induced failures (database locked, port conflict, shared temp file) are deterministic isolation defects, not flakiness. Fix the isolation. `#[serial]` or `--test-threads=1` is only permitted when the shared resource is physically singular; if used, justify it in `## Deviations from Plan`. (b) True flakiness (timing, network) must be documented with root cause identified; the final recorded run must show 0 failures. |
| 9.6 | **Environment-variable test isolation** — any test that calls `std::env::set_var` or `os.environ[...] =` MUST: (1) capture the pre-existing value before mutating; (2) restore every variable unconditionally as the last step of the test body, outside any conditional or assertion block; (3) be fully self-contained — never rely on env state from a prior test; (4) be annotated `#[serial]` (Rust) or placed in a serial pytest group (Python) — capture-and-restore alone does not prevent concurrent test threads from observing the mutated value mid-flight, because `std::env` is process-global and non-atomic. A test that mutates env vars without `#[serial]` is an isolation defect even if it restores correctly. See `docs/ENVIRONMENT.md §11.3` for the required pattern and the `serial_test` crate for the Rust annotation. |
| 9.7 | **`defers_to` code comment marker (ACT sessions).** If the current task's `defers_to` field (`docs/FORGE_TASK_AUTHORING_SPEC.md §4`, `§12a`) is non-empty, every stub, mock implementation, or intentionally-incomplete code path that corresponds to a deferred entry MUST carry a comment at the stub site in the exact form `// defers_to: <TASK_ID> — <short reason>` (or the language's comment syntax, e.g. `# defers_to: <TASK_ID> — ...`), naming the same `<TASK_ID>` that appears in the task's JSON `defers_to` field. This is required even though the JSON field already records the link mechanically — the JSON is only visible to The Forge and to someone reading the task graph; the comment is what a future engineer reading the source file itself will see. A stub written without this comment, when the task's `defers_to` is non-empty, is incomplete — add the comment before writing the implementation report. This is the only point where The Forge's defer tracking and the actual codebase are required to agree in the same artifact a human reads. |
| 9.7a | **Empty `defers_to` forbids stubbing in-scope functionality (ACT sessions) — applies whether or not the approved plan tried to defer it.** When the current task's `defers_to` field is empty or absent, no file this task touches may contain `NotImplementedError`, a `TODO` placeholder, or a mock-only return path standing in for functionality the task's own `context` or the approved plan's `## In Scope` describes. This holds even if the approved plan itself contains an `## Out of Scope` bullet purporting to defer that functionality — an approved plan is not a license to violate this rule; a plan that defers without `defers_to` backing is itself defective (§4.7a), and discovering that defect during ACT is not authorization to execute it anyway. Do not silently work around a defective plan by implementing it as written. Write `## Blockers` identifying the specific bullet and the fact that no corresponding `defers_to` entry exists, set `Status=BLOCKED`, and STOP — the agent has no authority to repair the plan report or the task graph, so surfacing the defect is the only correct action. Marking a task `Status=COMPLETE` while a stub for its own in-scope functionality remains, with no `defers_to` entry to justify it, is a session failure discovered after the fact has the same severity as one caught at the time: it must never happen, regardless of what the plan said. |

---

## 9a. End-of-Phase Deliverable Audit

Before the PLAN session for a phase's final/integration task (the last task
in the phase's `tasks_phase<NNN>.json`, or the task explicitly tagged as
the phase's closing task in `TASKS_PHASE<NNN>.md`) may proceed, the agent
MUST run this audit and resolve every finding.

`docs/FORGE_TASK_AUTHORING_SPEC.md §5` already guarantees that every
`defers_to` entry anywhere in the phase resolved to a real, downstream task
at startup — that part needs no re-checking here. This audit re-checks the
one thing startup validation cannot: whether each `defers_to` target's own
wording, and the actual source tree, still genuinely match what was
claimed when the phase was authored.

**Procedure, run in order, for every task in the current phase whose
`defers_to` field is non-empty:**

1. For each such task `<TASK_ID>`, read its `defers_to` list from
   `tasks_phase<NNN>.json`.
2. For each target `<OWNER_ID>` in that list:
   a. Confirm `<OWNER_ID>` is marked `completed` in `.forge/state/state.json`,
      or is itself still pending with its own valid plan to deliver the
      scope. If `<OWNER_ID>` is `completed`, read its implementation
      report (`.forge/reports/<OWNER_ID>_implement.md`) and confirm the
      deferred scope actually appears delivered there (in `## Files
      Changed` or `## Public API Delta`) — not merely that the task ran.
   b. Run, against `<TASK_ID>`'s own source files listed in its
      implementation report's `## Files Changed`:
      ```
      grep -rn "defers_to: <OWNER_ID>" <those files>
      ```
      Confirm the §9.7 comment marker is present at the stub site and
      names the same `<OWNER_ID>` recorded in the JSON `defers_to` field.
      A mismatch (JSON says one task, code comment says another or is
      absent) is a finding.
3. For every finding from 2a or 2b — the owner doesn't deliver the scope,
   or the code comment doesn't match the JSON — the phase is **not**
   closed. This is the same situation `FORGE_AGENT_RULES.md §4.7` describes
   for a PLAN session encountering a bad `defers_to` entry while planning:
   the agent has no authority to repair `tasks_phase<NNN>.json`. Write a
   blocker under `## Blockers` in the final/integration task's plan report
   identifying exactly which `<TASK_ID>` → `<OWNER_ID>` link failed and
   why, set `Status=BLOCKED`, and STOP. A human must author the missing
   coverage or correct the task graph before the phase can close.
4. If every `defers_to` entry in the phase passes both checks, record the
   full `<TASK_ID>` → `<OWNER_ID>` list and a one-line confirmation per
   entry in the final/integration task's plan report, under a
   `## Phase Deliverable Audit` subsection of `## Approach`.

A phase whose final/integration task is planned without this audit having
been run and recorded is non-compliant with this section.

### 9a.1 Unmarked-stub sweep (catches what the procedure above cannot)

The procedure above only examines tasks whose `defers_to` field is
non-empty. It cannot detect a task that left functionality unimplemented
**without ever populating `defers_to`** — exactly the incident `§4.7a`/
`§9.7a` and `FORGE_TASK_AUTHORING_SPEC.md §12a` exist to prevent. Because
this incident has occurred before and is the documented motivation for
those rules, this sweep is mandatory and mechanical, run in addition to
the procedure above, not instead of it:

1. Run, across every source file modified by any task in the current
   phase (per each task's implementation report `## Files Changed`):
   ```
   grep -rn "NotImplementedError\|unimplemented!\|todo!\|# TODO\|// TODO" <those files>
   ```
2. For every match, identify which task's `## Files Changed` introduced
   that line, then check that task's own `defers_to` field in
   `tasks_phase<NNN>.json`.
3. If `defers_to` is non-empty and the stub site carries the matching
   `defers_to: <TASK_ID>` comment (§9.7), this match is accounted for —
   it was already covered by the procedure above. No finding.
4. If `defers_to` is empty or absent, **or** the stub site has no
   `defers_to` comment naming an entry in that field, this is a finding
   regardless of what the task's plan or implementation report claims in
   prose. A prose justification ("deferred to a future phase", "stubbed
   pending confirmation") is not a substitute for the field — that is the
   literal defect this sweep exists to catch.
5. Any finding from step 4 means the phase is **not** closed, for the same
   reason as step 3 above: write a blocker in the final/integration task's
   plan report naming the exact file, line, and originating `<TASK_ID>`,
   set `Status=BLOCKED`, and STOP. A human must decide whether to author a
   `defers_to`-backed task to receive the scope or revert the stub and
   require the originating task to be redone in full.
6. If the sweep finds nothing, record `"Unmarked-stub sweep: 0 findings"`
   in the same `## Phase Deliverable Audit` subsection as the procedure
   above, including the exact grep command run and its output (even if
   empty) — not a prose claim that the sweep was performed.

### 9a.2 Dual-mode parity-marker sweep (only where the project defines markers — see §5.13)

If the project's `docs/<PROJECT>_DESIGN.md` defines a dual-mode test parity
marker convention (§5.13) — e.g. AnvilML's `REAL_PATH_VERIFIED`/
`MOCK_PATH_VERIFIED` pair (`ANVILML_DESIGN.md §10.6`) — this sweep is
mandatory at every phase close, for the same reason §9a.1 is mandatory: a
function that silently lacks its real-mode (or mock-mode) counterpart test
is structurally the same defect class as an unmarked stub — an entire code
path missing, with nothing to grep for unless the marker convention itself
is checked. If the project defines no such convention, this section does
not apply; do not invent markers to satisfy it.

1. Identify the marker pair and the function categories they apply to from
   the project's design doc (e.g. `ANVILML_DESIGN.md §10.4`'s fixed method
   names: `execute()`, `load()`, `sample()`, `decode()`,
   `compute_latent_shape()`).
2. Run, across every source file modified by any task in the current phase
   that defines a function in one of those categories:
   ```
   grep -L "REAL_PATH_VERIFIED:" <those files>
   grep -L "MOCK_PATH_VERIFIED:" <those files>
   ```
   (`grep -L` lists files that do **not** contain the pattern — the files
   missing a marker, not the files that have one.)
3. For every file returned by either command, confirm whether it actually
   defines a function in scope of the convention (a file that only
   re-exports, or a test helper file, is not a finding even if it lacks a
   marker — the convention applies to the function definitions, not every
   file in the directory).
4. For every marker that IS present, confirm the named test actually exists
   and is collectible (e.g. `pytest --collect-only "<named test>"` exits 0)
   and is not skipped, `xfail`-marked, or gated behind the mode it claims
   to verify (a `MOCK_PATH_VERIFIED` marker naming a test that requires the
   real backend is a finding, and vice versa).
5. Any finding from step 3 or step 4 means the phase is **not** closed: write
   a blocker in the final/integration task's plan report naming the exact
   file, function, and which marker is missing, stale, or pointing at a
   non-collectible test; set `Status=BLOCKED`; STOP. A human must decide
   whether to author a task to add the missing test/marker or revert the
   change requiring the originating task to be redone in full.
6. If the sweep finds nothing, record
   `"Dual-mode parity-marker sweep: 0 findings"` in the same
   `## Phase Deliverable Audit` subsection as §9a.1, including the exact
   grep commands run and their output (even if empty).

---

## 10. File and Path Conventions

| Convention | Detail |
|:-----------|:-------|
| Report files | `.forge/reports/<TASK_ID>_plan.md` (PLAN session); `.forge/reports/<TASK_ID>_implement.md` (ACT session). Committed by The Forge. |
| State file | `.forge/state/CURRENT_TASK.md` — update at end of every session. Format: `Task: <ID>`, `Step: <PLAN|IMPLEMENT>`, `Status: <COMPLETE|PARTIAL|BLOCKED>`, `Updated: <ISO 8601 UTC>` |
| Phase task docs | `docs/TASKS_PHASE<NNN>.md` — read; do not modify. |
| Task JSON files | `.forge/tasks/tasks_phase<NNN>.json` — read; do not modify. |
| Project scope | The task's `project` field names the single repository. Do NOT read or write files outside that repository. |
| Root files | Do not create files at the repository root unless explicitly listed in the plan's "Files Affected" table. |

---

## 11. Logging Standards

Logging is **mandatory** — not optional, not deferred. Every task that adds or modifies
code must include appropriate logging before the task is marked complete. The specific
mandatory log points for this project are defined in `docs/ENVIRONMENT.md §9`.

### 11.1 General instrumentation obligation

Every function or code path **added or modified** by a task must be assessed for
observability. For each non-trivial code path, ask:

1. **Would an operator need to know this ran?** If yes → INFO (lifecycle) or DEBUG (routine).
2. **Would an operator need to know what it decided?** If a branch is taken, a value is
   selected, or a fallback is used → DEBUG with the relevant fields.
3. **Would an operator need to know why it failed or was skipped?** If work is discarded,
   retried, or falls back silently → at minimum WARN with context.

Code that silently succeeds or silently discards work without any log call is a defect
unless the function is a pure data transformation with no side effects and no decision
points (e.g. a type conversion or a sort).

When in doubt: instrument at DEBUG. A DEBUG call costs nothing at the default INFO level
and is invaluable during diagnosis.

### 11.2 Level assignment

See `docs/ENVIRONMENT.md §9` for the level table and field discipline.

### 11.3 Mandatory INFO log points

See `docs/ENVIRONMENT.md §9` for the complete list. A task is not complete if a mandatory
INFO log point is absent in a subsystem the task touches.

### 11.4 WARN field discipline

Include `error=` in a WARN message **only** when it adds information beyond what the other
structured fields already convey. A "not found" error on a `path=` field that already names
the missing file is redundant — omit `error=`. An unexpected OS error is not redundant —
include `error=`.

### 11.5 Mandatory DEBUG log points

See `docs/ENVIRONMENT.md §9` for the complete list.

### 11.6 Instrumentation

- Apply `#[tracing::instrument]` (Rust) to async functions representing a meaningful unit
  of work: migration runner, seed loader, worker spawn, job dispatch, model scan.
- Span names must be lowercase `snake_case` matching the function or subsystem name.
- Do not instrument tight inner loops or per-packet/per-frame functions.
- Span fields must use structured notation: `tracing::info!(addr = %addr, "listening")`
  not `tracing::info!("listening on {addr}")`.

### 11.7 Plan and report obligations

**PLAN sessions:** if a task adds, modifies, or touches a subsystem listed in the
mandatory INFO or DEBUG log point tables in `docs/ENVIRONMENT.md §9`, the plan's Approach
section must explicitly list the log calls to be added or verified. Do not leave logging
as an implicit side effect.

**ACT sessions:** after implementing, scan every file changed by this task for missing
mandatory log points. Add any that are absent. Record them in `## Files Changed`. Do not
mark a task COMPLETE if a mandatory INFO log point is absent in a subsystem the task touches.

---

## 12. Inline Documentation Standards

Inline documentation is **mandatory** and is not an optional quality enhancement. A task
that introduces or modifies code without meeting these standards is incomplete. This section
defines what is required; `docs/ENVIRONMENT.md §10` provides language-specific examples.

### 12.1 Public API documentation (Rust `///`, Python docstring)

**Every `pub` item** in Rust (`pub fn`, `pub struct`, `pub enum`, `pub trait`, `pub const`,
`pub type`, `pub mod`) must have a `///` doc comment describing:
- What it *does*.
- Any non-obvious preconditions or postconditions.
- For `fn`: what each argument represents and what is returned (or what error variants
  may be returned).

**Every Python class and non-trivial function** must have a Google-style docstring with
at minimum a one-sentence summary. Functions that take arguments, return values, or raise
exceptions must include the corresponding `Args:`, `Returns:`, and `Raises:` sections.

Missing doc comments on public items are treated the same as missing mandatory log calls:
the task is not complete.

### 12.2 Inline decision-point comments (Rust `//`, Python `#`)

**Every non-trivial decision point** in function bodies — whether in new code or in code
being modified by this task — must have an inline comment explaining *why* the branch was
taken, the value was chosen, or the fallback was used.

"Non-trivial" is defined as: anything that would not be immediately obvious to a competent
developer familiar with the language but **unfamiliar with this codebase** reading the code
for the first time. When in doubt, comment.

**Decision points that always require an inline comment:**
- A guard condition preventing an edge case (explain the edge case).
- A fallback path (explain why primary failed and what fallback does).
- A `#[cfg(...)]` or platform-specific branch (explain the constraint being guarded).
- A magic number or constant that is not self-explanatory (explain origin or meaning).
- A `#[allow(...)]` suppression (explain why it is legitimate at this site).
- Any `unsafe` block (explain the invariant being upheld).
- A ZeroMQ, tokio, or concurrency primitive configuration setting (explain the behaviour).
- A non-obvious algorithmic choice (explain why this algorithm over the obvious alternative).

### 12.3 `lib.rs` discipline

Every crate's `lib.rs` must begin with a `//!` crate-level doc comment describing what
the crate owns and its hard constraints (e.g. "Zero I/O. Zero async."). It must contain
only `pub mod`, `pub use`, and the crate-level doc comment. No implementation code in
`lib.rs`. The file must not exceed 80 lines.

### 12.4 Plan and report obligations

**PLAN sessions:** if the task adds or modifies any source file, the plan's Approach
section must explicitly note the documentation and inline comments that will be added.
This is not optional boilerplate — it is evidence that the agent has thought about
what needs to be explained and why.

**ACT sessions:** after implementing, scan every file changed by this task for:
- Missing `///` doc comments on any new `pub` item.
- Missing `#` or `//` inline comments at any new decision point.
- `lib.rs` files that now contain implementation code.

Add any missing documentation before marking the task COMPLETE. Record the additions in
`## Files Changed` — not as a separate section, but as part of the normal file change log.

---

## 13. File Size Guidelines

These thresholds trigger a **mandatory review and justification**, not an automatic split.
When a file reaches its threshold the agent must stop and ask: *does this file own one
coherent concern, or has it grown because unrelated logic was added?*

- If the answer is **mixed concerns** → split. Extract the unrelated logic to a sibling
  module with a name that describes what it owns. Record the split under `## Files Changed`.
- If the answer is **one coherent concern** that genuinely requires this much code (a
  complex state machine, a comprehensive protocol codec, a large but unified test suite)
  → keep it whole. Document the justification in `## Deviations from Plan` with a one-
  sentence explanation of why the cohesion argument outweighs the size concern.

Splitting purely to hit a threshold produces worse architecture than a coherent large file.
The goal is cohesion and readability, not line count minimisation.

| File type | Review threshold | Common split signal |
|:----------|:----------------|:--------------------|
| Rust source (`.rs`) | 400 lines | Mixes data types, business logic, and utility functions |
| Python source (`.py`) | 350 lines | Mixes I/O, computation, and configuration |
| Test files (any language) | 500 lines | Tests covering more than one logical unit of behaviour |
| `lib.rs` in any crate | 80 lines | Contains any implementation code (never appropriate) |

**`lib.rs` is the one absolute rule:** it must contain only `pub mod`, `pub use`, and the
crate-level `//!` doc comment. Implementation code in `lib.rs` is never appropriate
regardless of how small it is. The 80-line threshold exists because legitimate `lib.rs`
content never approaches it — reaching it is always a structural error.

**`#[cfg(test)]` inline blocks** in Rust source files are discouraged except for trivial
unit tests of a single pure function (≤ 20 lines, no test helpers, no I/O). Prefer
`crates/{name}/tests/` for all but the most trivial cases. If an inline test block is
kept, document the reason in `## Deviations from Plan`. Python tests always go in
`worker/tests/`.

---

## 14. Crate Version Bumping

Every task that modifies source files inside a crate must increment that crate's patch
version (`Z` in `X.Y.Z`) before staging. Only `Z` changes. The workspace release version
(`[workspace.package] version` in the root `Cargo.toml`) is **read-only** — never modify
it in a task.

See `docs/ENVIRONMENT.md §12` for the exact procedure, manifest locations, and bump command.

**Plan obligations:** for every crate listed in `## Files Affected` whose source files
will be modified, include a row in the Files Affected table:
```
| Modify | crates/<name>/Cargo.toml | Bump patch version X.Y.Z → X.Y.(Z+1) |
```

**ACT obligations:** verify the version was bumped for every modified crate before staging.
Record each bump in `## Files Changed`. Do not mark a task COMPLETE if a crate's source
files were modified but its version was not bumped.

---

## 15. Prohibited Behaviours

Unconditional prohibitions regardless of task context or instruction:

- No `git push`, `git push --force`, or any remote write operation.
- No modifications to `forge.py`, `state.json`, or any file under `.forge/tasks/`.
- No modifications to files outside the single project repo named in the task's `project` field.
- No use of environment variables, secrets, or API keys not already present in the repository's documented configuration.
- No network calls to external services except via configured MCP tools.
- No interactive prompts — all tool invocations must be non-interactive (`-y`, `--yes`, `--non-interactive` flags where applicable).
- No spawning of background processes or daemons that outlive the session.
- No modifications to `.env` files or secrets unless the task explicitly lists the specific change in "Files Affected".
- No `#[ignore]` attributes on tests in committed code. A test that cannot pass is fixed or deleted.
- No `#[allow(dead_code)]`, `#[allow(unused_imports)]`, or similar suppression annotations without an inline comment explaining exactly why the suppression is legitimate at this specific site.

---

## 16. Plan Report Format

**Authoritative source:** `agents/forge-plan.md`. This section reproduces the format for
convenient reference during a session. If any detail conflicts with `agents/forge-plan.md`,
the agent file takes precedence.

Output path: `.forge/reports/<TASK_ID>_plan.md`

Every section is mandatory. Use exactly these 11 headings in this order. `grep "^## "` on
the finished report must return exactly 12 lines.

```markdown
# Plan Report: <TASK_ID>

| Field       | Value                        |
|-------------|------------------------------|
| Task ID     | <TASK_ID>                    |
| Phase       | <NNN> — <Phase Name>         |
| Description | <task description>           |
| Depends on  | <TASK_ID> or "none"          |
| Project     | <project name>               |
| Planned at  | <ISO 8601 UTC timestamp>     |
| Attempt     | 1                            |

## Objective

<One paragraph: what this task produces, why it is needed at this point in the build
sequence, and the observable state of the system when the task completes — a curl command
that now works, a test that now passes, a log line that now appears.>

## Scope

### In Scope

<Explicit list of files to be created or modified, and logic to be implemented.
Name the specific functions, structs, and traits — not just the file.>

### Out of Scope

<Explicit list of what this task intentionally does NOT do. If a stub will be completed
by a future task, say so here.>

## Existing Codebase Assessment

<One to three paragraphs summarising what was found during the codebase inspection:
(a) what already exists that this task builds on;
(b) the established patterns (naming, error handling, test style, logging) to follow;
(c) any gap between the design doc and current source that affects the approach.
If no prior source exists (Phase 000/001): "No prior source exists. This task establishes
the baseline patterns for subsequent phases.">

## Resolved Dependencies

<One row per external crate or package this task introduces or references by name.
Every row must be resolved via MCP — not recalled from training data.
If no new dependencies: write "None." Do not omit the section heading.>

| Type   | Name    | Version verified | MCP source     | Feature flags confirmed |
|--------|---------|-----------------|----------------|------------------------|
| crate  | zeromq  | 0.6.1           | rust-docs MCP  | tokio                  |
| python | pyzmq   | 26.2.0          | pypi-query MCP | n/a                    |

If the MCP result differs from the task context or design doc, record both versions and
add a note: "Task context specified X.Y.Z — overridden by MCP result."

## Approach

<Step-by-step implementation plan. Each step must be specific enough that a programmer can
execute it without making architectural decisions. For each step that introduces a non-obvious
implementation choice, include one sentence of inline rationale.
For each file to be created or modified:
- The specific functions, types, or traits to be written, with their full signatures.
- Any external API names must have been confirmed via MCP before appearing here.
  Do not write type names or method names from training-data memory.
- The log calls required (see §11.7).
- The doc comments and decision-point inline comments required (see §12).
- The tests to be written.>

## Public API Surface

<Table or code block showing every new pub item — full function signatures, struct
definitions, trait impls, Python class/function signatures — with crate or module path.
The ACT agent verifies this table before staging. If no new public items: "None.">

## Files Affected

| Action | Path | Description |
|--------|------|-------------|
| CREATE / MODIFY | <path> | <one-line description> |
| Modify | <crate>/Cargo.toml | Bump patch version X.Y.Z → X.Y.(Z+1) |

## Tests

<One row per test introduced by this task. The ACT agent uses this table as the source
of truth when writing `docs/TESTS.md` entries — populate all columns fully while the
test's purpose is freshest. The ACT agent must not need to reconstruct this information
from the source code.>

| Test File | Test Name | What It Verifies | Preconditions | Inputs | Expected Output | Acceptance Command |
|-----------|-----------|-----------------|---------------|--------|----------------|--------------------|
| <path> | <fn_name or test_id> | <one sentence> | <setup state required> | <inputs or env vars> | <expected result or side effect> | <runnable command> exits 0 |

## CI Impact

<State whether any CI job behaviour changes. If a new file type, gate, or test module is
added, explain which CI job picks it up. If no CI changes: "No CI changes required.">

## Platform Considerations

<State any platform-specific behaviour this task introduces. Name any #[cfg(unix)] /
#[cfg(windows)] guards required. If platform-neutral: "None identified. The Windows
cross-check in ENVIRONMENT.md §7 is sufficient.">

## Risks and Mitigations

<Minimum two rows. Risk rows must name a specific failure condition — not a general
category. "None identified" is only acceptable for pure documentation tasks where
codebase inspection found no gaps or inconsistencies.>

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| <specific condition> | Low/Med/High | Low/Med/High | <specific mitigation> |

## Acceptance Criteria

<Runnable shell commands only. Vague criteria ("works correctly") are not permitted.>

- [ ] <command> exits 0
```

## 17. Implementation Report Format

**Authoritative source:** `agents/forge-act.md`. This section reproduces the format for
convenient reference during a session. If any detail conflicts with `agents/forge-act.md`,
the agent file takes precedence.

Output path: `.forge/reports/<TASK_ID>_implement.md`

Every section is mandatory. Use exactly these 11 headings in this order. `grep "^## "` on
the finished report must return exactly 11 lines.

```markdown
# Implementation Report: <TASK_ID>

| Field         | Value                              |
|---------------|------------------------------------|
| Task ID       | <TASK_ID>                          |
| Phase         | <NNN> — <Phase Name>               |
| Description   | <task description>                 |
| Implemented   | <ISO 8601 UTC timestamp>           |
| Status        | COMPLETE | PARTIAL | BLOCKED       |

## Summary

<One paragraph summarising what was implemented and the final state.>

## Resolved Dependencies

<One row per new dependency added or updated. Every row must reflect a live MCP lookup —
not a version recalled from training data. If no new dependencies: write "None."
Do not omit the section heading.>

| Type   | Name    | Version resolved | Source        |
|--------|---------|-----------------|---------------|
| crate  | zeromq  | 0.6.1           | rust-docs MCP |

If the MCP result differs from the approved plan's version, record both and note:
"Plan specified X.Y.Z — overridden by MCP result per version floor rule."

## Files Changed

| Action | Path | Description |
|--------|------|-------------|

## Commit Log

<verbatim output of git diff --stat>

## Test Results

<verbatim test runner output — not a prose summary>

## Format Gate

<verbatim output of the formatter run in check-only mode (pass 2), or
"Not applicable — task wrote no source files">

## Platform Cross-Check

<verbatim output of all cross-check commands from docs/ENVIRONMENT.md §7>

## Project Gates

<verbatim output for each applicable gate from docs/ENVIRONMENT.md §8, or
"None applicable — task does not touch config fields, handler signatures, or node types.">

## Public API Delta

<Output of: git diff HEAD -- <modified_files> | grep "^+.*pub " | head -40
List every new pub item introduced — name, type (fn/struct/trait/enum), module path.
If the grep returned nothing: "No new pub items introduced.">

## Deviations from Plan

<Bulleted list of any deviations from the approved plan's In Scope, Files Affected, or
Public API Surface sections. If a deviation changes a type or signature other tasks depend
on, flag it explicitly. "None." if no deviations.>

## Blockers

<"None." or description of unresolved issues, including MCP unavailability.>
```

---

## 18. Phase Numbering Reference

Phase numbers are zero-padded to three digits in filenames (`001`, `002`, …) and displayed
as plain integers in task IDs (`P1-A3`, not `P001-A3`). The canonical mapping is in
`docs/PHASES.md` — read it; do not rely on this file's examples.

Retrofit phases (`9xx`) are inserted between primary phases when a gap is identified in
committed code. They are self-documenting: each has its own `tasks_phase9NN.json` and
`TASKS_PHASE9NN.md`. `PHASES.md` is not updated when a retrofit phase is added.