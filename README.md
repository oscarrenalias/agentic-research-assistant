# Agentic Tasks

A terminal-first, multi-stage research and writing workflow built with Textual.

The app ingests a brief, fans out research tasks, builds an outline, drafts content, runs critique/revision gates, and exports a final markdown artifact with references.

Please note that this app is vibe coded, it works but it was mostly intended for me to work with coordinator-agent agentic flows to execute a somewhat realistic process, in this case researching and writing articles.

Main use case is to produce articles for social media based on minimal guidance such as a title, a few key points about the objective, and some sources to get started.

## What it does
- Runs a staged workflow: `Ingest -> Research -> Outline -> Draft -> Critique -> Revise -> Final`.
- Persists run state in SQLite so you can resume by `run_id`.
- Uses coordinator + specialist engines (research, writing, review).
- Supports chat-driven approvals and iteration at key gates.
- Exports final output to markdown (`/export`).

## Requirements
- Python `>=3.14` (from `pyproject.toml`).
- `uv` for dependency management.
- Optional: `OPENAI_API_KEY` for inference-enabled mode.

## Install
```bash
uv sync
```

## Run
```bash
uv run python main.py
```

CLI options:
- `--input <path>`: input brief file (default: `data/input.txt`)
- `--db <path>`: SQLite path (default: `.agentic_tasks.db`)
- `--run-id <id>`: resume an existing run

Examples:
```bash
uv run python main.py --input "data/input - ai capex out of control?.txt"
uv run python main.py --db .agentic_tasks.db
uv run python main.py --run-id <run_id>
```

## Environment variables
`.env` is auto-loaded if present.

- `OPENAI_API_KEY`: enables model-backed coordinator/research/writing/review engines.
- `RESEARCH_MODEL` (default: `gpt-4o-mini`)
- `COORDINATOR_MODEL` (default: `RESEARCH_MODEL`, then `gpt-4o-mini`)
- `WRITING_MODEL` (default: `RESEARCH_MODEL`, then `gpt-4o-mini`)
- `REVIEW_MODEL` (default: `RESEARCH_MODEL`, then `gpt-4o-mini`)

Without `OPENAI_API_KEY`, the app stays usable with deterministic/fallback behavior, but output quality is lower.

## Input brief format
Provide a plain-text brief with section headers. Required fields are derived from:
- `Objective:`
- `Audience:`
- `Tone and style constraints:`
- `Draft output preference:` and/or `Questions to answer explicitly:`

Helpful optional fields:
- `Working title:` (or `Title:`)
- `Core points to explore:`
- `Potential sources to investigate:` (URLs are also auto-detected anywhere in the file)

If required fields are missing, startup fails with a validation error.

## TUI controls
Keyboard:
- `n`: advance to next stage
- `a`: approve current gate
- `d`: inject demo agent status message
- `Ctrl+D`: quit

## Slash commands
- `/help`
- `/plan`
- `/run`
- `/stages`
- `/events`
- `/ledger`
- `/sources`
- `/agents`
- `/inbox <agent_id>`
- `/agent <agent_id>`
- `/task <task_id_or_prefix>`
- `/approve`
- `/reject <reason>`
- `/export [path]`
- `/view compact|detailed`
- `/scope focus|all`
- `/internal on|off`
- `/progress on|off`

## Approval gates
User approval is required at:
- `Ingest`
- `Outline`
- `Draft`
- `Final`

At each gate, natural-language feedback can trigger coordinator-led iteration before progressing.

## Persistence and exports
Run state is stored in SQLite, including:
- stage statuses and approvals
- artifacts per stage
- shared chat messages
- task ledger and event log

Markdown exports are written to `exports/` by default (or a custom path via `/export <path>`).
