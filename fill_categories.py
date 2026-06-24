"""
fill_categories.py
==================

Fills the `canonical_category` column of category_mapping.csv.

Background
----------
`extract_categories.py` produces a CSV with one row per distinct
(store, raw_product_type) combination found in the scraped JSON files:

    store,raw_product_type,product_count,canonical_category

The last column is left empty so that a human can map each raw store
category to one of the project's canonical categories. Doing that by hand
for ~1,500 rows is slow and error-prone, so this script does the bulk of
the work using a curated lookup table (see CATEGORY_MAP below).

Two hard rules drive the design:

1. The `raw_product_type` value MUST stay exactly as it was written by the
   scrapers, because the ingestion step looks each product's product_type
   up against this exact string. This script therefore NEVER rewrites the
   raw value; it only writes into the empty `canonical_category` column.

2. Anything we are not confident about is left empty on purpose. An empty
   `canonical_category` means "exclude this raw category from the catalog"
   (the ingestion step skips empty rows). It is much safer to drop a fuzzy
   category than to file products under the wrong one.

Matching is done on a normalized "fingerprint" of the text rather than on
an exact string match. This is needed because some store categories contain
Latin look-alike capitals inside otherwise-Greek words (e.g. a Latin "K"
instead of a Greek kappa) and inconsistent accents. The fingerprint folds
both alphabets and strips accents/spacing so equivalent labels line up.

Usage
-----
    python3 fill_categories.py                 # edits ./category_mapping.csv in place
    python3 fill_categories.py path/to/file.csv

A one-time backup of the original file is written next to it as
"<name>.bak" so the unfilled version is never lost.
"""

import csv
import os
import sys
import unicodedata
from collections import defaultdict


# ---------------------------------------------------------------------------
# The 21 canonical categories used across the whole project. Keeping them in
# one place lets us validate that every value we assign is spelled correctly.
# ---------------------------------------------------------------------------
CANONICAL_CATEGORIES = {
    # Tech
    "smartphones", "laptops", "tablets", "desktops", "monitors", "tvs",
    "smartwatches", "headphones", "speakers", "consoles", "cameras",
    "smart_home",
    # Home appliances
    "refrigerators", "washing_machines", "dryers", "dishwashers", "ovens",
    "air_conditioners", "vacuums", "coffee_machines", "air_fryers",
}


# ---------------------------------------------------------------------------
# Greek -> Latin transliteration table. Used by fingerprint() so that a label
# written with Greek letters and the same label written with Latin look-alike
# letters collapse to the same comparison key.
# ---------------------------------------------------------------------------
GREEK_TO_LATIN = {
    "α": "a", "β": "b", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i",
    "θ": "th", "ι": "i", "κ": "k", "λ": "l", "μ": "m", "ν": "n", "ξ": "x",
    "ο": "o", "π": "p", "ρ": "r", "σ": "s", "ς": "s", "τ": "t", "υ": "y",
    "φ": "f", "χ": "x", "ψ": "ps", "ω": "o",
}


def fingerprint(text):
    """Return a charset/accent/spacing-insensitive comparison key for `text`.

    Steps:
      1. Decompose accents (NFD) and drop the combining accent marks.
      2. Lower-case everything (Greek capitals become Greek lower-case).
      3. Transliterate Greek letters to Latin; let Latin letters and digits
         pass through; throw away spaces, punctuation and symbols.

    The result is a compact ASCII string, e.g.:
        "Ψυγειοκαταψύκτες"      -> "psygeiokatapsyktes"
        "Kλιματιστικά Inverter" -> "klimatistikainverter"  (Latin K folds in)
    """
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.lower()

    out = []
    for ch in text:
        if ch in GREEK_TO_LATIN:
            out.append(GREEK_TO_LATIN[ch])
        elif ch.isalnum():          # plain Latin letters and digits
            out.append(ch)
        # everything else (spaces, dashes, slashes, &, …) is dropped
    return "".join(out)


# ---------------------------------------------------------------------------
# The curated lookup table.
#
# It is keyed by store so that an ambiguous label can be treated differently
# from shop to shop (for example a bare "Δαπέδου" means a floor air-conditioner
# at one store but is just a fan elsewhere). Each entry is:
#
#       (raw_label_as_seen, canonical_category)
#
# The raw labels are written here in clean Greek/English; fingerprint() makes
# them match the real strings even when those contain Latin look-alikes.
#
# Anything NOT listed here stays empty on purpose -> excluded from the catalog
# (accessories, cables, cases, toys, party/seasonal goods, kitchenware, power
# tools, garden, beauty, networking, printers, storage, peripherals, heaters,
# fans, small kitchen gadgets that are outside the 21 categories, etc.).
# ---------------------------------------------------------------------------
CATEGORY_MAP = {
    "istorm": [
        ("iPad", "tablets"),
        ("Mac", "laptops"),                       # mixed MacBook/iMac/Mac mini
        ("Apple Watch", "smartwatches"),
        ("Apple Watch Series 9", "smartwatches"),
        ("Smart Home", "smart_home"),
        ("Ηχεία", "speakers"),
        ("iPhone", "smartphones"),
        ("Ακουστικά", "headphones"),
        ("AirPods", "headphones"),
        ("Οθόνες", "monitors"),
        ("Action cameras", "cameras"),
    ],

    "kotsovolos": [
        ("Τηλεοράσεις", "tvs"),
        ("Πλυντήρια Ρούχων", "washing_machines"),
        ("Smartphones & iPhone", "smartphones"),
        ("Smartwatches", "smartwatches"),
        ("Ακουστικά Κεφαλής", "headphones"),
        ("Ακουστικά Earbuds", "headphones"),
        ("Φορητά Ηχεία Bluetooth", "speakers"),
        ("Ψυγειοκαταψύκτες", "refrigerators"),
        ("Φούρνοι", "ovens"),
        ("Εστίες", "ovens"),
        ("Set Εντοιχισμού", "ovens"),
        ("Κλιματιστικά Inverter", "air_conditioners"),
        ("Laptop - MacBook", "laptops"),
        ("Tablets - iPad", "tablets"),
        ("Εντοιχιζόμενα Πλυντήρια Πιάτων", "dishwashers"),
        ("All in One - PCs – Mac", "desktops"),
        ("Έξυπνες Λάμπες", "smart_home"),
        ("Πλυντήρια Πιάτων", "dishwashers"),
        ("Κουζίνες", "ovens"),
        ("Ψυγεία", "refrigerators"),
        ("Φούρνοι Μικροκυμάτων", "ovens"),
        ("Σκούπες Stick", "vacuums"),
        ("Soundbars", "speakers"),
        ("Στεγνωτήρια", "dryers"),
        ("Πλυντήρια-Στεγνωτήρια", "washing_machines"),
        ("Εντοιχιζόμενοι Ψυγειοκαταψύκτες", "refrigerators"),
        ("Ηχεία - Speakers", "speakers"),
        ("Σκούπες Ρομπότ", "vacuums"),
        ("Ακουστικά Ψείρες", "headphones"),
        ("Air Fryers – Πολυμάγειρες", "air_fryers"),
        ("IP Κάμερα", "smart_home"),
        ("Action cameras", "cameras"),
        ("Εντοιχιζόμενα Ψυγεία", "refrigerators"),
        ("Έξυπνοι Αισθητήρες & Πρίζες", "smart_home"),
        ("Gaming Headsets", "headphones"),
        ("Καταψύκτες", "refrigerators"),
        ("Συντηρητές Κρασιών", "refrigerators"),
        ("Ηλεκτρικές Σκούπες", "vacuums"),
        ("Compact", "cameras"),
        ("Ηχεία Hi Power", "speakers"),
        ("Activity Trackers", "smartwatches"),
        ("Καφετιέρες Φίλτρου", "coffee_machines"),
        ("Instant", "cameras"),
        ("Αυτόματες Μηχανές Espresso", "coffee_machines"),
        ("Μονόπορτα ψυγεία - Mini Bars", "refrigerators"),
        ("Σκουπάκια", "vacuums"),
        ("Mirrorless", "cameras"),
        ("Εντοιχιζόμενοι Καταψύκτες", "refrigerators"),
        ("Κινητά Τηλέφωνα", "smartphones"),
        ("Ηχεία", "speakers"),
        ("Μηχανές Espresso - Με Κάψουλα", "coffee_machines"),
        ("Ακουστικά - Headsets", "headphones"),
        ("Χειροκίνητες Μηχανές Espresso", "coffee_machines"),
        ("Συσκευές Φραπέ", "coffee_machines"),
        ("Σκούπες Stick Wet & Dry", "vacuums"),
        ("Bluetooth Neckband", "headphones"),
        ("Μηχανές Ροφημάτων", "coffee_machines"),
        ("Καφετιέρες", "coffee_machines"),
        ("Hi Fi", "speakers"),
        ("Θερμοηλεκτρικά Ψυγεία", "refrigerators"),
        ("Κλιμαστικά Multi", "air_conditioners"),   # note: store's own typo
        ("Κλιματιστικά", "air_conditioners"),
        ("Φορητά Κλιματιστικά", "air_conditioners"),
        ("Όλες οι οθόνες", "monitors"),
        ("Εντοιχιζόμενα Πλυντήρια Ρούχων", "washing_machines"),
        ("Μηχανές Κυπριακού Καφέ", "coffee_machines"),
        ("Κονσόλες PS5", "consoles"),
        ("Κονσόλες Nintendo Switch 2", "consoles"),
        ("Φουρνάκια – Κουζινάκια", "ovens"),
        ("Επιτραπέζιες Εστίες", "ovens"),
        ("DSLR", "cameras"),
        ("Ακουστικά Ύπνου", "headphones"),
        ("Εντοιχιζόμενα Πλυντήρια - Στεγνωτήρια", "washing_machines"),
        ("Κονσόλες Nintendo Switch", "consoles"),
        ("Bluetooth Headsets", "headphones"),
        ("Ψυγεία Βιτρίνες", "refrigerators"),
        ("Φορητές Gaming Κονσόλες", "consoles"),
        ("Πλυντήρια - Στεγνωτήρια", "washing_machines"),
        ("Οθόνες Multimedia", "monitors"),
        ("Όλοι οι φούρνοι μικροκυμάτων", "ovens"),
        ("Κονσόλες Xbox Series S | X", "consoles"),
        ("Home Cinema", "speakers"),
        ("Wearables", "smartwatches"),
    ],

    "stephanis": [
        ("Bluetooth Headsets", "headphones"),
        ("Smartphones", "smartphones"),
        ("Laptops", "laptops"),
        ("Portable Speakers", "speakers"),
        ("Earphones with Microphone", "headphones"),
        ("Gaming Headphones/Earphones", "headphones"),
        ("Smartwatches", "smartwatches"),
        ("Headphones", "headphones"),
        ("Television", "tvs"),
        ("PC Monitors", "monitors"),
        ("Tablets", "tablets"),
        ("Party Speakers", "speakers"),
        ("Compact cameras", "cameras"),
        ("Coffee Machines", "coffee_machines"),
        ("Upright Vacuum/Mop Cleaners", "vacuums"),
        ("Refrigerators", "refrigerators"),
        ("Sound Bars", "speakers"),
        ("Robotic Vacuum & Mopping Cleaners", "vacuums"),
        ("IP Cameras", "smart_home"),
        ("PC", "desktops"),
        ("Fryers", "air_fryers"),
        ("Cylinder Vacuum Cleaners", "vacuums"),
        ("Feature phones", "smartphones"),
        ("Air Conditioners", "air_conditioners"),
        ("Desktop PCs", "desktops"),
        ("Washing Machines", "washing_machines"),
        ("Action Cameras", "cameras"),
        ("Earphones", "headphones"),
        ("Ovens", "ovens"),
        ("Microwaves", "ovens"),
        ("Hobs", "ovens"),
        ("PC Speakers", "speakers"),
        ("Cooker", "ovens"),
        ("Mirrorless cameras", "cameras"),
        ("Fitness Bands", "smartwatches"),
        ("Hi-Fi", "speakers"),
        ("All in One PCs", "desktops"),
        ("Frappe Mixers", "coffee_machines"),
        ("Headphones Multimedia", "headphones"),
        ("Gaming Speakers", "speakers"),
        ("Freestanding Dishwashers", "dishwashers"),
        ("Side by Side", "refrigerators"),
        ("Mini Vacuum Cleaners", "vacuums"),
        ("Portable Hobs", "ovens"),
        # Deliberate product-owner choice: map this small (14-item) bucket to
        # consoles. Sampled contents were ~6 genuine retro/mini consoles
        # (A500/C64/Atari/SEGA minis) plus ~8 Tamagotchi-style handheld devices;
        # all are treated as console/handheld hardware for the catalog.
        ("Retro", "consoles"),
        ("Tumble Dryer", "dryers"),
        ("Freezers", "refrigerators"),
        ("Built-in Dishwashers", "dishwashers"),
        ("Washer Dryers", "washing_machines"),
        ("Built-in Microwaves", "ovens"),
        ("Free Standing Wine Conditioning Units", "refrigerators"),
        ("Portable Refrigerators", "refrigerators"),
        ("Smart Rings", "smartwatches"),
        ("Undercounter & Mini bar", "refrigerators"),
        ("DSLR cameras", "cameras"),
        ("Fridges w/o freezer", "refrigerators"),
        ("Robot Cleaning Devices", "vacuums"),
        ("Home Cinema", "speakers"),
        ("Cookers", "ovens"),
    ],

    "electroline": [
        ("Κινητά-Smartphones", "smartphones"),
        ("Smartwatches", "smartwatches"),
        ("Εντοιχιζόμενες Εστίες", "ovens"),
        ("Τηλεοράσεις", "tvs"),
        ("Ηχεία Bluetooth", "speakers"),
        ("True Wireless", "headphones"),
        ("Φούρνοι", "ovens"),
        ("On/Over Ακουστικά", "headphones"),
        ("Καφετιέρες Espresso", "coffee_machines"),
        ("Κλιματιστικά Inverter", "air_conditioners"),
        ("Φορητοί Υπολογιστές", "laptops"),
        ("Πλυντήρια", "washing_machines"),
        ("Εντοιχιζόμενα Πλυντήρια Πιάτων", "dishwashers"),
        ("Έξυπνος LED Φωτισμός", "smart_home"),
        ("Μπάρες Ηχείων", "speakers"),
        ("Ηχεία Καραόκε", "speakers"),
        ("Ψυγειοκαταψύκτες", "refrigerators"),
        ("Δίπορτα", "refrigerators"),
        ("Ηλεκτρικές Σκούπες", "vacuums"),
        ("In Ear Ακουστικά", "headphones"),
        ("IP Smart Κάμερες Εξωτερικού Χώρου", "smart_home"),
        ("Εντοιχιζόμενοι Ψυγειοκαταψύκτες", "refrigerators"),
        ("Gaming Οθόνες", "monitors"),
        ("Σκούπες Stick", "vacuums"),
        ("Φούρνοι Μικροκυμάτων", "ovens"),
        ("Τετράπορτα", "refrigerators"),
        ("Instant Κάμερες", "cameras"),
        ("Φριτέζες Λαδιού & Αέρος", "air_fryers"),
        ("Side-by-Side", "refrigerators"),
        ("Action Κάμερες", "cameras"),
        ("Στεγνωτήρια", "dryers"),
        ("Gaming Ακουστικά", "headphones"),
        ("Οθόνες", "monitors"),
        ("Αθλητικά Ακουστικά", "headphones"),
        ("Παιδικά Ακουστικά", "headphones"),
        ("Gaming Laptops", "laptops"),
        ("Εντοιχιζόμενες Καφετιέρες", "coffee_machines"),
        ("IP Smart Κάμερες Εσωτερικού Χώρου", "smart_home"),
        ("Φραπεδιέρες", "coffee_machines"),
        ("Ρομποτικές Σκούπες", "vacuums"),
        ("Εντοιχιζόμενα Ψυγεία", "refrigerators"),
        ("Μονόπορτα", "refrigerators"),
        ("Πλυντήρια & Στεγνωτήρια", "washing_machines"),
        ("Καφετιέρες Φίλτρου", "coffee_machines"),
        ("Ψυγεία/Καταψύκτες Twins", "refrigerators"),
        ("Κινητά Απλής Χρήσης", "smartphones"),
        ("Καφετιέρες με Κάψουλες", "coffee_machines"),
        ("Κονσόλες", "consoles"),
        ("Ακουστικά", "headphones"),
        ("Αισθητήρες", "smart_home"),
        ("Επιτραπέζιοι Υπολογιστές", "desktops"),
        ("Καφετιέρες Κυπριακού Καφέ", "coffee_machines"),
        ("Εντοιχιζόμενοι Συντηρητές Κρασιών", "refrigerators"),
        ("Φούρνοι Πίτσας", "ovens"),
        ("Ηχεία", "speakers"),
        ("Συντηρητές Κρασιών Single Zone", "refrigerators"),
        ("All-In-One", "desktops"),
        ("Πολυμάγειρες", "air_fryers"),
        ("Digital Κάμερες", "cameras"),
        ("Φουρνάκια", "ovens"),
        ("Σταθερά Ηχεία", "speakers"),
        ("HI-FI Micro", "speakers"),
        ("Gaming Ηχεία", "speakers"),
        ("Ψυγεία French Door", "refrigerators"),
        ("Συντηρητές Κρασιών Dual Zone", "refrigerators"),
        ("Φορητές Εστίες", "ovens"),
        ("Υπογούφερ", "speakers"),
        ("Έξυπνα Θυροτηλέφωνα", "smart_home"),
        ("Έξυπνα Ηχεία", "speakers"),
        ("Έξυπνοι LED Λαμπτήρες", "smart_home"),
        ("Εντοιχιζόμενα Πλυντήρια", "washing_machines"),
        ("Κλιματιστικά Κασέτα", "air_conditioners"),
        ("Επαγωγικές", "ovens"),
        ("Bluetooth Headset", "headphones"),
        ("Έξυπνες Πρίζες", "smart_home"),
        ("Κλιματιστικά Φορητά", "air_conditioners"),
        ("Κλιματιστικά Επιδαπέδια", "air_conditioners"),
        ("Καναλάτο", "air_conditioners"),
        ("Κλιματιστικά Καναλάτα", "air_conditioners"),
        ("Κλιματιστικά Δαπέδου/Οροφής", "air_conditioners"),
        ("Φούρνοι Ατμού", "ovens"),
        ("Εντοιχιζόμενα Πλυντήρια/Στεγνωτήρια", "washing_machines"),
        ("ΜΟΝΟΠΟΡΤΑ", "refrigerators"),
        ("Σετ Εντοιχισμού", "ovens"),
        ("Συντηρητές Κρασιών 3 Zone", "refrigerators"),
        ("Πλυντήρια Ρούχων", "washing_machines"),
        ("ΨΥΓΕΙΟΚΑΤΑΨΥΚΤΕΣ", "refrigerators"),
        ("Εστίες Υγραερίου", "ovens"),
        ("Έξυπνα Συστήματα Ασφαλείας Σετ", "smart_home"),
        ("Ακουστικά/Ψείρες", "headphones"),
    ],

    "public": [
        ("Smartphones", "smartphones"),
        ("Τηλεοράσεις", "tvs"),
        ("Tablets", "tablets"),
        ("Smartwatches", "smartwatches"),
        ("Laptops", "laptops"),
        ("Ακουστικά", "headphones"),
        ("Gaming Headsets", "headphones"),
        ("Φορητά Ηχεία", "speakers"),
        ("Εντοιχιζόμενοι Φούρνοι & Κουζίνες", "ovens"),
        ("Ψυγειοκαταψύκτες", "refrigerators"),
        ("Εντοιχιζόμενες Εστίες", "ovens"),
        ("Κλιματιστικά Τοίχου", "air_conditioners"),
        ("Πλυντήρια Ρούχων", "washing_machines"),
        ("Οθόνες", "monitors"),
        ("Σκούπες Stick", "vacuums"),
        ("IP Cameras", "smart_home"),
        ("Μηχανές Espresso", "coffee_machines"),
        ("Έξυπνος Φωτισμός", "smart_home"),
        ("Soundbars", "speakers"),
        ("Στεγνωτήρια", "dryers"),
        ("Δαπέδου", "air_conditioners"),
        ("Ηλεκτρικές Σκούπες", "vacuums"),
        ("Φριτέζες", "air_fryers"),
        ("Ηχεία Υπολογιστή", "speakers"),
        ("Πλυντήρια Πιάτων", "dishwashers"),
        ("Καφετιέρες Φίλτρου", "coffee_machines"),
        ("Instant Φωτογραφικές Μηχανές", "cameras"),
        ("Εντοιχιζόμενα Πλυντήρια Πιάτων", "dishwashers"),
        ("Ντουλάπες & Multi-Door", "refrigerators"),
        ("Ακουστικά Headset", "headphones"),
        ("Πλυντήρια - Στεγνωτήρια", "washing_machines"),
        ("Action Cameras", "cameras"),
        ("Ακουστικά Κεφαλής", "headphones"),
        ("Εντοιχιζόμενοι Φούρνοι Μικροκυμάτων", "ovens"),
        ("Compact Φωτογραφικές Μηχανές", "cameras"),
        ("Κουζίνες", "ovens"),
        ("Καταψύκτες", "refrigerators"),
        ("Ψυγεία Δίπορτα", "refrigerators"),
        ("Activity Trackers", "smartwatches"),
        ("Σκούπες Robot", "vacuums"),
        ("All In One PC", "desktops"),
        ("Φούρνοι Μικροκυμάτων", "ovens"),
        ("Ηχεία", "speakers"),
        ("Mirrorless Φωτογραφικές Μηχανές", "cameras"),
        ("Συσκευές Φραπέ", "coffee_machines"),
        ("Σκουπάκια Χειρός", "vacuums"),
        ("Εντοιχιζόμενα Ψυγεία", "refrigerators"),
        ("Smart Rings", "smartwatches"),
        ("Κινητά Απλής Χρήσης", "smartphones"),
        ("Mini Bars & Μονόπορτα Ψυγεία", "refrigerators"),
        ("Nespresso", "coffee_machines"),
        ("Desktops", "desktops"),
        ("Ακουστικά Χωρίς Μικρόφωνο", "headphones"),
        ("Συντηρητές Κρασιών", "refrigerators"),
        ("Micro HiFi & Radio/CD", "speakers"),
        ("Φουρνάκια", "ovens"),
        ("Retro Κονσόλες", "consoles"),
        ("Μπρίκια Espresso Χειρός", "coffee_machines"),
        ("Ηχεία Hi-Fi", "speakers"),
        ("Αυτοενισχυόμενα Ηχεία", "speakers"),
        ("Party Speakers", "speakers"),
        ("Nintendo Switch Κονσόλες", "consoles"),
        ("Εντοιχιζόμενες Καφετιέρες", "coffee_machines"),
        ("Φουρνάκια Ρομπότ", "ovens"),
        ("Σκούπες Στάχτης", "vacuums"),
        ("DSLR Φωτογραφικές Μηχανές", "cameras"),
        ("Εντοιχιζόμενα Πλυντήρια Ρούχων", "washing_machines"),
        ("Φούρνοι Πίτσας", "ovens"),
        ("Φορητές Κονσόλες", "consoles"),
        ("Οροφής", "air_conditioners"),
        ("Επιτραπέζιες Εστίες", "ovens"),
        ("Φορητά Κλιματιστικά", "air_conditioners"),
        ("Πλυντικές Σκούπες", "vacuums"),
        ("Αισθητήρες", "smart_home"),
        ("Headset Με Μικρόφωνο", "headphones"),
        ("Subwoofers", "speakers"),
        ("ηχεία 2.0", "speakers"),
        ("Βιντεοκάμερες", "cameras"),
        ("Home Cinema & HiFi", "speakers"),
        ("Playstation Portal", "consoles"),
        ("Εντοιχιζόμενα Σετ", "ovens"),
        ("Τοίχου", "air_conditioners"),
    ],

    "bionic": [
        ("Headphone", "headphones"),
        ("NoteBooks", "laptops"),
        ("Speakers", "speakers"),
        ("Monitors", "monitors"),
        ("Mobile phones", "smartphones"),
        ("Tablets", "tablets"),
        ("Computers", "desktops"),
        ("TVs", "tvs"),
        ("Digital Cameras", "cameras"),
        ("Smartwatches", "smartwatches"),
        ("Smartwatches eSIM", "smartwatches"),
        ("Robot Vacuum", "vacuums"),
        ("Home Cinema", "speakers"),
        ("Smart Living", "smart_home"),
        ("Game consoles", "consoles"),
        ("All in one computers", "desktops"),
        ("Smart Hubs", "smart_home"),
    ],
}


def build_lookup():
    """Turn CATEGORY_MAP into a dict keyed by (store, fingerprint(label))."""
    lookup = {}
    for store, entries in CATEGORY_MAP.items():
        for raw_label, canonical in entries:
            if canonical not in CANONICAL_CATEGORIES:
                raise ValueError(
                    "Unknown canonical category %r for %r/%r"
                    % (canonical, store, raw_label)
                )
            lookup[(store, fingerprint(raw_label))] = canonical
    return lookup


def fill(path):
    """Read the CSV at `path`, fill canonical_category, write it back."""
    lookup = build_lookup()

    # --- read every row, preserving raw values exactly -------------------
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames
        rows = list(reader)

    expected = ["store", "raw_product_type", "product_count", "canonical_category"]
    if fieldnames != expected:
        raise SystemExit(
            "Unexpected CSV header.\n  found:    %s\n  expected: %s"
            % (fieldnames, expected)
        )

    # --- one-time backup so the unfilled file is never lost --------------
    backup = path + ".bak"
    if not os.path.exists(backup):
        with open(backup, "w", newline="", encoding="utf-8") as out:
            writer = csv.DictWriter(out, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    # --- assign canonical categories -------------------------------------
    seen_keys = set()
    per_category_rows = defaultdict(int)
    per_category_products = defaultdict(int)
    mapped_rows = 0
    mapped_products = 0
    excluded_products = 0

    for row in rows:
        store = row["store"]
        raw = row["raw_product_type"]
        try:
            count = int(row["product_count"])
        except (TypeError, ValueError):
            count = 0

        key = (store, fingerprint(raw))
        seen_keys.add(key)

        canonical = lookup.get(key, "")
        row["canonical_category"] = canonical

        if canonical:
            mapped_rows += 1
            mapped_products += count
            per_category_rows[canonical] += 1
            per_category_products[canonical] += count
        else:
            excluded_products += count

    # --- write the filled file back in place -----------------------------
    with open(path, "w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    # --- report ----------------------------------------------------------
    print("=" * 64)
    print("Filled:", path)
    print("Backup of original :", backup)
    print("=" * 64)
    print("Mapped rows      : %d" % mapped_rows)
    print("Mapped products  : %d" % mapped_products)
    print("Excluded products: %d" % excluded_products)
    print("-" * 64)
    print("Products per canonical category (high -> low):")
    for cat in sorted(per_category_products, key=per_category_products.get, reverse=True):
        print("  %-18s %6d products  (%d store-rows)"
              % (cat, per_category_products[cat], per_category_rows[cat]))
    print("-" * 64)

    # Any map entry that matched no row almost certainly means the label in
    # the table no longer matches the scraped data. Surface it loudly.
    unmatched = []
    for store, entries in CATEGORY_MAP.items():
        for raw_label, _ in entries:
            if (store, fingerprint(raw_label)) not in seen_keys:
                unmatched.append("%s / %s" % (store, raw_label))
    if unmatched:
        print("WARNING: %d map entries matched nothing in the file:" % len(unmatched))
        for item in unmatched:
            print("   -", item)
    else:
        print("All map entries matched at least one row. OK.")
    print("-" * 64)

    # Largest categories we left out, so they can be reviewed by eye.
    leftover = [(r["store"], r["raw_product_type"], int(r["product_count"]))
                for r in rows
                if not r["canonical_category"]
                and r["product_count"].isdigit()]
    leftover.sort(key=lambda t: t[2], reverse=True)
    print("Top 25 EXCLUDED categories by product_count (review if needed):")
    for store, raw, count in leftover[:25]:
        print("  %6d  %-12s %s" % (count, store, raw))
    print("=" * 64)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "category_mapping.csv"
    if not os.path.exists(target):
        raise SystemExit("File not found: %s" % target)
    fill(target)