from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

from rich.markdown import Markdown
from textual.widgets import RichLog, Static

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

if TYPE_CHECKING:
    from app.tui import AgenticTUI


def restore_logs(app: AgenticTUI) -> None:
    if not app.state:
        return
    for message in app.state.messages:
        app._write_chat_renderable(message)
    app._render_all()


async def initialize_run_async(app: AgenticTUI) -> None:
    if app.repo is None:
        app._log_event("Repository is not initialized.")
        app._set_status("Initialization failed.", level="error")
        return

    if app.resume_run_id:
        loaded = app.repo.load_run(app.resume_run_id)
        if loaded is None:
            app._log_event(f"Run not found in DB: {app.resume_run_id}")
            app._render_summary(error=f"Run not found: {app.resume_run_id}")
            app._set_status("Run not found.", level="error")
            return
        app.state = loaded
        for stage in REQUIRED_APPROVAL_STAGES:
            if stage not in app.state.approvals:
                app.state.approvals[stage] = False
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
        brief_text = app.input_path.read_text(encoding="utf-8")
        app.package, extracted = await asyncio.to_thread(
            app.coordinator_engine.infer_brief_package,
            input_path=str(app.input_path),
            brief_text=brief_text,
        )
    except Exception as exc:  # noqa: BLE001
        app._log_event(f"Failed to initialize run: {exc}")
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
        artifacts={
            "user_brief": {"input_path": app.package.input_path, "brief_text": brief_text[:40_000]},
            "brief_extraction": extracted,
        },
        tasks=[],
    )

    app.repo.create_run(app.state)
    app.repo.upsert_artifact(app.state.run_id, "user_brief", app.state.artifacts["user_brief"])
    app.repo.upsert_artifact(app.state.run_id, "brief_extraction", app.state.artifacts["brief_extraction"])

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
    extraction = {}
    if app.state:
        artifact = app.state.artifacts.get("brief_extraction", {})
        extraction = artifact if isinstance(artifact, dict) else {}

    constraints_preview = "; ".join(app.package.constraints[:4]) if app.package.constraints else "n/a"
    key_points_preview = "; ".join(app.package.key_points[:6]) if app.package.key_points else "n/a"
    extraction_lines = (
        "Inferred brief package:\n"
        f"- Objective: {app.package.objective[:220]}\n"
        f"- Audience: {app.package.audience[:180]}\n"
        f"- Tone: {app.package.tone[:180]}\n"
        f"- Constraints: {constraints_preview[:260]}\n"
        f"- Key points: {key_points_preview[:260]}\n"
        f"- Source candidates: {len(app.package.source_candidates)}"
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
            content=extraction_lines,
        )
    )

    extraction_notes = extraction.get("extraction_notes", [])
    if isinstance(extraction_notes, list) and extraction_notes:
        notes_text = "Inference notes:\n" + "\n".join([f"- {str(item)}" for item in extraction_notes[:6]])
        app._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="coordinator",
                to_agent="user",
                message_type="status",
                stage="Ingest",
                priority="normal",
                timestamp=now_iso(),
                content=notes_text,
            )
        )

    summary = str(app.coordinator_plan.get("summary_for_user", "")).strip()
    execution_plan = str(app.coordinator_plan.get("execution_plan_for_user", "")).strip()
    if not summary:
        summary = (
            "Ingest summary: "
            f"Objective='{app.package.objective[:160]}', "
            f"Audience='{app.package.audience[:120]}', "
            f"Tone='{app.package.tone[:120]}', "
            f"Source candidates={len(app.package.source_candidates)}, "
            f"Constraints(sample)='{constraints_preview[:180]}'."
        )
    extraction_confirmation = str(
        extraction.get(
            "confirmation_question",
            "Please confirm these extracted brief details before we proceed.",
        )
    ).strip()
    plan_approval = str(
        app.coordinator_plan.get("approval_question", "Please approve this ingest plan so I can start Research.")
    ).strip()
    approval_question = f"{extraction_confirmation}\n\n{plan_approval}"
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


def _content_workspace_markdown(app: AgenticTUI, *, error: str | None = None) -> str:
    if error:
        return f"## Initialization Error\n\n{error}"
    if not app.state:
        return "## Content Workspace\n\nRun not initialized."

    stage = app._next_stage() or "Done"
    artifacts = app.state.artifacts
    final_payload = artifacts.get("final_post", {})
    revised_payload = artifacts.get("revised_draft", {})
    draft_payload = artifacts.get("first_draft", "")
    outline_payload = artifacts.get("approved_outline", {})

    if isinstance(final_payload, dict):
        post_text = str(final_payload.get("post_text", "")).strip()
        if post_text:
            return f"## Final Post\n\n_Showing artifact: `final_post`_\n\n{post_text}"

    if isinstance(revised_payload, dict):
        revised_text = str(revised_payload.get("revised_draft", "")).strip()
        if revised_text:
            return f"## Revised Draft\n\n_Showing artifact: `revised_draft`_\n\n{revised_text}"

    draft_text = str(draft_payload).strip()
    if draft_text:
        return f"## Draft\n\n_Showing artifact: `first_draft`_\n\n{draft_text}"

    if isinstance(outline_payload, dict) and outline_payload:
        hook = str(outline_payload.get("hook", "")).strip() or "n/a"
        sections = outline_payload.get("sections", [])
        section_lines = "\n".join(
            [f"- {str(item)}" for item in sections[:12]]
        ) if isinstance(sections, list) and sections else "- n/a"
        return (
            "## Outline\n\n"
            "_Showing artifact: `approved_outline`_\n\n"
            f"**Hook**: {hook}\n\n"
            "**Sections**\n"
            f"{section_lines}"
        )

    return (
        "## Content Workspace\n\n"
        f"Current next stage: `{stage}`.\n\n"
        "Content will appear here once Outline/Draft artifacts are produced."
    )


def persist_artifact(app: AgenticTUI, name: str, value: object) -> None:
    if app.state is None or app.repo is None:
        return
    app.state.artifacts[name] = value
    app.repo.upsert_artifact(app.state.run_id, name, value)
    # Keep the top content workspace in sync with artifact writes.
    app._render_summary()


def render_summary(app: AgenticTUI, error: str | None = None) -> None:
    content_view = app.query_one("#content-view", RichLog)
    content_view.clear()
    content_view.write(Markdown(_content_workspace_markdown(app, error=error)))


def render_stages(app: AgenticTUI) -> None:
    return


def render_all(app: AgenticTUI) -> None:
    app._render_summary()
    return


def render_tasks(app: AgenticTUI) -> None:
    return


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
        app._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="coordinator",
                to_agent="user",
                message_type="question",
                stage="Ingest",
                priority="high",
                timestamp=now_iso(),
                content="Ingest approval is still pending. Approve the ingest plan or request changes.",
            )
        )
        app._render_all()
        return False

    if stage == "Critique" and not app.state.approvals.get("Draft", False):
        app._log_event("Cannot start Critique: Draft approval is pending.")
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
                    "Draft approval is pending. Share feedback to iterate the draft, or approve to move to Critique."
                ),
            )
        )
        app._render_all()
        return False

    if stage == "Draft" and not app.state.approvals.get("Outline", False):
        app._log_event("Cannot start Draft: Outline approval is pending.")
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
                    "Outline approval is pending. Share feedback to iterate the outline, "
                    "or approve to move to Draft."
                ),
            )
        )
        app._render_all()
        return False

    if stage == "Final" and not app.state.approvals.get("Final", False):
        app._log_event("Cannot start Final: Final approval is pending.")
        app._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="coordinator",
                to_agent="user",
                message_type="question",
                stage="Final",
                priority="high",
                timestamp=now_iso(),
                content="Final approval is pending. Approve when you are ready to finalize.",
            )
        )
        app._render_all()
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
