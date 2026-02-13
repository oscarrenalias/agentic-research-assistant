from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

from app.domain.models import ChatMessage, TaskRecord, now_iso

if TYPE_CHECKING:
    from app.tui import AgenticTUI


async def advance_stage(app: AgenticTUI) -> None:
    if not app.state:
        return

    next_stage = app._next_stage()
    if not next_stage:
        app._log_event("Run already complete.")
        return

    if not app._start_stage(next_stage):
        app._render_all()
        return

    stage_task: TaskRecord | None = None
    try:
        if next_stage == "Research":
            output = await app._execute_research_parallel()
        elif next_stage == "Outline":
            stage_task = TaskRecord(
                task_id=str(uuid.uuid4()),
                run_id=app.state.run_id,
                stage="Outline",
                owner="writing_agent_1",
                status="queued",
                input_ref="coordinator_outline_assignment",
            )
            app.state.tasks.append(stage_task)
            if app.repo is not None:
                app.repo.upsert_task(stage_task)
            app._set_task_status(stage_task, status="in_progress")
            objective = app.package.objective if app.package else ""
            audience = app.package.audience if app.package else ""
            tone = app.package.tone if app.package else ""
            evidence = app.state.artifacts.get("evidence_pack", {}) if app.state else {}
            claims = evidence.get("claims", []) if isinstance(evidence, dict) else []
            claim_count = len(claims) if isinstance(claims, list) else 0

            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="coordinator",
                    to_agent="writing_agent_1",
                    message_type="status",
                    stage="Outline",
                    priority="normal",
                    timestamp=now_iso(),
                    content=(
                        "Outline assignment:\n"
                        f"- Objective: {objective[:180] or 'n/a'}\n"
                        f"- Audience: {audience[:120] or 'n/a'}\n"
                        f"- Tone: {tone[:120] or 'n/a'}\n"
                        f"- Evidence claims available: {claim_count}"
                    ),
                )
            )
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="writing_agent_1",
                    to_agent="coordinator",
                    message_type="status",
                    stage="Outline",
                    priority="normal",
                    timestamp=now_iso(),
                    content="Acknowledged. Building an outline mapped to evidence now.",
                )
            )
            output = await asyncio.to_thread(app._outline_stage_output)
            hook = str(output.get("hook", "")).strip() if isinstance(output, dict) else ""
            sections = output.get("sections", []) if isinstance(output, dict) else []
            section_list = [str(x) for x in sections[:8]] if isinstance(sections, list) else []
            sections_text = ", ".join(section_list) if section_list else "n/a"
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="writing_agent_1",
                    to_agent="coordinator",
                    message_type="status",
                    stage="Outline",
                    priority="normal",
                    timestamp=now_iso(),
                    content=(
                        "Outline delivered:\n"
                        f"- Hook: {hook or 'n/a'}\n"
                        f"- Sections: {sections_text}"
                    ),
                )
            )
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="coordinator",
                    to_agent="user",
                    message_type="question",
                    stage="Outline",
                    priority="high",
                    timestamp=now_iso(),
                    content=(
                        "Outline is ready for your review. "
                        "Share feedback to iterate (structure/evidence/focus), "
                        "or approve outline to move to Draft."
                    ),
                )
            )
            if stage_task is not None:
                app._set_task_status(stage_task, status="done", output=output if isinstance(output, dict) else None)
        elif next_stage == "Draft":
            stage_task = TaskRecord(
                task_id=str(uuid.uuid4()),
                run_id=app.state.run_id,
                stage="Draft",
                owner="writing_agent_1",
                status="queued",
                input_ref="coordinator_draft_assignment",
            )
            app.state.tasks.append(stage_task)
            if app.repo is not None:
                app.repo.upsert_task(stage_task)
            app._set_task_status(stage_task, status="in_progress")
            outline = app.state.artifacts.get("approved_outline", {}) if app.state else {}
            sections = outline.get("sections", []) if isinstance(outline, dict) else []
            section_count = len(sections) if isinstance(sections, list) else 0
            evidence = app.state.artifacts.get("evidence_pack", {}) if app.state else {}
            claims = evidence.get("claims", []) if isinstance(evidence, dict) else []
            claim_count = len(claims) if isinstance(claims, list) else 0
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="coordinator",
                    to_agent="writing_agent_1",
                    message_type="status",
                    stage="Draft",
                    priority="normal",
                    timestamp=now_iso(),
                    content=(
                        "Draft assignment:\n"
                        f"- Outline sections: {section_count}\n"
                        f"- Evidence claims available: {claim_count}\n"
                        "- Produce a complete first draft with citation markers."
                    ),
                )
            )
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="writing_agent_1",
                    to_agent="coordinator",
                    message_type="status",
                    stage="Draft",
                    priority="normal",
                    timestamp=now_iso(),
                    content="Acknowledged. Drafting now using the approved outline and evidence pack.",
                )
            )
            output = await asyncio.to_thread(app._draft_stage_output)
            draft_text = str(output or "")
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="writing_agent_1",
                    to_agent="coordinator",
                    message_type="status",
                    stage="Draft",
                    priority="normal",
                    timestamp=now_iso(),
                    content=f"Draft delivered. Approximate word count: {len(draft_text.split())}.",
                )
            )
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="coordinator",
                    to_agent="user",
                    message_type="question",
                    stage="Draft",
                    priority="high",
                    timestamp=now_iso(),
                    content=(
                        "Draft is ready for your review. "
                        "Share feedback to iterate (e.g., longer/shorter/expand topic), "
                        "or approve draft to move to Critique."
                    ),
                )
            )
            if stage_task is not None:
                app._set_task_status(
                    stage_task,
                    status="done",
                    output={"word_count": len(draft_text.split()), "char_count": len(draft_text)},
                )
        elif next_stage == "Critique":
            stage_task = TaskRecord(
                task_id=str(uuid.uuid4()),
                run_id=app.state.run_id,
                stage="Critique",
                owner="review_agent_1",
                status="queued",
                input_ref="coordinator_critique_assignment",
            )
            app.state.tasks.append(stage_task)
            if app.repo is not None:
                app.repo.upsert_task(stage_task)
            app._set_task_status(stage_task, status="in_progress")
            draft_payload = app.state.artifacts.get("first_draft", "") if app.state else ""
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="coordinator",
                    to_agent="review_agent_1",
                    message_type="status",
                    stage="Critique",
                    priority="normal",
                    timestamp=now_iso(),
                    content=(
                        "Critique assignment:\n"
                        f"- Draft length (chars): {len(str(draft_payload))}\n"
                        "- Evaluate quality gates and citation integrity."
                    ),
                )
            )
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="review_agent_1",
                    to_agent="coordinator",
                    message_type="status",
                    stage="Critique",
                    priority="normal",
                    timestamp=now_iso(),
                    content="Acknowledged. Running draft evaluation now.",
                )
            )
            output = await asyncio.to_thread(app._critique_stage_output)
            passed = bool(output.get("pass", False)) if isinstance(output, dict) else False
            score = int(output.get("total_score", 0)) if isinstance(output, dict) else 0
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="review_agent_1",
                    to_agent="coordinator",
                    message_type="status",
                    stage="Critique",
                    priority="normal",
                    timestamp=now_iso(),
                    content=f"Critique delivered. pass={passed}, total_score={score}.",
                )
            )
            if stage_task is not None:
                app._set_task_status(stage_task, status="done", output=output if isinstance(output, dict) else None)
        elif next_stage == "Revise":
            stage_task = TaskRecord(
                task_id=str(uuid.uuid4()),
                run_id=app.state.run_id,
                stage="Revise",
                owner="writing_agent_1",
                status="queued",
                input_ref="coordinator_revision_assignment",
            )
            app.state.tasks.append(stage_task)
            if app.repo is not None:
                app.repo.upsert_task(stage_task)
            app._set_task_status(stage_task, status="in_progress")
            critique_payload = app.state.artifacts.get("critique_feedback", {}) if app.state else {}
            critique_pass = bool(critique_payload.get("pass", False)) if isinstance(critique_payload, dict) else False
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="coordinator",
                    to_agent="writing_agent_1",
                    message_type="status",
                    stage="Revise",
                    priority="normal",
                    timestamp=now_iso(),
                    content=(
                        "Revision assignment:\n"
                        f"- Current critique pass state: {critique_pass}\n"
                        "- Revise draft to satisfy all quality gates."
                    ),
                )
            )
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="writing_agent_1",
                    to_agent="coordinator",
                    message_type="status",
                    stage="Revise",
                    priority="normal",
                    timestamp=now_iso(),
                    content="Acknowledged. Applying critique and running revision rounds.",
                )
            )
            output = await asyncio.to_thread(app._revise_stage_output)
            if isinstance(output, dict):
                final_critique = output.get("final_critique")
                if isinstance(final_critique, dict):
                    app._persist_artifact("critique_feedback", final_critique)
            attempts = int(output.get("revision_attempts", 0)) if isinstance(output, dict) else 0
            passed = bool(output.get("passes_quality_gate", False)) if isinstance(output, dict) else False
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="writing_agent_1",
                    to_agent="coordinator",
                    message_type="status",
                    stage="Revise",
                    priority="normal",
                    timestamp=now_iso(),
                    content=f"Revision delivered. attempts={attempts}, passes_quality_gate={passed}.",
                )
            )
            if stage_task is not None:
                app._set_task_status(stage_task, status="done", output=output if isinstance(output, dict) else None)
            if isinstance(output, dict) and not bool(output.get("passes_quality_gate", False)):
                app._post_chat_message(
                    ChatMessage(
                        msg_id=str(uuid.uuid4()),
                        from_agent="coordinator",
                        to_agent="user",
                        message_type="status",
                        stage="Revise",
                        priority="high",
                        timestamp=now_iso(),
                        content=(
                            "Revise completed but quality gate still failing after max rounds. "
                            "Please adjust constraints/objective and retry."
                        ),
                    )
                )
            else:
                app._post_chat_message(
                    ChatMessage(
                        msg_id=str(uuid.uuid4()),
                        from_agent="coordinator",
                        to_agent="user",
                        message_type="question",
                        stage="Final",
                        priority="high",
                        timestamp=now_iso(),
                        content="Revision passed quality gates. Approve finalization when ready.",
                    )
                )
        elif next_stage == "Final":
            stage_task = TaskRecord(
                task_id=str(uuid.uuid4()),
                run_id=app.state.run_id,
                stage="Final",
                owner="writing_agent_1",
                status="queued",
                input_ref="coordinator_finalization_assignment",
            )
            app.state.tasks.append(stage_task)
            if app.repo is not None:
                app.repo.upsert_task(stage_task)
            app._set_task_status(stage_task, status="in_progress")
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="coordinator",
                    to_agent="writing_agent_1",
                    message_type="status",
                    stage="Final",
                    priority="normal",
                    timestamp=now_iso(),
                    content="Finalization assignment: produce final post and reference list from revised draft.",
                )
            )
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="writing_agent_1",
                    to_agent="coordinator",
                    message_type="status",
                    stage="Final",
                    priority="normal",
                    timestamp=now_iso(),
                    content="Acknowledged. Finalizing output package now.",
                )
            )
            output = await asyncio.to_thread(app._final_stage_output)
            references = output.get("references", []) if isinstance(output, dict) else []
            post_text = str(output.get("post_text", "")) if isinstance(output, dict) else ""
            ref_count = len(references) if isinstance(references, list) else 0
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="writing_agent_1",
                    to_agent="coordinator",
                    message_type="status",
                    stage="Final",
                    priority="normal",
                    timestamp=now_iso(),
                    content=f"Final package delivered. post_chars={len(post_text)}, references={ref_count}.",
                )
            )
            if stage_task is not None:
                app._set_task_status(stage_task, status="done", output=output if isinstance(output, dict) else None)
        else:
            output = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        if stage_task is not None:
            app._set_task_status(stage_task, status="failed", error=str(exc))
        app.state.stage_status[next_stage] = "failed"
        app._persist_run_status()
        app._log_event(f"Stage failed: {next_stage} ({exc})")
        app._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="coordinator",
                to_agent="user",
                message_type="status",
                stage=next_stage,
                priority="high",
                timestamp=now_iso(),
                content=f"{next_stage} failed: {exc}",
            )
        )
        app._render_all()
        return

    app._complete_stage(next_stage, output)
    app._post_chat_message(
        ChatMessage(
            msg_id=str(uuid.uuid4()),
            from_agent="coordinator",
            to_agent="broadcast",
            message_type="status",
            stage=next_stage,
            priority="normal",
            timestamp=now_iso(),
            content=f"{next_stage} stage completed.",
        )
    )
    app._render_all()
