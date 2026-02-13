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

    def _require_chain(self, chain: Any, operation: str) -> None:
        if self.enabled and chain is not None:
            return
        reason = self._init_error or "model chain not initialized"
        raise RuntimeError(f"{operation} failed: writing inference unavailable ({reason}).")

    def create_outline(
        self,
        *,
        objective: str,
        audience: str,
        tone: str,
        claims: list[dict[str, object]],
    ) -> dict[str, object]:
        self._require_chain(self._outline_chain, "Outline generation")
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
            hook = parsed.get("hook")
            sections = parsed.get("sections")
            argument_flow = parsed.get("argument_flow")
            evidence_map = parsed.get("evidence_map")
            if not isinstance(hook, str) or not isinstance(sections, list) or not isinstance(argument_flow, list) or not isinstance(evidence_map, list):
                raise ValueError("invalid outline schema")
            return {
                "hook": hook,
                "sections": [str(x) for x in sections],
                "argument_flow": [str(x) for x in argument_flow],
                "evidence_map": evidence_map,
            }
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Outline generation failed: {exc}") from exc

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
        self._require_chain(self._draft_chain, "Draft generation")
        try:
            draft = str(
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
            if not draft:
                raise ValueError("empty draft")
            return draft
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Draft generation failed: {exc}") from exc

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
        self._require_chain(self._outline_revise_chain, "Outline revision")
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
            hook = parsed.get("hook")
            sections = parsed.get("sections")
            argument_flow = parsed.get("argument_flow")
            evidence_map = parsed.get("evidence_map")
            changelog = parsed.get("changelog")
            if not isinstance(hook, str) or not isinstance(sections, list) or not isinstance(argument_flow, list) or not isinstance(evidence_map, list) or not isinstance(changelog, list):
                raise ValueError("invalid outline revision schema")
            return {
                "hook": hook,
                "sections": [str(x) for x in sections],
                "argument_flow": [str(x) for x in argument_flow],
                "evidence_map": evidence_map,
                "changelog": [str(x) for x in changelog],
            }
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Outline revision failed: {exc}") from exc

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
        self._require_chain(self._revise_chain, "Draft revision")
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
            revised_draft = parsed.get("revised_draft")
            changelog = parsed.get("changelog")
            if not isinstance(revised_draft, str) or not revised_draft.strip() or not isinstance(changelog, list):
                raise ValueError("invalid draft revision schema")
            return {
                "revised_draft": revised_draft.strip(),
                "changelog": [str(x) for x in changelog],
            }
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Draft revision failed: {exc}") from exc
