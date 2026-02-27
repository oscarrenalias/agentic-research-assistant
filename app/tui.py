from __future__ import annotations

import argparse
import asyncio
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Footer, Header, Input, RichLog, Static

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
    command_events as ui_command_events,
    command_inbox as ui_command_inbox,
    command_ledger as ui_command_ledger,
    command_run_summary as ui_command_run_summary,
    command_stages as ui_command_stages,
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
    #content-pane {
        width: 1fr;
        border: round $panel;
        padding: 0 1;
    }
    #content-view {
        height: 1fr;
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
    """

    BINDINGS = [
        ("n", "advance_stage", "Advance Stage"),
        ("a", "approve_gate", "Approve Gate"),
        ("d", "demo_message", "Demo Msg"),
        ("ctrl+d", "quit", "Quit"),
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
        self.chat_scope_mode = "all"
        self.show_internal_messages = False
        self.show_progress_updates = True
        self.repo: RunRepository | None = None
        self.package: NormalizedTaskPackage | None = None
        self.state: RunState | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="top"):
            with Vertical(id="content-pane"):
                yield Static("Content Workspace")
                yield RichLog(id="content-view", wrap=True, highlight=True)
        with Vertical(id="chat-pane"):
            yield Static("Shared Chat")
            yield RichLog(id="chat-log", wrap=True, highlight=True)
            yield Input(placeholder="Type a message to coordinator and press Enter", id="input-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.repo = RunRepository(self.db_path)
        self.repo.init_schema()
        if not self.coordinator_engine.enabled and self.coordinator_engine.init_error:
            self._log_event(f"Coordinator engine fallback mode: {self.coordinator_engine.init_error}")
        if not self.research_engine.enabled and self.research_engine.init_error:
            self._log_event(f"Research engine fallback mode: {self.research_engine.init_error}")
        if not self.writing_engine.enabled and self.writing_engine.init_error:
            self._log_event(f"Writing engine inference unavailable: {self.writing_engine.init_error}")
        if not self.review_engine.enabled and self.review_engine.init_error:
            self._log_event(f"Review engine fallback mode: {self.review_engine.init_error}")
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

    def _command_stages(self) -> str:
        return ui_command_stages(self)

    def _command_events(self) -> str:
        return ui_command_events(self)

    def _command_ledger(self) -> str:
        return ui_command_ledger(self)

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
        self._render_all()

    @staticmethod
    def _format_task_instruction_message(objective: str, instructions: str, source: str) -> str:
        return (
            "Task assignment:\n"
            f"- Objective: {objective}\n"
            f"- Instructions: {instructions}\n"
            f"- Source candidate: {source}\n"
            "- Expected output: 4-5 evidence-backed claims + evidence notes + confidence."
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

    def _outline_gate_pending(self) -> bool:
        return bool(
            self.state
            and self.state.stage_status.get("Outline") == "completed"
            and self.state.stage_status.get("Draft") == "not_started"
            and not self.state.approvals.get("Outline", False)
        )

    def _draft_gate_pending(self) -> bool:
        return bool(
            self.state
            and self.state.stage_status.get("Draft") == "completed"
            and self.state.stage_status.get("Critique") == "not_started"
            and not self.state.approvals.get("Draft", False)
        )

    def _final_gate_pending(self) -> bool:
        return bool(
            self.state
            and self.state.stage_status.get("Revise") == "completed"
            and not self.state.approvals.get("Final", False)
        )

    def _gate_context_summary(self, stage: str) -> str:
        if not self.state:
            return ""
        if stage == "Outline":
            outline = self.state.artifacts.get("approved_outline", {})
            if isinstance(outline, dict):
                hook = str(outline.get("hook", "")).strip()
                sections = outline.get("sections", [])
                section_count = len(sections) if isinstance(sections, list) else 0
                return f"hook={hook[:180]}; section_count={section_count}"
        if stage == "Draft":
            draft_payload = self.state.artifacts.get("first_draft", "")
            draft_text = str(draft_payload).strip()
            return f"draft_chars={len(draft_text)}"
        if stage == "Final":
            revised = self.state.artifacts.get("revised_draft", {})
            if isinstance(revised, dict):
                revised_text = str(revised.get("revised_draft", "")).strip()
                passes = bool(revised.get("passes_quality_gate", False))
                return f"revised_chars={len(revised_text)}; passes_quality_gate={passes}"
            return "revised_draft_unavailable"
        return ""

    def _approve_outline(self, decision_text: str) -> None:
        if not self.state:
            return
        self.state.approvals["Outline"] = True
        self._persist_run_status()
        self._log_event("Approval granted: Outline checkpoint.")
        self._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="user",
                to_agent="coordinator",
                message_type="decision",
                stage="Outline",
                priority="normal",
                timestamp=now_iso(),
                content=decision_text,
            )
        )
        self._render_all()

    def _outline_evidence_summary(self) -> dict[str, object]:
        outline_payload = self.state.artifacts.get("approved_outline", {}) if self.state else {}
        outline = outline_payload if isinstance(outline_payload, dict) else {}
        evidence = self._evidence_pack()
        claims_payload = evidence.get("claims", []) if isinstance(evidence, dict) else []
        sources_payload = evidence.get("sources", []) if isinstance(evidence, dict) else []
        claims = [item for item in claims_payload if isinstance(item, dict)] if isinstance(claims_payload, list) else []
        sources = [item for item in sources_payload if isinstance(item, dict)] if isinstance(sources_payload, list) else []
        mapped_source_ids: set[str] = set()
        evidence_map = outline.get("evidence_map", [])
        if isinstance(evidence_map, list):
            for row in evidence_map:
                if not isinstance(row, dict):
                    continue
                src_ids = row.get("source_ids", [])
                if isinstance(src_ids, list):
                    for sid in src_ids:
                        text_sid = str(sid).strip()
                        if text_sid:
                            mapped_source_ids.add(text_sid)
        total_source_ids = {
            str(item.get("source_id", "")).strip()
            for item in sources
            if str(item.get("source_id", "")).strip()
        }
        coverage_ratio = 0.0
        if total_source_ids:
            coverage_ratio = len(mapped_source_ids & total_source_ids) / len(total_source_ids)
        return {
            "claim_count": len(claims),
            "source_count": len(sources),
            "mapped_source_count": len(mapped_source_ids),
            "citation_coverage_ratio": round(coverage_ratio, 3),
            "source_ids": sorted(list(total_source_ids))[:20],
        }

    @staticmethod
    def _merge_evidence_pack(base: dict[str, object], additional: dict[str, object]) -> dict[str, object]:
        base_sources_raw = base.get("sources", []) if isinstance(base, dict) else []
        base_claims_raw = base.get("claims", []) if isinstance(base, dict) else []
        add_sources_raw = additional.get("sources", []) if isinstance(additional, dict) else []
        add_claims_raw = additional.get("claims", []) if isinstance(additional, dict) else []

        base_sources = [deepcopy(item) for item in base_sources_raw if isinstance(item, dict)]
        base_claims = [deepcopy(item) for item in base_claims_raw if isinstance(item, dict)]
        add_sources = [deepcopy(item) for item in add_sources_raw if isinstance(item, dict)]
        add_claims = [deepcopy(item) for item in add_claims_raw if isinstance(item, dict)]

        existing_ids: set[str] = set()
        for source in base_sources:
            source_id = str(source.get("source_id", "")).strip()
            if source_id:
                existing_ids.add(source_id)

        id_map: dict[str, str] = {}
        next_counter = len(existing_ids) + 1
        for source in add_sources:
            old_id = str(source.get("source_id", "")).strip()
            new_id = old_id
            if not new_id or new_id in existing_ids:
                while f"S{next_counter}" in existing_ids:
                    next_counter += 1
                new_id = f"S{next_counter}"
                next_counter += 1
            source["source_id"] = new_id
            if old_id:
                id_map[old_id] = new_id
            existing_ids.add(new_id)
            base_sources.append(source)

        for claim in add_claims:
            old_id = str(claim.get("source_id", "")).strip()
            if old_id in id_map:
                claim["source_id"] = id_map[old_id]
            base_claims.append(claim)

        return {
            "summary": str(additional.get("summary", base.get("summary", ""))) if isinstance(additional, dict) else "",
            "sources": base_sources,
            "claims": base_claims,
        }

    def _refresh_outline_from_current_evidence(self, feedback_text: str) -> dict[str, object]:
        current_outline = self.state.artifacts.get("approved_outline", {}) if self.state else {}
        outline_payload = current_outline if isinstance(current_outline, dict) else {}
        evidence = self._evidence_pack()
        claims_payload = evidence.get("claims", []) if isinstance(evidence, dict) else []
        claims = [item for item in claims_payload if isinstance(item, dict)] if isinstance(claims_payload, list) else []
        objective = self.package.objective if self.package else ""
        audience = self.package.audience if self.package else ""
        tone = self.package.tone if self.package else ""
        return self.writing_engine.revise_outline(
            objective=objective,
            audience=audience,
            tone=tone,
            outline=outline_payload,
            claims=claims,
            feedback=feedback_text,
        )

    async def _iterate_outline_with_feedback(self, feedback_text: str) -> None:
        if not self.state:
            return
        self._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="user",
                to_agent="coordinator",
                message_type="question",
                stage="Outline",
                priority="normal",
                timestamp=now_iso(),
                content=feedback_text,
            )
        )
        evidence_summary = self._outline_evidence_summary()
        current_outline = self.state.artifacts.get("approved_outline", {})
        outline_payload = current_outline if isinstance(current_outline, dict) else {}
        objective = self.package.objective if self.package else ""
        audience = self.package.audience if self.package else ""
        tone = self.package.tone if self.package else ""
        decision = await asyncio.to_thread(
            self.coordinator_engine.decide_outline_feedback,
            objective=objective,
            audience=audience,
            tone=tone,
            outline=outline_payload,
            evidence_summary=evidence_summary,
            feedback=feedback_text,
        )
        intent = str(decision.get("intent", "revise_outline")).strip().lower() if isinstance(decision, dict) else "revise_outline"
        response = (
            str(decision.get("response_to_user", "I captured your feedback."))
            if isinstance(decision, dict)
            else "I captured your feedback."
        )
        reasoning = str(decision.get("reasoning_summary", "")) if isinstance(decision, dict) else ""
        focus = decision.get("research_focus", []) if isinstance(decision, dict) else []
        focus_list = [str(item) for item in focus] if isinstance(focus, list) else [feedback_text]
        try:
            max_tasks = int(decision.get("max_additional_tasks", 3)) if isinstance(decision, dict) else 3
        except Exception:  # noqa: BLE001
            max_tasks = 3
        max_tasks = max(1, min(25, max_tasks))
        if intent == "approve":
            self._approve_outline("Outline approved. Proceed to Draft.")
            self._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="coordinator",
                    to_agent="user",
                    message_type="status",
                    stage="Outline",
                    priority="normal",
                    timestamp=now_iso(),
                    content=response,
                )
            )
            await self.action_advance_stage()
            return
        if intent == "question":
            self._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="coordinator",
                    to_agent="user",
                    message_type="status",
                    stage="Outline",
                    priority="normal",
                    timestamp=now_iso(),
                    content=response,
                )
            )
            self._set_status("Outline unchanged. Ask follow-up or provide revision feedback.", level="done")
            return

        if intent in {"supplement_research", "rerun_research"}:
            plan_payload = self.coordinator_plan if isinstance(self.coordinator_plan, dict) else {}
            base_candidates = list(self.package.source_candidates) if self.package else []
            fallback_source = base_candidates[0] if base_candidates else (self.package.objective if self.package else "web research")
            tasks = []
            for i in range(max_tasks):
                focus_item = focus_list[i % len(focus_list)] if focus_list else feedback_text
                source_hint = base_candidates[i % len(base_candidates)] if base_candidates else fallback_source
                tasks.append(
                    {
                        "agent_id": f"research_agent_{i+1}",
                        "objective": f"Collect evidence for outline revision focus: {focus_item[:160]}",
                        "source_hint": source_hint,
                        "instructions": (
                            "Extract 4-5 verifiable claims that directly support the requested outline change. "
                            "Include caveats and confidence for each claim."
                        ),
                        "priority": "high",
                    }
                )
            original_tasks = plan_payload.get("analyst_tasks", []) if isinstance(plan_payload.get("analyst_tasks", []), list) else []
            self.coordinator_plan["analyst_tasks"] = tasks
            self._set_status("Coordinator requested additional research for outline feedback...", level="in_progress")
            try:
                supplemental = await self._execute_research_parallel()
            except Exception as exc:  # noqa: BLE001
                self._post_chat_message(
                    ChatMessage(
                        msg_id=str(uuid.uuid4()),
                        from_agent="coordinator",
                        to_agent="user",
                        message_type="status",
                        stage="Outline",
                        priority="high",
                        timestamp=now_iso(),
                        content=f"Research refresh for outline feedback failed: {exc}",
                    )
                )
                self._set_status("Outline iteration failed during research refresh.", level="error")
                return
            finally:
                self.coordinator_plan["analyst_tasks"] = original_tasks
            if intent == "rerun_research":
                self._persist_artifact("evidence_pack", supplemental)
                self._log_event("Outline feedback triggered full research refresh.")
            else:
                existing = self._evidence_pack()
                merged = self._merge_evidence_pack(existing, supplemental if isinstance(supplemental, dict) else {})
                self._persist_artifact("evidence_pack", merged)
                self._log_event("Outline feedback triggered targeted supplemental research.")

        self._set_status("Applying your outline feedback...", level="in_progress")
        try:
            revised_outline = await asyncio.to_thread(self._refresh_outline_from_current_evidence, feedback_text)
        except Exception as exc:  # noqa: BLE001
            self._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="coordinator",
                    to_agent="user",
                    message_type="status",
                    stage="Outline",
                    priority="high",
                    timestamp=now_iso(),
                    content=f"I could not apply outline feedback: {exc}",
                )
            )
            self._set_status("Outline revision failed.", level="error")
            return
        if not isinstance(revised_outline, dict) or not revised_outline:
            self._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="coordinator",
                    to_agent="user",
                    message_type="status",
                    stage="Outline",
                    priority="high",
                    timestamp=now_iso(),
                    content="I couldn't produce a valid outline revision. Please refine feedback and retry.",
                )
            )
            self._set_status("Outline revision failed.", level="error")
            return

        self._persist_artifact("approved_outline", revised_outline)
        self.state.approvals["Outline"] = False
        self._persist_run_status()
        change_items = revised_outline.get("changelog", [])
        change_note = ""
        if isinstance(change_items, list) and change_items:
            change_note = " Key edits: " + "; ".join([str(item) for item in change_items[:4]])
        reason_note = f"\n\nEvidence check: {reasoning}" if reasoning else ""
        self._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="coordinator",
                to_agent="user",
                message_type="status",
                stage="Outline",
                priority="normal",
                timestamp=now_iso(),
                content=f"{response}{change_note}{reason_note}",
            )
        )
        self._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="coordinator",
                to_agent="user",
                message_type="question",
                stage="Outline",
                priority="high",
                timestamp=now_iso(),
                content=(
                    "Updated outline is ready. Share more feedback, or approve outline to continue to Draft."
                ),
            )
        )
        self._set_status("Outline updated. Continue feedback or approve when ready.", level="done")
        self._render_all()

    def _revise_draft_with_user_feedback(self, feedback_text: str) -> dict[str, object]:
        draft_payload = self.state.artifacts.get("first_draft", "") if self.state else ""
        draft_text = str(draft_payload).strip()
        if not draft_text:
            return {"ok": False, "message": "No first draft is available yet."}

        claims = []
        evidence = self._evidence_pack()
        claims_payload = evidence.get("claims", []) if isinstance(evidence, dict) else []
        if isinstance(claims_payload, list):
            claims = [item for item in claims_payload if isinstance(item, dict)]

        objective = self.package.objective if self.package else ""
        audience = self.package.audience if self.package else ""
        tone = self.package.tone if self.package else ""
        constraints = self.package.constraints if self.package else []
        pseudo_critique: dict[str, object] = {
            "pass": False,
            "issues": [feedback_text],
            "hard_gates": {
                "factual_accuracy_min": True,
                "evidence_quality_min": True,
                "no_fabricated_citations": True,
            },
        }
        try:
            revised = self.writing_engine.revise_draft(
                objective=objective,
                audience=audience,
                tone=tone,
                constraints=constraints,
                draft=draft_text,
                critique=pseudo_critique,
                claims=claims,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": str(exc)}
        revised_text = str(revised.get("revised_draft", draft_text)).strip()
        if not revised_text:
            return {"ok": False, "message": "Draft revision returned empty content."}
        changelog = [str(x) for x in revised.get("changelog", [])]
        return {
            "ok": True,
            "revised_draft": revised_text,
            "changelog": changelog,
        }

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

        if self._outline_gate_pending():
            self._approve_outline("Outline approved. Proceed to Draft.")
            return

        if (
            self.state.stage_status.get("Draft") == "completed"
            and self.state.stage_status.get("Critique") == "not_started"
            and not self.state.approvals.get("Draft", False)
        ):
            self.state.approvals["Draft"] = True
            self._persist_run_status()
            self._log_event("Approval granted: Draft checkpoint.")
            self._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="user",
                    to_agent="coordinator",
                    message_type="decision",
                    stage="Draft",
                    priority="normal",
                    timestamp=now_iso(),
                    content="Draft approved. Proceed to Critique.",
                )
            )
            self._render_all()
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
            elif intent == "iterate":
                await self._iterate_ingest_with_feedback(text)
            else:
                stage = "Ingest"
                self._post_chat_message(
                    ChatMessage(
                        msg_id=str(uuid.uuid4()),
                        from_agent="user",
                        to_agent="coordinator",
                        message_type="question",
                        stage=stage,
                        priority="normal",
                        timestamp=now_iso(),
                        content=text,
                    )
                )
                reply = self.coordinator_engine.answer_plan_question(
                    current_plan=self.coordinator_plan,
                    user_message=text,
                )
                self._post_chat_message(
                    ChatMessage(
                        msg_id=str(uuid.uuid4()),
                        from_agent="coordinator",
                        to_agent="user",
                        message_type="status",
                        stage=stage,
                        priority="normal",
                        timestamp=now_iso(),
                        content=reply,
                    )
                )
                self._set_status("Plan unchanged. Ask more questions or request changes.", level="done")
            return

        if self._outline_gate_pending():
            intent, reason = self.coordinator_engine.classify_gate_intent(
                stage="Outline",
                gate_context=self._gate_context_summary("Outline"),
                user_message=text,
            )
            self._log_event(f"Coordinator gate intent classification (Outline): {intent} ({reason})")
            if intent == "approve":
                self._approve_outline(text)
                await self.action_advance_stage()
            else:
                await self._iterate_outline_with_feedback(text)
            return

        if self._draft_gate_pending():
            intent, reason = self.coordinator_engine.classify_gate_intent(
                stage="Draft",
                gate_context=self._gate_context_summary("Draft"),
                user_message=text,
            )
            self._log_event(f"Coordinator gate intent classification (Draft): {intent} ({reason})")
            if intent == "approve":
                self.action_approve_gate()
                if self.state and self.state.approvals.get("Draft", False):
                    await self.action_advance_stage()
                return
            self._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="user",
                    to_agent="coordinator",
                    message_type="question",
                    stage="Draft",
                    priority="normal",
                    timestamp=now_iso(),
                    content=text,
                )
            )
            self._set_status("Applying your draft feedback...", level="in_progress")
            revise_result = await asyncio.to_thread(self._revise_draft_with_user_feedback, text)
            if bool(revise_result.get("ok", False)):
                revised_text = str(revise_result.get("revised_draft", ""))
                self._persist_artifact("first_draft", revised_text)
                self._log_event("Draft revised from user feedback before Critique.")
                changelog = revise_result.get("changelog", [])
                change_note = (
                    "Key edits: " + "; ".join([str(item) for item in changelog[:4]])
                    if isinstance(changelog, list) and changelog
                    else "Draft updated based on your feedback."
                )
                self._post_chat_message(
                    ChatMessage(
                        msg_id=str(uuid.uuid4()),
                        from_agent="coordinator",
                        to_agent="user",
                        message_type="status",
                        stage="Draft",
                        priority="normal",
                        timestamp=now_iso(),
                        content=f"{change_note}\n\nShare more feedback, or approve draft to move to Critique.",
                    )
                )
                self._set_status("Draft updated. Add more feedback or approve when ready.", level="done")
                self._render_all()
            else:
                fail_msg = str(revise_result.get("message", "Draft revision failed."))
                self._post_chat_message(
                    ChatMessage(
                        msg_id=str(uuid.uuid4()),
                        from_agent="coordinator",
                        to_agent="user",
                        message_type="status",
                        stage="Draft",
                        priority="high",
                        timestamp=now_iso(),
                        content=f"I could not apply feedback: {fail_msg}",
                    )
                )
                self._set_status("Draft feedback iteration failed.", level="error")
            return

        if self._final_gate_pending():
            intent, reason = self.coordinator_engine.classify_gate_intent(
                stage="Final",
                gate_context=self._gate_context_summary("Final"),
                user_message=text,
            )
            self._log_event(f"Coordinator gate intent classification (Final): {intent} ({reason})")
            if intent == "approve":
                self.action_approve_gate()
                if self.state and self.state.approvals.get("Final", False):
                    await self.action_advance_stage()
            else:
                self._post_chat_message(
                    ChatMessage(
                        msg_id=str(uuid.uuid4()),
                        from_agent="user",
                        to_agent="coordinator",
                        message_type="question",
                        stage="Final",
                        priority="normal",
                        timestamp=now_iso(),
                        content=text,
                    )
                )
                self._post_chat_message(
                    ChatMessage(
                        msg_id=str(uuid.uuid4()),
                        from_agent="coordinator",
                        to_agent="user",
                        message_type="status",
                        stage="Final",
                        priority="normal",
                        timestamp=now_iso(),
                        content="Final approval is pending. Say approve when you are ready, or share requested changes.",
                    )
                )
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
        action, reply, action_payload = await asyncio.to_thread(
            self.coordinator_engine.runtime_response,
            run_context=self._build_runtime_context(),
            user_message=text,
        )
        if (
            action == "revise_draft"
            and self.state
            and self.state.stage_status.get("Draft") == "completed"
            and self.state.stage_status.get("Critique") == "not_started"
        ):
            feedback = str(action_payload.get("feedback", "")).strip() if isinstance(action_payload, dict) else ""
            if not feedback:
                feedback = text
            self._set_status("Applying your draft feedback...", level="in_progress")
            revise_result = await asyncio.to_thread(self._revise_draft_with_user_feedback, feedback)
            if bool(revise_result.get("ok", False)):
                revised_text = str(revise_result.get("revised_draft", ""))
                self._persist_artifact("first_draft", revised_text)
                self._log_event("Draft revised from user feedback before Critique.")
                changelog = revise_result.get("changelog", [])
                change_note = (
                    "Key edits: " + "; ".join([str(item) for item in changelog[:4]])
                    if isinstance(changelog, list) and changelog
                    else "Draft updated based on your feedback."
                )
                reply = f"{reply}\n\n{change_note}"
                self._set_status("Draft updated. Add more feedback or say proceed.", level="done")
                self._render_all()
            else:
                fail_msg = str(revise_result.get("message", "Draft revision failed."))
                reply = f"{reply}\n\nI could not apply feedback: {fail_msg}"
                self._set_status("Draft feedback iteration failed.", level="error")
        elif action == "revise_draft":
            reply = (
                f"{reply}\n\nI can apply draft feedback only after Draft is completed and before Critique starts."
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
