from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

from app.domain.models import ChatMessage, now_iso

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

    try:
        if next_stage == "Research":
            output = await app._execute_research_parallel()
        elif next_stage == "Outline":
            output = await asyncio.to_thread(app._outline_stage_output)
        elif next_stage == "Draft":
            output = await asyncio.to_thread(app._draft_stage_output)
        elif next_stage == "Critique":
            output = await asyncio.to_thread(app._critique_stage_output)
        elif next_stage == "Revise":
            output = await asyncio.to_thread(app._revise_stage_output)
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
            output = await asyncio.to_thread(app._final_stage_output)
        else:
            output = {"ok": True}
    except Exception as exc:  # noqa: BLE001
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
