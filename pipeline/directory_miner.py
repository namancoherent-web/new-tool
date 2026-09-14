from __future__ import annotations

import asyncio
import logging
import re

import httpx
from bs4 import BeautifulSoup

from config import CONFIG
from pipeline.models import Candidate
from sources.common import USER_AGENT, extract_domain, is_junk_domain

logger = logging.getLogger(__name__)

# Domains/title patterns that indicate a page lists many companies (industry
# association member lists, trade-fair exhibitor lists, genuine directory
# sites that link out to each company's own site) rather than being a single
# company's own site. Discovery results matching these get mined for their
# outbound company links instead of being crawled/classified as a single
# candidate themselves.
#
# Deliberately excludes big B2B marketplaces (TradeIndia, IndiaMART, Alibaba,
# Made-in-China) -- tested and confirmed they keep seller pages on their own
# subdomains rather than linking to sellers' external sites from category
# listing pages, even after JS rendering. Mining them for outbound links would
# need a much deeper per-seller-profile crawl for uncertain payoff, so their
# listing pages are left as regular search candidates instead (their text
# still gives DeepSeek useful classification context).
DIRECTORY_DOMAIN_HINTS = re.compile(
    r"(europages|kompass|thomasnet|globalspec|b2bhint|getmanufacturers)",
    re.I,
)
DIRECTORY_TITLE_HINTS = re.compile(
    r"\b(association|exhibitors?|members?)\b.*\b(list|directory)\b",
    re.I,
)

MAX_DIRECTORY_PAGES_PER_RUN = 15
MAX_LINKS_PER_DIRECTORY_PAGE = 60


def looks_like_directory(candidate: Candidate) -> bool:
    if DIRECTORY_DOMAIN_HINTS.search(candidate.domain):
        return True
    if DIRECTORY_TITLE_HINTS.search(candidate.name):
        return True
    return False


async def mine_directory_page(client: httpx.AsyncClient, url: str, source_domain: str) -> list[Candidate]:
    """Extract linked companies from an industry directory / association /
    trade-fair-exhibitor listing page."""
    try:
        resp = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=CONFIG.http_timeout_seconds)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("Directory fetch failed for %r: %s", url, e)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    results: list[Candidate] = []
    for a in soup.find_all("a", href=True):
        if len(results) >= MAX_LINKS_PER_DIRECTORY_PAGE:
            break
        href = a["href"]
        text = a.get_text(strip=True)
        if not text or len(text) < 2 or len(text) > 80:
            continue
        domain = extract_domain(href)
        if not domain or is_junk_domain(domain) or domain == source_domain:
            continue
        results.append(
            Candidate(
                name=text,
                domain=domain,
                url=href,
                source=f"directory:{source_domain}",
                discovery_query=url,
            )
        )
    return results


async def expand_via_directories(candidates: list[Candidate]) -> list[Candidate]:
    """Find candidates that look like directory/association pages (not single
    companies) among discovery results, crawl a bounded number of them, and
    return the company links extracted from them as new candidates."""
    directory_candidates = [c for c in candidates if looks_like_directory(c)]
    if not directory_candidates:
        return []

    to_mine = directory_candidates[:MAX_DIRECTORY_PAGES_PER_RUN]
    logger.info(
        "Found %d directory-style pages in discovery results, mining %d of them for company links",
        len(directory_candidates), len(to_mine),
    )

    async with httpx.AsyncClient(follow_redirects=True, http2=True) as client:
        results = await asyncio.gather(
            *(mine_directory_page(client, c.url, c.domain) for c in to_mine if c.url)
        )

    expanded: list[Candidate] = []
    for r in results:
        expanded.extend(r)
    return expanded
