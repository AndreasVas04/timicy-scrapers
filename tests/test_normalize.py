"""
test_normalize.py
-----------------
Unit tests for the product normalization module.

Covers brand aliasing, Greek accent handling, unit normalization,
color stripping, title pipeline idempotency, EAN cleaning, and
match-key formatting.

Run with:
    python -m pytest tests/test_normalize.py -v
    # or
    python -m unittest tests.test_normalize -v
"""

import unittest

from matching.normalize import (
    clean_ean,
    ean_key,
    mpn_key,
    mpn_root_key,
    normalize_brand,
    normalize_identifier,
    normalize_title,
    normalize_units,
    strip_accents,
    strip_color,
    title_hash,
    title_key,
)


class TestNormalizeBrand(unittest.TestCase):
    """Brand aliasing: various spellings collapse to the same canonical form."""

    def test_delonghi_variants(self):
        """De'Longhi, DELONGHI, and 'de longhi' all map to the same canonical."""
        canonical = normalize_brand("De'Longhi")
        self.assertEqual(canonical, normalize_brand("DELONGHI"))
        self.assertEqual(canonical, normalize_brand("de longhi"))
        self.assertEqual(canonical, "De'Longhi")

    def test_samsung(self):
        self.assertEqual(normalize_brand("samsung"), "Samsung")
        self.assertEqual(normalize_brand("SAMSUNG"), "Samsung")
        self.assertEqual(normalize_brand("  Samsung  "), "Samsung")

    def test_hp_variant(self):
        self.assertEqual(normalize_brand("Hewlett Packard"), "HP")
        self.assertEqual(normalize_brand("HP"), "HP")

    def test_unknown_brand_stable(self):
        """An unknown brand returns a stable uppercased key."""
        result = normalize_brand("Obscure-Brand X")
        self.assertEqual(result, normalize_brand("Obscure-Brand X"))
        # The result should be the lookup key (uppercase, no special chars).
        self.assertEqual(result, "OBSCUREBRANDX")

    def test_none_and_empty(self):
        self.assertEqual(normalize_brand(None), "")
        self.assertEqual(normalize_brand(""), "")
        self.assertEqual(normalize_brand("   "), "")


class TestStripAccents(unittest.TestCase):
    """Greek accent removal and final-sigma unification."""

    def test_greek_title(self):
        """'Πλυντήριο Ρούχων' normalizes consistently with accents stripped."""
        result = strip_accents("Πλυντήριο Ρούχων")
        self.assertNotIn("ή", result)
        self.assertNotIn("ύ", result)
        self.assertEqual(result, "Πλυντηριο Ρουχων")

    def test_final_sigma(self):
        """Greek final sigma ς is unified to regular sigma σ."""
        result = strip_accents("ίντσες")
        # Accent stripped and final sigma unified.
        self.assertNotIn("ς", result)
        self.assertIn("σ", result)

    def test_latin_accents(self):
        """Latin accents are also removed (e.g. café -> cafe)."""
        self.assertEqual(strip_accents("café"), "cafe")

    def test_no_accents_passthrough(self):
        """Plain ASCII text passes through unchanged."""
        self.assertEqual(strip_accents("hello world"), "hello world")


class TestNormalizeUnits(unittest.TestCase):
    """Storage (GB/TB) and screen-size normalization."""

    def test_tb_to_gb(self):
        self.assertEqual(normalize_units("1TB"), "1024gb")
        self.assertEqual(normalize_units("1 tb"), "1024gb")
        self.assertEqual(normalize_units("2TB SSD"), "2048gb SSD")

    def test_gb(self):
        self.assertEqual(normalize_units("512 GB"), "512gb")
        self.assertEqual(normalize_units("512GB"), "512gb")
        self.assertEqual(normalize_units("128gb"), "128gb")

    def test_screen_double_quote(self):
        self.assertEqual(normalize_units('55"'), "55in")
        self.assertEqual(normalize_units('55" TV'), "55in TV")

    def test_screen_comma_decimal_greek(self):
        """Greek inch word with comma decimal separator."""
        self.assertEqual(normalize_units("6,7 ίντσες"), "6.7in")

    def test_screen_dot_decimal_inch(self):
        self.assertEqual(normalize_units("6.7 inch"), "6.7in")
        self.assertEqual(normalize_units("15.6 inches"), "15.6in")

    def test_plain_numbers_untouched(self):
        """Numbers that are not storage or screen sizes stay as-is."""
        self.assertEqual(normalize_units("model 5000"), "model 5000")


class TestStripColor(unittest.TestCase):
    """Color and finish tokens are removed from text."""

    def test_single_color(self):
        result = strip_color("iphone 15 128gb black")
        self.assertNotIn("black", result)

    def test_multi_word_finish(self):
        result = strip_color("galaxy s24 ultra space gray 256gb")
        self.assertNotIn("space gray", result)

    def test_greek_color(self):
        result = strip_color("τηλεοραση 55in μαυρο")
        self.assertNotIn("μαυρο", result)


class TestNormalizeTitle(unittest.TestCase):
    """Full title pipeline: color-invariant, idempotent, brand-aware."""

    def test_color_invariance(self):
        """Two titles differing only in color produce the SAME normalized output."""
        brand = normalize_brand("Apple")
        t1 = normalize_title("iPhone 15 128GB Black", brand)
        t2 = normalize_title("iPhone 15 128GB Blue", brand)
        self.assertEqual(t1, t2)

    def test_color_invariance_gives_same_title_key(self):
        """Color-only difference -> identical title_key."""
        brand = normalize_brand("Apple")
        t1 = normalize_title("iPhone 15 128GB Black", brand)
        t2 = normalize_title("iPhone 15 128GB Blue", brand)
        k1 = title_key(brand, "smartphones", t1)
        k2 = title_key(brand, "smartphones", t2)
        self.assertEqual(k1, k2)

    def test_idempotency(self):
        """Applying normalize_title twice gives the same result as once."""
        brand = normalize_brand("Samsung")
        raw = "SAMSUNG Galaxy S24 Ultra 512GB Titanium Black"
        once = normalize_title(raw, brand)
        twice = normalize_title(once, brand)
        self.assertEqual(once, twice)

    def test_brand_removed(self):
        """The brand name is stripped from the normalized title."""
        brand = normalize_brand("Samsung")
        result = normalize_title("Samsung Galaxy S24", brand)
        self.assertNotIn("samsung", result)

    def test_greek_title(self):
        """A Greek title normalizes cleanly."""
        brand = normalize_brand("Bosch")
        result = normalize_title("Πλυντήριο Ρούχων BOSCH WAX32M41BY", brand)
        self.assertNotIn("bosch", result)
        # Accent on ή should be stripped.
        self.assertNotIn("ή", result)

    def test_none_and_empty(self):
        self.assertEqual(normalize_title(None), "")
        self.assertEqual(normalize_title(""), "")

    def test_units_normalized_in_title(self):
        """Storage and screen units are normalized inside the title pipeline."""
        brand = normalize_brand("Samsung")
        result = normalize_title('Samsung TV 55" 4K', brand)
        self.assertIn("55in", result)


class TestNormalizeIdentifier(unittest.TestCase):
    """MPN / mpn_root canonicalization."""

    def test_basic(self):
        self.assertEqual(normalize_identifier("sm-s928b"), "SM-S928B")
        self.assertEqual(normalize_identifier("  abc 123  "), "ABC 123")

    def test_none_and_empty(self):
        self.assertEqual(normalize_identifier(None), "")
        self.assertEqual(normalize_identifier(""), "")


class TestCleanEan(unittest.TestCase):
    """EAN cleaning: keep digits, accept only length 8 or 13."""

    def test_valid_13(self):
        self.assertEqual(clean_ean("5901234123457"), "5901234123457")

    def test_valid_8(self):
        self.assertEqual(clean_ean("96385074"), "96385074")

    def test_strips_non_digits(self):
        self.assertEqual(clean_ean("5901-234-123457"), "5901234123457")

    def test_wrong_length_returns_none(self):
        self.assertIsNone(clean_ean("12345"))
        self.assertIsNone(clean_ean("123456789012345"))

    def test_none_input(self):
        self.assertIsNone(clean_ean(None))

    def test_empty_string(self):
        self.assertIsNone(clean_ean(""))


class TestTitleHash(unittest.TestCase):
    """title_hash produces a stable 12-char hex string."""

    def test_length(self):
        h = title_hash("some normalized title")
        self.assertEqual(len(h), 12)
        # Must be valid hex.
        int(h, 16)

    def test_deterministic(self):
        self.assertEqual(
            title_hash("galaxy s24 256gb"),
            title_hash("galaxy s24 256gb"),
        )


class TestMatchKeys(unittest.TestCase):
    """Match-key builders produce correctly formatted strings."""

    def test_ean_key(self):
        self.assertEqual(ean_key("5901234123457"), "ean:5901234123457")

    def test_ean_key_invalid(self):
        self.assertEqual(ean_key("123"), "")

    def test_mpn_root_key(self):
        result = mpn_root_key("Samsung", "SM-S928")
        self.assertEqual(result, "mpnroot:samsung:SM-S928")

    def test_mpn_key(self):
        result = mpn_key("Apple", "MU7A3GH/A")
        self.assertEqual(result, "mpn:apple:MU7A3GH/A")

    def test_title_key_format(self):
        brand = "Apple"
        title_norm = "iphone 15 128gb"
        result = title_key(brand, "smartphones", title_norm)
        self.assertTrue(result.startswith("title:apple:smartphones:"))
        # Hash part should be 12 hex chars.
        hash_part = result.split(":")[-1]
        self.assertEqual(len(hash_part), 12)

    def test_missing_components_return_empty(self):
        """Keys with missing required components return empty string."""
        self.assertEqual(mpn_root_key("", "SM-S928"), "")
        self.assertEqual(mpn_root_key("Samsung", ""), "")
        self.assertEqual(mpn_key("", "X123"), "")
        self.assertEqual(title_key("", "smartphones", "something"), "")
        self.assertEqual(title_key("Apple", "", "something"), "")


if __name__ == "__main__":
    unittest.main()
