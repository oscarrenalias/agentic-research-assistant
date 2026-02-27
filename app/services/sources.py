from __future__ import annotations

import html
import io
import re
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from app.domain.models import now_iso

try:
    from pypdf import PdfReader
except Exception:  # noqa: BLE001
    PdfReader = None  # type: ignore[assignment]


def maybe_url(value: str) -> str | None:
    parsed = urlparse(value.strip())
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value.strip()
    return None


def infer_source_tier(source_ref: str) -> int:
    value = source_ref.lower()
    if any(token in value for token in ["iea", "iaea", "doe", "energy.gov", "oecd", "nea"]):
        return 1
    return 2


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _extract_html_text(raw_html: str) -> tuple[str, str, str]:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", raw_html, flags=re.IGNORECASE | re.DOTALL)
    title = html.unescape(_clean_text(title_match.group(1))) if title_match else ""

    published_match = re.search(
        r"""<meta[^>]+(?:property|name)=["'](?:article:published_time|publishdate|datePublished|pubdate)["'][^>]+content=["']([^"']+)["']""",
        raw_html,
        flags=re.IGNORECASE,
    )
    published_at = _clean_text(published_match.group(1)) if published_match else ""

    no_scripts = re.sub(r"<script\b[^<]*(?:(?!</script>)<[^<]*)*</script>", " ", raw_html, flags=re.IGNORECASE)
    no_styles = re.sub(r"<style\b[^<]*(?:(?!</style>)<[^<]*)*</style>", " ", no_scripts, flags=re.IGNORECASE)
    no_tags = re.sub(r"<[^>]+>", " ", no_styles)
    text = html.unescape(_clean_text(no_tags))
    return title, published_at, text


def _looks_like_pdf(url: str, content_type: str, raw_bytes: bytes) -> bool:
    lower_url = url.lower()
    if ".pdf" in lower_url or "application/pdf" in content_type:
        return True
    return raw_bytes.startswith(b"%PDF-")


def _extract_pdf_text(raw_bytes: bytes) -> tuple[str, str]:
    if PdfReader is None:
        return "", ""
    try:
        reader = PdfReader(io.BytesIO(raw_bytes))
        title = _clean_text(str(getattr(reader.metadata, "title", "") or ""))
        page_text: list[str] = []
        for page in reader.pages[:80]:
            extracted = page.extract_text() or ""
            cleaned = _clean_text(extracted)
            if cleaned:
                page_text.append(cleaned)
        return title, _clean_text(" ".join(page_text))
    except Exception:  # noqa: BLE001
        return "", ""


def _extract_search_result_url(href: str) -> str:
    value = href.strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc and "duckduckgo.com" not in parsed.netloc:
        return value
    if "duckduckgo.com/l/" in value:
        qs = parse_qs(parsed.query)
        candidate = unquote(str((qs.get("uddg") or [""])[0]))
        candidate_parsed = urlparse(candidate)
        if candidate_parsed.scheme in {"http", "https"} and candidate_parsed.netloc:
            return candidate
    return ""


def resolve_source_url(source_ref: str, *, timeout_s: float = 8.0) -> str:
    # If source_ref is already a URL, keep it unchanged.
    direct = maybe_url(source_ref)
    if direct:
        return direct

    query = quote_plus(source_ref.strip())
    if not query:
        return ""
    search_url = f"https://duckduckgo.com/html/?q={query}"
    try:
        req = urllib.request.Request(
            search_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; AgenticTasksResearch/0.1; +https://example.local)"
                )
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            raw_html = response.read(200_000).decode("utf-8", errors="replace")
        links = re.findall(r'href=["\']([^"\']+)["\']', raw_html, flags=re.IGNORECASE)
        for href in links:
            candidate = _extract_search_result_url(href)
            if candidate:
                return candidate
    except (TimeoutError, urllib.error.URLError, ValueError):
        return ""
    return ""


def fetch_source_material(source_ref: str, *, timeout_s: float = 8.0) -> dict[str, str]:
    retrieved_at = now_iso()
    url = maybe_url(source_ref) or resolve_source_url(source_ref, timeout_s=timeout_s)
    if not url:
        return {
            "source_ref": source_ref,
            "url": "",
            "title": _clean_text(source_ref)[:120],
            "publisher": "",
            "published_at": "",
            "retrieved_at": retrieved_at,
            "source_material": _clean_text(source_ref)[:4000],
            "fetch_status": "inline_text",
        }

    parsed = urlparse(url)
    publisher = parsed.netloc.lower()
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; AgenticTasksResearch/0.1; +https://example.local)"
                )
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            raw_bytes = response.read(2_500_000)
            content_type = response.headers.get("Content-Type", "").lower()
        title = ""
        published_at = ""
        extracted = ""
        if _looks_like_pdf(url, content_type, raw_bytes):
            title, extracted = _extract_pdf_text(raw_bytes)
            if not extracted:
                extracted = _clean_text(raw_bytes.decode("latin-1", errors="replace"))
        else:
            text = raw_bytes.decode("utf-8", errors="replace")
            if "html" in content_type or "<html" in text[:500].lower():
                title, published_at, extracted = _extract_html_text(text)
            else:
                extracted = _clean_text(text)

        if not extracted:
            extracted = _clean_text(source_ref)

        return {
            "source_ref": source_ref,
            "url": url,
            "title": title[:160] if title else _clean_text(source_ref)[:120],
            "publisher": publisher,
            "published_at": published_at[:50],
            "retrieved_at": retrieved_at,
            "source_material": extracted[:8000],
            "fetch_status": "fetched",
        }
    except (TimeoutError, urllib.error.URLError, ValueError):
        return {
            "source_ref": source_ref,
            "url": url,
            "title": _clean_text(source_ref)[:120],
            "publisher": publisher,
            "published_at": "",
            "retrieved_at": retrieved_at,
            "source_material": _clean_text(source_ref)[:4000],
            "fetch_status": "fetch_failed",
        }
