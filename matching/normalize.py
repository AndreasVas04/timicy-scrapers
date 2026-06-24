"""
normalize.py
------------
Pure, deterministic text-normalization functions for product matching.

Every function in this module is side-effect free: no database access,
no network calls, no file I/O, no global mutable state.  Inputs go in,
canonical strings come out.
"""

import hashlib
import re
import unicodedata

from .lexicons import BRAND_ALIASES, COLOR_WORDS, STORE_NOISE


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_lookup_key(text: str) -> str:
    """Build a brand-lookup key: uppercase, strip every non-alphanumeric char."""
    return re.sub(r"[^A-Z0-9]", "", text.upper())


# Pre-compile the sorted color patterns (longest first so multi-word phrases
# like "space gray" are removed before the single word "gray").
# Each pattern is word-boundary aware to avoid corrupting other words.
_COLOR_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?<!\w)" + re.escape(c) + r"(?!\w)")
    for c in sorted(COLOR_WORDS, key=len, reverse=True)
]

# Pre-compile store-noise patterns (longest first, same logic as colors).
_NOISE_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?<!\w)" + re.escape(n) + r"(?!\w)")
    for n in sorted(STORE_NOISE, key=len, reverse=True)
]

# Regex for storage quantities: an integer, optional space, then gb/tb.
_RE_STORAGE = re.compile(
    r"(\d+)\s*(tb|gb)",
    re.IGNORECASE,
)

# Regex for screen sizes: a number (dot or comma decimal), optional space,
# then an inch indicator (", '', ″, inch, inches, in, ίντσες, ιντσών).
_RE_SCREEN = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:\"|'{2}|\u2033|inches|inch|in\b|ίντσες|ιντσών|ιντσες|ιντσων)",
    re.IGNORECASE,
)

# Regex to collapse multiple whitespace chars into a single space.
_RE_MULTI_SPACE = re.compile(r"\s+")

# Regex matching punctuation characters to strip in the title pipeline.
# Keeps alphanumerics, spaces, and the dot (used in normalized units like "6.7in").
_RE_PUNCTUATION = re.compile(r"[^\w\s.]", re.UNICODE)


# ---------------------------------------------------------------------------
# 1. Brand normalization
# ---------------------------------------------------------------------------

def normalize_brand(raw_vendor: str | None) -> str:
    """Normalize a raw vendor / brand string to a canonical brand name.

    Lookup is done via BRAND_ALIASES using a key that is uppercased with all
    non-alphanumeric characters removed (so "De'Longhi" and "DELONGHI" match).
    Unknown brands are returned in their stripped, uppercased form so the
    output is stable across runs.  None or empty input returns "".
    """
    if not raw_vendor or not raw_vendor.strip():
        return ""
    cleaned = _RE_MULTI_SPACE.sub(" ", raw_vendor.strip())
    key = _make_lookup_key(cleaned)
    if not key:
        return ""
    # Return canonical form if known, otherwise the stable lookup key.
    return BRAND_ALIASES.get(key, key)


# ---------------------------------------------------------------------------
# 2. Accent / diacritic stripping (Greek-aware)
# ---------------------------------------------------------------------------

def strip_accents(text: str) -> str:
    """Remove combining diacritical marks (accents) and unify Greek final sigma.

    NFD-decomposes the string, drops all combining marks, then recomposes.
    Greek final sigma (ς, U+03C2) is replaced with regular sigma (σ, U+03C3)
    so that word-final and word-internal forms match.  Does NOT lowercase —
    that is handled separately in the title pipeline.
    """
    # Decompose so accents become separate combining characters.
    nfd = unicodedata.normalize("NFD", text)
    # Drop every combining mark (category starts with 'M').
    stripped = "".join(ch for ch in nfd if unicodedata.category(ch)[0] != "M")
    # Recompose to NFC for consistent representation.
    result = unicodedata.normalize("NFC", stripped)
    # Unify Greek final sigma ς -> σ.
    return result.replace("ς", "σ")


# ---------------------------------------------------------------------------
# 3. Unit normalization (storage and screen size)
# ---------------------------------------------------------------------------

def _replace_storage(m: re.Match) -> str:
    """Convert a storage match to normalized '{n}gb' form, expanding TB."""
    value = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "tb":
        value *= 1024
    return f"{value}gb"


def _replace_screen(m: re.Match) -> str:
    """Convert a screen-size match to normalized '{n}in' form."""
    # Normalize decimal separator: comma -> dot.
    num = m.group(1).replace(",", ".")
    return f"{num}in"


def normalize_units(text: str) -> str:
    """Normalize storage (GB/TB) and screen-size quantities in *text*.

    - Storage: integer + optional space + gb/tb -> "{n}gb" (TB*1024).
    - Screen: number (dot or comma decimal) + inch indicator -> "{n}in".
    - All other numbers are left untouched.
    """
    text = _RE_STORAGE.sub(_replace_storage, text)
    text = _RE_SCREEN.sub(_replace_screen, text)
    return text


# ---------------------------------------------------------------------------
# 4. Color stripping
# ---------------------------------------------------------------------------

def strip_color(text: str) -> str:
    """Remove color / finish words and phrases from *text*.

    Multi-word phrases (e.g. "space gray") are tried before single words so
    they are removed as a unit.  Matching is word-boundary aware.
    """
    for pat in _COLOR_PATTERNS:
        text = pat.sub("", text)
    return text


# ---------------------------------------------------------------------------
# 5. Full title normalization pipeline
# ---------------------------------------------------------------------------

def normalize_title(raw_title: str | None, brand_norm: str = "") -> str:
    """Normalize a product title into a stable, comparable token string.

    Pipeline order:
      1. Lowercase
      2. Strip accents (Greek-aware)
      3. Normalize units (storage, screen size)
      4. Remove the brand name (canonical + raw alias forms)
      5. Strip color / finish tokens
      6. Remove store-noise phrases
      7. Remove punctuation (keep alphanumerics, spaces, dots)
      8. Collapse whitespace and strip

    The result is deterministic and idempotent:
        normalize_title(normalize_title(x)) == normalize_title(x)
    """
    if not raw_title:
        return ""

    text = raw_title.lower()
    text = strip_accents(text)
    text = normalize_units(text)

    # Remove the canonical brand and any known alias forms so that
    # "SAMSUNG Galaxy S24" and "Galaxy S24" produce the same output.
    if brand_norm:
        # Remove canonical brand (lowercased).
        brand_lower = brand_norm.lower()
        brand_pattern = re.compile(
            r"(?<!\w)" + re.escape(strip_accents(brand_lower)) + r"(?!\w)"
        )
        text = brand_pattern.sub("", text)
        # Also remove any raw alias that maps to this brand.
        for alias_key, canonical in BRAND_ALIASES.items():
            if canonical == brand_norm:
                alias_lower = alias_key.lower()
                alias_pat = re.compile(
                    r"(?<!\w)" + re.escape(alias_lower) + r"(?!\w)"
                )
                text = alias_pat.sub("", text)

    text = strip_color(text)

    # Remove store-noise tokens.
    for pat in _NOISE_PATTERNS:
        text = pat.sub("", text)

    # Strip punctuation but keep dots (needed for units like "6.7in").
    text = _RE_PUNCTUATION.sub(" ", text)
    # Collapse whitespace and strip.
    text = _RE_MULTI_SPACE.sub(" ", text).strip()

    return text


# ---------------------------------------------------------------------------
# 6. Identifier normalization (MPN / mpn_root)
# ---------------------------------------------------------------------------

def normalize_identifier(value: str | None) -> str:
    """Canonicalize an MPN or mpn_root value.

    Uppercases and strips whitespace.  Does NOT re-derive mpn_root — the
    scrapers already provide it; this only ensures consistent formatting.
    """
    if not value or not value.strip():
        return ""
    return _RE_MULTI_SPACE.sub(" ", value.strip()).upper()


# ---------------------------------------------------------------------------
# 7. EAN cleaning
# ---------------------------------------------------------------------------

def clean_ean(raw_ean: str | None) -> str | None:
    """Keep only digits and return the EAN if its length is 8 or 13, else None.

    EAN-8 and EAN-13 are the two valid barcode lengths used in retail.
    """
    if not raw_ean:
        return None
    digits = re.sub(r"\D", "", raw_ean)
    if len(digits) in (8, 13):
        return digits
    return None


# ---------------------------------------------------------------------------
# 8. Title hash
# ---------------------------------------------------------------------------

def title_hash(title_norm: str) -> str:
    """Stable short hash of a normalized title: first 12 hex chars of SHA-1."""
    return hashlib.sha1(title_norm.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 9. Match-key builders
# ---------------------------------------------------------------------------
# Each function returns a string in a fixed format that serves as a lookup
# key for grouping potentially identical products.  All inputs are passed
# through the appropriate normalizer before formatting.

def ean_key(ean: str) -> str:
    """Build an EAN match key.  Format: 'ean:{cleaned_ean}'."""
    cleaned = clean_ean(ean)
    if not cleaned:
        return ""
    return f"ean:{cleaned}"


def mpn_root_key(brand_norm: str, mpn_root: str) -> str:
    """Build an mpn_root match key.  Format: 'mpnroot:{brand}:{mpn_root}'.

    Brand is lowercased; mpn_root goes through normalize_identifier.
    """
    b = normalize_brand(brand_norm).lower() if brand_norm else ""
    m = normalize_identifier(mpn_root)
    if not b or not m:
        return ""
    return f"mpnroot:{b}:{m}"


def mpn_key(brand_norm: str, mpn: str) -> str:
    """Build an MPN match key.  Format: 'mpn:{brand}:{mpn}'.

    Brand is lowercased; mpn goes through normalize_identifier.
    """
    b = normalize_brand(brand_norm).lower() if brand_norm else ""
    m = normalize_identifier(mpn)
    if not b or not m:
        return ""
    return f"mpn:{b}:{m}"


def title_key(brand_norm: str, category: str, title_norm: str) -> str:
    """Build a title match key.  Format: 'title:{brand}:{category}:{hash}'.

    Brand and category are lowercased; the title is hashed via title_hash.
    """
    b = brand_norm.lower() if brand_norm else ""
    c = category.lower() if category else ""
    h = title_hash(title_norm) if title_norm else ""
    if not b or not c or not h:
        return ""
    return f"title:{b}:{c}:{h}"
