from __future__ import annotations

import base64
import logging
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from config import CONFIG
from pipeline.models import Candidate
from sources.common import USER_AGENT, extract_domain, guess_company_name, is_junk_domain

logger = logging.getLogger(__name__)

BING_HTML_URL = "https://www.bing.com/search"


def _resolve_bing_redirect(url: str) -> str:
    """Bing wraps every organic result in a bing.com/ck/a? tracking redirect
    whose real destination is base64-encoded in the 'u' query parameter
    (prefixed with 'a1'). Without unwrapping this, every result domain reads
    as bing.com and gets dropped by the junk-domain filter."""
    if "bing.com/ck/a" not in url:
        return url
    try:
        qs = parse_qs(urlparse(url).query)
        encoded = qs.get("u", [""])[0]
        if encoded.startswith("a1"):
            encoded = encoded[2:]
        padded = encoded + "=" * (-len(encoded) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")
    except Exception:
        return url


async def search(client: httpx.AsyncClient, query: str, max_results: int = 15) -> list[Candidate]:
    """Search Bing's plain HTML results page (no API key required)."""
    try:
        resp = await client.get(
            BING_HTML_URL,
            params={"q": query},
            headers={"User-Agent": USER_AGENT},
            timeout=CONFIG.http_timeout_seconds,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.warning("Bing search failed for %r: %s", query, e)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    results: list[Candidate] = []
    for li in soup.select("li.b_algo")[:max_results]:
        a = li.select_one("h2 a")
        if not a:
            continue
        url = _resolve_bing_redirect(a.get("href", ""))
        title = a.get_text(strip=True)
        domain = extract_domain(url)
        if is_junk_domain(domain):
            continue
        results.append(
            Candidate(
                name=guess_company_name(title, domain),
                domain=domain,
                url=url,
                source="bing",
                discovery_query=query,
            )
        )
    return results
