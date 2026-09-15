from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MarketBoundary:
    in_scope: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)


@dataclass
class MarketUnderstanding:
    market_name: str
    geography: str
    category_prompt: str
    definition: str
    value_chain: list[dict] = field(default_factory=list)
    ecosystem_functions: list[str] = field(default_factory=list)
    boundary: MarketBoundary = field(default_factory=MarketBoundary)
    search_queries: list[str] = field(default_factory=list)
    brief: str = ""


@dataclass
class Candidate:
    name: str
    domain: str = ""
    url: str = ""
    source: str = ""
    discovery_query: str = ""

    def key(self) -> str:
        return (self.domain or self.name).strip().lower()


@dataclass
class EnrichedCandidate(Candidate):
    homepage_text: str = ""
    pages_crawled: list[str] = field(default_factory=list)
    crawl_ok: bool = False
    crawl_error: str = ""


@dataclass
class VerifiedCandidate(EnrichedCandidate):
    confidence: int = 0
    evidence: list[str] = field(default_factory=list)
    rejected: bool = False
    rejection_reason: str = ""


@dataclass
class ClassifiedCompany:
    company_name: str
    website: str
    hq_country: str
    operates_in_target_geography: bool
    category: str
    subcategory: str
    confidence: int
    matched_products: list[str]
    reason: str
    evidence_source: str
    source_url: str
    is_relevant: bool = True
    brand_name: str = ""
    parent_or_independent: str = "Independent"
