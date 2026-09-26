"""
tier_model_code.py
------------------
Tier 4 edge provider: groups offers by a TRUSTED, DISCRIMINATIVE model code,
but only within the same (category, effective_brand) block.

This module is a pure EDGE PROVIDER. It does NOT hold or mutate a UnionFind
instance — the caller applies the returned edges to the shared DSU. This
mirrors tiers 2 and 3 exactly (offers in, edges out).

Pipeline:
  1. Spec-pattern blocklist — filter out codes that look like spec fragments
     (screen sizes, RAM/storage configs, resolutions, etc.) rather than
     manufacturer model numbers.
  2. Block-spread filter — codes appearing in >= 2 distinct
     (category, effective_brand) blocks are non-discriminative and ignored.
  3. Trusted codes per offer — the codes surviving both filters above AND
     unique within the offer's store and block: a code carried by two or
     more DISTINCT VARIANTS (distinct normalised titles, bundle listings
     excluded) of the same store in the same (category, effective_brand)
     block is a series/family tag (e.g. one code across several screen
     sizes), not a model identity, and is dropped. Duplicate listings and
     colour variants of one product do not count as separate variants.
  4. One discriminative code per offer — pick a single code per offer to
     avoid chaining unrelated products via overlapping secondary codes.
     Priority: carried by the most stores, then longest, then rarest,
     then alphabetical.
  5. Blocked union — group offers sharing the same chosen code within the same
     (category, effective_brand) block; emit star-pattern edges.

Cross-block unions are NEVER created. The block key (category, effective_brand)
is part of the grouping key, so this is structurally enforced.
"""

import re
from collections import Counter, defaultdict

try:
    from .load import EnrichedOffer
except ImportError:
    from load import EnrichedOffer


# ---------------------------------------------------------------------------
# (1) SPEC-PATTERN BLOCKLIST
# ---------------------------------------------------------------------------
# Compiled regexes matched against the full code string (case-insensitive,
# anchored to start/end). Each pattern catches a family of spec fragments
# that extract_model_codes lets through (legitimately — they are real tokens)
# but which are NOT usable as tier-4 match keys because they describe
# specifications, not model identities.
#
# SHOULD BE BLOCKED (examples):
#   13inch, 14inch, 16inch         — screen sizes
#   10kg, 12kg10kg                 — weights / dual weights
#   2in1, 3in1, 4in1               — "N-in-M" combo labels
#   1080p, 1296p                   — resolutions
#   9000btu, 24000btu              — BTU ratings
#   6gb128gb, 8gb256gb, gb1tb      — RAM/storage configs
#   10core, 10core16gb256gb        — CPU core blobs
#   r57520u16gb512gb, i513420h16   — CPU/RAM/SSD concatenated specs
#   5120u16gb512gb                 — processor + storage blobs
#   gen2, pro5                     — standalone version tags
#   ip67, ip54, ip68               — IP (ingress protection) ratings
#   10gpu, 8gpu                    — GPU core counts
#   watch8, watch7                 — standalone series tags
#   15arp10, 15arp10e              — Lenovo platform codes, see the
#                                    platform-code pattern
#   14ahp9, 16irh8, 16iax10h       — Lenovo platform codes (same)
#   x360                           — HP convertible marketing tag
#   buds3, buds4                   — Samsung earbud series tags
#
# SHOULD PASS (genuine model codes):
#   1000mk2, mg23k3515as, scg6050ss, 14he0001nv, 13bg1000nv
#

_SPEC_PATTERNS: list[re.Pattern] = [
    # Screen sizes: 13inch, 14inch, 16inch, etc.
    re.compile(r"^\d{1,2}inch$", re.IGNORECASE),

    # Weights: 10kg, and dual like 12kg10kg.
    re.compile(r"^\d{1,2}kg(\d{1,2}kg)?$", re.IGNORECASE),

    # "N in M" combos: 2in1, 3in1, 4in1, etc.
    re.compile(r"^\d+in\d+$", re.IGNORECASE),

    # Resolutions: 1080p, 1296p, 720p, 2160p, etc.
    re.compile(r"^\d{3,4}p$", re.IGNORECASE),

    # BTU ratings: 9000btu, 24000btu, etc.
    re.compile(r"^\d{4,5}btu$", re.IGNORECASE),

    # RAM/storage configs: 6gb128gb, 8gb256gb, 12gb512gb, gb512gb, gb1tb,
    # and variations like 16gb1tb.
    re.compile(r"^(\d+)?gb\d+(gb|tb)$", re.IGNORECASE),

    # Core/CPU/config blobs: anything containing "core" adjacent to digits.
    # Catches 10core, 10core16gb256gb, 10core16gb512gb10core.
    re.compile(r".*\d+core.*", re.IGNORECASE),
    re.compile(r".*core\d+.*", re.IGNORECASE),

    # Long concatenated CPU/RAM/SSD/GPU spec blobs.
    # Pattern: a short letter prefix (processor family like "r5", "i5") followed
    # by digits, then memory/storage suffixes (gb, tb).
    # Catches: r57520u16gb512gb, i513420h16, 5120u16gb512gb.
    re.compile(r"^[a-z]?\d{3,}[a-z]\d+gb", re.IGNORECASE),

    # Standalone generic version tags: gen2, gen3, pro5, etc.
    # Only blocks when the ENTIRE code is just "genN" or "proN" —
    # does NOT block longer codes that merely contain these substrings.
    re.compile(r"^gen\d+$", re.IGNORECASE),
    re.compile(r"^pro\d+$", re.IGNORECASE),

    # IP (ingress protection) ratings: ip67, ip54, ip68, etc.
    # These are environmental protection specs, not model identifiers.
    re.compile(r"^ip\d{2}$", re.IGNORECASE),

    # GPU core counts: 10gpu, 8gpu, etc.
    re.compile(r"^\d+gpu$", re.IGNORECASE),

    # Standalone series tags: watch8, watch7, etc.
    # These are generic product-line tags (e.g. "Galaxy Watch 8") that merge
    # unrelated variants. Does NOT block longer codes containing "watch".
    re.compile(r"^watch\d+$", re.IGNORECASE),

    # Lenovo chassis platform codes: 14ahp9, 16irh8, 15irx10, 16agp11,
    # 16iax10h, 15arp10. Shape: 2-digit screen size, 3-4 letter platform
    # (CPU vendor / generation letters), 1-2 digit generation, optional
    # trailing letter. The same code is shared by LOQ, Legion, IdeaPad and
    # Yoga models, so it identifies a platform, not a product.
    re.compile(r"^\d{2}[a-z]{3,4}\d{1,2}[a-z]?$", re.IGNORECASE),

    # HP convertible marketing tag: x360. Shared across unrelated models
    # (Pavilion x360, Envy x360, Spectre x360), so it is never a model code.
    re.compile(r"^x360$", re.IGNORECASE),

    # Samsung earbud series tags: buds3, buds4, etc. Same reasoning as
    # watch\d above — a product-line tag, not a model identity. Does NOT
    # block longer codes containing "buds".
    re.compile(r"^buds\d$", re.IGNORECASE),

    # Mains voltage and frequency marking "D230/50", collapsed by the
    # joiner step into "d23050"; shared by unrelated appliances, so it is
    # never a model code.
    re.compile(r"^d230\d{2}$", re.IGNORECASE),
]


def is_spec_like_code(code: str) -> bool:
    """Return True if a code is a spec/config fragment, not a model number.

    Checks the code against the spec-pattern blocklist. The code is assumed
    to already be lowercase and accent-stripped (as produced by
    extract_model_codes).
    """
    for pat in _SPEC_PATTERNS:
        if pat.match(code):
            return True
    return False


# ---------------------------------------------------------------------------
# (1b) SERIES-SUFFIX GUARD
# ---------------------------------------------------------------------------
# Short codes matching the pattern "3-4 digits followed by 1-2 lowercase
# letters" are model-number suffixes shared across different product lines.
# For example, JBL "Tune 770NC" and JBL "Live 770NC" both extract "770nc",
# but they are distinct products. Unioning on the suffix alone merges them.
#
# Pattern: ^\d{3,4}[a-z]{1,2}$
#
# SHOULD BE REJECTED:
#   770nc, 670nc, 680nc, 520c, 310c, 520bt, 135bt, 125bt
#
# MUST NOT be rejected (these start with a letter, so the pattern does not
# match):
#   z150, h340, h111, m500, m185, m190, mdrzx310, mdrzx310ap, cre611s06,
#   bch6ath25, wh1000xm5, whch520
#

_SERIES_SUFFIX_RE = re.compile(r"^\d{3,4}[a-z]{1,2}$")


def is_series_suffix_code(code: str) -> bool:
    """Return True if a code is a short digit-prefix + letter-suffix fragment.

    These are model-number suffixes shared across different product lines
    (e.g. "770nc" from both "Tune 770NC" and "Live 770NC"). They must not
    be used as tier-4 match keys.

    The code is assumed to already be lowercase (as produced by
    extract_model_codes).
    """
    return bool(_SERIES_SUFFIX_RE.match(code))


def _code_is_eligible(code: str) -> bool:
    """Return True if a code passes BOTH the spec-pattern and series-suffix
    filters. A code must pass both to be considered for tier-4 matching."""
    return not is_spec_like_code(code) and not is_series_suffix_code(code)


# ---------------------------------------------------------------------------
# (2) BLOCK-SPREAD FILTER
# ---------------------------------------------------------------------------

def _build_block_spread(
    offers: list[EnrichedOffer],
) -> dict[str, int]:
    """Map each non-spec code to the number of distinct blocks it appears in.

    A "block" is a (category, effective_brand) pair. Codes flagged by
    is_spec_like_code are excluded entirely — they never enter the spread map.
    """
    code_blocks: dict[str, set[tuple[str | None, str | None]]] = defaultdict(set)
    for offer in offers:
        block = (offer.category, offer.effective_brand)
        for code in offer.model_codes:
            # Must pass both spec-pattern and series-suffix filters.
            if _code_is_eligible(code):
                code_blocks[code].add(block)
    return {code: len(blocks) for code, blocks in code_blocks.items()}


# ---------------------------------------------------------------------------
# (2b) INTRA-STORE UNIQUENESS COUNTS
# ---------------------------------------------------------------------------
# A code is only trusted if, inside one store and one block, it belongs to a
# single product variant. Counting raw offers is too strict: a store may list
# the same product twice, list colour variants separately, or list a set
# that contains the product. None of those make the code a family tag, so
# the count below is over distinct variants, with bundle listings skipped.

# Bundle detection. Two regexes, used together by _looks_like_bundle:
#
# _BUNDLE_WORDS_RE — words that on their own mark a set listing:
#   bundle               — "Gaming Bundle"
#   σετ                  — Greek "set"
#   design kit           — Bosch/Siemens "Flex Design Kit" oven sets
#   set εντοιχισμ...     — Greek "built-in set" (set εντοιχισμού)
# A bare "kit" is deliberately NOT a marker: it also appears in single
# product names (starter kits, cleaning kits sold as one SKU).
#
# _BUNDLE_JOINER_RE — joiners that combine two products in one title:
#   " & ", " + ", "&amp;" (HTML-escaped ampersand left in scraped titles).
# The ampersand and plus must be surrounded by spaces so that brand or model
# names like "B&O" or "Galaxy S24+" are not treated as joiners. A joiner is
# only a bundle signal when the offer also carries two or more model codes.
_BUNDLE_WORDS_RE = re.compile(
    r"(\bbundle\b|\bσετ\b|design kit|set εντοιχισμ)",
    re.IGNORECASE,
)
_BUNDLE_JOINER_RE = re.compile(r"(\s&\s|\s\+\s|&amp;)")


def _looks_like_bundle(offer: EnrichedOffer) -> bool:
    """Return True if an offer is a bundle or set listing rather than a
    single product.

    Two signals, either is enough:
      - an explicit bundle word in the title (bundle, σετ, design kit,
        set εντοιχισμού), or
      - a joiner (" & ", " + ", "&amp;") together with at least two
        extracted model codes, which is how retailers write oven-and-hob
        sets and camera-and-lens kits ("HBA514BS3 & PKE645BA2E").
    A joiner alone is not enough: "14AKP10 + Copilot+ PC" or
    "Citiz & Milk D123" carry one code and are single products.

    Bundle offers take no part in model-code matching at all: they are
    excluded from the per-store variant count AND receive no trusted
    codes, so a set can never attach to the base product it contains.
    Matching bundles to each other is a separate concern.
    """
    title = offer.title or ""
    # Signal 1: an explicit set/bundle word anywhere in the title.
    if _BUNDLE_WORDS_RE.search(title):
        return True
    # Signal 2: a joiner between two products, confirmed by two or more codes.
    if len(offer.model_codes) >= 2 and _BUNDLE_JOINER_RE.search(title):
        return True
    return False


def _build_store_block_counts(
    offers: list[EnrichedOffer],
) -> dict[tuple[str, str | None, str | None, str], int]:
    """Count, per (store, category, effective_brand, code), how many
    DISTINCT VARIANTS of that store in that block carry the code.

    A variant is identified by the offer's normalised title
    (offer.title_norm):
      - identical listings collapse to one variant (same title_norm);
      - colour variants collapse to one, because normalize_title strips
        colour words;
      - screen sizes, capacities and configurations stay distinct, because
        their tokens survive normalisation;
      - bundle listings (see _looks_like_bundle) are not counted at
        all, so a base product plus a set containing it is one variant.

    A code carried by two or more distinct variants of ONE store inside
    one block is a series or family tag (for example "qn70h" across six
    screen sizes, or "cy26" across two vacuum variants), not a model
    identity.  Such codes must never be used to union offers, otherwise
    every size or variant of the family collapses into a single product.
    Only codes flagged by neither the spec-pattern nor the series-suffix
    filter are counted.
    """
    # (store, category, effective_brand, code) -> set of distinct title_norm.
    variants: dict[tuple[str, str | None, str | None, str], set[str]] = (
        defaultdict(set)
    )
    for offer in offers:
        # Bundle/set listings would otherwise look like an extra variant of
        # every code they contain, so they are left out of the count.
        if _looks_like_bundle(offer):
            continue
        for code in offer.model_codes:
            # Ineligible codes are never trusted anyway, so skip counting them.
            if _code_is_eligible(code):
                key = (offer.store, offer.category, offer.effective_brand, code)
                variants[key].add(offer.title_norm)
    return {key: len(titles) for key, titles in variants.items()}


# ---------------------------------------------------------------------------
# (3) TRUSTED CODES PER OFFER
# ---------------------------------------------------------------------------

def _trusted_codes(
    offer: EnrichedOffer,
    block_spread: dict[str, int],
    store_block_counts: dict[tuple[str, str | None, str | None, str], int],
) -> list[str]:
    """Return the codes from this offer that pass all filters.

    A code is trusted if:
      - it passes the spec-pattern blocklist and the series-suffix guard,
      - it appears in exactly 1 (category, effective_brand) block, AND
      - within this offer's store and block it is carried by at most one
        distinct variant.  See _build_store_block_counts for how variants
        are counted and why a code spread over several variants inside
        one store is a family tag, not a model identity.

    A count of zero is possible: if every listing of this offer's store
    and block carrying the code looked like a bundle, none were counted.
    Such a code is allowed.

    A bundle or set listing (see _looks_like_bundle) gets no trusted codes
    at all, so it can never be unioned with the products it contains.
    """
    if _looks_like_bundle(offer):
        return []
    return [
        c for c in offer.model_codes
        if _code_is_eligible(c)
        # Cross-block codes are non-discriminative.
        and block_spread.get(c, 0) == 1
        # Codes spread over several variants within this store's block are
        # family/series tags. 0 (only bundle listings) or 1 variant is fine.
        and store_block_counts.get(
            (offer.store, offer.category, offer.effective_brand, c), 0
        ) <= 1
    ]


# ---------------------------------------------------------------------------
# (4) ONE DISCRIMINATIVE CODE PER OFFER
# ---------------------------------------------------------------------------

def choose_discriminative_code(
    offer: EnrichedOffer,
    freq: dict[str, int],
    block_spread: dict[str, int],
    store_block_counts: dict[tuple[str, str | None, str | None, str], int],
    store_spread: dict[str, int],
) -> str | None:
    """Pick exactly one discriminative code for an offer, or None.

    Selection priority (first wins):
      0. Family-prefix filter (applied before ranking): a code that is a
         prefix of another code in the same offer, at least four
         characters shorter, is dropped ("p500" beside
         "p500sv05210h0020", "zve10" beside "zve10m2kb").  The short
         form is a series label and would otherwise win step 1 simply
         because more stores print the series name.  A gap under four
         characters is kept ("hta3000" vs "hta3000cel"), so regional
         suffixes do not remove the shared root.
      1. Carried by the most stores (distinct stores whose offers carry
         the code, descending).  A code seen in only one store can never
         produce a cross-store match, so it must not outrank one that
         can.  This is what made retailers that write both a series name
         and a full model number ("WW5000D WW11DG5B25AELE",
         "SM-L330 SM-L330NDAAEUE") previously pick the series name and
         miss every match.  Ranking by the actual store count, not just
         ">= 2", also keeps an offer in the largest group: for the Sony
         HT-A3000, "hta3000" is shared by three stores and "hta3000cel"
         by two, so an offer carrying both must prefer "hta3000" or it
         leaves the larger group.
      2. Longest code (more characters = more specific).
      3. Smallest global offer-frequency (rarer = more specific).
      4. Alphabetical (final deterministic tie-break).

    'freq' maps each trusted code to the number of distinct offers
    containing it; 'store_spread' maps each trusted code to the number of
    distinct stores whose offers carry it.
    """
    codes = _trusted_codes(offer, block_spread, store_block_counts)
    if not codes:
        return None
    # Drop a code that is a short prefix of a much longer code in the
    # same offer: "p500" next to "p500sv05210h0020", "zve10" next to
    # "zve10m2kb", "sml330" next to "sml330ndaaeue". The short form is
    # a series or family label and the long form is the model. A gap of
    # fewer than four characters is kept, because regional suffixes
    # ("hta3000" vs "hta3000cel") must not remove the shared root.
    codes = [
        c for c in codes
        if not any(o != c and o.startswith(c) and len(o) - len(c) >= 4 for o in codes)
    ]
    if not codes:
        return None
    # Sort key: (store count descending, length descending, frequency
    # ascending, alpha ascending).  The first element after sorting is the
    # chosen code.
    codes.sort(
        key=lambda c: (
            -store_spread.get(c, 0),
            -len(c),
            freq.get(c, 0),
            c,
        )
    )
    return codes[0]


# ---------------------------------------------------------------------------
# (5) BLOCKED UNION — model_code_groups / model_code_edges
# ---------------------------------------------------------------------------

def _precompute(
    offers: list[EnrichedOffer],
) -> tuple[dict[str, int], dict[str, int], dict[int, str | None]]:
    """Precompute shared data needed by grouping and edge functions.

    Returns:
      block_spread: code -> number of distinct blocks it appears in
      freq: code -> number of distinct offers containing it (trusted only)
      chosen: offer index -> chosen discriminative code (or None)
    """
    block_spread = _build_block_spread(offers)

    # Per (store, category, effective_brand, code) distinct-variant counts,
    # used by _trusted_codes to drop codes spread over several variants
    # within one store's block.
    store_block_counts = _build_store_block_counts(offers)

    # Build the trusted-code frequency map (code -> offers carrying it) and
    # the store-spread map (code -> distinct stores carrying it) in one pass.
    freq: Counter[str] = Counter()
    code_stores: dict[str, set[str]] = defaultdict(set)
    for offer in offers:
        for code in _trusted_codes(offer, block_spread, store_block_counts):
            freq[code] += 1
            code_stores[code].add(offer.store)
    store_spread = {code: len(stores) for code, stores in code_stores.items()}

    # Choose one discriminative code per offer.
    freq_map = dict(freq)
    chosen: dict[int, str | None] = {}
    for idx, offer in enumerate(offers):
        chosen[idx] = choose_discriminative_code(
            offer, freq_map, block_spread, store_block_counts, store_spread
        )

    return block_spread, freq_map, chosen


def model_code_groups(
    offers: list[EnrichedOffer],
) -> dict[tuple[str, str], dict[str, list[int]]]:
    """Group offers by (category, effective_brand) block and chosen code.

    Outer key: (category, effective_brand) block.
    Inner key: the chosen discriminative code.
    Value: sorted offer indices in that block sharing that code.

    Offers with no chosen code, or empty/None effective_brand, are skipped.
    Deterministic ordering throughout.
    """
    _block_spread, _freq, chosen = _precompute(offers)

    # Nested grouping: block -> code -> [offer indices].
    groups: dict[tuple[str, str], dict[str, list[int]]] = {}
    for idx, offer in enumerate(offers):
        code = chosen[idx]
        if code is None:
            continue
        # Skip offers with empty/None effective_brand.
        if not offer.effective_brand:
            continue
        cat = offer.category or ""
        block_key = (cat, offer.effective_brand)
        if block_key not in groups:
            groups[block_key] = defaultdict(list)
        groups[block_key][code].append(idx)

    # Sort member indices within each code group, and convert to plain dicts
    # with sorted keys for deterministic output.
    result: dict[tuple[str, str], dict[str, list[int]]] = {}
    for block_key in sorted(groups):
        inner = groups[block_key]
        result[block_key] = {
            code: sorted(inner[code]) for code in sorted(inner)
        }
    return result


def model_code_edges(offers: list[EnrichedOffer]) -> list[tuple[int, int]]:
    """Return edges linking offers that share a discriminative code within a block.

    For each (block, code) group of size >= 2, emits star-pattern edges to
    the smallest-index member. Never links offers across different blocks.

    Groups of size 1 emit no edges (nothing to merge).
    """
    edges: list[tuple[int, int]] = []
    for _block, code_groups in model_code_groups(offers).items():
        for _code, members in code_groups.items():
            if len(members) < 2:
                continue
            hub = members[0]
            for member in members[1:]:
                edges.append((hub, member))
    return edges


# ---------------------------------------------------------------------------
# (6) REVIEW SIGNALS — inspection only, no write-back
# ---------------------------------------------------------------------------

def review_signals(
    offers: list[EnrichedOffer],
    clusters: list[list[int]],
) -> list[tuple[list[int], str]]:
    """Flag non-trivial clusters (size >= 2) that need manual review.

    Singletons are never flagged — there is no union to review.

    Returns a list of (cluster, reason) pairs for clusters where:
      (a) any member has is_suspicious_brand=True AND brand_from_title is
          None/empty -> "unresolved_suspicious_brand"
      (b) members have >= 2 distinct non-empty effective_brand values
          -> "cross_effective_brand" (a risky cross-brand union)

    Does NOT write anywhere — returns data for the caller to display.
    """
    flagged: list[tuple[list[int], str]] = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        # (a) Check for unresolved suspicious brands.
        for idx in cluster:
            o = offers[idx]
            if o.is_suspicious_brand and not o.brand_from_title:
                flagged.append((cluster, "unresolved_suspicious_brand"))
                break
        # (b) Check for cross-effective-brand union.
        distinct_brands = {offers[idx].effective_brand for idx in cluster
                          if offers[idx].effective_brand}
        if len(distinct_brands) >= 2:
            flagged.append((cluster, "cross_effective_brand"))
    return flagged


# ---------------------------------------------------------------------------
# Introspection helpers (used by inspect_offers.py for the --tier4 report)
# ---------------------------------------------------------------------------

def get_precomputed(
    offers: list[EnrichedOffer],
) -> tuple[dict[str, int], dict[str, int], dict[int, str | None]]:
    """Expose precomputed data for the inspection report.

    Returns (block_spread, freq, chosen) — same as _precompute.
    """
    return _precompute(offers)

def get_spec_blocked_codes(offers: list[EnrichedOffer]) -> set[str]:
    """Return the set of distinct codes blocked by the spec-pattern filter."""
    blocked: set[str] = set()
    for offer in offers:
        for code in offer.model_codes:
            if is_spec_like_code(code):
                blocked.add(code)
    return blocked


def get_series_suffix_blocked_codes(offers: list[EnrichedOffer]) -> set[str]:
    """Return the set of distinct codes blocked by the series-suffix guard.

    Only includes codes that pass the spec-pattern filter but fail the
    series-suffix check (to avoid double-counting with spec-blocked codes).
    """
    blocked: set[str] = set()
    for offer in offers:
        for code in offer.model_codes:
            if not is_spec_like_code(code) and is_series_suffix_code(code):
                blocked.add(code)
    return blocked
