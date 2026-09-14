from __future__ import annotations

import logging

import httpx

from config import CONFIG
from pipeline.models import Candidate
from sources import wikidata_source
from sources.common import extract_domain, is_junk_domain

logger = logging.getLogger(__name__)

WIKI_API_URL = "https://en.wikipedia.org/w/api.php"

# Wikimedia's API enforces a bot policy requiring a descriptive User-Agent
# with contact info; the generic browser UA used elsewhere gets a 403 here
# even though the request itself is legitimate (same issue as Wikidata's
# SPARQL endpoint -- see sources/wikidata_source.py).
WIKIPEDIA_USER_AGENT = (
    "MarketUniverseFinder/2.0 (company-research tool; "
    "https://github.com/) python-httpx"
)


async def search(client: httpx.AsyncClient, query: str, max_results: int = 10) -> list[Candidate]:
    """Last-resort fallback: search Wikipedia for company/list pages, then
    resolve each hit's official website. Prefers Wikidata's structured P856
    "official website" property (precise, curated data) over scraping
    Wikipedia's raw external-links section, which mixes in news articles,
    archives, and unrelated references alongside the real company site."""
    try:
        resp = await client.get(
            WIKI_API_URL,
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "format": "json",
                "srlimit": max_results,
            },
            headers={"User-Agent": WIKIPEDIA_USER_AGENT},
            timeout=CONFIG.http_timeout_seconds,
        )
        resp.raise_for_status()
        hits = resp.json().get("query", {}).get("search", [])
    except Exception as e:
        logger.warning("Wikipedia search failed for %r: %s", query, e)
        return []

    results: list[Candidate] = []
    for hit in hits[:max_results]:
        title = hit.get("title", "")
        if not title:
            continue
        wiki_url = f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"

        website = await wikidata_source.lookup_website_by_name(client, title)
        domain = extract_domain(website) if website else ""

        if not domain or is_junk_domain(domain):
            # fall back to raw extlinks scraping only if Wikidata has no
            # structured website for this exact title
            fallback = await resolve_external_links(client, wiki_url)
            if fallback:
                results.append(fallback[0])
                continue
            results.append(
                Candidate(name=title, domain="", url=wiki_url, source="wikipedia", discovery_query=query)
            )
            continue

        results.append(
            Candidate(
                name=title, domain=domain, url=website, source="wikipedia_wikidata", discovery_query=query
            )
        )

    return results


async def resolve_external_links(client: httpx.AsyncClient, wiki_url: str) -> list[Candidate]:
    """Given a Wikipedia page URL, pull the external links section — used only
    as a fallback when Wikidata has no structured official-website entry for
    this article's title."""
    title = wiki_url.rsplit("/", 1)[-1]
    try:
        resp = await client.get(
            WIKI_API_URL,
            params={
                "action": "query",
                "titles": title,
                "prop": "extlinks",
                "format": "json",
                "ellimit": 20,
            },
            headers={"User-Agent": WIKIPEDIA_USER_AGENT},
            timeout=CONFIG.http_timeout_seconds,
        )
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", {})
    except Exception as e:
        logger.warning("Wikipedia extlinks failed for %r: %s", wiki_url, e)
        return []

    out: list[Candidate] = []
    for page in pages.values():
        for link in page.get("extlinks", []):
            url = link.get("*", "")
            domain = extract_domain(url)
            if is_junk_domain(domain) or not domain:
                continue
            out.append(
                Candidate(
                    name=title.replace("_", " "),
                    domain=domain,
                    url=url,
                    source="wikipedia_extlink",
                    discovery_query=title,
                )
            )
    return out
