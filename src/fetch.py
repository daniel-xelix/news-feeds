"""Async parallel source fetchers."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import feedparser
import httpx
from bs4 import BeautifulSoup

from src import paths
from src.types import CandidateItem, SourceConfig, TrendingRepo

logger = logging.getLogger(__name__)


def load_sources() -> list[SourceConfig]:
    """Load source configurations from config/sources.json. Late-binds
    paths.config_dir so test monkeypatches take effect regardless of
    fetch.py's import order."""
    raw = json.loads((paths.config_dir() / "sources.json").read_text())
    return [SourceConfig(**entry) for entry in raw]


def canonicalize_url(url: str) -> str:
    """Strip utm_* and similar tracking query parameters so the same article
    coming via TLDR AI (which appends ?utm_source=tldrai) and via the original
    source dedups against the same seen-urls entry."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if not parsed.query:
        return url
    DROP_PREFIXES = ("utm_", "ref_", "ref=", "mc_", "fbclid")
    DROP_KEYS = {"ref", "fbclid", "gclid", "mc_eid", "mc_cid"}
    kept = [
        (k, v) for (k, v) in parse_qsl(parsed.query, keep_blank_values=True)
        if not (k.startswith(DROP_PREFIXES) or k in DROP_KEYS)
    ]
    new_q = urlencode(kept, doseq=True)
    return urlunparse(parsed._replace(query=new_q))


def _to_datetime(struct_time) -> datetime | None:
    if not struct_time:
        return None
    try:
        return datetime(*struct_time[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


async def fetch_rss(client: httpx.AsyncClient, cfg: SourceConfig) -> list[CandidateItem]:
    """Fetch and parse an RSS/Atom feed. Returns normalized CandidateItems."""
    resp = await client.get(cfg.url, timeout=10.0)
    resp.raise_for_status()
    feed = feedparser.parse(resp.text)
    out: list[CandidateItem] = []
    for entry in feed.entries:
        url = entry.get("link") or entry.get("id")
        if not url:
            continue
        url = canonicalize_url(url)
        title = entry.get("title", "(untitled)")
        snippet = (entry.get("summary") or entry.get("description") or "")[:500]
        published = _to_datetime(
            entry.get("published_parsed") or entry.get("updated_parsed")
        )
        out.append(CandidateItem(
            url=url,
            source=cfg.name,
            title=title,
            snippet=snippet,
            published_at=published,
            raw=dict(entry),
        ))
    return out


async def fetch_hn(client: httpx.AsyncClient, cfg) -> list[CandidateItem]:
    """Fetch from Hacker News Algolia API."""
    resp = await client.get(cfg.url, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()
    out: list[CandidateItem] = []
    for hit in data.get("hits", []):
        url = hit.get("url")
        if not url:
            continue
        url = canonicalize_url(url)
        score = hit.get("points") or 0
        if cfg.min_score is not None and score < cfg.min_score:
            continue
        title = hit.get("title", "(untitled)")
        snippet = (hit.get("story_text") or "")[:500]
        published = None
        if hit.get("created_at"):
            try:
                published = datetime.fromisoformat(hit["created_at"].replace("Z", "+00:00"))
            except ValueError:
                pass
        out.append(CandidateItem(
            url=url,
            source=cfg.name,
            title=title,
            snippet=snippet,
            published_at=published,
            raw=hit,
        ))
    return out


async def fetch_reddit(client: httpx.AsyncClient, cfg) -> list[CandidateItem]:
    """Fetch from Reddit JSON listing."""
    headers = {"User-Agent": "news-tracker/0.1 (https://danielkeller.com)"}
    resp = await client.get(cfg.url, timeout=10.0, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    out: list[CandidateItem] = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        score = d.get("score") or 0
        if cfg.min_score is not None and score < cfg.min_score:
            continue
        # External URL preferred; fall back to permalink for self-posts.
        url = d.get("url")
        permalink = d.get("permalink")
        if not url and permalink:
            url = f"https://www.reddit.com{permalink}"
        if not url:
            continue
        url = canonicalize_url(url)
        title = d.get("title", "(untitled)")
        snippet = (d.get("selftext") or "")[:500]
        published = None
        if d.get("created_utc"):
            published = datetime.fromtimestamp(d["created_utc"], tz=timezone.utc)
        out.append(CandidateItem(
            url=url,
            source=cfg.name,
            title=title,
            snippet=snippet,
            published_at=published,
            raw=d,
        ))
    return out


async def fetch_github_releases(client: httpx.AsyncClient, cfg) -> list[CandidateItem]:
    """Fetch GitHub releases for a single repo. Uses GITHUB_TOKEN if set
    (raises the unauthenticated 60 req/hr limit to 5,000)."""
    import os
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = await client.get(cfg.url, timeout=10.0, headers=headers)
    resp.raise_for_status()
    releases = resp.json()
    out: list[CandidateItem] = []
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        url = rel.get("html_url")
        if not url:
            continue
        url = canonicalize_url(url)
        tag = rel.get("name") or rel.get("tag_name") or "release"
        title = f"{cfg.name.replace('GitHub: ', '')} {tag}"
        snippet = (rel.get("body") or "")[:500]
        published = None
        if rel.get("published_at"):
            try:
                published = datetime.fromisoformat(rel["published_at"].replace("Z", "+00:00"))
            except ValueError:
                pass
        out.append(CandidateItem(
            url=url,
            source=cfg.name,
            title=title,
            snippet=snippet,
            published_at=published,
            raw=rel,
        ))
    return out


async def fetch_html(client: httpx.AsyncClient, cfg) -> list[CandidateItem]:
    """Generic HTML scraper: finds <article> blocks with a heading-link and optional <time>.

    Works for Anthropic news, Mistral news, Cursor changelog. If a source's HTML
    structure changes and zero articles are extracted, this raises so source-soft-fail
    upstream catches it and notes the source as unavailable.
    """
    resp = await client.get(cfg.url, timeout=10.0, follow_redirects=True)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out: list[CandidateItem] = []
    for art in soup.find_all("article"):
        link = art.find("a", href=True)
        if not link:
            continue
        heading = link.find(["h1", "h2", "h3", "h4"]) or link
        title = heading.get_text(strip=True)
        if not title:
            continue
        href = canonicalize_url(urljoin(cfg.url, link["href"]))
        # Snippet from a <p> or first 500 chars of article text
        p = art.find("p")
        snippet = (p.get_text(" ", strip=True) if p else art.get_text(" ", strip=True))[:500]
        # Date from <time datetime="...">
        published = None
        time_el = art.find("time")
        if time_el and time_el.get("datetime"):
            try:
                ds = time_el["datetime"]
                published = datetime.fromisoformat(ds.replace("Z", "+00:00"))
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        out.append(CandidateItem(
            url=href,
            source=cfg.name,
            title=title,
            snippet=snippet,
            published_at=published,
            raw={"href": href, "title": title},
        ))
    if not out:
        raise ValueError(f"No articles extracted from {cfg.url}")
    return out


# TLDR AI is treated as a high-priority editorial aggregator: its RSS feed
# only carries issue titles ("GPT-5.5 Instant ⚡, SubQ 12M context 🧠, …"),
# so we fetch the issue URL and extract the individual story blocks. Each
# story becomes its own CandidateItem and the LLM gets full context.
TLDR_AI_LOOKBACK_ISSUES = 10  # RSS feed lists last ~20; cap HTML round-trips
TLDR_AI_SECTION_HEADERS = {
    "headlines & launches", "deep dives & analysis", "engineering & research",
    "miscellaneous", "quick links", "tldr ai",
}


def _is_tldr_section_header(title: str) -> bool:
    """The page has centered <h3>Headlines & Launches</h3> etc. that aren't stories."""
    return title.strip().lower() in TLDR_AI_SECTION_HEADERS


def _is_tldr_sponsor(title: str) -> bool:
    return "(sponsor)" in title.lower()


async def _fetch_tldr_issue(
    client: httpx.AsyncClient, issue_url: str, issue_date: datetime, source_name: str
) -> list[CandidateItem]:
    """Scrape one TLDR AI daily issue page; return one CandidateItem per story.

    Story structure: <article class="mt-3"> wrapping <a class="font-bold"
    href=outbound_url><h3>Title (X minute read)</h3></a> followed by the
    summary paragraph in the same article. We rebuild the summary by stripping
    the title from article.get_text()."""
    resp = await client.get(issue_url, timeout=15.0)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out: list[CandidateItem] = []
    for article in soup.find_all("article"):
        a = article.find("a", class_="font-bold")
        if not a or not a.get("href"):
            continue
        h3 = a.find("h3")
        if not h3:
            continue
        title = h3.get_text(strip=True)
        if not title or _is_tldr_section_header(title) or _is_tldr_sponsor(title):
            continue
        href = canonicalize_url(a["href"])
        # Skip TLDR-internal links (sponsorship pages on tldr.tech don't carry a story).
        if "tldr.tech" in href and "/ai/" not in href:
            continue
        full_text = article.get_text(" ", strip=True)
        summary = full_text.replace(title, "", 1).strip()[:500]
        out.append(CandidateItem(
            url=href,
            source=source_name,
            title=title,
            snippet=summary,
            published_at=issue_date,
            raw={"issue_url": issue_url},
        ))
    return out


async def fetch_tldr_ai(client: httpx.AsyncClient, cfg) -> list[CandidateItem]:
    """Fetch the TLDR AI index RSS, then scrape each recent issue's HTML in
    parallel for the individual stories. cfg.url is the RSS feed URL."""
    resp = await client.get(cfg.url, timeout=10.0)
    resp.raise_for_status()
    feed = feedparser.parse(resp.text)
    issue_jobs: list[tuple[str, datetime]] = []
    for entry in feed.entries[:TLDR_AI_LOOKBACK_ISSUES]:
        issue_url = entry.get("link") or entry.get("id")
        if not issue_url:
            continue
        issue_date = _to_datetime(
            entry.get("published_parsed") or entry.get("updated_parsed")
        )
        if issue_date is None:
            continue
        issue_jobs.append((issue_url, issue_date))

    # TLDR's edge intermittently 404s under concurrent fan-out from a single
    # IP (looks like a cache-miss/rate-limit interaction). Cap to 2 in flight
    # at a time and retry once on 404 — empirically this gets us all issues.
    sem = asyncio.Semaphore(2)

    async def _one(issue_url: str, issue_date: datetime) -> list[CandidateItem]:
        async with sem:
            for attempt in range(2):
                try:
                    return await _fetch_tldr_issue(client, issue_url, issue_date, cfg.name)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404 and attempt == 0:
                        await asyncio.sleep(0.5)
                        continue
                    logger.warning("TLDR AI issue %s failed: %s", issue_url, e)
                    return []
                except Exception as e:  # noqa: BLE001
                    logger.warning("TLDR AI issue %s failed: %s", issue_url, e)
                    return []
            return []

    nested = await asyncio.gather(*[_one(u, d) for u, d in issue_jobs])
    flat: list[CandidateItem] = []
    for items in nested:
        flat.extend(items)
    return flat


_DISPATCH = {
    "rss": fetch_rss,
    "json_hn": fetch_hn,
    "json_reddit": fetch_reddit,
    "json_github": fetch_github_releases,
    "html": fetch_html,
    "tldr_ai_index": fetch_tldr_ai,
}


async def fetch_all(sources: list[SourceConfig]) -> tuple[list[CandidateItem], list[str]]:
    """Fetch all sources in parallel. Returns (items, list of failed source names).

    A failure on any single source is logged and the source name is recorded in the
    failed list, but does not raise — the run continues.
    """
    # retries=3 on the transport retries failed TCP connects (not HTTP errors).
    # Flaky networks (hotel/guest Wi-Fi dropping connections to whole CDN IP
    # ranges) otherwise take out most sources in one shot.
    transport = httpx.AsyncHTTPTransport(retries=3)
    async with httpx.AsyncClient(
        follow_redirects=True, transport=transport,
        headers={"User-Agent": "Mozilla/5.0 (compatible; news-feeds/1.0; +https://github.com/dkeller/news-feeds)"},
    ) as client:
        async def _one(cfg: SourceConfig):
            fn = _DISPATCH[cfg.type]
            try:
                return cfg.name, await fn(client, cfg), None
            except Exception as e:  # noqa: BLE001 — explicit broad catch
                # Include the exception type: httpx timeout exceptions often
                # stringify to "", which made mass-timeout runs undiagnosable.
                logger.warning("source %s failed: %s: %s", cfg.name, type(e).__name__, e)
                return cfg.name, [], e

        results = await asyncio.gather(*[_one(s) for s in sources])

    items: list[CandidateItem] = []
    unavailable: list[str] = []
    for name, src_items, err in results:
        if err is not None:
            unavailable.append(name)
        items.extend(src_items)
    return items, unavailable


async def fetch_trending_repos(
    *,
    days: int = 7,
    top_n: int = 5,
    now: datetime | None = None,
) -> list[TrendingRepo]:
    """Top-N most-starred repos created in the last `days` days, via GitHub
    Search API. Used by the Friday weekly digest. Soft-fails (returns []) on
    any error, since the weekly should still render without this section."""
    import os
    from urllib.parse import quote_plus

    if now is None:
        now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    q = f"created:>={since}"
    url = (
        f"https://api.github.com/search/repositories?q={quote_plus(q)}"
        f"&sort=stars&order=desc&per_page={top_n}"
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "news-tracker/1.0",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        transport = httpx.AsyncHTTPTransport(retries=3)
        async with httpx.AsyncClient(follow_redirects=True, transport=transport) as client:
            resp = await client.get(url, headers=headers, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("trending repos fetch failed: %s", e)
        return []
    out: list[TrendingRepo] = []
    for item in data.get("items", [])[:top_n]:
        out.append(TrendingRepo(
            full_name=item.get("full_name", ""),
            html_url=item.get("html_url", ""),
            stars=int(item.get("stargazers_count") or 0),
            language=item.get("language"),
            topics=list(item.get("topics") or []),
            description=(item.get("description") or "").strip(),
        ))
    return out
