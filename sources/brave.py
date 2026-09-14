from __future__ import annotations

import logging

import httpx

from config import CONFIG
from pipeline.models import Candidate
from sources.common import extract_domain, is_junk_domain

logger = logging.getLogger(__name__)

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


async def search(client: httpx.AsyncClient, query: str, max_results: int = 15) -> list[Candidate]:
    """Search via the Brave Search API -- a real indexed search API (free
    tier: 2000 queries/month), not an HTML scrape. Used as the primary
    discovery source since scraping DDG/Bing's unauthenticated HTML endpoints
    proved unreliable under sustained pipeline use (anti-bot blocks, low
    result relevance)."""
    if not CONFIG.brave_api_key:
        return []

    try:
        resp = await client.get(
            BRAVE_SEARCH_URL,
            params={"q": query, "count": min(max_results, 20)},
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": CONFIG.brave_api_key,
            },
            timeout=CONFIG.http_timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("Brave search failed for %r: %s", query, e)
        return []

    results: list[Candidate] = []
    for item in data.get("web", {}).get("results", [])[:max_results]:
        url = item.get("url", "")
        title = item.get("title", "")
        domain = extract_domain(url)
        if not domain or is_junk_domain(domain):
            continue
        results.append(
            Candidate(
                name=title or domain,
                domain=domain,
                url=url,
                source="brave",
                discovery_query=query,
            )
        )
    return results
