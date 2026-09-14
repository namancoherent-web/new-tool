from __future__ import annotations

import logging

import httpx
from bs4 import BeautifulSoup

from config import CONFIG
from pipeline.models import Candidate
from sources.common import USER_AGENT, extract_domain, guess_company_name, is_junk_domain

logger = logging.getLogger(__name__)

DDG_HTML_URL = "https://html.duckduckgo.com/html/"


async def search(client: httpx.AsyncClient, query: str, max_results: int = 15) -> list[Candidate]:
    """Search DuckDuckGo's lite HTML endpoint (no API key required)."""
    try:
        resp = await client.post(
            DDG_HTML_URL,
            data={"q": query},
            headers={"User-Agent": USER_AGENT},
            timeout=CONFIG.http_timeout_seconds,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.warning("DDG search failed for %r: %s", query, e)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    results: list[Candidate] = []
    for a in soup.select("a.result__a")[:max_results]:
        url = a.get("href", "")
        title = a.get_text(strip=True)
        domain = extract_domain(url)
        if is_junk_domain(domain):
            continue
        results.append(
            Candidate(
                name=guess_company_name(title, domain),
                domain=domain,
                url=url,
                source="duckduckgo",
                discovery_query=query,
            )
        )
    return results
