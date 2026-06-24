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
    "XIAOMI": "Xiaomi",
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
    "PHILIPS": "Philips",

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
    "LOGITECH": "Logitech",

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
}


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
    "space gray",
    "space grey",
    "sierra blue",
    "alpine green",
    "deep purple",
    "natural titanium",
    "blue titanium",
    "black titanium",
    "white titanium",
    "desert titanium",
    "titanium",
    "τιτανιο",     # titanium (Greek)
    "graphite",
    "midnight",
    "starlight",
    "product red",
    "phantom black",
    "phantom white",
    "phantom",
    "mystic bronze",
    "cream",
    "lavender",
    "burgundy",
    "lime",
    "mint",
    "ice blue",
    "sky blue",
    "ocean blue",
    "forest green",
    "sage green",
    "rose gold",
    "matte black",
    "jet black",
    "ceramic white",
    "obsidian",
    "hazel",
    "porcelain",
    "charcoal",
    "chalk",
    "sorta sage",
    "cloudy white",
    "stormy black",
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
