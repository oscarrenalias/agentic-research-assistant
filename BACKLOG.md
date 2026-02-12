# Backlog

This backlog is functionality-first. Decisions in `SPECS.md` are treated as implementation constraints, not backlog scope.

## Priority model
- `P0`: required for an end-to-end usable PoC.
- `P1`: important improvements that materially increase reliability and usability.
- `P2`: optional enhancements after core validation.

## P0

### BL-001 Ingest and normalize user brief
- Priority: `P0`
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
- Goal: operate the full system from terminal.
- Scope:
  - `run <input_path>`
  - `status <run_id>`
  - `show-chat <run_id> [--agent <agent_id>]`
  - `show-sources <run_id>`
  - `approve <run_id> <checkpoint>`
  - `reject <run_id> <checkpoint> --reason "..."`
  - `export <run_id> --out <path>`
- Acceptance criteria:
  - User can complete one end-to-end run via CLI only.
  - Mandatory approvals pause progression until explicit action.
- Dependencies: BL-002, BL-003, BL-004, BL-007
- Guided by decisions: D4, D7

## P1

### BL-010 Run budgets, retries, and failure handling
- Priority: `P1`
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

## P2

### BL-015 Dynamic agent archetype expansion
- Priority: `P2`
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
5. BL-010 through BL-014
6. BL-015 through BL-017
