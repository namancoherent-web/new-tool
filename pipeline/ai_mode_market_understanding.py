from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from config import CONFIG
from pipeline.models import MarketBoundary, MarketUnderstanding
from sources import google_ai_mode

logger = logging.getLogger(__name__)

MAX_SEARCH_QUERIES = 70

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_UI_NOISE = re.compile(
    r"\b(?:Use code with caution\.?|Content may be inaccurate\.?|"
    r"Was this helpful\??|Show (?:more|less)|Copy code)\s*",
    re.IGNORECASE,
)


def _build_query(market_name: str, geography: str, category_prompt: str, brief: str) -> str:
    scope = brief.strip() if brief.strip() else (
        f"Market: {market_name}. Geography: {geography}."
        + (f" Focus on companies that are: {category_prompt}." if category_prompt.strip() else "")
    )
    return (
        "You are scoping a market research project. Read the market description below and "
        "define the market precisely, so that a later search can find the right companies.\n\n"
        f'Market description:\n"""\n{scope}\n"""\n\n'
        f"Market name: {market_name}\nGeography: {geography}\n\n"
        "Return a single JSON object with exactly these keys:\n"
        '  "definition": one paragraph defining precisely what this market covers.\n'
        '  "in_scope": array of strings -- the kinds of companies that belong in this market.\n'
        '  "out_of_scope": array of strings -- the kinds of companies that must be excluded '
        "because they are adjacent, upstream, downstream or only loosely related.\n"
        '  "ecosystem_functions": array of strings -- the roles companies play in this market '
        "(e.g. manufacturer, brand owner, supplier, distributor).\n"
        '  "search_queries": array of 30-60 short search queries that together would surface the '
        "widest possible set of real companies in this market, covering different regions, "
        "company sizes and roles.\n\n"
        "Output only the JSON object, with no commentary before or after it."
    )


def _parse(answer_text: str) -> dict:
    """Read the JSON object out of the answer. Matches discovery's approach:
    the scraper reads rendered DOM text, where code fences do not survive and
    Google injects helper phrases into code blocks."""
    cleaned = _UI_NOISE.sub("", answer_text)
    match = _JSON_OBJECT.search(cleaned)
    if not match:
        return {}
    blob = match.group(0)
    try:
        data = json.loads(blob)
    except ValueError:
        # Salvage the individual fields that matter most rather than losing
        # the whole object to one malformed spot.
        data = {}
        qs = re.findall(r'"([^"]{8,120})"', blob)
        if qs:
            data["search_queries"] = qs
    return data if isinstance(data, dict) else {}


def understand_market_via_ai_mode(
    market_name: str, geography: str, category_prompt: str, brief: str = ""
) -> MarketUnderstanding:
    """Define the market using Google AI Mode instead of a paid LLM, so the
    whole pipeline runs on a single source. Falls back to a minimal
    understanding built from the user's own text if AI Mode returns nothing --
    discovery sends the user's brief verbatim anyway, so a thin understanding
    degrades quality slightly rather than breaking the run."""
    query = _build_query(market_name, geography, category_prompt, brief)
    profile_dir = str(Path(tempfile.gettempdir()) / f"ai_mode_scope_{uuid.uuid4().hex[:8]}")
    data: dict = {}
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
        if result.get("success"):
            data = _parse(result.get("answer") or "")
    except google_ai_mode.ChromiumNotFoundError:
        raise
    except Exception as e:
        logger.warning("Market understanding via AI Mode failed: %s", e)
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)

    queries = [q for q in data.get("search_queries", []) if isinstance(q, str) and q.strip()]
    queries = list(dict.fromkeys(queries))[:MAX_SEARCH_QUERIES]

    if not data:
        logger.warning(
            "AI Mode returned no usable market definition for %r -- continuing with the user's "
            "own description, which discovery sends verbatim regardless.",
            market_name,
        )
    else:
        logger.info(
            "Market understanding via AI Mode: %d in-scope rules, %d out-of-scope rules, %d queries",
            len(data.get("in_scope") or []), len(data.get("out_of_scope") or []), len(queries),
        )

    def _strs(key: str) -> list[str]:
        return [s for s in data.get(key, []) if isinstance(s, str) and s.strip()]

    return MarketUnderstanding(
        market_name=market_name,
        geography=geography,
        category_prompt=category_prompt,
        definition=str(data.get("definition") or "").strip(),
        value_chain=[],
        ecosystem_functions=_strs("ecosystem_functions"),
        boundary=MarketBoundary(in_scope=_strs("in_scope"), out_of_scope=_strs("out_of_scope")),
        search_queries=queries,
        brief=brief,
    )
