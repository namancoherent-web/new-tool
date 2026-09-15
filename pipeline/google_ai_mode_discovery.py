from __future__ import annotations

import logging
import shutil
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from config import CONFIG
from pipeline.models import EnrichedCandidate, MarketUnderstanding
from sources import google_ai_mode

logger = logging.getLogger(__name__)

# Attempts within a round run concurrently, each in its own throwaway
# Chromium profile (AI Mode works fine signed out, confirmed across every
# test run, so no login/extension state needs to be shared or cloned).
# Bounded to avoid hammering the machine/network with too many simultaneous
# real browser sessions -- this is a real resource cost, not a free knob.
PARALLEL_ATTEMPTS_PER_ROUND = 3

# Every run targets at least this many companies internally, regardless of
# what count (if any) the user's own prompt/brief asks for -- confirmed
# explicit requirement: a user brief asking for 40, 100, or 200 should still
# have the pipeline aim for 200+ candidates before classification narrows
# it down to whatever's actually real and verifiable.
MIN_TARGET_COMPANIES = 200

# If AI Mode queries haven't reached the target company count, retry up to
# this many total attempts (run in parallel rounds of
# PARALLEL_ATTEMPTS_PER_ROUND, so wall-clock cost stays roughly
# attempts / PARALLEL_ATTEMPTS_PER_ROUND browser-session lengths, not one
# per attempt). AI Mode's own response quality is genuinely non-deterministic
# run to run -- the same prompt can return 0, 1, 40, or 180+ mentions on
# different attempts even with no code change (confirmed by direct
# reproduction) -- so more independent attempts per unit of wall-clock time
# is the most reliable lever against that variance.
MAX_DISCOVERY_ATTEMPTS = 12

TABLE_FORMAT_HINT = (
    "as a table with columns: Company Name, Headquarter Country, Core Products/Brands, "
    "Official Website."
)

# Wrapped around EVERY query sent to AI Mode, whether it's the user's own
# detailed brief or our generated fallback. This is the single place that
# pushes AI Mode toward real, verifiable data instead of padding a list with
# invented names once it runs out of famous/well-documented companies --
# confirmed necessary after a spot-check of a real run found a handful of
# unverifiable company names mixed into an otherwise-solid list, all in the
# "smaller regional player" tail of the response where AI Mode has the least
# grounding. Every candidate still goes through DeepSeek classification
# afterwards, but garbage caught here never has to be classified at all.
ACCURACY_PREFIX = (
    "Answer using only real, verifiable, currently operating companies that you have "
    "specific knowledge of -- do not invent, guess, or extrapolate plausible-sounding "
    "company names to pad the list toward any target count. If you are not confident a "
    "company genuinely exists and matches the request, leave it out rather than include it. "
    "It is far better to return a shorter list of companies you are confident are real than "
    "a longer list that includes uncertain or fabricated entries.\n\n"
)

ACCURACY_SUFFIX = (
    "\n\nBefore finalizing your answer, silently double check every company you are about to "
    "list: are you certain it is a real, currently operating company, and does it specifically "
    "and verifiably match the product/category described above (not just a related or "
    "similarly-named industry)? Drop any entry you are not confident about rather than include "
    "it. Do not pad the list to reach any particular count -- accuracy matters more than "
    "quantity.\n\n"
    "For each company, include its official website domain (e.g. company.com) directly next to "
    "its name if you know it with confidence. Leave it out entirely rather than guess at a domain "
    "you are not sure of -- an omitted website is fine, a wrong one is not."
)


def _category_hint(category_prompt: str) -> str:
    normalized = category_prompt.strip().lower()
    if normalized in {"", "all", "all players", "all relevant players", "all companies", "players", "companies"}:
        return "Include all company types: manufacturers, brand owners, suppliers, and distributors."
    return f"Focus specifically on companies that are {category_prompt}."


def build_primary_query(mu: MarketUnderstanding) -> str:
    """Build the single AI Mode query for this run. If the user supplied a
    detailed brief (inclusion/exclusion rules, segmentation, independence
    rules -- e.g. via the web UI's "Describe it in your own words" or the
    CLI's --brief), that brief is sent to AI Mode verbatim, since testing
    confirmed a single well-detailed prompt like that reliably returns
    100+ structured company mentions in one pass -- far more effective than
    splitting into many shorter generic queries. The requested count inside
    the user's own brief text (e.g. "Top 140-150") is left as-is; the
    pipeline's own MIN_TARGET_COMPANIES floor is enforced separately via
    retries, not by rewriting the user's prompt."""
    if mu.brief.strip():
        return ACCURACY_PREFIX + mu.brief.strip() + ACCURACY_SUFFIX

    hint = _category_hint(mu.category_prompt)
    base = (
        f"Identify and provide a validated list of at least {MIN_TARGET_COMPANIES} independent, "
        f"relevant, non-overlapping companies/players operating in the {mu.market_name} "
        f"({mu.geography}). {hint} Include major, mid-size, and smaller regional or specialist "
        f"companies -- not just the most famous names. {TABLE_FORMAT_HINT}"
    )
    return ACCURACY_PREFIX + base + ACCURACY_SUFFIX


def build_retry_query(mu: MarketUnderstanding, attempt: int, already_found: list[str]) -> str:
    """Build a follow-up query for a retry attempt, explicitly asking for
    companies not already found, to reduce duplicate-heavy responses."""
    base = build_primary_query(mu)
    if not already_found:
        return base
    exclusion_note = (
        f"\n\nDo not repeat any of these {len(already_found)} companies already identified: "
        f"{', '.join(already_found[:60])}"
        f"{'...' if len(already_found) > 60 else ''}. "
        f"Find additional real companies not on this list, including smaller regional and "
        f"lesser-known players."
    )
    return base + exclusion_note


def _mention_to_enriched_candidate(mention, query: str) -> EnrichedCandidate:
    """Build an EnrichedCandidate directly from an AI Mode mention, using its
    own text (country + products) as the classification evidence instead of
    crawling the company's site separately. This is faster but less
    independently verified than a real crawl -- the classifier is trusting
    AI Mode's own description of the company, not the company's own words."""
    evidence_text = f"Company: {mention.name}\n"
    if mention.hq_country:
        evidence_text += f"Headquarters: {mention.hq_country}\n"
    if mention.products:
        evidence_text += f"Products/description: {mention.products}\n"
    else:
        evidence_text += (
            "Products/description: none -- no per-company description was available "
            "(this entry came from a name+domain-only list format).\n"
        )
    evidence_text += "\n(Source: Google AI Mode summary, not an independently crawled website.)"

    domain = mention.domain or ""
    return EnrichedCandidate(
        name=mention.name,
        domain=domain,
        url=f"https://{domain}" if domain else "",
        source="google_ai_mode",
        discovery_query=query,
        homepage_text=evidence_text,
        pages_crawled=[],
        crawl_ok=True,
        crawl_error="",
    )


def _run_one_attempt(query: str, attempt_label: str) -> tuple[str, list]:
    """Run a single AI Mode attempt in its own throwaway Chromium profile
    (deleted afterwards) so it can safely run concurrently with others
    without colliding on a shared --user-data-dir lock file."""
    profile_dir = str(Path(tempfile.gettempdir()) / f"ai_mode_profile_{uuid.uuid4().hex[:8]}")
    try:
        mentions = google_ai_mode.search(
            query, headless=CONFIG.google_ai_mode_headless, profile_dir=profile_dir
        )
        return attempt_label, mentions
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


def discover_via_google_ai_mode(mu: MarketUnderstanding) -> list[EnrichedCandidate]:
    """Send the user's brief (or a generated equivalent) to Google AI Mode,
    running attempts in rounds of PARALLEL_ATTEMPTS_PER_ROUND concurrent
    browser sessions (each in its own throwaway profile) up to
    MAX_DISCOVERY_ATTEMPTS total, stopping once MIN_TARGET_COMPANIES is
    reached. Running attempts in parallel matters because AI Mode's own
    response quality is genuinely non-deterministic per call -- the same
    prompt can return 0, 1, 40, or 180+ mentions on different attempts with
    no code change -- so more independent tries per unit of wall-clock time
    directly buys a better chance of hitting the target. Every mention
    returned is still just a name + context -- it flows into the same
    verify -> classify -> Golden Rule filter pipeline as every other
    discovery source; AI Mode is never trusted as a final answer."""
    if not CONFIG.google_ai_mode_enabled:
        return []

    seen_names: set[str] = set()
    candidates: list[EnrichedCandidate] = []
    attempts_run = 0
    consecutive_zero_rounds = 0

    with ThreadPoolExecutor(max_workers=PARALLEL_ATTEMPTS_PER_ROUND) as executor:
        while attempts_run < MAX_DISCOVERY_ATTEMPTS and len(candidates) < MIN_TARGET_COMPANIES:
            round_size = min(PARALLEL_ATTEMPTS_PER_ROUND, MAX_DISCOVERY_ATTEMPTS - attempts_run)
            # Every attempt in a round is built from the SAME already_found
            # snapshot (since they run concurrently, none can see another's
            # results yet) -- duplicates across the round are still caught
            # by seen_names when merging results afterwards.
            already_found = [c.name for c in candidates]
            queries = [
                build_primary_query(mu) if attempts_run == 0 and i == 0
                else build_retry_query(mu, attempts_run + i + 1, already_found)
                for i in range(round_size)
            ]

            logger.info(
                "Google AI Mode discovery round: launching %d parallel attempt(s) "
                "(%d/%d attempts used so far, running total: %d companies)",
                round_size, attempts_run, MAX_DISCOVERY_ATTEMPTS, len(candidates),
            )

            futures = [
                executor.submit(_run_one_attempt, q, f"attempt-{attempts_run + i + 1}")
                for i, q in enumerate(queries)
            ]

            new_this_round = 0
            for future in as_completed(futures):
                try:
                    label, mentions = future.result()
                except google_ai_mode.ChromiumNotFoundError as e:
                    # Not a normal per-attempt failure (bad response,
                    # timeout) -- the browser can never launch, so every
                    # remaining attempt in every remaining round would fail
                    # identically. Stop the whole discovery loop immediately
                    # instead of burning through all 12 attempts uselessly
                    # (confirmed as a real failure mode on a user machine
                    # with no Chromium installed).
                    logger.error("Google AI Mode discovery cannot run: %s", e)
                    for f in futures:
                        f.cancel()
                    raise
                logger.info("Google AI Mode %s returned %d company mentions", label, len(mentions))
                new_this_attempt = 0
                for mention in mentions:
                    key = mention.name.lower().strip()
                    if not key or key in seen_names:
                        continue
                    seen_names.add(key)
                    candidates.append(_mention_to_enriched_candidate(mention, queries[0]))
                    new_this_attempt += 1
                new_this_round += new_this_attempt

            attempts_run += round_size
            logger.info(
                "Round added %d new companies, running total: %d (target: %d+, %d/%d attempts used)",
                new_this_round, len(candidates), MIN_TARGET_COMPANIES, attempts_run, MAX_DISCOVERY_ATTEMPTS,
            )

            if len(candidates) >= MIN_TARGET_COMPANIES:
                logger.info("Reached target of %d+ companies after %d attempt(s)", MIN_TARGET_COMPANIES, attempts_run)
                break

            # A near-empty round (all attempts in it combined added almost
            # nothing) is common transient flakiness in AI Mode's own
            # response generation, confirmed by direct reproduction --
            # only stop early after three rounds in a row come back
            # empty/near-empty, so the attempt budget actually gets used
            # instead of bailing on a short unlucky streak.
            if new_this_round <= 2:
                consecutive_zero_rounds += 1
                if consecutive_zero_rounds >= 3:
                    logger.warning(
                        "%d consecutive near-empty rounds -- stopping early",
                        consecutive_zero_rounds,
                    )
                    break
            else:
                consecutive_zero_rounds = 0

    if len(candidates) < MIN_TARGET_COMPANIES:
        logger.warning(
            "Google AI Mode discovery finished with only %d companies after %d attempts "
            "(target was %d+) -- proceeding with what was found",
            len(candidates), attempts_run, MIN_TARGET_COMPANIES,
        )

    logger.info("Google AI Mode discovery produced %d unique company candidates total", len(candidates))
    return candidates
