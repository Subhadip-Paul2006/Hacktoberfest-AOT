"""Concrete source adapters for the SUTRA Research Agent.

Adapters implemented:
  - GitHubAdapter     — public GitHub repositories and README via official REST API
  - GitLabAdapter     — public GitLab projects via official REST API
  - RedditAdapter     — public Reddit posts via public JSON API (no auth required for read)
  - WebSearchAdapter  — DuckDuckGo HTML search (no API key; rate-limited)
  - HackerNewsAdapter — HN Algolia search API (public, no auth)

Notes:
  - All adapters respect rate limits and timeouts.
  - Credentials are read from environment variables only.
  - API keys are never logged.
  - Adapters that lack credentials are skipped gracefully.
  - X/Twitter is NOT implemented: their API requires paid access and does not
    permit the use case described. An empty stub is provided so it can be
    configured without breaking the pipeline.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote_plus, urlparse

from .sources import SearchQuery, SourceAdapter, SourceCategory, SourceResult, RetrievalStatus, _iso_now
from .retrieval import fetch_url

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared HTTP helper (async wrapper around synchronous httpx)
# ---------------------------------------------------------------------------

async def _async_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, str, dict[str, str]]:
    """Run a synchronous httpx GET in an executor. Returns (status, body, resp_headers)."""
    try:
        import httpx
    except ImportError:
        return 0, "", {}
    loop = asyncio.get_event_loop()
    def _do_get() -> tuple[int, str, dict[str, str]]:
        try:
            with httpx.Client(follow_redirects=True, max_redirects=5, timeout=timeout, headers=headers) as client:
                resp = client.get(url)
                return resp.status_code, resp.text, dict(resp.headers)
        except Exception as exc:
            return 0, str(exc), {}
    return await loop.run_in_executor(None, _do_get)


def _gh_headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("SUTRA_GITHUB_TOKEN", "")
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
         "User-Agent": "SUTRA-Research/1.0"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _gl_headers() -> dict[str, str]:
    token = os.environ.get("GITLAB_TOKEN", "") or os.environ.get("SUTRA_GITLAB_TOKEN", "")
    h = {"User-Agent": "SUTRA-Research/1.0"}
    if token:
        h["PRIVATE-TOKEN"] = token
    return h


# ---------------------------------------------------------------------------
# GitHub adapter
# ---------------------------------------------------------------------------

class GitHubAdapter(SourceAdapter):
    source_id = "github"
    display_name = "GitHub"
    category = SourceCategory.CODE_REPOSITORY
    requires_credentials = False   # Public search works without token (rate-limited)
    credential_env_var = "GITHUB_TOKEN"

    async def search(self, query: SearchQuery) -> list[SourceResult]:
        q = quote_plus(f"{query.query} in:readme,description")
        url = f"https://api.github.com/search/repositories?q={q}&sort=stars&order=desc&per_page={min(query.max_results, 10)}"
        status, body, _ = await _async_get(url, _gh_headers(), self.timeout_seconds)
        results: list[SourceResult] = []
        if status == 0:
            return [self._make_result_error("https://api.github.com", RetrievalStatus.ERROR, body)]
        if status == 403:
            return [self._make_result_error(url, RetrievalStatus.RATE_LIMITED, "GitHub API rate limit reached")]
        if status != 200:
            return [self._make_result_error(url, RetrievalStatus.ERROR, f"HTTP {status}")]
        try:
            data = json.loads(body)
            items = data.get("items", [])
        except Exception as exc:
            return [self._make_result_error(url, RetrievalStatus.ERROR, str(exc))]
        for item in items[:query.max_results]:
            desc = item.get("description") or ""
            lang = item.get("language") or ""
            stars = item.get("stargazers_count", 0)
            pushed = item.get("pushed_at", "")
            license_name = (item.get("license") or {}).get("spdx_id", "")
            snippet = f"{desc} | Stars: {stars} | Language: {lang}"
            results.append(SourceResult(
                source_id=self.source_id,
                category=self.category,
                url=item.get("html_url", ""),
                title=item.get("full_name", ""),
                snippet=snippet,
                text=desc,
                published_at=pushed[:10] if pushed else None,
                retrieved_at=_iso_now(),
                status=RetrievalStatus.OK,
                license=license_name,
                metadata={"stars": stars, "language": lang, "topics": item.get("topics", [])},
            ))
        return results

    async def fetch(self, url: str) -> SourceResult:
        """Fetch README for a GitHub repo URL."""
        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")
        if len(parts) < 2:
            return self._make_result_error(url, RetrievalStatus.ERROR, "Invalid GitHub repo URL")
        owner, repo = parts[0], parts[1]
        api_url = f"https://api.github.com/repos/{owner}/{repo}/readme"
        status, body, resp_headers = await _async_get(api_url, {**_gh_headers(), "Accept": "application/vnd.github.raw+json"}, self.timeout_seconds)
        if status == 404:
            return self._make_result_error(url, RetrievalStatus.NOT_FOUND, "README not found")
        if status != 200:
            return self._make_result_error(url, RetrievalStatus.ERROR, f"HTTP {status}")
        try:
            data = json.loads(body)
            import base64
            content = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
        except Exception:
            content = body[:4000]
        return SourceResult(
            source_id=self.source_id, category=self.category, url=url,
            title=f"README: {owner}/{repo}", text=content[:6000],
            retrieved_at=_iso_now(), status=RetrievalStatus.OK,
        )


# ---------------------------------------------------------------------------
# GitLab adapter
# ---------------------------------------------------------------------------

class GitLabAdapter(SourceAdapter):
    source_id = "gitlab"
    display_name = "GitLab"
    category = SourceCategory.CODE_REPOSITORY
    requires_credentials = False
    credential_env_var = "GITLAB_TOKEN"

    async def search(self, query: SearchQuery) -> list[SourceResult]:
        q = quote_plus(query.query)
        url = f"https://gitlab.com/api/v4/projects?search={q}&order_by=star_count&sort=desc&per_page={min(query.max_results, 10)}"
        status, body, _ = await _async_get(url, _gl_headers(), self.timeout_seconds)
        results: list[SourceResult] = []
        if status != 200:
            return [self._make_result_error(url, RetrievalStatus.ERROR, f"HTTP {status}")]
        try:
            items = json.loads(body)
        except Exception as exc:
            return [self._make_result_error(url, RetrievalStatus.ERROR, str(exc))]
        for item in items[:query.max_results]:
            desc = item.get("description") or ""
            stars = item.get("star_count", 0)
            results.append(SourceResult(
                source_id=self.source_id,
                category=self.category,
                url=item.get("web_url", ""),
                title=item.get("name_with_namespace", ""),
                snippet=f"{desc} | Stars: {stars}",
                text=desc,
                retrieved_at=_iso_now(),
                status=RetrievalStatus.OK,
                metadata={"stars": stars},
            ))
        return results


# ---------------------------------------------------------------------------
# Reddit adapter (public JSON API — no auth required for reading)
# ---------------------------------------------------------------------------

class RedditAdapter(SourceAdapter):
    source_id = "reddit"
    display_name = "Reddit"
    category = SourceCategory.COMMUNITY
    requires_credentials = False

    _SUBREDDITS = ["programming", "softwarearchitecture", "devops", "compsci", "backend", "SRE", "Python"]

    async def search(self, query: SearchQuery) -> list[SourceResult]:
        q = quote_plus(query.query)
        sub_list = "+".join(self._SUBREDDITS)
        url = f"https://www.reddit.com/r/{sub_list}/search.json?q={q}&restrict_sr=1&sort=relevance&limit={min(query.max_results, 10)}&t=year"
        headers = {"User-Agent": "SUTRA-Research/1.0 (research bot)"}
        status, body, _ = await _async_get(url, headers, self.timeout_seconds)
        results: list[SourceResult] = []
        if status == 0:
            return [self._make_result_error(url, RetrievalStatus.ERROR, body or "Connection failed")]
        if status == 429:
            return [self._make_result_error(url, RetrievalStatus.RATE_LIMITED, "Reddit rate limit")]
        if status != 200:
            return [self._make_result_error(url, RetrievalStatus.ERROR, f"HTTP {status}")]
        try:
            data = json.loads(body)
            posts = data.get("data", {}).get("children", [])
        except Exception as exc:
            return [self._make_result_error(url, RetrievalStatus.ERROR, str(exc))]
        for post in posts[:query.max_results]:
            pd = post.get("data", {})
            permalink = f"https://www.reddit.com{pd.get('permalink', '')}"
            selftext = pd.get("selftext", "")[:2000]
            created = pd.get("created_utc", 0)
            pub_date = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d") if created else None
            score = pd.get("score", 0)
            results.append(SourceResult(
                source_id=self.source_id, category=self.category,
                url=permalink, title=pd.get("title", ""),
                snippet=selftext[:300], text=selftext,
                author=pd.get("author", ""), published_at=pub_date,
                retrieved_at=_iso_now(), status=RetrievalStatus.OK,
                metadata={"score": score, "num_comments": pd.get("num_comments", 0), "subreddit": pd.get("subreddit", "")},
            ))
        return results


# ---------------------------------------------------------------------------
# Hacker News adapter (Algolia API — public, no key)
# ---------------------------------------------------------------------------

class HackerNewsAdapter(SourceAdapter):
    source_id = "hackernews"
    display_name = "Hacker News"
    category = SourceCategory.COMMUNITY
    requires_credentials = False

    async def search(self, query: SearchQuery) -> list[SourceResult]:
        q = quote_plus(query.query)
        url = f"https://hn.algolia.com/api/v1/search?query={q}&tags=story&hitsPerPage={min(query.max_results, 10)}"
        status, body, _ = await _async_get(url, {"User-Agent": "SUTRA-Research/1.0"}, self.timeout_seconds)
        results: list[SourceResult] = []
        if status != 200:
            return [self._make_result_error(url, RetrievalStatus.ERROR, f"HTTP {status}")]
        try:
            data = json.loads(body)
            hits = data.get("hits", [])
        except Exception as exc:
            return [self._make_result_error(url, RetrievalStatus.ERROR, str(exc))]
        for hit in hits[:query.max_results]:
            hn_url = hit.get("url", "") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
            created = hit.get("created_at", "")
            pub_date = created[:10] if created else None
            results.append(SourceResult(
                source_id=self.source_id, category=self.category,
                url=hn_url, title=hit.get("title", ""),
                snippet=f"Points: {hit.get('points', 0)} | Comments: {hit.get('num_comments', 0)}",
                author=hit.get("author", ""), published_at=pub_date,
                retrieved_at=_iso_now(), status=RetrievalStatus.OK,
                metadata={"points": hit.get("points", 0), "hn_id": hit.get("objectID", "")},
            ))
        return results


# ---------------------------------------------------------------------------
# DuckDuckGo web search (HTML scraping — no API key; rate limited)
# ---------------------------------------------------------------------------

class WebSearchAdapter(SourceAdapter):
    """DuckDuckGo HTML search. No API key required. Rate-limited."""
    source_id = "websearch"
    display_name = "Web Search (DuckDuckGo)"
    category = SourceCategory.SEARCH
    requires_credentials = False

    async def search(self, query: SearchQuery) -> list[SourceResult]:
        import re as _re
        q = quote_plus(query.query)
        url = f"https://html.duckduckgo.com/html/?q={q}"
        headers = {"User-Agent": "Mozilla/5.0 (compatible; SUTRA-Research/1.0)"}
        # Rate limit: 2 seconds between requests
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: time.sleep(2))
        status, body, _ = await _async_get(url, headers, self.timeout_seconds)
        results: list[SourceResult] = []
        if status != 200:
            return [self._make_result_error(url, RetrievalStatus.ERROR, f"HTTP {status}")]
        # Extract results from DDG HTML (simple pattern)
        result_blocks = _re.findall(r'<h2 class="result__title".*?</div>', body, _re.S)
        for block in result_blocks[:query.max_results]:
            link_match = _re.search(r'href="([^"]+)"', block)
            title_match = _re.search(r'class="result__a"[^>]*>(.*?)</a>', block, _re.S)
            snippet_match = _re.search(r'class="result__snippet"[^>]*>(.*?)</span>', block, _re.S)
            if not link_match:
                continue
            raw_url = link_match.group(1)
            # DDG redirects: extract actual URL
            if "uddg=" in raw_url:
                from urllib.parse import parse_qs, urlparse as _up
                qs = parse_qs(_up(raw_url).query)
                raw_url = qs.get("uddg", [raw_url])[0]
            title = _re.sub(r"<[^>]+>", "", title_match.group(1)) if title_match else raw_url
            snippet = _re.sub(r"<[^>]+>", "", snippet_match.group(1)) if snippet_match else ""
            results.append(SourceResult(
                source_id=self.source_id, category=self.category,
                url=raw_url, title=title.strip(), snippet=snippet.strip(),
                retrieved_at=_iso_now(), status=RetrievalStatus.OK,
            ))
        if not results:
            results.append(self._make_result_error(url, RetrievalStatus.ERROR, "No results parsed from DuckDuckGo HTML"))
        return results


# ---------------------------------------------------------------------------
# X / Twitter stub (not implemented — paid API required)
# ---------------------------------------------------------------------------

class XAdapter(SourceAdapter):
    """X (formerly Twitter) adapter stub.

    This platform requires a paid API subscription (Basic tier or above)
    for search access. This stub is present so the adapter can be listed
    in diagnostics and configured without breaking the research pipeline.
    """
    source_id = "x"
    display_name = "X (Twitter) — not implemented"
    category = SourceCategory.SOCIAL
    requires_credentials = True
    credential_env_var = "X_BEARER_TOKEN"

    def is_available(self) -> bool:
        return False  # Always unavailable

    def availability_message(self) -> str:
        return "unavailable: X API requires a paid subscription (Basic or Pro tier)"

    async def search(self, query: SearchQuery) -> list[SourceResult]:
        return [SourceResult(
            source_id=self.source_id, category=self.category, url="",
            status=RetrievalStatus.SKIPPED,
            error="X adapter not implemented: paid API required",
            retrieved_at=_iso_now(),
        )]


# ---------------------------------------------------------------------------
# Registry of all available adapters
# ---------------------------------------------------------------------------

ALL_ADAPTERS: list[type[SourceAdapter]] = [
    GitHubAdapter,
    GitLabAdapter,
    RedditAdapter,
    HackerNewsAdapter,
    WebSearchAdapter,
    XAdapter,
]


def get_available_adapters(max_results: int = 5, timeout: float = 10.0) -> list[SourceAdapter]:
    """Return instantiated adapters that are currently available."""
    adapters: list[SourceAdapter] = []
    for cls in ALL_ADAPTERS:
        instance = cls(timeout_seconds=timeout, max_results=max_results)
        if instance.is_available():
            adapters.append(instance)
    return adapters


def get_all_adapters(max_results: int = 5, timeout: float = 10.0) -> list[SourceAdapter]:
    """Return all adapter instances (including unavailable ones)."""
    return [cls(timeout_seconds=timeout, max_results=max_results) for cls in ALL_ADAPTERS]
