# Backlog

This backlog is functionality-first. Decisions in `SPECS.md` are treated as implementation constraints, not backlog scope.

## Priority model
- `P0`: required for an end-to-end usable PoC.
- `P1`: important improvements that materially increase reliability and usability.
- `P2`: optional enhancements after core validation.

## Status legend
- `Completed`: implemented and usable in current codebase.
- `Partial`: implemented in part; still missing acceptance-criteria coverage.
- `Open`: not yet implemented.

## P0

### BL-001 Ingest and normalize user brief
- Priority: `P0`
- Status: `Completed`
- Notes: Brief parsing + required-field validation + normalized task package creation are in place.
- Goal: convert raw user input and optional files into a normalized task package.
- Scope:
  - Parse input text/file.
  - Extract objective, audience, tone, constraints, and source candidates.
  - Validate required fields and prompt for missing mandatory fields.
- Acceptance criteria:
  - Produces one normalized task package object per run.
  - Missing required fields block stage progression and request clarification.
- Dependencies: none
- Guided by decisions: D4, D7

### BL-002 Coordinator stage machine and ledger
- Priority: `P0`
- Status: `Completed`
- Notes: 7-stage machine, transition checks, approvals, and persisted ledger are implemented.
- Goal: execute the 7-stage pipeline with deterministic gating.
- Scope:
  - Implement stage transitions and required-input checks.
  - Maintain run ledger (`not_started`, `in_progress`, `completed`, `failed`, `blocked`).
  - Enforce that only coordinator advances stage state.
- Acceptance criteria:
  - Pipeline cannot skip required stages.
  - Invalid transition attempts are rejected and logged.
  - Run state is recoverable from persistence.
- Dependencies: BL-001, BL-004
- Guided by decisions: D1, D2, D4

### BL-003 Shared chat protocol and routing
- Priority: `P0`
- Status: `Completed`
- Notes: Message envelope validation is enforced (type/stage/priority/task_id/routing), `reply_to` is persisted, and `/inbox <agent_id>` derives filtered inbox from the shared log.
- Goal: support team-like communication with deterministic task pickup.
- Scope:
  - Validate message envelope fields.
  - Enforce routing rules (`broadcast` announcements only, direct assignment for tasks).
  - Support `task`, `question`, `result`, `review`, `decision`, `status` types.
- Acceptance criteria:
  - Invalid messages are rejected with actionable errors.
  - Task messages require single assignee and `task_id`.
  - Per-agent filtered inbox view can be derived from shared log.
- Dependencies: BL-004
- Guided by decisions: D2, D7

### BL-004 Persistence layer (SQLite)
- Priority: `P0`
- Status: `Completed`
- Notes: SQLite schema/repository implemented for runs, artifacts, messages, events, and tasks with resume support.
- Goal: persist all run-critical artifacts.
- Scope:
  - SQLite schema and access layer for runs, messages, tasks, stage ledger, evidence pack, outputs.
  - Migration/bootstrap command.
  - Basic indexing for run/task/message lookups.
- Acceptance criteria:
  - Restarting app preserves run history and resumable state.
  - CRUD operations cover all stage artifacts.
- Dependencies: none
- Guided by decisions: D2

### BL-005 Worker queue and parallel task execution
- Priority: `P0`
- Status: `Completed`
- Notes: Parallel research fan-out/fan-in is implemented via background worker execution and task lifecycle updates.
- Goal: run independent tasks in parallel without losing coordinator control.
- Scope:
  - Local queue for background tasks.
  - Worker execution with task ownership and status updates.
  - Coordinator fan-out/fan-in for independent research tasks.
- Acceptance criteria:
  - At least two independent tasks can run concurrently within a run.
  - Coordinator receives task completion/failure events and updates ledger.
- Dependencies: BL-002, BL-004
- Guided by decisions: D1, D3

### BL-006 Research stage and evidence pack generation
- Priority: `P0`
- Status: `Completed`
- Notes: URL source fetch/extract pipeline is implemented (with fallback), evidence metadata is captured per source, and claims are mapped to `source_id`.
- Goal: produce structured evidence from provided links/text.
- Scope:
  - Source fetch/extract pipeline.
  - Claim extraction and mapping to source metadata.
  - Tiering and confidence assignment.
- Acceptance criteria:
  - Evidence pack contains required source metadata fields.
  - Each factual claim in downstream draft can reference at least one source id.
- Dependencies: BL-001, BL-005
- Guided by decisions: D3, D6

### BL-007 Draft, critique, revise loop
- Priority: `P0`
- Status: `Completed`
- Notes: Outline/draft generation, rubric-based critique with hard gates, and iterative revise rounds are implemented; `Final` is blocked unless critique passes.
- Goal: generate a draft, review it with rubric, and revise until pass or escalation.
- Scope:
  - Outline creation.
  - Draft generation with inline citation markers.
  - Review scoring and prioritized feedback.
  - Revision pass with change summary.
- Acceptance criteria:
  - Review outputs rubric scores and hard-gate checks.
  - Failed drafts re-enter revise loop.
  - Pass threshold and hard-gate policy are enforced before finalization.
- Dependencies: BL-002, BL-006
- Guided by decisions: D3, D4

### BL-008 Citation integrity and anti-fabrication guardrails
- Priority: `P0`
- Status: `Completed`
- Notes: Finalization now enforces citation integrity (marker resolution, required source fields, URL/date sanity, and unmapped-link rejection) and fails gracefully on violations.
- Goal: prevent unsupported or invented citations.
- Scope:
  - Marker-to-source validation (`[Sx]` must resolve).
  - Required citation fields enforcement.
  - Reject fabricated links/dates/source metadata patterns.
- Acceptance criteria:
  - Finalization fails on unresolved markers.
  - Finalization fails on missing required source fields.
- Dependencies: BL-006, BL-007
- Guided by decisions: D3, D6

### BL-009 Console UX (core commands)
- Priority: `P0`
- Status: `Completed`
- Notes: End-to-end run control is now TUI-native via chat commands (including approvals, source inspection, and export) with no mandatory external CLI steps.
- Goal: operate the full system through an interactive TUI as the primary control surface.
- Scope:
  - Chat-first operation for run control and stage progression.
  - In-TUI approvals/rejections and coordinator feedback loop.
  - In-TUI inspection of plan, run state, agents, inboxes, tasks, and evidence references.
  - TUI commands as convenience (e.g., slash commands), not mandatory shell commands.
  - Export final post + references from the TUI flow.
- Acceptance criteria:
  - User can complete one end-to-end run using only the TUI.
  - No mandatory external CLI commands are required during normal operation.
  - Mandatory approvals pause progression until explicit user action.
- Dependencies: BL-002, BL-003, BL-004, BL-007
- Guided by decisions: D4, D7

### BL-018 Modularize the application architecture
- Priority: `P0`
- Status: `Completed`
- Notes:
  - Quick debt snapshot:
    - Runtime decomposition is in progress; structure is now modular under `app/`.
    - Remaining debt is mostly behavioral hardening/verification rather than file-size coupling.
  - Progress update:
    - Root entrypoint is now thin (`main.py`), with runtime moved under `app/`.
    - TUI runtime lives in `app/tui.py`.
    - Stage progression and slash-command handling have been extracted to `app/stages.py` and `app/commands.py`.
    - Shared domain constants and dataclasses were extracted to `app/domain/models.py`.
    - SQLite schema and `RunRepository` were extracted to `app/storage/repository.py`.
    - Inference engines were extracted to `app/engines/` (`coordinator`, `research`, `writing`, `review`).
    - Service helpers were extracted to `app/services/` (`ingest`, `sources`, `citations`, `export`).
    - Workflow-heavy stage/research/content logic was extracted to `app/workflow/` (`research`, `content`).
    - UI/chat presentation and formatting logic was extracted to `app/ui/presentation.py`.
    - Run lifecycle/state/render orchestration was extracted to `app/ui/runtime.py`.
    - `app/tui.py` was reduced to ~526 lines; all runtime modules are now below 600 lines.
    - Compile validation currently passes after refactor (`uv run python -m py_compile ...`).
- Goal: split the single-file implementation into maintainable modules without changing behavior.
- Scope:
  - Create module boundaries (for example: `ui/`, `engines/`, `domain/`, `storage/`, `services/`, `utils/`).
  - Extract TUI view/rendering and command handling from orchestration logic.
  - Extract stage execution logic into dedicated workflow/service modules.
  - Keep stable public entrypoint (`main.py`) as a thin bootstrap.
- Acceptance criteria:
  - No single module exceeds 600 lines for core runtime code.
  - `main.py` acts as a composition/bootstrap file only.
  - Existing run/resume behavior and TUI command behavior remain functionally equivalent.
  - Compile + smoke run succeed after refactor.
- Dependencies: BL-009
- Guided by decisions: D1, D2, D7

### BL-019 Technical debt hardening pass
- Priority: `P0`
- Status: `Open`
- Notes:
  - Quick debt snapshot:
    - Broad `except Exception` handling is widely used and can hide actionable failures.
    - Error reporting is inconsistent between engines/stage execution paths.
    - Some validation/guardrail logic is duplicated across stages.
- Goal: reduce fragility and improve debuggability after modularization.
- Scope:
  - Replace broad exception handling with narrower/typed handling where practical.
  - Standardize error envelopes/reason codes for stage and task failures.
  - Centralize shared validations (message envelope, citation checks, gate checks) in reusable services.
  - Add targeted regression tests for failure scenarios and gate enforcement.
- Acceptance criteria:
  - Critical paths expose consistent, actionable failure reasons.
  - Key failure modes (fetch failure, inference failure, invalid citations, invalid routing) are covered by tests.
  - No behavior regressions in end-to-end TUI flow.
- Dependencies: BL-018
- Guided by decisions: D3, D5

## P1

### BL-010 Run budgets, retries, and failure handling
- Priority: `P1`
- Status: `Open`
- Notes: Centralized timeout/retry/budget enforcement is still pending.
- Goal: enforce runtime and cost constraints consistently.
- Scope:
  - Per-task timeout policy.
  - Retry policy with exponential backoff.
  - Run-level task and token caps.
  - Escalation when budgets are exhausted.
- Acceptance criteria:
  - Timeouts/retries/caps are configurable and enforced.
  - Breach events are visible in run status and logs.
- Dependencies: BL-002, BL-005
- Guided by decisions: D3

### BL-011 Checkpoint UX and run-time approvals
- Priority: `P1`
- Status: `Partial`
- Notes: Ingest/Final approval flow exists; richer reject-routing + optional Outline gate configuration remain open.
- Goal: make approvals explicit and auditable.
- Scope:
  - Checkpoint prompts at Ingest and Final.
  - Optional Outline gate per run config.
  - Approval/rejection audit trail.
- Acceptance criteria:
  - Stage progression blocked when required approval is pending.
  - Reject path captures reason and routes back to correct stage.
- Dependencies: BL-002, BL-009
- Guided by decisions: D4, D7

### BL-012 Security baseline controls
- Priority: `P1`
- Status: `Open`
- Notes: URL/network restrictions and secret-redaction policies are not fully enforced yet.
- Goal: apply minimal safe defaults for web-enabled research.
- Scope:
  - Block non-HTTP(S), localhost, and private-network URLs.
  - Treat retrieved content as untrusted.
  - Prevent leaking secrets to chat/log storage.
- Acceptance criteria:
  - Disallowed URLs are rejected before fetch.
  - Logs redact known secret patterns and environment keys.
- Dependencies: BL-006
- Guided by decisions: D6

### BL-013 Observability and diagnostics
- Priority: `P1`
- Status: `Partial`
- Notes: Event log, task table, and run context exist; consolidated timeline/diagnostic commanding still needs expansion.
- Goal: make failures debuggable without deep code inspection.
- Scope:
  - Structured logs for run, stage, task, and message events.
  - Run summary including durations, retries, and failures.
  - Correlate events by `run_id` and `task_id`.
- Acceptance criteria:
  - One command can show timeline summary for a run.
  - Failures include actionable reason codes.
- Dependencies: BL-002, BL-004, BL-005
- Guided by decisions: D1, D2, D3

### BL-014 Lightweight automated testing
- Priority: `P1`
- Status: `Open`
- Notes: No committed automated smoke suite yet.
- Goal: maintain confidence while iterating quickly.
- Scope:
  - E2E smoke tests (include `data/input.txt`).
  - Coordinator flow sanity tests.
  - Citation integrity tests.
- Acceptance criteria:
  - CI/local test command runs core smoke suite.
  - Core regression suite catches broken stage order and citation mapping.
- Dependencies: BL-002, BL-007, BL-008
- Guided by decisions: D5

### BL-020 Decouple UI adapters from application logic
- Priority: `P1`
- Status: `Open`
- Notes: Core engines/storage are reusable, but stage orchestration and workflow execution still depend on `AgenticTUI`-shaped context.
- Goal: enable multiple UI frontends (TUI, web UI, API) over one shared orchestration core.
- Scope:
  - Introduce an application service/orchestrator layer independent of Textual/TUI widgets.
  - Replace direct `AgenticTUI` coupling in workflow/stage modules with a narrow runtime interface.
  - Keep TUI as one adapter implementation; add a web/API adapter without duplicating business logic.
  - Preserve persisted run state, approvals, and stage-gating semantics across adapters.
- Acceptance criteria:
  - Core stage progression and gate logic run without importing TUI classes.
  - TUI behavior remains functionally equivalent after extraction.
  - A non-TUI entrypoint (web/API) can initialize/resume runs and trigger stage actions using the same core orchestration.
- Dependencies: BL-018, BL-019
- Guided by decisions: D1, D2, D7

## P2

### BL-015 Dynamic agent archetype expansion
- Priority: `P2`
- Status: `Open`
- Goal: support richer specialist roles based on plan complexity.
- Scope:
  - Programmatic creation of specialized agents (analyst, copywriter variant, fact-checker).
  - Capability registry and selection heuristics.
- Acceptance criteria:
  - Coordinator can instantiate and assign at least one additional dynamic archetype.
- Dependencies: BL-005, BL-007
- Guided by decisions: D1

### BL-016 Quality optimization loop
- Priority: `P2`
- Status: `Open`
- Goal: improve writing quality and consistency over baseline pass criteria.
- Scope:
  - Heuristic or model-based rewrite strategies by rubric dimension.
  - Optional extra revision round when score is near threshold.
- Acceptance criteria:
  - Measurable rubric improvement in at least one dimension on sample runs.
- Dependencies: BL-007
- Guided by decisions: D3, D5

### BL-017 Export formats and publishing helpers
- Priority: `P2`
- Status: `Open`
- Goal: improve downstream publishing workflow.
- Scope:
  - Export to plain text and markdown with references.
  - Optional LinkedIn-optimized formatting preset.
- Acceptance criteria:
  - Exported artifact preserves citations and reference mapping.
- Dependencies: BL-009
- Guided by decisions: D7

## Suggested build sequence
1. BL-004, BL-001, BL-002
2. BL-003, BL-005
3. BL-006, BL-007, BL-008
4. BL-009 (end-to-end usable PoC milestone)
5. BL-018, BL-019 (modularization + debt hardening)
6. BL-010 through BL-014, BL-020
7. BL-015 through BL-017
