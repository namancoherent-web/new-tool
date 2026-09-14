from __future__ import annotations

import logging

import httpx

from config import CONFIG
from pipeline.models import Candidate
from sources.common import extract_domain

logger = logging.getLogger(__name__)

WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"

# Wikidata's SPARQL endpoint enforces a bot policy requiring a descriptive
# User-Agent with contact info; the generic browser UA used elsewhere gets a
# 403 here even though the request itself is legitimate.
WIKIDATA_USER_AGENT = (
    "MarketUniverseFinder/2.0 (company-research tool; "
    "https://github.com/) python-httpx"
)

# Companies (Q4830453 = business) whose label matches the search term,
# with an official website (P856) if available.
SPARQL_TEMPLATE = """
SELECT ?companyLabel ?website WHERE {{
  ?company wdt:P31/wdt:P279* wd:Q4830453 .
  ?company rdfs:label ?companyLabel .
  FILTER(CONTAINS(LCASE(?companyLabel), LCASE("{term}")))
  FILTER(LANG(?companyLabel) = "en")
  OPTIONAL {{ ?company wdt:P856 ?website . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT {limit}
"""


# Exact-name lookup for a single company's official website (P856), used to
# resolve a Wikipedia article title to its real site precisely -- unlike
# scraping Wikipedia's extlinks section (which mixes in news articles,
# archives, and unrelated references), Wikidata's P856 property is
# structured data curated specifically to be "the official website."
EXACT_LABEL_TEMPLATE = """
SELECT ?website WHERE {{
  ?company rdfs:label "{name}"@en .
  ?company wdt:P856 ?website .
}}
LIMIT 1
"""


async def lookup_website_by_name(client: httpx.AsyncClient, company_name: str) -> str:
    """Look up a specific company's official website (Wikidata P856) by exact
    English label match. Returns an empty string if not found."""
    query = EXACT_LABEL_TEMPLATE.format(name=company_name.replace('"', ""))
    try:
        resp = await client.get(
            WIKIDATA_SPARQL_URL,
            params={"query": query, "format": "json"},
            headers={
                "Accept": "application/sparql-results+json",
                "User-Agent": WIKIDATA_USER_AGENT,
            },
            timeout=CONFIG.http_timeout_seconds,
        )
        resp.raise_for_status()
        bindings = resp.json().get("results", {}).get("bindings", [])
    except Exception as e:
        logger.warning("Wikidata website lookup failed for %r: %s", company_name, e)
        return ""

    if not bindings:
        return ""
    return bindings[0].get("website", {}).get("value", "")


async def search(client: httpx.AsyncClient, term: str, max_results: int = 20) -> list[Candidate]:
    """Structured lookup against Wikidata for companies matching a keyword
    (e.g. a market/product term), treated as a trustworthy source."""
    query = SPARQL_TEMPLATE.format(term=term.replace('"', ""), limit=max_results)
    try:
        resp = await client.get(
            WIKIDATA_SPARQL_URL,
            params={"query": query, "format": "json"},
            headers={
                "Accept": "application/sparql-results+json",
                "User-Agent": WIKIDATA_USER_AGENT,
            },
            timeout=CONFIG.http_timeout_seconds,
        )
        resp.raise_for_status()
        bindings = resp.json().get("results", {}).get("bindings", [])
    except Exception as e:
        logger.warning("Wikidata search failed for %r: %s", term, e)
        return []

    results: list[Candidate] = []
    for b in bindings:
        name = b.get("companyLabel", {}).get("value", "")
        website = b.get("website", {}).get("value", "")
        if not name:
            continue
        results.append(
            Candidate(
                name=name,
                domain=extract_domain(website) if website else "",
                url=website,
                source="wikidata",
                discovery_query=term,
            )
        )
    return results
