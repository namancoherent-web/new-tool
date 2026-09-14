from __future__ import annotations

import asyncio
import logging

from config import CONFIG
from pipeline.deepseek_client import DeepSeekClient, DeepSeekError
from pipeline.models import ClassifiedCompany, MarketUnderstanding, VerifiedCandidate
from prompts.classification import build_messages

logger = logging.getLogger(__name__)


def classify_one(client: DeepSeekClient, candidate: VerifiedCandidate, mu: MarketUnderstanding) -> ClassifiedCompany | None:
    system, user = build_messages(
        market_name=mu.market_name,
        geography=mu.geography,
        definition=mu.definition,
        out_of_scope=mu.boundary.out_of_scope,
        company_name=candidate.name,
        website=candidate.domain,
        evidence_text=candidate.homepage_text or "(no website text could be retrieved)",
        brief=mu.brief,
    )
    try:
        data = client.chat_json(system, user)
    except DeepSeekError as e:
        logger.warning("Classification failed for %s: %s", candidate.name, e)
        return None

    return ClassifiedCompany(
        company_name=data.get("company_name") or candidate.name,
        website=candidate.domain,
        hq_country=data.get("hq_country", ""),
        operates_in_target_geography=bool(data.get("operates_in_target_geography", False)),
        category=data.get("category", "Other"),
        subcategory=data.get("subcategory", ""),
        confidence=int(data.get("confidence", 0) or 0),
        matched_products=data.get("matched_products", []) or [],
        reason=data.get("reason", ""),
        evidence_source=candidate.source,
        source_url=candidate.url or f"https://{candidate.domain}",
        is_relevant=bool(data.get("is_relevant", False)),
    )


async def classify_candidates(
    candidates: list[VerifiedCandidate], mu: MarketUnderstanding
) -> list[ClassifiedCompany]:
    """One DeepSeek call per verified candidate, bounded concurrency via a
    thread pool since the DeepSeek client is sync/httpx-based."""
    client = DeepSeekClient()
    sem = asyncio.Semaphore(CONFIG.max_concurrent_classifications)
    loop = asyncio.get_running_loop()

    async def bounded(c: VerifiedCandidate) -> ClassifiedCompany | None:
        async with sem:
            return await loop.run_in_executor(None, classify_one, client, c, mu)

    try:
        results = await asyncio.gather(*(bounded(c) for c in candidates))
    finally:
        client.close()

    return [r for r in results if r is not None]
