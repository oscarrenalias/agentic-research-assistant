from __future__ import annotations

import re
import uuid
from pathlib import Path

from app.domain.models import INLINE_FIELD_RE, SECTION_HEADER_RE, URL_RE, NormalizedTaskPackage, now_iso


def normalize_label(value: str) -> str:
    return value.strip().lower()


def parse_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current_header: str | None = None

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            if current_header:
                sections.setdefault(current_header, []).append("")
            continue

        header_match = SECTION_HEADER_RE.match(stripped)
        if header_match:
            current_header = normalize_label(header_match.group("header"))
            sections.setdefault(current_header, [])
            continue

        inline_match = INLINE_FIELD_RE.match(stripped)
        if inline_match and normalize_label(inline_match.group("key")) in {"title", "objective", "audience"}:
            key = normalize_label(inline_match.group("key"))
            sections.setdefault(key, []).append(inline_match.group("value").strip())
            current_header = key
            continue

        if current_header:
            sections.setdefault(current_header, []).append(stripped)

    return sections


def normalize_bullets(lines: list[str]) -> list[str]:
    items: list[str] = []
    for line in lines:
        item = line.strip()
        if not item:
            continue
        item = re.sub(r"^[-*]\s*", "", item)
        items.append(item)
    return items


def first_nonempty_line(lines: list[str]) -> str:
    for line in lines:
        if line.strip():
            return line.strip()
    return ""


def build_normalized_task(input_path: Path) -> NormalizedTaskPackage:
    text = input_path.read_text(encoding="utf-8")
    section_map = parse_sections(text)

    objective = first_nonempty_line(section_map.get("objective", []))
    audience = first_nonempty_line(section_map.get("audience", []))
    tone = "; ".join(normalize_bullets(section_map.get("tone and style constraints", [])))

    constraints: list[str] = []
    constraints.extend(normalize_bullets(section_map.get("draft output preference", [])))
    constraints.extend(normalize_bullets(section_map.get("questions to answer explicitly", [])))

    missing = []
    if not objective:
        missing.append("objective")
    if not audience:
        missing.append("audience")
    if not tone:
        missing.append("tone")
    if not constraints:
        missing.append("constraints")
    if missing:
        raise ValueError(f"Input brief missing required fields: {', '.join(missing)}")

    source_candidates: list[str] = normalize_bullets(section_map.get("potential sources to investigate", []))
    for match in URL_RE.findall(text):
        if match not in source_candidates:
            source_candidates.append(match)

    run_id = str(uuid.uuid4())
    return NormalizedTaskPackage(
        run_id=run_id,
        created_at=now_iso(),
        input_path=str(input_path),
        objective=objective,
        audience=audience,
        tone=tone,
        constraints=constraints,
        source_candidates=source_candidates,
        title=first_nonempty_line(section_map.get("title", [])) or None,
        key_points=normalize_bullets(section_map.get("core points to explore", [])),
    )
