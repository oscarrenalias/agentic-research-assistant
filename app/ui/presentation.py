from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from rich.markdown import Markdown
from rich.text import Text
from textual.widgets import RichLog

from app.domain.models import (
    MESSAGE_TYPES,
    PRIORITY_LEVELS,
    REQUIRED_APPROVAL_STAGES,
    STAGES,
    TASK_RELATED_MESSAGE_TYPES,
    ChatMessage,
    now_iso,
)

if TYPE_CHECKING:
    from app.tui import AgenticTUI


def format_coordinator_plan_markdown(plan: dict[str, Any]) -> str:
    if not plan:
        return "No coordinator plan is available yet."

    summary = str(plan.get("summary_for_user", "")).strip() or "n/a"
    execution_plan = str(plan.get("execution_plan_for_user", "")).strip() or "n/a"
    approval = str(plan.get("approval_question", "")).strip() or "n/a"
    topics = [str(x) for x in plan.get("key_topics", [])] if isinstance(plan.get("key_topics"), list) else []
    rationale = [str(x) for x in plan.get("priority_rationale", [])] if isinstance(plan.get("priority_rationale"), list) else []
    tasks = plan.get("analyst_tasks", [])

    topic_lines = "\n".join([f"- {item}" for item in topics[:8]]) or "- n/a"
    rationale_lines = "\n".join([f"- {item}" for item in rationale[:8]]) or "- n/a"

    task_lines_list: list[str] = []
    if isinstance(tasks, list):
        for item in tasks[:12]:
            if not isinstance(item, dict):
                continue
            agent = str(item.get("agent_id", "research_agent"))
            objective = str(item.get("objective", ""))
            source = str(item.get("source_hint", ""))
            task_lines_list.append(f"- **{agent}**: {objective} _(source: {source})_")
    task_lines = "\n".join(task_lines_list) or "- n/a"

    return (
        "## Coordinator Plan\n"
        f"**Summary**: {summary}\n\n"
        f"**Execution Plan**: {execution_plan}\n\n"
        f"**Approval Question**: {approval}\n\n"
        "**Key Topics**\n"
        f"{topic_lines}\n\n"
        "**Priority Rationale**\n"
        f"{rationale_lines}\n\n"
        "**Analyst Assignments**\n"
        f"{task_lines}"
    )


def format_json_block(value: object) -> str:
    return f"```json\n{json.dumps(value, ensure_ascii=True, indent=2)}\n```"


def format_help_markdown() -> str:
    return (
        "## Chat Commands\n"
        "- `/help`: Show available commands.\n"
        "- `/plan`: Show the latest coordinator plan.\n"
        "- `/run`: Show run summary and approval/checkpoint state.\n"
        "- `/stages`: Show status of all stages.\n"
        "- `/events`: Show run events log.\n"
        "- `/ledger`: Show task ledger snapshot.\n"
        "- `/sources`: Show current evidence sources.\n"
        "- `/agents`: List active agents and aggregate task status counts.\n"
        "- `/inbox <agent_id>`: Show a filtered inbox from shared chat.\n"
        "- `/agent <agent_id>`: Show tasks and outputs for one agent.\n"
        "- `/task <task_id_or_prefix>`: Inspect one task output/error details.\n"
        "- `/approve`: Approve current gate in chat.\n"
        "- `/reject <reason>`: Reject/feedback current gate in chat.\n"
        "- `/export [path]`: Export post + references to markdown file.\n"
        "- `/view compact|detailed`: Switch chat density.\n"
        "- `/scope focus|all`: Show only user/coordinator chat or all traffic.\n"
        "- `/internal on|off`: Show/hide internal status chatter.\n"
        "- `/progress on|off`: Show/hide long-running progress updates."
    )


def post_coordinator_markdown(app: AgenticTUI, content: str, *, stage: str | None = None) -> None:
    if not app.state:
        return
    effective_stage = stage or (app._next_stage() or "Final")
    if effective_stage not in STAGES:
        effective_stage = "Ingest"
    app._post_chat_message(
        ChatMessage(
            msg_id=str(uuid.uuid4()),
            from_agent="coordinator",
            to_agent="user",
            message_type="status",
            stage=effective_stage,
            priority="normal",
            timestamp=now_iso(),
            content=content,
        )
    )


def command_run_summary(app: AgenticTUI) -> str:
    if not app.state:
        return "No active run."
    next_stage = app._next_stage() or "Done"
    approvals = []
    for stage in sorted(REQUIRED_APPROVAL_STAGES):
        status = "approved" if app.state.approvals.get(stage, False) else "pending"
        approvals.append(f"- {stage}: {status}")
    approvals_block = "\n".join(approvals) or "- none"
    return (
        "## Run Summary\n"
        f"- Run ID: `{app.state.run_id}`\n"
        f"- Input: `{app.state.input_path}`\n"
        f"- Next stage: `{next_stage}`\n"
        f"- Tasks tracked: `{len(app.state.tasks)}`\n"
        "\n**Approvals**\n"
        f"{approvals_block}"
    )


def command_stages(app: AgenticTUI) -> str:
    if not app.state:
        return "No active run."

    lines = ["## Stages", "| Stage | Status | Approval |", "|---|---|---|"]
    for stage in STAGES:
        status = str(app.state.stage_status.get(stage, "not_started"))
        if stage in REQUIRED_APPROVAL_STAGES:
            approval = "approved" if app.state.approvals.get(stage, False) else "pending"
        else:
            approval = "-"
        lines.append(f"| `{stage}` | `{status}` | `{approval}` |")
    return "\n".join(lines)


def command_sources(app: AgenticTUI) -> str:
    if not app.state:
        return "No active run."
    evidence = app.state.artifacts.get("evidence_pack", {})
    if not isinstance(evidence, dict):
        return "No evidence pack available yet."
    sources = evidence.get("sources", [])
    if not isinstance(sources, list) or not sources:
        return "No evidence sources available yet."
    lines = [
        "## Evidence Sources",
        "| Source ID | Title | Publisher | Tier | Confidence |",
        "|---|---|---|---:|---:|",
    ]
    for source in sources[:30]:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("source_id", ""))
        title = str(source.get("title", "")).replace("|", " ")
        publisher = str(source.get("publisher", "")).replace("|", " ")
        tier = str(source.get("tier", ""))
        confidence = str(source.get("confidence", ""))
        lines.append(f"| `{source_id}` | {title} | {publisher} | {tier} | {confidence} |")
    return "\n".join(lines)


def command_events(app: AgenticTUI) -> str:
    if not app.state:
        return "No active run."
    if not app.state.events:
        return "No events recorded yet."

    lines = ["## Events", f"- Total events: `{len(app.state.events)}`", ""]
    for entry in app.state.events[-200:]:
        lines.append(f"- `{entry.timestamp}` {entry.message}")
    return "\n".join(lines)


def command_ledger(app: AgenticTUI) -> str:
    if not app.state:
        return "No active run."
    if not app.state.tasks:
        return "No tasks have been created yet."

    lines = [
        "## Task Ledger",
        "| Task ID | Owner | Stage | Status | Started | Completed |",
        "|---|---|---|---|---|---|",
    ]
    for task in app.state.tasks[-200:]:
        started = task.started_at or "-"
        completed = task.completed_at or "-"
        lines.append(
            f"| `{task.task_id[:8]}` | `{task.owner}` | `{task.stage}` | `{task.status}` | `{started}` | `{completed}` |"
        )
    return "\n".join(lines)


def build_runtime_context(app: AgenticTUI) -> dict[str, Any]:
    if not app.state:
        return {"next_stage": "unknown"}

    next_stage = app._next_stage() or "Done"
    pending_approvals = [stage for stage, approved in app.state.approvals.items() if not approved]
    task_counts: dict[str, int] = {}
    owner_counts: dict[str, int] = {}
    for task in app.state.tasks:
        task_counts[task.status] = task_counts.get(task.status, 0) + 1
        owner_counts[task.owner] = owner_counts.get(task.owner, 0) + 1

    artifacts = app.state.artifacts
    stage_outputs: dict[str, str] = {}

    outline = artifacts.get("approved_outline", {})
    if isinstance(outline, dict) and outline:
        hook = str(outline.get("hook", "")).strip()
        sections = outline.get("sections", [])
        section_list = [str(x) for x in sections[:8]] if isinstance(sections, list) else []
        stage_outputs["Outline"] = (
            f"hook={hook or 'n/a'}; sections={', '.join(section_list) if section_list else 'n/a'}"
        )

    draft = artifacts.get("first_draft", "")
    draft_text = str(draft).strip()
    if draft_text:
        preview = " ".join(draft_text.split())[:280]
        stage_outputs["Draft"] = f"chars={len(draft_text)}; preview={preview}"

    critique = artifacts.get("critique_feedback", {})
    if isinstance(critique, dict) and critique:
        stage_outputs["Critique"] = (
            f"pass={bool(critique.get('pass', False))}; "
            f"total_score={int(critique.get('total_score', 0))}; "
            f"issues={len(critique.get('issues', [])) if isinstance(critique.get('issues'), list) else 0}"
        )

    revised = artifacts.get("revised_draft", {})
    revised_text = ""
    if isinstance(revised, dict):
        revised_text = str(revised.get("revised_draft", "")).strip()
    if revised_text:
        stage_outputs["Revise"] = (
            f"chars={len(revised_text)}; "
            f"passes_quality_gate={bool(revised.get('passes_quality_gate', False))}; "
            f"revision_attempts={int(revised.get('revision_attempts', 0))}"
        )

    final_post = artifacts.get("final_post", {})
    if isinstance(final_post, dict) and final_post:
        post_text = str(final_post.get("post_text", "")).strip()
        refs = final_post.get("references", [])
        ref_count = len(refs) if isinstance(refs, list) else 0
        stage_outputs["Final"] = f"post_chars={len(post_text)}; references={ref_count}"

    evidence_pack = artifacts.get("evidence_pack", {})
    if isinstance(evidence_pack, dict) and evidence_pack:
        claims = evidence_pack.get("claims", [])
        sources = evidence_pack.get("sources", [])
        claim_count = len(claims) if isinstance(claims, list) else 0
        source_count = len(sources) if isinstance(sources, list) else 0
        stage_outputs["Research"] = f"claims={claim_count}; sources={source_count}"

    recent_agent_messages: list[dict[str, str]] = []
    for message in app.state.messages:
        if message.to_agent == "broadcast":
            continue
        if message.from_agent == "user" or message.to_agent == "user":
            continue
        recent_agent_messages.append(
            {
                "from": message.from_agent,
                "to": message.to_agent,
                "stage": message.stage,
                "type": message.message_type,
                "content": " ".join((message.content or "").split())[:220],
            }
        )

    all_tasks = [
        {
            "task_id": task.task_id,
            "run_id": task.run_id,
            "stage": task.stage,
            "owner": task.owner,
            "status": task.status,
            "input_ref": task.input_ref,
            "output": task.output,
            "error": task.error,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "completed_at": task.completed_at,
        }
        for task in app.state.tasks
    ]
    all_messages = [message.to_dict() for message in app.state.messages]
    all_events = [{"timestamp": entry.timestamp, "message": entry.message} for entry in app.state.events]
    package_payload = app.package.to_dict() if app.package else {}
    coordinator_plan_payload = app.coordinator_plan if isinstance(app.coordinator_plan, dict) else {}

    return {
        "run_id": app.state.run_id,
        "input_path": app.state.input_path,
        "normalized_task_package": package_payload,
        "coordinator_plan": coordinator_plan_payload,
        "artifacts": dict(app.state.artifacts),
        "stage_status": dict(app.state.stage_status),
        "approvals": dict(app.state.approvals),
        "pending_approvals": pending_approvals,
        "next_stage": next_stage,
        "task_status_counts": task_counts,
        "active_agents": owner_counts,
        "stage_outputs": stage_outputs,
        "recent_agent_messages": recent_agent_messages,
        "tasks": all_tasks,
        "messages": all_messages,
        "events": all_events,
        "last_events": [entry.message for entry in app.state.events],
        "objective": (app.package.objective if app.package else ""),
    }


def command_agents_summary(app: AgenticTUI) -> str:
    if not app.state:
        return "No active run."
    owners = sorted({task.owner for task in app.state.tasks})
    if not owners:
        return "No agent tasks have been created yet."

    lines = ["## Agents", "| Agent | Queued | In Progress | Done | Failed |", "|---|---:|---:|---:|---:|"]
    for owner in owners:
        queued = sum(1 for task in app.state.tasks if task.owner == owner and task.status == "queued")
        in_progress = sum(1 for task in app.state.tasks if task.owner == owner and task.status == "in_progress")
        done = sum(1 for task in app.state.tasks if task.owner == owner and task.status == "done")
        failed = sum(1 for task in app.state.tasks if task.owner == owner and task.status == "failed")
        lines.append(f"| `{owner}` | {queued} | {in_progress} | {done} | {failed} |")
    return "\n".join(lines)


def command_agent_details(app: AgenticTUI, agent_id: str) -> str:
    if not app.state:
        return "No active run."
    tasks = [task for task in app.state.tasks if task.owner == agent_id]
    if not tasks:
        return f"No tasks found for agent `{agent_id}`."

    lines = [f"## Agent `{agent_id}`", f"- Total tasks: `{len(tasks)}`", ""]
    for task in tasks[-8:]:
        lines.append(
            (
                f"### Task `{task.task_id}`\n"
                f"- Stage: `{task.stage}`\n"
                f"- Status: `{task.status}`\n"
                f"- Source: `{task.input_ref}`"
            )
        )
        if task.error:
            lines.append(f"- Error: `{task.error}`")
        if task.output is not None:
            lines.append("**Output**")
            lines.append(format_json_block(task.output))
        lines.append("")
    return "\n".join(lines).strip()


def command_inbox(app: AgenticTUI, agent_id: str) -> str:
    if not app.state:
        return "No active run."
    inbox = [
        message
        for message in app.state.messages
        if message.to_agent in {agent_id, "broadcast"}
    ]
    if not inbox:
        return f"No inbox messages for `{agent_id}`."

    lines = [f"## Inbox `{agent_id}`", f"- Messages: `{len(inbox)}`", ""]
    for message in inbox[-25:]:
        ts = message.timestamp.split("T")[1][:8] if "T" in message.timestamp else message.timestamp
        task_hint = f" task={message.task_id[:8]}" if message.task_id else ""
        preview = " ".join((message.content or "").split())
        if len(preview) > 120:
            preview = preview[:119].rstrip() + "…"
        lines.append(
            f"- `{ts}` `{message.message_type}` `{message.stage}` from `{message.from_agent}`{task_hint}: {preview}"
        )
    return "\n".join(lines)


def validate_chat_message(app: AgenticTUI, message: ChatMessage) -> list[str]:
    errors: list[str] = []
    if not message.msg_id.strip():
        errors.append("msg_id is required.")
    if not message.from_agent.strip():
        errors.append("from_agent is required.")
    if not message.to_agent.strip():
        errors.append("to_agent is required.")
    if message.message_type not in MESSAGE_TYPES:
        errors.append(f"message_type must be one of: {', '.join(sorted(MESSAGE_TYPES))}.")
    if message.stage not in STAGES:
        errors.append(f"stage must be one of: {', '.join(STAGES)}.")
    if message.priority not in PRIORITY_LEVELS:
        errors.append(f"priority must be one of: {', '.join(sorted(PRIORITY_LEVELS))}.")
    if not message.timestamp.strip():
        errors.append("timestamp is required.")
    if not message.content.strip():
        errors.append("content is required.")

    if message.to_agent == "broadcast" and message.message_type not in {"status", "decision"}:
        errors.append("broadcast supports only status/decision message types.")
    if message.message_type in TASK_RELATED_MESSAGE_TYPES:
        if not message.task_id:
            errors.append(f"{message.message_type} requires task_id.")
        if message.to_agent == "broadcast":
            errors.append("task-related messages cannot target broadcast.")

    if message.reply_to and app.state:
        known_ids = {entry.msg_id for entry in app.state.messages}
        if message.reply_to not in known_ids:
            errors.append("reply_to does not reference a known message id.")
    return errors


def command_task_details(app: AgenticTUI, task_key: str) -> str:
    if not app.state:
        return "No active run."
    needle = task_key.strip().lower()
    matches = [task for task in app.state.tasks if task.task_id.lower().startswith(needle)]
    if not matches:
        return f"No task found for id/prefix `{task_key}`."
    if len(matches) > 1:
        options = "\n".join([f"- `{task.task_id}` ({task.owner}, {task.status})" for task in matches[:10]])
        return (
            f"Task prefix `{task_key}` matched multiple tasks.\n"
            "Use a longer prefix:\n"
            f"{options}"
        )

    task = matches[0]
    lines = [
        f"## Task `{task.task_id}`",
        f"- Owner: `{task.owner}`",
        f"- Stage: `{task.stage}`",
        f"- Status: `{task.status}`",
        f"- Source: `{task.input_ref}`",
    ]
    if task.started_at:
        lines.append(f"- Started: `{task.started_at}`")
    if task.completed_at:
        lines.append(f"- Completed: `{task.completed_at}`")
    if task.error:
        lines.append(f"- Error: `{task.error}`")
    if task.output is not None:
        lines.append("")
        lines.append("**Output**")
        lines.append(format_json_block(task.output))
    return "\n".join(lines)


def is_internal_message(message: ChatMessage) -> bool:
    return (
        message.to_agent == "broadcast"
        and message.message_type == "status"
    )


def is_progress_update(message: ChatMessage) -> bool:
    if not (
        message.from_agent == "coordinator"
        and message.to_agent == "broadcast"
        and message.message_type == "status"
    ):
        return False
    body = (message.content or "").strip()
    return body.startswith(("⏳", "🔄", "⌛", "🔃", "✅", "❌"))


def should_display_chat_message(app: AgenticTUI, message: ChatMessage) -> bool:
    if app.show_progress_updates and is_progress_update(message):
        return True

    if app.chat_scope_mode == "focus":
        focus_pair = (
            (message.from_agent == "user" and message.to_agent == "coordinator")
            or (message.from_agent == "coordinator" and message.to_agent == "user")
        )
        if not focus_pair:
            return False

    if not app.show_internal_messages and is_internal_message(message):
        return False

    return True


def repaint_chat_log(app: AgenticTUI) -> None:
    chat = app.query_one("#chat-log", RichLog)
    chat.clear()
    if not app.state:
        return
    for message in app.state.messages:
        app._write_chat_renderable(message)


def type_icon(message_type: str, content: str) -> str:
    lowered = content.lower()
    if "failed" in lowered or "error" in lowered:
        return "❌"
    if message_type == "task":
        return "📌"
    if message_type == "question":
        return "❓"
    if message_type == "result":
        return "📤"
    if message_type == "decision":
        return "✅"
    if message_type == "review":
        return "🧪"
    if message_type == "status":
        if lowered.startswith("✅") or "completed" in lowered or "approved" in lowered:
            return "✅"
        if lowered.startswith("⏳") or "started" in lowered or "revising" in lowered or "analyzing" in lowered:
            return "⏳"
        return "📣"
    return "💬"


def looks_like_markdown(content: str) -> bool:
    markers = ("## ", "- ", "* ", "1. ", "```", "| ")
    return any(marker in content for marker in markers)


def render_chat_header(app: AgenticTUI, message: ChatMessage) -> Text:
    icon = type_icon(message.message_type, message.content)
    task_hint = f" #{message.task_id[:8]}" if message.task_id else ""
    timestamp = message.timestamp.split("T")[1][:8] if "T" in message.timestamp else message.timestamp

    if app.chat_view_mode == "detailed":
        header = Text()
        header.append(f"{icon} {message.from_agent} -> {message.to_agent}", style="bold")
        header.append(f" [{message.message_type}] [{message.stage}] {timestamp}", style="dim")
        if task_hint:
            header.append(task_hint, style="dim")
        header.append(f" id={message.msg_id[:8]}", style="dim")
        return header

    header = Text()
    header.append(f"{icon} {message.from_agent} -> {message.to_agent}", style="bold")
    header.append(f" [{message.message_type}] [{message.stage}] {timestamp}", style="dim")
    if task_hint:
        header.append(task_hint, style="dim")
    return header


def write_chat_renderable(app: AgenticTUI, message: ChatMessage) -> None:
    if not should_display_chat_message(app, message):
        return
    chat = app.query_one("#chat-log", RichLog)
    chat.write(render_chat_header(app, message))

    body = (message.content or "").strip()
    if not body:
        return
    if looks_like_markdown(body):
        chat.write(Markdown(body))
    else:
        chat.write(body)
    chat.write("")
