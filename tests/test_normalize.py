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
    extract_brand_from_title,
    extract_model_codes,
    looks_suspicious_brand,
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


class TestSubBrandAliases(unittest.TestCase):
    """Sub-brand aliases: known sub-brands collapse to their parent brand.

    The lookup is exact on the fully-normalized key (uppercase, non-alphanumeric
    stripped), so "Logitech G" (key LOGITECHG) matches but "Logitech G502"
    (key LOGITECHG502) does not.
    """

    def test_philips_hue_collapses_to_philips(self):
        self.assertEqual(normalize_brand("Philips Hue"), normalize_brand("Philips"))

    def test_xiaomi_mijia_collapses_to_xiaomi(self):
        self.assertEqual(normalize_brand("Xiaomi Mijia"), normalize_brand("Xiaomi"))

    def test_logitech_g_collapses_to_logitech(self):
        self.assertEqual(normalize_brand("Logitech G"), normalize_brand("Logitech"))

    def test_parent_brands_unchanged(self):
        """Parent brands still normalize to their own canonical value."""
        self.assertEqual(normalize_brand("Philips"), "Philips")
        self.assertEqual(normalize_brand("Xiaomi"), "Xiaomi")
        self.assertEqual(normalize_brand("Logitech"), "Logitech")

    def test_logitech_g502_not_collapsed(self):
        """'Logitech G502' must NOT collapse to 'Logitech' — it is a product
        code, not the sub-brand 'Logitech G'."""
        self.assertNotEqual(normalize_brand("Logitech G502"),
                            normalize_brand("Logitech"))

    def test_idempotency(self):
        """normalize_brand(normalize_brand(x)) == normalize_brand(x) for
        sub-brand aliases."""
        for raw in ("Philips Hue", "Xiaomi Mijia", "Logitech G"):
            once = normalize_brand(raw)
            twice = normalize_brand(once)
            self.assertEqual(once, twice, f"not idempotent for {raw!r}")


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


class TestNewColorWords(unittest.TestCase):
    """Newly added color/finish tokens are stripped correctly."""

    def test_clear_stripped(self):
        result = strip_color("case clear transparent")
        self.assertNotIn("clear", result)

    def test_lilac_stripped(self):
        result = strip_color("galaxy a54 lilac 128gb")
        self.assertNotIn("lilac", result)

    def test_inox_stripped(self):
        result = strip_color("ψυγειο inox 380l")
        self.assertNotIn("inox", result)

    def test_color_invariance_with_new_tokens(self):
        """Titles differing only by new color tokens produce the same output."""
        brand = normalize_brand("Samsung")
        t1 = normalize_title("Samsung Galaxy A54 128GB lilac", brand)
        t2 = normalize_title("Samsung Galaxy A54 128GB black", brand)
        self.assertEqual(t1, t2)

    def test_idempotency_with_new_colors(self):
        brand = normalize_brand("Samsung")
        raw = "Samsung Galaxy A54 128GB clear"
        once = normalize_title(raw, brand)
        twice = normalize_title(once, brand)
        self.assertEqual(once, twice)


class TestVolumeNormalization(unittest.TestCase):
    """Volume unit normalization: comma/dot decimal + l/ml."""

    def test_comma_decimal_liters(self):
        """'1,25L' normalizes to '1.25l', not broken '1 25l'."""
        result = normalize_units("1,25L")
        self.assertIn("1.25l", result)
        self.assertNotIn("1 25l", result)

    def test_space_before_unit(self):
        """'0,5 L' normalizes to '0.5l'."""
        result = normalize_units("0,5 L")
        self.assertIn("0.5l", result)

    def test_ml_preserved(self):
        """ml is kept as-is, not converted to l."""
        result = normalize_units("500ml")
        self.assertIn("500ml", result)

    def test_dot_decimal_liters(self):
        result = normalize_units("1.5l")
        self.assertIn("1.5l", result)

    def test_wattage_untouched(self):
        """Non-volume numbers like wattage are not affected."""
        result = normalize_units("800W kettle 1,25L")
        # The wattage should remain unchanged (800W is not a volume unit).
        self.assertIn("800W", result)
        self.assertIn("1.25l", result)


class TestHyphenatedBrandRemoval(unittest.TestCase):
    """Hyphenated brand forms are removed from titles."""

    def test_hyphenated_brand_removed(self):
        """'PRO-MOUNTS' is stripped when brand normalizes to 'Pro-Mounts'."""
        brand = normalize_brand("PRO-MOUNTS")
        result = normalize_title("PRO-MOUNTS TV Wall Mount 55in", brand)
        # The hyphenated brand form should be fully removed.
        self.assertNotIn("promounts", result.replace(" ", ""))
        # The remaining content should still be present.
        self.assertIn("tv", result)
        self.assertIn("55in", result)


class TestExtractModelCodes(unittest.TestCase):
    """Model-code extraction from product titles."""

    def test_basic_codes(self):
        codes = extract_model_codes("Microwave mg23k3515as 800W")
        self.assertIn("mg23k3515as", codes)

    def test_scg_code(self):
        codes = extract_model_codes("Coffee Grinder SCG6050SS Black")
        self.assertIn("scg6050ss", codes)

    def test_hyphen_joined(self):
        """Hyphenated model code 'NP-BY1' is joined to 'npby1'."""
        codes = extract_model_codes("Speaker NP-BY1 Portable")
        self.assertIn("npby1", codes)

    def test_unit_tokens_excluded(self):
        """Unit/spec tokens should NOT be extracted as model codes."""
        codes = extract_model_codes("800w 1024gb 55in 1500mah 600mbps 4k")
        self.assertNotIn("800w", codes)
        self.assertNotIn("1024gb", codes)
        self.assertNotIn("55in", codes)
        self.assertNotIn("1500mah", codes)
        self.assertNotIn("600mbps", codes)

    def test_pure_word_excluded(self):
        """Tokens with only letters (no digits) like 'macbook' are excluded."""
        codes = extract_model_codes("Apple MacBook Air 2024")
        self.assertNotIn("macbook", codes)

    def test_deduplicated(self):
        """Duplicate codes appear only once."""
        codes = extract_model_codes("scg6050ss coffee grinder scg6050ss")
        self.assertEqual(codes.count("scg6050ss"), 1)

    def test_greek_letter_rejected(self):
        """Tokens containing Greek characters are not valid model codes."""
        # "c1001lβ" has a trailing Greek beta — mixed-script tokens can't
        # match the same code written in Latin at another store.
        codes = extract_model_codes("device c1001lβ accessories")
        self.assertNotIn("c1001lβ", codes)
        # Also verify Greek mu or alpha mixed in.
        codes2 = extract_model_codes("model αbc123 part")
        for c in codes2:
            self.assertTrue(c.isascii(), f"non-ASCII code returned: {c!r}")

    def test_spec_quantity_suffixes_excluded(self):
        """Spec/quantity suffixes (bar, lt, pin, bit, pcs, tmx) are excluded."""
        codes = extract_model_codes("15bar 13lt 10lt 2pin 24bit 100pcs 5tmx")
        for tok in ["15bar", "13lt", "10lt", "2pin", "24bit", "100pcs", "5tmx"]:
            self.assertNotIn(tok, codes)

    def test_real_codes_still_extracted(self):
        """Known real model codes are correctly extracted."""
        text = "SCG6050SS MCM3100W mg23k3515as FCTE110EBK50 SRS-RA3000 M500"
        codes = extract_model_codes(text)
        for expected in ["scg6050ss", "mcm3100w", "mg23k3515as",
                         "fcte110ebk50", "srsra3000", "m500"]:
            self.assertIn(expected, codes, f"{expected} should be extracted")


class TestExtractBrandFromTitle(unittest.TestCase):
    """Extracting a known brand from a product title."""

    def test_sencor_found(self):
        result = extract_brand_from_title(
            "Αποχυμωτής Αργής Σύνθλιψης Sencor ssj4050np"
        )
        self.assertEqual(result, "Sencor")

    def test_no_known_brand(self):
        result = extract_brand_from_title("Generic Widget 3000 XL Pro")
        self.assertIsNone(result)

    def test_none_input(self):
        self.assertIsNone(extract_brand_from_title(None))


class TestLooksSuspiciousBrand(unittest.TestCase):
    """Brand-trust flag: detect likely-wrong vendor-derived brands."""

    def test_unknown_brand_with_title_brand(self):
        """SSJ (unknown) with Sencor in title -> suspicious."""
        self.assertTrue(
            looks_suspicious_brand("SSJ", "Αποχυμωτής Sencor ssj4050np")
        )

    def test_known_brand_not_suspicious(self):
        """Apple with an iPhone title -> not suspicious."""
        self.assertFalse(
            looks_suspicious_brand("Apple", "iPhone 15 128GB Black")
        )

    def test_empty_brand_suspicious(self):
        """Empty brand is always suspicious."""
        self.assertTrue(looks_suspicious_brand("", "some product"))

    def test_whitespace_brand_suspicious(self):
        self.assertTrue(looks_suspicious_brand("   ", "some product"))


class TestMVPColorInvariance(unittest.TestCase):
    """MVP rule: brand + model + storage = same canonical product, color ignored.

    These tests encode the core matching invariant: titles that differ ONLY
    in color/finish must produce identical normalize_title output and
    identical title_key values. If a new color leaks through and breaks
    one of these, add it to COLOR_WORDS in lexicons.py.
    """

    def test_apple_iphone16_color_set(self):
        """iPhone 16 128GB in Black / Teal / Ultramarine -> same output."""
        brand = normalize_brand("Apple")
        titles = [
            "iPhone 16 128GB Black",
            "iPhone 16 128GB Teal",
            "iPhone 16 128GB Ultramarine",
        ]
        norms = [normalize_title(t, brand) for t in titles]
        # All normalized forms must be identical.
        self.assertEqual(norms[0], norms[1])
        self.assertEqual(norms[1], norms[2])
        # Verify the expected canonical content is present.
        self.assertIn("iphone", norms[0])
        self.assertIn("16", norms[0])
        self.assertIn("128gb", norms[0])

    def test_apple_iphone16_title_keys_match(self):
        """Same iPhone 16 color variants must produce identical title_key."""
        brand = normalize_brand("Apple")
        titles = [
            "iPhone 16 128GB Black",
            "iPhone 16 128GB Teal",
            "iPhone 16 128GB Ultramarine",
        ]
        keys = [
            title_key(brand, "smartphones", normalize_title(t, brand))
            for t in titles
        ]
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(keys[1], keys[2])
        # Keys must be non-empty.
        self.assertTrue(keys[0])

    def test_samsung_galaxy_s25_color_set(self):
        """Galaxy S25 128GB in Navy / Icyblue / Black -> same output."""
        brand = normalize_brand("Samsung")
        titles = [
            "Galaxy S25 128GB Navy",
            "Galaxy S25 128GB Icyblue",
            "Galaxy S25 128GB Black",
        ]
        norms = [normalize_title(t, brand) for t in titles]
        self.assertEqual(norms[0], norms[1])
        self.assertEqual(norms[1], norms[2])
        self.assertIn("galaxy", norms[0])
        self.assertIn("s25", norms[0])
        self.assertIn("128gb", norms[0])

    def test_samsung_galaxy_s25_title_keys_match(self):
        """Same Galaxy S25 color variants must produce identical title_key."""
        brand = normalize_brand("Samsung")
        titles = [
            "Galaxy S25 128GB Navy",
            "Galaxy S25 128GB Icyblue",
            "Galaxy S25 128GB Black",
        ]
        keys = [
            title_key(brand, "smartphones", normalize_title(t, brand))
            for t in titles
        ]
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(keys[1], keys[2])
        self.assertTrue(keys[0])

    def test_multiword_finish_fully_removed(self):
        """Multi-word finishes leave no leftover modifier tokens."""
        # Each phrase should be stripped completely — no "silver", "deep",
        # "space", or "awesome" left behind.
        for phrase in ["Silver Shadow", "Deep Blue", "Space Black", "Awesome Black"]:
            result = strip_color(phrase.lower())
            self.assertEqual(
                result.strip(), "",
                f"'{phrase}' was not fully removed, got: {result.strip()!r}",
            )

    def test_idempotency_still_holds(self):
        """normalize_title remains idempotent with the expanded color list."""
        brand = normalize_brand("Apple")
        raw = "Apple iPhone 16 128GB Ultramarine"
        once = normalize_title(raw, brand)
        twice = normalize_title(once, brand)
        self.assertEqual(once, twice)


if __name__ == "__main__":
    unittest.main()
