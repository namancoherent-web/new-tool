from __future__ import annotations

import asyncio
import logging

import httpx

from config import CONFIG
from pipeline.models import Candidate
from sources import brave, ddg, wikidata_source, wikipedia_source
from sources.common import USER_AGENT

logger = logging.getLogger(__name__)

# DDG's unauthenticated HTML endpoint returns the best-quality free results
# in testing (real manufacturer/directory pages), but is prone to a soft
# anti-bot "anomaly" challenge (HTTP 202 with a fake results page) under
# sustained use, especially once a network/session has been flagged from
# earlier heavy testing.
#
# Bing's unauthenticated HTML search is NOT used at all -- confirmed via
# direct testing to be non-functional for this use case, not just noisy: it
# effectively ignores all but one keyword in a multi-word query and returns
# whatever generic/dictionary-style page ranks for that single word (e.g.
# "contract wafer biscuit manufacturing companies" returned Windows File
# Explorer help pages, because it locked onto "contract" and matched
# contract-law definition pages). This isn't fixable by better query wording
# or headers -- it's how the endpoint behaves for any multi-word technical
# phrase when hit without a real browser session. Keeping it in the fallback
# chain only wastes crawl/DeepSeek budget on zero-signal results.
#
# Each query gets its own retry-with-backoff attempt on DDG before falling
# back to Wikipedia -- the anti-bot flag can be transient/query-specific
# rather than a full session ban, and per-query retries stand a real chance
# of getting through.
DDG_MIN_INTERVAL_SECONDS = 1.5
DDG_MAX_RETRIES_PER_QUERY = 3
DDG_RETRY_BASE_DELAY_SECONDS = 2.0
MIN_RESULTS_BEFORE_BACKFILL = 4


# If DDG hasn't produced a single successful result across this many
# consecutive query attempts (each attempt already including its own
# per-query retries), treat it as a full session-level block for the rest of
# the run rather than paying the multi-attempt backoff cost on every
# remaining query -- otherwise a fully-blocked DDG could add tens of minutes
# of pure retry-waiting across 40-70 queries for zero benefit.
DDG_GIVE_UP_AFTER_CONSECUTIVE_QUERY_FAILURES = 5


class _DdgLimiter:
    """Serializes DDG requests (shared pacing across all queries in a run) and
    retries each individual query with exponential backoff before giving up
    on that one query and falling back to Wikipedia. If DDG fails several
    queries in a row even after retries, it's treated as fully blocked for
    the rest of the run to avoid paying the retry cost on every remaining
    query."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._consecutive_query_failures = 0
        self.session_blocked = False

    async def _paced_request(self, client: httpx.AsyncClient, query: str) -> list[Candidate]:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait = DDG_MIN_INTERVAL_SECONDS - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = asyncio.get_event_loop().time()
        return await ddg.search(client, query)

    async def search(self, client: httpx.AsyncClient, query: str) -> list[Candidate]:
        if self.session_blocked:
            return []

        for attempt in range(1, DDG_MAX_RETRIES_PER_QUERY + 1):
            results = await self._paced_request(client, query)
            if results:
                self._consecutive_query_failures = 0
                return results
            if attempt < DDG_MAX_RETRIES_PER_QUERY:
                delay = DDG_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                logger.info(
                    "DDG returned no results for %r (attempt %d/%d, likely anti-bot challenge) -- "
                    "retrying in %.0fs",
                    query, attempt, DDG_MAX_RETRIES_PER_QUERY, delay,
                )
                await asyncio.sleep(delay)

        self._consecutive_query_failures += 1
        logger.info("DDG exhausted retries for %r -- falling back to Wikipedia for this query", query)

        if self._consecutive_query_failures >= DDG_GIVE_UP_AFTER_CONSECUTIVE_QUERY_FAILURES:
            logger.warning(
                "DDG failed %d consecutive queries even with retries -- treating as fully "
                "session-blocked and skipping DDG for the rest of this run.",
                self._consecutive_query_failures,
            )
            self.session_blocked = True
        return []


async def _search_one_query(
    client: httpx.AsyncClient, query: str, engines_ok: dict[str, bool], ddg_limiter: _DdgLimiter
) -> list[Candidate]:
    results: list[Candidate] = []

    if CONFIG.brave_api_key and engines_ok.get("brave", True):
        brave_hits = await brave.search(client, query)
        results.extend(brave_hits)
    else:
        brave_hits = []

    # DDG/Bing only run as backfill when the primary source (Brave, if
    # configured) didn't return enough results -- or as the only source if
    # Brave isn't configured at all.
    need_backfill = len(brave_hits) < MIN_RESULTS_BEFORE_BACKFILL

    ddg_hits: list[Candidate] = []
    if need_backfill and engines_ok.get("duckduckgo", True):
        ddg_hits = await ddg_limiter.search(client, query)
        results.extend(ddg_hits)

    # Bing intentionally excluded -- see module docstring above: confirmed
    # non-functional for multi-word technical queries via direct testing.

    if len(results) < 3 and engines_ok.get("wikipedia", True):
        wiki_hits = await wikipedia_source.search(client, query, max_results=5)
        results.extend(wiki_hits)

    return results


async def run_search_queries(
    queries: list[str], concurrency: int = 5, engines_ok: dict[str, bool] | None = None
) -> list[Candidate]:
    """Run every query through the search chain concurrently (bounded), plus a
    Wikidata structured lookup, and dedup down to one candidate per domain/name.

    engines_ok should come from engine_health.probe_engines() -- any engine
    marked unreachable there is skipped entirely for this run rather than
    retried per query, which is what turns a blocked-network run into 1hr+."""
    if engines_ok is None:
        engines_ok = {"brave": True, "duckduckgo": True, "wikipedia": True, "wikidata": True}

    if not CONFIG.brave_api_key and not any(
        engines_ok.get(k) for k in ("duckduckgo", "wikipedia")
    ):
        logger.error("No usable search engines for this run -- discovery cannot proceed.")
        return []

    sem = asyncio.Semaphore(concurrency)
    ddg_limiter = _DdgLimiter()
    all_results: list[Candidate] = []

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True, http2=True
    ) as client:

        async def bounded(q: str) -> list[Candidate]:
            async with sem:
                return await _search_one_query(client, q, engines_ok, ddg_limiter)

        query_results = await asyncio.gather(*(bounded(q) for q in queries))
        for r in query_results:
            all_results.extend(r)

        if engines_ok.get("wikidata", True):
            wikidata_results = await asyncio.gather(
                *(wikidata_source.search(client, q) for q in queries[:10])
            )
            for r in wikidata_results:
                all_results.extend(r)

    return dedup_candidates(all_results)


def dedup_candidates(candidates: list[Candidate]) -> list[Candidate]:
    seen: dict[str, Candidate] = {}
    for c in candidates:
        key = c.key()
        if not key:
            continue
        if key not in seen:
            seen[key] = c
    return list(seen.values())
