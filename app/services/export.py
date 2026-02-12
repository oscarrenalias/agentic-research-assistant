from __future__ import annotations

from pathlib import Path

from app.domain.models import RunState


def export_run_markdown(*, state: RunState | None, output_path: Path) -> str:
    if not state:
        return "No active run."

    final_payload = state.artifacts.get("final_post")
    post_text = ""
    references: list[dict[str, str]] = []
    if isinstance(final_payload, dict):
        post_text = str(final_payload.get("post_text", "")).strip()
        raw_refs = final_payload.get("references", [])
        if isinstance(raw_refs, list):
            for item in raw_refs:
                if isinstance(item, dict):
                    references.append(
                        {
                            "source_id": str(item.get("source_id", "")),
                            "title": str(item.get("title", "")),
                            "url": str(item.get("url", "")),
                        }
                    )

    if not post_text:
        revised = state.artifacts.get("revised_draft", {})
        if isinstance(revised, dict):
            post_text = str(revised.get("revised_draft", "")).strip()
        if not post_text:
            post_text = str(state.artifacts.get("first_draft", "")).strip()
        evidence = state.artifacts.get("evidence_pack", {})
        if isinstance(evidence, dict):
            maybe_sources = evidence.get("sources", [])
            if isinstance(maybe_sources, list):
                for source in maybe_sources[:50]:
                    if isinstance(source, dict):
                        references.append(
                            {
                                "source_id": str(source.get("source_id", "")),
                                "title": str(source.get("title", "")),
                                "url": str(source.get("url", "")),
                            }
                        )

    if not post_text:
        return "Nothing to export yet. Complete Draft/Revise first."

    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Post", "", post_text, "", "## References", ""]
    if references:
        for ref in references:
            sid = ref.get("source_id", "")
            title = ref.get("title", "")
            url = ref.get("url", "")
            if url:
                lines.append(f"- [{sid}] {title} - {url}")
            else:
                lines.append(f"- [{sid}] {title}")
    else:
        lines.append("- (none)")
    output_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return f"Exported markdown to `{output_path}`."
