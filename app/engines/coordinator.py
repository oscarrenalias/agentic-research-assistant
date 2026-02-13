from __future__ import annotations

import json
import os
import re
from typing import Any

from app.domain.models import NormalizedTaskPackage


class CoordinatorEngine:

    def __init__(self) -> None:
        self.enabled = False
        self._chain = None
        self._feedback_chain = None
        self._intent_chain = None
        self._gate_intent_chain = None
        self._outline_feedback_chain = None
        self._runtime_llm = None
        self._runtime_tools: set[str] = set()
        self._plan_qa_chain = None
        self._init_error: str | None = None

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        model = os.getenv("COORDINATOR_MODEL", os.getenv("RESEARCH_MODEL", "gpt-4o-mini")).strip()
        if not api_key:
            self._init_error = "OPENAI_API_KEY not set"
            return

        try:
            from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
            from langchain_core.output_parsers import StrOutputParser
            from langchain_core.prompts import ChatPromptTemplate
            from langchain_core.tools import tool
            from langchain_openai import ChatOpenAI

            prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        (
                            "You are the coordinator in a multi-agent research workflow. "
                            "Return ONLY JSON with keys: "
                            "summary_for_user (string), "
                            "execution_plan_for_user (string), "
                            "approval_question (string), "
                            "key_topics (array of strings), "
                            "research_focus (array of strings), "
                            "priority_rationale (array of strings), "
                            "analyst_tasks (array of objects with keys: agent_id, objective, source_hint, instructions, priority), "
                            "notes (array of strings). "
                            "Do not return markdown."
                        ),
                    ),
                    (
                        "human",
                        (
                            "Title: {title}\n"
                            "Objective: {objective}\n"
                            "Audience: {audience}\n"
                            "Tone: {tone}\n"
                            "Constraints: {constraints}\n"
                            "Key points: {key_points}\n"
                            "Source candidates: {source_candidates}\n"
                            "Create an ingest understanding and research fan-out plan."
                        ),
                    ),
                ]
            )
            feedback_prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        (
                            "You are the coordinator in a multi-agent research workflow. "
                            "Given the current plan and user feedback, produce an updated plan. "
                            "Return ONLY JSON with keys: "
                            "response_to_user (string), "
                            "updated_plan (object with keys: summary_for_user, execution_plan_for_user, approval_question, key_topics, "
                            "research_focus, priority_rationale, analyst_tasks, notes). "
                            "analyst_tasks items must contain: agent_id, objective, source_hint, instructions, priority."
                        ),
                    ),
                    (
                        "human",
                        (
                            "Title: {title}\n"
                            "Objective: {objective}\n"
                            "Audience: {audience}\n"
                            "Tone: {tone}\n"
                            "Constraints: {constraints}\n"
                            "Current plan JSON: {current_plan_json}\n"
                            "User feedback: {feedback}\n"
                            "Revise the plan while preserving parts not contradicted by feedback."
                        ),
                    ),
                ]
            )
            intent_prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        (
                            "Classify user intent in a planning conversation. "
                            "Return ONLY JSON with keys: intent, reason. "
                            "intent must be one of: approve, iterate, question, hold. "
                            "Use 'question' for clarifications that should NOT modify the plan. "
                            "Use 'iterate' only when the user asks to change/update/regenerate plan content. "
                            "Use 'approve' only for explicit approval to proceed."
                        ),
                    ),
                    (
                        "human",
                        (
                            "Current plan summary: {plan_summary}\n"
                            "Current approval question: {approval_question}\n"
                            "User message: {user_message}\n"
                            "Classify intent precisely. Do not mark clarification questions as iterate."
                        ),
                    ),
                ]
            )
            outline_feedback_prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        (
                            "You are the coordinator in a multi-agent research workflow. "
                            "User is giving post-outline feedback. Decide if current evidence is sufficient. "
                            "Return ONLY JSON with keys: "
                            "intent (one of approve, revise_outline, supplement_research, rerun_research, question), "
                            "response_to_user (string), "
                            "reasoning_summary (string), "
                            "research_focus (array of strings), "
                            "max_additional_tasks (integer)."
                        ),
                    ),
                    (
                        "human",
                        (
                            "Objective: {objective}\nAudience: {audience}\nTone: {tone}\n"
                            "Current outline JSON: {outline_json}\n"
                            "Evidence summary JSON: {evidence_summary_json}\n"
                            "User feedback: {feedback}\n"
                            "Choose revise_outline if evidence is sufficient for requested changes. "
                            "Choose supplement_research for targeted gaps. "
                            "Choose rerun_research when evidence is broadly insufficient or stale."
                        ),
                    ),
                ]
            )
            gate_intent_prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        (
                            "Classify user intent at an approval gate in a multi-stage workflow. "
                            "Return ONLY JSON with keys: intent, reason. "
                            "intent must be one of: approve, iterate, hold. "
                            "Use approve only when user clearly authorizes proceeding to the next stage. "
                            "Use iterate for all other actionable feedback or change requests."
                        ),
                    ),
                    (
                        "human",
                        (
                            "Gate stage: {stage}\n"
                            "Gate context: {gate_context}\n"
                            "User message: {user_message}\n"
                            "Classify intent."
                        ),
                    ),
                ]
            )
            @tool
            def get_stage_outcome(stage: str) -> str:
                """Get the concrete stored outcome for one stage (e.g., Outline, Draft, Critique)."""
                return f"stage={stage}"

            @tool
            def get_stage_status(stage: str) -> str:
                """Get the execution status for one stage."""
                return f"stage={stage}"

            @tool
            def get_process_summary() -> str:
                """Get a compact end-to-end process summary including completion and pending approvals."""
                return "summary"

            @tool
            def search_process_messages(query: str, stage: str = "", limit: int = 5) -> str:
                """Search historical coordinator/agent process messages."""
                return f"query={query}; stage={stage}; limit={limit}"

            @tool
            def advance_to_next_step() -> str:
                """Request advancing the workflow to the next executable stage."""
                return "advance"

            @tool
            def revise_draft_with_feedback(feedback: str) -> str:
                """Request revising the current draft using explicit user feedback before critique/finalization."""
                return f"feedback={feedback}"
            plan_qa_prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        (
                            "You are the coordinator in a multi-agent planning workflow. "
                            "Answer user clarification questions about the current ingest plan without modifying the plan. "
                            "Return ONLY compact JSON with key: reply_for_user (string)."
                        ),
                    ),
                    (
                        "human",
                        (
                            "Current ingest plan JSON: {current_plan_json}\n"
                            "User clarification question: {user_message}\n"
                            "Provide a direct clarification answer grounded in the plan."
                        ),
                    ),
                ]
            )
            llm = ChatOpenAI(model=model, temperature=0)
            self._chain = prompt | llm | StrOutputParser()
            self._feedback_chain = feedback_prompt | llm | StrOutputParser()
            self._intent_chain = intent_prompt | llm | StrOutputParser()
            self._gate_intent_chain = gate_intent_prompt | llm | StrOutputParser()
            self._outline_feedback_chain = outline_feedback_prompt | llm | StrOutputParser()
            runtime_tools = [
                get_stage_outcome,
                get_stage_status,
                get_process_summary,
                search_process_messages,
                advance_to_next_step,
                revise_draft_with_feedback,
            ]
            self._runtime_llm = llm.bind_tools(runtime_tools, tool_choice="auto")
            self._runtime_tools = {tool_obj.name for tool_obj in runtime_tools}
            self._plan_qa_chain = plan_qa_prompt | llm | StrOutputParser()
            self.enabled = True
            self._runtime_msg_types = (SystemMessage, HumanMessage, ToolMessage)
        except Exception as exc:  # noqa: BLE001
            self._init_error = str(exc)

    @property
    def init_error(self) -> str | None:
        return self._init_error

    @staticmethod
    def _parse_llm_json(raw: str) -> dict[str, Any]:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            if match:
                return json.loads(match.group(0))
            raise

    @staticmethod
    def _normalize_stage_name(value: str) -> str:
        raw = value.strip().lower()
        mapping = {
            "ingest": "Ingest",
            "research": "Research",
            "outline": "Outline",
            "draft": "Draft",
            "critique": "Critique",
            "review": "Critique",
            "revise": "Revise",
            "revision": "Revise",
            "final": "Final",
            "publish": "Final",
            "publication": "Final",
        }
        return mapping.get(raw, value.strip())

    @classmethod
    def _execute_runtime_tool(cls, *, run_context: dict[str, Any], tool_name: str, tool_args: dict[str, Any]) -> str:
        name = tool_name.strip().lower()
        args = tool_args if isinstance(tool_args, dict) else {}

        if name == "get_process_summary":
            next_stage = str(run_context.get("next_stage", "unknown"))
            stage_status = run_context.get("stage_status", {})
            pending = run_context.get("pending_approvals", [])
            completed = 0
            total = 0
            if isinstance(stage_status, dict):
                total = len(stage_status)
                completed = sum(1 for value in stage_status.values() if str(value) == "completed")
            pending_text = ", ".join([str(x) for x in pending]) if isinstance(pending, list) and pending else "none"
            return (
                f"Process summary: completed_stages={completed}/{total}, next_stage={next_stage}, "
                f"pending_approvals={pending_text}."
            )

        if name == "get_stage_status":
            stage = cls._normalize_stage_name(str(args.get("stage", "")))
            stage_status = run_context.get("stage_status", {})
            if isinstance(stage_status, dict) and stage in stage_status:
                return f"{stage} status: {stage_status[stage]}."
            return f"Stage status not found for `{stage}`."

        if name == "get_stage_outcome":
            stage = cls._normalize_stage_name(str(args.get("stage", "")))
            stage_outputs = run_context.get("stage_outputs", {})
            stage_status = run_context.get("stage_status", {})
            details = ""
            if isinstance(stage_outputs, dict):
                details = str(stage_outputs.get(stage, "")).strip()
            status = ""
            if isinstance(stage_status, dict):
                status = str(stage_status.get(stage, "")).strip()
            if details:
                return f"{stage} outcome ({status or 'unknown status'}): {details}"
            if status:
                return f"{stage} status is `{status}`, but no detailed outcome payload is available."
            return f"Outcome not found for `{stage}`."

        if name == "search_process_messages":
            query = str(args.get("query", "")).strip().lower()
            stage = cls._normalize_stage_name(str(args.get("stage", "")).strip()) if str(args.get("stage", "")).strip() else ""
            limit_raw = args.get("limit", 5)
            try:
                limit = max(1, min(20, int(limit_raw)))
            except Exception:  # noqa: BLE001
                limit = 5
            messages = run_context.get("messages", [])
            if not isinstance(messages, list):
                return "No process messages are available."

            hits: list[str] = []
            for item in reversed(messages):
                if not isinstance(item, dict):
                    continue
                msg_stage = str(item.get("stage", ""))
                content = str(item.get("content", ""))
                if stage and msg_stage != stage:
                    continue
                body = content.lower()
                if query and query not in body:
                    continue
                hits.append(
                    f"[{msg_stage}] {item.get('from_agent', '?')} -> {item.get('to_agent', '?')}: "
                    f"{' '.join(content.split())[:180]}"
                )
                if len(hits) >= limit:
                    break
            if not hits:
                return "No matching process messages found."
            return "Recent matching messages:\n" + "\n".join([f"- {row}" for row in hits])

        if name == "advance_to_next_step":
            next_stage = str(run_context.get("next_stage", "unknown"))
            return f"Advance request accepted. Next stage: {next_stage}."

        if name == "revise_draft_with_feedback":
            feedback = str(args.get("feedback", "")).strip()
            if not feedback:
                return "Draft revision request received without feedback text."
            return f"Draft revision request accepted with feedback: {feedback}"

        return "Requested tool is not available."

    @staticmethod
    def _fallback_plan(package: NormalizedTaskPackage, note: str) -> dict[str, Any]:
        key_topics = package.key_points[:5] if package.key_points else [package.objective[:100]]
        task_count = max(2, min(4, len(package.source_candidates) or 2))
        analyst_tasks: list[dict[str, str]] = []
        for i in range(task_count):
            source_hint = package.source_candidates[i] if i < len(package.source_candidates) else package.objective
            analyst_tasks.append(
                {
                    "agent_id": f"research_agent_{i+1}",
                    "objective": "Extract one verifiable claim relevant to the user objective.",
                    "source_hint": source_hint,
                    "instructions": "Find one concrete claim, include caveats, and estimate confidence.",
                    "priority": "normal",
                }
            )
        return {
            "summary_for_user": (
                "Fallback coordinator plan active because inference is unavailable. "
                f"Objective interpreted as: {package.objective[:180]}"
            ),
            "execution_plan_for_user": (
                "I will assign multiple research agents to cover the main topics, then aggregate findings "
                "into a single evidence pack before drafting."
            ),
            "approval_question": "Approve this fallback plan so Research can start?",
            "key_topics": key_topics,
            "research_focus": key_topics,
            "priority_rationale": [
                "Cover the core claim first (cost competitiveness and deployment feasibility).",
                "Add risk-heavy topics early to avoid one-sided conclusions.",
            ],
            "analyst_tasks": analyst_tasks,
            "notes": [note],
        }

    @staticmethod
    def _normalize_plan_dict(parsed: dict[str, Any]) -> dict[str, Any]:
        tasks = parsed.get("analyst_tasks", [])
        normalized_tasks: list[dict[str, str]] = []
        for i, task in enumerate(tasks):
            if not isinstance(task, dict):
                continue
            normalized_tasks.append(
                {
                    "agent_id": str(task.get("agent_id", f"research_agent_{i+1}")),
                    "objective": str(task.get("objective", "Extract one relevant claim.")),
                    "source_hint": str(task.get("source_hint", "")),
                    "instructions": str(task.get("instructions", "Provide one evidence-backed claim.")),
                    "priority": str(task.get("priority", "normal")),
                }
            )
        return {
            "summary_for_user": str(parsed.get("summary_for_user", "")),
            "execution_plan_for_user": str(parsed.get("execution_plan_for_user", "")),
            "approval_question": str(parsed.get("approval_question", "Approve ingest plan?")),
            "key_topics": [str(x) for x in parsed.get("key_topics", [])],
            "research_focus": [str(x) for x in parsed.get("research_focus", [])],
            "priority_rationale": [str(x) for x in parsed.get("priority_rationale", [])],
            "analyst_tasks": normalized_tasks,
            "notes": [str(x) for x in parsed.get("notes", [])],
        }

    def plan(self, package: NormalizedTaskPackage) -> dict[str, Any]:
        if not self.enabled or self._chain is None:
            return self._fallback_plan(package, "coordinator_fallback_mode")

        try:
            raw = self._chain.invoke(
                {
                    "title": package.title or "",
                    "objective": package.objective,
                    "audience": package.audience,
                    "tone": package.tone,
                    "constraints": "; ".join(package.constraints),
                    "key_points": "; ".join(package.key_points),
                    "source_candidates": "; ".join(package.source_candidates),
                }
            )
            parsed = self._parse_llm_json(raw)
            return self._normalize_plan_dict(parsed)
        except Exception:  # noqa: BLE001
            return self._fallback_plan(package, "coordinator_call_failed_fallback")

    def revise_plan(
        self,
        *,
        package: NormalizedTaskPackage,
        current_plan: dict[str, Any],
        feedback: str,
    ) -> tuple[dict[str, Any], str]:
        if not self.enabled or self._feedback_chain is None:
            revised = dict(current_plan)
            notes = [str(x) for x in revised.get("notes", [])]
            notes.append("feedback_received_fallback")
            revised["notes"] = notes
            response = (
                "I captured your feedback and adjusted the plan context, but inference is unavailable. "
                "I can keep iterating in fallback mode."
            )
            return revised, response

        try:
            raw = self._feedback_chain.invoke(
                {
                    "title": package.title or "",
                    "objective": package.objective,
                    "audience": package.audience,
                    "tone": package.tone,
                    "constraints": "; ".join(package.constraints),
                    "current_plan_json": json.dumps(current_plan, ensure_ascii=True),
                    "feedback": feedback,
                }
            )
            parsed = self._parse_llm_json(raw)
            response = str(parsed.get("response_to_user", "Thanks, I revised the plan."))
            updated = parsed.get("updated_plan", {})
            if not isinstance(updated, dict):
                return current_plan, "I couldn't parse a revised plan; keeping the current plan."
            normalized = self._normalize_plan_dict(updated)
            return normalized, response
        except Exception:
            revised = dict(current_plan)
            notes = [str(x) for x in revised.get("notes", [])]
            notes.append("feedback_call_failed_fallback")
            revised["notes"] = notes
            response = (
                "I received your feedback, but plan revision inference failed. "
                "I kept the current plan and can try again with more specific guidance."
            )
            return revised, response

    def classify_intent(self, *, current_plan: dict[str, Any], user_message: str) -> tuple[str, str]:
        if not self.enabled or self._intent_chain is None:
            return "hold", "inference_unavailable"

        try:
            raw = self._intent_chain.invoke(
                {
                    "plan_summary": str(current_plan.get("summary_for_user", "")),
                    "approval_question": str(current_plan.get("approval_question", "")),
                    "user_message": user_message,
                }
            )
            parsed = self._parse_llm_json(raw)
            intent = str(parsed.get("intent", "iterate")).strip().lower()
            if intent not in {"approve", "iterate", "question", "hold"}:
                return "hold", "invalid_intent_from_model"
            reason = str(parsed.get("reason", ""))
            return intent, reason
        except Exception:
            return "hold", "intent_classification_failed"

    def classify_gate_intent(self, *, stage: str, gate_context: str, user_message: str) -> tuple[str, str]:
        if not self.enabled or self._gate_intent_chain is None:
            return "hold", "inference_unavailable"
        try:
            raw = self._gate_intent_chain.invoke(
                {
                    "stage": stage,
                    "gate_context": gate_context,
                    "user_message": user_message,
                }
            )
            parsed = self._parse_llm_json(raw)
            intent = str(parsed.get("intent", "iterate")).strip().lower()
            if intent not in {"approve", "iterate", "hold"}:
                return "hold", "invalid_gate_intent_from_model"
            reason = str(parsed.get("reason", ""))
            return intent, reason
        except Exception:
            return "hold", "gate_intent_classification_failed"

    def answer_plan_question(self, *, current_plan: dict[str, Any], user_message: str) -> str:
        if not self.enabled or self._plan_qa_chain is None:
            return (
                "I could not run clarification inference. "
                "Use `/plan` to view the latest plan, or ask again once inference is available."
            )
        try:
            raw = self._plan_qa_chain.invoke(
                {
                    "current_plan_json": json.dumps(current_plan, ensure_ascii=True),
                    "user_message": user_message,
                }
            )
            parsed = self._parse_llm_json(raw)
            reply = str(parsed.get("reply_for_user", "")).strip()
            if not reply:
                return "I can clarify the current plan. Please ask your question again with more detail."
            return reply
        except Exception:
            return "I couldn't generate a reliable clarification response. Please rephrase your question."

    def runtime_response(
        self,
        *,
        run_context: dict[str, Any],
        user_message: str,
    ) -> tuple[str, str, dict[str, Any]]:
        if not self.enabled or self._runtime_llm is None:
            next_stage = str(run_context.get("next_stage", "unknown"))
            return "none", f"I can help with process tracking. Next stage appears to be `{next_stage}`.", {}

        try:
            from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

            messages: list[object] = [
                SystemMessage(
                    content=(
                        "You are the coordinator in a multi-agent workflow. "
                        "Use available tools for process state, outcomes, and history lookups. "
                        "Do not guess process details when a tool can provide them. "
                        "If the user requests progression (continue/proceed/next step), call `advance_to_next_step`. "
                        "If the user asks to refine or rewrite the draft (e.g., longer, shorter, expand topic), "
                        "call `revise_draft_with_feedback` with their exact feedback. "
                        "Then provide a concise user-facing response."
                    )
                ),
                HumanMessage(
                    content=(
                        f"Run context JSON: {json.dumps(run_context, ensure_ascii=True)}\n"
                        f"User message: {user_message}\n"
                        "Use tools as needed for grounded answers."
                    )
                ),
            ]

            response = self._runtime_llm.invoke(messages)
            tool_rounds = 0
            requested_action = "none"
            requested_payload: dict[str, Any] = {}
            while getattr(response, "tool_calls", None) and tool_rounds < 3:
                tool_rounds += 1
                tool_messages: list[ToolMessage] = []
                for call in response.tool_calls:
                    name = str(call.get("name", "")).strip()
                    if name not in self._runtime_tools:
                        continue
                    if name == "advance_to_next_step":
                        requested_action = "advance_stage"
                    if name == "revise_draft_with_feedback":
                        requested_action = "revise_draft"
                    args = call.get("args", {})
                    if not isinstance(args, dict):
                        args = {}
                    if name == "revise_draft_with_feedback":
                        requested_payload["feedback"] = str(args.get("feedback", "")).strip()
                    tool_output = self._execute_runtime_tool(
                        run_context=run_context,
                        tool_name=name,
                        tool_args=args,
                    )
                    tool_messages.append(
                        ToolMessage(
                            tool_call_id=str(call.get("id", "")),
                            content=tool_output,
                        )
                    )
                messages.append(response)
                messages.extend(tool_messages)
                response = self._runtime_llm.invoke(messages)

            raw_content = response.content
            if isinstance(raw_content, list):
                content = " ".join([str(item) for item in raw_content]).strip()
            else:
                content = str(raw_content or "").strip()

            action = requested_action
            reply = content or "Here is the current status and next step based on the run context."
            if action == "advance_stage" and reply == "Here is the current status and next step based on the run context.":
                next_stage = str(run_context.get("next_stage", "unknown"))
                reply = f"Proceeding to `{next_stage}`."
            return action, reply, requested_payload
        except Exception:
            next_stage = str(run_context.get("next_stage", "unknown"))
            return "none", f"I couldn't classify that reliably. Next stage appears to be `{next_stage}`.", {}

    def decide_outline_feedback(
        self,
        *,
        objective: str,
        audience: str,
        tone: str,
        outline: dict[str, Any],
        evidence_summary: dict[str, Any],
        feedback: str,
    ) -> dict[str, Any]:
        if not self.enabled or self._outline_feedback_chain is None:
            claim_count = int(evidence_summary.get("claim_count", 0))
            source_count = int(evidence_summary.get("source_count", 0))
            low_coverage = claim_count < 3 or source_count < 2
            intent = "rerun_research" if low_coverage else "revise_outline"
            return {
                "intent": intent,
                "response_to_user": (
                    "I captured your outline feedback. "
                    + (
                        "Evidence coverage looks thin, so I will refresh research first."
                        if intent == "rerun_research"
                        else "I can update the outline directly with current evidence."
                    )
                ),
                "reasoning_summary": (
                    f"fallback_mode: claim_count={claim_count}, source_count={source_count}, intent={intent}"
                ),
                "research_focus": [feedback],
                "max_additional_tasks": 4,
            }
        try:
            raw = self._outline_feedback_chain.invoke(
                {
                    "objective": objective,
                    "audience": audience,
                    "tone": tone,
                    "outline_json": json.dumps(outline, ensure_ascii=True),
                    "evidence_summary_json": json.dumps(evidence_summary, ensure_ascii=True),
                    "feedback": feedback,
                }
            )
            parsed = self._parse_llm_json(raw)
            intent = str(parsed.get("intent", "revise_outline")).strip().lower()
            if intent not in {"approve", "revise_outline", "supplement_research", "rerun_research", "question"}:
                intent = "revise_outline"
            max_tasks_raw = parsed.get("max_additional_tasks", 3)
            try:
                max_tasks = max(1, min(8, int(max_tasks_raw)))
            except Exception:  # noqa: BLE001
                max_tasks = 3
            focus = parsed.get("research_focus", [])
            return {
                "intent": intent,
                "response_to_user": str(parsed.get("response_to_user", "I updated the plan for your outline feedback.")),
                "reasoning_summary": str(parsed.get("reasoning_summary", "")),
                "research_focus": [str(x) for x in focus] if isinstance(focus, list) else [feedback],
                "max_additional_tasks": max_tasks,
            }
        except Exception:
            return {
                "intent": "revise_outline",
                "response_to_user": (
                    "I captured your outline feedback. Outline-decision inference failed, so I will revise directly."
                ),
                "reasoning_summary": "outline_feedback_inference_failed_fallback",
                "research_focus": [feedback],
                "max_additional_tasks": 3,
            }
