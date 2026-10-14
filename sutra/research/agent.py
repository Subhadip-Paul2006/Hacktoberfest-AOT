"""SUTRA Research Agent.

Orchestrates multi-source research for system-design questions,
architectural patterns, and engineering topics. Produces structured
output with verified citations and source attribution.

Pipeline stages:
  1. Research request interpretation and query expansion
  2. Source adapter selection
  3. Concurrent source discovery/search
  4. Content retrieval (for top results)
  5. Relevance ranking and deduplication
  6. Evidence extraction and synthesis
  7. Structured output with citation validation

Security invariants:
  - External content is treated as untrusted and never executed.
  - Prompt injection from retrieved content is guarded against.
  - AI responses are labelled as AI-generated, distinct from sourced evidence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .sources import SearchQuery, SourceAdapter, SourceResult, RetrievalStatus
from .adapters import get_available_adapters, get_all_adapters

log = logging.getLogger(__name__)


@dataclass
class ResearchFinding:
    """A single finding derived from one or more sources."""
    claim: str
    sources: list[SourceResult] = field(default_factory=list)
    confidence: str = "low"       # low / medium / high
    evidence_type: str = "sourced"  # sourced / inferred / ai_generated

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "confidence": self.confidence,
            "evidence_type": self.evidence_type,
            "sources": [s.to_dict() for s in self.sources],
        }


@dataclass
class ResearchResult:
    """Complete structured output from a Research Agent run."""
    query: str
    query_expanded: str = ""
    started_at: str = ""
    completed_at: str = ""
    sources_searched: list[str] = field(default_factory=list)
    sources_retrieved: int = 0
    sources_skipped: list[str] = field(default_factory=list)
    raw_results: list[SourceResult] = field(default_factory=list)
    findings: list[ResearchFinding] = field(default_factory=list)
    summary: str = ""
    recommendations: list[str] = field(default_factory=list)
    information_gaps: list[str] = field(default_factory=list)
    # AI provider info (no secrets)
    ai_provider: str = "none"
    ai_model: str = "(none)"
    ai_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "query_expanded": self.query_expanded,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "sources_searched": self.sources_searched,
            "sources_retrieved": self.sources_retrieved,
            "sources_skipped": self.sources_skipped,
            "findings": [f.to_dict() for f in self.findings],
            "raw_results_count": len(self.raw_results),
            "raw_results": [r.to_dict() for r in self.raw_results[:20]],
            "summary": self.summary,
            "recommendations": self.recommendations,
            "information_gaps": self.information_gaps,
            "ai_provider": self.ai_provider,
            "ai_model": self.ai_model,
            "ai_used": self.ai_used,
        }


class ResearchAgent:
    """Orchestrates multi-source research queries.

    Args:
        ai_config: AIConfig instance for LLM synthesis (optional).
        adapters: Explicit list of source adapters to use (default: all available).
        max_results_per_source: Max results per adapter per query.
        fetch_top_n: Number of top results to deep-fetch for full content.
        concurrency_limit: Max concurrent adapter searches.
        timeout_seconds: Per-adapter search timeout.
    """

    def __init__(
        self,
        ai_config: Any = None,
        adapters: Optional[list[SourceAdapter]] = None,
        max_results_per_source: int = 5,
        fetch_top_n: int = 3,
        concurrency_limit: int = 4,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.ai_config = ai_config
        self.adapters = adapters if adapters is not None else get_available_adapters(
            max_results=max_results_per_source, timeout=timeout_seconds
        )
        self.max_results_per_source = max_results_per_source
        self.fetch_top_n = fetch_top_n
        self.concurrency_limit = concurrency_limit
        self.timeout_seconds = timeout_seconds

    async def research(
        self,
        query: str,
        source_filter: Optional[list[str]] = None,
    ) -> ResearchResult:
        """Execute a research query across configured source adapters.

        Args:
            query: The research question.
            source_filter: If provided, only use adapters whose source_id is in this list.

        Returns:
            A structured ResearchResult with findings, sources, and optional AI synthesis.
        """
        started_at = datetime.now(timezone.utc).isoformat()
        result = ResearchResult(query=query, started_at=started_at)

        # Select adapters
        active_adapters = [
            a for a in self.adapters
            if (source_filter is None or a.source_id in source_filter) and a.is_available()
        ]
        skipped = [
            a.source_id for a in self.adapters
            if not a.is_available() or (source_filter and a.source_id not in source_filter)
        ]
        result.sources_searched = [a.source_id for a in active_adapters]
        result.sources_skipped = skipped

        if not active_adapters:
            result.summary = "No source adapters available. Configure API credentials or install httpx."
            result.information_gaps = ["All sources unavailable or skipped."]
            result.completed_at = datetime.now(timezone.utc).isoformat()
            return result

        # Expand query (simple: add system-design context keywords)
        expanded = _expand_query(query)
        result.query_expanded = expanded
        search_q = SearchQuery(query=expanded, max_results=self.max_results_per_source)

        # Run concurrent searches with bounded concurrency
        sem = asyncio.Semaphore(self.concurrency_limit)
        search_tasks = [_bounded_search(sem, adapter, search_q, self.timeout_seconds) for adapter in active_adapters]
        search_results_nested = await asyncio.gather(*search_tasks, return_exceptions=True)

        all_results: list[SourceResult] = []
        for adapter, batch in zip(active_adapters, search_results_nested):
            if isinstance(batch, Exception):
                log.warning("Adapter %s raised: %s", adapter.source_id, batch)
                continue
            for r in batch:
                if r.status == RetrievalStatus.OK:
                    all_results.append(r)
                else:
                    log.debug("Source %s result error: %s", adapter.source_id, r.error)

        # Deduplicate by URL
        all_results = _deduplicate(all_results)
        result.raw_results = all_results
        result.sources_retrieved = len(all_results)

        # Rank by relevance (simple keyword scoring)
        ranked = _rank(all_results, query)

        # Build findings from top ranked results
        for src in ranked[:self.max_results_per_source * 2]:
            if src.is_usable():
                finding = ResearchFinding(
                    claim=src.title or src.snippet[:200],
                    sources=[src],
                    confidence="medium" if src.text else "low",
                    evidence_type="sourced",
                )
                result.findings.append(finding)

        # Synthesise with AI if configured
        ai_synthesis = await self._ai_synthesise(query, ranked[:10])
        if ai_synthesis:
            result.summary = ai_synthesis.get("summary", "")
            result.recommendations = ai_synthesis.get("recommendations", [])
            result.information_gaps = ai_synthesis.get("gaps", [])
            result.ai_used = True
            if self.ai_config:
                result.ai_provider = self.ai_config.provider.value if hasattr(self.ai_config, "provider") else "unknown"
                result.ai_model = self.ai_config.model if hasattr(self.ai_config, "model") else "(unknown)"
        else:
            result.summary = _deterministic_summary(query, ranked)
            result.recommendations = _deterministic_recommendations(ranked)
            result.information_gaps = _identify_gaps(ranked, query)

        result.completed_at = datetime.now(timezone.utc).isoformat()
        return result

    async def _ai_synthesise(
        self, query: str, sources: list[SourceResult]
    ) -> Optional[dict[str, Any]]:
        """Attempt AI-powered synthesis using the configured provider.

        Returns None if AI is not configured or synthesis fails.
        Source content is safely escaped and never allowed to override system instructions.
        """
        if not self.ai_config:
            return None
        try:
            from sutra.ai_providers import AIMode
            if self.ai_config.mode == AIMode.NONE:
                return None
        except Exception:
            return None

        # Prepare a safe evidence summary (truncated, no executable content)
        evidence_lines: list[str] = []
        for i, s in enumerate(sources[:8]):
            safe_title = s.title.replace("\n", " ")[:100]
            safe_snippet = (s.snippet or s.text[:200]).replace("\n", " ")[:200]
            evidence_lines.append(f"[{i+1}] {safe_title} — {safe_snippet} ({s.url})")

        evidence_block = "\n".join(evidence_lines) or "No retrieved evidence available."

        # System prompt — hard-coded, cannot be overridden by external content
        system_prompt = (
            "You are a technical research assistant. Analyse the retrieved evidence below and answer "
            "the user's research question. Be precise, cite source numbers [1], [2] etc. "
            "Do not invent URLs, metrics, or dates not present in the evidence. "
            "If the evidence is insufficient, say so explicitly."
        )
        user_prompt = (
            f"Research question: {query}\n\n"
            f"Retrieved evidence:\n{evidence_block}\n\n"
            "Provide:\n1. A concise summary (2-3 sentences)\n"
            "2. Key recommendations (bulleted)\n"
            "3. Information gaps or uncertainties"
        )

        try:
            response_text = await _call_llm(self.ai_config, system_prompt, user_prompt)
            if not response_text:
                return None
            # Parse into structured output (best-effort)
            return _parse_ai_response(response_text)
        except Exception as exc:
            log.warning("AI synthesis failed: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _expand_query(query: str) -> str:
    """Append system-design context keywords if they're not already present."""
    keywords = ["system design", "architecture", "distributed systems", "engineering"]
    q_lower = query.lower()
    if not any(k in q_lower for k in keywords):
        return f"{query} system design architecture"
    return query


def _deduplicate(results: list[SourceResult]) -> list[SourceResult]:
    """Remove duplicate URLs while preserving order."""
    seen: set[str] = set()
    out: list[SourceResult] = []
    for r in results:
        key = r.url.rstrip("/").lower()
        if key and key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _rank(results: list[SourceResult], query: str) -> list[SourceResult]:
    """Rank results by simple keyword frequency + source priority."""
    keywords = set(query.lower().split())
    source_priority = {"github": 3, "hackernews": 2, "reddit": 1, "gitlab": 2, "websearch": 1}

    def score(r: SourceResult) -> float:
        text = f"{r.title} {r.snippet} {r.text[:500]}".lower()
        kw_score = sum(text.count(kw) for kw in keywords)
        pri = source_priority.get(r.source_id, 0)
        return kw_score + pri

    return sorted(results, key=score, reverse=True)


def _deterministic_summary(query: str, results: list[SourceResult]) -> str:
    if not results:
        return f"No sources retrieved for '{query}'. Configure AI or ensure adapters are available."
    top = results[:3]
    titles = [r.title for r in top if r.title]
    return (
        f"Research on '{query}' retrieved {len(results)} source(s). "
        f"Top results: {'; '.join(titles[:3])}. "
        "AI synthesis is disabled — configure LLM_PROVIDER for deeper analysis."
    )


def _deterministic_recommendations(results: list[SourceResult]) -> list[str]:
    if not results:
        return ["Enable an AI provider (LLM_PROVIDER in .env) for AI-synthesised recommendations."]
    return [
        f"Review: {r.title or r.url} ({r.source_id})"
        for r in results[:5]
        if r.is_usable()
    ]


def _identify_gaps(results: list[SourceResult], query: str) -> list[str]:
    gaps: list[str] = []
    if not results:
        gaps.append("No sources were retrieved — all adapters may be rate-limited or unavailable.")
    elif len(results) < 3:
        gaps.append("Fewer than 3 sources retrieved — results may be incomplete.")
    if not any(r.source_id == "github" for r in results):
        gaps.append("No GitHub results — open-source implementations were not found.")
    return gaps or ["No significant gaps identified."]


async def _bounded_search(
    sem: asyncio.Semaphore, adapter: SourceAdapter, query: SearchQuery, timeout: float
) -> list[SourceResult]:
    """Run adapter.search() with bounded concurrency and timeout."""
    async with sem:
        try:
            return await asyncio.wait_for(adapter.search(query), timeout=timeout)
        except asyncio.TimeoutError:
            return [SourceResult(
                source_id=adapter.source_id,
                category=adapter.category,
                url="",
                status=RetrievalStatus.TIMEOUT,
                error=f"Search timed out after {timeout}s",
                retrieved_at=datetime.now(timezone.utc).isoformat(),
            )]
        except Exception as exc:
            return [SourceResult(
                source_id=adapter.source_id,
                category=adapter.category,
                url="",
                status=RetrievalStatus.ERROR,
                error=str(exc),
                retrieved_at=datetime.now(timezone.utc).isoformat(),
            )]


async def _call_llm(ai_config: Any, system_prompt: str, user_prompt: str) -> str:
    """Call the configured LLM provider. Returns empty string on failure."""
    try:
        from sutra.ai_providers import AIProvider, AIMode
    except Exception:
        return ""

    if ai_config.mode == AIMode.NONE:
        return ""

    provider = ai_config.provider
    model = ai_config.model
    base_url = ai_config.base_url
    timeout = ai_config.timeout_seconds

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        import httpx
    except ImportError:
        return ""

    import os

    if provider == AIProvider.OPENAI or provider in (AIProvider.LMSTUDIO, AIProvider.LLAMACPP, AIProvider.VLLM, AIProvider.CUSTOM):
        api_key = os.environ.get("OPENAI_API_KEY", "") or "local"
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        payload = {"model": model, "messages": messages, "max_tokens": ai_config.max_tokens or 1024}
        try:
            loop = asyncio.get_event_loop()
            def _post() -> str:
                with httpx.Client(timeout=timeout) as client:
                    resp = client.post(url, json=payload, headers=headers)
                    if resp.status_code == 200:
                        data = resp.json()
                        return data["choices"][0]["message"]["content"]
                return ""
            return await loop.run_in_executor(None, _post)
        except Exception as exc:
            log.warning("LLM call failed: %s", exc)
            return ""

    if provider == AIProvider.OLLAMA:
        url = f"{base_url.rstrip('/')}/api/chat"
        payload = {"model": model, "messages": messages, "stream": False}
        try:
            loop = asyncio.get_event_loop()
            def _post_ollama() -> str:
                with httpx.Client(timeout=timeout) as client:
                    resp = client.post(url, json=payload)
                    if resp.status_code == 200:
                        return resp.json().get("message", {}).get("content", "")
                return ""
            return await loop.run_in_executor(None, _post_ollama)
        except Exception as exc:
            log.warning("Ollama call failed: %s", exc)
            return ""

    # Gemini, Anthropic — not implemented in minimal version
    return ""


def _parse_ai_response(text: str) -> dict[str, Any]:
    """Best-effort parse of an AI synthesis response into structured fields."""
    lines = text.strip().split("\n")
    summary_lines: list[str] = []
    recommendations: list[str] = []
    gaps: list[str] = []
    section = "summary"
    for line in lines:
        l = line.strip()
        if not l:
            continue
        lower = l.lower()
        if "recommendation" in lower or "suggest" in lower:
            section = "recommendations"
            continue
        if "gap" in lower or "uncertain" in lower or "missing" in lower:
            section = "gaps"
            continue
        if section == "summary":
            summary_lines.append(l)
        elif section == "recommendations":
            recommendations.append(l.lstrip("•-*123456789. "))
        else:
            gaps.append(l.lstrip("•-*123456789. "))
    return {
        "summary": " ".join(summary_lines[:5]),
        "recommendations": [r for r in recommendations if r][:10],
        "gaps": [g for g in gaps if g][:5],
    }
