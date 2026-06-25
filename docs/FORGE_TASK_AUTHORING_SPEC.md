# The Forge — Task Authoring Specification

**Document:** `FORGE_TASK_AUTHORING_SPEC.md`  
**Applies to:** `.forge/tasks/tasks_phase<NNN>.json`, `docs/TASKS_PHASE<NNN>.md`  
**Audience:** Human authors and LLMs generating task content for The Forge orchestrator

---

## Table of Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [Concepts and Definitions](#2-concepts-and-definitions)
3. [Task JSON — Format Specification](#3-task-json--format-specification)
4. [Task JSON — Field Reference](#4-task-json--field-reference)
5. [Task JSON — Validation Rules](#5-task-json--validation-rules)
6. [Task JSON — ID and Phase Numbering](#6-task-json--id-and-phase-numbering)
7. [TASKS_PHASE Document — Purpose and Location](#7-tasks_phase-document--purpose-and-location)
8. [TASKS_PHASE Document — Format Specification](#8-tasks_phase-document--format-specification)
   - [8a. Phase Acceptance Criteria — Worked Pattern Reference](#8a-phase-acceptance-criteria--worked-pattern-reference)
9. [TASKS_PHASE Document — Section Reference](#9-tasks_phase-document--section-reference)
   - [9a. docs/RUNNABLE_PROOF.md — the project-wide proof summary](#9a-docsrunnable_proofmd--the-project-wide-proof-summary)
10. [Task Sizing Rules](#10-task-sizing-rules)
11. [Context Field Writing Guide](#11-context-field-writing-guide)
12. [Dependency (prereqs) Design Guide](#12-dependency-prereqs-design-guide)
13. [Tag Reference](#13-tag-reference)
14. [LLM Generation Prompt Template](#14-llm-generation-prompt-template)
15. [Complete Worked Example (Illustrative Only)](#15-complete-worked-example-illustrative-only)

---

## 1. Purpose and Scope

The Forge orchestrator drives OpenCode through a plan → approve → implement → approve → commit/push cycle, one atomic task at a time. It reads task definitions from two sources:

- **`tasks_phase<NNN>.json`** — per-phase machine-readable task arrays, stored inside each project repo at `.forge/tasks/`. These are the authoritative sources for task execution.
- **`docs/TASKS_PHASE<NNN>.md`** — a human-readable narrative document for each phase, read by OpenCode at the start of every session to understand the broader context of the task it is executing.

These two sources must stay in sync. Every task that appears in `tasks_phase<NNN>.json` for a given phase must have a corresponding entry in the matching `TASKS_PHASE<NNN>.md`. They are authored together and treated as a pair.

This document specifies the exact format, field semantics, validation rules, and quality standards for both. It is written so that an LLM can use it as a complete prompt context to generate correct task sets without further clarification.

---

## 2. Concepts and Definitions

**Task** — the smallest unit of work The Forge can execute. One OpenCode PLAN session followed by one OpenCode ACT session. Must be completable within a single 120-minute OpenCode session.

**Phase** — a named group of tasks that together achieve a major milestone (e.g. "Core Domain Types" or "Scheduler"). Phases are numbered 001–899 for primary development phases. Phase numbers are sequential but not necessarily contiguous within the 001–899 range. Phases 900–999 are reserved for retrofit, correction, and adjustment phases (see §6). A phase has one `tasks_phase<NNN>.json` per project and one `TASKS_PHASE<NNN>.md` per project.

**Project** — one of the repositories registered in the orchestrator's `repos.json` for the current deployment. The set of valid project names is whatever `repos.json` lists — it is not fixed by this document and varies per deployment (a single-repo project might register only one name; a multi-repo project might register several). Each task targets exactly one project. Cross-project work must be split into separate tasks, one per project.

**DAG** — the task dependency graph. `prereqs` references create directed edges. The Forge resolves execution order by topological sort within and across phase files. Cycles are a fatal error.

**Atomic task** — a task whose implementation fits within one OpenCode ACT session (≤120 min), produces a self-contained, testable increment, and does not leave the codebase in a broken state if the next task is delayed.

---

## 3. Task JSON — Format Specification

Task definitions live inside each project repository under `.forge/tasks/`. Each phase has its own file. The Forge loads all phase files for the active project at startup and merges them into a single DAG.

```
<project_repo>/
  .forge/
    tasks/
      tasks_phase001.json   ← phase 1 tasks for this project
      tasks_phase002.json   ← phase 2 tasks for this project
      ...
    reports/                ← written by OpenCode during sessions
    state/                  ← CURRENT_TASK.md, state.json
```

Each file is a JSON array of task objects. Files are loaded in phase-number order. Duplicate task IDs across files are a fatal error.

### Top-level structure

```json
[
  { /* task object */ },
  { /* task object */ }
]
```

The array is ordered. The Forge respects the order when multiple tasks are simultaneously unblocked (first unblocked task in array order is picked first). Order tasks to respect natural build order within a phase even when the DAG would technically allow any order.

### Task object structure

```json
{
  "id":          "<TASK_ID>",
  "description": "<short description>",
  "phase":       "<NNN>",
  "project":     "<project_name>",
  "prereqs":     ["<TASK_ID>", ...],
  "context":     "<implementation context>",
  "tags":        ["<tag>", ...],
  "defers_to":   ["<TASK_ID>", ...]
}
```

The first six fields are required. `defers_to` is optional — omit it, or set it to
`[]`, when this task defers no scope to a later task. No other additional fields
are permitted. The Forge rejects tasks with unknown fields (to catch the
deprecated `repos` field and similar mistakes).

---

## 4. Task JSON — Field Reference

### `id` — string, required

Unique identifier for the task. Used as the filename base for reports (`<id>_plan.md`, `<id>_implement.md`), as the key in `state.json`, and as the reference in `prereqs` arrays.

**Format:** `P<phase_short>-<group><sequence>`

- `<phase_short>` — phase number without leading zeros (phase `001` → `1`, phase `012` → `12`)
- `<group>` — a single uppercase letter grouping related tasks within a phase (e.g. `A` for core types, `B` for IPC, `C` for frontend)
- `<sequence>` — a positive integer, unique within the group

**Examples:** `P1-A1`, `P1-A2`, `P1-B1`, `P12-C3`

**Rules:**
- Must be globally unique across all `tasks_phase<NNN>.json` files for the project
- Must not contain spaces or special characters other than `-`
- The phase in the ID must match the `phase` field value (short form)

---

### `description` — string, required

A single-line, imperative-mood summary of what the task produces. This text appears in Discord notifications, the DAG status table (`--list`), commit messages, and report headers. It must be self-explanatory without additional context.

**Format:** `<component or subsystem>: <what is produced>`

**Good examples:**
```
"anvilml-core: config types"          ← e.g. for project "anvilml"
"demoproject-registry: SQLite persistence"
"web-ui: artifact gallery component"
"Launcher binary: graceful shutdown"
```

**Bad examples:**
```
"Do the config stuff"           ← vague, no component reference
"Implement everything in core"  ← not atomic
"Fix bug"                       ← not descriptive enough
"demoproject-core"              ← no verb/outcome
```

**Length:** 4–80 characters. Longer descriptions are truncated in commit messages.

---

### `phase` — string, required

The phase number as a string, without leading zeros.

**Valid values:** `"1"`, `"2"`, `"12"`, etc. NOT `"001"` or `"01"`.

The string form is used because JSON has no integer-string distinction in practice, and The Forge uses this value to construct the docs filename: `docs/TASKS_PHASE` + `phase.zfill(3)` + `.md` → `docs/TASKS_PHASE001.md`.

---

### `project` — string, required

The logical name of the single repository this task operates on. Must exactly match a key in `repos.json` for the current deployment.

**Valid values:** whatever keys are present in this deployment's `repos.json`. This document does not hardcode a list — every deployment of The Forge registers its own set of projects (a single-repo deployment registers one key; a multi-repo deployment registers one key per repository). Consult the live `repos.json` for the authoritative set, never this document.

**Rules:**
- Exactly one project per task. If a task naturally spans two projects, split it.
- The project name determines where OpenCode runs (`cwd`), where reports are written (`.forge/reports/` inside that repo), and which repo The Forge commits and pushes.
- A bare `"repos"` field (plural, listing multiple projects) is not supported — use `"project"` with a single string value. See §5 validation rules.

---

### `prereqs` — array of strings, required

Task IDs that must be in the `completed` state before this task becomes eligible for execution. An empty array `[]` means the task is immediately unblocked.

**Rules:**
- Every ID listed must exist in the project's task files (any phase file)
- Must not form a cycle (directly or transitively)
- List only the direct predecessors — do not list transitive dependencies. If A→B→C, task C lists only `["B"]`, not `["A", "B"]`
- Order within the array does not matter

**Guidance:**
- A task should list a prereq if it reads files that prereq creates, or if it would fail to compile/test without the prereq's output
- Do not add prereqs out of caution — unnecessary prereqs increase the critical path and reduce parallelism

---

### `context` — string, required

The primary implementation instruction for the task. OpenCode reads this field at the start of the PLAN session and uses it as the authoritative specification for what to build. The `context` field is the difference between a task that produces correct output and one that produces plausible-but-wrong output.

See [Section 11](#11-context-field-writing-guide) for full writing guidance. Key rules:

- State exactly which files to create or modify
- Name every function, struct, enum, trait, or component to implement
- Reference the authoritative design document (e.g. `API_CONTRACT.md`, `IPC_PROTOCOL.md`) for any interface that must match exactly
- State the acceptance criterion as a runnable command with a concrete exit condition (e.g. `cargo test -p anvilml-core exits 0 with >=4 tests`)
- Do not exceed 1000 characters. If the task needs more context than that, it is too large and must be split.

---

### `tags` — array of strings, required

Optional hints to The Forge and the model about the nature of the task. Use an empty array `[]` if no tags apply. Do not invent new tag values — use only the tags defined in [Section 13](#13-tag-reference).

---

### `defers_to` — array of strings, optional

Task IDs that will deliver functionality this task is intentionally not
implementing. Use this field — never a bare comment in `context` or prose
under a plan's `## Out of Scope` section — any time a task's `description`
or `context` would otherwise leave named functionality unimplemented with
the expectation that something else covers it. Omit the field, or set it
to `[]`, when the task defers nothing.

**This field exists because the alternative — a free-text deferral
mentioned only in prose — is exactly the defect this field prevents.** A
prose deferral can name a task that doesn't exist, name the wrong task, or
name itself, and nothing will catch it until a human notices much later
(see the worked incident in [§12a](#12a-the-defers_to-field-why-it-exists)).
`defers_to` is mechanical: The Forge validates every entry at startup,
before any agent session runs.

**Rules:**
- Every ID listed must exist in the project's task files (any phase file) — checked automatically at startup, same as `prereqs`.
- Every ID listed must be genuinely downstream of this task — reachable by following `prereqs` forward from this task — checked automatically at startup. A same-subsystem task with no dependency relationship to this task does not satisfy this rule even if it sounds related.
- A task must not list itself.
- This field declares *where* the scope is going, not *that* it is safe to defer. The author is still responsible for confirming, by reading the target task's actual `description` and `context`, that it genuinely claims the deferred scope — see [§12a](#12a-the-defers_to-field-why-it-exists) for the authoring procedure this requires.
- If no task exists yet that can receive the deferral, do not invent one to satisfy this field. Either author that task now (with its own ID, correct `prereqs`, and a `context` that genuinely states the scope) before referencing it, or fold the scope into this task instead.

**What `defers_to` validates and what it cannot:** the startup check in
§5 below confirms the target exists and is structurally downstream — both
syntactic, mechanical facts. It cannot confirm the target's own wording
actually covers the deferred scope; that is a semantic judgment only the
author (at authoring time) or an OpenCode agent re-checking the claim
(at execution time, per `FORGE_AGENT_RULES.md §4.7`) can make.

---

## 5. Task JSON — Validation Rules

The Forge runs these checks at startup (`validate_task_schema` per task, then
`validate_task_graph` once over the full set) and aborts if any fail. An LLM
or human generating tasks must satisfy all of them before The Forge will run.

| Rule | Error message |
|------|---------------|
| Every task has all six required fields | `missing required field '<field>'` |
| `id` is unique across the array | `duplicate task ID: '<id>'` |
| `phase` matches the numeric prefix of `id` | `phase mismatch: id 'P1-A3' but phase '2'` |
| `project` is registered in `repos.json` | `project '<name>' is not registered in repos.json` |
| `"repos"` field is absent | `field 'repos' is no longer supported. Rename it to 'project'` |
| Every `prereqs` entry exists as a task ID | `prereq '<id>' in task '<id>' does not exist` |
| The prereq graph has no cycles | `cycle detected involving tasks: <list>` |
| `description` is non-empty | `field 'description' must be a non-empty string` |
| `context` is non-empty | `field 'context' must be a non-empty string` |
| Every `defers_to` entry exists as a task ID | `defers_to target '<id>' in task '<id>' does not exist` |
| A task does not list itself in `defers_to` | `task '<id>' lists itself in defers_to — a task cannot defer to itself` |
| Every `defers_to` entry is downstream of the deferring task in the prereq graph | `defers_to target '<id>' in task '<id>' is not downstream of '<id>' in the prereq graph` |

**What this table validates, and what it cannot.** Every row above is a
syntactic or structural property of the task graph — field presence, ID
existence, graph shape — and is fully automated: `forge.py` checks it at
startup and on every hot-reload of the task files, before any OpenCode
session runs. **No row checks whether a `defers_to` target's own wording
actually claims the deferred scope.** That is a semantic judgment about
what a task's `description`/`context` text means, not a property a
startup-time check can verify. Confirming it is a mandatory **manual**
obligation at two points: the task author, before writing `defers_to`
(see [§12a](#12a-the-defers_to-field-why-it-exists)), and the OpenCode
PLAN agent, before relying on an existing `defers_to` entry while planning
a task that reads one (`FORGE_AGENT_RULES.md §4.7`). Do not treat the
presence of these rows as license to skip that manual check — a
`defers_to` entry that passes every rule in this table can still be a lie
if the target task's content does not actually cover the scope.

---

## 6. Task JSON — ID and Phase Numbering

### Phase number assignment

Phases are assigned sequentially starting at 1. They represent major development milestones. Each phase builds on the previous. Phase numbers are chosen before task authoring begins and documented in `docs/PHASES.md`.

**Primary phases: 001–899.** Normal development phases. Assigned sequentially; gaps are permitted.

**Retrofit phases: 900–999.** Reserved exclusively for retrofit, correction, and adjustment work that must be inserted between already-executed primary phases without renumbering them. Retrofit phases are never part of the original plan — they are authored on demand when a gap in the committed codebase is identified (e.g. a rule added after earlier phases ran, a production bug requiring correction before the next phase begins). The filename satisfies the loader's `\d{3}` pattern and the task ID prefix reflects the numeric phase value (e.g. phase `900` → IDs `P900-A1`, `P900-A2`). Execution order is determined entirely by `prereqs`, not by the phase number, so a phase `900` file is picked up correctly relative to any primary phase as long as its prereqs and the prereqs of the tasks that depend on it are set correctly. When authoring a retrofit phase, identify every task in subsequent primary phases whose prereq chain must be updated to route through the new retrofit tasks, and update those prereq fields.

Example mapping (illustrative only — substitute this project's actual phase names and milestones):

| Phase | Name | Description |
|-------|------|-------------|
| 001 | Repository Scaffold | Repo structure, CI skeleton, package/crate stubs |
| 002 | Core Domain Types | Config, domain types, IPC messages |
| 003 | Hardware Detection | Backend-specific device detection, mock detector |
| 004 | Worker Management | Worker pool, IPC bridge, env injection |
| ... | ... | ... |
| 900 | Logging Retrofit | Retrofit §11 logging to phases 000–008 before phase 009 begins |
| 901–999 | (reserved) | Future retrofit phases as needed |

### Group letter assignment within a phase

Within a phase, tasks are grouped by subsystem using uppercase letters. There is no fixed mapping of letters to subsystems — assign letters to maintain logical grouping within each phase. Document the mapping in the phase's `TASKS_PHASE<NNN>.md`.

Example for a hypothetical phase 1 in a project with a Rust backend, a Python worker, and a TypeScript frontend (substitute this project's actual subsystem names):
- `A` — core domain types
- `B` — persistence/registry
- `C` — Python worker
- `D` — worker pool/IPC bridge
- `E` — scheduler
- `F` — HTTP/WS server
- `G` — OpenAPI generation
- `H` — Launcher binary
- `I` — frontend UI
- `J` — Integration

### Sequence numbers within a group

Start at 1. Increment by 1. Gaps are allowed if tasks are removed but not recommended — they suggest instability in the task set.

---

## 7. TASKS_PHASE Document — Purpose and Location

### Purpose

The `TASKS_PHASE<NNN>.md` document is read by OpenCode at the start of every PLAN and ACT session for tasks belonging to that phase. It provides the narrative context that `tasks_phase<NNN>.json` deliberately omits: the architectural rationale, cross-task dependencies explained in prose, interfaces that tasks must conform to, and the overall shape of the phase.

It is NOT a duplicate of `tasks_phase<NNN>.json`. It does not list every field of every task. It provides the context a developer would need to understand why the tasks are structured the way they are, in the order they are.

### Location

The file lives inside the target repository's `docs/` directory, at the path OpenCode will read:

```
<project_repo>/
  docs/
    TASKS_PHASE001.md
    TASKS_PHASE002.md
    ...
    ENVIRONMENT.md
    ARCHITECTURE.md
    API_CONTRACT.md
```

When a phase spans multiple projects (e.g. a phase that includes both a Rust backend project and a TypeScript frontend project), a copy of the `TASKS_PHASE<NNN>.md` must exist in each project's `docs/` directory. The copies may differ in the sections they emphasise — a backend project's copy might cover Rust/Python concerns in depth, while a frontend project's copy covers TypeScript/React concerns.

### Filename

`TASKS_PHASE` + phase number zero-padded to three digits + `.md`

Examples: `TASKS_PHASE001.md`, `TASKS_PHASE012.md`, `TASKS_PHASE099.md`

---

## 8. TASKS_PHASE Document — Format Specification

````markdown
# Tasks: Phase <NNN> — <Phase Name>

**Phase:** <NNN>
**Name:** <Phase Name>
**Project(s):** <comma-separated project names>
**Status:** Draft | Approved | In Progress | Complete
**Depends on phases:** <comma-separated phase numbers, or "none">

---

## Overview

<2–4 paragraphs. What this phase builds. Why it exists at this point in the
sequence. What state the codebase is in at the start of this phase. What state
it will be in at the end. What the next phase depends on from this one.>

---

## Group Reference

<One row per group letter used in this phase's task IDs.>

| Group | Subsystem | Tasks | Summary |
|-------|-----------|-------|---------|
| A     | <name>    | P<N>-A1 … P<N>-A<M> | <one line> |
| B     | <name>    | P<N>-B1 … P<N>-B<M> | <one line> |

---

## Prerequisites

<List what must exist before this phase can begin. Reference the specific
outputs from prior phases that tasks in this phase depend on. Be concrete:
"anvilml-core crate must compile with all domain types from Phase 1" is
better than "Phase 1 must be complete".>

---

## Interfaces and Contracts

<List every external contract this phase's tasks must conform to. For each,
state the document and the specific sections or types that apply.>

| Contract document | Relevant to tasks | What must match |
|-------------------|-------------------|-----------------|
| `API_CONTRACT.md` | P<N>-F2, P<N>-F3  | Response shapes for /v1/jobs, /v1/system |
| `IPC_PROTOCOL.md` | P<N>-A5, P<N>-C1  | All WorkerMessage and WorkerEvent variants |

---

## Task Descriptions

<One subsection per group. For each task: the full task ID, a plain-English
description of what to implement and why, the files to create or modify, and
the acceptance criterion. This is the narrative that OpenCode reads alongside
the context field in tasks_phase<NNN>.json. The four fields below are all
mandatory — see §9 "Task Descriptions" for the per-field minimum content
rules that this template's bracketed hints summarise.>

### Group A — <Subsystem Name>

#### <TASK_ID>: <description>

**Goal:** <1–2 sentences. What this task produces and why it is needed at this point.>

**Files to create or modify:**
- `<path>` — <what it contains or what changes>

**Key implementation notes:**
- <note>
- <note>

**Acceptance criterion:** `<command>` exits 0<, with >=N tests>.

---

<repeat for each task in the group>

---

### Group B — <Subsystem Name>

<same structure>

---

## Phase Acceptance Criteria

<The full set of commands that must exit 0 for the phase to be considered
complete. These are checked manually before moving to the next phase.
Each line is a concrete, runnable command.

This section has two parts, in order:
1. The standard gate commands (cargo test, pytest, clippy, Windows cross-check,
   etc.) — these prove the code compiles and unit/integration tests pass.
2. A Runnable Proof: one or more commands, marked with a `# Runnable Proof
   (manual):` comment, that exercise the phase's new observable capability
   against a live running instance (a bound server, a real subprocess, a
   real file on disk) — not just the test suite. See §9 "Phase Acceptance
   Criteria — mandatory" for the full requirement and the narrow exemptions.
   If the phase produces no new external observable behaviour, write
   `# Runnable Proof: not applicable — <one-sentence reason>` instead of
   omitting the section.>

```
cargo test --workspace --features mock-hardware
cargo clippy --workspace --features mock-hardware -- -D warnings
cargo run -p <openapi-generator-package>
pnpm type-check
pnpm test:run
python -m py_compile $(git ls-files '<worker-dir>/*.py')
<PROJECT>_WORKER_MOCK=1 python -m pytest
# Runnable Proof (manual): <one-line statement of the capability being proven>
cargo run --features mock-hardware &
sleep <N>
curl -s <endpoint> | python3 -c "import sys,json; d=json.load(sys.stdin); assert <condition>"
# -> <expected observable result>
kill %1
```
````

---

## 8a. Phase Acceptance Criteria — Worked Pattern Reference

The standard shape for a Runnable Proof against a live server, used consistently
across this project's phases, is:

```bash
cargo run --features mock-hardware &
sleep <N>                                   # allow startup / worker Ready
curl -s <verb-and-path> | python3 -c "..."  # exercise the capability; assert on the JSON
# -> <one-line comment stating the expected, literal result>
kill %1
```

Adapt the verb (`curl -X POST ...`, `websocat ...`, a `for` polling loop, a
`kill <pid>` to simulate a crash) to what the phase actually delivers, but keep
the three-part shape: start the binary, exercise it, tear it down. Never write a
Runnable Proof step that only re-runs `cargo test` under another name — if the
only way to observe the capability is via the test suite, the phase has no new
external observable behaviour and should use the "not applicable" form from §9
instead of a disguised test invocation.

---

```markdown
## Known Constraints and Gotchas

<Anything that is not obvious from reading the task descriptions and would
cause a session to fail if not known in advance. Examples: ordering
requirements not captured by prereqs, environment variables that must be set,
feature flags that must be passed to cargo, platform-specific behaviour.>

- <constraint>
- <constraint>
```

---

## 9. TASKS_PHASE Document — Section Reference

Each section is mandatory unless marked optional.

### Header block — mandatory

```
# Tasks: Phase <NNN> — <Phase Name>
```

The metadata block beneath must include all five fields. `Status` must be one of: `Draft`, `Approved`, `In Progress`, `Complete`. `Depends on phases` uses short phase numbers (`1, 2`) not zero-padded.

### Overview — mandatory

2–4 paragraphs of continuous prose. No bullet points. The Overview must answer:
- What is being built in this phase?
- Why at this point in the sequence? (What would break if this phase were deferred?)
- What is the observable state of the system at phase end?

### Group Reference — mandatory

A table mapping group letters to subsystems. All group letters used in this phase's task IDs must appear. The Tasks column lists the full ID range (e.g. `P1-A1 … P1-A4`). The Summary column is one sentence maximum.

### Prerequisites — mandatory

Prose description of what prior work must exist. Reference specific outputs (files, types, crate features) not just phase names. If this phase has no prerequisites, write "None. This is the first phase."

### Interfaces and Contracts — mandatory if any task references an external document; otherwise omit

A table. The `Contract document` column names the file (relative to `docs/`). The `Relevant to tasks` column lists task IDs. The `What must match` column names the specific types, endpoints, or protocol variants.

### Task Descriptions — mandatory

One H3 subsection per group letter. Within each group, one H4 subsection per task. Every task in `tasks_phase<NNN>.json` for this phase must appear here; every task described here must appear in `tasks_phase<NNN>.json`.

Each task's H4 subsection must contain exactly the four fields templated in §8 — **Goal**, **Files to create or modify**, **Key implementation notes**, **Acceptance criterion** — in that order, with no field omitted. The OpenCode PLAN agent (Qwen3 35B A3B) reads this section alongside the JSON `context` field at session start; `context` is capped at 1000 characters (§11) and deliberately omits rationale, so a missing or thin field here is not recoverable from the JSON side. Per-field minimum content:

- **Goal:** 1–2 sentences. Must state what the task produces and why it is needed at this point in the phase — not a restatement of the task description string.
- **Files to create or modify:** at least one path, relative to the project root, each with a trailing `—` clause naming what it contains or what changes. A bare path with no clause is insufficient.
- **Key implementation notes:** at least one bullet. Each note must name a concrete struct, function, field, ordering constraint, or edge case the agent would otherwise have to guess — not a generic reminder (e.g. "write good tests" is not a valid note; "`would_fit` returns false if the device index is unknown" is).
- **Acceptance criterion:** must be a runnable shell command or sequence. Vague criteria like "works correctly" or "looks good" are not permitted.

A subsection missing any of the four fields, or containing a field that fails its minimum content rule above, is not valid and must be corrected before the phase is approved.

### Phase Acceptance Criteria — mandatory

A fenced code block containing the full set of commands to run. Must include all test commands across all projects touched by this phase.

**This block must also contain a Runnable Proof** — one or more commands, clearly marked with a `# Runnable Proof (manual):` comment, that exercise the phase's new observable capability against a live running instance of the system (a bound server answering a real HTTP/WebSocket request, a real subprocess being killed and observed, a real file written to disk) rather than against the test suite. `cargo test`, `pytest`, `cargo clippy`, and the Windows cross-check are necessary gates but are never sufficient on their own and never substitute for the Runnable Proof — see `docs/PHASES.md` §Structure, rule 2 ("Every phase ends with a Runnable Proof"). The "Runnable Proof (summary)" column already committed for this phase number in `docs/PHASES.md`'s Phase Map is the floor, not the ceiling — restate it here as a concrete, copy-pasteable command sequence.

The standard shape (see §8a for the full pattern and rationale) is: start the binary in the background, `sleep` long enough for startup or worker `Ready`, exercise the capability with `curl`/`websocat`/a poll loop, assert on the literal observable result in a trailing `# ->` comment, then `kill` the background process. Do not write a Runnable Proof step that only re-invokes the test suite under a different name.

**Narrow exemption.** A phase may omit the live-instance proof, replacing it with the literal line `# Runnable Proof: not applicable — <one-sentence reason>`, only when **both** of the following hold:
- The phase introduces no new HTTP endpoint, WebSocket event, CLI flag, file-on-disk artifact, or other capability a human or script could observe from outside the test process; and
- The phase is one of: (a) pure repository/CI scaffolding with nothing yet running (e.g. the first phase of a project), (b) an internal refactor or correctness fix with no `pub` API or behavioural change (tagged `"refactor"` per §13), or (c) a phase whose own stated deliverable is a build/lint artifact (a generated file, a packaged binary, a documentation site) where the build or lint command itself *is* the full proof.

A phase that adds a new endpoint, event, or CLI surface does not qualify for the exemption merely because its mock-mode behavior is limited (e.g. an empty result set, a zero-length list) — an empty-but-200 response, or a 503-then-200 transition, is still a real, demonstrable, externally observable result and must be shown.

### Known Constraints and Gotchas — mandatory

Even if there are no gotchas, include the section with the text "None identified." Omitting it entirely signals the section was forgotten, not that there are no constraints.

### docs/RUNNABLE_PROOF.md update — mandatory

Authoring or amending a phase's Runnable Proof is not complete until `docs/RUNNABLE_PROOF.md` is updated in the same change. See [Section 9a](#9a-docsrunnable_proofmd--the-project-wide-proof-summary) for the document's required format and update procedure. A `TASKS_PHASE<NNN>.md` Phase Acceptance Criteria edit and the corresponding `docs/RUNNABLE_PROOF.md` entry are authored together, exactly like the `tasks_phase<NNN>.json` / `TASKS_PHASE<NNN>.md` pairing in §1 — one is never committed without the other.

---

## 9a. docs/RUNNABLE_PROOF.md — the project-wide proof summary

### Purpose

`docs/RUNNABLE_PROOF.md` is a single project-wide index of every phase's Runnable Proof, kept separate from the standard per-phase test gates. Its purpose is to let a human or agent answer "how do I manually verify phase N actually works" by reading one document, without paging through every `TASKS_PHASE<NNN>.md` and mentally filtering out the `cargo test`/`pytest`/`clippy`/cross-check boilerplate that is identical across nearly all of them.

### What it must contain

One entry per phase, in phase order, each naming the phase, the capability it proves, and the literal Runnable Proof command sequence — copied verbatim from that phase's `TASKS_PHASE<NNN>.md`, comment markers and all. Phases that are legitimately exempt under §9's narrow exemption are still listed, with the `# Runnable Proof: not applicable — <reason>` line shown rather than omitted, so the document remains a complete index and a reader is never left wondering whether a phase was simply skipped during authoring.

### What it must never contain

The standard gate commands — `cargo test`, `cargo clippy`, the dynamically-typed-language
syntax/compile gate (e.g. `python -m py_compile`, per `docs/ENVIRONMENT.md §6` and
§5 rule 5.11 in `FORGE_AGENT_RULES.md`), the project's mock-mode test invocation (e.g.
`<PROJECT>_WORKER_MOCK=1 ... pytest`), the Windows cross-check, `cargo fmt --check` —
are never reproduced in this document, even as context. They are identical across
nearly every phase and add no information; repeating them here would defeat the
document's purpose of being a fast, low-noise reference. If a phase's only Runnable
Proof line happens to be a non-standard test invocation that genuinely demonstrates
external behaviour (e.g. a 1000-trip stress test, which is itself the proof), that
line is included; the routine gates are not.

### Update procedure

Whenever a `TASKS_PHASE<NNN>.md`'s Phase Acceptance Criteria block gains, loses, or changes its Runnable Proof lines, `docs/RUNNABLE_PROOF.md` is updated in the same commit to match — copy the new Runnable Proof block verbatim into that phase's entry. When a new phase is authored, its `docs/RUNNABLE_PROOF.md` entry is added at the same time as its `TASKS_PHASE<NNN>.md`, not deferred to a later cleanup pass. This document is never the source of truth for a phase's proof — `TASKS_PHASE<NNN>.md` is — but it must never silently drift out of sync with it.

---

## 10. Task Sizing Rules

These rules prevent tasks from becoming too large for a single OpenCode session, and prevent them from becoming too trivial to justify the overhead of the approval cycle.

### Upper bounds (task is too large if any are true)

- Implementation would take a senior developer more than 2 hours
- The task creates more than 8 new source files
- The task adds more than ~400 lines of net new production code
- The `context` field exceeds 1000 characters even after removing redundancy
- The task requires reading more than 3 external reference documents
- The task touches more than one logical subsystem (e.g. both scheduler and server)
- The task description names more than one independent real-path concern (e.g. "assemble the pipeline, then handle the callback shape, then invoke the sampler, with cancellation") — each named concern is a separate place a future implementer can silently mock-and-defer one of them while appearing to complete the task; this trigger applies regardless of character count or whether `context` otherwise fits within bounds. A task this shape is also the most common source of an unverified `defers_to` (§12a) — splitting it along its independent concerns usually removes the need to defer anything at all.
- The task cannot complete within a 120-minute OpenCode ACT session (use context window §65% threshold in `FORGE_AGENT_RULES.md §7` as a proxy: if the plan calls for more than ~6 major file operations, split)

**Remedy:** Split the task. Prefer splitting along data structure vs behaviour lines (types first, then logic that uses them), or along "create stub" vs "implement" lines.

### Lower bounds (task is too small if any are true)

- The task can be completed by copying an example file and changing 3 values
- The `context` field is under 80 characters and still complete
- The task has no tests and cannot be given any
- The task is purely mechanical (e.g. "run cargo fmt")

**Remedy:** Merge with a related task. If the mechanical step is a prerequisite for something else, include it as a step in the succeeding task's `context` rather than as a standalone task.

### The atomicity test

A task is atomic if: when it is complete, the full test suite passes, nothing it produces is an intermediate incomplete state that a later task must fix, and it could in principle be reverted cleanly without breaking other completed tasks.

---

## 11. Context Field Writing Guide

The `context` field is injected directly into the OpenCode PLAN prompt. It is the primary specification. Write it as if you are a senior engineer leaving precise instructions for a colleague who knows the language and toolchain but has not read any other document about this project.

### Structure

Write the `context` as a dense sequence of concrete directives. No preamble ("In this task you will..."). No conclusion ("Once done, move to the next task"). Start with the first action.

```
Implement <thing> in <file>. <Specific fields/methods/variants>.
<Reference document> defines the exact shape.
<Test file>: >=<N> tests covering <behaviour>. <test command> exits 0.
```

### What to always include

1. **The file path(s)** where the implementation goes. Not just the module name — the relative path from the project root.
2. **The exact names** of every struct, enum, trait, function, or component to create. OpenCode must not invent names that later tasks will reference by a different name.
3. **The reference document** for any interface contract. Do not restate the contract inline — cite the document.
4. **The test command** as a complete shell command, with the minimum test count where relevant (e.g. `>=5 tests`). If the project defines a dual-mode parity-marker convention (`FORGE_AGENT_RULES.md §5.13` — e.g. AnvilML's `REAL_PATH_VERIFIED`/`MOCK_PATH_VERIFIED` pair on every node/arch-module function, `ANVILML_DESIGN.md §10.6`) and the function(s) this task implements fall within that convention's scope, state both the mock-mode and real-mode test commands explicitly — not just one. A `context` field that names only one mode for a covered function leaves the PLAN agent to guess at the second test, which is exactly the kind of unstated requirement this field exists to remove.
5. **Feature flags** if required (e.g. `--features mock-hardware`).
6. **Environment variables** that must be set for tests (e.g. `<PROJECT>_WORKER_MOCK=1`).

### What to never include

- Implementation details that are already obvious from the language/framework (e.g. "use `#[derive(Debug)]`")
- Explanations of why the design is the way it is — that belongs in `TASKS_PHASE<NNN>.md`
- Instructions to read standard docs — OpenCode agents always read `FORGE_AGENT_RULES.md`, `ENVIRONMENT.md`, `ARCHITECTURE.md`, and the relevant `TASKS_PHASE<NNN>.md` at session start; do not repeat these in the context field
- Steps that are handled by The Forge (committing, pushing) or by OpenCode automatically (writing plan and implementation reports, staging changes)

### Tone and style

- Imperative mood: "Implement", "Add", "Create", "Return", "Ensure" — not "Should implement" or "May need to add"
- Name things precisely: `WorkerPool` not "the pool struct", `cargo test -p <crate-name>` not "run tests"
- Cite measurements: `>=4 tests`, `exits 0`, `within 6s` — not "adequate tests" or "should pass"

### Length guideline

Aim for 300–650 characters for straightforward tasks. Tasks requiring 650–1000 characters indicate higher complexity and should be scrutinised for potential splitting. Tasks that cannot be specified in under 1000 characters are too large.

---

## 12. Dependency (prereqs) Design Guide

### Direct vs transitive

List only direct predecessors. If C depends on B and B depends on A, C's `prereqs` is `["B"]` only.

**Exception:** list A in C's `prereqs` if C would fail without A's specific output even if B happened to not use A's output. In practice this is rare — if the DAG is well-structured it doesn't arise.

### Parallelism

The Forge executes tasks sequentially (one at a time), but the DAG structure documents the true dependency relationships for the benefit of human reviewers and future tooling. Minimise prereqs to keep the critical path short.

### Cross-project dependencies

A task in one project that depends on a task in another project (e.g. a frontend task that needs a backend project's generated `openapi.json`) must list that other project's task in its `prereqs`. Cross-project prereqs are valid and The Forge handles them correctly — the DAG does not care which project a task belongs to. (This only applies to deployments with more than one registered project; a single-repo deployment has no cross-project dependencies.)

### Phase boundaries

Tasks in phase N+1 should not prereq tasks in phase N unless they genuinely need that specific output. The preferred pattern is to start a new phase only when all tasks in the previous phase are complete, enforced by giving the first task of phase N+1 a prereq on the last task(s) of phase N.

---

### Retrofit leaf tasks spawned from blocker deviations

When a task is blocked by a semver-incompatible dependency upgrade (or any
blocker that requires a follow-on fix in a file outside the blocked task's
original scope), a retrofit leaf task must be manually authored. That task's
`context` field MUST open with an explicit origin reference identifying the
task that generated the blocker and describing what was pinned and why:

```
"<PRIOR_TASK_ID> pinned <crate_name> at <old_version> due to semver
incompatibility with <affected_file_or_type>. Migrate to <new_version>: ..."
```

**Example:**

```json
{
  "id": "P7-D1",
  "context": "P7-C1 pinned tower at 0.4 due to breaking API changes in
    tower 0.5 affecting anvilml-server/src/lib.rs ServiceExt usage.
    Migrate tower to current stable: update ServiceExt call sites in
    crates/anvilml-server/src/lib.rs and any integration tests that use
    tower::ServiceExt::oneshot. cargo test --workspace --features
    mock-hardware exits 0.",
  ...
}
```

---

## 12a. The `defers_to` field — why it exists, and the authoring procedure it requires

### The incident this prevents

A prior phase shipped seven "real path" stubs where the real implementation
was deferred in one of three ways, all expressed only as prose in `context`
or a bare code comment, none of them checked by anything:

1. **Self-reference** — a task deferred to its own ID, impossible to fulfil
   once that task completes.
2. **Wrong reference** — deferred to a real task ID that exists but never
   actually touches the relevant file.
3. **No reference** — a `# TODO: implement real path` comment with no task
   ID at all.

None of these were caught until a human happened to read the stub code long
after the phase was marked complete. `defers_to` exists to make this class
of defect structurally impossible to ship unnoticed: it turns "I am
deferring this to something" from a sentence buried in prose into a field
The Forge checks the same way it already checks `prereqs`.

### `defers_to` is the only legitimate deferral mechanism

If a task's scope excludes functionality that some other task is expected
to deliver, that exclusion is recorded **only** via the `defers_to` field
(§4) — never as a bare prose statement in `context`, never as a `## Out of
Scope` bullet with no corresponding field entry, never as a code comment
with no JSON counterpart. A deferral that exists only in prose is, by
definition, the unvalidated form of exactly the defect described above —
it doesn't matter how carefully the prose is worded.

### The authoring procedure (generalizes the retrofit-leaf pattern above)

The retrofit-leaf pattern above already establishes the right discipline
for one specific case (a semver-blocker retrofit): the deferring task names
its target explicitly, and the target's own `context` is written to
genuinely receive that scope. `defers_to` generalizes this to every
deferral, with the link made structural rather than rhetorical:

1. **Before adding a `<TASK_ID>` to `defers_to`,** the author (human or LLM,
   per §14) must verify it the same way §4's `defers_to` rules require:
   the target must already exist in the task set being authored, must be
   genuinely downstream in the `prereqs` graph, and its own `description`/
   `context` must already state the deferred functionality as something it
   delivers. If no such task exists yet, **author it now**, in the same
   authoring pass — give it a real ID, correct `prereqs` placing it
   downstream, and a `context` that genuinely claims the scope — then
   reference it. Never write a `defers_to` entry pointing at a task that
   doesn't yet claim the scope, on the assumption that it will be patched
   up later.
2. **Symmetric documentation.** Per the retrofit-leaf precedent, the link
   should be recoverable from either end. The deferring task's `context`
   should name what is being deferred and why, even though the formal
   pointer lives in `defers_to`, not in that prose. The receiving task's
   `context` does not need to repeat the deferring task's ID (the JSON
   `defers_to` field already encodes that direction), but if the receiving
   task was authored specifically to receive this deferral, say so — the
   same way the retrofit-leaf example opens with its origin reference.
3. **Code-level marker — mandatory, carried through to ACT.** Every stub
   site that corresponds to a `defers_to` entry must, when implemented,
   carry a comment in the exact form:

   ```
   // defers_to: <TASK_ID> — <short reason>
   ```

   (or the language's comment syntax, e.g. `# defers_to: <TASK_ID> — ...`
   in Python). This is not optional documentation — it is how the link
   between `defers_to` (a JSON-only fact) and the actual stub in the
   source tree survives into the codebase, where a future engineer reading
   the file (not the task graph) can find it. See
   `FORGE_AGENT_RULES.md §9.7` for the ACT-session obligation that
   enforces this, and `FORGE_AGENT_RULES.md §9a` for how it is re-checked
   at phase close.

### What `defers_to` does not grant

`defers_to` records where scope is going; it does not grant permission to
skip verifying that the destination is real and correct. An entry that
satisfies every automated check in §5 (target exists, is downstream, no
self-reference) can still be invalid if the target's own wording does not
actually cover the deferred scope — that check has no mechanical
substitute and remains the author's and the PLAN agent's responsibility
(`FORGE_AGENT_RULES.md §4.7`).

---

## 13. Tag Reference

Tags are hints, not commands. They do not change The Forge's execution logic. They serve as searchable metadata and may inform future model-selection logic.

| Tag | Meaning | When to use |
|-----|---------|-------------|
| `"reasoning"` | This task requires non-trivial algorithmic reasoning, protocol matching, or subtle ordering logic. | Topological sort implementations, protocol framing, race condition handling, cycle detection, complex state machines. Not for routine CRUD. |
| `"manual"` | This task requires human intervention and cannot be completed automatically. | End-to-end smoke tests, hardware-dependent validation, anything requiring a running GPU. |
| `"breaking"` | This task changes a public interface that other tasks or projects depend on. | Adding variants to an enum used across crates, changing API response shapes, altering the IPC protocol. |
| `"scaffold"` | This task creates file structure and stubs with no real logic. | Cargo workspace creation, directory layout, empty component files. |
| `"refactor"` | This task makes zero observable behaviour changes (see FORGE_AGENT_RULES §4.6). ACT agent must verify no `pub` signature changed before writing the implementation report. |

Use `[]` when no tag applies. Do not combine `"manual"` with other tags — manual tasks are inherently special-cased.

---

## 14. LLM Generation Prompt Template

Use the following prompt structure when asking an LLM to generate tasks for a new phase. Replace all `<placeholder>` values.

---

```
You are generating task definitions for The Forge autonomous development
orchestrator (OpenCode-based). Read this entire spec before producing any output.

## Spec summary (read in full before generating)

<paste the full contents of FORGE_TASK_AUTHORING_SPEC.md here>

## Project context

Registered projects (replace with this deployment's actual `repos.json` keys — there may be one or several):
- <project_key_1>: <brief description>
- <project_key_2>: <brief description>

Reference documents available in each project's docs/ directory:
- ENVIRONMENT.md — environment variables and config fields
- ARCHITECTURE.md — crate layout, component structure, design tokens
- API_CONTRACT.md — HTTP endpoint shapes and WebSocket event schemas
- IPC_PROTOCOL.md — Rust↔Python IPC message types

## Phase to generate

Phase number: <NNN>
Phase name: <name>
Phase description: <1–2 sentences>
Projects involved: <list>
Depends on phases: <list or "none">

## Prior phase task IDs (for prereqs reference)

<list of task IDs from preceding phases that this phase may depend on>

## What this phase must produce

<Technical description of the desired end state. List the specific
files, types, endpoints, or features that must exist when this phase
is complete. Be as precise as possible.>

## Constraints

- Maximum context field length: 1000 characters
- All task IDs must use the format P<phase_short>-<group><seq>
- Each task targets exactly one project (no "repos" field)
- Every task must have a runnable acceptance criterion command
- Tasks tagged "manual" need no acceptance criterion command
- Do not create tasks that span two subsystems
- The phase's "Phase Acceptance Criteria" block must include a Runnable Proof
  (commands marked `# Runnable Proof (manual):` that exercise the phase's new
  capability against a live running instance, not just the test suite) unless
  the phase qualifies for the narrow exemption in §9 — in which case write the
  `# Runnable Proof: not applicable — <reason>` line instead of omitting it
- `docs/RUNNABLE_PROOF.md` must be updated with this phase's entry as part of
  the same output — see §9a for its required format

## Output format

Produce two outputs:

### Output 1: tasks_phase<NNN>.json entries

A JSON array of task objects conforming exactly to the spec.
This file will be placed at `.forge/tasks/tasks_phase<NNN>.json` inside the project repo.
Output only the JSON — no explanation, no markdown fences around the JSON itself.

### Output 2: TASKS_PHASE<NNN>.md

The full phase document conforming exactly to the spec.
Output only the markdown — no explanation before or after.

### Output 3: docs/RUNNABLE_PROOF.md entry

The single entry to append (or update in place, if this phase already has one)
to `docs/RUNNABLE_PROOF.md`, conforming to §9a. Output only the markdown for
that one phase's entry — no explanation before or after, and do not reproduce
the standard test-gate commands.
```

---

### Reviewing LLM output

After generation, validate against these checks before using the output:

- [ ] Every task has all six fields: `id`, `description`, `phase`, `project`, `prereqs`, `context`, `tags`
- [ ] No task has a `repos` field
- [ ] All `project` values are registered names — i.e. they exist as keys in this deployment's `repos.json` (this document does not enumerate them; they vary per deployment)
- [ ] All `prereqs` IDs exist in the array or in prior phases
- [ ] No two tasks share an ID
- [ ] Phase prefix in ID matches the `phase` field value
- [ ] No `context` field exceeds 1000 characters
- [ ] Every `context` ends with a runnable acceptance criterion
- [ ] Group letters in the TASKS_PHASE doc match the IDs in the JSON
- [ ] Every task ID in the JSON appears in the TASKS_PHASE doc and vice versa
- [ ] No cycle exists in the prereqs graph (trace manually for small sets; use `python forge.py --repo <project> --list` for large sets)
- [ ] The Phase Acceptance Criteria block contains a `# Runnable Proof (manual):` section exercising a live instance, or the explicit `# Runnable Proof: not applicable — <reason>` line if exempt under §9
- [ ] The Runnable Proof (or its "not applicable" line) is not a disguised re-invocation of `cargo test`/`pytest`/`clippy`
- [ ] `docs/RUNNABLE_PROOF.md` has a matching, up-to-date entry for this phase

---

## 15. Complete Worked Example (Illustrative Only)

> **This section is illustrative, not normative.** It uses a real project, `anvilml`
> (a Rust/Python AI-inference service), purely to show what a fully-formed task pair
> looks like with concrete, non-placeholder content — concrete names read better than
> `<placeholder>` text when learning the format. Nothing here implies `anvilml` is a
> registered project in your deployment, that `sindristudio`/`bloomeryui`/`anvilml`
> are the only valid `project` values (they are not — see §4 `project` field and §2
> Concepts and Definitions), or that any of the specific types, crate names, or file
> paths below (`JobQueue`, `VramLedger`, `anvilml-scheduler`, etc.) are conventions
> this spec requires. Substitute your own project's real names when authoring tasks.

The following is a minimal but complete example of two tasks in a hypothetical Phase 3 targeting the `anvilml` project. It shows both the `tasks_phase003.json` entries and the corresponding `TASKS_PHASE003.md` sections.

### tasks_phase003.json entries

```json
[
  {
    "id": "P3-A1",
    "description": "anvilml-scheduler: JobQueue + VramLedger",
    "phase": "3",
    "project": "anvilml",
    "prereqs": ["P2-A4"],
    "context": "Implement queue.rs (JobQueue: push, pop_front, cancel, get, list, len) and ledger.rs (VramLedger: per-device VRAM budget, would_fit(vram_mib), mark_loaded, mark_evicted). Pure logic, no async or IO. cargo test -p anvilml-scheduler exits 0 with >=8 tests.",
    "tags": ["reasoning"]
  },
  {
    "id": "P3-A2",
    "description": "anvilml-scheduler: JobScheduler dispatch loop",
    "phase": "3",
    "project": "anvilml",
    "prereqs": ["P3-A1"],
    "context": "Implement JobScheduler in scheduler.rs: submit(job)->JobId, cancel(id), get_job(id), list_jobs(), start_dispatch_loop(workers, ledger) assigns queued jobs to idle workers via WorkerPool.acquire_idle(). Implement dag.rs: topological sort + cycle detection. cargo test -p anvilml-scheduler --features mock-hardware exits 0 with >=6 tests.",
    "tags": ["reasoning"]
  }
]
```

### Corresponding TASKS_PHASE003.md section

````markdown
# Tasks: Phase 3 — AnvilML Scheduler

**Phase:** 3
**Name:** AnvilML Scheduler
**Project(s):** anvilml
**Status:** Approved
**Depends on phases:** 1, 2

---

## Overview

Phase 3 implements the job scheduler that sits between the HTTP server and
the worker pool. When a job is submitted via POST /v1/jobs, the scheduler
queues it, tracks VRAM availability across all devices, selects an idle
worker whose device has sufficient free VRAM, and dispatches the job to that
worker. It also maintains the per-job status that GET /v1/jobs/:id queries.

The scheduler is purely in-memory for this phase. Persistence across restarts
is deferred to a later phase. The dispatch loop runs as a Tokio task and
must not block the async runtime.

---

## Group Reference

| Group | Subsystem          | Tasks        | Summary                                     |
|-------|--------------------|--------------|---------------------------------------------|
| A     | anvilml-scheduler  | P3-A1, P3-A2 | Job queue, VRAM ledger, dispatch loop, DAG  |

---

## Prerequisites

The WorkerPool from Phase 2 must be complete. Specifically:
- `WorkerPool::acquire_idle()` must be callable and return a worker handle
- `WorkerPool::set_busy()` and `set_idle()` must exist
- `anvilml-core` domain types (Job, JobStatus, GpuDevice) must be final

---

## Interfaces and Contracts

| Contract document | Relevant to tasks | What must match                          |
|-------------------|-------------------|------------------------------------------|
| `API_CONTRACT.md` | P3-A2             | JobStatus enum values in GET /v1/jobs    |

---

## Task Descriptions

### Group A — anvilml-scheduler

#### P3-A1: anvilml-scheduler: JobQueue + VramLedger

**Goal:** Provide the two data structures the dispatch loop depends on.
JobQueue is a FIFO queue with cancel support. VramLedger tracks per-device
free VRAM to prevent over-scheduling.

**Files to create or modify:**
- `crates/anvilml-scheduler/src/queue.rs` — JobQueue implementation
- `crates/anvilml-scheduler/src/ledger.rs` — VramLedger implementation
- `crates/anvilml-scheduler/src/lib.rs` — re-export both

**Key implementation notes:**
- VramLedger is keyed by device index (u32), not device type
- `would_fit` returns false if the device index is unknown
- Both structures must be Send + Sync for use behind Arc<Mutex<>>

**Acceptance criterion:** `cargo test -p anvilml-scheduler` exits 0 with >=8 tests.

---

#### P3-A2: anvilml-scheduler: JobScheduler dispatch loop

**Goal:** Wire the queue and ledger into a running scheduler that moves jobs
from Queued to Running as workers become available.

**Files to create or modify:**
- `crates/anvilml-scheduler/src/scheduler.rs` — JobScheduler
- `crates/anvilml-scheduler/src/dag.rs` — topological sort and cycle detection
- `crates/anvilml-scheduler/src/lib.rs` — re-export JobScheduler

**Key implementation notes:**
- `start_dispatch_loop` must be called once and returns a `JoinHandle`
- Dispatch is triggered both by new job submissions and by worker idle events
- Cycle detection must return a descriptive error naming the involved node IDs

**Acceptance criterion:** `cargo test -p anvilml-scheduler --features mock-hardware`
exits 0 with >=6 tests.

---

## Phase Acceptance Criteria

```
cargo test -p anvilml-scheduler --features mock-hardware
cargo clippy -p anvilml-scheduler --features mock-hardware -- -D warnings
# Runnable Proof (manual): a submitted job is dispatched and reaches Running
cargo run --features mock-hardware &
sleep 5
JOB_ID=$(curl -s -X POST http://127.0.0.1:8488/v1/jobs -H 'Content-Type: application/json' \\
  -d '{"graph":{"nodes":[]},"settings":{}}' | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
curl -s "http://127.0.0.1:8488/v1/jobs/$JOB_ID" | python3 -c "import sys,json; assert json.load(sys.stdin)['status'] in ('Queued','Running')"
# -> 200 with status Queued or Running (dispatch loop picked it up)
kill %1
```

---

## Known Constraints and Gotchas

- The dispatch loop must use `tokio::sync::Mutex`, not `std::sync::Mutex`,
  because it is held across await points when communicating with workers.
- Tests that spawn workers must pass `--features mock-hardware` or they will
  attempt real GPU detection and fail in CI.
````

### Corresponding docs/RUNNABLE_PROOF.md entry

This is Output 3 alongside the JSON and the TASKS_PHASE doc above — the same
Runnable Proof block, without the standard `cargo test`/`clippy` lines:

````markdown
## Phase 3 — AnvilML Scheduler

Capability: jobs submitted via `POST /v1/jobs` are queued and dispatched to a
worker.

```bash
cargo run --features mock-hardware &
sleep 5
JOB_ID=$(curl -s -X POST http://127.0.0.1:8488/v1/jobs -H 'Content-Type: application/json' \\
  -d '{"graph":{"nodes":[]},"settings":{}}' | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
curl -s "http://127.0.0.1:8488/v1/jobs/$JOB_ID" | python3 -c "import sys,json; assert json.load(sys.stdin)['status'] in ('Queued','Running')"
# -> 200 with status Queued or Running (dispatch loop picked it up)
kill %1
```
````

---

*End of specification.*