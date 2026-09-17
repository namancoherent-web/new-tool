from __future__ import annotations

import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from sources.common import extract_domain, is_junk_domain

logger = logging.getLogger(__name__)

# google_ai_scraper.py lives at the project root (market-universe-finder/),
# alongside start.bat, so it ships with the repo instead of depending on a
# path outside it.
_SCRAPER_ROOT = Path(__file__).resolve().parent.parent
if str(_SCRAPER_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRAPER_ROOT))

_DOMAIN_PATTERN = re.compile(
    r"\b([a-z0-9][a-z0-9-]*\.(?:com|co|net|org|in|io|de|fr|it|uk|eu|biz)[a-z0-9./-]*)\b", re.I
)
_GENERIC_LINE_WORDS = {
    "the", "these", "if", "would", "you", "let", "please", "note", "here",
    "based", "focus", "region", "regions", "category", "categories",
}

# AI Mode's UI renders small inline citation-source chips (e.g. "IndiaMART",
# "Justdial", "Mordor Intelligence", "Biscuit people") right before the next
# company entry with no separating punctuation, so the flattened text can
# read "...supply. Biscuit people Bakewell Biscuits Pvt. Ltd. (bakewell...".
# These known source names are stripped from the front of a matched name
# rather than causing the whole entry to be dropped.
_KNOWN_CITATION_CHIP_PREFIXES = (
    "indiamart", "justdial", "mordor intelligence", "biscuit people",
    "keychain.com", "grand view research", "spherical insights",
    "tasteatlas", "bakemate",
)


def _strip_citation_chip_prefix(name: str) -> str:
    lowered = name.lower()
    for chip in _KNOWN_CITATION_CHIP_PREFIXES:
        if lowered.startswith(chip + " "):
            return name[len(chip):].strip()
    return name


@dataclass
class AiModeCompanyMention:
    """One company mention pulled from an AI Mode answer -- name plus
    whatever extra context AI Mode gave (country, products, website). None of
    this is trusted as final fact; it's passed through to the pipeline's own
    crawl -> verify -> classify stages as extra context, and the website (if
    given) is used as the crawl target instead of re-deriving it."""

    name: str
    hq_country: str = ""
    products: str = ""
    domain: str = ""


def _find_col(header: list[str], *keywords: str) -> int | None:
    for i, h in enumerate(header):
        if any(k in h for k in keywords):
            return i
    return None


def _mentions_from_table(table_md: str) -> list[AiModeCompanyMention]:
    """Pull full rows (name, country, products, website) out of a markdown
    table, as produced by GoogleAIModeScraper's table extraction."""
    lines = [l.strip() for l in table_md.splitlines() if l.strip()]
    if len(lines) < 3:
        return []

    header = [h.strip().lower() for h in lines[0].strip("|").split("|")]
    name_idx = _find_col(header, "company", "name")
    country_idx = _find_col(header, "country", "headquarter", "hq", "region")
    products_idx = _find_col(header, "product", "brand")
    website_idx = _find_col(header, "website", "domain")
    if name_idx is None:
        name_idx = 0

    mentions: list[AiModeCompanyMention] = []
    for row_line in lines[2:]:
        cells = [c.strip() for c in row_line.strip("|").split("|")]
        if len(cells) <= name_idx:
            continue
        name = re.sub(r"\*\*|\[|\]\([^)]*\)", "", cells[name_idx]).strip()
        if not name or name.lower() in {"-", "n/a"}:
            continue

        domain = ""
        if website_idx is not None and website_idx < len(cells):
            match = _DOMAIN_PATTERN.search(cells[website_idx])
            if match:
                candidate_domain = extract_domain("https://" + match.group(1))
                if candidate_domain and not is_junk_domain(candidate_domain):
                    domain = candidate_domain

        mentions.append(
            AiModeCompanyMention(
                name=name,
                hq_country=cells[country_idx].strip() if country_idx is not None and country_idx < len(cells) else "",
                products=cells[products_idx].strip() if products_idx is not None and products_idx < len(cells) else "",
                domain=domain,
            )
        )
    return mentions


# Tail of a previous entry that can precede a real company name once entries
# run together with no line breaks: a bare domain ("amcor.com",
# "toppan.co.jp"), a lone TLD fragment left by a split ("com", "co.jp", "ag"),
# or a finished sentence. Anchored to the FRONT of a captured name so only
# leading noise is removed -- a period inside the name itself ("H.B. Fuller",
# "Crocco S.p.A.") is never touched.
# Any TLD-shaped token is stripped rather than an explicit country list --
# a fixed list inevitably misses one (".ae" was missing and left
# "adnoc.ae Sasol Limited" as a company name), and this must work for every
# market/geography a user asks about, not just the ones seen so far.
_LEADING_DOMAIN_OR_TLD = re.compile(
    r"^(?:[a-z0-9][a-z0-9-]*\.)?[a-z]{2,6}(?:\.[a-z]{2,3})*\b[\s.]*(?=[A-Z0-9])",
)


# Section headings AI Mode emits inline ("North American Manufacturers &
# Suppliers Amcor plc ..."), which glue onto the first company name of the
# section when the answer has no line breaks. Confirmed in a real run's
# candidate names.
_SECTION_HEADING_PREFIX = re.compile(
    r"^(?:(?:North|South|Latin|Central)\s+America(?:n)?|Asia[- ]Pacific|APAC|EMEA|"
    r"Europe(?:an)?|Middle\s+East(?:\s*(?:&|and)\s*Africa)?|Africa(?:n)?|Global|"
    r"Key|Major|Leading|Other|Additional|Top)\b[^.]{0,60}?"
    r"(?:Manufacturers?|Suppliers?|Producers?|Players?|Companies|Brands?|Vendors?|"
    r"Distributors?|Converters?)\b(?:\s*(?:&|and)\s*(?:Manufacturers?|Suppliers?|"
    r"Producers?|Players?|Companies|Brands?|Vendors?|Distributors?|Converters?)\b)*[\s:—–-]*",
    re.IGNORECASE,
)


# A bare region/segment heading directly in front of a company name, with no
# "Manufacturers/Suppliers" word after it ("Asia-Pacific Toppan", "Latin
# America Vitopel", "Middle East & Africa Taghleef Industries LLC",
# "Specialty & Technical Film Segments 3M Company"). Confirmed in a real run.
# Requires a capitalised company token to follow, so a company whose real name
# genuinely starts with a region word is not truncated to nothing.
_BARE_REGION_PREFIX = re.compile(
    r"^(?:Asia[- ]Pacific|Latin\s+America|North\s+America|South\s+America|"
    r"Middle\s+East(?:\s*(?:&|and)\s*Africa)?|Europe|EMEA|APAC|"
    r"Specialty\s*(?:&|and)\s*Technical\s+Film\s+Segments?)\s+(?=[A-Z0-9])",
    re.IGNORECASE,
)

# A name that is only a corporate suffix or a bare generic word is a leftover
# fragment, never a company ("Inc.", "Group", "p.A.", "r.l.", "LLC").
_FRAGMENT_ONLY_NAME = re.compile(
    r"^(?:inc|ltd|llc|plc|corp|co|group|holdings?|limited|gmbh|ag|sa|nv|bv|"
    r"p\.?a|r\.?l|s\.?p\.?a|s\.?a\.?r\.?l|kg|kgaa|oyj|ab|as|pte|pvt|berhad|tbk)\.?$",
    re.IGNORECASE,
)


def is_fragment_only_name(name: str) -> bool:
    return bool(_FRAGMENT_ONLY_NAME.match(name.strip()))


def _trim_leading_noise(name: str) -> str:
    """Strip a previous entry's trailing fragment off the front of a captured
    name. Runs repeatedly because a split can leave more than one layer (e.g.
    a domain followed by a sentence tail). Only ever removes from the start,
    so real internal punctuation is preserved."""
    prev = None
    while prev != name:
        prev = name
        # A completed sentence before the name: keep only what follows the
        # last ". " that is NOT part of an initial/abbreviation. Checked
        # first so "Leading supplier. Berry Global Inc." -> "Berry Global Inc."
        # Never split on a period that ends a common corporate abbreviation
        # ("Co.", "Inc.", "Ltd.", "S.p.A.", "H.B.") -- those are inside a real
        # name, not a sentence end. Without this, "Henkel AG & Co. KGaA" got
        # truncated to just "KGaA".
        sentence_split = re.split(
            r"(?<![A-Z])(?<!\.[a-z])(?<!\.[a-z][a-z])(?<!\.[a-z][a-z][a-z])"
            r"(?<!\bCo)(?<!\bInc)(?<!\bLtd)(?<!\bCorp)(?<!\bPvt)(?<!\bPte)(?<!\bBros)"
            r"(?<!\bSt)(?<!\bMfg)(?<!\bIndus)(?<!\bPlc)(?<!\bGmbH)(?<!\bSA)(?<!\bAG)"
            # Personal/professional titles that legitimately start a company
            # name ("Dr. Reddy's Laboratories", "St. Jude Medical") -- without
            # these the title was read as a sentence end and the name was
            # truncated. Applies to any market, not just the one being tested.
            r"(?<!\bDr)(?<!\bMr)(?<!\bMrs)(?<!\bMs)(?<!\bProf)(?<!\bSte)(?<!\bMt)"
            r"(?<!\bNo)(?<!\bJr)(?<!\bSr)(?<!\bMessrs)"
            r"\.\s+",
            name,
        )
        if len(sentence_split) > 1 and sentence_split[-1].strip():
            name = sentence_split[-1].strip()
        name = _SECTION_HEADING_PREFIX.sub("", name).strip()
        name = _BARE_REGION_PREFIX.sub("", name).strip()
        name = _LEADING_DOMAIN_OR_TLD.sub("", name).strip()
        # A leading ALL-CAPS/short technical token followed by a period is a
        # description tail ("BOPP. Polyplex Corporation"), not part of the
        # name -- the abbreviation guard above deliberately protects periods,
        # so this handles the leading-position case explicitly.
        name = re.sub(r"^[A-Z0-9]{2,6}\.\s+(?=[A-Z])", "", name).strip()
        # A lowercase-leading token is never the start of a real company
        # name here -- it is the tail of the previous description.
        name = re.sub(r"^(?:[a-z][A-Za-z0-9&'’-]*\s+)+(?=[A-Z0-9])", "", name).strip()
    return name


def _mentions_from_text(answer_text: str) -> list[AiModeCompanyMention]:
    """Fallback for prose answers. AI Mode's real output uses the shape
    "Company Name (Country) – description." or, since the discovery prompt
    now explicitly asks for website domains, "Company Name (domain.com) –
    description." (confirmed against real captured responses -- both shapes
    occur, sometimes even within the same answer). The actual DOM extraction
    frequently collapses all line breaks so the entire multi-thousand-
    character answer arrives as ONE continuous string (no newlines between
    entries at all). A pattern anchored on end-of-line ($/MULTILINE) is
    therefore unusable: with no newlines, "description.$" greedily matches
    to the end of the whole string, swallowing every subsequent company
    entry into the first match's description and finding nothing else.

    Instead, each entry's description is bounded by lookahead to the START
    of the next "Name (...) –" occurrence (or end of string for the last
    one), which works regardless of whether newlines are present."""
    mentions: list[AiModeCompanyMention] = []

    # Name (Country|domain) <dash> -- the anchor marking the start of each
    # entry. The name must start right after a sentence boundary (period+
    # space, newline, or start-of-text) rather than any arbitrary space --
    # without this, a capitalized word in the middle of the PREVIOUS entry's
    # description (e.g. "...Kinder Bueno. Bahlsen GmbH & Co. KG (Germany)")
    # can get swept into the name of the next entry, since there's no
    # newline to mark where one entry ends and the next begins.
    # The boundary before a name must be a REAL sentence break, not just any
    # period. Matching on a bare "." (the previous version) let a match start
    # in the middle of a domain or a legal suffix, because those contain
    # periods too: "...packaging. amcor.com Berry Global Inc. (USA) - ..."
    # resumed right after "amcor." and captured the name as "com Berry Global
    # Inc.", and "Henkel AG & Co. KGaA (Germany)" captured just "KGaA".
    # Confirmed as the true source of the corrupted candidate names seen in a
    # real run ("com Berry Global Inc.", "co.jp Toppan Holdings Inc.",
    # "ag Crocco S.p.A.", "B. Fuller", "p.A.", "SAOG") -- every one of those
    # candidates then failed to match its verdict during verification and was
    # silently dropped as not relevant, which is what collapsed a 200-company
    # run down to 17.
    #
    # A genuine break is a period followed by whitespace, where the period is
    # NOT preceded by a single letter (initials like "H.B.", "S.p.A.") and NOT
    # part of a domain (a TLD-looking token). Newline and start-of-text stay
    # unconditional boundaries.
    # Rather than trying to detect the boundary with a lookbehind on "." (the
    # previous approach), the name is anchored by what FOLLOWS it -- the
    # "(Country|domain) <dash>" structure -- and the name itself is taken as
    # the trailing run of name-like tokens immediately before that paren.
    # Anchoring on a bare "." boundary was the true source of the corrupted
    # candidate names seen in a real run: periods occur inside domains and
    # legal suffixes too, so "...packaging. amcor.com Berry Global Inc.
    # (USA) - ..." resumed matching right after "amcor." and produced the
    # name "com Berry Global Inc."; "Henkel AG & Co. KGaA (Germany)" produced
    # just "KGaA". Every such candidate then failed to match its verdict
    # during verification and was silently dropped as not relevant, which is
    # what collapsed a 200-company run down to 17 final companies.
    #
    # _trim_leading_noise below then strips any leftover tail of the previous
    # entry (a domain fragment or a finished sentence) off the front of the
    # captured name, which a single regex cannot reliably do on its own.
    entry_start = re.compile(
        r"(?P<name>[A-Za-z0-9][A-Za-z0-9&.,'’À-ÿ\-]*(?:\s+[A-Za-z0-9&.,'’À-ÿ\-]+){0,10})"
        r"\*{0,2}\s*"
        r"\((?P<paren>[A-Za-z0-9][A-Za-z0-9 /.&'’-]{1,60})\)"
        r"\s*[-–—:]\s*",
        re.MULTILINE,
    )
    starts = list(entry_start.finditer(answer_text))
    for i, m in enumerate(starts):
        name = _trim_leading_noise(_strip_citation_chip_prefix(m.group("name").strip()))

        # AI Mode's UI embeds inline citation chips (e.g. "Keychain.com",
        # "Biscuit people", source-name badges) directly in the flattened
        # text with no separating punctuation, which can get swept into a
        # name match. A real company name never contains a bare domain
        # substring, so this is a reliable tell that the match is noise
        # bleeding in from a citation chip rather than a truncation to fix.
        if _DOMAIN_PATTERN.search(name):
            continue

        # A stray "X." abbreviation fragment (e.g. "A." left over from a
        # sentence-boundary false-positive inside "Loacker S.p.A.") is not
        # a usable company name on its own.
        words = [w.lower() for w in name.split()]
        # A leading article is ignored when testing for generic prose words --
        # plenty of real companies legitimately begin with "The" ("The Goldman
        # Sachs Group, Inc.", "The Dow Chemical Company", "The Coca-Cola
        # Company") and were being dropped entirely by this filter.
        meaningful_words = words[1:] if words[:1] == ["the"] else words
        if len(name) < 2 or len(name) > 80 or any(w in _GENERIC_LINE_WORDS for w in meaningful_words):
            continue
        if len(name) <= 3 and name.rstrip(".").isalpha():
            continue
        if is_fragment_only_name(name):
            continue
        # An all-lowercase match is a fragment of a domain or description
        # ("coca" out of "coca-colacompany.com"), never an extracted
        # company name -- AI Mode always capitalises real company names.
        if name.islower():
            continue

        # description runs from the end of this match to the start of the
        # next entry (or a hard cap if entries are sparse/end-of-string)
        desc_end = starts[i + 1].start() if i + 1 < len(starts) else min(len(answer_text), m.end() + 300)
        rest = answer_text[m.end():desc_end]

        # The parenthetical after the name is either a domain (the prompt
        # now asks AI Mode for one inline) or a country name -- never both
        # in the same spot. Route it correctly instead of assuming country.
        paren = m.group("paren").strip()
        domain = ""
        hq_country = ""
        paren_domain_match = _DOMAIN_PATTERN.fullmatch(paren)
        if paren_domain_match:
            candidate_domain = extract_domain("https://" + paren)
            if candidate_domain and not is_junk_domain(candidate_domain):
                domain = candidate_domain
        else:
            hq_country = paren

        # If the parenthetical was a domain (or no domain was found there),
        # still check the description text for one -- and always check the
        # description for a domain if we don't have one yet, since AI Mode
        # sometimes puts the site mid-sentence instead of in the parens.
        if not domain:
            dmatch = _DOMAIN_PATTERN.search(rest)
            if dmatch:
                candidate_domain = extract_domain("https://" + dmatch.group(1))
                if candidate_domain and not is_junk_domain(candidate_domain):
                    domain = candidate_domain

        mentions.append(
            AiModeCompanyMention(
                name=name,
                hq_country=hq_country,
                products=rest.strip()[:250],
                domain=domain,
            )
        )

    return mentions


_TUPLE_LIST_ENTRY = re.compile(
    r'\(\s*"\s*(?P<name>[^"]+?)\s*"\s*,\s*"\s*(?P<domain>[^"]*?)\s*"\s*,\s*"\s*(?P<country>[^"]*?)\s*"\s*\)'
)

_DASH_DOMAIN_ENTRY = re.compile(
    r"(?:^|(?<=[.\n])\s*)(?:\d+[\.\)]|[-*•]\s*)?\*{0,2}"
    r"(?P<name>[A-Z][A-Za-z0-9&.,'’À-ÿ\-]+(?:\s+[A-Za-z0-9&.,'’À-ÿ\-]+){0,6}?)"
    r"\*{0,2}\s*[-–—]\s*"
    r"(?P<domain>[a-z0-9][a-z0-9-]*\.(?:com|co|net|org|in|io|de|fr|it|uk|eu|biz|tw|jp|kr|cz|sk|hr|rs|hu|ro|md|ua|kz|lv|lt|se|no|fi|es|pt|ph|th|my|sg|hk|id)[a-z0-9./-]*)"
    r"\s*(?:\((?P<country>[A-Za-z][A-Za-z /.&'’-]{1,40})\))?",
    re.MULTILINE | re.IGNORECASE,
)


def _mentions_from_tuple_list(answer_text: str) -> list[AiModeCompanyMention]:
    """AI Mode sometimes answers a big list request by writing (and showing)
    a Python snippet like players = [("Loacker", "loacker.com", "Italy"), ...]
    instead of prose -- confirmed against a real captured response where this
    was actually the MOST complete, well-structured part of the answer (250+
    entries) while the prose portion above it used a format the other
    parsers don't handle either. This is the most reliable shape to parse
    when present since it's already structured, so it's tried first."""
    mentions: list[AiModeCompanyMention] = []
    for m in _TUPLE_LIST_ENTRY.finditer(answer_text):
        name = m.group("name").strip()
        if len(name) < 2 or len(name) > 100:
            continue
        domain_raw = m.group("domain").strip()
        domain = ""
        if domain_raw:
            candidate_domain = extract_domain("https://" + domain_raw)
            if candidate_domain and not is_junk_domain(candidate_domain):
                domain = candidate_domain
        mentions.append(
            AiModeCompanyMention(
                name=name,
                hq_country=m.group("country").strip(),
                products="",
                domain=domain,
            )
        )
    return mentions


def _mentions_from_dash_domain_text(answer_text: str) -> list[AiModeCompanyMention]:
    """Handles the prose shape "Company Name — domain.com (Country)" (domain
    BEFORE the country parenthetical, separated by a dash, no trailing dash/
    colon) -- confirmed as a real AI Mode response shape distinct from the
    "Name (Country) – description" shape _mentions_from_text targets. The
    two formats are structurally different enough (domain position, no
    trailing separator) that one regex can't cleanly cover both without
    false-matching the other, so this runs as a separate pass."""
    mentions: list[AiModeCompanyMention] = []
    for m in _DASH_DOMAIN_ENTRY.finditer(answer_text):
        name = _trim_leading_noise(_strip_citation_chip_prefix(m.group("name").strip()))
        if _DOMAIN_PATTERN.search(name):
            continue
        words = [w.lower() for w in name.split()]
        # A leading article is ignored when testing for generic prose words --
        # plenty of real companies legitimately begin with "The" ("The Goldman
        # Sachs Group, Inc.", "The Dow Chemical Company", "The Coca-Cola
        # Company") and were being dropped entirely by this filter.
        meaningful_words = words[1:] if words[:1] == ["the"] else words
        if len(name) < 2 or len(name) > 80 or any(w in _GENERIC_LINE_WORDS for w in meaningful_words):
            continue
        if len(name) <= 3 and name.rstrip(".").isalpha():
            continue
        if is_fragment_only_name(name):
            continue
        # An all-lowercase match is a fragment of a domain or description
        # ("coca" out of "coca-colacompany.com"), never an extracted
        # company name -- AI Mode always capitalises real company names.
        if name.islower():
            continue

        domain_raw = m.group("domain").strip()
        domain = ""
        candidate_domain = extract_domain("https://" + domain_raw)
        if candidate_domain and not is_junk_domain(candidate_domain):
            domain = candidate_domain

        mentions.append(
            AiModeCompanyMention(
                name=name,
                hq_country=(m.group("country") or "").strip(),
                products="",
                domain=domain,
            )
        )
    return mentions


_BARE_DOMAIN_PAREN_ENTRY = re.compile(
    r"(?:^|(?<=[.\n)])\s*)(?:\d+[\.\)]|[-*•]\s*)?\*{0,2}"
    r"(?P<name>[A-Z][A-Za-z0-9&.,'’À-ÿ\-]+(?:\s+[A-Za-z0-9&.,'’À-ÿ\-]+){0,6}?)"
    r"\*{0,2}\s*"
    r"\(\s*(?P<domain>[a-z0-9][a-z0-9-]*\.(?:com|co|net|org|in|io|de|fr|it|uk|eu|biz|tw|jp|kr|cz|sk|hr|rs|hu|ro|md|ua|kz|lv|lt|se|no|fi|es|pt|ph|th|my|sg|hk|id|biz|ru)[a-z0-9./-]*)\s*\)",
    re.MULTILINE | re.IGNORECASE,
)


def _mentions_from_bare_domain_paren(answer_text: str) -> list[AiModeCompanyMention]:
    """Handles the prose shape "Company Name (domain.com)" with NO trailing
    separator at all -- entries run directly into each other with just a
    space, e.g. "Dabur India Limited (dabur.com) Shree Baidyanath Ayurved
    Bhawan Pvt. Ltd. (baidyanath.co.in) Patanjali Ayurved Limited
    (patanjaliayurved.net)" -- confirmed as a real AI Mode response shape
    (seen when the flattened answer has no line breaks AND no dash/colon
    after the domain parenthetical, unlike every other shape handled
    above). The only usable anchor is the closing paren itself, since
    there's no other boundary marker between one entry and the next."""
    mentions: list[AiModeCompanyMention] = []
    for m in _BARE_DOMAIN_PAREN_ENTRY.finditer(answer_text):
        name = _trim_leading_noise(_strip_citation_chip_prefix(m.group("name").strip()))
        if _DOMAIN_PATTERN.search(name):
            continue
        words = [w.lower() for w in name.split()]
        # A leading article is ignored when testing for generic prose words --
        # plenty of real companies legitimately begin with "The" ("The Goldman
        # Sachs Group, Inc.", "The Dow Chemical Company", "The Coca-Cola
        # Company") and were being dropped entirely by this filter.
        meaningful_words = words[1:] if words[:1] == ["the"] else words
        if len(name) < 2 or len(name) > 80 or any(w in _GENERIC_LINE_WORDS for w in meaningful_words):
            continue
        if len(name) <= 3 and name.rstrip(".").isalpha():
            continue
        if is_fragment_only_name(name):
            continue
        # An all-lowercase match is a fragment of a domain or description
        # ("coca" out of "coca-colacompany.com"), never an extracted
        # company name -- AI Mode always capitalises real company names.
        if name.islower():
            continue

        domain_raw = m.group("domain").strip()
        domain = ""
        candidate_domain = extract_domain("https://" + domain_raw)
        if candidate_domain and not is_junk_domain(candidate_domain):
            domain = candidate_domain

        mentions.append(
            AiModeCompanyMention(name=name, hq_country="", products="", domain=domain)
        )
    return mentions


# Individual JSON objects, found directly in the answer text. Deliberately
# NOT anchored on a ```json fence: the scraper reads rendered DOM text, and
# the fence markers do not survive that (the only ``` in a captured answer
# was our own prompt echoed back by the page). Nor is the whole array parsed
# as one unit -- Google's UI injects helper text such as "Use code with
# caution." into the middle of the code block, which makes json.loads fail
# for the entire array. Confirmed on a real 60k-char answer: it contained
# 307 companies, one injected phrase broke the array parse, and every single
# company was lost. Reading object-by-object salvages 303 of those 307.
_JSON_OBJECT = re.compile(r"\{[^{}]*\}", re.DOTALL)

# UI/helper phrases Google injects into rendered code blocks, stripped before
# an object is parsed so they cannot corrupt the fields around them.
_UI_NOISE = re.compile(
    r"\b(?:Use code with caution\.?|Content may be inaccurate\.?|"
    r"Was this helpful\??|Show (?:more|less)|Copy code)\s*",
    re.IGNORECASE,
)


def _mentions_from_json(answer_text: str) -> list[AiModeCompanyMention]:
    """Read the structured JSON array the discovery prompt asks for.

    This is the preferred path: unlike every prose parser below it, there is
    no entry boundary to infer, so a name can never be corrupted by a period
    inside a domain ("amcor.com"), a corporate suffix ("Henkel AG & Co.
    KGaA") or a title ("Dr. Reddy's Laboratories") -- the exact failure that
    produced candidate names like "com Berry Global Inc." and silently lost
    them during verification. AI Mode does not always honour the format, so
    the prose parsers remain as a fallback."""
    cleaned = _UI_NOISE.sub("", answer_text)

    rows: list[dict] = []
    for match in _JSON_OBJECT.finditer(cleaned):
        chunk = match.group(0)
        if '"name"' not in chunk:
            continue
        try:
            row = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)

    mentions: list[AiModeCompanyMention] = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        # AI Mode sometimes puts the website inside the name field as well
        # ("Accredo Packaging (accredopackaging.com)"). Left in place it
        # reaches the export as part of the company name, and creates a
        # false duplicate of the same company written without it
        # ("Flex Films (flexfilm.com)" alongside "Flex Films (USA) Inc.").
        # A trailing parenthetical that is a bare domain is never part of a
        # real company name, so it is removed; other parentheticals (e.g.
        # "(USA)") are genuine and kept.
        name_domain = re.search(r"\s*\((\s*[a-z0-9][a-z0-9.-]*\.[a-z]{2,}[^)]*)\)\s*$", name, re.IGNORECASE)
        if name_domain:
            name = name[: name_domain.start()].strip()
        if not name or len(name) > 120 or is_fragment_only_name(name):
            continue

        domain = ""
        raw_site = str(row.get("website") or "").strip()
        if not raw_site and name_domain:
            raw_site = name_domain.group(1).strip()
        if raw_site:
            candidate_domain = extract_domain(
                raw_site if raw_site.startswith("http") else "https://" + raw_site
            )
            if candidate_domain and not is_junk_domain(candidate_domain):
                domain = candidate_domain

        mentions.append(
            AiModeCompanyMention(
                name=name,
                hq_country=str(row.get("country") or "").strip(),
                products=str(row.get("products") or "").strip(),
                domain=domain,
            )
        )
    if mentions:
        logger.info("Parsed %d companies from AI Mode's JSON response", len(mentions))
    return mentions


def extract_company_mentions(result: dict) -> list[AiModeCompanyMention]:
    """Extract company mentions (name + whatever context is available) from
    a scraper result dict.

    The JSON array the prompt asks for is read first, since it carries clean
    field boundaries and cannot produce a mangled name. Everything after it
    is a fallback for answers that ignore the requested format: markdown
    tables, a Python-tuple-list snippet, the "Name (Country/domain) –
    description" prose shape, the "Name — domain.com (Country)" shape, and
    the bare "Name (domain.com)" shape with no separator at all. An answer
    can contain more than one shape (e.g. a prose summary followed by a
    structured recap), so results from multiple parsers are merged rather
    than stopping at the first non-empty one."""
    mentions: list[AiModeCompanyMention] = []

    answer_text = result.get("answer") or ""
    if answer_text:
        mentions.extend(_mentions_from_json(answer_text))

    for table_md in result.get("tables") or []:
        mentions.extend(_mentions_from_table(table_md))

    if answer_text:
        mentions.extend(_mentions_from_tuple_list(answer_text))
        mentions.extend(_mentions_from_text(answer_text))
        mentions.extend(_mentions_from_dash_domain_text(answer_text))
        mentions.extend(_mentions_from_bare_domain_paren(answer_text))

    seen: dict[str, AiModeCompanyMention] = {}
    for m in mentions:
        key = m.name.lower().strip()
        if key and key not in seen:
            seen[key] = m
    return list(seen.values())


class ChromiumNotFoundError(RuntimeError):
    """Raised when the Chromium binary Selenium needs isn't installed at
    the expected path. Distinct from a normal search() failure (bad
    response, timeout, etc.) so callers can fail the whole discovery loop
    fast with a clear message instead of retrying up to 12 times against a
    browser that will never launch -- confirmed as a real failure mode: a
    user's machine had no Chromium installed and every single attempt
    burned through Selenium's own multi-second startup + connection
    timeout before failing, with only a raw stacktrace buried in the logs
    to explain why."""


class RateLimitedError(RuntimeError):
    """Raised when Google AI Mode itself returns "You've reached the
    request limit for AI responses. Try again in a little while." --
    distinct from a normal search() failure. Unlike ChromiumNotFoundError
    (which means every remaining attempt will fail identically, so the
    whole discovery loop should stop immediately), a rate limit is
    transient: the caller should back off for a while and then keep
    trying, not abandon the run and not hammer Google again instantly
    with a fresh profile (a fresh local Chromium profile doesn't bypass a
    Google-side rate limit tied to the account/IP)."""


def _chromium_missing() -> bool:
    from pathlib import Path as _Path
    expected = _Path.home() / "AppData" / "Local" / "Chromium" / "Application" / "chrome.exe"
    return not expected.is_file()


def search(query: str, headless: bool = False, profile_dir: str | None = None) -> list[AiModeCompanyMention]:
    """Query Google AI Mode via the Selenium scraper and return company
    mentions (name + any country/products/website context given) -- none of
    this is trusted as final fact. Every mention still goes through the
    pipeline's own website resolution (if no domain given), crawl, verify,
    and classify stages before being accepted, per the spec's rule against
    trusting LLM output without independent verification.

    headless defaults to False: testing showed Google's bot detection blocks
    headless Chromium instantly with a reCAPTCHA wall, while a visible browser
    with a real profile passes through cleanly.

    profile_dir: override the default shared Chromium profile directory --
    required when running multiple search() calls concurrently, since two
    Chromium instances pointed at the same --user-data-dir collide on the
    profile's lock file and one of them fails outright.
    """
    try:
        from google_ai_scraper import GoogleAIModeScraper
    except ImportError as e:
        logger.warning("google_ai_scraper module not available: %s", e)
        return []

    if _chromium_missing():
        raise ChromiumNotFoundError(
            "Chromium is not installed at the expected location "
            "(%AppData%\\Local\\Chromium\\Application\\chrome.exe). "
            "Run SETUP.bat to install it automatically, or install it "
            "manually from https://www.chromium.org/getting-involved/download-chromium/"
        )

    scraper = None
    try:
        scraper = GoogleAIModeScraper(headless=headless, verbose=False, profile_dir=profile_dir)
        result = scraper.ask_ai_mode(query)
    except Exception as e:
        logger.warning("Google AI Mode query failed for %r: %s", query, e)
        return []
    finally:
        if scraper:
            try:
                scraper.close()
            except Exception:
                pass

    if result.get("rate_limited"):
        raise RateLimitedError(
            "Google AI Mode returned its own request-limit message -- back off before retrying."
        )

    if not result.get("success"):
        logger.warning("Google AI Mode returned no result for %r: %s", query, result.get("error"))
        return []

    answer_text = result.get("answer") or ""
    answer_len = len(answer_text)
    table_count = len(result.get("tables") or [])
    mentions = extract_company_mentions(result)
    logger.info(
        "Google AI Mode answer: %d chars, %d tables -> %d company mentions for query %r",
        answer_len, table_count, len(mentions), query[:60],
    )
    if len(mentions) <= 2 and answer_len > 10000:
        # A substantial answer that parses to almost nothing is either a
        # genuine parser gap (a response shape the regex doesn't handle) or
        # AI Mode giving a long hedged/clarifying non-list answer -- both
        # worth being able to inspect after the fact instead of only during
        # live debugging, since this has turned out to be intermittent and
        # hard to reproduce on demand. Best-effort only; never let a dump
        # failure break discovery.
        try:
            dump_dir = Path(__file__).resolve().parent.parent / "logs" / "low_mention_dumps"
            dump_dir.mkdir(parents=True, exist_ok=True)
            dump_path = dump_dir / f"{int(time.time())}_{len(mentions)}mentions_{answer_len}chars.txt"
            dump_path.write_text(answer_text, encoding="utf-8")
            logger.warning(
                "Only %d mentions parsed from a %d-char answer -- likely a refusal or an "
                "unparsed response format, not necessarily an empty AI Mode response. "
                "Raw answer saved to %s for inspection.",
                len(mentions), answer_len, dump_path,
            )
        except OSError:
            logger.warning(
                "Only %d mentions parsed from a %d-char answer -- likely a refusal or an "
                "unparsed response format (raw-answer dump failed, continuing anyway)",
                len(mentions), answer_len,
            )
    return mentions
