from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
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
VERIFY_BATCH_SIZE = 40
PARALLEL_VERIFY_BATCHES = 6

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
        f"them not relevant. Do not add companies that were not in the list."
    )


_TABLE_ROW_PATTERN = re.compile(r"^\|(.+)\|$", re.MULTILINE)


def _parse_verify_table(answer_text: str, tables: list[str], batch_names: set[str]) -> dict[str, dict]:
    """Parse AI Mode's verification response into {company_name_lower: {...}}.
    Tries markdown tables first (most reliable when AI Mode cooperates),
    falls back to a looser per-line parse of the flattened prose if no
    table is present -- mirroring the same tolerance discovery parsing
    needed for AI Mode's inconsistent response shapes."""
    results: dict[str, dict] = {}

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
            key = name.lower()
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


def _run_one_verify_batch(query: str, label: str) -> tuple[str, dict]:
    """Run a single verification query in its own throwaway Chromium
    profile so batches can run concurrently without colliding on a shared
    --user-data-dir lock file, same pattern as discovery attempts."""
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
        if not result.get("success"):
            logger.warning("Verification batch %s failed: %s", label, result.get("error"))
            return label, {}
        return label, {"answer": result.get("answer") or "", "tables": result.get("tables") or []}
    except google_ai_mode.ChromiumNotFoundError:
        raise
    except Exception as e:
        logger.warning("Verification batch %s errored: %s", label, e)
        return label, {}
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


def verify_and_classify_via_ai_mode(
    candidates: list[VerifiedCandidate], mu: MarketUnderstanding
) -> list[ClassifiedCompany]:
    """Replaces DeepSeek classification entirely: batches discovered
    candidates into groups of VERIFY_BATCH_SIZE, sends each batch to
    Google AI Mode as a verification query (running batches in parallel,
    same pattern as discovery), and parses the structured table response
    back into ClassifiedCompany records. A company AI Mode doesn't
    explicitly confirm is dropped as not relevant rather than kept by
    default -- silence is treated as "could not verify", not "assume
    it's fine"."""
    if not candidates:
        return []

    batches = [candidates[i:i + VERIFY_BATCH_SIZE] for i in range(0, len(candidates), VERIFY_BATCH_SIZE)]
    logger.info("Verifying %d candidates via Google AI Mode in %d batch(es) of up to %d",
                len(candidates), len(batches), VERIFY_BATCH_SIZE)

    all_verdicts: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=PARALLEL_VERIFY_BATCHES) as executor:
        for round_start in range(0, len(batches), PARALLEL_VERIFY_BATCHES):
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
