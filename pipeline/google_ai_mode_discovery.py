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

# How many candidates discovery collects before it stops. Deliberately far
# above the 200 RELEVANT companies a run aims to deliver, because
# verification rejects a large share of what discovery finds: measured
# across real runs roughly 40% survive (232 discovered -> 91 relevant;
# 283 -> 131). Stopping discovery at 200 therefore guaranteed a final
# result near 100. Collecting ~600 means one pass clears the target at the
# observed survival rate, instead of needing extra discover/verify passes.
MIN_TARGET_COMPANIES = 600

# If AI Mode queries haven't reached the target company count, retry up to
# this many total attempts (run in parallel rounds of
# PARALLEL_ATTEMPTS_PER_ROUND, so wall-clock cost stays roughly
# attempts / PARALLEL_ATTEMPTS_PER_ROUND browser-session lengths, not one
# per attempt). AI Mode's own response quality is genuinely non-deterministic
# run to run -- the same prompt can return 0, 1, 40, or 180+ mentions on
# different attempts even with no code change (confirmed by direct
# reproduction) -- so more independent attempts per unit of wall-clock time
# is the most reliable lever against that variance.
MAX_DISCOVERY_ATTEMPTS = 24

# Some markets are genuinely narrow (e.g. "Porcelain Market in Turkey" --
# confirmed by direct manual testing that Google AI Mode itself, asked the
# exact same question with no automation involved, only produces ~60
# distinctly-named companies before running out of ones it's confident are
# real). MIN_TARGET_COMPANIES is aspirational for broad markets, but
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

# Hard cap on how many already-found company names are listed back to AI Mode
# in a retry query. This is a reliability limit, not a quality one: at 60
# names a retry query reached ~5000 characters, and Google AI Mode responds
# "Something went wrong and the content wasn't generated" to inputs that
# large. Keeping the prompt small is what stops that error, and the most
# recent names are the ones AI Mode is most likely to repeat anyway.
EXCLUSION_LIST_MAX_NAMES = 25

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
# Kept deliberately short. An over-long prompt is itself a failure mode:
# Google AI Mode answers "Something went wrong and the content wasn't
# generated" on very large inputs, and the wrapper plus a 60-name exclusion
# list had pushed retry queries to ~5000 characters, which is what made that
# error appear repeatedly. Every rule below still earns its place -- they are
# just stated once, briefly, instead of at length.
ACCURACY_PREFIX = (
    "List only real, currently operating companies you actually know. "
    "Never invent names to reach a count -- a shorter true list beats a padded one.\n\n"
)

ACCURACY_SUFFIX = (
    # The relevance test matters: discovery previously used a looser bar than
    # the verification step that follows it, so a real run discovered 283
    # companies and verification threw out 152. Stating the same test here
    # means the companies that come back are the ones that will survive.
    "\n\nInclude a company only if it itself makes, supplies or sells this exact product as a "
    "real part of its business. Exclude adjacent industries, buyers/users of the product, "
    "machinery and equipment makers, and conglomerates linked only via an unrelated division.\n\n"
    # JSON removes the need to guess where one company ends and the next
    # begins: AI Mode's prose arrives as one unbroken string, and periods
    # inside domains ("amcor.com"), suffixes ("Henkel AG & Co. KGaA") and
    # titles ("Dr. Reddy's Laboratories") made the text parser produce
    # corrupted names. With JSON the name field is the name.
    "Reply with ONLY a JSON array, no other text:\n"
    # An obviously-fake placeholder name is used here on purpose. A real run
    # showed AI Mode echoing "Full Company Name" back as if it were an actual
    # discovered company when the market was hard to find candidates for --
    # a schema example with realistic-looking field values gets treated as
    # real data under exactly those conditions.
    '[{"name":"ACME EXAMPLE CO (do not output this exact name)","country":"HQ country",'
    '"website":"domain.com","products":"what it makes"}]\n'
    "Use \"\" for anything you are unsure of -- never guess a website."
)


def _category_hint(category_prompt: str) -> str:
    normalized = category_prompt.strip().lower()
    if normalized in {"", "all", "all players", "all relevant players", "all companies", "players", "companies"}:
        return "Include all company types: manufacturers, brand owners, suppliers, and distributors."
    return f"Focus specifically on companies that are {category_prompt}."


# Longest brief that is sent to AI Mode untouched. Real user briefs can be
# very long -- a detailed market-scope brief with full segmentation ran to
# ~4900 characters, which pushed the finished query past 5600 and the retry
# query past 6800. Google AI Mode answers "Something went wrong and the
# content wasn't generated" at that size, so an over-long brief silently
# broke every attempt in the run. Briefs under this limit are never altered.
MAX_BRIEF_CHARS = 3200

# Section headings whose contents are enumeration rather than instruction.
# When a brief has to be shortened these go first: a segmentation matrix
# ("By Form: Powder, Liquid, ...") tells AI Mode far less about WHICH
# companies to find than the inclusion/exclusion rules do.
_DROPPABLE_SECTION_PREFIXES = (
    "by solution type", "by sugar-reduction level", "by form", "by application",
    "by end user", "by distribution channel", "by product type", "by price range",
    "by material grade", "by type", "by segment", "by category", "by channel",
)


def _fit_brief(brief: str) -> str:
    """Shorten an over-long brief while keeping the parts that actually steer
    which companies come back.

    Order of removal: segmentation/enumeration sections first, then a hard
    truncation as a last resort. The selection and exclusion rules are what
    make results relevant, so they are preserved for as long as possible."""
    if len(brief) <= MAX_BRIEF_CHARS:
        return brief

    lines = brief.splitlines()
    kept: list[str] = []
    dropping = False
    for line in lines:
        stripped = line.strip().lower()
        if any(stripped.startswith(p) for p in _DROPPABLE_SECTION_PREFIXES):
            dropping = True
            continue
        # A new non-list heading ends the dropped section. List items under a
        # segmentation heading are short and unpunctuated, so anything longer
        # or sentence-like is treated as the start of real instruction again.
        if dropping and (len(stripped) > 60 or stripped.endswith((":", ".")) or stripped.startswith("-")):
            dropping = False
        if not dropping:
            kept.append(line)

    trimmed = "\n".join(kept).strip()

    # Still too long: drop whole sections in order of least value to company
    # discovery. A blunt tail-truncation was tried first and was wrong -- it
    # cut the independence and no-duplicate rules, which are exactly the
    # rules that keep the final list clean.
    if len(trimmed) > MAX_BRIEF_CHARS:
        for heading in ("final validation", "player selection criteria", "north america relevance"):
            out, dropping_section = [], False
            for line in trimmed.splitlines():
                low = line.strip().lower()
                if low.startswith(heading):
                    dropping_section = True
                    continue
                if dropping_section and low and not low.startswith(("-", "•")) and len(low) < 45 and low[:1].isupper():
                    dropping_section = False
                if not dropping_section:
                    out.append(line)
            trimmed = "\n".join(out).strip()
            if len(trimmed) <= MAX_BRIEF_CHARS:
                break

    if len(trimmed) > MAX_BRIEF_CHARS:
        trimmed = trimmed[:MAX_BRIEF_CHARS].rsplit("\n", 1)[0].strip()

    logger.warning(
        "Brief shortened from %d to %d chars to stay under the size Google AI Mode "
        "reliably accepts. Selection and exclusion rules are preserved first.",
        len(brief), len(trimmed),
    )
    return trimmed


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
        brief_text = _fit_brief(mu.brief.strip()) + (
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

    # The exclusion list is capped hard. Listing 60 names pushed retry queries
    # to ~5000 characters, and Google AI Mode answers "Something went wrong and
    # the content wasn't generated" on inputs that large -- the exclusion list
    # meant to improve results was in fact the main cause of failed attempts.
    # The most recently found names are the ones AI Mode is most likely to
    # repeat, so those are the ones worth sending.
    # "Already found" as a standalone lead-in phrase was itself picked up as a
    # company name in a real run once -- reworded so the exclusion list can
    # never be mistaken for the start of an entry list.
    recent = already_found[-EXCLUSION_LIST_MAX_NAMES:]
    exclusion_note = (
        f"\n\nDo NOT include any of these {len(already_found)} companies (already found): "
        f"{', '.join(recent)}.\nFind DIFFERENT companies, including smaller and lesser-known ones."
    )
    if attempt >= 3:
        exclusion_note += (
            "\n\nThe obvious market leaders are likely covered. Widen into the supply chain: "
            "raw material and component suppliers, tier-2 and regional producers, private-label "
            "makers, and distributors -- only ones genuinely active in this market."
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


# "Something went wrong and the content wasn't generated" is transient and
# not account- or IP-bound, so unlike a rate limit it is worth retrying
# immediately rather than backing off. Each retry builds a brand-new
# throwaway profile below, so the retry always starts from clean cookies
# and state. Observed repeatedly in real runs; previously undetected, which
# silently wasted the attempt.
GENERATION_FAILED_ATTEMPTS = 3
GENERATION_FAILED_PAUSE_SECONDS = 5

# Any other AI Mode failure (timeout, stale element, renderer crash, blank
# page) also gets a clean-profile retry rather than silently costing the
# attempt. Each retry builds a new throwaway profile, so state is always
# fresh.
OTHER_ERROR_ATTEMPTS = 3


def _run_one_attempt(query: str, attempt_label: str) -> tuple[str, list]:
    """Run a single AI Mode attempt in its own throwaway Chromium profile
    (deleted afterwards) so it can safely run concurrently with others
    without colliding on a shared --user-data-dir lock file."""
    rate_limit_retry_used = False
    generation_failures = 0
    other_failures = 0

    while True:
        profile_dir = str(Path(tempfile.gettempdir()) / f"ai_mode_profile_{uuid.uuid4().hex[:8]}")
        try:
            mentions = google_ai_mode.search(
                query, headless=CONFIG.google_ai_mode_headless, profile_dir=profile_dir
            )
            return attempt_label, mentions
        except google_ai_mode.GenerationFailedError:
            generation_failures += 1
            if generation_failures < GENERATION_FAILED_ATTEMPTS:
                logger.warning(
                    "%s: AI Mode failed to generate content (%d/%d) -- retrying with a clean profile",
                    attempt_label, generation_failures, GENERATION_FAILED_ATTEMPTS,
                )
                time.sleep(GENERATION_FAILED_PAUSE_SECONDS)
                continue
            logger.warning(
                "%s: AI Mode failed to generate content %d times -- giving up on this attempt",
                attempt_label, generation_failures,
            )
            return attempt_label, []
        except google_ai_mode.RateLimitedError:
            if not rate_limit_retry_used:
                rate_limit_retry_used = True
                logger.warning(
                    "%s hit Google's rate limit -- waiting %ds before one retry",
                    attempt_label, RATE_LIMIT_BACKOFF_SECONDS,
                )
                time.sleep(RATE_LIMIT_BACKOFF_SECONDS)
                continue
            logger.warning("%s still rate-limited after backoff -- giving up on this attempt", attempt_label)
            return attempt_label, []
        except google_ai_mode.CaptchaBlockedError:
            # A bot check the solver could not clear. Retrying immediately
            # with yet another fresh profile tends to make this worse, since
            # a brand-new profile is exactly what Google challenges, so this
            # attempt is abandoned instead.
            logger.warning("%s: bot check could not be cleared -- abandoning this attempt", attempt_label)
            return attempt_label, []
        except google_ai_mode.ChromiumNotFoundError:
            raise
        except Exception as e:
            # Any other failure (timeout, stale element, renderer crash, a
            # blank page) is treated the same way: throw the profile away and
            # try again from clean state. Previously these fell through and
            # silently cost the whole attempt.
            other_failures += 1
            if other_failures < OTHER_ERROR_ATTEMPTS:
                logger.warning(
                    "%s errored (%d/%d): %s -- retrying with a clean profile",
                    attempt_label, other_failures, OTHER_ERROR_ATTEMPTS, e,
                )
                time.sleep(GENERATION_FAILED_PAUSE_SECONDS)
                continue
            logger.warning("%s errored %d times -- giving up on this attempt: %s",
                           attempt_label, other_failures, e)
            return attempt_label, []
        finally:
            shutil.rmtree(profile_dir, ignore_errors=True)


def discover_via_google_ai_mode(
    mu: MarketUnderstanding,
    cancel_event: threading.Event | None = None,
    already_found: list[str] | None = None,
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

    # Names already collected by an earlier pass. They seed seen_names so this
    # pass never returns them again, and they are fed into the retry queries'
    # exclusion list so AI Mode is explicitly asked for different companies --
    # without this, a second pass just re-returns the first pass's list.
    prior_names = [n.strip() for n in (already_found or []) if n.strip()]
    seen_names: set[str] = {n.lower() for n in prior_names}
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
            already_found = prior_names + [c.name for c in candidates]

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
                already_found = prior_names + [c.name for c in candidates]
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
