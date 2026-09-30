from datetime import datetime, timedelta, timezone
from src.filter import filter_candidates
from src.types import CandidateItem


def _item(url: str, published: datetime | None = None) -> CandidateItem:
    return CandidateItem(
        url=url,
        source="x",
        title="t",
        snippet="s",
        published_at=published,
    )


def test_keeps_items_in_window():
    now = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    since = now - timedelta(hours=24)
    items = [
        _item("https://a", now - timedelta(hours=2)),    # in window
        _item("https://b", now - timedelta(hours=48)),   # too old
    ]
    out = filter_candidates(items, since=since, seen=set())
    assert [i.url for i in out] == ["https://a"]


def test_drops_items_in_seen_set():
    now = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    items = [
        _item("https://a", now - timedelta(hours=2)),
        _item("https://b", now - timedelta(hours=2)),
    ]
    out = filter_candidates(items, since=now - timedelta(hours=24), seen={"https://a"})
    assert [i.url for i in out] == ["https://b"]


def test_keeps_items_with_no_published_at():
    """No publish date - keep, let the LLM decide."""
    now = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    items = [_item("https://a", None)]
    out = filter_candidates(items, since=now - timedelta(hours=24), seen=set())
    assert len(out) == 1


def test_caps_at_max_items():
    now = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    items = [_item(f"https://{i}", now - timedelta(minutes=i)) for i in range(150)]
    out = filter_candidates(
        items, since=now - timedelta(hours=24), seen=set(), max_items=100
    )
    assert len(out) == 100


def test_dedups_by_url_within_one_run():
    """Two sources can return the same URL; keep only one."""
    now = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    items = [_item("https://a", now), _item("https://a", now)]
    out = filter_candidates(items, since=now - timedelta(hours=24), seen=set())
    assert len(out) == 1
