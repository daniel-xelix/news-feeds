# news-feeds

Feed snapshots for Daniel's CTO Briefing. A GitHub Actions workflow fetches every
source in `config/sources.json` twice a day (05:07 and 15:07 UTC) and commits:

- `feeds/latest.json`: items from the last 96 hours, newest first, deduped by URL (`also_in` lists other sources that carried the same link)
- `feeds/archive/YYYY-MM-DD.json`: items by publication day, kept 45 days
- `feeds/status.json`: which sources failed on the last run

The CTO Briefing scheduled tasks (daily, weekly, monthly) clone this repo and use
these files as their RSS input next to Gmail newsletters and web search.

Fetch code is ported from `~/dev_p/tracker/news-tracker` (fetch.py, filter.py, types.py);
the LLM curation step lives in the scheduled tasks, so this repo needs no API key.

## Edit sources

Add or remove entries in `config/sources.json` (types: rss, json_hn, json_reddit,
json_github, html, tldr_ai_index). Run the workflow manually from the Actions tab
to test. Failed sources show up in `feeds/status.json`.

## Run locally

    uv sync
    uv run python export.py
    uv run pytest
