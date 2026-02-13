from __future__ import annotations

from pathlib import Path
import uuid
from typing import TYPE_CHECKING

from app.domain.models import ChatMessage, now_iso

if TYPE_CHECKING:
    from app.tui import AgenticTUI


async def handle_slash_command(app: AgenticTUI, text: str) -> bool:
    command, _, remainder = text.partition(" ")
    cmd = command.strip().lower()
    arg = remainder.strip()

    if cmd in {"/help", "/commands"}:
        app._post_coordinator_markdown(app._format_help_markdown(), stage=app._next_stage() or "Ingest")
        return True

    if cmd == "/plan":
        app._post_coordinator_markdown(app._format_coordinator_plan_markdown(app.coordinator_plan), stage="Ingest")
        app._set_status("Coordinator plan posted in chat.", level="done")
        return True

    if cmd == "/run":
        app._post_coordinator_markdown(app._command_run_summary())
        return True

    if cmd == "/stages":
        app._post_coordinator_markdown(app._command_stages())
        return True

    if cmd == "/events":
        app._post_coordinator_markdown(app._command_events())
        return True

    if cmd == "/ledger":
        app._post_coordinator_markdown(app._command_ledger())
        return True

    if cmd == "/sources":
        app._post_coordinator_markdown(app._command_sources())
        return True

    if cmd == "/agents":
        app._post_coordinator_markdown(app._command_agents_summary())
        return True

    if cmd == "/inbox":
        if not arg:
            app._post_coordinator_markdown("Usage: `/inbox <agent_id>`")
            return True
        app._post_coordinator_markdown(app._command_inbox(arg))
        return True

    if cmd == "/agent":
        if not arg:
            app._post_coordinator_markdown("Usage: `/agent <agent_id>`")
            return True
        app._post_coordinator_markdown(app._command_agent_details(arg))
        return True

    if cmd == "/task":
        if not arg:
            app._post_coordinator_markdown("Usage: `/task <task_id_or_prefix>`")
            return True
        app._post_coordinator_markdown(app._command_task_details(arg))
        return True

    if cmd == "/view":
        value = arg.lower()
        if value not in {"compact", "detailed"}:
            app._post_coordinator_markdown("Usage: `/view compact` or `/view detailed`")
            return True
        app.chat_view_mode = value
        app._post_coordinator_markdown(f"Chat view set to `{value}`.")
        return True

    if cmd == "/scope":
        value = arg.lower()
        if value not in {"focus", "all"}:
            app._post_coordinator_markdown("Usage: `/scope focus` or `/scope all`")
            return True
        app.chat_scope_mode = value
        app._repaint_chat_log()
        app._post_coordinator_markdown(f"Chat scope set to `{value}`.")
        return True

    if cmd == "/internal":
        value = arg.lower()
        if value not in {"on", "off"}:
            app._post_coordinator_markdown("Usage: `/internal on` or `/internal off`")
            return True
        app.show_internal_messages = value == "on"
        app._repaint_chat_log()
        state = "on" if app.show_internal_messages else "off"
        app._post_coordinator_markdown(f"Internal messages set to `{state}`.")
        return True

    if cmd == "/progress":
        value = arg.lower()
        if value not in {"on", "off"}:
            app._post_coordinator_markdown("Usage: `/progress on` or `/progress off`")
            return True
        app.show_progress_updates = value == "on"
        app._repaint_chat_log()
        state = "on" if app.show_progress_updates else "off"
        app._post_coordinator_markdown(f"Progress updates set to `{state}`.")
        return True

    if cmd == "/approve":
        if app.state and not app.state.approvals.get("Ingest", False):
            app._approve_ingest("Plan approved. Continue.", auto_advance=True)
            await app.action_advance_stage()
            return True
        if app._outline_gate_pending():
            app.action_approve_gate()
            if app.state and app.state.approvals.get("Outline", False):
                await app.action_advance_stage()
            return True
        if (
            app.state
            and app.state.stage_status.get("Draft") == "completed"
            and app.state.stage_status.get("Critique") == "not_started"
            and not app.state.approvals.get("Draft", False)
        ):
            app.action_approve_gate()
            if app.state.approvals.get("Draft", False):
                await app.action_advance_stage()
            return True
        if app.state and app.state.stage_status.get("Revise") == "completed" and not app.state.approvals.get("Final", False):
            app.action_approve_gate()
            if app.state.approvals.get("Final", False):
                await app.action_advance_stage()
            return True
        app._post_coordinator_markdown("No pending approval gate.")
        return True

    if cmd == "/reject":
        reason = arg.strip() or "Rejected by user. Please revise."
        if app.state and not app.state.approvals.get("Ingest", False):
            await app._iterate_ingest_with_feedback(reason)
            return True
        if app._outline_gate_pending():
            await app._iterate_outline_with_feedback(reason)
            return True
        if (
            app.state
            and app.state.stage_status.get("Draft") == "completed"
            and app.state.stage_status.get("Critique") == "not_started"
            and not app.state.approvals.get("Draft", False)
        ):
            app._post_coordinator_markdown(
                "Draft approval remains pending. Share draft feedback in chat (e.g., shorter/longer/expand topic), "
                "and I will revise before moving to Critique."
            )
            return True
        if app.state and app.state.stage_status.get("Revise") == "completed" and not app.state.approvals.get("Final", False):
            app._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="user",
                    to_agent="coordinator",
                    message_type="decision",
                    stage="Final",
                    priority="high",
                    timestamp=now_iso(),
                    content=f"Final approval rejected: {reason}",
                )
            )
            app._post_coordinator_markdown("Understood. I will keep iterating before finalization.")
            return True
        app._post_coordinator_markdown("No pending approval gate to reject.")
        return True

    if cmd == "/export":
        path_arg = arg or f"exports/final-{(app.state.run_id[:8] if app.state else 'run')}.md"
        output = app._export_markdown(Path(path_arg))
        app._post_coordinator_markdown(output)
        return True

    app._post_coordinator_markdown(
        f"Unknown command: `{cmd}`\n\nUse `/help` to see available commands.",
        stage=app._next_stage() or "Ingest",
    )
    return True
