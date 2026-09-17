from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time
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
# Wall-clock cost is roughly (attempts / this value) x ~60-150s per round
# (each attempt waits for AI Mode's own response to finish generating,
# which dominates the time, not CPU work). Higher values finish faster but
# open more simultaneous real Chromium windows -- this tool is distributed
# to low-spec laptops (older i3, 8GB RAM) where too many at once makes the
# whole machine unusable during a run, not just the tool itself, so the
# default is deliberately conservative. Raise
# GOOGLE_AI_MODE_MAX_PARALLEL_BROWSERS in .env on a faster machine.
PARALLEL_ATTEMPTS_PER_ROUND = CONFIG.google_ai_mode_max_parallel_browsers

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

# Some markets are genuinely narrow (e.g. "Porcelain Market in Turkey" --
# confirmed by direct manual testing that Google AI Mode itself, asked the
# exact same question with no automation involved, only produces ~60
# distinctly-named companies before running out of ones it's confident are
# real). MIN_TARGET_COMPANIES (200) is aspirational for broad markets, but
# a niche market finishing under this floor after the normal attempt
# budget should get extra widened rounds rather than being accepted as
# final -- confirmed directly that explicitly asking AI Mode to widen into
# adjacent supply-chain categories (raw material suppliers, component
# suppliers, trade houses) surfaced ~50 more real companies in one extra
# response for the same narrow market. NICHE_MARKET_FLOOR is the bar this
# extension phase tries to clear; MAX_EXTRA_NICHE_ATTEMPTS caps how far it
# will go so a truly tiny market doesn't burn attempts forever.
NICHE_MARKET_FLOOR = 90
MAX_EXTRA_NICHE_ATTEMPTS = 10

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
    # Discovery previously used a looser relevance bar than the verification
    # step that follows it, so it surfaced companies that were then rejected:
    # a real run discovered 283 companies and verification threw out 152 of
    # them. Stating the same test here means the companies that come back
    # are the ones that will survive, instead of being found twice and
    # discarded once.
    "Apply this test to each company before including it: does this company itself actually "
    "make, supply, or sell the specific product described above as a real part of its business? "
    "Exclude companies that only operate in a broader or adjacent industry, that merely use or "
    "buy this product rather than provide it, that supply machinery or equipment for making it "
    "unless the request asks for equipment makers, and parent conglomerates whose connection is "
    "only through an unrelated division. If the company does not clearly pass that test, leave "
    "it out.\n\n"
    # Asking for JSON removes the need to guess where one company ends and
    # the next begins. AI Mode's prose answers arrive as one continuous
    # string with no line breaks, so the text parser had to infer entry
    # boundaries from punctuation -- and periods appear inside domains
    # ("amcor.com"), corporate suffixes ("Henkel AG & Co. KGaA") and titles
    # ("Dr. Reddy's Laboratories") just as they do at the end of a sentence.
    # That produced corrupted candidate names ("com Berry Global Inc.",
    # "KGaA"), which then failed to match their verdict during verification
    # and were silently dropped as not relevant. With JSON there is no
    # boundary to infer: the name field is the name. The prose parsers stay
    # in place as a fallback for when AI Mode ignores this instruction,
    # which it sometimes does.
    "Return your answer as a single JSON array inside a ```json code block, with one object "
    "per company and exactly these keys: \"name\" (the full official company name), "
    "\"country\" (headquarters country, or \"\" if unsure), \"website\" (official domain such "
    "as company.com, or \"\" if you are not confident), \"products\" (a short phrase describing "
    "what it makes or does in this market). Output only the JSON array -- no commentary before "
    "or after it. Never guess a website: an empty string is correct when unsure, a wrong domain "
    "is not."
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
    CLI's --brief), the brief's own wording is never rewritten or altered --
    the user's exact text (e.g. their own "150" or "200+ companies") is
    preserved verbatim. The MIN_TARGET_COMPANIES floor is still appended
    AFTER it as a safety net (users are expected to state their own count,
    but a brief that forgets to should still push for real breadth rather
    than default to a short illustrative list)."""
    if mu.brief.strip():
        brief_text = mu.brief.strip() + (
            f"\n\nIf the above does not already specify a target number of companies, "
            f"aim for at least {MIN_TARGET_COMPANIES} real, verifiable companies rather "
            f"than a short illustrative list."
        )
        return ACCURACY_PREFIX + brief_text + ACCURACY_SUFFIX

    # No brief at all -- falls back to a generic auto-generated query
    # instead of whatever the user actually described. This should only
    # ever happen for the bare CLI path with no --brief given; if it
    # happens for a web-UI-driven run, the user's Step 2 description was
    # lost somewhere upstream (frontend state reset, empty textarea, etc.)
    # and they're silently getting a completely different query than what
    # they wrote -- log loudly so this is traceable instead of invisible.
    logger.warning(
        "No brief provided for %r (%s) -- falling back to a generic auto-generated query "
        "instead of a user-described one. If this run came from the web UI, the user's "
        "Step 2 description did not reach the backend.",
        mu.market_name, mu.geography,
    )
    hint = _category_hint(mu.category_prompt)
    base = (
        f"Identify and provide a validated list of at least {MIN_TARGET_COMPANIES} independent, "
        f"relevant, non-overlapping companies/players operating in the {mu.market_name} "
        f"({mu.geography}). {hint} Include major, mid-size, and smaller regional or specialist "
        f"companies -- not just the most famous names. {TABLE_FORMAT_HINT}"
    )
    return ACCURACY_PREFIX + base + ACCURACY_SUFFIX


# A single broad query reliably returns manufacturers/brand owners (the
# most documented, most-searched company type) but rarely surfaces pure
# distributors, suppliers, or technology/equipment providers even when the
# brief explicitly asks for "all company types" -- confirmed via a real run
# that returned 140 companies, all labeled Manufacturer/Parent Company/
# Brand, none Distributor/Supplier/Technology Provider. These targeted
# queries are fired alongside the broad one specifically to surface the
# categories a single broad ask tends to under-represent.
CATEGORY_DIVERSITY_QUERIES = [
    "distributors and wholesalers that SPECIFICALLY distribute or resell products in "
    "this exact market (not general grocery/food wholesalers with no specific, named "
    "connection to this market's products) -- they do not manufacture the products "
    "themselves, but their distribution business is specifically and verifiably tied "
    "to this market's product category",
    "raw material and ingredient suppliers, and equipment/technology/machinery "
    "providers whose supplies or equipment are SPECIFICALLY used to produce this "
    "market's products (not general-purpose suppliers with no named, verifiable "
    "connection to this specific market)",
]


def build_category_diversity_query(mu: MarketUnderstanding, role_description: str) -> str:
    """Build a query focused specifically on one under-represented role
    (e.g. distributors, or suppliers/technology providers), reusing the
    same brief as context so the market definition and inclusion/exclusion
    rules still apply -- only the role focus changes."""
    if mu.brief.strip():
        role_focused = (
            f"{mu.brief.strip()}\n\n"
            f"For this specific query, focus ONLY on identifying real, verifiable "
            f"{role_description}. Do not list manufacturers or brand owners here -- "
            f"only the role described above."
        )
        return ACCURACY_PREFIX + role_focused + ACCURACY_SUFFIX

    hint = _category_hint(mu.category_prompt)
    base = (
        f"Identify and provide a validated list of real, verifiable {role_description} "
        f"operating in the {mu.market_name} ({mu.geography}). {hint} {TABLE_FORMAT_HINT}"
    )
    return ACCURACY_PREFIX + base + ACCURACY_SUFFIX


def build_retry_query(mu: MarketUnderstanding, attempt: int, already_found: list[str]) -> str:
    """Build a follow-up query for a retry attempt, explicitly asking for
    companies not already found, to reduce duplicate-heavy responses.

    Beyond a certain attempt, just repeating "find more, excluding this
    list" plateaus fast -- confirmed directly: manually re-asking AI Mode
    for "more of the same" for a niche market (Turkish porcelain) returned
    almost nothing new, but explicitly asking it to widen into adjacent
    supply-chain categories (raw material/mineral suppliers, component
    suppliers, glaze/frit developers, trade houses/wholesalers) surfaced
    ~50 additional real companies in one response. So once a few retries
    have run, the exclusion note also explicitly invites those adjacent
    categories instead of only asking for "more of the same kind"."""
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
    if attempt >= 3:
        exclusion_note += (
            " The primary list above may already cover the best-known manufacturers and brand "
            "owners -- to find more real companies, widen into the broader supply chain and "
            "adjacent tiers: raw material and mineral processors/suppliers, component suppliers, "
            "industrial glaze/frit/chemical developers, private-label and tier-2 regional "
            "producers, and trade houses/wholesalers/distributors serving this market. Only "
            "include ones you are confident genuinely operate in or supply this specific market."
        )
    return base + exclusion_note


def _mention_to_enriched_candidate(mention, query: str, role_hint: str = "") -> EnrichedCandidate:
    """Build an EnrichedCandidate directly from an AI Mode mention, using its
    own text (country + products) as the classification evidence instead of
    crawling the company's site separately. This is faster but less
    independently verified than a real crawl -- the classifier is trusting
    AI Mode's own description of the company, not the company's own words.

    role_hint: which role-focused query surfaced this mention (e.g.
    "distributors and wholesalers"), if it came from one of the targeted
    CATEGORY_DIVERSITY_QUERIES rather than the broad primary query.
    Confirmed necessary: without this, a company correctly discovered via
    a distributor-focused query was still classified as Manufacturer by
    default, since the classifier only ever saw generic name/product
    evidence with no signal about which role search found it. This is a
    hint the classifier weighs, not a label it blindly trusts -- the
    evidence itself still has to support the final category."""
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
    if role_hint:
        evidence_text += (
            f"\nDiscovery context: this company was found via a search specifically for "
            f"{role_hint} in this market -- treat this as a hint toward that role, but "
            f"still verify against the actual evidence above rather than assuming it.\n"
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


# How long to back off after Google AI Mode's own rate-limit message,
# before trying exactly once more. A fresh throwaway profile doesn't help
# here (the limit is Google-side, not a local cookie/profile issue), so
# the only real remedy is waiting -- this is a single retry, not a loop,
# so a sustained rate limit still surfaces as "0 mentions" for this
# attempt rather than blocking the whole round indefinitely.
RATE_LIMIT_BACKOFF_SECONDS = 45


def _run_one_attempt(query: str, attempt_label: str) -> tuple[str, list]:
    """Run a single AI Mode attempt in its own throwaway Chromium profile
    (deleted afterwards) so it can safely run concurrently with others
    without colliding on a shared --user-data-dir lock file."""
    for retry in range(2):
        profile_dir = str(Path(tempfile.gettempdir()) / f"ai_mode_profile_{uuid.uuid4().hex[:8]}")
        try:
            mentions = google_ai_mode.search(
                query, headless=CONFIG.google_ai_mode_headless, profile_dir=profile_dir
            )
            return attempt_label, mentions
        except google_ai_mode.RateLimitedError:
            if retry == 0:
                logger.warning(
                    "%s hit Google's rate limit -- waiting %ds before one retry",
                    attempt_label, RATE_LIMIT_BACKOFF_SECONDS,
                )
                time.sleep(RATE_LIMIT_BACKOFF_SECONDS)
                continue
            logger.warning("%s still rate-limited after backoff -- giving up on this attempt", attempt_label)
            return attempt_label, []
        finally:
            shutil.rmtree(profile_dir, ignore_errors=True)
    return attempt_label, []


def discover_via_google_ai_mode(
    mu: MarketUnderstanding, cancel_event: threading.Event | None = None
) -> list[EnrichedCandidate]:
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
    discovery source; AI Mode is never trusted as a final answer.

    cancel_event, if given, is checked between rounds (not mid-round, since
    there's no way to safely abort a live browser session already in
    flight) -- if set, whatever candidates have been found so far are
    returned immediately instead of starting another round."""
    if not CONFIG.google_ai_mode_enabled:
        return []

    seen_names: set[str] = set()
    candidates: list[EnrichedCandidate] = []
    attempts_run = 0
    consecutive_zero_rounds = 0
    # Raised past MAX_DISCOVERY_ATTEMPTS once the normal budget is
    # exhausted, if the result is still under NICHE_MARKET_FLOOR -- see
    # the extension check after the loop below. Kept as a separate
    # variable (not a reassignment of the module constant) so concurrent
    # runs never interfere with each other.
    attempt_budget = MAX_DISCOVERY_ATTEMPTS

    with ThreadPoolExecutor(max_workers=PARALLEL_ATTEMPTS_PER_ROUND) as executor:
        while (
            attempts_run < attempt_budget
            and len(candidates) < MIN_TARGET_COMPANIES
            and not (cancel_event is not None and cancel_event.is_set())
        ):
            round_size = min(PARALLEL_ATTEMPTS_PER_ROUND, attempt_budget - attempts_run)
            # Every attempt in a round is built from the SAME already_found
            # snapshot (since they run concurrently, none can see another's
            # results yet) -- duplicates across the round are still caught
            # by seen_names when merging results afterwards.
            already_found = [c.name for c in candidates]

            # Each entry is (query_text, role_hint) -- role_hint is empty for
            # the broad primary/retry queries, and set for the targeted
            # CATEGORY_DIVERSITY_QUERIES so mentions from those can carry
            # that context through to classification (see
            # _mention_to_enriched_candidate's role_hint parameter).
            query_plan: list[tuple[str, str]] = []
            if attempts_run == 0:
                # Reserve a couple of round-1 slots specifically for the
                # categories a broad query tends to miss, instead of every
                # slot asking the same broad question -- this is the fix
                # for a real run that came back 100% Manufacturer/Parent
                # Company/Brand with zero Distributors or Suppliers.
                query_plan.append((build_primary_query(mu), ""))
                for role_description in CATEGORY_DIVERSITY_QUERIES:
                    if len(query_plan) >= round_size:
                        break
                    query_plan.append((build_category_diversity_query(mu, role_description), role_description))
                while len(query_plan) < round_size:
                    query_plan.append((build_retry_query(mu, attempts_run + len(query_plan) + 1, already_found), ""))
            else:
                query_plan = [
                    (build_retry_query(mu, attempts_run + i + 1, already_found), "")
                    for i in range(round_size)
                ]

            logger.info(
                "Google AI Mode discovery round: launching %d parallel attempt(s) "
                "(%d/%d attempts used so far, running total: %d companies)",
                round_size, attempts_run, MAX_DISCOVERY_ATTEMPTS, len(candidates),
            )

            labels = [f"attempt-{attempts_run + i + 1}" for i in range(round_size)]
            futures = {
                executor.submit(_run_one_attempt, q, label): (q, role_hint)
                for (q, role_hint), label in zip(query_plan, labels)
            }

            new_this_round = 0
            for future in as_completed(futures):
                query_for_future, role_hint_for_future = futures[future]
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
                    candidates.append(
                        _mention_to_enriched_candidate(mention, query_for_future, role_hint_for_future)
                    )
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

            if cancel_event is not None and cancel_event.is_set():
                logger.info("Discovery cancelled by user after %d attempt(s), %d companies found so far", attempts_run, len(candidates))
                break

            # A near-empty round (all attempts in it combined added almost
            # nothing) is common transient flakiness in AI Mode's own
            # response generation, confirmed by direct reproduction -- only
            # stop early after enough consecutive near-empty rounds, so the
            # attempt budget actually gets used instead of bailing on a
            # short unlucky streak.
            #
            # Both the per-round "near empty" bar and the number of
            # consecutive rounds tolerated need to scale with round_size.
            # This was originally tuned as a flat "<=2 new companies, 3
            # rounds in a row" back when rounds had 6 parallel attempts --
            # a real flakiness signal at that width (2 of 6 attempts
            # contributing anything). At the low-spec-laptop default of 2
            # parallel browsers, the same flat "<=2 from 2 attempts, 3
            # rounds" bar triggered almost immediately: confirmed directly
            # as the cause of runs stopping at 50-60 companies after only 6
            # of the 12 available attempts, despite the brief explicitly
            # asking for 200+. Scaling both numbers down with round_size
            # keeps the same total "wasted attempt" budget regardless of
            # how many browsers run per round.
            near_empty_threshold = max(1, round_size // 3)
            rounds_before_stopping = max(3, -(-18 // round_size))  # ceil(18 / round_size), floor of 3
            if new_this_round <= near_empty_threshold:
                consecutive_zero_rounds += 1
                if consecutive_zero_rounds >= rounds_before_stopping:
                    logger.warning(
                        "%d consecutive near-empty rounds -- stopping early",
                        consecutive_zero_rounds,
                    )
                    break
            else:
                consecutive_zero_rounds = 0

        # Extension phase: the normal attempt budget is exhausted (or
        # early-stopped) but the result is still under NICHE_MARKET_FLOOR --
        # give it more attempts using the widened supply-chain-aware retry
        # query (build_retry_query already broadens scope once attempt >=
        # 3) instead of accepting a low count as final. Only runs once per
        # discovery call, capped at MAX_EXTRA_NICHE_ATTEMPTS extra attempts,
        # and still respects cancellation between rounds.
        if (
            len(candidates) < NICHE_MARKET_FLOOR
            and len(candidates) < MIN_TARGET_COMPANIES
            and not (cancel_event is not None and cancel_event.is_set())
        ):
            logger.warning(
                "Only %d companies after the normal %d-attempt budget (below the %d floor) -- "
                "this looks like a niche market, running up to %d more widened attempts",
                len(candidates), attempts_run, NICHE_MARKET_FLOOR, MAX_EXTRA_NICHE_ATTEMPTS,
            )
            attempt_budget = attempts_run + MAX_EXTRA_NICHE_ATTEMPTS
            consecutive_zero_rounds = 0
            while (
                attempts_run < attempt_budget
                and len(candidates) < NICHE_MARKET_FLOOR
                and len(candidates) < MIN_TARGET_COMPANIES
                and not (cancel_event is not None and cancel_event.is_set())
            ):
                round_size = min(PARALLEL_ATTEMPTS_PER_ROUND, attempt_budget - attempts_run)
                already_found = [c.name for c in candidates]
                query_plan = [
                    (build_retry_query(mu, attempts_run + i + 1, already_found), "")
                    for i in range(round_size)
                ]
                labels = [f"niche-attempt-{attempts_run + i + 1}" for i in range(round_size)]
                futures = {
                    executor.submit(_run_one_attempt, q, label): (q, role_hint)
                    for (q, role_hint), label in zip(query_plan, labels)
                }
                new_this_round = 0
                for future in as_completed(futures):
                    query_for_future, role_hint_for_future = futures[future]
                    try:
                        label, mentions = future.result()
                    except google_ai_mode.ChromiumNotFoundError as e:
                        logger.error("Google AI Mode discovery cannot run: %s", e)
                        for f in futures:
                            f.cancel()
                        raise
                    logger.info("Google AI Mode %s returned %d company mentions", label, len(mentions))
                    for mention in mentions:
                        key = mention.name.lower().strip()
                        if not key or key in seen_names:
                            continue
                        seen_names.add(key)
                        candidates.append(
                            _mention_to_enriched_candidate(mention, query_for_future, role_hint_for_future)
                        )
                        new_this_round += 1
                attempts_run += round_size
                logger.info(
                    "Niche-extension round added %d new companies, running total: %d (floor: %d, %d extra attempts used)",
                    new_this_round, len(candidates), NICHE_MARKET_FLOOR, attempts_run - (attempt_budget - MAX_EXTRA_NICHE_ATTEMPTS),
                )
                if new_this_round == 0:
                    consecutive_zero_rounds += 1
                    if consecutive_zero_rounds >= 2:
                        logger.warning("No new companies in %d consecutive niche-extension rounds -- stopping", consecutive_zero_rounds)
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
