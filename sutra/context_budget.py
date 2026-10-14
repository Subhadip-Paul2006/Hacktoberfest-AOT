"""LLM context budgeting and prioritized token allocation."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Sequence

log = logging.getLogger(__name__)

# Heuristic: 1 token ≈ 4 characters
CHARS_PER_TOKEN = 4
DEFAULT_TOTAL_TOKEN_BUDGET = 8000
DEFAULT_RESERVED_OUTPUT_TOKENS = 1500


class ContextPriority(IntEnum):
    """Priorities for context retention when trimming to fit budget. Lower number = higher priority."""
    SYSTEM_INSTRUCTIONS = 1
    USER_PROMPT = 2
    ERRORS_AND_FAILURES = 3
    RELEVANT_CODE_SNIPPETS = 4
    RECENT_AGENT_EVENTS = 5
    HISTORICAL_OUTPUT = 6


@dataclass
class ContextItem:
    item_id: str
    content: str
    priority: ContextPriority
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def token_estimate(self) -> int:
        return max(1, (len(self.content) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


class ContextBudgetManager:
    """Manages token budget for LLM requests, ensuring total input + reserved output <= budget."""

    def __init__(
        self,
        total_token_budget: int = DEFAULT_TOTAL_TOKEN_BUDGET,
        reserved_output_tokens: int = DEFAULT_RESERVED_OUTPUT_TOKENS,
    ) -> None:
        self.total_token_budget = max(500, total_token_budget)
        self.reserved_output_tokens = min(reserved_output_tokens, self.total_token_budget // 2)
        self.available_input_tokens = self.total_token_budget - self.reserved_output_tokens
        self._items: list[ContextItem] = []

    def add_item(
        self,
        item_id: str,
        content: str,
        priority: ContextPriority = ContextPriority.RECENT_AGENT_EVENTS,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Add a context item to the working collection."""
        if not content:
            return
        self._items.append(ContextItem(
            item_id=item_id,
            content=content,
            priority=priority,
            metadata=metadata or {},
        ))

    def clear(self) -> None:
        self._items.clear()

    def assemble_context(self) -> dict[str, Any]:
        """Assemble items strictly within the available input token budget.
        
        Trims lowest priority items first.
        """
        # Sort by priority ascending (1 = highest priority first)
        sorted_items = sorted(self._items, key=lambda it: it.priority.value)

        selected: list[ContextItem] = []
        used_tokens = 0
        dropped_count = 0

        for item in sorted_items:
            tokens = item.token_estimate
            if used_tokens + tokens <= self.available_input_tokens:
                selected.append(item)
                used_tokens += tokens
            else:
                # If high-priority item doesn't fit completely, see if partial slice can be added
                remaining_tokens = self.available_input_tokens - used_tokens
                if remaining_tokens > 50 and item.priority in (
                    ContextPriority.SYSTEM_INSTRUCTIONS,
                    ContextPriority.USER_PROMPT,
                    ContextPriority.ERRORS_AND_FAILURES,
                    ContextPriority.RELEVANT_CODE_SNIPPETS,
                ):
                    allowed_chars = remaining_tokens * CHARS_PER_TOKEN
                    trimmed_content = item.content[:allowed_chars] + "\n[...TRUNCATED TO FIT CONTEXT BUDGET]"
                    selected.append(ContextItem(
                        item_id=item.item_id,
                        content=trimmed_content,
                        priority=item.priority,
                        metadata={**item.metadata, "truncated": True},
                    ))
                    used_tokens += remaining_tokens
                else:
                    dropped_count += 1

        # Re-sort selected items back into natural chronological / priority order
        final_text = "\n\n".join(item.content for item in selected)

        return {
            "text": final_text,
            "used_input_tokens": used_tokens,
            "available_input_tokens": self.available_input_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "total_token_budget": self.total_token_budget,
            "items_included": len(selected),
            "items_dropped": dropped_count,
        }
