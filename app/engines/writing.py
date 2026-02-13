from __future__ import annotations

import json
import os
import re
from typing import Any

class WritingEngine:
    def __init__(self) -> None:
        self.enabled = False
        self._outline_chain = None
        self._outline_revise_chain = None
        self._draft_chain = None
        self._revise_chain = None
        self._init_error: str | None = None

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        model = os.getenv("WRITING_MODEL", os.getenv("RESEARCH_MODEL", "gpt-4o-mini")).strip()
        if not api_key:
            self._init_error = "OPENAI_API_KEY not set"
            return

        try:
            from langchain_core.output_parsers import StrOutputParser
            from langchain_core.prompts import ChatPromptTemplate
            from langchain_openai import ChatOpenAI

            outline_prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        (
                            "You are a writing planner. Return ONLY compact JSON with keys: "
                            "hook (string), sections (array of strings), argument_flow (array of strings), "
                            "evidence_map (array of objects with keys: section, source_ids)."
                        ),
                    ),
                    (
                        "human",
                        (
                            "Objective: {objective}\nAudience: {audience}\nTone: {tone}\n"
                            "Evidence claims JSON: {claims_json}\n"
                            "Create a concise outline mapped to evidence source ids."
                        ),
                    ),
                ]
            )
            draft_prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        (
                            "You are a professional LinkedIn writer. Produce one polished draft in plain text. "
                            "Cite factual claims inline using markers like [S1], [S2]."
                        ),
                    ),
                    (
                        "human",
                        (
                            "Objective: {objective}\nAudience: {audience}\nTone: {tone}\nConstraints: {constraints}\n"
                            "Outline JSON: {outline_json}\nEvidence claims JSON: {claims_json}\n"
                            "Write the draft with clear sections and citation markers."
                        ),
                    ),
                ]
            )
            outline_revise_prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        (
                            "You are a writing planner. Return ONLY compact JSON with keys: "
                            "hook (string), sections (array of strings), argument_flow (array of strings), "
                            "evidence_map (array of objects with keys: section, source_ids), changelog (array of strings)."
                        ),
                    ),
                    (
                        "human",
                        (
                            "Objective: {objective}\nAudience: {audience}\nTone: {tone}\n"
                            "Current outline JSON: {outline_json}\n"
                            "Evidence claims JSON: {claims_json}\n"
                            "User feedback: {feedback}\n"
                            "Update the outline using feedback while keeping structure evidence-grounded."
                        ),
                    ),
                ]
            )
            revise_prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        (
                            "You are a revision writer. Return ONLY compact JSON with keys: revised_draft, changelog. "
                            "changelog must be an array of short strings. Keep citation markers."
                        ),
                    ),
                    (
                        "human",
                        (
                            "Objective: {objective}\nAudience: {audience}\nTone: {tone}\nConstraints: {constraints}\n"
                            "Current draft:\n{draft}\n"
                            "Critique JSON: {critique_json}\n"
                            "Evidence claims JSON: {claims_json}\n"
                            "Revise the draft to address critique while preserving factual grounding and citations."
                        ),
                    ),
                ]
            )
            llm = ChatOpenAI(model=model, temperature=0.2)
            self._outline_chain = outline_prompt | llm | StrOutputParser()
            self._outline_revise_chain = outline_revise_prompt | llm | StrOutputParser()
            self._draft_chain = draft_prompt | llm | StrOutputParser()
            self._revise_chain = revise_prompt | llm | StrOutputParser()
            self.enabled = True
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
    def _fallback_outline(claims: list[dict[str, object]]) -> dict[str, object]:
        sections = ["Hook", "Context", "Evidence", "Implications", "Takeaway"]
        source_ids = [str(c.get("source_id", "")) for c in claims[:5] if str(c.get("source_id", ""))]
        return {
            "hook": "Why this topic matters now.",
            "sections": sections,
            "argument_flow": sections,
            "evidence_map": [{"section": "Evidence", "source_ids": source_ids}],
        }

    @staticmethod
    def _fallback_draft(objective: str, claims: list[dict[str, object]]) -> str:
        lines = [
            f"{objective}",
            "",
            "Evidence highlights:",
        ]
        for claim in claims[:4]:
            marker = str(claim.get("source_id", "S1"))
            text = str(claim.get("claim", ""))
            lines.append(f"- {text} [{marker}]")
        lines.append("")
        lines.append("Takeaway: Practical, evidence-backed action is possible with trade-offs.")
        return "\n".join(lines)

    @staticmethod
    def _fallback_revision(draft: str) -> dict[str, object]:
        revised = draft.strip()
        if revised and not revised.endswith("\n"):
            revised = f"{revised}\n"
        revised += "\nRevision note: tightened structure and clarified evidence framing."
        return {
            "revised_draft": revised,
            "changelog": ["Tightened structure.", "Clarified evidence framing."],
        }

    def create_outline(
        self,
        *,
        objective: str,
        audience: str,
        tone: str,
        claims: list[dict[str, object]],
    ) -> dict[str, object]:
        if not self.enabled or self._outline_chain is None:
            return self._fallback_outline(claims)
        try:
            raw = self._outline_chain.invoke(
                {
                    "objective": objective,
                    "audience": audience,
                    "tone": tone,
                    "claims_json": json.dumps(claims, ensure_ascii=True),
                }
            )
            parsed = self._parse_llm_json(raw)
            return {
                "hook": str(parsed.get("hook", "Why this topic matters now.")),
                "sections": [str(x) for x in parsed.get("sections", [])],
                "argument_flow": [str(x) for x in parsed.get("argument_flow", [])],
                "evidence_map": parsed.get("evidence_map", []),
            }
        except Exception:
            return self._fallback_outline(claims)

    def create_draft(
        self,
        *,
        objective: str,
        audience: str,
        tone: str,
        constraints: list[str],
        outline: dict[str, object],
        claims: list[dict[str, object]],
    ) -> str:
        if not self.enabled or self._draft_chain is None:
            return self._fallback_draft(objective, claims)
        try:
            return str(
                self._draft_chain.invoke(
                    {
                        "objective": objective,
                        "audience": audience,
                        "tone": tone,
                        "constraints": "; ".join(constraints),
                        "outline_json": json.dumps(outline, ensure_ascii=True),
                        "claims_json": json.dumps(claims, ensure_ascii=True),
                    }
                )
            ).strip()
        except Exception:
            return self._fallback_draft(objective, claims)

    def revise_outline(
        self,
        *,
        objective: str,
        audience: str,
        tone: str,
        outline: dict[str, object],
        claims: list[dict[str, object]],
        feedback: str,
    ) -> dict[str, object]:
        if not self.enabled or self._outline_revise_chain is None:
            fallback = dict(outline) if isinstance(outline, dict) else self._fallback_outline(claims)
            fallback.setdefault("changelog", [])
            changelog = fallback.get("changelog", [])
            if isinstance(changelog, list):
                changelog.append("Captured user outline feedback (fallback mode).")
            else:
                fallback["changelog"] = ["Captured user outline feedback (fallback mode)."]
            return fallback
        try:
            raw = self._outline_revise_chain.invoke(
                {
                    "objective": objective,
                    "audience": audience,
                    "tone": tone,
                    "outline_json": json.dumps(outline, ensure_ascii=True),
                    "claims_json": json.dumps(claims, ensure_ascii=True),
                    "feedback": feedback,
                }
            )
            parsed = self._parse_llm_json(raw)
            return {
                "hook": str(parsed.get("hook", "Why this topic matters now.")),
                "sections": [str(x) for x in parsed.get("sections", [])],
                "argument_flow": [str(x) for x in parsed.get("argument_flow", [])],
                "evidence_map": parsed.get("evidence_map", []),
                "changelog": [str(x) for x in parsed.get("changelog", [])],
            }
        except Exception:
            fallback = dict(outline) if isinstance(outline, dict) else self._fallback_outline(claims)
            fallback["changelog"] = ["Outline revision inference failed; keeping current outline."]
            return fallback

    def revise_draft(
        self,
        *,
        objective: str,
        audience: str,
        tone: str,
        constraints: list[str],
        draft: str,
        critique: dict[str, object],
        claims: list[dict[str, object]],
    ) -> dict[str, object]:
        if not self.enabled or self._revise_chain is None:
            return self._fallback_revision(draft)
        try:
            raw = self._revise_chain.invoke(
                {
                    "objective": objective,
                    "audience": audience,
                    "tone": tone,
                    "constraints": "; ".join(constraints),
                    "draft": draft,
                    "critique_json": json.dumps(critique, ensure_ascii=True),
                    "claims_json": json.dumps(claims, ensure_ascii=True),
                }
            )
            parsed = self._parse_llm_json(raw)
            return {
                "revised_draft": str(parsed.get("revised_draft", draft)).strip(),
                "changelog": [str(x) for x in parsed.get("changelog", [])],
            }
        except Exception:
            return self._fallback_revision(draft)

