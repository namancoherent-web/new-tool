from __future__ import annotations

import re

from rapidfuzz import fuzz

from pipeline.models import ClassifiedCompany

LEGAL_SUFFIXES = re.compile(
    r"\b(inc\.?|incorporated|ltd\.?|limited|llc|l\.l\.c\.?|gmbh|s\.?a\.?|s\.?r\.?l\.?|"
    r"s\.?p\.?a\.?|plc|pvt\.?\s*ltd\.?|pty\.?\s*ltd\.?|co\.?|corp\.?|corporation|"
    r"b\.?v\.?|n\.?v\.?|ag|kg|oy|ab)\b\.?",
    re.I,
)

NAME_MATCH_THRESHOLD = 90


def normalize_name(name: str) -> str:
    name = LEGAL_SUFFIXES.sub("", name)
    name = re.sub(r"[^a-z0-9 ]", "", name.lower())
    return re.sub(r"\s+", " ", name).strip()


def normalize_domain(domain: str) -> str:
    return re.sub(r"^www\.", "", domain.lower().strip())


def deduplicate(companies: list[ClassifiedCompany]) -> list[ClassifiedCompany]:
    """Merge duplicates by domain first (exact), then by fuzzy name match.
    On collision, keep the higher-confidence row."""
    by_domain: dict[str, ClassifiedCompany] = {}
    no_domain: list[ClassifiedCompany] = []

    for c in companies:
        domain = normalize_domain(c.website)
        if domain:
            existing = by_domain.get(domain)
            if existing is None or c.confidence > existing.confidence:
                by_domain[domain] = c
        else:
            no_domain.append(c)

    merged = list(by_domain.values())

    final: list[ClassifiedCompany] = []
    normalized_kept: list[tuple[str, ClassifiedCompany]] = []

    for c in merged + no_domain:
        norm = normalize_name(c.company_name)
        dup_idx = None
        for i, (kept_norm, kept_c) in enumerate(normalized_kept):
            if norm and kept_norm and fuzz.token_set_ratio(norm, kept_norm) >= NAME_MATCH_THRESHOLD:
                dup_idx = i
                break
        if dup_idx is None:
            normalized_kept.append((norm, c))
            final.append(c)
        else:
            kept_norm, kept_c = normalized_kept[dup_idx]
            if c.confidence > kept_c.confidence:
                normalized_kept[dup_idx] = (kept_norm, c)
                final[final.index(kept_c)] = c

    return final
