# Agentic Tasks

Console-first interactive app (TUI) for orchestrating a multi-agent content workflow.

## Run
- Install dependencies with `uv`.
- Start the app:
  - `uv run python main.py`
- Use a custom brief file:
  - `uv run python main.py --input data/input.txt`
- Use a custom SQLite database path:
  - `uv run python main.py --db .agentic_tasks.db`
- Resume an existing run:
  - `uv run python main.py --run-id <run_id>`

## LangChain research mode
- The Coordinator and Research stages use LangChain + OpenAI when `OPENAI_API_KEY` is set.
- Optional model override:
  - `RESEARCH_MODEL` (default: `gpt-4o-mini`)
  - `COORDINATOR_MODEL` (defaults to `RESEARCH_MODEL`, then `gpt-4o-mini`)
- Without `OPENAI_API_KEY`, the app stays usable in fallback mode with lower-confidence heuristic claims.

## TUI controls
- `n`: Advance stage
- `a`: Approve required checkpoint
- `d`: Add demo agent status message
- `q`: Quit
- Type in the input bar and press Enter to send a user message to coordinator.
- Type `/plan` in chat to have coordinator post the current structured plan in the chat stream.
- Research stage now fans out into parallel research subtasks; progress appears in the Task Ledger panel.
- Before Ingest approval, natural-language input is treated as coordinator feedback and triggers plan revision.
- Before Ingest approval, coordinator also uses inference to classify your intent (`approve` vs `iterate`) from natural language.
- After Outline completion, approval is required before Draft, and natural-language feedback triggers coordinator-led outline iteration.
- During outline iteration, coordinator checks evidence sufficiency and can run targeted supplemental research or a full Research refresh before regenerating the outline.
- Coordinator plan now includes an explicit execution strategy and priority rationale, visible in chat.
- Chat messages support markdown rendering for lightweight formatting.

## Notes
- Ingest is initialized automatically from the input brief.
- Research cannot start until Ingest approval is granted.
- Final cannot start until Final approval is granted.
- Run state is persisted in SQLite:
  - stage statuses and approvals
  - artifacts (including normalized task package and stage outputs)
  - shared chat messages
  - event log entries
  - task ledger (`queued`, `in_progress`, `done`, `failed`)
