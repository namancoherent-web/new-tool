from __future__ import annotations

import re

from pipeline.models import Candidate, MarketUnderstanding

# Free-tier search engines (especially the unauthenticated Bing HTML endpoint)
# frequently return generic noise for niche B2B queries instead of real
# results -- confirmed across multiple test runs: dictionaries, semiconductor/
# industrial "wafer" homonyms, e-commerce bestseller pages, real estate
# listings (Zillow/Redfin/Realtor), recipe sites (Food.com/NDTV Food),
# delivery apps (Swiggy/Zomato), English-learning sites, law/business jargon
# glossaries, and login/account portals. These waste crawl and DeepSeek
# budget and, in bulk, can drown out the few real candidates. This module
# rejects such noise using only the search result's own name/title/domain,
# before any network crawl happens.

NOISE_DOMAIN_PATTERNS = re.compile(
    r"(dictionary|merriam-webster|wiktionary|britannica|wikipedia|computerhope|"
    r"calculator|amazon\.|flipkart|bigbasket|ebay\.|walmart\.|alibaba\.|myntra|"
    r"microsoft\.com|office\.com|office365|login\.|accounts\.google|translate\.google|"
    r"crazygames|iqiyi|xnxx|forum\.|blog\.|astrology|nationalgeographic|"
    r"github\.com|apps\.apple\.com|play\.google\.com|whatsapp\.com|outlook\.|"
    # real estate
    r"zillow|redfin|realtor\.com|trulia|movoto|homes\.com|"
    # recipe/food-media (vs actual food manufacturers)
    r"foodnetwork|food\.com|ndtv\.com|timesofindia|thehindu|cookist|"
    r"liveeatlearn|healthyfoodtribe|nutritionadvance|onlyfoods|frutopedia|"
    # food delivery apps (not manufacturers)
    r"swiggy|zomato|"
    # english/grammar learning
    r"grammarhow|grammarschooling|englishan|englishstudyonline|eslbuzz|"
    r"mrmrsenglish|writingexplained|vocabulary\.com|synonym|"
    # law/business glossary/jargon sites
    r"lawbhoomi|msrlaw|businessjargons|indiacode\.nic\.in|"
    # unrelated consumer/entertainment
    r"emojidb|comperize|countyoffice|placementstore|ultraviewer|ultrawin|"
    r"ultraplay|winni\.in|brandedgirls|globalmusicvibe|globle-game|"
    r"vanillatweaks|vanillataiwan|moneycontrol|fortuneindia|derstandard)",
    re.I,
)

GENERIC_TITLE_PATTERNS = re.compile(
    r"^(what is|definition|meaning|how to|best price|bestsellers?|top \d+|"
    r"members?,?\s*albums?)\b|"
    r"\b(songs? of all time|album|lyrics|discography|band\b|recipe|"
    r"nutrition facts|calories in)\b",
    re.I,
)

MIN_KEYWORD_SCORE = 1


def _market_keywords(mu: MarketUnderstanding) -> set[str]:
    words: set[str] = set()
    for text in (mu.market_name, mu.category_prompt):
        words.update(w.lower() for w in re.findall(r"[a-zA-Z]{4,}", text))
    for func in mu.ecosystem_functions:
        words.update(w.lower() for w in re.findall(r"[a-zA-Z]{4,}", func))
    # generic role words are too common to be useful signal on their own
    words -= {"global", "market", "company", "companies", "products", "product", "players"}
    return words


def _keyword_overlap_score(candidate: Candidate, keywords: set[str]) -> int:
    title_words = set(re.findall(r"[a-zA-Z]{4,}", candidate.name.lower()))
    return len(title_words & keywords)


def is_obvious_noise(candidate: Candidate, mu: MarketUnderstanding) -> bool:
    if NOISE_DOMAIN_PATTERNS.search(candidate.domain):
        return True
    if GENERIC_TITLE_PATTERNS.search(candidate.name.strip()):
        return True
    return False


def prefilter_candidates(
    candidates: list[Candidate], mu: MarketUnderstanding
) -> list[Candidate]:
    """Drop candidates that are obviously irrelevant noise based on domain/title
    patterns -- cheap, no network calls, runs before the expensive crawl+
    classify stages.

    Deliberately does NOT reject on missing keyword overlap: a real, well-known
    manufacturer's search result title is very often just the brand name
    ("Loacker", "Ferrero SpA") with no product/market words in it at all, so a
    hard keyword-overlap gate would systematically reject exactly the
    well-known players a market-universe search most wants to surface. Keyword
    overlap remains available via _keyword_overlap_score for future use as a
    confidence signal (e.g. in the verifier), not a filter here."""
    return [c for c in candidates if not is_obvious_noise(c, mu)]
