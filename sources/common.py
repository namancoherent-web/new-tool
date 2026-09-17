from __future__ import annotations

import re
from urllib.parse import urlparse

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

JUNK_DOMAINS = {
    "wikipedia.org", "linkedin.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "pinterest.com", "reddit.com",
    "crunchbase.com", "bloomberg.com", "yellowpages.com", "yelp.com",
    "indeed.com", "glassdoor.com", "medium.com", "amazon.com", "ebay.com",
    "alibaba.com", "google.com", "bing.com", "duckduckgo.com",
}


# A real domain has at least one dot and only domain-legal characters. AI
# Mode occasionally answers a website field with garbage/placeholder text
# ("N/A", "unknown", "none", "-", ":") instead of leaving it empty, and
# without this check each of those passed straight through as if it were a
# genuine domain -- confirmed directly: a real export contained "Timac Agro
# Perú" with a website of literally ":".
_VALID_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def extract_domain(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
        domain = re.sub(r"^www\.", "", netloc)
    except Exception:
        return ""
    return domain if _VALID_DOMAIN.match(domain) else ""


def is_junk_domain(domain: str) -> bool:
    if not domain:
        return True
    return any(domain == j or domain.endswith("." + j) for j in JUNK_DOMAINS)


def guess_company_name(title: str, domain: str) -> str:
    title = re.sub(r"\s*[\|\-–—:]\s*(Home|Official Site|Homepage).*$", "", title, flags=re.I).strip()
    if title:
        return title.split(" | ")[0].split(" - ")[0].strip()
    return domain
