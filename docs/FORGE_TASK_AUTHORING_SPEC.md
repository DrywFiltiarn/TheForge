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
9. [TASKS_PHASE Document — Section Reference](#9-tasks_phase-document--section-reference)
10. [Task Sizing Rules](#10-task-sizing-rules)
11. [Context Field Writing Guide](#11-context-field-writing-guide)
12. [Dependency (prereqs) Design Guide](#12-dependency-prereqs-design-guide)
13. [Tag Reference](#13-tag-reference)
14. [LLM Generation Prompt Template](#14-llm-generation-prompt-template)
15. [Complete Worked Example](#15-complete-worked-example)

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

**Phase** — a named group of tasks that together achieve a major milestone (e.g. "AnvilML Core Types"). Phases are numbered 001–999. Phase numbers are sequential but not necessarily contiguous. A phase has one `tasks_phase<NNN>.json` per project and one `TASKS_PHASE<NNN>.md` per project.

**Project** — one of the registered repositories (`sindristudio`, `anvilml`, `bloomeryui`). Each task targets exactly one project. Cross-project work must be split into separate tasks, one per project.

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
  "tags":        ["<tag>", ...]
}
```

All six fields are required. No additional fields are permitted. The Forge rejects tasks with unknown fields (to catch the deprecated `repos` field and similar mistakes).

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
"anvilml-core: config types"
"anvilml-registry: SQLite persistence"
"BloomeryUI: artifact gallery component"
"Launcher binary: graceful shutdown"
```

**Bad examples:**
```
"Do the config stuff"           ← vague, no component reference
"Implement everything in core"  ← not atomic
"Fix bug"                       ← not descriptive enough
"anvilml-core"                  ← no verb/outcome
```

**Length:** 4–80 characters. Longer descriptions are truncated in commit messages.

---

### `phase` — string, required

The phase number as a string, without leading zeros.

**Valid values:** `"1"`, `"2"`, `"12"`, etc. NOT `"001"` or `"01"`.

The string form is used because JSON has no integer-string distinction in practice, and The Forge uses this value to construct the docs filename: `docs/TASKS_PHASE` + `phase.zfill(3)` + `.md` → `docs/TASKS_PHASE001.md`.

---

### `project` — string, required

The logical name of the single repository this task operates on. Must exactly match a key in `repos.json`.

**Current valid values:** `"sindristudio"`, `"anvilml"`, `"bloomeryui"`

**Rules:**
- Exactly one project per task. If a task naturally spans two projects, split it.
- The project name determines where OpenCode runs (`cwd`), where reports are written (`.forge/reports/` inside that repo), and which repo The Forge commits and pushes.
- `"root"` is no longer a valid value (deprecated). Tasks that would have targeted `root` should target `sindristudio`.

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
- Do not exceed 600 characters. If the task needs more context than that, it is too large and must be split.

---

### `tags` — array of strings, required

Optional hints to The Forge and the model about the nature of the task. Use an empty array `[]` if no tags apply. Do not invent new tag values — use only the tags defined in [Section 13](#13-tag-reference).

---

## 5. Task JSON — Validation Rules

The Forge runs these checks at startup and aborts if any fail. An LLM generating tasks must satisfy all of them.

| Rule | Error message |
|------|---------------|
| Every task has all six required fields | `missing required field '<field>'` |
| `id` is unique across the array | `duplicate task ID: '<id>'` |
| `phase` matches the numeric prefix of `id` | `phase mismatch: id 'P1-A3' but phase '2'` |
| `project` is registered in `repos.json` | `project '<name>' is not registered in repos.json` |
| `"repos"` field is absent | `field 'repos' is no longer supported. Rename it to 'project'` |
| Every `prereqs` entry exists as a task ID | `prereq '<id>' in task '<id>' does not exist` |
| The DAG has no cycles | `cycle detected involving tasks: <list>` |
| `description` is non-empty | `field 'description' must be a non-empty string` |
| `context` is non-empty | `field 'context' must be a non-empty string` |

---

## 6. Task JSON — ID and Phase Numbering

### Phase number assignment

Phases are assigned sequentially starting at 1. They represent major development milestones. Each phase builds on the previous. Phase numbers are chosen before task authoring begins and documented in `docs/PHASES.md`.

Example mapping:

| Phase | Name | Description |
|-------|------|-------------|
| 001 | Repository Scaffold | Repo structure, CI skeleton, crate stubs |
| 002 | AnvilML Core Types | Config, domain types, IPC messages |
| 003 | Hardware Detection | ROCm, CUDA, IPEX, mock detector |
| 004 | Worker Management | WorkerPool, IPC bridge, env injection |
| ... | ... | ... |

### Group letter assignment within a phase

Within a phase, tasks are grouped by subsystem using uppercase letters. There is no fixed mapping of letters to subsystems — assign letters to maintain logical grouping within each phase. Document the mapping in the phase's `TASKS_PHASE<NNN>.md`.

Example for phase 1:
- `A` — anvilml-core
- `B` — anvilml-registry
- `C` — Python worker
- `D` — anvilml-worker
- `E` — anvilml-scheduler
- `F` — anvilml-server
- `G` — OpenAPI generation
- `H` — Launcher binary
- `I` — BloomeryUI
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

When a phase spans multiple projects (e.g. Phase 2 includes both `anvilml` and `bloomeryui` tasks), a copy of the `TASKS_PHASE<NNN>.md` must exist in each project's `docs/` directory. The copies may differ in the sections they emphasise — the AnvilML copy covers Rust/Python concerns in depth; the BloomeryUI copy covers TypeScript/React concerns.

### Filename

`TASKS_PHASE` + phase number zero-padded to three digits + `.md`

Examples: `TASKS_PHASE001.md`, `TASKS_PHASE012.md`, `TASKS_PHASE099.md`

---

## 8. TASKS_PHASE Document — Format Specification

```markdown
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
the context field in tasks_phase<NNN>.json.>

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
Each line is a concrete, runnable command.>

```
cargo test --workspace --features mock-hardware
cargo clippy --workspace --features mock-hardware -- -D warnings
cargo run -p anvilml-openapi
pnpm type-check
pnpm test:run
ANVILML_WORKER_MOCK=1 python -m pytest
```

---

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

The **Acceptance criterion** line must be a runnable shell command or sequence. Vague criteria like "works correctly" or "looks good" are not permitted.

### Phase Acceptance Criteria — mandatory

A fenced code block containing the full set of commands to run. Must include all test commands across all projects touched by this phase.

### Known Constraints and Gotchas — mandatory

Even if there are no gotchas, include the section with the text "None identified." Omitting it entirely signals the section was forgotten, not that there are no constraints.

---

## 10. Task Sizing Rules

These rules prevent tasks from becoming too large for a single OpenCode session, and prevent them from becoming too trivial to justify the overhead of the approval cycle.

### Upper bounds (task is too large if any are true)

- Implementation would take a senior developer more than 2 hours
- The task creates more than 8 new source files
- The task adds more than ~400 lines of net new production code
- The `context` field exceeds 600 characters even after removing redundancy
- The task requires reading more than 3 external reference documents
- The task touches more than one logical subsystem (e.g. both scheduler and server)
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
4. **The test command** as a complete shell command, with the minimum test count where relevant (e.g. `>=5 tests`).
5. **Feature flags** if required (e.g. `--features mock-hardware`).
6. **Environment variables** that must be set for tests (e.g. `ANVILML_WORKER_MOCK=1`).

### What to never include

- Implementation details that are already obvious from the language/framework (e.g. "use `#[derive(Debug)]`")
- Explanations of why the design is the way it is — that belongs in `TASKS_PHASE<NNN>.md`
- Instructions to read standard docs — OpenCode agents always read `FORGE_AGENT_RULES.md`, `ENVIRONMENT.md`, `ARCHITECTURE.md`, and the relevant `TASKS_PHASE<NNN>.md` at session start; do not repeat these in the context field
- Steps that are handled by The Forge (committing, pushing) or by OpenCode automatically (writing plan and implementation reports, staging changes)

### Tone and style

- Imperative mood: "Implement", "Add", "Create", "Return", "Ensure" — not "Should implement" or "May need to add"
- Name things precisely: `WorkerPool` not "the pool struct", `cargo test -p anvilml-worker` not "run tests"
- Cite measurements: `>=4 tests`, `exits 0`, `within 6s` — not "adequate tests" or "should pass"

### Length guideline

Aim for 200–400 characters for straightforward tasks. Tasks requiring 400–600 characters indicate higher complexity and should be scrutinised for potential splitting. Tasks that cannot be specified in under 600 characters are too large.

---

## 12. Dependency (prereqs) Design Guide

### Direct vs transitive

List only direct predecessors. If C depends on B and B depends on A, C's `prereqs` is `["B"]` only.

**Exception:** list A in C's `prereqs` if C would fail without A's specific output even if B happened to not use A's output. In practice this is rare — if the DAG is well-structured it doesn't arise.

### Parallelism

The Forge executes tasks sequentially (one at a time), but the DAG structure documents the true dependency relationships for the benefit of human reviewers and future tooling. Minimise prereqs to keep the critical path short.

### Cross-project dependencies

A task in `bloomeryui` that depends on an `anvilml` task (e.g. because it needs the generated `openapi.json`) must list that `anvilml` task in its `prereqs`. Cross-project prereqs are valid and The Forge handles them correctly — the DAG does not care which project a task belongs to.

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

## 13. Tag Reference

Tags are hints, not commands. They do not change The Forge's execution logic. They serve as searchable metadata and may inform future model-selection logic.

| Tag | Meaning | When to use |
|-----|---------|-------------|
| `"reasoning"` | This task requires non-trivial algorithmic reasoning, protocol matching, or subtle ordering logic. | Topological sort implementations, protocol framing, race condition handling, cycle detection, complex state machines. Not for routine CRUD. |
| `"manual"` | This task requires human intervention and cannot be completed automatically. | End-to-end smoke tests, hardware-dependent validation, anything requiring a running GPU. |
| `"breaking"` | This task changes a public interface that other tasks or projects depend on. | Adding variants to an enum used across crates, changing API response shapes, altering the IPC protocol. |
| `"scaffold"` | This task creates file structure and stubs with no real logic. | Cargo workspace creation, directory layout, empty component files. |

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

Registered projects:
- sindristudio: <brief description>
- anvilml: <brief description>
- bloomeryui: <brief description>

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

- Maximum context field length: 600 characters
- All task IDs must use the format P<phase_short>-<group><seq>
- Each task targets exactly one project (no "repos" field)
- Every task must have a runnable acceptance criterion command
- Tasks tagged "manual" need no acceptance criterion command
- Do not create tasks that span two subsystems

## Output format

Produce two outputs:

### Output 1: tasks_phase<NNN>.json entries

A JSON array of task objects conforming exactly to the spec.
This file will be placed at `.forge/tasks/tasks_phase<NNN>.json` inside the project repo.
Output only the JSON — no explanation, no markdown fences around the JSON itself.

### Output 2: TASKS_PHASE<NNN>.md

The full phase document conforming exactly to the spec.
Output only the markdown — no explanation before or after.
```

---

### Reviewing LLM output

After generation, validate against these checks before using the output:

- [ ] Every task has all six fields: `id`, `description`, `phase`, `project`, `prereqs`, `context`, `tags`
- [ ] No task has a `repos` field
- [ ] All `project` values are registered names (`sindristudio`, `anvilml`, `bloomeryui`)
- [ ] All `prereqs` IDs exist in the array or in prior phases
- [ ] No two tasks share an ID
- [ ] Phase prefix in ID matches the `phase` field value
- [ ] No `context` field exceeds 600 characters
- [ ] Every `context` ends with a runnable acceptance criterion
- [ ] Group letters in the TASKS_PHASE doc match the IDs in the JSON
- [ ] Every task ID in the JSON appears in the TASKS_PHASE doc and vice versa
- [ ] No cycle exists in the prereqs graph (trace manually for small sets; use `python forge.py --repo <project> --list` for large sets)

---

## 15. Complete Worked Example

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

```markdown
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

\`\`\`
cargo test -p anvilml-scheduler --features mock-hardware
cargo clippy -p anvilml-scheduler --features mock-hardware -- -D warnings
\`\`\`

---

## Known Constraints and Gotchas

- The dispatch loop must use `tokio::sync::Mutex`, not `std::sync::Mutex`,
  because it is held across await points when communicating with workers.
- Tests that spawn workers must pass `--features mock-hardware` or they will
  attempt real GPU detection and fail in CI.
```

---

*End of specification.*