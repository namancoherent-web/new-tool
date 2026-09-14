from __future__ import annotations

import re

from pipeline.models import ClassifiedCompany

# Canonical categories the classifier is instructed to use, each mapped to a
# set of synonym/singular/plural tokens so a free-text user prompt like
# "Parent Companies" or "manufacturer" reliably matches the DeepSeek-assigned
# category string even with minor wording differences.
CATEGORY_SYNONYMS: dict[str, list[str]] = {
    "parent company": ["parent", "holding company", "owner"],
    "manufacturer": ["manufacturer", "maker", "producer", "oem manufacturer"],
    "distributor": ["distributor", "wholesaler"],
    "supplier": ["supplier", "vendor"],
    "raw material supplier": ["raw material supplier", "material supplier"],
    "technology provider": ["technology provider", "machinery provider", "equipment provider", "software provider"],
    "oem": ["oem", "original equipment manufacturer"],
    "retailer": ["retailer", "reseller"],
    "brand": ["brand", "brand owner"],
    "investor": ["investor", "private equity", "venture capital"],
    "service provider": ["service provider", "consultancy", "consulting"],
}


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def _canonical_category(category_text: str) -> str | None:
    """Resolve free text to exactly one canonical category using whole-phrase
    equality only (never substring containment) so distinct categories that
    happen to share a word -- e.g. "Supplier" vs "Raw Material Supplier" --
    never cross-match. Longer/more specific canonical names are checked first
    so "raw material supplier" wins over the more generic "supplier"."""
    norm = _normalize(category_text)
    if not norm:
        return None

    # exact match against a canonical name or one of its listed synonyms
    for canonical, synonyms in sorted(
        CATEGORY_SYNONYMS.items(), key=lambda kv: -len(kv[0])
    ):
        if norm == canonical or norm in synonyms:
            return canonical

    # loose match for plural/singular differences, checked per-word so
    # "Parent Companies" still matches "parent" (singular, one-word synonym)
    def singularize(word: str) -> str:
        if word.endswith("ies") and len(word) > 4:
            return word[:-3] + "y"
        if word.endswith("s") and len(word) > 3:
            return word[:-1]
        return word

    norm_singular = " ".join(singularize(w) for w in norm.split())
    for canonical, synonyms in sorted(
        CATEGORY_SYNONYMS.items(), key=lambda kv: -len(kv[0])
    ):
        for phrase in [canonical, *synonyms]:
            phrase_singular = " ".join(singularize(w) for w in phrase.split())
            if norm_singular == phrase_singular:
                return canonical

    return None


def matches_category(company_category: str, user_category_prompt: str) -> bool:
    target = _canonical_category(user_category_prompt)
    company_canonical = _canonical_category(company_category)

    if target is None:
        # user typed something outside our synonym table (e.g. a bespoke
        # category) -- fall back to exact normalized text match only, never
        # substring containment, to avoid accidental cross-category matches
        return _normalize(company_category) == _normalize(user_category_prompt)

    if company_canonical is None:
        return False

    return company_canonical == target


def apply_golden_rule(
    companies: list[ClassifiedCompany], user_category_prompt: str
) -> tuple[list[ClassifiedCompany], list[ClassifiedCompany]]:
    """Hard code-level gate: only companies whose classified category matches
    the user's requested category survive, regardless of how loosely DeepSeek
    may have labeled them. Also drops anything marked not relevant or with
    is_relevant=False from the classifier. Returns (kept, dropped)."""
    kept: list[ClassifiedCompany] = []
    dropped: list[ClassifiedCompany] = []

    for c in companies:
        if not c.is_relevant:
            dropped.append(c)
            continue
        if not matches_category(c.category, user_category_prompt):
            dropped.append(c)
            continue
        kept.append(c)

    return kept, dropped
