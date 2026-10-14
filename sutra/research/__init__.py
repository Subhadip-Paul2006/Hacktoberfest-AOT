"""SUTRA Research module — public API."""
from .agent import ResearchAgent, ResearchResult, ResearchFinding
from .sources import SourceAdapter, SourceResult, SourceCategory, RetrievalStatus, SearchQuery
from .adapters import (
    GitHubAdapter,
    GitLabAdapter,
    RedditAdapter,
    HackerNewsAdapter,
    WebSearchAdapter,
    XAdapter,
    get_available_adapters,
    get_all_adapters,
)

__all__ = [
    "ResearchAgent",
    "ResearchResult",
    "ResearchFinding",
    "SourceAdapter",
    "SourceResult",
    "SourceCategory",
    "RetrievalStatus",
    "SearchQuery",
    "GitHubAdapter",
    "GitLabAdapter",
    "RedditAdapter",
    "HackerNewsAdapter",
    "WebSearchAdapter",
    "XAdapter",
    "get_available_adapters",
    "get_all_adapters",
]
