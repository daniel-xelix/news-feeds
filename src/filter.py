"""Filter normalized candidate items by time window and seen-urls."""
from __future__ import annotations

from datetime import datetime
from src.types import CandidateItem


def filter_candidates(
    items: list[CandidateItem],
    *,
    since: datetime,
    seen: set[str],
    max_items: int = 100,
) -> list[CandidateItem]:
    """Keep items published >= since, drop ones in seen, dedup by URL, cap."""
    out: list[CandidateItem] = []
    seen_in_run: set[str] = set()
    for item in items:
        if item.url in seen or item.url in seen_in_run:
            continue
        if item.published_at is not None and item.published_at < since:
            continue
        out.append(item)
        seen_in_run.add(item.url)
    return out[:max_items]
