from __future__ import annotations

import asyncio
import logging

import httpx
import trafilatura

from config import CONFIG
from pipeline.models import Candidate, EnrichedCandidate
from sources.common import USER_AGENT

logger = logging.getLogger(__name__)

INTERESTING_PATHS = ["", "/about", "/about-us", "/company", "/products", "/contact"]

MIN_USEFUL_TEXT_LEN = 200


async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    try:
        resp = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=CONFIG.http_timeout_seconds)
        if resp.status_code >= 400:
            return ""
        return resp.text
    except Exception:
        return ""


def _extract_text(html: str) -> str:
    if not html:
        return ""
    text = trafilatura.extract(html) or ""
    return text.strip()


async def _crawl_with_playwright(url: str) -> str:
    """Fallback for JS-rendered sites that return near-empty content via plain HTTP."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("Playwright not installed, skipping fallback for %s", url)
        return ""

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(user_agent=USER_AGENT)
            await page.goto(url, timeout=CONFIG.playwright_timeout_seconds * 1000, wait_until="domcontentloaded")
            html = await page.content()
            await browser.close()
            return _extract_text(html)
    except Exception as e:
        logger.warning("Playwright crawl failed for %s: %s", url, e)
        return ""


async def enrich_candidate(client: httpx.AsyncClient, candidate: Candidate) -> EnrichedCandidate:
    """Fetch homepage + a few key pages via lightweight HTTP first; fall back
    to Playwright only if the homepage text comes back too thin to be useful."""
    if not candidate.domain:
        return EnrichedCandidate(**candidate.__dict__, crawl_ok=False, crawl_error="no domain")

    base_url = f"https://{candidate.domain}"
    homepage_html = await _fetch(client, base_url)
    homepage_text = _extract_text(homepage_html)

    if len(homepage_text) < MIN_USEFUL_TEXT_LEN:
        pw_text = await _crawl_with_playwright(base_url)
        if len(pw_text) > len(homepage_text):
            homepage_text = pw_text

    pages_crawled = [base_url] if homepage_text else []
    extra_texts = [homepage_text]

    if homepage_text:
        for path in INTERESTING_PATHS[1:3]:
            url = base_url + path
            html = await _fetch(client, url)
            text = _extract_text(html)
            if text:
                extra_texts.append(text)
                pages_crawled.append(url)

    combined = "\n\n".join(t for t in extra_texts if t)[:8000]

    return EnrichedCandidate(
        **candidate.__dict__,
        homepage_text=combined,
        pages_crawled=pages_crawled,
        crawl_ok=bool(combined),
        crawl_error="" if combined else "no usable content",
    )


async def enrich_candidates(candidates: list[Candidate]) -> list[EnrichedCandidate]:
    sem = asyncio.Semaphore(CONFIG.max_concurrent_crawls)

    async with httpx.AsyncClient(follow_redirects=True, http2=True) as client:

        async def bounded(c: Candidate) -> EnrichedCandidate:
            async with sem:
                return await enrich_candidate(client, c)

        return list(await asyncio.gather(*(bounded(c) for c in candidates)))
