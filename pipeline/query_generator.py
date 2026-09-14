from __future__ import annotations

from pipeline.models import MarketUnderstanding

WIDEN_SUFFIXES = [
    "list of companies",
    "directory",
    "association members",
    "trade fair exhibitors",
    "competitors",
    "manufacturers",
    "suppliers",
    "market report companies",
]


def base_queries(mu: MarketUnderstanding) -> list[str]:
    """The initial 40-60 queries produced by market understanding."""
    return list(dict.fromkeys(mu.search_queries))


def widen_queries(mu: MarketUnderstanding, round_num: int) -> list[str]:
    """Extra broader/rephrased queries for the widen loop when yield is too low."""
    base = f"{mu.market_name} {mu.geography}".strip()
    extra = [f"{base} {suffix}" for suffix in WIDEN_SUFFIXES]
    if round_num >= 2:
        extra += [f"{mu.category_prompt} {mu.market_name} {mu.geography}".strip()]
        for func in mu.ecosystem_functions:
            extra.append(f"{func} {mu.market_name} {mu.geography}".strip())
    return list(dict.fromkeys(extra))
