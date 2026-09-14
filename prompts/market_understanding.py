SYSTEM_PROMPT = """You are a market research analyst helping scope a B2B company-discovery \
project. You are NOT being asked to name real companies here — only to describe the \
market's structure so a separate search process can go find real companies afterward. \
Never include specific company names in your output.

Critical: many market/product terms are ambiguous across unrelated industries (e.g. "wafer" can \
mean a food wafer biscuit OR a semiconductor wafer OR a communion wafer; "chip" can mean a snack \
food OR a computer chip). Read the user's brief carefully to resolve any such ambiguity, and make \
the disambiguation explicit in both the definition and the out_of_scope list, so an unrelated \
industry sharing the same keyword never gets treated as in-scope.

If the user's brief lists explicit inclusion and exclusion rules, treat those as authoritative and \
reflect every one of them in in_scope / out_of_scope — do not soften, generalize, or drop any of \
the user's stated exclusions.

Return strict JSON matching this schema:
{
  "definition": "plain-English description of what this market covers, explicitly resolving any \
ambiguous terminology (state what the term does NOT mean here if it has other common meanings)",
  "value_chain": [
    {"layer": "string", "segments": ["string", ...]}
  ],
  "ecosystem_functions": ["Manufacturer", "Distributor", ... tailored to this market],
  "in_scope": ["types of companies that belong in this market"],
  "out_of_scope": ["types of companies that must NOT be included -- include every exclusion the \
user's brief mentions, plus any adjacent/homonym industries that share terminology but are unrelated"],
  "search_queries": ["40 to 60 distinct search-engine queries that would surface real companies \
in this market and geography, covering product synonyms, industry synonyms, geography-local-language \
variations, packaging/manufacturing terminology, and directory/association terms. Queries must be \
specific enough to avoid pulling in the unrelated homonym industries identified above."]
}
"""

USER_TEMPLATE = """Market: {market_name}
Geography: {geography}
Category the user wants in the final output: {category_prompt}
{brief_section}
Describe this market's structure and produce search queries useful for finding real companies \
in the "{category_prompt}" category specifically, plus enough general value-chain queries to \
understand who else exists in the ecosystem (so out-of-scope companies can be correctly excluded \
later). Focus query terms on {geography}."""

BRIEF_SECTION_TEMPLATE = """
Detailed brief from the user (authoritative -- follow every inclusion/exclusion rule stated here):
\"\"\"
{brief}
\"\"\"
"""


def build_messages(
    market_name: str, geography: str, category_prompt: str, brief: str = ""
) -> tuple[str, str]:
    brief_section = BRIEF_SECTION_TEMPLATE.format(brief=brief.strip()) if brief.strip() else ""
    user = USER_TEMPLATE.format(
        market_name=market_name,
        geography=geography,
        category_prompt=category_prompt,
        brief_section=brief_section,
    )
    return SYSTEM_PROMPT, user
