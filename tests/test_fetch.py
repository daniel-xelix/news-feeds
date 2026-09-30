import json
from datetime import datetime, timezone
import httpx
import pytest
from pytest_httpx import HTTPXMock
from pathlib import Path
from src.fetch import fetch_rss, fetch_trending_repos, load_sources
from src.types import SourceConfig, TrendingRepo


def test_load_sources_returns_list():
    sources = load_sources()
    assert len(sources) >= 30
    assert all(isinstance(s, SourceConfig) for s in sources)
    names = {s.name for s in sources}
    assert "Hacker News (AI)" in names
    assert "Hugging Face blog" in names
    assert "OpenAI blog" in names
    assert "Cursor changelog" in names
    assert "GitHub: anthropics/claude-code" in names


@pytest.fixture
def fixtures_dir():
    return Path(__file__).parent / "fixtures"


async def test_fetch_rss_parses_atom(httpx_mock: HTTPXMock, fixtures_dir):
    body = (fixtures_dir / "sample_atom.xml").read_text()
    httpx_mock.add_response(url="https://example.com/feed", text=body)
    cfg = SourceConfig(name="Sample", type="rss", url="https://example.com/feed")
    async with httpx.AsyncClient() as client:
        items = await fetch_rss(client, cfg)
    assert len(items) == 2
    assert items[0].title == "First Post"
    assert items[0].url == "https://example.com/first"
    assert "AI agents" in items[0].snippet
    assert items[0].source == "Sample"
    assert items[0].published_at is not None
    assert items[0].published_at.year == 2026


async def test_fetch_hn_filters_by_min_score(httpx_mock: HTTPXMock, fixtures_dir):
    body = (fixtures_dir / "sample_hn.json").read_text()
    httpx_mock.add_response(url="https://hn.example/api", text=body)
    cfg = SourceConfig(name="HN", type="json_hn", url="https://hn.example/api", min_score=50)
    async with httpx.AsyncClient() as client:
        from src.fetch import fetch_hn
        items = await fetch_hn(client, cfg)
    # hn1 (250 pts) kept; hn2 (30 pts) dropped; hn3 (no url) dropped
    assert len(items) == 1
    assert items[0].title == "AI agents are taking over"
    assert items[0].url == "https://example.com/hn1"


async def test_fetch_reddit_filters_by_min_score(httpx_mock: HTTPXMock, fixtures_dir):
    body = (fixtures_dir / "sample_reddit.json").read_text()
    httpx_mock.add_response(url="https://reddit.example/api", text=body)
    cfg = SourceConfig(
        name="r/LocalLLaMA", type="json_reddit", url="https://reddit.example/api", min_score=100,
    )
    async with httpx.AsyncClient() as client:
        from src.fetch import fetch_reddit
        items = await fetch_reddit(client, cfg)
    assert len(items) == 1
    assert items[0].title == "Llama 5 weights leaked"


async def test_fetch_reddit_self_post_uses_permalink(httpx_mock: HTTPXMock, fixtures_dir):
    """A reddit self post with no external url should use the permalink."""
    body = (fixtures_dir / "sample_reddit.json").read_text()
    httpx_mock.add_response(url="https://reddit.example/api", text=body)
    cfg = SourceConfig(
        name="r/LocalLLaMA", type="json_reddit", url="https://reddit.example/api", min_score=10,
    )
    async with httpx.AsyncClient() as client:
        from src.fetch import fetch_reddit
        items = await fetch_reddit(client, cfg)
    # both items kept; second has external url == permalink
    second = next(i for i in items if i.title == "Tiny model crushing")
    assert "/r/LocalLLaMA/comments/a2/" in second.url


async def test_fetch_github_releases(httpx_mock: HTTPXMock, fixtures_dir):
    body = (fixtures_dir / "sample_github.json").read_text()
    httpx_mock.add_response(url="https://gh.example/api", text=body)
    cfg = SourceConfig(name="GH", type="json_github", url="https://gh.example/api")
    async with httpx.AsyncClient() as client:
        from src.fetch import fetch_github_releases
        items = await fetch_github_releases(client, cfg)
    assert len(items) == 2
    assert items[0].title.startswith("GH v1.0") or items[0].title == "v1.0"
    assert items[0].url == "https://github.com/owner/repo/releases/tag/v1.0"


async def test_fetch_html_extracts_articles(httpx_mock: HTTPXMock, fixtures_dir):
    body = (fixtures_dir / "sample_anthropic.html").read_text()
    httpx_mock.add_response(url="https://www.anthropic.com/news", text=body)
    cfg = SourceConfig(name="Anthropic news", type="html", url="https://www.anthropic.com/news")
    async with httpx.AsyncClient() as client:
        from src.fetch import fetch_html
        items = await fetch_html(client, cfg)
    assert len(items) == 2
    titles = [i.title for i in items]
    assert "Big new feature announcement" in titles
    # URLs are made absolute
    assert any(i.url == "https://www.anthropic.com/news/some-announcement" for i in items)
    # Dates parsed from <time datetime="...">
    target = next(i for i in items if i.title == "Big new feature announcement")
    assert target.published_at is not None
    assert target.published_at.year == 2026


async def test_fetch_all_collects_from_multiple_sources(httpx_mock: HTTPXMock, fixtures_dir):
    """fetch_all returns items from all sources plus a list of unavailable source names."""
    httpx_mock.add_response(
        url="https://hn.example/api",
        text=(fixtures_dir / "sample_hn.json").read_text(),
    )
    httpx_mock.add_response(
        url="https://example.com/feed",
        text=(fixtures_dir / "sample_atom.xml").read_text(),
    )
    sources = [
        SourceConfig(name="HN", type="json_hn", url="https://hn.example/api", min_score=10),
        SourceConfig(name="Sample", type="rss", url="https://example.com/feed"),
    ]
    from src.fetch import fetch_all
    items, unavailable = await fetch_all(sources)
    assert unavailable == []
    assert len(items) >= 3  # 2 from HN (hn1, hn2 with min_score=10) + 2 from RSS, minus filtered


async def test_fetch_all_records_failed_source(httpx_mock: HTTPXMock, fixtures_dir):
    """A failing source is logged in 'unavailable' and does not break the run."""
    httpx_mock.add_response(
        url="https://example.com/feed",
        text=(fixtures_dir / "sample_atom.xml").read_text(),
    )
    httpx_mock.add_response(url="https://broken.example/api", status_code=500)
    sources = [
        SourceConfig(name="Sample", type="rss", url="https://example.com/feed"),
        SourceConfig(name="Broken", type="json_hn", url="https://broken.example/api"),
    ]
    from src.fetch import fetch_all
    items, unavailable = await fetch_all(sources)
    assert "Broken" in unavailable
    assert len(items) == 2  # only the 2 RSS items


async def test_fetch_trending_repos_parses_response(httpx_mock: HTTPXMock):
    payload = {
        "items": [
            {
                "full_name": "acme/foo",
                "html_url": "https://github.com/acme/foo",
                "stargazers_count": 12345,
                "language": "Python",
                "topics": ["llm", "agent"],
                "description": "A library.",
            },
            {
                "full_name": "acme/bar",
                "html_url": "https://github.com/acme/bar",
                "stargazers_count": 100,
                "language": None,
                "topics": [],
                "description": None,
            },
        ]
    }
    # Match any GitHub Search URL — saves us hand-crafting the encoded date.
    httpx_mock.add_response(url=__import__("re").compile(r"https://api\.github\.com/search/repositories.*"), json=payload)
    repos = await fetch_trending_repos(
        days=7, top_n=5, now=datetime(2026, 5, 8, tzinfo=timezone.utc)
    )
    assert len(repos) == 2
    assert isinstance(repos[0], TrendingRepo)
    assert repos[0].full_name == "acme/foo"
    assert repos[0].stars == 12345
    assert repos[0].topics == ["llm", "agent"]
    assert repos[0].description == "A library."
    # Empty description / None language tolerated.
    assert repos[1].language is None
    assert repos[1].topics == []
    assert repos[1].description == ""


async def test_fetch_trending_repos_soft_fails_on_http_error(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=__import__("re").compile(r"https://api\.github\.com/search/repositories.*"),
        status_code=500,
    )
    repos = await fetch_trending_repos(
        days=7, top_n=5, now=datetime(2026, 5, 8, tzinfo=timezone.utc)
    )
    # Soft-fail: weekly should still render without trending repos.
    assert repos == []


def test_canonicalize_url_strips_utm_params():
    from src.fetch import canonicalize_url
    assert canonicalize_url("https://openai.com/x?utm_source=tldrai") == "https://openai.com/x"
    assert canonicalize_url("https://x.com/?utm_source=tldrai&utm_medium=email&id=42") == "https://x.com/?id=42"
    # No-op when nothing to strip.
    assert canonicalize_url("https://x.com/path") == "https://x.com/path"
    assert canonicalize_url("https://x.com/path?id=1") == "https://x.com/path?id=1"
    # fbclid + gclid + ref get dropped.
    assert canonicalize_url("https://x.com?fbclid=abc&keep=1") == "https://x.com?keep=1"


async def test_fetch_tldr_ai_extracts_stories(httpx_mock: HTTPXMock, fixtures_dir):
    """Smoke: index RSS lists 2 issues; each issue HTML has 2 stories."""
    rss_body = """<?xml version="1.0"?><rss><channel>
      <item><title>X</title><link>https://tldr.tech/ai/2026-05-06</link><pubDate>Wed, 06 May 2026 00:00:00 GMT</pubDate></item>
      <item><title>Y</title><link>https://tldr.tech/ai/2026-05-05</link><pubDate>Tue, 05 May 2026 00:00:00 GMT</pubDate></item>
    </channel></rss>"""
    issue_html = """<html><body>
      <article class="mt-3">
        <a class="font-bold" href="https://openai.com/index/gpt-5-5-instant?utm_source=tldrai"><h3>GPT-5.5 Instant (8 minute read)</h3></a>
        <p>OpenAI's new default with 128K context.</p>
      </article>
      <article class="mt-3">
        <a class="font-bold" href="https://example.com/sponsor"><h3>Some Ad (Sponsor)</h3></a>
        <p>A sponsor block.</p>
      </article>
      <article class="mt-3">
        <a class="font-bold" href="https://thenewstack.io/subq?utm_source=tldrai"><h3>SubQ 12M context (8 minute read)</h3></a>
        <p>Subquadratic 12M-token window.</p>
      </article>
    </body></html>"""
    httpx_mock.add_response(url="https://tldr.tech/api/rss/ai", text=rss_body)
    httpx_mock.add_response(url="https://tldr.tech/ai/2026-05-06", text=issue_html)
    httpx_mock.add_response(url="https://tldr.tech/ai/2026-05-05", text=issue_html)
    cfg = SourceConfig(name="TLDR AI", type="tldr_ai_index", url="https://tldr.tech/api/rss/ai")
    from src.fetch import fetch_tldr_ai
    async with httpx.AsyncClient(follow_redirects=True) as client:
        items = await fetch_tldr_ai(client, cfg)
    # 2 stories per issue × 2 issues = 4 (sponsor dropped).
    assert len(items) == 4
    titles = [i.title for i in items]
    assert any("GPT-5.5 Instant" in t for t in titles)
    assert any("SubQ 12M context" in t for t in titles)
    # utm_source stripped via canonicalize_url.
    assert all("utm_source=tldrai" not in i.url for i in items)
    # source label preserved so the LLM prompt can prioritize TLDR items.
    assert all(i.source == "TLDR AI" for i in items)
    # published_at carries the issue date.
    assert any(i.published_at and i.published_at.day == 6 for i in items)
