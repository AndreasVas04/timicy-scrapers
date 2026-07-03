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

from .lexicons import BRAND_ALIASES, COLOR_WORDS, PRODUCT_LINE_ALIAS_KEYS, STORE_NOISE


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Greek capital letters that are visual homoglyphs of Latin capitals.
# Transliterating these before the [^A-Z0-9] strip lets vendor strings like
# "ΧΙΑΟΜΙ" (Greek Chi-Iota-Alpha-Omicron-Mu-Iota) resolve to "XIAOMI" and
# hit the existing Xiaomi alias instead of producing an empty lookup key.
_GREEK_HOMOGLYPH_TABLE = str.maketrans(
    "\u0391\u0392\u0395\u0396\u0397\u0399\u039A\u039C\u039D\u039F\u03A1\u03A4\u03A5\u03A7",
    "ABEZHIKMNOPTYX",
)


def _make_lookup_key(text: str) -> str:
    """Build a brand-lookup key: uppercase, transliterate Greek homoglyphs,
    then strip every non-alphanumeric char."""
    upper = text.upper()
    upper = upper.translate(_GREEK_HOMOGLYPH_TABLE)
    return re.sub(r"[^A-Z0-9]", "", upper)


# Regex matching any character in the Greek and Greek Extended Unicode blocks.
# Used by _has_untransliterated_greek to reject partially-transliterable
# tokens before brand lookup (see extract_brand_from_title).
_RE_GREEK_BLOCK = re.compile(r"[\u0370-\u03FF\u1F00-\u1FFF]")


def _has_untransliterated_greek(text: str) -> bool:
    """Return True if *text* still contains Greek-block characters after
    uppercasing and applying the homoglyph transliteration table.

    This catches partially-mappable Greek tokens like "Πλήρως" (uppercase
    ΠΛΗΡΩΣ) where only a subset of letters have Latin homoglyphs (Η→H,
    Ρ→P).  The remaining Greek letters (Π, Λ, Ω, Σ) would be silently
    stripped by _make_lookup_key's [^A-Z0-9] regex, producing a spurious
    residue (e.g. "HP") that could false-match a real brand alias.

    Fully-mappable tokens like "ΧΙΑΟΜΙ" (all letters have homoglyphs) pass
    this check and are correctly transliterated to "XIAOMI".
    """
    upper = text.upper()
    upper = upper.translate(_GREEK_HOMOGLYPH_TABLE)
    return bool(_RE_GREEK_BLOCK.search(upper))


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

# Regex for volume quantities: a number with dot or comma decimal, optional
# space, then "l" or "ml" at a word boundary. Handles cases like "1,25L",
# "0.5 L", "500ml". Normalizes the decimal separator to a dot.
_RE_VOLUME = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(ml|l)\b",
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


def _replace_volume(m: re.Match) -> str:
    """Convert a volume match to normalized form with dot decimal.

    Does NOT convert ml to l — keeps the original unit, just normalizes
    the decimal separator and removes any space before the unit.
    """
    num = m.group(1).replace(",", ".")
    unit = m.group(2).lower()
    return f"{num}{unit}"


def normalize_units(text: str) -> str:
    """Normalize storage (GB/TB), screen-size, and volume quantities in *text*.

    - Storage: integer + optional space + gb/tb -> "{n}gb" (TB*1024).
    - Screen: number (dot or comma decimal) + inch indicator -> "{n}in".
    - Volume: number (dot or comma decimal) + optional space + l/ml -> "{n}l"
      or "{n}ml" with dot decimal. E.g. "1,25L" -> "1.25l", "0,5 L" -> "0.5l".
    - All other numbers are left untouched.
    """
    text = _RE_STORAGE.sub(_replace_storage, text)
    text = _RE_SCREEN.sub(_replace_screen, text)
    text = _RE_VOLUME.sub(_replace_volume, text)
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
    # Also handles hyphenated brand forms: e.g. vendor "PRO-MOUNTS" has
    # lookup key "PROMOUNTS", but the title text "PRO-MOUNTS" would survive
    # without this extra step. We generate spacing/hyphen variants of each
    # alias key and strip those too, before punctuation removal.
    if brand_norm:
        # Remove canonical brand (lowercased).
        brand_lower = brand_norm.lower()
        brand_pattern = re.compile(
            r"(?<!\w)" + re.escape(strip_accents(brand_lower)) + r"(?!\w)"
        )
        text = brand_pattern.sub("", text)
        # Also remove any raw alias that maps to this brand, plus its
        # common hyphen/space variants derived from the title text.
        for alias_key, canonical in BRAND_ALIASES.items():
            if canonical == brand_norm:
                # Skip product-line aliases — these tokens ("iphone",
                # "galaxy", etc.) are discriminative model info that must
                # remain in the title to keep match_keys stable.
                if alias_key in PRODUCT_LINE_ALIAS_KEYS:
                    continue
                alias_lower = alias_key.lower()
                alias_pat = re.compile(
                    r"(?<!\w)" + re.escape(alias_lower) + r"(?!\w)"
                )
                text = alias_pat.sub("", text)
        # Strip any remaining token that, after removing hyphens/spaces,
        # matches the lookup-normalized brand key. This catches hyphenated
        # forms like "pro-mounts" whose stripped form is "promounts".
        brand_key = _make_lookup_key(brand_norm)
        if brand_key:
            # Match tokens that may contain hyphens/spaces but whose
            # alphanumeric content equals the brand key.
            def _brand_variant_replacer(m: re.Match) -> str:
                candidate = re.sub(r"[^A-Za-z0-9]", "", m.group(0)).upper()
                if candidate == brand_key:
                    return ""
                return m.group(0)
            # Pattern: sequences of word-chars optionally joined by
            # single hyphens or spaces (to catch "pro-mounts", "pro mounts").
            text = re.sub(
                r"(?<!\w)\w+(?:[-\s]\w+)*(?!\w)",
                _brand_variant_replacer,
                text,
            )

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


# ---------------------------------------------------------------------------
# 10. Model-code extraction (pure)
# ---------------------------------------------------------------------------

# Known unit suffixes to exclude from model-code candidates.
# A token like "1024gb" or "800w" is a spec value, not a model code.
# The second group ("bar", "lt", etc.) are non-discriminative spec/quantity
# suffixes found in real data — "15bar", "13lt", "5tmx" are sizes/quantities,
# not manufacturer model codes, and would cause false matches.
_UNIT_SUFFIXES = {
    "gb", "tb", "mb", "kb",
    "mah", "wh", "w", "v", "a",
    "mbps", "gbps", "ghz", "mhz", "hz",
    "in", "mm", "cm", "nm",
    "l", "ml",
    "rpm",
    "k",
    # Spec/quantity suffixes added from data inspection:
    "bar",   # pressure rating (e.g. "15bar")
    "lt",    # liters, alternate abbreviation (e.g. "13lt", "10lt")
    "pin",   # pin count (e.g. "2pin")
    "bit",   # bit depth (e.g. "24bit")
    "pcs",   # piece count (e.g. "100pcs")
    "tmx",   # Greek τεμάχια / pieces (e.g. "5tmx")
}

# Regex to split a token's trailing alphabetic suffix from its numeric prefix,
# used for the unit-suffix exclusion check.
_RE_UNIT_TOKEN = re.compile(r"^(\d+(?:\.\d+)?)([a-z]+)$")


def _contains_greek(token: str) -> bool:
    """Return True if *token* contains any Greek character (U+0370–U+03FF).

    Model codes must be pure Latin alphanumeric to match reliably across
    stores. A code mixing Greek and Latin scripts (e.g. "c1001lβ" where
    the trailing character is Greek beta) will never match the same code
    written in Latin at another store, so it is not a usable signal.
    """
    return any("\u0370" <= ch <= "\u03ff" for ch in token)


def extract_model_codes(text: str) -> list[str]:
    """Pull tokens that look like manufacturer model codes from *text*.

    Model codes (e.g. "scg6050ss", "mg23k3515as", "np-by1") are strong
    cross-language match signals because stores often describe the same
    product in different languages, making model codes the only shared text.

    Processing:
      1. Lowercase and strip accents.
      2. Join alphanumeric segments separated by a single hyphen or slash
         into one token (so "np-by1" becomes "npby1").
      3. Split on whitespace.
      4. A token qualifies if ALL of the following hold:
         - alphanumeric-only (after the join step),
         - length >= 4,
         - contains at least one Latin letter AND one digit,
         - contains NO Greek characters (mixed-script tokens are not
           usable as cross-store match signals),
         - is NOT a known unit/spec token (e.g. "1024gb", "800w", "15bar").

    Returns a de-duplicated list in first-seen order.

    Known limitation: codes that appear space-separated in the source
    (e.g. "SWK 2511BK" -> "swk 2511bk") are NOT joined and will be missed
    by this single-token rule. Rejoining is deliberately left to the matcher
    stage where brand/category context is available to do it safely.
    """
    if not text:
        return []

    # Lowercase and strip accents to normalize the input.
    t = strip_accents(text.lower())

    # Join alphanumeric segments separated by a single hyphen or slash.
    # "np-by1" -> "npby1", "abc/def" -> "abcdef", but "a--b" stays as-is.
    t = re.sub(r"([a-z0-9])[-/]([a-z0-9])", r"\1\2", t)

    tokens = t.split()
    seen: set[str] = set()
    result: list[str] = []

    for tok in tokens:
        # Must be purely alphanumeric after the join step.
        if not tok.isalnum():
            continue
        # Must be at least 4 characters.
        if len(tok) < 4:
            continue
        # Reject tokens containing Greek characters — mixed-script tokens
        # will never match the same code in Latin at another store.
        if _contains_greek(tok):
            continue
        # Must contain at least one letter and one digit.
        has_alpha = any(c.isalpha() for c in tok)
        has_digit = any(c.isdigit() for c in tok)
        if not (has_alpha and has_digit):
            continue
        # Exclude tokens whose alphabetic suffix is a known unit or spec
        # quantity (gb, w, mah, bar, lt, tmx, etc.).
        m = _RE_UNIT_TOKEN.match(tok)
        if m and m.group(2) in _UNIT_SUFFIXES:
            continue
        # De-duplicate, preserving first-seen order.
        if tok not in seen:
            seen.add(tok)
            result.append(tok)

    return result


# ---------------------------------------------------------------------------
# 11. Brand-trust primitives (pure, read-only inspection helpers)
# ---------------------------------------------------------------------------
# These functions only inspect data — they make no decisions and write nothing.
# They are intended for a future matcher to consume.

def known_canonical_brands() -> set[str]:
    """Return the set of canonical brand strings from BRAND_ALIASES, lowercased.

    Useful for quickly checking whether a brand value is recognized.
    """
    return {v.lower() for v in BRAND_ALIASES.values()}


def extract_brand_from_title(raw_title: str | None) -> str | None:
    """Try to find a known brand in the product title.

    Lowercase + strip accents the title, tokenize on whitespace, and for
    each token build the brand lookup key (uppercase, non-alphanumeric
    removed). Return the canonical brand of the FIRST token that resolves
    to a known brand, or None if no brand is found.

    Example:
        "Αποχυμωτής Αργής Σύνθλιψης Sencor ssj4050np" -> "Sencor"
    """
    if not raw_title:
        return None

    text = strip_accents(raw_title.lower())
    tokens = text.split()

    for tok in tokens:
        # Skip tokens that contain Greek characters which survive homoglyph
        # transliteration.  Such tokens are only partially mappable to Latin
        # (e.g. "Πλήρως" → residue "HP") and would produce spurious brand
        # matches.  Fully-mappable Greek tokens (e.g. "ΧΙΑΟΜΙ" → "XIAOMI")
        # pass this check and are correctly looked up.
        if _has_untransliterated_greek(tok):
            continue
        key = _make_lookup_key(tok)
        if key and key in BRAND_ALIASES:
            return BRAND_ALIASES[key]

    return None


def looks_suspicious_brand(brand_norm: str, raw_title: str | None) -> bool:
    """Return True if the brand value looks wrong and should be reviewed.

    Flags the brand as suspicious if ANY of these hold:
      - brand_norm is empty,
      - brand_norm (lowercased) is NOT a known canonical brand AND the title
        contains a known brand that differs from it.

    Conservative by design — only flags when the title clearly offers a
    better-known brand than the vendor field. This avoids false positives
    on legitimate brands that simply aren't in BRAND_ALIASES yet.

    Examples:
        ("SSJ", "...Sencor ssj4050np")  -> True  (SSJ unknown, Sencor in title)
        ("Apple", "iPhone 15 128GB")    -> False  (Apple is known)
        ("", "anything")                -> True   (empty brand)
    """
    if not brand_norm or not brand_norm.strip():
        return True

    # If the brand is already a known canonical brand, trust it.
    if brand_norm.lower() in known_canonical_brands():
        return False

    # Brand is unknown — check if the title contains a better-known one.
    title_brand = extract_brand_from_title(raw_title)
    if title_brand and title_brand.lower() != brand_norm.lower():
        return True

    return False
