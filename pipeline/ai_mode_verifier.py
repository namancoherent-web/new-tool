from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from config import CONFIG
from pipeline.models import ClassifiedCompany, MarketUnderstanding, VerifiedCandidate
from sources import google_ai_mode

logger = logging.getLogger(__name__)

# Google AI Mode replaces DeepSeek entirely for the verify/classify step --
# DeepSeek is only used for market understanding now. A single AI Mode
# query can't reliably hold hundreds of companies and return clean
# structured JSON for every one, so candidates are split into batches and
# verified in parallel, mirroring the discovery step's own approach.
# PARALLEL_VERIFY_BATCHES shares the same low-spec-laptop constraint as
# discovery's PARALLEL_ATTEMPTS_PER_ROUND -- each batch is a real Chromium
# window, so this is capped by the same .env setting rather than a
# separate hardcoded number, to avoid two independent concurrency knobs
# that both need tuning for the same underlying hardware limit.
VERIFY_BATCH_SIZE = 40
PARALLEL_VERIFY_BATCHES = CONFIG.google_ai_mode_max_parallel_browsers

ACCURACY_PREFIX = (
    "You are verifying a list of companies that were already discovered as candidates "
    "for a specific market. For each company below, use your own real knowledge to judge "
    "whether it genuinely, verifiably belongs in this market -- do not assume a company "
    "belongs just because its name was given to you. If you are not confident a company "
    "is real or relevant, mark it as not relevant rather than guessing.\n\n"
)


def _build_verify_query(mu: MarketUnderstanding, batch: list[VerifiedCandidate]) -> str:
    company_lines = "\n".join(f"- {c.name}" + (f" ({c.domain})" if c.domain else "") for c in batch)
    scope_section = mu.brief.strip() if mu.brief.strip() else (
        f"Market: {mu.market_name} ({mu.geography}). "
        f"Definition: {mu.definition or 'not specified'}. "
        f"Out-of-scope types: {', '.join(mu.boundary.out_of_scope) if mu.boundary and mu.boundary.out_of_scope else 'none specified'}."
    )
    return (
        f"{ACCURACY_PREFIX}"
        f"Market scope and rules:\n\"\"\"\n{scope_section}\n\"\"\"\n\n"
        f"Companies to verify:\n{company_lines}\n\n"
        f"For EVERY company listed above, respond as a table with exactly these columns: "
        f"Company Name, Is Relevant (yes/no), Category (one of: Manufacturer, Parent Company, "
        f"Distributor, Supplier, Technology Provider, Brand, Retailer, Investor, Service Provider, Other), "
        f"Brand Name (the product brand if different from the company name, else repeat the company name), "
        f"Parent Or Independent (Independent, or \"Subsidiary of X\" naming the real parent if you know one, "
        f"or \"Parent Company\" if this company IS a parent to others), HQ Country, Reason (one sentence "
        f"citing why it is or is not relevant, and what specifically it makes/does).\n\n"
        f"Cover every single company listed above, in the same order -- do not skip any, even to mark "
        f"them not relevant. Do not add companies that were not in the list.\n\n"
        # Same reasoning as the discovery prompt: a JSON array has explicit
        # field boundaries, so a verdict's company name cannot pick up a
        # fragment of the previous row (the markdown-table path produced
        # names like "com Berry Global Inc." and "have been omitted as
        # requested. Akgun Seramik", which then failed to match the
        # candidate they belonged to and were dropped as not relevant).
        # Table parsing stays as a fallback for answers that ignore this.
        f"Return the answer as a single JSON array inside a ```json code block, one object per "
        f"company, with exactly these keys: \"name\" (copy the company name exactly as given "
        f"above), \"is_relevant\" (true or false), \"category\", \"brand_name\", "
        f"\"parent_or_independent\", \"country\", \"reason\". Output only the JSON array, with "
        f"no commentary before or after it."
    )


_TABLE_ROW_PATTERN = re.compile(r"^\|(.+)\|$", re.MULTILINE)

# A real company name cell should not start with a bare domain-suffix
# fragment (leftover from the previous row's website getting merged in,
# e.g. "com Berry Global Inc.", "co.jp Rengo Co., Ltd.") or with lowercase
# disclaimer prose (leftover from a footnote like "... have been omitted
# as requested." bleeding into the next cell). This must stay narrow --
# legal suffixes like "S.A. de C.V.", "U.S. Plastic Corp.", "GmbH & Co.
# KG", "A.J. Plast..." are extremely common in real company names and
# must never be flagged just for containing a period followed by more
# text (confirmed directly: an earlier, broader version of this check
# wrongly flagged real companies like "Treofan Germany GmbH & Co. KG" and
# "U.S. Plastic Corp." as corrupted).
_GARBAGE_NAME_PREFIX = re.compile(
    r"^(com(\.[a-z]{2,3})?\b|co\.[a-z]{2}\b|have been|were omitted|as requested|and\s)",
    re.IGNORECASE,
)
# A leaked bare 2-letter country-code TLD prefix ("cn Sigma Plastics
# Group", "kr Kolon Industries") must be checked case-sensitively --
# genuine initials like "MF Art Ceramic" or "TC Transcontinental" would
# also match a case-insensitive [a-z]{2}, wrongly flagging real names.
_GARBAGE_TLD_PREFIX = re.compile(r"^[a-z]{2}\s[A-Z]")

# Lowercase-first is treated as corrupted only when it also looks like a
# leaked sentence fragment (multiple lowercase words) rather than a
# legitimately lowercase-styled brand name ("ePac Flexible Packaging",
# "vonco products") -- confirmed both those real names have a single
# lowercase-leading word followed by title-case/normal words, so requiring
# at least two consecutive lowercase words before flagging avoids treating
# real (if unusually styled) names as garbage.
_LOWERCASE_SENTENCE_FRAGMENT = re.compile(r"^[a-z]+\s+[a-z]+")


def _looks_like_company_name(name: str) -> bool:
    if _GARBAGE_NAME_PREFIX.match(name) or _GARBAGE_TLD_PREFIX.match(name):
        return False
    if name[:1].isupper() or name[:1].isdigit():
        return True
    # Lowercase-first: only corrupted if it reads as a sentence fragment
    # (two+ lowercase words in a row), not a single stylized lowercase
    # brand-name word followed by normal capitalization.
    return not _LOWERCASE_SENTENCE_FRAGMENT.match(name)


def _recover_name_from_garbage(name: str) -> str:
    """A corrupted cell often still has the real company name as the tail
    of the fragment, after a leaked bare domain-suffix prefix ('com.tr
    Karaca' -> 'Karaca', 'com Berry Global Inc.' -> 'Berry Global Inc.')
    or a disclaimer sentence boundary ('have been omitted as requested.
    Akgün Seramik' -> 'Akgün Seramik'). Best-effort only."""
    # First try stripping a leaked bare domain-suffix prefix -- this is
    # the common case and must not require splitting on "." first, since
    # the recovered name itself may legitimately contain more periods
    # (e.g. "com Berry Global Inc." -> "Berry Global Inc."). Covers a
    # bare "com"/"co.jp"-style leak as well as a bare 2-letter
    # country-code TLD leak ("cn Sigma Plastics Group", "kr Kolon
    # Industries, Inc.").
    stripped = re.sub(
        r"^(com(\.[a-z]{2,3})?|co\.[a-z]{2}|[a-z]{2}|and)\s+(?=[A-Z0-9])", "", name, flags=re.IGNORECASE
    ).strip()
    if stripped != name and stripped and (stripped[:1].isupper() or stripped[:1].isdigit()):
        return stripped
    # Otherwise assume a disclaimer-sentence boundary: take the tail after
    # the last ". " and only accept it if that tail alone still looks like
    # a company name (recursing into the same garbage-prefix check catches
    # a domain-suffix leak stacked after the sentence boundary too).
    parts = re.split(r"[.]\s+", name)
    tail = parts[-1].strip() if parts else ""
    if tail and tail != name:
        return _recover_name_from_garbage(tail) or (
            tail if (tail[:1].isupper() or tail[:1].isdigit()) and _looks_like_company_name(tail) else ""
        )
    return ""


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL | re.IGNORECASE)
_BARE_JSON_ARRAY = re.compile(r"(\[\s*\{.*\}\s*\])", re.DOTALL)


def _parse_verify_json(answer_text: str) -> dict[str, dict]:
    """Read the JSON array of verdicts the verification prompt asks for.
    Preferred over the markdown-table path because the name field has an
    explicit boundary and cannot absorb a fragment of the previous row."""
    blocks = [m.group(1) for m in _JSON_BLOCK.finditer(answer_text)]
    if not blocks:
        bare = _BARE_JSON_ARRAY.search(answer_text)
        if bare:
            blocks = [bare.group(1)]

    results: dict[str, dict] = {}
    for block in blocks:
        try:
            rows = json.loads(block)
        except ValueError:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            relevant = row.get("is_relevant")
            if isinstance(relevant, str):
                relevant = relevant.strip().lower().startswith(("y", "t"))
            results[name.lower()] = {
                "company_name": name,
                "is_relevant": bool(relevant),
                "category": str(row.get("category") or "").strip() or "Other",
                "brand_name": str(row.get("brand_name") or "").strip() or name,
                "parent_or_independent": str(row.get("parent_or_independent") or "").strip() or "Independent",
                "hq_country": str(row.get("country") or "").strip(),
                "reason": str(row.get("reason") or "").strip(),
            }
    if results:
        logger.info("Parsed %d verdicts from AI Mode's JSON response", len(results))
    return results


def _parse_verify_table(answer_text: str, tables: list[str], batch_names: set[str]) -> dict[str, dict]:
    """Parse AI Mode's verification response into {company_name_lower: {...}}.
    Reads the requested JSON array first, then falls back to markdown
    tables for answers that ignore the format -- mirroring the same
    tolerance discovery parsing needs for AI Mode's inconsistent response
    shapes."""
    results: dict[str, dict] = dict(_parse_verify_json(answer_text))

    for table_md in tables:
        lines = [l.strip() for l in table_md.splitlines() if l.strip()]
        if len(lines) < 3:
            continue
        header = [h.strip().lower() for h in lines[0].strip("|").split("|")]

        def find_col(*keywords: str) -> int | None:
            for i, h in enumerate(header):
                if any(k in h for k in keywords):
                    return i
            return None

        name_idx = find_col("company", "name") or 0
        relevant_idx = find_col("relevant")
        category_idx = find_col("category")
        brand_idx = find_col("brand")
        parent_idx = find_col("parent", "independent")
        country_idx = find_col("country", "hq")
        reason_idx = find_col("reason")

        for row_line in lines[2:]:
            cells = [c.strip() for c in row_line.strip("|").split("|")]
            if len(cells) <= name_idx:
                continue
            name = re.sub(r"\*\*|\[|\]\([^)]*\)", "", cells[name_idx]).strip()
            if not name or name.lower() in {"-", "n/a"}:
                continue
            if not _looks_like_company_name(name):
                # AI Mode occasionally renders a malformed row -- a
                # trailing domain-suffix fragment (".com", ".com.tr") or a
                # disclaimer sentence ("... have been omitted as
                # requested.") bleeding into what should be the next row's
                # name cell -- confirmed via a real run where 19 real
                # companies (including well-known ones like Vitra) were
                # silently dropped because their verdict row's name cell
                # got corrupted this way. Try to recover the real company
                # name from the tail of the fragment (after the last
                # sentence-ending period) instead of accepting garbage or
                # silently losing the row.
                recovered = _recover_name_from_garbage(name)
                if recovered:
                    # Prefer an exact match against the batch's own
                    # candidate names when there is one (most reliable),
                    # but don't require it -- exact punctuation/suffix
                    # differences between AI Mode's rendering and our
                    # stored candidate name ("Berry Global" vs "Berry
                    # Global Inc.") shouldn't cause a successful recovery
                    # to be thrown away. Confirmed as a real cause of mass
                    # verdict loss: a whole batch's names were all
                    # prefixed with a leaked domain suffix, recovery
                    # worked, but the exact-match requirement discarded
                    # every single one anyway.
                    name = recovered
                else:
                    logger.warning("Skipping malformed verification row (name cell looked corrupted): %r", name)
                    continue
            key = name.lower()
            # A JSON verdict for this company is authoritative -- it has
            # clean field boundaries, so it must not be replaced by a
            # table row for the same name.
            if key in results:
                continue
            results[key] = {
                "company_name": name,
                "is_relevant": _cell(cells, relevant_idx).lower().startswith("y"),
                "category": _cell(cells, category_idx) or "Other",
                "brand_name": _cell(cells, brand_idx) or name,
                "parent_or_independent": _cell(cells, parent_idx) or "Independent",
                "hq_country": _cell(cells, country_idx),
                "reason": _cell(cells, reason_idx),
            }

    return results


def _cell(cells: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(cells):
        return ""
    return cells[idx].strip()


# Same rationale as discovery's RATE_LIMIT_BACKOFF_SECONDS: a fresh local
# profile doesn't bypass Google's own server-side rate limit, so the only
# real remedy is waiting once before giving up on this batch.
RATE_LIMIT_BACKOFF_SECONDS = 45

# Total attempts per batch, covering both a Google-side rate limit and a
# transient browser/Selenium fault. Losing a batch costs up to
# VERIFY_BATCH_SIZE real companies (they get marked not relevant), so a
# couple of extra attempts is cheap insurance against a flaky browser.
VERIFY_ATTEMPTS = 3
TRANSIENT_RETRY_SECONDS = 5


def _run_one_verify_batch(query: str, label: str) -> tuple[str, dict]:
    """Run a single verification query in its own throwaway Chromium
    profile so batches can run concurrently without colliding on a shared
    --user-data-dir lock file, same pattern as discovery attempts."""
    for retry in range(VERIFY_ATTEMPTS):
        profile_dir = str(Path(tempfile.gettempdir()) / f"ai_mode_verify_profile_{uuid.uuid4().hex[:8]}")
        try:
            from google_ai_scraper import GoogleAIModeScraper
            scraper = None
            try:
                scraper = GoogleAIModeScraper(
                    headless=CONFIG.google_ai_mode_headless, verbose=False, profile_dir=profile_dir
                )
                result = scraper.ask_ai_mode(query)
            finally:
                if scraper:
                    try:
                        scraper.close()
                    except Exception:
                        pass
            if result.get("rate_limited"):
                if retry < VERIFY_ATTEMPTS - 1:
                    logger.warning(
                        "Verification batch %s hit Google's rate limit -- waiting %ds before retry",
                        label, RATE_LIMIT_BACKOFF_SECONDS,
                    )
                    time.sleep(RATE_LIMIT_BACKOFF_SECONDS)
                    continue
                logger.warning("Verification batch %s still rate-limited after backoff -- giving up", label)
                return label, {}
            if not result.get("success"):
                if retry < VERIFY_ATTEMPTS - 1:
                    logger.warning(
                        "Verification batch %s failed (%s) -- retrying in a fresh browser",
                        label, result.get("error"),
                    )
                    time.sleep(TRANSIENT_RETRY_SECONDS)
                    continue
                logger.warning("Verification batch %s failed after %d attempts: %s",
                               label, VERIFY_ATTEMPTS, result.get("error"))
                return label, {}
            return label, {"answer": result.get("answer") or "", "tables": result.get("tables") or []}
        except google_ai_mode.ChromiumNotFoundError:
            raise
        except Exception as e:
            # A transient Selenium/browser fault ("element not interactable",
            # a stale element, a renderer crash) must not permanently lose the
            # batch. Confirmed as a real, expensive failure: one such error on
            # a single batch silently dropped 40 genuine companies (Sealed Air,
            # Klockner Pentaplast, Toray, Uflex, 3M, Avery Dennison, ...) from
            # a real run, because a batch that returns nothing has every one of
            # its companies marked not relevant. Retry in a brand-new browser
            # profile before accepting that loss.
            if retry < VERIFY_ATTEMPTS - 1:
                logger.warning(
                    "Verification batch %s errored (%s) -- retrying in a fresh browser", label, e
                )
                time.sleep(TRANSIENT_RETRY_SECONDS)
                continue
            logger.warning("Verification batch %s errored after %d attempts: %s", label, VERIFY_ATTEMPTS, e)
            return label, {}
        finally:
            shutil.rmtree(profile_dir, ignore_errors=True)
    return label, {}


def verify_and_classify_via_ai_mode(
    candidates: list[VerifiedCandidate],
    mu: MarketUnderstanding,
    cancel_event: threading.Event | None = None,
) -> list[ClassifiedCompany]:
    """Replaces DeepSeek classification entirely: batches discovered
    candidates into groups of VERIFY_BATCH_SIZE, sends each batch to
    Google AI Mode as a verification query (running batches in parallel,
    same pattern as discovery), and parses the structured table response
    back into ClassifiedCompany records. A company AI Mode doesn't
    explicitly confirm is dropped as not relevant rather than kept by
    default -- silence is treated as "could not verify", not "assume
    it's fine".

    cancel_event, if given, is checked between rounds (not mid-round, for
    the same reason as discovery -- a browser mid-query can't be safely
    interrupted). Without this, clicking Stop while verification is
    running (which can take minutes) did nothing until the whole step
    finished on its own -- confirmed as a real user-facing bug."""
    if not candidates:
        return []

    batches = [candidates[i:i + VERIFY_BATCH_SIZE] for i in range(0, len(candidates), VERIFY_BATCH_SIZE)]
    logger.info("Verifying %d candidates via Google AI Mode in %d batch(es) of up to %d",
                len(candidates), len(batches), VERIFY_BATCH_SIZE)

    all_verdicts: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=PARALLEL_VERIFY_BATCHES) as executor:
        for round_start in range(0, len(batches), PARALLEL_VERIFY_BATCHES):
            if cancel_event is not None and cancel_event.is_set():
                logger.info("Verification cancelled by user after %d/%d batch(es)", round_start, len(batches))
                break
            round_batches = batches[round_start:round_start + PARALLEL_VERIFY_BATCHES]
            futures = {}
            for i, batch in enumerate(round_batches):
                query = _build_verify_query(mu, batch)
                label = f"verify-batch-{round_start + i + 1}"
                batch_names = {c.name.lower() for c in batch}
                futures[executor.submit(_run_one_verify_batch, query, label)] = batch_names

            for future in as_completed(futures):
                batch_names = futures[future]
                label, result = future.result()
                if not result:
                    logger.warning("Batch %s returned nothing -- its %d companies will be marked not relevant",
                                    label, len(batch_names))
                    continue
                verdicts = _parse_verify_table(result.get("answer", ""), result.get("tables", []), batch_names)
                logger.info("Batch %s: parsed %d verdicts for %d companies sent", label, len(verdicts), len(batch_names))
                all_verdicts.update(verdicts)

    classified: list[ClassifiedCompany] = []
    for c in candidates:
        verdict = all_verdicts.get(c.name.lower())
        if verdict is None:
            logger.info("No verdict returned for %r -- marking not relevant (silence is not assumed fine)", c.name)
            classified.append(ClassifiedCompany(
                company_name=c.name, website=c.domain, hq_country="", operates_in_target_geography=False,
                category="Other", subcategory="", confidence=0, matched_products=[],
                reason="Google AI Mode did not return a verdict for this company during verification.",
                evidence_source=c.source, source_url=c.url or f"https://{c.domain}", is_relevant=False,
                brand_name=c.name, parent_or_independent="Independent",
            ))
            continue
        classified.append(ClassifiedCompany(
            company_name=verdict["company_name"] or c.name,
            website=c.domain,
            hq_country=verdict.get("hq_country", ""),
            operates_in_target_geography=True,
            category=verdict.get("category") or "Other",
            subcategory="",
            confidence=70 if verdict["is_relevant"] else 0,
            matched_products=[],
            reason=verdict.get("reason", ""),
            evidence_source=c.source,
            source_url=c.url or f"https://{c.domain}",
            is_relevant=bool(verdict["is_relevant"]),
            brand_name=verdict.get("brand_name") or c.name,
            parent_or_independent=verdict.get("parent_or_independent") or "Independent",
        ))

    logger.info("Google AI Mode verification produced %d classified companies (%d relevant)",
                len(classified), sum(1 for c in classified if c.is_relevant))
    return classified
