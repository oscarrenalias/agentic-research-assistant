from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

from textual.widgets import DataTable, RichLog, Static

from app.domain.models import (
    REQUIRED_APPROVAL_STAGES,
    STAGES,
    STAGE_OUTPUT_ARTIFACT,
    ChatMessage,
    EventEntry,
    NormalizedTaskPackage,
    RunState,
    now_iso,
)
from app.services.ingest import build_normalized_task

if TYPE_CHECKING:
    from app.tui import AgenticTUI


def restore_logs(app: AgenticTUI) -> None:
    if not app.state:
        return
    events = app.query_one("#event-log", RichLog)
    for entry in app.state.events:
        events.write(f"[{entry.timestamp}] {entry.message}")
    for message in app.state.messages:
        app._write_chat_renderable(message)
    app._render_tasks()


async def initialize_run_async(app: AgenticTUI) -> None:
    event_log = app.query_one("#event-log", RichLog)
    if app.repo is None:
        event_log.write("[error]Repository is not initialized[/error]")
        app._set_status("Initialization failed.", level="error")
        return

    if app.resume_run_id:
        loaded = app.repo.load_run(app.resume_run_id)
        if loaded is None:
            event_log.write(f"[error]Run not found in DB: {app.resume_run_id}[/error]")
            app._render_summary(error=f"Run not found: {app.resume_run_id}")
            app._set_status("Run not found.", level="error")
            return
        app.state = loaded
        package_payload = loaded.artifacts.get("normalized_task_package")
        if isinstance(package_payload, dict):
            app.package = NormalizedTaskPackage.from_dict(package_payload)
        plan_payload = loaded.artifacts.get("coordinator_plan")
        if isinstance(plan_payload, dict):
            app.coordinator_plan = plan_payload
        app._restore_logs()
        app._log_event("Run resumed from SQLite state.")
        app._render_all()
        app._set_status("Run resumed. You can chat with coordinator or press 'n' to continue.", level="done")
        return

    try:
        app.package = build_normalized_task(app.input_path)
    except Exception as exc:  # noqa: BLE001
        event_log.write(f"[error]Failed to initialize run:[/error] {exc}")
        app._render_summary(error=str(exc))
        app._set_status("Initialization failed.", level="error")
        return

    created = now_iso()
    app.state = RunState(
        run_id=app.package.run_id,
        input_path=app.package.input_path,
        created_at=created,
        updated_at=created,
        stage_status={stage: "not_started" for stage in STAGES},
        approvals={stage: False for stage in REQUIRED_APPROVAL_STAGES},
        artifacts={"user_brief": {"input_path": app.package.input_path}},
        tasks=[],
    )

    app.repo.create_run(app.state)
    app.repo.upsert_artifact(app.state.run_id, "user_brief", app.state.artifacts["user_brief"])

    app._start_stage("Ingest")
    app._complete_stage("Ingest", app.package.to_dict())
    await app._generate_coordinator_plan_async()
    app._post_ingest_summary_and_approval_request()
    app._log_event("Run initialized. Ingest completed. Waiting for approval to proceed.")
    app._render_all()
    app._set_status("Waiting for your feedback or approval.", level="done")


async def generate_coordinator_plan_async(app: AgenticTUI) -> None:
    if not app.package:
        return
    app._set_status("Coordinator is analyzing your request and drafting a plan...", level="in_progress")
    app.coordinator_plan = await asyncio.to_thread(app.coordinator_engine.plan, app.package)
    app._persist_artifact("coordinator_plan", app.coordinator_plan)
    if app.coordinator_engine.enabled:
        app._log_event("Coordinator plan generated via inference.")
    else:
        app._log_event("Coordinator fallback planning used (no inference).")
    app._set_status("Coordinator plan ready. Review and provide feedback or approval.", level="done")


def post_ingest_summary_and_approval_request(app: AgenticTUI) -> None:
    if not app.package:
        return
    summary = str(app.coordinator_plan.get("summary_for_user", "")).strip()
    execution_plan = str(app.coordinator_plan.get("execution_plan_for_user", "")).strip()
    if not summary:
        constraints_preview = "; ".join(app.package.constraints[:2]) if app.package.constraints else "n/a"
        summary = (
            "Ingest summary: "
            f"Objective='{app.package.objective[:160]}', "
            f"Audience='{app.package.audience[:120]}', "
            f"Tone='{app.package.tone[:120]}', "
            f"Source candidates={len(app.package.source_candidates)}, "
            f"Constraints(sample)='{constraints_preview[:180]}'."
        )
    approval_question = str(
        app.coordinator_plan.get("approval_question", "Please approve this ingest plan so I can start Research.")
    )
    if not execution_plan:
        execution_plan = (
            "Execution plan: run parallel research agents across key topics, prioritize disputed/controversial "
            "areas for deeper evidence checks, then synthesize findings into one evidence pack."
        )
    app._post_chat_message(
        ChatMessage(
            msg_id=str(uuid.uuid4()),
            from_agent="coordinator",
            to_agent="user",
            message_type="status",
            stage="Ingest",
            priority="normal",
            timestamp=now_iso(),
            content=summary,
        )
    )
    app._post_chat_message(
        ChatMessage(
            msg_id=str(uuid.uuid4()),
            from_agent="coordinator",
            to_agent="user",
            message_type="status",
            stage="Ingest",
            priority="normal",
            timestamp=now_iso(),
            content=execution_plan,
        )
    )
    app._post_chat_message(
        ChatMessage(
            msg_id=str(uuid.uuid4()),
            from_agent="coordinator",
            to_agent="user",
            message_type="question",
            stage="Ingest",
            priority="high",
            timestamp=now_iso(),
            content=approval_question,
        )
    )


def set_status(app: AgenticTUI, text: str, *, level: str = "info") -> None:
    if level == "in_progress":
        spinner_frames = ["⏳", "🔄", "⌛", "🔃"]
        prefix = spinner_frames[app._spinner_idx % len(spinner_frames)]
        app._spinner_idx += 1
    elif level == "done":
        prefix = "✅"
    elif level == "error":
        prefix = "❌"
    else:
        prefix = "ℹ️"
    content = f"{prefix} {text}"
    stage = "Ingest"
    if app.state:
        stage = app._next_stage() or "Final"
    app._write_chat_renderable(
        ChatMessage(
            msg_id=str(uuid.uuid4()),
            from_agent="coordinator",
            to_agent="broadcast",
            message_type="status",
            stage=stage if stage in STAGES else "Ingest",
            priority="normal",
            timestamp=now_iso(),
            content=content,
        )
    )


def approve_ingest(app: AgenticTUI, decision_text: str, *, auto_advance: bool = False) -> None:
    if not app.state:
        return
    app.state.approvals["Ingest"] = True
    app._persist_run_status()
    app._log_event("Approval granted: Ingest checkpoint.")
    app._post_chat_message(
        ChatMessage(
            msg_id=str(uuid.uuid4()),
            from_agent="user",
            to_agent="coordinator",
            message_type="decision",
            stage="Ingest",
            priority="normal",
            timestamp=now_iso(),
            content=decision_text,
        )
    )
    app._post_chat_message(
        ChatMessage(
            msg_id=str(uuid.uuid4()),
            from_agent="coordinator",
            to_agent="broadcast",
            message_type="status",
            stage="Ingest",
            priority="normal",
            timestamp=now_iso(),
            content="Ingest plan approved by user. Proceeding when commanded.",
        )
    )
    app._render_all()
    if auto_advance:
        app._set_status("Ingest approved. Starting Research now.", level="done")
    else:
        app._set_status("Ingest approved. Press 'n' to start Research.", level="done")


async def iterate_ingest_with_feedback(app: AgenticTUI, feedback_text: str) -> None:
    if not app.state or not app.package:
        return
    app._post_chat_message(
        ChatMessage(
            msg_id=str(uuid.uuid4()),
            from_agent="user",
            to_agent="coordinator",
            message_type="question",
            stage="Ingest",
            priority="normal",
            timestamp=now_iso(),
            content=feedback_text,
        )
    )
    app._set_status("Coordinator is revising the plan based on your feedback...", level="in_progress")
    plan_response = await asyncio.to_thread(
        app.coordinator_engine.revise_plan,
        package=app.package,
        current_plan=app.coordinator_plan,
        feedback=feedback_text,
    )
    if not isinstance(plan_response, tuple) or len(plan_response) != 2:
        app._log_event("Coordinator revise_plan returned an invalid payload.")
        app._set_status("Plan revision failed. Please retry feedback.", level="error")
        return
    updated_plan, response_text = plan_response
    if not isinstance(updated_plan, dict):
        app._log_event("Coordinator revise_plan returned invalid plan shape.")
        app._set_status("Plan revision failed due to malformed plan payload.", level="error")
        return
    app.coordinator_plan = updated_plan
    app._persist_artifact("coordinator_plan", app.coordinator_plan)
    app._post_chat_message(
        ChatMessage(
            msg_id=str(uuid.uuid4()),
            from_agent="coordinator",
            to_agent="user",
            message_type="status",
            stage="Ingest",
            priority="normal",
            timestamp=now_iso(),
            content=str(response_text),
        )
    )
    app._post_ingest_summary_and_approval_request()
    app._log_event("Coordinator revised ingest plan based on user feedback.")
    app._render_all()
    app._set_status("Plan updated. Continue feedback, or approve when satisfied.", level="done")


def next_stage(app: AgenticTUI) -> str | None:
    if not app.state:
        return None
    for stage in STAGES:
        if app.state.stage_status.get(stage) != "completed":
            return stage
    return None


def persist_run_status(app: AgenticTUI) -> None:
    if app.repo is None or app.state is None:
        return
    app.repo.save_run_status(app.state)


def log_event(app: AgenticTUI, message: str) -> None:
    timestamp = now_iso()
    if app.state:
        app.state.events.append(EventEntry(timestamp=timestamp, message=message))
        if app.repo is not None:
            app.repo.add_event(app.state.run_id, message=message, timestamp=timestamp)
    app.query_one("#event-log", RichLog).write(message)


def persist_artifact(app: AgenticTUI, name: str, value: object) -> None:
    if app.state is None or app.repo is None:
        return
    app.state.artifacts[name] = value
    app.repo.upsert_artifact(app.state.run_id, name, value)


def render_summary(app: AgenticTUI, error: str | None = None) -> None:
    summary = app.query_one("#run-summary", Static)
    if error:
        summary.update(f"[b red]Initialization error[/b red]\n{error}")
        return
    if not app.state:
        summary.update("Run not initialized")
        return

    next_stage_name = app._next_stage() or "Done"
    ingest_approval = "approved" if app.state.approvals.get("Ingest") else "pending"
    final_approval = "approved" if app.state.approvals.get("Final") else "pending"
    title = app.package.title if app.package else "n/a"

    summary.update(
        "\n".join(
            [
                f"[b]Run ID:[/b] {app.state.run_id}",
                f"[b]Input:[/b] {app.state.input_path}",
                f"[b]Title:[/b] {title}",
                f"[b]Next Stage:[/b] {next_stage_name}",
                f"[b]Ingest Approval:[/b] {ingest_approval}",
                f"[b]Final Approval:[/b] {final_approval}",
            ]
        )
    )


def render_stages(app: AgenticTUI) -> None:
    stages_table = app.query_one("#stages", DataTable)
    stages_table.clear(columns=False)
    if not app.state:
        return

    for stage in STAGES:
        approval = "required" if stage in REQUIRED_APPROVAL_STAGES else "-"
        if stage in app.state.approvals:
            approval = "approved" if app.state.approvals[stage] else "pending"
        stages_table.add_row(stage, app.state.stage_status.get(stage, "not_started"), approval)


def render_all(app: AgenticTUI) -> None:
    app._render_summary()
    app._render_stages()
    app._render_tasks()


def render_tasks(app: AgenticTUI) -> None:
    tasks_table = app.query_one("#tasks", DataTable)
    tasks_table.clear(columns=False)
    if not app.state:
        return
    for task in app.state.tasks[-30:]:
        tasks_table.add_row(task.task_id[:8], task.owner, task.stage, task.status)


def start_stage(app: AgenticTUI, stage: str) -> bool:
    if not app.state:
        return False

    index = STAGES.index(stage)
    for prev in STAGES[:index]:
        if app.state.stage_status.get(prev) != "completed":
            app._log_event(f"Cannot start {stage}: previous stage {prev} is not completed.")
            return False

    if stage == "Research" and not app.state.approvals.get("Ingest", False):
        app._log_event("Cannot start Research: Ingest approval is pending.")
        return False

    if stage == "Final" and not app.state.approvals.get("Final", False):
        app._log_event("Cannot start Final: Final approval is pending.")
        return False
    if stage == "Final":
        critique_payload = app.state.artifacts.get("critique_feedback", {})
        critique = critique_payload if isinstance(critique_payload, dict) else {}
        if not bool(critique.get("pass", False)):
            app._log_event("Cannot start Final: critique hard gates are not passing.")
            return False

    app.state.stage_status[stage] = "in_progress"
    app._persist_run_status()
    app._log_event(f"Stage started: {stage}")
    return True


def complete_stage(app: AgenticTUI, stage: str, output: object) -> None:
    if not app.state:
        return

    artifact_key = STAGE_OUTPUT_ARTIFACT[stage]
    app._persist_artifact(artifact_key, output)
    app.state.stage_status[stage] = "completed"
    app._persist_run_status()
    app._log_event(f"Stage completed: {stage}")


def post_chat_message(app: AgenticTUI, message: ChatMessage) -> bool:
    if not app.state:
        return False

    errors = app._validate_chat_message(message)
    if errors:
        for error in errors:
            app._log_event(f"Message rejected: {error}")
        return False

    app.state.messages.append(message)
    if app.repo is not None:
        app.repo.add_message(app.state.run_id, message)
    app._write_chat_renderable(message)
    return True
