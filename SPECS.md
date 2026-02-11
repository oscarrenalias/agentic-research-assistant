# Agentic Tasks and Research system[SPECS.md](SPECS.md) 

# Objective
This is proof of concept app that will help me write posts in linkedin, but I'd like my posts to be well researched, structured, with relevant data references as opposed to the usual AI-generated drivel that is usually posted there. 

The purpose is to demonstrate how agents can work as human team mates in order to accomplish a specific task together – in this case, a project manager overseeing the process, researchers/analysts to provide and validate the required data, a copywriter (one or more) to write and review text, and so on.

# Deeper dive
In order to succeed in this activity, I'd like to build a multi-agent system that, given a source text (in a file) with some key points, ideas, links to relevant information, youtube videos, etc, creates the required content for me. 

There will be a set of fixed agents that will always exist, and a set of agents that will be created programmatically depending on the purpose, as well as a coordinator agent that will manage the process flow.

There is a shared text chat that all agents have access to, that is used to communicate with each other, e.g., from the coordinator agent to issue a task to a dedicated agent, from the user, or between them if needed in order to work together to accomplish a task.

Agents communicate in plain English with each other as well as with the user.

## The coordinator agent
The coordinator agent will:

1. process the problem statement, make sure it's clear (and ask clarification questions from the user if it isn't)
2. determine how to break down the problem into manageable steps that will be carried out by specific agents, e.g., writing agent, research agent, review agents, and so on. This list of agents should be totally dynamic.
3. Validate the plan from the user

## Common sub-agents (static/always there)
- Review agent: reviews written content as provided and returns feedback

## Archetypical agents (dynamic)
Although the overall type, purpose and number of agents will be dynamic based on the plan devised by the coordinator agent, we expect that there will be a set of archetypes for these dynamic agents depending on their purpose:

- Data research agent: given a link, text, or data points, will analyze the data and provide specific analysis
- Writing agent: receives input with draft text, refines it and makes it cohesive

## Agent to Agent communication
- Each sub-agent should be able to identify its own activities from the chat
- Sub-agents are allowed to ask for clarifications from the main agent as required, via the common chat

To support a shared chat while keeping execution deterministic, all agent messages must use a standard envelope and routing rules.

## Communication protocol (shared chat)

### Message envelope (required fields)
Every message posted in shared chat must include:
- `msg_id`: unique id for traceability
- `from`: sender agent id (or `user`)
- `to`: target agent id, `coordinator`, or `broadcast`
- `type`: one of `task`, `question`, `result`, `review`, `decision`, `status`
- `stage`: current pipeline stage (`Ingest`, `Research`, `Outline`, `Draft`, `Critique`, `Revise`, `Final`)
- `task_id`: task identifier (required for all task-related messages)
- `reply_to`: optional parent message id when responding
- `priority`: `low`, `normal`, or `high`
- `timestamp`: ISO-8601 timestamp

### Routing and ownership rules
- `broadcast` is only for announcements and status updates.
- Executable work must never be broadcast.
- Every executable task must target exactly one owner via `to=<agent_id>`.
- If a message has `type=task`, the recipient is accountable for either completing it or returning a blocker.

### Task and result contract
- `task` messages must include:
  - `objective`
  - `input_refs` (ids/links/files to use)
  - `output_schema` (required structure of the response)
  - `deadline` (or timeout)
  - `done_when` (acceptance criteria)
- `result` messages must include:
  - same `task_id`
  - `output` matching `output_schema`
  - `confidence`
  - `open_risks` (if any)

### Coordinator authority and stage control
- Only the coordinator can transition stage state (for example, `Research -> Outline`).
- Sub-agents can propose transitions or escalations, but cannot self-advance the pipeline.
- Coordinator resolves conflicts (duplicate claims, contradictory evidence, missing inputs).

### Clarification and failure handling
- Sub-agents can send `question` messages to coordinator or peer agents when requirements are ambiguous.
- If blocked, the assignee sends `status` with blocker reason and needed input.
- If no response is received before deadline, coordinator can retry, reassign, or reduce scope.

### Shared-chat noise model
- Keep the full shared chat log for transparency and "team-like" collaboration.
- Maintain per-agent filtered inbox views derived from the same log for deterministic task pickup.

# Pipeline stages and handoffs
To avoid ambiguity between agents, the system should follow a fixed high-level pipeline. Agent selection can remain dynamic, but stage outputs must be deterministic and structured.

The coordinator agent will keep this flow under its control as a ledger, and will use it to keep track of where we are in the process, what activities are completed, in flight or not started.

## Stages

1. Ingest
- Owner: coordinator agent
- Input: user brief + optional source file(s) with notes, links, and ideas
- Output: normalized task package with objective, audience, tone, constraints, and list of source candidates

2. Research
- Owner: one or more data research agents
- Input: normalized task package + source candidates
- Output: evidence pack with extracted claims, supporting data points, and source metadata (title, link, date, confidence)

3. Outline
- Owner: writing agent (or planner agent if created)
- Input: normalized task package + evidence pack
- Output: post outline with hook, key sections, argument flow, and mapped evidence per section

4. Draft
- Owner: writing agent
- Input: approved outline + evidence pack
- Output: first draft of the post, including inline attribution markers to evidence items

5. Critique
- Owner: review agent
- Input: first draft + original objective + evidence pack
- Output: structured feedback with issues grouped by clarity, factual grounding, tone, and structure

6. Revise
- Owner: writing agent
- Input: critique feedback + first draft
- Output: revised draft + short changelog explaining which review points were applied

7. Final
- Owner: coordinator agent
- Input: revised draft + changelog
- Output: final post text + final reference list

Notes:
- A stage cannot start until its required input contract is satisfied.
- Coordinator is responsible for retries, clarifications, and resolving conflicts between agent outputs.

# Source and citation rules
These rules define what "well researched" means for the system.

## Source quality tiers
- Tier 1 (preferred): primary and official sources (original studies, company reports, government/institution data, product docs, transcripts).
- Tier 2 (acceptable with caution): reputable secondary analysis (well-known media, industry analysis, expert commentary).
- Tier 3 (avoid unless explicitly requested): opinion-only posts, unverified social posts, anonymous summaries.

## Minimum evidence requirements per post
- At least 3 unique sources total.
- At least 2 sources must be Tier 1 when available.
- Every factual or numerical claim must map to at least one source id in the evidence pack.
- If a claim cannot be verified, the draft must mark it as unverified or remove it.

## Freshness and relevance rules
- Time-sensitive topics (AI tools, markets, regulations, product updates): prefer sources from the last 12 months.
- Evergreen concepts (principles, historical context): older sources allowed if still authoritative.
- If sources conflict, include both perspectives and mark confidence level.

## Citation format and traceability
- Evidence pack format per source:
  - `source_id`, `title`, `url`, `publisher`, `published_at`, `retrieved_at`, `tier`, `confidence`, `key_claims`.
- Draft and final output must include inline markers like `[S1]`, `[S2]` near supported claims.
- Final post output must append a reference list mapping markers to links and titles.

## Disallowed citation behavior
- No fabricated links, titles, dates, quotes, or statistics.
- No citation marker without a corresponding source in the evidence pack.
- No claim presented as fact if only supported by Tier 3 sources.

# Quality rubric
The review agent should score each draft before approval for final output.

## Scoring dimensions (0-5 each)
1. Factual accuracy
- 0: multiple unsupported or incorrect claims
- 3: mostly correct, minor weakly supported points
- 5: all factual claims supported and internally consistent

2. Evidence quality
- 0: weak or missing sources
- 3: acceptable sources, some gaps in quality/freshness
- 5: strong, relevant, and well-balanced sources with traceability

3. Structure and coherence
- 0: unclear flow, hard to follow
- 3: understandable but uneven transitions or focus
- 5: clear narrative arc with logical progression and strong flow

4. Clarity and readability
- 0: verbose/unclear writing
- 3: mostly clear with some awkward phrasing
- 5: concise, direct, and easy to scan

5. Tone and audience fit
- 0: mismatched tone for LinkedIn/professional audience
- 3: generally appropriate with minor mismatch
- 5: clearly tailored to target audience and intent

6. Originality and insight
- 0: generic AI-style summary with no point of view
- 3: some useful framing but limited novelty
- 5: clear angle, synthesis, and actionable insight

## Pass/fail policy
- Maximum score: 30
- Pass threshold: at least 24/30
- Hard gates (must pass regardless of score):
  - Factual accuracy >= 4
  - Evidence quality >= 4
  - Zero fabricated citations
- If a draft fails, review agent returns prioritized revision tasks and the flow returns to Revise.

# Outcome

The outcome should be a well-crafted article, for posting in LinkedIn or other reputable site.

# Technical specs
- Language and runtime: Python
- Build tool: uv
- Agent framework: LangChain (and LangGraph if needed)
- Inference provider: OpenAI 

# Execution-level decisions (iterative)
This section captures implementation decisions as we finalize them.

## Decision 1: Runtime architecture
- Status: decided
- Choice: single-process coordinator + background workers (option 2)
- Rationale:
  - Supports parallel execution of agent tasks to reduce end-to-end runtime.
  - Keeps complexity lower than a fully distributed multi-service setup.
  - Provides a clear migration path to distributed workers if load grows.
- v0 implementation notes:
  - Coordinator runs in the main process and controls stage state.
  - Long-running or independent tasks are dispatched to a local worker queue.
  - Research subtasks can run concurrently when inputs are independent.

## Decision 2: Persistence and data model
- Status: decided
- Choice: SQLite for v0 persistence
- Rationale:
  - Fast to set up for a local PoC.
  - Durable and queryable, unlike flat JSON-only storage.
  - Good enough for moderate concurrency with careful write patterns.
- v0 implementation notes:
  - Persist shared chat messages (full log) and filtered task views.
  - Persist task lifecycle (`queued`, `in_progress`, `blocked`, `done`, `failed`).
  - Persist stage ledger transitions and run-level metadata.
  - Persist evidence packs and citation mappings used in draft/final outputs.

## Decision 3: Error budgets and runtime limits
- Status: decided
- Choice: balanced profile (option B)
- Rationale:
  - Controls cost and latency without making runs too brittle.
  - Allows transient failures to recover while preserving quality gates.
- v0 implementation defaults:
  - Task timeout: 180s default, 300s for research tasks.
  - Retries: up to 2 retries with exponential backoff for transient failures.
  - Run task cap: maximum 20 agent tasks per run before coordinator must escalate.
  - Token budget: maximum 120k total tokens per run (all agent calls combined).
  - Failure policy:
    - Fail-fast for citation integrity violations and fabricated-source signals.
    - Retry for transient tool/model errors.
    - Escalate to user when budget cap is reached before quality gates pass.

## Decision 4: Human approval checkpoints
- Status: decided
- Choice: mixed checkpoints (mandatory + optional)
- Rationale:
  - Keeps user control at high-impact moments without slowing every stage.
  - Preserves momentum for iterative drafting and revision loops.
- v0 checkpoint policy:
  - Mandatory approval after plan creation (`Ingest` output).
  - Optional approval after `Outline` (auto-continue unless user enables this gate for the run).
  - Mandatory approval before `Final` output is emitted.

## Decision 5: Test strategy and acceptance criteria
- Status: decided
- Choice: lightweight v0 testing (not strict)
- Rationale:
  - Prioritizes delivery speed for PoC validation.
  - Covers critical behavior without heavy test investment upfront.
- v0 testing scope:
  - End-to-end smoke tests using representative inputs (including `data/input.txt`).
  - Basic coordinator flow checks (stage order, required approvals, terminal states).
  - Essential citation integrity checks (no missing source ids for citation markers).
  - Output-constraint assertions (required sections/fields exist), without enforcing exact wording.

## Decision 6: Security and safety defaults
- Status: decided
- Choice: pragmatic v0 safety baseline
- Rationale:
  - Reduces common agentic risks without adding heavy operational overhead.
  - Maintains flexibility for broad web research in PoC mode.
- v0 security defaults:
  - URL access policy:
    - Agents may browse public `http://` and `https://` URLs.
    - Block non-HTTP(S) schemes.
    - Block localhost and private-network targets.
  - Prompt-injection handling:
    - Treat all retrieved content as untrusted input.
    - Retrieved text cannot override system instructions, task contracts, or citation rules.
  - Citation hardening:
    - Every cited source must include `title`, `url`, and `retrieved_at`.
    - Reject outputs with unresolved citation markers.
  - Fabrication guard:
    - Reject outputs containing invented links, dates, or source metadata.
  - Secret hygiene:
    - Never write API keys, tokens, or environment secrets to shared chat or persisted logs.

## Decision 7: User interface mode
- Status: decided
- Choice: console text-based interface for v0
- Rationale:
  - Matches the chat-centric and pipeline-centric nature of the system.
  - Faster to build and iterate than a web UI for PoC goals.
  - Improves observability while debugging multi-agent orchestration.
- v0 interface scope:
  - Start a run from input text/file.
  - Show live stage status and task ledger summaries.
  - View shared chat log and per-agent filtered views.
  - Approve/reject checkpoints when required.
  - Inspect evidence pack and references before finalization.
  - Export final post + references to file.
- Suggested v0 command set:
  - `run <input_path>`
  - `status <run_id>`
  - `show-chat <run_id> [--agent <agent_id>]`
  - `show-sources <run_id>`
  - `approve <run_id> <checkpoint>`
  - `reject <run_id> <checkpoint> --reason "..."`
  - `export <run_id> --out <path>`
