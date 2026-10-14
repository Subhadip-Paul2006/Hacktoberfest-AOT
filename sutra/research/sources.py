"""Source adapter contract and base classes for the SUTRA Research Agent.

A SourceAdapter encapsulates:
  - Search/discovery of relevant content
  - Content retrieval (fetch)
  - Metadata extraction
  - Controlled rate limiting and timeout handling
  - Graceful failure when a source is unavailable

Security invariants:
  - Credentials are never logged or included in result representations.
  - External content is treated as untrusted input and never executed.
  - URL and redirect validation is enforced by each adapter.
  - robots.txt directives and rate limits are respected.
"""
from __future__ import annotations

import abc
import enum
import time
from dataclasses import dataclass, field
from typing import Any, Optional


class SourceCategory(str, enum.Enum):
    """High-level category of a research source."""
    CODE_REPOSITORY = "code_repository"   # GitHub, GitLab
    COMMUNITY = "community"               # Reddit, HN, dev.to
    SOCIAL = "social"                     # X (Twitter)
    DOCUMENTATION = "documentation"       # Official docs
    ARTICLE = "article"                   # Engineering blogs, Medium
    SEARCH = "search"                     # Web search aggregator
    OTHER = "other"


class RetrievalStatus(str, enum.Enum):
    """Retrieval outcome for a single source result."""
    OK = "ok"
    RATE_LIMITED = "rate_limited"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    TIMEOUT = "timeout"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass
class SourceResult:
    """Normalised result from any source adapter."""
    source_id: str                       # Adapter identifier
    category: SourceCategory
    url: str                             # Canonical URL
    title: str = ""
    author: str = ""
    published_at: Optional[str] = None   # ISO-8601 date or None
    retrieved_at: str = ""               # ISO-8601 timestamp of this fetch
    text: str = ""                       # Extracted plain text
    snippet: str = ""                    # Short excerpt for quick ranking
    status: RetrievalStatus = RetrievalStatus.OK
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    # Citation fields
    license: str = ""
    content_type: str = ""

    def is_usable(self) -> bool:
        return self.status == RetrievalStatus.OK and bool(self.text or self.snippet)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "category": self.category.value,
            "url": self.url,
            "title": self.title,
            "author": self.author,
            "published_at": self.published_at,
            "retrieved_at": self.retrieved_at,
            "text": self.text[:2000] if self.text else "",  # truncate for output
            "snippet": self.snippet,
            "status": self.status.value,
            "error": self.error,
            "license": self.license,
        }


@dataclass
class SearchQuery:
    """Normalised search request passed to source adapters."""
    query: str
    max_results: int = 5
    language: str = "en"
    date_range_days: Optional[int] = None
    extra: dict[str, Any] = field(default_factory=dict)


class SourceAdapter(abc.ABC):
    """Abstract base class for all research source adapters."""

    #: Unique identifier for this adapter (lowercase, no spaces)
    source_id: str = ""

    #: Human-readable display name
    display_name: str = ""

    #: Category of this source
    category: SourceCategory = SourceCategory.OTHER

    #: Whether this adapter requires credentials to function
    requires_credentials: bool = False

    #: Credential environment variable name (if required)
    credential_env_var: str = ""

    def __init__(
        self,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        max_results: int = 5,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_results = max_results
        self._last_request_time: float = 0.0

    def is_available(self) -> bool:
        """Return True if this adapter can be used with current configuration."""
        if not self.requires_credentials:
            return True
        import os
        val = os.environ.get(self.credential_env_var, "").strip()
        return bool(val)

    def availability_message(self) -> str:
        """Human-readable availability status for diagnostics."""
        if not self.requires_credentials:
            return "available (no credentials required)"
        if self.is_available():
            return f"available (credential {self.credential_env_var} present)"
        return f"unavailable (set {self.credential_env_var} to enable)"

    @abc.abstractmethod
    async def search(self, query: SearchQuery) -> list[SourceResult]:
        """Search for content matching the query. Never raises — returns errors in results."""

    async def fetch(self, url: str) -> SourceResult:
        """Fetch a specific URL. Default raises NotImplementedError."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support direct fetch")

    def _make_result_error(self, url: str, status: RetrievalStatus, error: str) -> SourceResult:
        return SourceResult(
            source_id=self.source_id,
            category=self.category,
            url=url,
            status=status,
            error=error,
            retrieved_at=_iso_now(),
        )

    def _rate_limit(self, min_interval: float = 1.0) -> None:
        """Enforce a minimum interval between requests."""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_request_time = time.monotonic()


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
