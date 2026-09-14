from __future__ import annotations

import logging

import httpx

from config import CONFIG
from sources.common import USER_AGENT
from sources.wikidata_source import WIKIDATA_SPARQL_URL, WIKIDATA_USER_AGENT

logger = logging.getLogger(__name__)

# Reachability is tested by hitting each engine's real endpoint directly and
# checking for a successful HTTP response -- NOT by requiring a specific probe
# query to return results, since a legitimate zero-result query (e.g. no
# company label containing an arbitrary test phrase) would be indistinguishable
# from a blocked/broken engine if we just checked "did search() return hits".
PROBES: dict[str, tuple[str, dict]] = {
    "duckduckgo": ("https://html.duckduckgo.com/html/", {}),
    "bing": ("https://www.bing.com/search", {"q": "test"}),
    "wikipedia": (
        "https://en.wikipedia.org/w/api.php",
        {"action": "query", "list": "search", "srsearch": "test", "format": "json", "srlimit": 1},
    ),
    "wikidata": (
        WIKIDATA_SPARQL_URL,
        {"query": "SELECT ?x WHERE { ?x ?y ?z } LIMIT 1", "format": "json"},
    ),
}

BRAVE_PROBE_URL = "https://api.search.brave.com/res/v1/web/search"


async def probe_engines() -> dict[str, bool]:
    """Smoke-test each search source once before committing to a full
    discovery run. On a locked-down office network, or under anti-bot
    detection, DDG/Bing scraping can be silently blocked -- without this
    check, every discovery query would still wait out the full HTTP timeout
    (or worse, silently accept a fake "no results" challenge page) before
    failing, turning a 10-minute run into 1hr+ of wasted requests.

    Any engine that fails here is skipped for the entire run instead of
    being retried per-query."""
    results: dict[str, bool] = {}

    async with httpx.AsyncClient(follow_redirects=True, timeout=CONFIG.http_timeout_seconds) as client:
        for name, (url, params) in PROBES.items():
            headers = {"User-Agent": WIKIDATA_USER_AGENT if name == "wikidata" else USER_AGENT}
            try:
                if name == "duckduckgo":
                    resp = await client.post(url, data={"q": "test"}, headers=headers)
                    resp.raise_for_status()
                    # DDG's anti-bot challenge page returns HTTP 202 (a
                    # "success" status) with a fake results page instead of a
                    # real 4xx/5xx error, so raise_for_status() alone can't
                    # catch it -- check the response body for the anomaly marker.
                    if resp.status_code == 202 or "anomaly" in resp.text.lower():
                        raise RuntimeError("DDG anti-bot challenge page detected")
                else:
                    resp = await client.get(url, params=params, headers=headers)
                    resp.raise_for_status()
                results[name] = True
            except Exception as e:
                logger.warning("Engine probe failed for %s: %s", name, e)
                results[name] = False

        if CONFIG.brave_api_key:
            try:
                resp = await client.get(
                    BRAVE_PROBE_URL,
                    params={"q": "test", "count": 1},
                    headers={"Accept": "application/json", "X-Subscription-Token": CONFIG.brave_api_key},
                )
                resp.raise_for_status()
                results["brave"] = True
            except Exception as e:
                logger.warning("Engine probe failed for brave: %s", e)
                results["brave"] = False
        else:
            results["brave"] = False

    reachable = [k for k, v in results.items() if v]
    unreachable = [k for k, v in results.items() if not v]
    if unreachable:
        logger.warning(
            "Search engines unreachable/unusable and will be SKIPPED for this run: %s. "
            "Reachable engines: %s",
            unreachable, reachable or "NONE",
        )
    return results
