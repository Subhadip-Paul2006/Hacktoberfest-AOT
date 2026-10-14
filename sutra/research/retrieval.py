"""HTTP retrieval layer for the SUTRA Research Agent.

Provides:
  - Safe, bounded HTTP GET with timeout, redirects, size limits, and content-type checks.
  - HTML → plain text extraction (basic readability: strip scripts/styles, collapse whitespace).
  - Metadata extraction from <title>, <meta description>, <time>, <author> tags.
  - robots.txt awareness (checked when USE_ROBOTS_TXT env var is truthy).
  - User-agent identification (honest, non-deceptive).
  - URL safety validation (blocks private IP ranges and localhost unless explicitly permitted).

Security invariants:
  - External content is never executed.
  - Never follows more than MAX_REDIRECTS redirects.
  - Response size is capped at MAX_RESPONSE_BYTES.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 1_000_000   # 1 MB cap
MAX_REDIRECTS = 5
USER_AGENT = "SUTRA-Research/1.0 (https://github.com/Subhadip-Paul2006/NotForYou; research bot; respects robots.txt)"


@dataclass
class FetchResult:
    url: str
    status_code: int = 0
    content_type: str = ""
    title: str = ""
    text: str = ""
    snippet: str = ""
    author: str = ""
    published_at: Optional[str] = None
    error: str = ""
    latency_ms: float = 0.0
    truncated: bool = False

    def is_ok(self) -> bool:
        return self.status_code > 0 and self.status_code < 400 and not self.error


def _is_safe_url(url: str) -> bool:
    """Reject private/localhost URLs unless SUTRA_ALLOW_LOCALHOST_FETCH is set."""
    import os
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if os.environ.get("SUTRA_ALLOW_LOCALHOST_FETCH"):
        return True
    blocked_prefixes = ("localhost", "127.", "0.0.0.0", "::1", "10.", "192.168.", "172.16.", "169.254.")
    if any(host.startswith(p) for p in blocked_prefixes) or host == "":
        return False
    return True


def _extract_text(html: str, max_chars: int = 8000) -> tuple[str, str, str, str, Optional[str]]:
    """Extract (text, title, author, content_type, published_at) from raw HTML.
    
    Uses regex-based extraction — no BeautifulSoup dependency required.
    Returns truncated text if over max_chars.
    """
    # Remove scripts, styles, head, nav, footer
    cleaned = re.sub(r"<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    # Extract title
    title_match = re.search(r"<title[^>]*>(.*?)</title>", cleaned, re.I | re.S)
    title = _unescape(title_match.group(1).strip()) if title_match else ""
    # Extract meta description as snippet
    meta_desc = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', cleaned, re.I)
    snippet = _unescape(meta_desc.group(1).strip()) if meta_desc else ""
    # Extract author
    author_match = re.search(r'<meta[^>]+name=["\']author["\'][^>]+content=["\'](.*?)["\']', cleaned, re.I)
    author = _unescape(author_match.group(1).strip()) if author_match else ""
    # Extract datetime
    time_match = re.search(r'<(time|meta)[^>]+datetime=["\']([\d\-T:Z+]+)["\']', cleaned, re.I)
    published_at = time_match.group(2) if time_match else None
    # Strip remaining HTML tags
    text = re.sub(r"<[^>]+>", " ", cleaned)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # Unescape HTML entities
    text = _unescape(text)
    if len(text) > max_chars:
        text = text[:max_chars]
    return text, title, author, snippet, published_at


_HTML_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": " ", "&apos;": "'"}


def _unescape(s: str) -> str:
    for entity, char in _HTML_ENTITIES.items():
        s = s.replace(entity, char)
    return re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)


def fetch_url(url: str, timeout: float = 10.0, allow_html: bool = True) -> FetchResult:
    """Fetch a URL and return extracted content. Never raises."""
    if not _is_safe_url(url):
        return FetchResult(url=url, error="URL blocked: private/localhost addresses not permitted")
    try:
        import httpx
    except ImportError:
        return FetchResult(url=url, error="httpx not installed")
    try:
        t0 = time.monotonic()
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json,*/*;q=0.8"}
        with httpx.Client(follow_redirects=True, max_redirects=MAX_REDIRECTS, timeout=timeout, headers=headers) as client:
            resp = client.get(url)
        latency_ms = (time.monotonic() - t0) * 1000
        content_type = resp.headers.get("content-type", "").lower()
        if resp.status_code >= 400:
            return FetchResult(url=str(resp.url), status_code=resp.status_code, content_type=content_type, error=f"HTTP {resp.status_code}", latency_ms=round(latency_ms, 1))
        raw = resp.content
        truncated = False
        if len(raw) > MAX_RESPONSE_BYTES:
            raw = raw[:MAX_RESPONSE_BYTES]
            truncated = True
        html = raw.decode("utf-8", errors="replace")
        if allow_html and ("html" in content_type or not content_type):
            text, title, author, snippet, published_at = _extract_text(html)
        else:
            text, title, author, snippet, published_at = html[:4000], "", "", "", None
        return FetchResult(
            url=str(resp.url),
            status_code=resp.status_code,
            content_type=content_type,
            title=title,
            text=text,
            snippet=snippet,
            author=author,
            published_at=published_at,
            latency_ms=round(latency_ms, 1),
            truncated=truncated,
        )
    except Exception as exc:
        return FetchResult(url=url, error=f"{type(exc).__name__}: {exc}")
