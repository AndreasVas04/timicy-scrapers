"""
lexicons.py
-----------
Editable data tables consumed by normalize.py.

All data here is plain Python literals — no I/O, no imports beyond builtins.
Extend any dict / set by simply adding entries; the normalization code picks
them up automatically.
"""

# ---------------------------------------------------------------------------
# 1. BRAND_ALIASES
# ---------------------------------------------------------------------------
# Maps a *lookup key* to the canonical brand string.
#
# The lookup key is the raw brand string uppercased with every non-alphanumeric
# character removed.  This means "De'Longhi", "DELONGHI", and "de longhi" all
# collapse to the key "DELONGHI".
#
# To add a new brand or variant: insert a new entry whose key is the
# uppercased, stripped form and whose value is the display-ready canonical name.
# ---------------------------------------------------------------------------
BRAND_ALIASES: dict[str, str] = {
    # Apple
    "APPLE": "Apple",

    # Samsung
    "SAMSUNG": "Samsung",
    "SAMSUNGELECTRONICS": "Samsung",

    # LG
    "LG": "LG",
    "LGELECTRONICS": "LG",

    # Sony
    "SONY": "Sony",

    # Xiaomi
    # "Xiaomi Mijia" is a home/IoT sub-brand of Xiaomi; collapsing it keeps
    # Mijia-branded products in the same block as other Xiaomi products.
    "XIAOMI": "Xiaomi",
    "XIAOMIMIJIA": "Xiaomi",
    "MI": "Xiaomi",
    "REDMI": "Xiaomi",
    "POCO": "Xiaomi",

    # Huawei / Honor
    "HUAWEI": "Huawei",
    "HONOR": "Honor",

    # Lenovo
    "LENOVO": "Lenovo",

    # HP
    "HP": "HP",
    "HEWLETTPACKARD": "HP",

    # Dell
    "DELL": "Dell",

    # Asus
    "ASUS": "ASUS",
    "ASUSTEK": "ASUS",

    # Acer
    "ACER": "Acer",

    # MSI
    "MSI": "MSI",
    "MICROSTAR": "MSI",

    # Microsoft
    "MICROSOFT": "Microsoft",

    # Google
    "GOOGLE": "Google",

    # JBL
    "JBL": "JBL",

    # Bose
    "BOSE": "Bose",

    # Sennheiser
    "SENNHEISER": "Sennheiser",

    # DeLonghi
    "DELONGHI": "De'Longhi",
    "BELONGHI": "De'Longhi",  # common OCR / typo variant

    # Nespresso
    "NESPRESSO": "Nespresso",

    # Philips
    # "Philips Hue" is a smart-lighting sub-brand of Philips; collapsing it
    # ensures Hue products land in the same (category, effective_brand) block
    # as other Philips products for matching purposes.
    "PHILIPS": "Philips",
    "PHILIPSHUE": "Philips",

    # Bosch
    "BOSCH": "Bosch",

    # Siemens
    "SIEMENS": "Siemens",

    # Whirlpool
    "WHIRLPOOL": "Whirlpool",

    # AEG
    "AEG": "AEG",

    # Electrolux
    "ELECTROLUX": "Electrolux",

    # Beko
    "BEKO": "Beko",

    # Toshiba
    "TOSHIBA": "Toshiba",

    # Panasonic
    "PANASONIC": "Panasonic",

    # Dyson
    "DYSON": "Dyson",

    # Tefal / T-fal
    "TEFAL": "Tefal",
    "TFAL": "Tefal",

    # Canon
    "CANON": "Canon",

    # Nikon
    "NIKON": "Nikon",

    # GoPro
    "GOPRO": "GoPro",

    # Garmin
    "GARMIN": "Garmin",

    # Nintendo
    "NINTENDO": "Nintendo",

    # OnePlus
    "ONEPLUS": "OnePlus",

    # Oppo
    "OPPO": "OPPO",

    # Realme
    "REALME": "Realme",

    # TCL
    "TCL": "TCL",

    # Hisense
    "HISENSE": "Hisense",

    # Logitech
    # "Logitech G" is the gaming sub-brand of Logitech; collapsing it ensures
    # gaming peripherals land in the same block as other Logitech products.
    # Note: the lookup key "LOGITECHG" matches only the exact brand string
    # "Logitech G" — a product code like "Logitech G502" produces key
    # "LOGITECHG502" which does NOT match, so no false positives.
    "LOGITECH": "Logitech",
    "LOGITECHG": "Logitech",

    # Marshall
    "MARSHALL": "Marshall",

    # Bang & Olufsen
    "BANGOLUFSEN": "Bang & Olufsen",
    "BO": "Bang & Olufsen",

    # Sonos
    "SONOS": "Sonos",

    # Amazon
    "AMAZON": "Amazon",

    # Motorola
    "MOTOROLA": "Motorola",

    # Nothing
    "NOTHING": "Nothing",

    # Haier
    "HAIER": "Haier",

    # Midea
    "MIDEA": "Midea",

    # Inventor
    "INVENTOR": "Inventor",

    # Candy
    "CANDY": "Candy",

    # Hoover
    "HOOVER": "Hoover",

    # iRobot
    "IROBOT": "iRobot",

    # Roborock
    "ROBOROCK": "Roborock",

    # Ecovacs
    "ECOVACS": "Ecovacs",

    # Breville / Sage
    "BREVILLE": "Breville",
    "SAGE": "Sage",

    # Sencor
    "SENCOR": "Sencor",

    # Pro-Mounts
    "PROMOUNTS": "Pro-Mounts",

    # Bowers & Wilkins
    "BOWERSWILKINS": "Bowers & Wilkins",
    "BW": "Bowers & Wilkins",

    # Fresh 'n Rebel
    "FRESHNREBEL": "Fresh 'n Rebel",

    # OTL
    "OTL": "OTL",

    # -----------------------------------------------------------------------
    # Vendor-data defect aliases
    # -----------------------------------------------------------------------
    # These map real-world vendor-data defects — typos, HTML entity artefacts,
    # and product lines incorrectly used as the vendor name — to the correct
    # canonical brand.  Each entry was observed in production scraper output.
    "PHLIPS": "Philips",                       # vendor typo (Electroline)
    "BANGAMPOLUFSENBAMPO": "Bang & Olufsen",   # HTML &amp; entities (Stephanis)
    "BOWERSAMPWILKINSBAMPW": "Bowers & Wilkins",  # HTML &amp; entities (Stephanis)
    "FRESHANDREBEL": "Fresh 'n Rebel",         # "FRESH AND REBEL" variant (Public)
    "OTLTECHNOLOGIES": "OTL",                  # "OTL Technologies" variant
    "IPHONE": "Apple",                         # product line used as vendor (Electroline)
    "AIRPODS": "Apple",                        # product line used as vendor
    "GALAXY": "Samsung",                       # product line used as vendor
}

# ---------------------------------------------------------------------------
# Product-line alias keys that must NOT be stripped from titles
# ---------------------------------------------------------------------------
# These lookup keys map product lines used as vendor values (e.g. "IPHONE" →
# "Apple") to their real brand for vendor resolution.  However, the tokens
# themselves are discriminative product-model information ("iphone", "airpods",
# "galaxy") and must NEVER be stripped from titles — stripping them would
# change normalized_title and destabilize existing match_keys.
#
# REDMI is intentionally NOT in this set — it predates this change and
# existing keys already reflect its stripping.
# ---------------------------------------------------------------------------
PRODUCT_LINE_ALIAS_KEYS: frozenset[str] = frozenset({
    "IPHONE", "AIRPODS", "GALAXY",
})


# ---------------------------------------------------------------------------
# 2. COLOR_WORDS
# ---------------------------------------------------------------------------
# Bilingual (Greek + English) color and finish tokens to strip from titles.
# Multi-word phrases are included so that "space gray" is removed as a unit
# before the single word "gray" is tried.
#
# This list is expected to grow as new device finishes appear each year.
# ---------------------------------------------------------------------------
COLOR_WORDS: set[str] = {
    # --- Basic colors (English) ---
    "black",
    "white",
    "blue",
    "red",
    "green",
    "silver",
    "gold",
    "grey",
    "gray",
    "pink",
    "purple",
    "yellow",
    "orange",
    "brown",
    "beige",
    "cream",
    "ivory",
    "bronze",
    "copper",
    "coral",

    # --- Basic colors (Greek) ---
    "μαυρο",       # black
    "λευκο",       # white
    "μπλε",        # blue
    "κοκκινο",     # red
    "πρασινο",     # green
    "ασημι",       # silver
    "χρυσο",       # gold
    "γκρι",        # grey
    "ροζ",         # pink
    "μωβ",         # purple
    "κιτρινο",     # yellow
    "πορτοκαλι",   # orange
    "καφε",        # brown
    "μπεζ",        # beige

    # --- Vendor finishes (Apple / Samsung / Google etc.) ---
    # This list grows with periodic data inspection — add entries confirmed in
    # real scraper output. Perfection is not required here: the fuzzy title
    # matching tier is the intended backstop for any color that still leaks.
    # Multi-word phrases are matched longest-first at runtime, so adding a
    # full phrase (e.g. "deep blue") is safe even when its modifier word
    # ("deep") should NOT be stripped on its own.

    # -- Multi-word finish phrases (full phrases only; risky standalone
    #    modifiers like deep/space/light/shadow are NOT added solo) --
    "space gray",
    "space grey",
    "space black",
    "sierra blue",
    "pacific blue",
    "alpine green",
    "deep purple",
    "deep blue",
    "deep navy",
    "baltic blue",
    "anchor blue",
    "electric lavender",
    "soft pink",
    "bright guava",
    "light moss",
    "cosmic orange",
    "silver shadow",
    "natural titanium",
    "blue titanium",
    "black titanium",
    "white titanium",
    "desert titanium",
    "titanium",
    "τιτανιο",     # titanium (Greek)
    "product red",
    "phantom black",
    "phantom white",
    "phantom",
    "mystic bronze",
    "ice blue",
    "sky blue",
    "ocean blue",
    "forest green",
    "sage green",
    "rose gold",
    "matte black",
    "jet black",
    "ceramic white",
    "cloudy white",
    "stormy black",
    "sorta sage",

    # -- Single-word finishes (confirmed safe — never part of a model name
    #    in this catalog, verified via scraper data inspection) --
    "graphite",
    "midnight",
    "starlight",
    "obsidian",
    "hazel",
    "porcelain",
    "charcoal",
    "chalk",
    "cream",
    "lavender",
    "burgundy",
    "lime",
    "mint",
    "clear",
    "lilac",
    "desert stone",
    "inox",
    "alpine",
    "ocean",
    "ultramarine",
    "teal",
    "navy",
    "icyblue",
    "peach",
    "indigo",
    "blush",
    "citrus",
    "vanilla",
    "guava",
    "tangerine",
    "cosmic",
    "lila",

    # Samsung marketing prefix — appears as "Awesome Black/White/Lime/etc."
    # Safe to strip standalone in this catalog.
    "awesome",
}


# ---------------------------------------------------------------------------
# 3. STORE_NOISE
# ---------------------------------------------------------------------------
# Marketing / store-specific tokens and phrases to remove from titles.
# Order does not matter — they are tried longest-first at runtime.
#
# Extend freely; these are matched case-insensitively after accent stripping.
# ---------------------------------------------------------------------------
STORE_NOISE: list[str] = [
    # Store names that appear in product titles
    "stephanis",
    "kotsovolos",
    "public",
    "istorm",
    "electroline",
    "bionic",

    # Common marketing / ecommerce filler (English)
    "online",
    "buy now",
    "shop now",
    "best price",
    "free shipping",
    "new arrival",
    "official",
    "original",
    "genuine",
    "brand new",
    "in stock",

    # Common marketing filler (Greek)
    "αγορα",        # buy / purchase
    "προσφορα",     # offer
    "εκπτωση",      # discount
    "δωρεαν",       # free
    "αποστολη",     # shipping
    "επισημο",      # official
    "εγγυηση",      # warranty
    "τιμη",         # price

    # Punctuation-like separators often embedded in titles
    "|",
    "–",
    "—",
    "·",
]
