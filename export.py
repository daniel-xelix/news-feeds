#!/usr/bin/env python3
"""Fetch every source in config/sources.json and write feed snapshots.

Outputs (committed by the GitHub Actions workflow):
  feeds/latest.json          items from the last LATEST_HOURS hours, newest first
  feeds/archive/YYYY-MM-DD.json  items published on that UTC day (merged across runs)
  feeds/status.json          per-source health for the last run

No LLM call: curation happens in the CTO Briefing scheduled tasks, which read
these files. Exit code is non-zero only when every source failed.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src import fetch
from src.types import CandidateItem

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "feeds"
ARCHIVE = OUT / "archive"
LATEST_HOURS = 96          # covers a Friday-to-Monday gap with margin
ARCHIVE_KEEP_DAYS = 45     # enough for the monthly digest
SNIPPET_CHARS = 400

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("export")

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean(text: str, limit: int) -> str:
    text = html.unescape(_TAG.sub(" ", text or ""))
    return _WS.sub(" ", text).strip()[:limit]


def to_row(item: CandidateItem, weights: dict[str, float], fetched_at: str) -> dict:
    return {
        "url": item.url,
        "source": item.source,
        "weight": weights.get(item.source, 1.0),
        "title": clean(item.title, 300),
        "snippet": clean(item.snippet, SNIPPET_CHARS),
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "first_seen": fetched_at,
    }


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(path)


def load_rows(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text())["items"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return []


async def main() -> int:
    now = datetime.now(timezone.utc)
    fetched_at = now.isoformat(timespec="seconds")
    sources = fetch.load_sources()
    weights = {s.name: s.weight for s in sources}
    items, unavailable = await fetch.fetch_all(sources)
    log.info("fetched %d items from %d sources; %d unavailable", len(items), len(sources), len(unavailable))

    if len(unavailable) == len(sources):
        log.error("all sources failed")
        return 1

    # Dedup by canonical URL; keep the highest-weight source as primary.
    by_url: dict[str, dict] = {}
    for it in items:
        row = to_row(it, weights, fetched_at)
        prev = by_url.get(row["url"])
        if prev is None:
            row["also_in"] = []
            by_url[row["url"]] = row
        elif row["source"] != prev["source"] and row["source"] not in prev["also_in"]:
            if row["weight"] > prev["weight"]:
                row["also_in"] = prev["also_in"] + [prev["source"]]
                row["first_seen"] = prev["first_seen"]
                by_url[row["url"]] = row
            else:
                prev["also_in"].append(row["source"])

    # Items without a date are only trusted as "new" the first time we see them.
    known: set[str] = set()
    for p in ARCHIVE.glob("*.json"):
        known.update(r["url"] for r in load_rows(p))

    cutoff = now - timedelta(hours=LATEST_HOURS)
    latest, per_day = [], {}
    for row in by_url.values():
        ts = row["published_at"]
        dt = datetime.fromisoformat(ts) if ts else None
        if dt is not None and dt > now + timedelta(days=1):
            continue  # future-dated entries are event listings, not news
        if dt is None and row["url"] in known:
            continue
        day_dt = dt or now
        if day_dt >= cutoff:
            latest.append(row)
        if day_dt >= now - timedelta(days=ARCHIVE_KEEP_DAYS):
            per_day.setdefault(day_dt.strftime("%Y-%m-%d"), []).append(row)

    latest.sort(key=lambda r: r["published_at"] or r["first_seen"], reverse=True)
    counts: dict[str, int] = {}
    for r in latest:
        counts[r["source"]] = counts.get(r["source"], 0) + 1

    write_json(OUT / "latest.json", {
        "generated_at": fetched_at,
        "window_hours": LATEST_HOURS,
        "sources_total": len(sources),
        "unavailable": sorted(unavailable),
        "counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "items": latest,
    })

    for day, rows in per_day.items():
        path = ARCHIVE / f"{day}.json"
        merged = {r["url"]: r for r in load_rows(path)}
        for r in rows:
            merged.setdefault(r["url"], r)
        write_json(path, {"date": day, "items": sorted(
            merged.values(), key=lambda r: r["published_at"] or r["first_seen"], reverse=True)})

    for p in ARCHIVE.glob("*.json"):
        try:
            if datetime.strptime(p.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc) < now - timedelta(days=ARCHIVE_KEEP_DAYS):
                p.unlink()
        except ValueError:
            pass

    write_json(OUT / "status.json", {
        "generated_at": fetched_at,
        "ok": len(sources) - len(unavailable),
        "failed": sorted(unavailable),
        "items_in_latest": len(latest),
    })
    log.info("latest.json: %d items; archive days touched: %d", len(latest), len(per_day))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
