from __future__ import annotations

import argparse
import asyncio
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Input, RichLog, Static

from app.domain.models import (
    STAGES,
    ChatMessage,
    NormalizedTaskPackage,
    RunState,
    TaskRecord,
    now_iso,
)
from app.engines import CoordinatorEngine, ResearchEngine, ReviewEngine, WritingEngine
from app.services.export import export_run_markdown
from app.storage.repository import RunRepository
from app.workflow.content import (
    critique_for_draft as workflow_critique_for_draft,
    critique_stage_output as workflow_critique_stage_output,
    draft_stage_output as workflow_draft_stage_output,
    evidence_pack as workflow_evidence_pack,
    final_stage_output as workflow_final_stage_output,
    outline_stage_output as workflow_outline_stage_output,
    revise_stage_output as workflow_revise_stage_output,
    validate_citation_integrity as workflow_validate_citation_integrity,
)
from app.workflow.research import (
    execute_research_parallel as workflow_execute_research_parallel,
    run_instruction_review_loop as workflow_run_instruction_review_loop,
    run_research_subtask as workflow_run_research_subtask,
)
from app.ui.presentation import (
    build_runtime_context as ui_build_runtime_context,
    command_agent_details as ui_command_agent_details,
    command_agents_summary as ui_command_agents_summary,
    command_inbox as ui_command_inbox,
    command_run_summary as ui_command_run_summary,
    command_sources as ui_command_sources,
    command_task_details as ui_command_task_details,
    format_coordinator_plan_markdown as ui_format_coordinator_plan_markdown,
    format_help_markdown as ui_format_help_markdown,
    format_json_block as ui_format_json_block,
    is_internal_message as ui_is_internal_message,
    is_progress_update as ui_is_progress_update,
    looks_like_markdown as ui_looks_like_markdown,
    post_coordinator_markdown as ui_post_coordinator_markdown,
    render_chat_header as ui_render_chat_header,
    repaint_chat_log as ui_repaint_chat_log,
    should_display_chat_message as ui_should_display_chat_message,
    type_icon as ui_type_icon,
    validate_chat_message as ui_validate_chat_message,
    write_chat_renderable as ui_write_chat_renderable,
)
from app.ui.runtime import (
    approve_ingest as ui_approve_ingest,
    complete_stage as ui_complete_stage,
    generate_coordinator_plan_async as ui_generate_coordinator_plan_async,
    initialize_run_async as ui_initialize_run_async,
    iterate_ingest_with_feedback as ui_iterate_ingest_with_feedback,
    log_event as ui_log_event,
    next_stage as ui_next_stage,
    persist_artifact as ui_persist_artifact,
    persist_run_status as ui_persist_run_status,
    post_chat_message as ui_post_chat_message,
    post_ingest_summary_and_approval_request as ui_post_ingest_summary_and_approval_request,
    render_all as ui_render_all,
    render_stages as ui_render_stages,
    render_summary as ui_render_summary,
    render_tasks as ui_render_tasks,
    restore_logs as ui_restore_logs,
    set_status as ui_set_status,
    start_stage as ui_start_stage,
)

# Load environment variables from .env automatically if present.
load_dotenv()


class AgenticTUI(App[None]):
    CSS = """
    Screen { layout: vertical; }
    #top { height: 1fr; }
    #left-pane, #right-pane {
        width: 1fr;
        border: round $panel;
        padding: 0 1;
    }
    #chat-pane {
        height: 1fr;
        border: round $panel;
        padding: 0 1;
    }
    #chat-log {
        height: 1fr;
    }
    #input-bar {
        height: auto;
    }
    #run-summary { height: 8; }
    #stages { height: 12; }
    #tasks { height: 12; }
    #event-log { height: 1fr; }
    """

    BINDINGS = [
        ("n", "advance_stage", "Advance Stage"),
        ("a", "approve_gate", "Approve Gate"),
        ("d", "demo_message", "Demo Msg"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, input_path: Path, db_path: Path, resume_run_id: str | None = None):
        super().__init__()
        self.input_path = input_path
        self.db_path = db_path
        self.resume_run_id = resume_run_id
        self.coordinator_engine = CoordinatorEngine()
        self.research_engine = ResearchEngine()
        self.writing_engine = WritingEngine()
        self.review_engine = ReviewEngine()
        self.coordinator_plan: dict[str, Any] = {}
        self.task_briefs: dict[str, dict[str, str]] = {}
        self._spinner_idx = 0
        self.chat_view_mode = "compact"
        self.chat_scope_mode = "focus"
        self.show_internal_messages = False
        self.show_progress_updates = True
        self.repo: RunRepository | None = None
        self.package: NormalizedTaskPackage | None = None
        self.state: RunState | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="top"):
            with Vertical(id="left-pane"):
                yield Static("Run not initialized", id="run-summary")
                yield DataTable(id="stages")
            with Vertical(id="right-pane"):
                yield Static("Task Ledger")
                yield DataTable(id="tasks")
                yield Static("Events")
                yield RichLog(id="event-log", wrap=True, highlight=True)
        with Vertical(id="chat-pane"):
            yield Static("Shared Chat")
            yield RichLog(id="chat-log", wrap=True, highlight=True)
            yield Input(placeholder="Type a message to coordinator and press Enter", id="input-bar")
        yield Footer()

    def on_mount(self) -> None:
        stages = self.query_one("#stages", DataTable)
        stages.add_columns("Stage", "Status", "Approval")
        tasks = self.query_one("#tasks", DataTable)
        tasks.add_columns("Task ID", "Owner", "Stage", "Status")
        self.repo = RunRepository(self.db_path)
        self.repo.init_schema()
        if not self.coordinator_engine.enabled and self.coordinator_engine.init_error:
            self.query_one("#event-log", RichLog).write(
                f"Coordinator engine fallback mode: {self.coordinator_engine.init_error}"
            )
        if not self.research_engine.enabled and self.research_engine.init_error:
            self.query_one("#event-log", RichLog).write(
                f"Research engine fallback mode: {self.research_engine.init_error}"
            )
        if not self.writing_engine.enabled and self.writing_engine.init_error:
            self.query_one("#event-log", RichLog).write(
                f"Writing engine fallback mode: {self.writing_engine.init_error}"
            )
        if not self.review_engine.enabled and self.review_engine.init_error:
            self.query_one("#event-log", RichLog).write(
                f"Review engine fallback mode: {self.review_engine.init_error}"
            )
        self.set_focus(self.query_one("#input-bar", Input))
        self._set_status("Initializing run and coordinator plan...", level="in_progress")
        self.run_worker(self._initialize_run_async(), exclusive=True, group="startup")

    def on_unmount(self) -> None:
        if self.repo is not None:
            self.repo.close()

    def _restore_logs(self) -> None:
        ui_restore_logs(self)

    async def _initialize_run_async(self) -> None:
        await ui_initialize_run_async(self)

    async def _generate_coordinator_plan_async(self) -> None:
        await ui_generate_coordinator_plan_async(self)

    def _post_ingest_summary_and_approval_request(self) -> None:
        ui_post_ingest_summary_and_approval_request(self)

    def _set_status(self, text: str, *, level: str = "info") -> None:
        ui_set_status(self, text, level=level)

    def _approve_ingest(self, decision_text: str, *, auto_advance: bool = False) -> None:
        ui_approve_ingest(self, decision_text, auto_advance=auto_advance)

    async def _iterate_ingest_with_feedback(self, feedback_text: str) -> None:
        await ui_iterate_ingest_with_feedback(self, feedback_text)

    def _next_stage(self) -> str | None:
        return ui_next_stage(self)

    def _persist_run_status(self) -> None:
        ui_persist_run_status(self)

    def _log_event(self, message: str) -> None:
        ui_log_event(self, message)

    def _persist_artifact(self, name: str, value: object) -> None:
        ui_persist_artifact(self, name, value)

    def _render_summary(self, error: str | None = None) -> None:
        ui_render_summary(self, error=error)

    def _render_stages(self) -> None:
        ui_render_stages(self)

    def _render_all(self) -> None:
        ui_render_all(self)

    def _render_tasks(self) -> None:
        ui_render_tasks(self)

    def _start_stage(self, stage: str) -> bool:
        return ui_start_stage(self, stage)

    def _complete_stage(self, stage: str, output: object) -> None:
        ui_complete_stage(self, stage, output)

    def _post_chat_message(self, message: ChatMessage) -> bool:
        return ui_post_chat_message(self, message)

    @staticmethod
    def _format_coordinator_plan_markdown(plan: dict[str, Any]) -> str:
        return ui_format_coordinator_plan_markdown(plan)

    @staticmethod
    def _format_json_block(value: object) -> str:
        return ui_format_json_block(value)

    def _format_help_markdown(self) -> str:
        return ui_format_help_markdown()

    def _post_coordinator_markdown(self, content: str, *, stage: str | None = None) -> None:
        ui_post_coordinator_markdown(self, content, stage=stage)

    def _command_run_summary(self) -> str:
        return ui_command_run_summary(self)

    def _command_sources(self) -> str:
        return ui_command_sources(self)

    def _export_markdown(self, output_path: Path) -> str:
        return export_run_markdown(state=self.state, output_path=output_path)

    def _build_runtime_context(self) -> dict[str, Any]:
        return ui_build_runtime_context(self)

    def _command_agents_summary(self) -> str:
        return ui_command_agents_summary(self)

    def _command_agent_details(self, agent_id: str) -> str:
        return ui_command_agent_details(self, agent_id)

    def _command_inbox(self, agent_id: str) -> str:
        return ui_command_inbox(self, agent_id)

    def _validate_chat_message(self, message: ChatMessage) -> list[str]:
        return ui_validate_chat_message(self, message)

    def _command_task_details(self, task_key: str) -> str:
        return ui_command_task_details(self, task_key)

    async def _handle_slash_command(self, text: str) -> bool:
        from app.commands import handle_slash_command

        return await handle_slash_command(self, text)

    def _is_internal_message(self, message: ChatMessage) -> bool:
        return ui_is_internal_message(message)

    @staticmethod
    def _is_progress_update(message: ChatMessage) -> bool:
        return ui_is_progress_update(message)

    def _should_display_chat_message(self, message: ChatMessage) -> bool:
        return ui_should_display_chat_message(self, message)

    def _repaint_chat_log(self) -> None:
        ui_repaint_chat_log(self)

    @staticmethod
    def _type_icon(message_type: str, content: str) -> str:
        return ui_type_icon(message_type, content)

    @staticmethod
    def _looks_like_markdown(content: str) -> bool:
        return ui_looks_like_markdown(content)

    def _render_chat_header(self, message: ChatMessage) -> Text:
        return ui_render_chat_header(self, message)

    def _write_chat_renderable(self, message: ChatMessage) -> None:
        ui_write_chat_renderable(self, message)

    def _set_task_status(
        self,
        task: TaskRecord,
        *,
        status: str,
        output: dict[str, object] | None = None,
        error: str | None = None,
    ) -> None:
        task.status = status
        if status == "in_progress":
            task.started_at = now_iso()
        if status in {"done", "failed"}:
            task.completed_at = now_iso()
        if output is not None:
            task.output = output
        if error is not None:
            task.error = error
        if self.repo is not None:
            self.repo.upsert_task(task)
        self._render_tasks()

    @staticmethod
    def _format_task_instruction_message(objective: str, instructions: str, source: str) -> str:
        return (
            "Task assignment:\n"
            f"- Objective: {objective}\n"
            f"- Instructions: {instructions}\n"
            f"- Source candidate: {source}\n"
            "- Expected output: one evidence-backed claim + evidence note + confidence."
        )

    async def _run_instruction_review_loop(self, tasks: list[TaskRecord]) -> None:
        await workflow_run_instruction_review_loop(self, tasks)

    def _run_research_subtask(self, task: TaskRecord) -> dict[str, object]:
        return workflow_run_research_subtask(self, task)

    async def _execute_research_parallel(self) -> dict[str, object]:
        return await workflow_execute_research_parallel(self)

    def _evidence_pack(self) -> dict[str, object]:
        return workflow_evidence_pack(self)

    def _outline_stage_output(self) -> dict[str, object]:
        return workflow_outline_stage_output(self)

    def _draft_stage_output(self) -> str:
        return workflow_draft_stage_output(self)

    def _critique_for_draft(self, draft_text: str) -> dict[str, object]:
        return workflow_critique_for_draft(self, draft_text)

    def _validate_citation_integrity(self, draft_text: str, evidence_pack: dict[str, object]) -> dict[str, object]:
        return workflow_validate_citation_integrity(self, draft_text, evidence_pack)

    def _critique_stage_output(self) -> dict[str, object]:
        return workflow_critique_stage_output(self)

    def _revise_stage_output(self) -> dict[str, object]:
        return workflow_revise_stage_output(self)

    def _final_stage_output(self) -> dict[str, object]:
        return workflow_final_stage_output(self)

    async def action_advance_stage(self) -> None:
        from app.stages import advance_stage

        await advance_stage(self)

    def action_approve_gate(self) -> None:
        if not self.state:
            return

        if not self.state.approvals.get("Ingest", False):
            self._approve_ingest("Plan approved. Continue.")
            return

        if self.state.stage_status.get("Revise") == "completed" and not self.state.approvals.get("Final", False):
            self.state.approvals["Final"] = True
            self._persist_run_status()
            self._log_event("Approval granted: Final checkpoint.")
            self._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="user",
                    to_agent="coordinator",
                    message_type="decision",
                    stage="Final",
                    priority="normal",
                    timestamp=now_iso(),
                    content="Final approval granted.",
                )
            )
            self._render_all()
            return

        self._log_event("No approval gate is currently pending.")

    def action_demo_message(self) -> None:
        if not self.state:
            return
        stage = self._next_stage() or "Final"
        self._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="research_agent_1",
                to_agent="coordinator",
                message_type="status",
                stage=stage if stage in STAGES else "Research",
                priority="normal",
                timestamp=now_iso(),
                content="I can start when task contract is ready.",
            )
        )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-advance":
            await self.action_advance_stage()
        elif event.button.id == "btn-approve":
            self.action_approve_gate()
        elif event.button.id == "btn-demo":
            self.action_demo_message()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return

        if text.startswith("/"):
            await self._handle_slash_command(text)
            return

        if self.state and not self.state.approvals.get("Ingest", False):
            intent, reason = self.coordinator_engine.classify_intent(
                current_plan=self.coordinator_plan,
                user_message=text,
            )
            self._log_event(f"Coordinator intent classification: {intent} ({reason})")
            if intent == "approve":
                self._approve_ingest(text, auto_advance=True)
                await self.action_advance_stage()
            else:
                await self._iterate_ingest_with_feedback(text)
            return

        stage = self._next_stage() or "Final"
        self._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="user",
                to_agent="coordinator",
                message_type="question",
                stage=stage if stage in STAGES else "Research",
                priority="normal",
                timestamp=now_iso(),
                content=text,
            )
        )
        action, reply = await asyncio.to_thread(
            self.coordinator_engine.runtime_response,
            run_context=self._build_runtime_context(),
            user_message=text,
        )
        self._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="coordinator",
                to_agent="user",
                message_type="status",
                stage=stage if stage in STAGES else "Research",
                priority="normal",
                timestamp=now_iso(),
                content=reply,
            )
        )
        if action == "advance_stage":
            await self.action_advance_stage()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agentic Tasks TUI")
    parser.add_argument(
        "--input",
        default="data/input.txt",
        help="Path to input brief file (default: data/input.txt)",
    )
    parser.add_argument(
        "--db",
        default=".agentic_tasks.db",
        help="SQLite database file path (default: .agentic_tasks.db)",
    )
    parser.add_argument(
        "--run-id",
        help="Resume an existing run id from SQLite.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    app = AgenticTUI(
        input_path=Path(args.input),
        db_path=Path(args.db),
        resume_run_id=args.run_id,
    )
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
