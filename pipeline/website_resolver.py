from __future__ import annotations

import asyncio
import logging

import httpx

from pipeline.models import ClassifiedCompany
from sources.common import USER_AGENT, is_junk_domain
from sources.ddg import search as ddg_search

logger = logging.getLogger(__name__)

RESOLVE_CONCURRENCY = 5


async def _resolve_one(client: httpx.AsyncClient, company: ClassifiedCompany) -> None:
    try:
        hits = await ddg_search(client, f'"{company.company_name}" official website')
    except Exception as e:
        logger.warning("Website resolution failed for %r: %s", company.company_name, e)
        return

    for hit in hits:
        if hit.domain and not is_junk_domain(hit.domain):
            company.website = hit.domain
            if not company.source_url:
                company.source_url = f"https://{hit.domain}"
            return


async def resolve_missing_websites(companies: list[ClassifiedCompany]) -> None:
    """Fill in the website field for any classified company that reached
    export without one (e.g. AI-Mode-sourced companies whose answer didn't
    include a clean domain), via a targeted single-name DDG lookup. Mutates
    the companies in place. Best-effort: a name that can't be resolved is
    left with an empty website rather than blocking export."""
    missing = [c for c in companies if not c.website]
    if not missing:
        return

    sem = asyncio.Semaphore(RESOLVE_CONCURRENCY)

    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, follow_redirects=True, http2=True) as client:

        async def bounded(c: ClassifiedCompany) -> None:
            async with sem:
                await asyncio.sleep(0.3)
                await _resolve_one(client, c)

        await asyncio.gather(*(bounded(c) for c in missing))

    resolved = sum(1 for c in missing if c.website)
    logger.info("Resolved websites for %d of %d companies missing one", resolved, len(missing))
