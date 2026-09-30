"""Pydantic models for the news-tracker pipeline."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


SourceType = Literal[
    "rss",          # generic RSS/Atom via feedparser
    "json_hn",      # Hacker News Algolia API
    "json_reddit",  # Reddit JSON API
    "json_github",  # GitHub releases
    "html",         # BeautifulSoup-based scraper
    "tldr_ai_index",  # TLDR AI: RSS lists issue URLs, then per-issue HTML scrape
]


class SourceConfig(BaseModel):
    name: str
    type: SourceType
    url: str
    weight: float = 1.0
    min_score: int | None = None  # for HN/Reddit, drop items below score


class CandidateItem(BaseModel):
    """A single news candidate after normalization, before LLM curation."""
    url: str
    source: str
    title: str
    snippet: str
    published_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


# url/title are the load-bearing fields; the model occasionally omits the
# free-text why/summary on large (catch-up / launch-heavy) responses. Default
# those to "" so one under-specified item degrades gracefully instead of
# failing json→schema validation for the entire brief.
class MustRead(BaseModel):
    url: str
    title: str
    why: str = ""


class BriefItem(BaseModel):
    url: str
    title: str
    summary: str = ""
    why: str = ""


SectionKey = Literal[
    "tools_and_frameworks",
    "open_models_and_local",
    "industry_and_trends",
    "org_and_leadership",
]


class BriefResponse(BaseModel):
    """The structured response we expect back from the LLM."""
    model_config = ConfigDict(extra="forbid")

    # Up to 3 ultra-short news names ("Opus 4.7", "Anthropic SpaceX/xAI Deal").
    # Default empty so old fixtures and quiet days still validate.
    headlines: list[str] = Field(default_factory=list)
    quiet_day: bool
    lead_summary: str
    must_reads: list[MustRead]
    sections: dict[SectionKey, list[BriefItem]]


class ThemeItem(BaseModel):
    url: str
    title: str
    summary: str
    source: str


class WeeklyTheme(BaseModel):
    title: str
    narrative: str
    items: list[ThemeItem]


class WatchListEntry(BaseModel):
    topic: str
    why: str
    items: list[ThemeItem]


class DeepRead(BaseModel):
    url: str
    title: str
    why: str


class QuoteOfWeek(BaseModel):
    text: str
    source: str
    url: str


class Launch(BaseModel):
    """A discrete product / model / feature / deal release in the window.
    Enumerated by the weekly LLM call so landmark items never get buried in
    thematic synthesis (TLDR-AI-style coverage)."""
    name: str  # short label, e.g. "GPT-5.5 Instant", "Claude Code Worktrees"
    url: str
    one_liner: str  # 1 sentence: what shipped and what it does
    source: str  # source name where it was reported
    # One of: model, feature, product, deal, release, research. Loose typing
    # because the LLM occasionally invents adjacent labels and we don't want
    # to hard-fail on those.
    kind: str = "release"


class TrendingRepo(BaseModel):
    """A GitHub repo for the weekly trending section. Sourced separately from
    the LLM curation pipeline (direct GitHub Search API call)."""
    full_name: str
    html_url: str
    stars: int
    language: str | None = None
    topics: list[str] = Field(default_factory=list)
    description: str = ""


class WeeklyResponse(BaseModel):
    """Structured response for the Friday weekly digest. Shape is intentionally
    different from BriefResponse — weekly is editorial / thematic, not a
    must-read list."""
    model_config = ConfigDict(extra="forbid")

    headlines: list[str] = Field(default_factory=list)
    lead: str
    launches: list[Launch] = Field(default_factory=list)  # 6-15 entries; mandatory
    themes: list[WeeklyTheme]
    watch_list: list[WatchListEntry]
    deep_read: DeepRead | None = None
    quote_of_week: QuoteOfWeek | None = None
