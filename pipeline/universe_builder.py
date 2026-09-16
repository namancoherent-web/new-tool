from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from config import CONFIG
from pipeline.ai_mode_verifier import verify_and_classify_via_ai_mode
from pipeline.category_filter import apply_golden_rule
from pipeline.crawler import enrich_candidates
from pipeline.deduplicator import deduplicate
from pipeline.deepseek_client import DeepSeekClient
from pipeline.domain_verifier import verify_domains
from pipeline.directory_miner import expand_via_directories, looks_like_directory
from pipeline.engine_health import probe_engines
from pipeline.exporter import export_all
from pipeline.google_ai_mode_discovery import discover_via_google_ai_mode
from pipeline.market_understanding import understand_market
from pipeline.models import ClassifiedCompany, MarketUnderstanding
from pipeline.prefilter import prefilter_candidates
from pipeline.query_generator import base_queries, widen_queries
from pipeline.search_engine import run_search_queries
from pipeline.verifier import verify_ai_mode_candidates, verify_candidates

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    market_understanding: MarketUnderstanding
    companies: list[ClassifiedCompany]
    dropped_by_category: int
    total_candidates_found: int
    total_verified: int
    engines_used: dict[str, bool]
    duration_seconds: float
    output_paths: dict[str, Path] = field(default_factory=dict)


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


_NO_FILTER_PHRASES = {
    "", "all", "all players", "all relevant players", "all companies",
    "players", "companies", "any", "everyone", "all roles",
}


def _is_no_filter_prompt(category_prompt: str) -> bool:
    return re.sub(r"[^a-z0-9 ]", "", category_prompt.lower()).strip() in _NO_FILTER_PHRASES


class RunCancelled(Exception):
    """Raised to unwind out of run_universe_search when the caller (the API
    layer's Stop button) sets the run's cancel_event. Not an error -- the
    caller catches this specifically and marks the run "cancelled" rather
    than "error". There is no way to forcibly kill a Python thread that's
    mid-Selenium-session or mid-HTTP-call, so cancellation is cooperative:
    checked between pipeline stages (and between AI Mode discovery rounds,
    the longest-running stage) rather than instant."""


def run_universe_search(
    market_name: str,
    geography: str,
    category_prompt: str,
    brief: str = "",
    progress_cb=None,
    cancel_event: threading.Event | None = None,
) -> RunResult:
    """Full Universe Mode pipeline: understand -> discover (with widen loop) ->
    enrich -> verify -> classify -> strict category filter -> dedup -> export.

    category_prompt: a specific role like "Parent Companies" or "Manufacturers"
    applies the strict single-category Golden Rule filter. Leave it as an
    empty string (or "all relevant players"/"players"/"companies") to skip the
    category gate entirely and just keep every company DeepSeek marks relevant
    -- useful for briefs (like a detailed market-scope brief) that want the
    full player universe across all roles, not one role.

    brief: optional detailed free-text scope brief (inclusion/exclusion rules,
    segmentation, independence/non-overlap rules) that gets passed verbatim
    into both market understanding and per-company classification, so rules
    like "exclude semiconductor wafer makers" actually reach the LLM instead
    of being lost when only a short market name is given.

    progress_cb, if given, is called with (stage_name: str, detail: str) at
    each stage transition -- used by CLI/Streamlit/API to show progress.

    cancel_event, if given, is checked between pipeline stages -- when set,
    RunCancelled is raised and the run stops at the next checkpoint (not
    instantly, since there's no way to safely interrupt a live browser
    session or in-flight HTTP call mid-stage)."""
    start = time.time()

    def report(stage: str, detail: str = "") -> None:
        logger.info("[%s] %s", stage, detail)
        if progress_cb:
            progress_cb(stage, detail)

    def check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            report("Cancelled", "Run stopped by user")
            raise RunCancelled()

    report("Understanding market", f"{market_name} / {geography} / {category_prompt}")
    ds_client = DeepSeekClient()
    try:
        mu = understand_market(ds_client, market_name, geography, category_prompt, brief)
    finally:
        ds_client.close()
    check_cancelled()

    ai_mode_only = CONFIG.google_ai_mode_enabled and CONFIG.google_ai_mode_only

    if ai_mode_only:
        engines_ok = {}
    else:
        report("Checking search engine availability", "")
        engines_ok = _run_async(probe_engines())
        if not any(engines_ok.values()):
            report(
                "Search engines unreachable",
                "All free search sources are blocked on this network. "
                "This commonly happens on office/corporate networks with proxy or firewall "
                "restrictions. Try again on a different network, or configure a proxy in .env.",
            )

    if ai_mode_only:
        # Explicit user choice: Google AI Mode is the sole discovery source.
        # Skips the DDG/Wikipedia/Wikidata widen loop and directory-mining
        # entirely -- slower and lower-volume per run than the multi-source
        # path, but avoids DDG's anti-bot blocking issues altogether.
        report("Querying Google AI Mode", "sole discovery source for this run (opens a visible browser window)")
        candidates = discover_via_google_ai_mode(mu, cancel_event=cancel_event)
        check_cancelled()
        report("Google AI Mode discovery complete", f"{len(candidates)} companies found")
    else:
        report("Searching sources", f"{len(mu.search_queries)} initial queries")
        queries = base_queries(mu)
        candidates = _run_async(run_search_queries(queries, engines_ok=engines_ok))

        widen_round = 0
        while (
            len(candidates) < CONFIG.discovery_target_companies
            and widen_round < CONFIG.discovery_max_widen_rounds
        ):
            widen_round += 1
            extra_queries = widen_queries(mu, widen_round)
            report("Discovering companies", f"widen round {widen_round}: {len(candidates)} found so far")
            extra_candidates = _run_async(run_search_queries(extra_queries, engines_ok=engines_ok))

            before = len(candidates)
            merged = {c.key(): c for c in candidates}
            for c in extra_candidates:
                merged.setdefault(c.key(), c)
            candidates = list(merged.values())

            new_found = len(candidates) - before
            if new_found == 0:
                report("Widen loop plateaued", f"round {widen_round} found no new companies, stopping")
                break

        if CONFIG.google_ai_mode_enabled:
            report("Querying Google AI Mode", "broad discovery pass (this opens a visible browser window)")
            ai_mode_candidates = discover_via_google_ai_mode(mu)
            before = len(candidates)
            merged = {c.key(): c for c in candidates}
            for c in ai_mode_candidates:
                merged.setdefault(c.key(), c)
            candidates = list(merged.values())
            report("Google AI Mode discovery complete", f"{len(candidates) - before} new companies found")

        directory_pages = [c for c in candidates if looks_like_directory(c)]
        if directory_pages:
            report(
                "Mining directory pages",
                f"{len(directory_pages)} directory/marketplace pages found in results, extracting company links",
            )
            directory_links = _run_async(expand_via_directories(candidates))
            before = len(candidates)
            merged = {c.key(): c for c in candidates}
            for c in directory_links:
                merged.setdefault(c.key(), c)
            candidates = list(merged.values())
            report("Directory mining complete", f"{len(candidates) - before} new companies found from directories")

        # directory/marketplace pages themselves are not companies -- drop
        # them now that their links have been extracted, so they don't get
        # crawled and classified as if they were a single business
        directory_domains = {c.domain for c in directory_pages}
        candidates = [c for c in candidates if c.domain not in directory_domains]

    total_candidates_found = len(candidates)
    candidates = prefilter_candidates(candidates, mu)
    noise_dropped = total_candidates_found - len(candidates)
    if noise_dropped:
        report(
            "Pre-filtering obvious noise",
            f"dropped {noise_dropped} clearly irrelevant results (dictionaries, portals, "
            f"generic retailers) before crawling, {len(candidates)} candidates remain",
        )

    if ai_mode_only:
        # AI-Mode-only candidates already carry their own evidence text
        # (from discover_via_google_ai_mode) -- no separate website crawl.
        report("Skipping website crawl", f"{len(candidates)} candidates already carry AI Mode evidence text")
        enriched = candidates
    else:
        report("Crawling websites", f"{len(candidates)} candidates")
        enriched = _run_async(enrich_candidates(candidates))

    check_cancelled()
    report("Verifying", f"{len(enriched)} enriched candidates")
    if ai_mode_only:
        verified = verify_ai_mode_candidates(enriched)
    else:
        verified = verify_candidates(enriched, mu)
    verified_ok = [v for v in verified if not v.rejected]
    total_verified = len(verified_ok)

    check_cancelled()
    report("Classifying", f"{total_verified} verified candidates (Google AI Mode)")
    classified = verify_and_classify_via_ai_mode(verified_ok, mu, cancel_event=cancel_event)
    check_cancelled()

    no_category_filter = _is_no_filter_prompt(category_prompt)
    if no_category_filter:
        report("Skipping category filter", "no specific role requested, keeping all relevant players")
        kept = [c for c in classified if c.is_relevant]
        dropped = [c for c in classified if not c.is_relevant]
    else:
        report("Applying category filter", f"target category: {category_prompt}")
        kept, dropped = apply_golden_rule(classified, category_prompt)

    report("Deduplicating", f"{len(kept)} companies before dedup")
    final_companies = deduplicate(kept)
    final_companies.sort(key=lambda c: (-c.confidence, c.company_name.lower()))

    report("Verifying website domains", f"checking {sum(1 for c in final_companies if c.website)} claimed domains")
    _run_async(verify_domains(final_companies))

    report("Exporting", f"{len(final_companies)} final companies")
    basename = f"{_slugify(market_name)}_{_slugify(geography)}_{_slugify(category_prompt)}"
    output_paths = export_all(final_companies, market_name, geography, CONFIG.outputs_dir, basename)

    duration = time.time() - start
    report("Done", f"{len(final_companies)} companies in {duration:.0f}s")

    return RunResult(
        market_understanding=mu,
        companies=final_companies,
        dropped_by_category=len(dropped),
        total_candidates_found=total_candidates_found,
        total_verified=total_verified,
        engines_used=engines_ok,
        duration_seconds=duration,
        output_paths=output_paths,
    )


def _run_async(coro):
    import asyncio

    return asyncio.run(coro)
