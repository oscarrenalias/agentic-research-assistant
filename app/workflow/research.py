from __future__ import annotations

import asyncio
import concurrent.futures
import time
import uuid
from typing import TYPE_CHECKING

from app.domain.models import ChatMessage, TaskRecord, now_iso
from app.services.sources import fetch_source_material, infer_source_tier, maybe_url

if TYPE_CHECKING:
    from app.tui import AgenticTUI


async def run_instruction_review_loop(app: AgenticTUI, tasks: list[TaskRecord]) -> None:
    if not tasks:
        return

    app._log_event("Instruction review loop started.")
    for task in tasks:
        brief = app.task_briefs.get(task.task_id, {})
        objective = brief.get("objective", "Extract one evidence-backed claim.")
        instructions = brief.get("instructions", "Provide claim and confidence.")
        source = task.input_ref

        review = await asyncio.to_thread(
            app.research_engine.review_task_instruction,
            task_objective=objective,
            task_instructions=instructions,
            source=source,
        )
        decision = str(review.get("decision", "clear")).lower()
        message = str(review.get("message", "Instructions look clear; I can proceed."))

        if decision == "question":
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent=task.owner,
                    to_agent="coordinator",
                    message_type="question",
                    stage="Research",
                    priority="normal",
                    timestamp=now_iso(),
                    task_id=task.task_id,
                    content=message,
                )
            )

            clarification = (
                "Clarification: focus on one concrete claim directly tied to the task objective; "
                "include one supporting note and confidence from 0 to 1."
            )
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="coordinator",
                    to_agent=task.owner,
                    message_type="status",
                    stage="Research",
                    priority="normal",
                    timestamp=now_iso(),
                    task_id=task.task_id,
                    content=clarification,
                )
            )
            updated_instructions = f"{instructions} {clarification}"
            app.task_briefs[task.task_id]["instructions"] = updated_instructions
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent=task.owner,
                    to_agent="coordinator",
                    message_type="status",
                    stage="Research",
                    priority="normal",
                    timestamp=now_iso(),
                    task_id=task.task_id,
                    content="Thanks, clarification received. I can proceed.",
                )
            )
        else:
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent=task.owner,
                    to_agent="coordinator",
                    message_type="status",
                    stage="Research",
                    priority="normal",
                    timestamp=now_iso(),
                    task_id=task.task_id,
                    content=message,
                )
            )
    app._log_event("Instruction review loop complete.")


def run_research_subtask(app: AgenticTUI, task: TaskRecord) -> dict[str, object]:
    time.sleep(0.05)
    objective = app.package.objective if app.package else ""
    audience = app.package.audience if app.package else ""
    tone = app.package.tone if app.package else ""
    constraints = app.package.constraints if app.package else []
    brief = app.task_briefs.get(task.task_id, {})
    task_objective = brief.get("objective", "Extract one evidence-backed claim.")
    task_instructions = brief.get("instructions", "Provide claim with confidence and caveats.")
    source_payload = fetch_source_material(task.input_ref)
    analysis = app.research_engine.analyze_source(
        source_ref=str(source_payload.get("source_ref", task.input_ref)),
        source_material=str(source_payload.get("source_material", task.input_ref)),
        objective=objective,
        audience=audience,
        tone=tone,
        constraints=constraints,
        task_objective=task_objective,
        task_instructions=task_instructions,
    )
    return {
        "source_ref": task.input_ref,
        "source_url": str(source_payload.get("url", "")),
        "source_title": str(source_payload.get("title", "")),
        "source_publisher": str(source_payload.get("publisher", "")),
        "source_published_at": str(source_payload.get("published_at", "")),
        "source_retrieved_at": str(source_payload.get("retrieved_at", now_iso())),
        "fetch_status": str(source_payload.get("fetch_status", "unknown")),
        "claim": analysis["claim"],
        "evidence_note": analysis["evidence_note"],
        "confidence": float(analysis["confidence"]),
        "risk_flags": analysis["risk_flags"],
    }


async def execute_research_parallel(app: AgenticTUI) -> dict[str, object]:
    if not app.state:
        return {"summary": "No state", "sources": [], "claims": []}

    candidates: list[str] = []
    if app.package:
        candidates = list(app.package.source_candidates)
    if not candidates:
        candidates = ["No explicit source provided"]

    plan_tasks = app.coordinator_plan.get("analyst_tasks", [])
    inferred_tasks: list[dict[str, str]] = []
    if isinstance(plan_tasks, list):
        for i, item in enumerate(plan_tasks):
            if not isinstance(item, dict):
                continue
            inferred_tasks.append(
                {
                    "agent_id": str(item.get("agent_id", f"research_agent_{i+1}")),
                    "objective": str(item.get("objective", "Extract one evidence-backed claim.")),
                    "source_hint": str(item.get("source_hint", "")),
                    "instructions": str(item.get("instructions", "Provide one claim plus confidence.")),
                    "priority": str(item.get("priority", "normal")),
                }
            )

    if not inferred_tasks:
        for i, source in enumerate(candidates[: min(6, len(candidates))]):
            inferred_tasks.append(
                {
                    "agent_id": f"research_agent_{(i % 3) + 1}",
                    "objective": "Extract one evidence-backed claim relevant to objective.",
                    "source_hint": source,
                    "instructions": "Provide claim, note, and confidence.",
                    "priority": "normal",
                }
            )

    capped = inferred_tasks[: min(8, len(inferred_tasks))]
    tasks: list[TaskRecord] = []
    for i, spec in enumerate(capped):
        owner = spec["agent_id"] or f"research_agent_{(i % 3) + 1}"
        source = spec["source_hint"] or candidates[i % len(candidates)]
        record = TaskRecord(
            task_id=str(uuid.uuid4()),
            run_id=app.state.run_id,
            stage="Research",
            owner=owner,
            status="queued",
            input_ref=source,
        )
        app.state.tasks.append(record)
        tasks.append(record)
        app.task_briefs[record.task_id] = {
            "objective": spec["objective"],
            "instructions": spec["instructions"],
        }
        if app.repo is not None:
            app.repo.upsert_task(record)

    app._render_tasks()
    app._log_event(f"Research fan-out: queued {len(tasks)} subtasks.")
    app._post_chat_message(
        ChatMessage(
            msg_id=str(uuid.uuid4()),
            from_agent="coordinator",
            to_agent="broadcast",
            message_type="status",
            stage="Research",
            priority="normal",
            timestamp=now_iso(),
            content=f"Research fan-out started with {len(tasks)} subtasks.",
        )
    )
    for task in tasks:
        brief = app.task_briefs.get(task.task_id, {})
        app._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="coordinator",
                to_agent=task.owner,
                message_type="task",
                stage="Research",
                priority="normal",
                timestamp=now_iso(),
                task_id=task.task_id,
                content=app._format_task_instruction_message(
                    brief.get("objective", "Extract one evidence-backed claim."),
                    brief.get("instructions", "Provide claim and confidence."),
                    task.input_ref,
                ),
            )
        )

    await run_instruction_review_loop(app, tasks)

    async def run_one(pool: concurrent.futures.ThreadPoolExecutor, task: TaskRecord) -> tuple[TaskRecord, dict[str, object] | None, Exception | None]:
        app._set_task_status(task, status="in_progress")
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(pool, run_research_subtask, app, task)
            return task, result, None
        except Exception as exc:  # noqa: BLE001
            return task, None, exc

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(tasks))) as pool:
        pending = [run_one(pool, task) for task in tasks]
        results = await asyncio.gather(*pending)

    claims: list[dict[str, object]] = []
    source_entries: list[dict[str, object]] = []
    source_id_counter = 1
    for task, result, err in results:
        if err is None and result is not None:
            app._set_task_status(task, status="done", output=result)
            app._log_event(f"Research subtask done: {task.task_id[:8]} by {task.owner}")
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent=task.owner,
                    to_agent="coordinator",
                    message_type="result",
                    stage="Research",
                    priority="normal",
                    timestamp=now_iso(),
                    task_id=task.task_id,
                    content=f"Claim extracted (confidence={result.get('confidence', 0.0)}).",
                )
            )
            source_ref = str(result.get("source_ref", task.input_ref))
            url = str(result.get("source_url", "")) or (maybe_url(source_ref) or "")
            title = str(result.get("source_title", "")) or source_ref[:120]
            publisher = str(result.get("source_publisher", ""))
            published_at = str(result.get("source_published_at", ""))
            retrieved_at = str(result.get("source_retrieved_at", now_iso()))
            source_id = f"S{source_id_counter}"
            source_id_counter += 1
            source_entries.append(
                {
                    "source_id": source_id,
                    "title": title,
                    "url": url,
                    "publisher": publisher,
                    "published_at": published_at,
                    "retrieved_at": retrieved_at,
                    "tier": infer_source_tier(source_ref),
                    "confidence": float(result.get("confidence", 0.5)),
                    "key_claims": [str(result.get("claim", ""))],
                    "fetch_status": str(result.get("fetch_status", "unknown")),
                }
            )
            claims.append(
                {
                    "source_id": source_id,
                    "source_ref": source_ref,
                    "claim": str(result.get("claim", "")),
                    "evidence_note": str(result.get("evidence_note", "")),
                    "confidence": float(result.get("confidence", 0.5)),
                    "risk_flags": [str(x) for x in result.get("risk_flags", [])],
                }
            )
        else:
            app._set_task_status(task, status="failed", error=str(err))
            app._log_event(f"Research subtask failed: {task.task_id[:8]} ({err})")
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent=task.owner,
                    to_agent="coordinator",
                    message_type="status",
                    stage="Research",
                    priority="high",
                    timestamp=now_iso(),
                    task_id=task.task_id,
                    content=f"Task failed: {err}",
                )
            )

    done_count = sum(1 for task in tasks if task.status == "done")
    app._log_event(f"Research fan-in complete: {done_count}/{len(tasks)} succeeded.")
    return {
        "summary": f"Parallel research complete: {done_count}/{len(tasks)} subtasks succeeded.",
        "sources": source_entries,
        "claims": claims,
    }
