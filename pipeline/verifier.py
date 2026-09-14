from __future__ import annotations

import re

from config import CONFIG
from pipeline.models import EnrichedCandidate, MarketUnderstanding, VerifiedCandidate


_UNRESTRICTED_GEOGRAPHIES = {"global", "worldwide", "world", "international", ""}


def _is_unrestricted_geography(geography: str) -> bool:
    return geography.strip().lower() in _UNRESTRICTED_GEOGRAPHIES


def _geography_terms(geography: str) -> list[str]:
    terms = [geography.strip()]
    # crude but useful: "Europe" implies common EU country names appear in text too
    if geography.strip().lower() == "europe":
        terms += [
            "Germany", "France", "Italy", "Spain", "Netherlands", "Belgium",
            "Poland", "Sweden", "Austria", "Portugal", "Denmark", "Finland",
            "Ireland", "Czech", "Europe", "EU",
        ]
    return terms


def verify_candidate(candidate: EnrichedCandidate, mu: MarketUnderstanding) -> VerifiedCandidate:
    evidence: list[str] = []
    score = 0
    text = candidate.homepage_text.lower()

    if candidate.crawl_ok:
        score += 25
        evidence.append("Official website reachable with content")
    else:
        evidence.append("Website unreachable or returned no usable content")

    if _is_unrestricted_geography(mu.geography):
        # no specific geography requested -- don't penalize candidates for
        # not literally mentioning "Global"/"Worldwide" on their homepage
        score += 20
        evidence.append("No specific geography restriction requested")
    else:
        geo_terms = _geography_terms(mu.geography)
        if any(re.search(re.escape(t.lower()), text) for t in geo_terms):
            score += 20
            evidence.append(f"Geography keyword match ({mu.geography})")

    market_words = re.findall(r"[a-zA-Z]{4,}", mu.market_name.lower())
    market_hits = sum(1 for w in market_words if w in text)
    if market_hits:
        score += min(20, market_hits * 5)
        evidence.append(f"Market keyword matches: {market_hits}")

    category_words = re.findall(r"[a-zA-Z]{4,}", mu.category_prompt.lower())
    category_hits = sum(1 for w in category_words if w in text)
    if category_hits:
        score += min(15, category_hits * 5)
        evidence.append(f"Category keyword matches: {category_hits}")

    if candidate.source in ("wikidata", "wikipedia_extlink"):
        score += 10
        evidence.append(f"Listed in trusted structured source: {candidate.source}")
    elif candidate.source.startswith("directory:"):
        score += 10
        evidence.append(f"Found via industry directory: {candidate.source}")

    if len(candidate.pages_crawled) > 1:
        score += 10
        evidence.append(f"Multiple pages crawled ({len(candidate.pages_crawled)})")

    score = min(100, score)
    rejected = score < CONFIG.min_verification_confidence

    return VerifiedCandidate(
        **candidate.__dict__,
        confidence=score,
        evidence=evidence,
        rejected=rejected,
        rejection_reason="" if not rejected else f"Confidence {score} below threshold {CONFIG.min_verification_confidence}",
    )


def verify_candidates(candidates: list[EnrichedCandidate], mu: MarketUnderstanding) -> list[VerifiedCandidate]:
    return [verify_candidate(c, mu) for c in candidates]


def verify_ai_mode_candidate(candidate: EnrichedCandidate) -> VerifiedCandidate:
    """Lightweight pass-through verification for Google-AI-Mode-only
    candidates. The keyword-scoring in verify_candidate() was built for the
    multi-source crawled path -- it penalizes candidates for not literally
    repeating the target geography or market-name words in a short evidence
    blurb (e.g. an AI Mode summary for an Indian Ayurvedic brand that never
    spells out "India" in its one-line description), which isn't a
    meaningful accuracy signal for this mode and was confirmed to reject
    ~95% of real, correct candidates on a non-"Global" geography run (235
    discovered -> only 11 passed the old scorer). In AI-Mode-only mode, the
    real accuracy gate is DeepSeek classification (tightened this session
    to reject wrong-category and unverifiable companies) -- this step just
    needs to pass candidates through with a baseline confidence, not
    re-implement that judgment with keyword matching."""
    return VerifiedCandidate(
        **candidate.__dict__,
        confidence=60,
        evidence=["Passed through for DeepSeek classification (Google AI Mode discovery)"],
        rejected=False,
        rejection_reason="",
    )


def verify_ai_mode_candidates(candidates: list[EnrichedCandidate]) -> list[VerifiedCandidate]:
    return [verify_ai_mode_candidate(c) for c in candidates]
