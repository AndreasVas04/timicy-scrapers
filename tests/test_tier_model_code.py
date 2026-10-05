"""
test_tier_model_code.py
-----------------------
Unit tests for the tier-4 (model-code) edge provider.

Covers: spec-pattern blocklist, block-spread filter, bundle detection,
the per-store family-tag guard, discriminative code selection, blocked
union (same-block vs cross-block), no-chaining guarantee, review signals,
and deterministic ordering.

Uses small synthetic EnrichedOffer fixtures — does not hit the database.

Run with:
    python -m pytest tests/test_tier_model_code.py -v
    # or
    python -m unittest tests.test_tier_model_code -v
"""

import unittest

from matching.load import EnrichedOffer, _build_offer
from matching.tier_model_code import (
    is_spec_like_code,
    is_series_suffix_code,
    choose_discriminative_code,
    get_precomputed,
    model_code_edges,
    model_code_groups,
    review_signals,
    _build_store_block_counts,
    _looks_like_bundle,
)


# ---------------------------------------------------------------------------
# Helper: build a minimal EnrichedOffer for tier-4 testing.
# ---------------------------------------------------------------------------

def _offer(
    model_codes: tuple[str, ...] = (),
    store: str = "teststore",
    category: str = "smartphones",
    effective_brand: str = "Samsung",
    brand_norm: str = "Samsung",
    title: str = "Test Product",
    is_suspicious_brand: bool = False,
    brand_from_title: str | None = None,
    title_norm: str = "test product",
) -> EnrichedOffer:
    """Build a minimal EnrichedOffer for tier-4 testing.

    Only fields relevant to tier 4 (model_codes, store, category,
    effective_brand, title, title_norm, is_suspicious_brand,
    brand_from_title) need realistic values; everything else gets safe
    defaults.

    title is read by the bundle detector. title_norm is read by the
    per-store variant count: offers of one store with the same title_norm
    are ONE variant, so with the default every offer of a test is the same
    variant unless the test passes its own title_norm.
    """
    return EnrichedOffer(
        store=store,
        store_product_id="sp-001",
        title=title,
        vendor="TestVendor",
        category=category,
        sku=None,
        price=None,
        available=True,
        image_url=None,
        product_url="https://example.com/p/1",
        mpn=None,
        mpn_root=None,
        ean=None,
        identifier_source="none",
        brand_norm=brand_norm,
        title_norm=title_norm,
        title_key="title:test:smartphones:abc123",
        ean_key=None,
        mpn_root_key=None,
        mpn_key=None,
        model_codes=model_codes,
        is_suspicious_brand=is_suspicious_brand,
        brand_from_title=brand_from_title,
        effective_brand=effective_brand,
    )


# ---------------------------------------------------------------------------
# Helper: build an offer from a raw store title, the way the loader does.
# ---------------------------------------------------------------------------

def _listing(title: str, store: str, vendor: str, category: str) -> EnrichedOffer:
    """Build an offer from a raw store title, exactly as the loader does.

    The model codes, the normalised title and the effective brand are all
    derived by the real normalisation code, so the fixture behaves like a
    scraped row. Used by the tests that are about how real titles are read:
    bundles, colour and size variants, series names next to model numbers.
    """
    return _build_offer({
        "store": store,
        "store_product_id": "sp-001",
        "title": title,
        "vendor": vendor,
        "category": category,
        "sku": None,
        "current_price": None,
        "available": True,
        "image_url": None,
        "product_url": "https://example.com/p/1",
        "mpn": None,
        "mpn_root": None,
        "ean": None,
        "identifier_source": "none",
    })


# ===========================================================================
# is_spec_like_code tests
# ===========================================================================

class TestIsSpecLikeCode(unittest.TestCase):
    """is_spec_like_code correctly blocks spec fragments and passes model codes."""

    # --- Should be BLOCKED ---

    def test_screen_size_13inch(self):
        self.assertTrue(is_spec_like_code("13inch"))

    def test_screen_size_14inch(self):
        self.assertTrue(is_spec_like_code("14inch"))

    def test_weight_10kg(self):
        self.assertTrue(is_spec_like_code("10kg"))

    def test_dual_weight_12kg10kg(self):
        self.assertTrue(is_spec_like_code("12kg10kg"))

    def test_combo_2in1(self):
        self.assertTrue(is_spec_like_code("2in1"))

    def test_resolution_1080p(self):
        self.assertTrue(is_spec_like_code("1080p"))

    def test_btu_9000btu(self):
        self.assertTrue(is_spec_like_code("9000btu"))

    def test_ram_storage_6gb128gb(self):
        self.assertTrue(is_spec_like_code("6gb128gb"))

    def test_ram_storage_8gb256gb(self):
        self.assertTrue(is_spec_like_code("8gb256gb"))

    def test_storage_gb1tb(self):
        self.assertTrue(is_spec_like_code("gb1tb"))

    def test_core_10core(self):
        self.assertTrue(is_spec_like_code("10core"))

    def test_core_blob_10core16gb256gb(self):
        self.assertTrue(is_spec_like_code("10core16gb256gb"))

    def test_cpu_spec_blob_r57520u16gb512gb(self):
        self.assertTrue(is_spec_like_code("r57520u16gb512gb"))

    def test_standalone_gen2(self):
        self.assertTrue(is_spec_like_code("gen2"))

    def test_ip_rating_ip67(self):
        self.assertTrue(is_spec_like_code("ip67"))

    def test_ip_rating_ip54(self):
        self.assertTrue(is_spec_like_code("ip54"))

    def test_gpu_core_10gpu(self):
        self.assertTrue(is_spec_like_code("10gpu"))

    def test_standalone_10core(self):
        """Standalone '10core' is blocked by the core-adjacent-to-digits pattern."""
        self.assertTrue(is_spec_like_code("10core"))

    def test_watch_series_watch8(self):
        self.assertTrue(is_spec_like_code("watch8"))

    # Lenovo platform codes. Shape: 2-digit screen size, 3-4 platform
    # letters, 1-2 digit generation, optional single trailing letter.
    # The same code is printed on LOQ, Legion, IdeaPad and Yoga laptops, so
    # it names a chassis platform, not one product, and must never be used
    # as a match key. (15arp10 and 15arp10e used to be accepted as model
    # codes; they are blocked now.)

    def test_lenovo_platform_15arp10(self):
        self.assertTrue(is_spec_like_code("15arp10"))

    def test_lenovo_platform_15arp10e(self):
        """The same platform code with the optional trailing letter."""
        self.assertTrue(is_spec_like_code("15arp10e"))

    def test_lenovo_platform_other_shapes(self):
        """One- and two-digit generations, three and four platform letters,
        with and without the trailing letter."""
        for code in ["14ahp9", "16irh8", "15irx10", "16agp11", "16iax10h",
                     "16arha7"]:
            self.assertTrue(is_spec_like_code(code), f"{code} should be blocked")

    def test_hp_marketing_tag_x360(self):
        """'x360' is printed on Pavilion, Envy and Spectre convertibles alike."""
        self.assertTrue(is_spec_like_code("x360"))

    def test_buds_series_buds3(self):
        self.assertTrue(is_spec_like_code("buds3"))

    def test_buds_series_buds4(self):
        self.assertTrue(is_spec_like_code("buds4"))

    def test_voltage_marking_d23050(self):
        """The mains marking 'D230/50' reaches this filter joined as 'd23050'."""
        self.assertTrue(is_spec_like_code("d23050"))

    # --- Should PASS (genuine model codes) ---

    def test_pass_1000mk2(self):
        self.assertFalse(is_spec_like_code("1000mk2"))

    def test_pass_mg23k3515as(self):
        self.assertFalse(is_spec_like_code("mg23k3515as"))

    def test_pass_scg6050ss(self):
        self.assertFalse(is_spec_like_code("scg6050ss"))

    def test_pass_14he0001nv(self):
        self.assertFalse(is_spec_like_code("14he0001nv"))

    def test_pass_13bg1000nv(self):
        self.assertFalse(is_spec_like_code("13bg1000nv"))

    # Near-misses of the Lenovo platform shape: real model codes that start
    # the same way (two digits, then letters) but are not platform codes.
    # If one of these starts failing, the platform rule has been widened and
    # is swallowing product codes.

    def test_pass_lenovo_part_number_83dv00jugm(self):
        """A Lenovo part number identifies one exact laptop. It has only
        two letters after the leading digits, and a long tail."""
        self.assertFalse(is_spec_like_code("83dv00jugm"))

    def test_pass_two_letters_27gs60f(self):
        """Two letters where a platform code has three or four
        (LG monitor 27GS60F)."""
        self.assertFalse(is_spec_like_code("27gs60f"))

    def test_pass_three_digit_number_55oled809(self):
        """Three digits where a platform code has one or two
        (Philips TV 55OLED809)."""
        self.assertFalse(is_spec_like_code("55oled809"))

    def test_pass_long_tail_65qned86t6a(self):
        """More than one character after the number
        (LG TV 65QNED86T6A)."""
        self.assertFalse(is_spec_like_code("65qned86t6a"))

    # The x360 and buds tags are blocked only when they are the whole code.

    def test_pass_code_containing_x360(self):
        """A longer code that merely contains 'x360' (router AX3600)."""
        self.assertFalse(is_spec_like_code("ax3600"))

    def test_pass_code_starting_with_buds_tag(self):
        """A longer code that starts with a buds tag is left alone."""
        self.assertFalse(is_spec_like_code("buds3pro"))


# ===========================================================================
# is_series_suffix_code tests
# ===========================================================================

class TestIsSeriesSuffixCode(unittest.TestCase):
    r"""is_series_suffix_code blocks short digit-prefix + letter-suffix codes
    shared across product lines, and passes genuine model codes.

    Pattern: ^\d{3,4}[a-z]{1,2}$

    SHOULD BE REJECTED:  770nc, 670nc, 680nc, 520c, 310c, 520bt, 135bt
    MUST NOT be rejected: z150, h340, h111, m500, m185, m190, mdrzx310,
                          mdrzx310ap, cre611s06, bch6ath25, wh1000xm5, whch520
    """

    # --- Should be REJECTED ---

    def test_reject_770nc(self):
        self.assertTrue(is_series_suffix_code("770nc"))

    def test_reject_670nc(self):
        self.assertTrue(is_series_suffix_code("670nc"))

    def test_reject_680nc(self):
        self.assertTrue(is_series_suffix_code("680nc"))

    def test_reject_520c(self):
        self.assertTrue(is_series_suffix_code("520c"))

    def test_reject_310c(self):
        self.assertTrue(is_series_suffix_code("310c"))

    def test_reject_520bt(self):
        self.assertTrue(is_series_suffix_code("520bt"))

    def test_reject_135bt(self):
        self.assertTrue(is_series_suffix_code("135bt"))

    # --- MUST NOT be rejected (start with a letter) ---

    def test_pass_z150(self):
        self.assertFalse(is_series_suffix_code("z150"))

    def test_pass_h340(self):
        self.assertFalse(is_series_suffix_code("h340"))

    def test_pass_h111(self):
        self.assertFalse(is_series_suffix_code("h111"))

    def test_pass_m500(self):
        self.assertFalse(is_series_suffix_code("m500"))

    def test_pass_m185(self):
        self.assertFalse(is_series_suffix_code("m185"))

    def test_pass_m190(self):
        self.assertFalse(is_series_suffix_code("m190"))

    def test_pass_mdrzx310(self):
        self.assertFalse(is_series_suffix_code("mdrzx310"))

    def test_pass_mdrzx310ap(self):
        self.assertFalse(is_series_suffix_code("mdrzx310ap"))

    def test_pass_cre611s06(self):
        self.assertFalse(is_series_suffix_code("cre611s06"))

    def test_pass_bch6ath25(self):
        self.assertFalse(is_series_suffix_code("bch6ath25"))

    def test_pass_wh1000xm5(self):
        self.assertFalse(is_series_suffix_code("wh1000xm5"))

    def test_pass_whch520(self):
        self.assertFalse(is_series_suffix_code("whch520"))


# ===========================================================================
# Combined filter test — must pass BOTH to be eligible
# ===========================================================================

class TestCombinedFilters(unittest.TestCase):
    """A code must pass both is_spec_like_code and is_series_suffix_code
    to be eligible for tier 4. Failing either one excludes it."""

    def test_spec_like_excludes_from_tier4(self):
        """A code blocked by spec-pattern (but not series-suffix) is excluded."""
        # "1080p" fails spec check, passes series-suffix check.
        offers = [
            _offer(model_codes=("1080p",), category="tvs",
                   effective_brand="Samsung", store="a"),
            _offer(model_codes=("1080p",), category="tvs",
                   effective_brand="Samsung", store="b"),
        ]
        edges = model_code_edges(offers)
        self.assertEqual(edges, [])

    def test_series_suffix_excludes_from_tier4(self):
        """A code blocked by series-suffix (but not spec-pattern) is excluded."""
        # "770nc" passes spec check, fails series-suffix check.
        self.assertFalse(is_spec_like_code("770nc"))
        self.assertTrue(is_series_suffix_code("770nc"))
        offers = [
            _offer(model_codes=("770nc",), category="headphones",
                   effective_brand="JBL", store="a"),
            _offer(model_codes=("770nc",), category="headphones",
                   effective_brand="JBL", store="b"),
        ]
        edges = model_code_edges(offers)
        self.assertEqual(edges, [])


# ===========================================================================
# Platform codes and series tags never link offers
# ===========================================================================

class TestPlatformAndSeriesTags(unittest.TestCase):
    """Lenovo platform codes, 'x360', 'buds<N>' and the 'D230/50' voltage
    marking are shared by different products, so they must never link two
    offers, even when two stores print the same tag."""

    def test_blocked_tags_do_not_link_offers(self):
        """Two offers in different stores that share nothing but a blocked
        tag stay unlinked."""
        for tag in ["15arp10", "x360", "buds3", "d23050"]:
            offers = [
                _offer(model_codes=(tag,), store="a"),
                _offer(model_codes=(tag,), store="b"),
            ]
            self.assertEqual(model_code_edges(offers), [],
                             f"{tag} linked two offers")

    def test_lenovo_platform_code_does_not_merge_two_laptops(self):
        """A LOQ and an IdeaPad built on the same platform (15ARP10) are
        different laptops. Each is keyed on its own part number."""
        offers = [
            _listing("Lenovo LOQ 15ARP10 83JC00K3GM Ryzen 7",
                     "kotsovolos", "Lenovo", "laptops"),
            _listing("Lenovo IdeaPad Slim 3 15ARP10 83K7000TGM Ryzen 5",
                     "stephanis", "Lenovo", "laptops"),
        ]
        # Both titles really carry the platform code.
        self.assertIn("15arp10", offers[0].model_codes)
        self.assertIn("15arp10", offers[1].model_codes)

        self.assertEqual(model_code_edges(offers), [])
        _block_spread, _freq, chosen = get_precomputed(offers)
        self.assertEqual(chosen, {0: "83jc00k3gm", 1: "83k7000tgm"})

    def test_same_lenovo_laptop_still_links_on_its_part_number(self):
        """Blocking the platform code does not stop the same laptop from
        being matched across stores through its part number."""
        offers = [
            _listing("Lenovo LOQ 15ARP10 83JC00K3GM Ryzen 7",
                     "kotsovolos", "Lenovo", "laptops"),
            _listing("Laptop Lenovo LOQ 15ARP10 83JC00K3GM",
                     "stephanis", "Lenovo", "laptops"),
        ]
        self.assertEqual(model_code_edges(offers), [(0, 1)])

    def test_hp_x360_tag_does_not_merge_two_laptops(self):
        """A Pavilion x360 and an Envy x360 share the tag and nothing else."""
        offers = [
            _listing("HP Pavilion x360 14-ek1001nv",
                     "kotsovolos", "HP", "laptops"),
            _listing("HP Envy x360 15-fe0003nv",
                     "stephanis", "HP", "laptops"),
        ]
        # Both titles really carry the tag.
        self.assertIn("x360", offers[0].model_codes)
        self.assertIn("x360", offers[1].model_codes)

        self.assertEqual(model_code_edges(offers), [])


# ===========================================================================
# Block-spread filter tests
# ===========================================================================

class TestBlockSpreadFilter(unittest.TestCase):
    """Codes in 2+ blocks are excluded; codes in exactly 1 block are kept."""

    def test_code_in_two_blocks_excluded(self):
        """A code appearing in two different (category, brand) blocks should
        not produce edges, because it is non-discriminative."""
        offers = [
            # Same code "abc123" in two different blocks.
            _offer(model_codes=("abc123",), category="laptops", effective_brand="HP"),
            _offer(model_codes=("abc123",), category="monitors", effective_brand="HP"),
        ]
        edges = model_code_edges(offers)
        # No edges — code spans 2 blocks.
        self.assertEqual(edges, [])

    def test_code_in_one_block_kept(self):
        """A code appearing only within one block should produce edges."""
        offers = [
            _offer(model_codes=("abc123",), category="laptops", effective_brand="HP"),
            _offer(model_codes=("abc123",), category="laptops", effective_brand="HP"),
        ]
        edges = model_code_edges(offers)
        self.assertEqual(edges, [(0, 1)])

    def test_code_in_two_blocks_different_brand(self):
        """Same code in same category but different brands = 2 blocks."""
        offers = [
            _offer(model_codes=("xyz999",), category="tvs", effective_brand="Samsung"),
            _offer(model_codes=("xyz999",), category="tvs", effective_brand="LG"),
        ]
        edges = model_code_edges(offers)
        self.assertEqual(edges, [])

    def test_code_seen_in_another_category_is_not_used_at_all(self):
        """Two stores share a code inside one block, but a third offer
        carries the same code in another category. The code is then not
        discriminative any more, so even the two same-block offers stay
        unlinked. (Without the third offer they are linked, see
        test_code_in_one_block_kept.)"""
        offers = [
            _offer(model_codes=("abc123",), category="laptops",
                   effective_brand="HP", store="a"),
            _offer(model_codes=("abc123",), category="laptops",
                   effective_brand="HP", store="b"),
            _offer(model_codes=("abc123",), category="monitors",
                   effective_brand="HP", store="c"),
        ]
        self.assertEqual(model_code_edges(offers), [])

    def test_code_seen_under_another_brand_is_not_used_at_all(self):
        """The same, with the third offer under another brand of the same
        category."""
        offers = [
            _offer(model_codes=("xyz999",), category="tvs",
                   effective_brand="Samsung", store="a"),
            _offer(model_codes=("xyz999",), category="tvs",
                   effective_brand="Samsung", store="b"),
            _offer(model_codes=("xyz999",), category="tvs",
                   effective_brand="LG", store="c"),
        ]
        self.assertEqual(model_code_edges(offers), [])


# ===========================================================================
# choose_discriminative_code tests
# ===========================================================================

class TestChooseDiscriminativeCode(unittest.TestCase):
    """Discriminative code selection.

    Order of preference: the code carried by the most stores, then the
    longest, then the rarest, then alphabetical. Before ranking, a code that
    is a much shorter prefix of another code of the same offer is dropped.

    The arguments mirror what the pipeline computes for all offers:
      freq               code -> number of offers carrying it
      block_spread       code -> number of (category, brand) blocks it is in
      store_block_counts (store, category, brand, code) -> number of
                         distinct variants of that store carrying it
      store_spread       code -> number of distinct stores carrying it
    """

    def _one_block(self, offer):
        """block_spread for an offer whose codes all live in one
        (category, brand) block, as every usable code does."""
        return {code: 1 for code in offer.model_codes}

    def _one_variant(self, offer):
        """store_block_counts for an offer whose codes each belong to a
        single variant in the offer's own store, so none is a family tag."""
        return {
            (offer.store, offer.category, offer.effective_brand, code): 1
            for code in offer.model_codes
        }

    # --- First rule: the code carried by the most stores ---

    def test_code_in_more_stores_beats_rarer_code(self):
        """The code that more stores carry wins, although it is the more
        common one. This replaces the old "rarest code wins" rule: a code
        seen in one store only can never match another store's offer.

        Both codes have the same length, and rarity and alphabet both
        favour "onlyhere1", so only the store count can explain the result.
        """
        offer = _offer(model_codes=("onlyhere1", "shared001"))
        result = choose_discriminative_code(
            offer,
            freq={"onlyhere1": 1, "shared001": 3},
            block_spread=self._one_block(offer),
            store_block_counts=self._one_variant(offer),
            store_spread={"onlyhere1": 1, "shared001": 3},
        )
        self.assertEqual(result, "shared001")

    def test_code_in_more_stores_beats_longer_code(self):
        """Store count is checked before length. Both codes are shared, so
        it is the exact number of stores that decides (3 against 2), not
        just "shared or not"."""
        offer = _offer(model_codes=("ab12", "abcde12"))
        result = choose_discriminative_code(
            offer,
            freq={"ab12": 3, "abcde12": 2},
            block_spread=self._one_block(offer),
            store_block_counts=self._one_variant(offer),
            store_spread={"ab12": 3, "abcde12": 2},
        )
        self.assertEqual(result, "ab12")

    # --- Tie-breaks, in order: longest, rarest, alphabetical ---

    def test_tiebreak_longest(self):
        """Same number of stores: the longest code wins, even though it is
        found in more offers (length is checked before rarity)."""
        offer = _offer(model_codes=("ab12", "abcde12"))
        result = choose_discriminative_code(
            offer,
            freq={"ab12": 2, "abcde12": 5},
            block_spread=self._one_block(offer),
            store_block_counts=self._one_variant(offer),
            store_spread={"ab12": 2, "abcde12": 2},
        )
        self.assertEqual(result, "abcde12")

    def test_tiebreak_rarest(self):
        """Same number of stores and same length: the code found in fewer
        offers wins. This is the only place left where rarity decides.
        Alphabetical order alone would have picked "aaa111"."""
        offer = _offer(model_codes=("aaa111", "zzz999"))
        result = choose_discriminative_code(
            offer,
            freq={"aaa111": 6, "zzz999": 2},
            block_spread=self._one_block(offer),
            store_block_counts=self._one_variant(offer),
            store_spread={"aaa111": 2, "zzz999": 2},
        )
        self.assertEqual(result, "zzz999")

    def test_tiebreak_alphabetical(self):
        """Same number of stores, same length, same frequency: alphabetical
        order decides, so the result is always the same."""
        offer = _offer(model_codes=("bbb111", "aaa111"))
        result = choose_discriminative_code(
            offer,
            freq={"bbb111": 3, "aaa111": 3},
            block_spread=self._one_block(offer),
            store_block_counts=self._one_variant(offer),
            store_spread={"bbb111": 2, "aaa111": 2},
        )
        self.assertEqual(result, "aaa111")

    # --- Before ranking: a short family prefix of a longer code is dropped ---

    def test_short_family_prefix_is_dropped(self):
        """A series label that is the beginning of the full model number in
        the same offer is dropped before ranking: "p500" next to
        "p500sv05210h0020". Without this the label would win, because more
        stores print the series name than the full model."""
        offer = _offer(model_codes=("p500", "p500sv05210h0020"),
                       category="desktops", effective_brand="ASUS")
        result = choose_discriminative_code(
            offer,
            freq={"p500": 3, "p500sv05210h0020": 2},
            block_spread=self._one_block(offer),
            store_block_counts=self._one_variant(offer),
            store_spread={"p500": 3, "p500sv05210h0020": 2},
        )
        self.assertEqual(result, "p500sv05210h0020")

    def test_prefix_four_characters_shorter_is_dropped(self):
        """The prefix is dropped from a difference of four characters on:
        "zve10" (5 characters) next to "zve10m2kb" (9 characters)."""
        offer = _offer(model_codes=("zve10", "zve10m2kb"),
                       category="cameras", effective_brand="Sony")
        result = choose_discriminative_code(
            offer,
            freq={"zve10": 3, "zve10m2kb": 1},
            block_spread=self._one_block(offer),
            store_block_counts=self._one_variant(offer),
            store_spread={"zve10": 3, "zve10m2kb": 1},
        )
        self.assertEqual(result, "zve10m2kb")

    def test_prefix_three_characters_shorter_is_kept(self):
        """A difference of three characters is a regional suffix, not a
        family label, so the shared root stays: "hta3000" (7 characters)
        next to "hta3000cel" (10 characters).

        Three stores print "hta3000" and two of them also print
        "hta3000cel". The offer must be keyed on "hta3000", otherwise it
        leaves the three-store group."""
        offer = _offer(model_codes=("hta3000", "hta3000cel"),
                       category="speakers", effective_brand="Sony")
        result = choose_discriminative_code(
            offer,
            freq={"hta3000": 3, "hta3000cel": 2},
            block_spread=self._one_block(offer),
            store_block_counts=self._one_variant(offer),
            store_spread={"hta3000": 3, "hta3000cel": 2},
        )
        self.assertEqual(result, "hta3000")

    # --- Offers that get no code at all ---

    def test_empty_trusted_codes_returns_none(self):
        """An offer with no trusted codes contributes nothing."""
        # All codes are spec-like. Spec-like codes never enter any of the
        # maps, so for this offer every map is empty.
        offer = _offer(model_codes=("1080p", "2in1"))
        result = choose_discriminative_code(
            offer,
            freq={},
            block_spread={},
            store_block_counts={},
            store_spread={},
        )
        self.assertIsNone(result)

    def test_no_model_codes_returns_none(self):
        """An offer with no model codes at all returns None."""
        offer = _offer(model_codes=())
        result = choose_discriminative_code(
            offer,
            freq={},
            block_spread={},
            store_block_counts={},
            store_spread={},
        )
        self.assertIsNone(result)

    def test_bundle_listing_returns_none(self):
        """A set listing gets no code, however good its codes are.

        The maps describe an oven that another store sells on its own. The
        set is not counted as a variant of its own store, which is why
        store_block_counts has no entry for it."""
        offer = _offer(
            model_codes=("hba514bs3", "pke645ba2e"),
            store="kotsovolos",
            category="ovens",
            effective_brand="Bosch",
            title="Bosch Σετ Φούρνος HBA514BS3 & Εστία PKE645BA2E",
        )
        result = choose_discriminative_code(
            offer,
            freq={"hba514bs3": 1},
            block_spread={"hba514bs3": 1, "pke645ba2e": 1},
            store_block_counts={("stephanis", "ovens", "Bosch", "hba514bs3"): 1},
            store_spread={"hba514bs3": 1},
        )
        self.assertIsNone(result)

        # Control: the very same offer and maps, without the set wording in
        # the title, does get a code.
        plain = _offer(
            model_codes=("hba514bs3", "pke645ba2e"),
            store="kotsovolos",
            category="ovens",
            effective_brand="Bosch",
            title="Bosch HBA514BS3 PKE645BA2E",
        )
        result = choose_discriminative_code(
            plain,
            freq={"hba514bs3": 1},
            block_spread={"hba514bs3": 1, "pke645ba2e": 1},
            store_block_counts={("stephanis", "ovens", "Bosch", "hba514bs3"): 1},
            store_spread={"hba514bs3": 1},
        )
        self.assertIsNotNone(result)


# ===========================================================================
# Code selection across stores (whole pipeline)
# ===========================================================================

class TestCodeSelectionAcrossStores(unittest.TestCase):
    """The selection rules seen from the outside: which offers end up linked
    when stores print series names and regional codes next to the model."""

    def test_series_name_next_to_full_model_still_matches(self):
        """One store prints the series name and the full model number, the
        other two print the full model only. All three are the same washing
        machine and must be linked through the full model.

        Under the old "rarest code wins" rule the first store was keyed on
        "ww5000d", which nobody else prints, and matched nothing."""
        offers = [
            _listing("Samsung WW5000D WW11DG5B25AELE Πλυντήριο Ρούχων",
                     "kotsovolos", "Samsung", "washing_machines"),
            _listing("Samsung WW11DG5B25AELE Washing Machine",
                     "stephanis", "Samsung", "washing_machines"),
            _listing("SAMSUNG Πλυντήριο WW11DG5B25AELE",
                     "public", "Samsung", "washing_machines"),
        ]
        self.assertEqual(offers[0].model_codes, ("ww5000d", "ww11dg5b25aele"))

        _block_spread, _freq, chosen = get_precomputed(offers)
        self.assertEqual(chosen[0], "ww11dg5b25aele")
        self.assertEqual(model_code_edges(offers), [(0, 1), (0, 2)])

    def test_offer_with_regional_code_stays_in_largest_group(self):
        """Three stores print "HT-A3000"; two of them also print the
        regional "HTA3000.CEL". Everybody must be keyed on "hta3000" so
        that all three land in one group. If the two stores were keyed on
        the longer regional code, the first store would be left out."""
        offers = [
            _listing("Sony HT-A3000 Soundbar",
                     "kotsovolos", "Sony", "speakers"),
            _listing("Sony HT-A3000 HTA3000.CEL Soundbar",
                     "stephanis", "Sony", "speakers"),
            _listing("SONY Soundbar HT-A3000 HTA3000.CEL Dolby Atmos",
                     "public", "Sony", "speakers"),
        ]
        self.assertEqual(offers[1].model_codes, ("hta3000", "hta3000cel"))

        self.assertEqual(model_code_edges(offers), [(0, 1), (0, 2)])

    def test_series_label_does_not_pull_offer_away_from_its_model(self):
        """Three stores print the series label "P500", only two print the
        full model "P500SV-05210H0020". The offer that prints both must be
        linked to the other listing of that exact model, and to nothing
        else, although the label is carried by more stores."""
        offers = [
            _listing("ASUS ExpertCenter P500 P500SV-05210H0020 Desktop",
                     "kotsovolos", "ASUS", "desktops"),
            _listing("ASUS P500SV-05210H0020 Desktop PC",
                     "stephanis", "ASUS", "desktops"),
            _listing("ASUS ExpertCenter P500 Mini Tower",
                     "public", "ASUS", "desktops"),
            _listing("ASUS ExpertCenter P500 Mini Tower",
                     "electroline", "ASUS", "desktops"),
        ]
        self.assertEqual(offers[0].model_codes, ("p500", "p500sv05210h0020"))

        _block_spread, _freq, chosen = get_precomputed(offers)
        self.assertEqual(chosen[0], "p500sv05210h0020")
        edges_of_first_offer = [e for e in model_code_edges(offers) if 0 in e]
        self.assertEqual(edges_of_first_offer, [(0, 1)])

    def test_store_count_is_about_stores_not_listings(self):
        """A code listed three times by ONE store is still carried by one
        store. A code listed once by each of two stores is carried by two
        and must be preferred."""
        offers = [
            # Store "a" lists the same product three times. Only the first
            # listing also prints the code that store "b" uses.
            _offer(model_codes=("abc123", "xyz789"), store="a"),
            _offer(model_codes=("abc123",), store="a"),
            _offer(model_codes=("abc123",), store="a"),
            _offer(model_codes=("xyz789",), store="b"),
        ]
        _block_spread, freq, chosen = get_precomputed(offers)
        # "abc123" is the more frequent code (3 offers against 2) ...
        self.assertEqual(freq, {"abc123": 3, "xyz789": 2})
        # ... but "xyz789" is in two stores, so the first offer is keyed on it.
        self.assertEqual(chosen[0], "xyz789")
        self.assertEqual(model_code_edges(offers), [(1, 2), (0, 3)])


# ===========================================================================
# Blocked union tests (same block vs cross block)
# ===========================================================================

class TestBlockedUnion(unittest.TestCase):
    """Offers in the SAME block sharing a trusted code are linked;
    offers in DIFFERENT blocks sharing the same code are NOT linked."""

    def test_same_block_same_code_linked(self):
        """Two offers in the same block with the same trusted code produce
        an edge."""
        offers = [
            _offer(model_codes=("sm928b",), category="phones",
                   effective_brand="Samsung", store="stephanis"),
            _offer(model_codes=("sm928b",), category="phones",
                   effective_brand="Samsung", store="public"),
        ]
        edges = model_code_edges(offers)
        self.assertEqual(edges, [(0, 1)])

    def test_different_blocks_same_code_not_linked(self):
        """Two offers in different blocks with the same code produce NO edge."""
        offers = [
            _offer(model_codes=("sm928b",), category="phones",
                   effective_brand="Samsung"),
            _offer(model_codes=("sm928b",), category="tablets",
                   effective_brand="Samsung"),
        ]
        edges = model_code_edges(offers)
        self.assertEqual(edges, [])

    def test_star_pattern_three_offers(self):
        """Three offers in the same block -> star edges from smallest index."""
        offers = [
            _offer(model_codes=("sm928b",), category="phones",
                   effective_brand="Samsung", store="a"),
            _offer(model_codes=("sm928b",), category="phones",
                   effective_brand="Samsung", store="b"),
            _offer(model_codes=("sm928b",), category="phones",
                   effective_brand="Samsung", store="c"),
        ]
        edges = model_code_edges(offers)
        self.assertEqual(edges, [(0, 1), (0, 2)])

    def test_empty_effective_brand_skipped(self):
        """Offers with empty effective_brand do not participate."""
        offers = [
            _offer(model_codes=("sm928b",), effective_brand=""),
            _offer(model_codes=("sm928b",), effective_brand=""),
        ]
        edges = model_code_edges(offers)
        self.assertEqual(edges, [])

    def test_none_effective_brand_skipped(self):
        """Offers with None effective_brand do not participate."""
        offers = [
            _offer(model_codes=("sm928b",), effective_brand=None),
            _offer(model_codes=("sm928b",), effective_brand=None),
        ]
        edges = model_code_edges(offers)
        self.assertEqual(edges, [])

    def test_dotted_code_links_two_stores(self):
        """A dotted manufacturer code ("BCO411.B") is read as one code,
        "bco411b", and links the two stores that print it."""
        offers = [
            _listing("De'Longhi BCO411.B Καφετιέρα",
                     "kotsovolos", "De'Longhi", "coffee_machines"),
            _listing("DeLonghi BCO411.B Combi Coffee Maker",
                     "stephanis", "DeLonghi", "coffee_machines"),
        ]
        self.assertEqual(offers[0].model_codes, ("bco411b",))
        self.assertEqual(offers[1].model_codes, ("bco411b",))

        self.assertEqual(model_code_edges(offers), [(0, 1)])


# ===========================================================================
# Bundle detection
# ===========================================================================

class TestBundleDetection(unittest.TestCase):
    """_looks_like_bundle: a set or bundle listing is recognised either by
    an explicit word, or by a joiner standing between two model codes."""

    def _from_title(self, title, vendor="Bosch", category="ovens"):
        """A listing built from a raw title; the store does not matter."""
        return _listing(title, "kotsovolos", vendor, category)

    # --- Explicit words ---

    def test_word_bundle(self):
        offer = self._from_title("Sony PlayStation 5 Gaming Bundle CFI-2016",
                                 vendor="Sony", category="consoles")
        self.assertTrue(_looks_like_bundle(offer))

    def test_greek_word_set(self):
        """'Σετ' is Greek for 'set'. Capital letters make no difference."""
        self.assertTrue(_looks_like_bundle(
            self._from_title("Bosch Σετ Φούρνος HBA514BS3")))
        self.assertTrue(_looks_like_bundle(
            self._from_title("BOSCH ΣΕΤ ΦΟΥΡΝΟΣ HBA514BS3")))

    def test_phrase_design_kit(self):
        """Bosch and Siemens sell oven sets as 'Flex Design Kit'."""
        self.assertTrue(_looks_like_bundle(
            self._from_title("Bosch HBG7741B1 Flex Design Kit")))

    def test_phrase_built_in_set(self):
        """'Set Εντοιχισμού' means 'built-in set'."""
        self.assertTrue(_looks_like_bundle(
            self._from_title("Bosch Set Εντοιχισμού HBA514BS3")))

    # --- A joiner between two model codes ---

    def test_ampersand_between_two_codes(self):
        offer = self._from_title("Bosch HBA514BS3 & PKE645BA2E")
        self.assertEqual(offer.model_codes, ("hba514bs3", "pke645ba2e"))
        self.assertTrue(_looks_like_bundle(offer))

    def test_plus_between_two_codes(self):
        offer = self._from_title("Bosch HBA514BS3 + PKE645BA2E")
        self.assertEqual(offer.model_codes, ("hba514bs3", "pke645ba2e"))
        self.assertTrue(_looks_like_bundle(offer))

    def test_html_escaped_ampersand_between_two_codes(self):
        """Scraped titles sometimes keep the ampersand as '&amp;'."""
        offer = self._from_title("Bosch HBA514BS3 &amp; PKE645BA2E")
        self.assertEqual(offer.model_codes, ("hba514bs3", "pke645ba2e"))
        self.assertTrue(_looks_like_bundle(offer))

    # --- Not bundles ---

    def test_joiner_with_a_single_code_is_not_a_bundle(self):
        """A joiner alone proves nothing. These titles contain ' + ' or
        ' & ' but only one code, and are single products."""
        phone = self._from_title(
            "Samsung Galaxy A56 5G SM-A566B 8GB + 256GB",
            vendor="Samsung", category="smartphones")
        self.assertEqual(phone.model_codes, ("sma566b",))
        self.assertFalse(_looks_like_bundle(phone))

        coffee = self._from_title(
            "Nespresso Citiz & Milk D123",
            vendor="Nespresso", category="coffee_machines")
        self.assertEqual(coffee.model_codes, ("d123",))
        self.assertFalse(_looks_like_bundle(coffee))

    def test_unspaced_plus_and_ampersand_are_not_joiners(self):
        """'S24+' and 'B&O' are names, not two products joined together.
        Both titles carry two codes, so only the missing spaces keep them
        from being read as bundles."""
        phone = self._from_title(
            "Samsung Galaxy S24+ SM-S926B SM-S926BZKDEUE",
            vendor="Samsung", category="smartphones")
        self.assertEqual(phone.model_codes, ("sms926b", "sms926bzkdeue"))
        self.assertFalse(_looks_like_bundle(phone))

        headphones = self._from_title(
            "B&O Beoplay H100 BEOPLAYH100",
            vendor="Bang & Olufsen", category="headphones")
        self.assertEqual(headphones.model_codes, ("h100", "beoplayh100"))
        self.assertFalse(_looks_like_bundle(headphones))

    def test_bare_kit_is_not_a_bundle_word(self):
        """'Kit' on its own also appears in single products, such as a
        camera sold with its kit lens. Two codes and the word 'Kit', but
        no joiner and no set wording: not a bundle."""
        offer = self._from_title("Sony ZV-E10 Kit 16-50mm ILCZV-E10L",
                                 vendor="Sony", category="cameras")
        self.assertEqual(offer.model_codes, ("zve10", "ilczve10l"))
        self.assertFalse(_looks_like_bundle(offer))

    def test_plain_listing_is_not_a_bundle(self):
        offer = self._from_title("Bosch HBA514BS3 Εντοιχιζόμενος Φούρνος")
        self.assertFalse(_looks_like_bundle(offer))


# ===========================================================================
# Bundle listings take no part in model-code matching
# ===========================================================================

class TestBundlesExcludedFromMatching(unittest.TestCase):
    """A set that contains a product is a different thing to buy than the
    product alone. Set listings get no code, are never linked to anything,
    and do not count as a variant of the product they contain."""

    OVEN = "Bosch HBA514BS3 Εντοιχιζόμενος Φούρνος"
    OVEN_OTHER_WORDING = "Bosch HBA514BS3 Built-in Oven"
    OVEN_AND_HOB_SET = "Bosch Σετ Φούρνος HBA514BS3 & Εστία PKE645BA2E"

    def _oven_listing(self, title, store):
        return _listing(title, store, "Bosch", "ovens")

    def test_set_is_not_linked_to_the_product_it_contains(self):
        """The oven alone in one store, the oven-and-hob set in another:
        both titles carry the oven's code, but they must not be linked."""
        offers = [
            self._oven_listing(self.OVEN, "kotsovolos"),
            self._oven_listing(self.OVEN_AND_HOB_SET, "stephanis"),
        ]
        self.assertIn("hba514bs3", offers[1].model_codes)

        self.assertEqual(model_code_edges(offers), [])
        # The set gets no code at all.
        _block_spread, _freq, chosen = get_precomputed(offers)
        self.assertIsNone(chosen[1])

    def test_same_product_without_set_wording_is_linked(self):
        """Control for the test above: put the oven alone in the second
        store as well and the two listings are linked."""
        offers = [
            self._oven_listing(self.OVEN, "kotsovolos"),
            self._oven_listing(self.OVEN_OTHER_WORDING, "stephanis"),
        ]
        self.assertEqual(model_code_edges(offers), [(0, 1)])

    def test_set_does_not_count_as_a_second_variant(self):
        """One store lists the oven alone AND the set that contains it.
        That is still one variant of the oven in that store, so the oven's
        code stays trusted and the oven is linked to the other store's
        oven. The set (index 1) stays out."""
        offers = [
            self._oven_listing(self.OVEN, "kotsovolos"),
            self._oven_listing(self.OVEN_AND_HOB_SET, "kotsovolos"),
            self._oven_listing(self.OVEN_OTHER_WORDING, "stephanis"),
        ]
        self.assertEqual(model_code_edges(offers), [(0, 2)])

    def test_two_listings_of_the_same_set_are_not_linked(self):
        """Sets take no part at all in model-code matching, not even with
        each other."""
        offers = [
            self._oven_listing(self.OVEN_AND_HOB_SET, "kotsovolos"),
            self._oven_listing(self.OVEN_AND_HOB_SET, "stephanis"),
        ]
        self.assertEqual(model_code_edges(offers), [])


# ===========================================================================
# Per-store variant counts
# ===========================================================================

class TestStoreVariantCounts(unittest.TestCase):
    """_build_store_block_counts: for each (store, category, brand, code),
    how many DISTINCT VARIANTS of that store carry the code. A variant is
    one normalised title, so repeated listings and colour variants are one
    variant, while sizes are separate variants."""

    def test_same_listing_twice_is_one_variant(self):
        offers = [
            _offer(model_codes=("qn70h",), store="kotsovolos", category="tvs",
                   title_norm="neo qled qn70h 55in"),
            _offer(model_codes=("qn70h",), store="kotsovolos", category="tvs",
                   title_norm="neo qled qn70h 55in"),
        ]
        self.assertEqual(
            _build_store_block_counts(offers),
            {("kotsovolos", "tvs", "Samsung", "qn70h"): 1},
        )

    def test_two_sizes_are_two_variants(self):
        """The screen size survives title normalisation, so a 55 inch and a
        65 inch listing are two variants."""
        offers = [
            _listing('Samsung Neo QLED QN70H 55" 4K Smart TV',
                     "kotsovolos", "Samsung", "tvs"),
            _listing('Samsung Neo QLED QN70H 65" 4K Smart TV',
                     "kotsovolos", "Samsung", "tvs"),
        ]
        self.assertEqual(
            _build_store_block_counts(offers),
            {("kotsovolos", "tvs", "Samsung", "qn70h"): 2},
        )

    def test_colour_variants_are_one_variant(self):
        """Colour words are stripped by title normalisation, so the black
        and the blue listing are the same variant."""
        offers = [
            _listing("Sony WH-CH520 Wireless Headphones Black",
                     "kotsovolos", "Sony", "headphones"),
            _listing("Sony WH-CH520 Wireless Headphones Blue",
                     "kotsovolos", "Sony", "headphones"),
        ]
        self.assertEqual(
            _build_store_block_counts(offers),
            {("kotsovolos", "headphones", "Sony", "whch520"): 1},
        )

    def test_each_store_is_counted_on_its_own(self):
        """Two sizes in one store and one listing in another store give a
        count of 2 for the first store and 1 for the second."""
        offers = [
            _listing('Samsung Neo QLED QN70H 55" 4K Smart TV',
                     "kotsovolos", "Samsung", "tvs"),
            _listing('Samsung Neo QLED QN70H 65" 4K Smart TV',
                     "kotsovolos", "Samsung", "tvs"),
            _listing('Samsung QN70H Neo QLED Τηλεόραση 55"',
                     "stephanis", "Samsung", "tvs"),
        ]
        self.assertEqual(
            _build_store_block_counts(offers),
            {
                ("kotsovolos", "tvs", "Samsung", "qn70h"): 2,
                ("stephanis", "tvs", "Samsung", "qn70h"): 1,
            },
        )

    def test_set_listing_is_not_counted(self):
        """A set listing is skipped: the oven is counted once, and the hob
        that appears only inside the set is not counted at all."""
        offers = [
            _listing("Bosch HBA514BS3 Εντοιχιζόμενος Φούρνος",
                     "kotsovolos", "Bosch", "ovens"),
            _listing("Bosch Σετ Φούρνος HBA514BS3 & Εστία PKE645BA2E",
                     "kotsovolos", "Bosch", "ovens"),
        ]
        self.assertEqual(
            _build_store_block_counts(offers),
            {("kotsovolos", "ovens", "Bosch", "hba514bs3"): 1},
        )

    def test_blocked_codes_are_not_counted(self):
        """Spec fragments and series suffixes are never usable, so they get
        no count. Only the real model code does."""
        offers = [
            _offer(model_codes=("1080p", "770nc", "abc123"), store="a"),
        ]
        self.assertEqual(
            _build_store_block_counts(offers),
            {("a", "smartphones", "Samsung", "abc123"): 1},
        )


# ===========================================================================
# Family-tag guard
# ===========================================================================

class TestFamilyTagGuard(unittest.TestCase):
    """A code carried by two or more distinct variants of ONE store, inside
    one (category, brand) block, is a family or series tag (one code over
    several screen sizes, for example), not a model. That store's offers
    must not be matched through it. One variant is fine."""

    # --- The threshold itself ---

    def _choose_with_variants_in_own_store(self, variants):
        """Selection for a TV whose only code is carried by the given
        number of distinct variants in the offer's own store. The other
        maps say that the code sits in one block and that one other store
        lists it once; they play no part in the threshold."""
        offer = _offer(model_codes=("qn70h",), store="kotsovolos",
                       category="tvs")
        return choose_discriminative_code(
            offer,
            freq={"qn70h": 1},
            block_spread={"qn70h": 1},
            store_block_counts={
                ("kotsovolos", "tvs", "Samsung", "qn70h"): variants,
                ("stephanis", "tvs", "Samsung", "qn70h"): 1,
            },
            store_spread={"qn70h": 1},
        )

    def test_one_variant_in_own_store_keeps_the_code(self):
        """Below the threshold: the code belongs to a single variant."""
        self.assertEqual(self._choose_with_variants_in_own_store(1), "qn70h")

    def test_two_variants_in_own_store_drop_the_code(self):
        """At the threshold: two variants make the code a family tag."""
        self.assertIsNone(self._choose_with_variants_in_own_store(2))

    def test_family_tag_dropped_but_full_model_kept(self):
        """Only the family tag is dropped, not the whole offer: the full
        model number next to it is still chosen. Without the guard the
        tag would win, because more stores print it."""
        offer = _offer(model_codes=("qn70h", "qe55qn70hatxxh"),
                       store="kotsovolos", category="tvs")
        result = choose_discriminative_code(
            offer,
            freq={"qn70h": 2, "qe55qn70hatxxh": 1},
            block_spread={"qn70h": 1, "qe55qn70hatxxh": 1},
            store_block_counts={
                ("kotsovolos", "tvs", "Samsung", "qn70h"): 2,
                ("kotsovolos", "tvs", "Samsung", "qe55qn70hatxxh"): 1,
            },
            store_spread={"qn70h": 2, "qe55qn70hatxxh": 1},
        )
        self.assertEqual(result, "qe55qn70hatxxh")

    # --- What it means for matching ---

    def test_two_sizes_in_one_store_are_not_merged(self):
        """The reason for the guard: a store lists the 55 inch and the
        65 inch model of a series, and both titles carry only the series
        code. They are different products and must stay apart."""
        offers = [
            _listing('Samsung Neo QLED QN70H 55" 4K Smart TV',
                     "kotsovolos", "Samsung", "tvs"),
            _listing('Samsung Neo QLED QN70H 65" 4K Smart TV',
                     "kotsovolos", "Samsung", "tvs"),
        ]
        self.assertEqual(offers[0].model_codes, ("qn70h",))
        self.assertEqual(offers[1].model_codes, ("qn70h",))

        self.assertEqual(model_code_edges(offers), [])

    def test_one_listing_per_store_is_linked(self):
        """Below the threshold: each store lists the product once. The
        titles are worded differently, but that is one variant PER STORE,
        so the code is trusted and the two listings are linked."""
        offers = [
            _listing('Samsung Neo QLED QN70H 55" 4K Smart TV',
                     "kotsovolos", "Samsung", "tvs"),
            _listing('Samsung QN70H Neo QLED Τηλεόραση 55"',
                     "stephanis", "Samsung", "tvs"),
        ]
        self.assertNotEqual(offers[0].title_norm, offers[1].title_norm)

        self.assertEqual(model_code_edges(offers), [(0, 1)])

    def test_store_with_several_sizes_is_not_linked_to_another_store(self):
        """The store that lists two sizes under one code gets no link
        through that code at all: not between its own listings, and not
        to the single listing of another store."""
        offers = [
            _listing('Samsung Neo QLED QN70H 55" 4K Smart TV',
                     "kotsovolos", "Samsung", "tvs"),
            _listing('Samsung Neo QLED QN70H 65" 4K Smart TV',
                     "kotsovolos", "Samsung", "tvs"),
            _listing('Samsung QN70H Neo QLED Τηλεόραση 55"',
                     "stephanis", "Samsung", "tvs"),
        ]
        self.assertEqual(model_code_edges(offers), [])
        # Neither listing of the first store gets a code.
        _block_spread, _freq, chosen = get_precomputed(offers)
        self.assertIsNone(chosen[0])
        self.assertIsNone(chosen[1])

    def test_repeated_listing_does_not_trip_the_guard(self):
        """A store that lists the very same product twice still has one
        variant, so all three listings are linked."""
        offers = [
            _offer(model_codes=("qe55qn70hatxxh",), store="kotsovolos",
                   category="tvs", title_norm="neo qled qe55qn70hatxxh 55in"),
            _offer(model_codes=("qe55qn70hatxxh",), store="kotsovolos",
                   category="tvs", title_norm="neo qled qe55qn70hatxxh 55in"),
            _offer(model_codes=("qe55qn70hatxxh",), store="stephanis",
                   category="tvs", title_norm="qe55qn70hatxxh smart tv 55in"),
        ]
        self.assertEqual(model_code_edges(offers), [(0, 1), (0, 2)])

    def test_colour_variants_do_not_trip_the_guard(self):
        """A store that lists two colours of one product still has one
        variant, so the colours are linked to each other and to the other
        store's listing."""
        offers = [
            _listing("Sony WH-CH520 Wireless Headphones Black",
                     "kotsovolos", "Sony", "headphones"),
            _listing("Sony WH-CH520 Wireless Headphones Blue",
                     "kotsovolos", "Sony", "headphones"),
            _listing("Sony WH-CH520 Ασύρματα Ακουστικά Λευκά",
                     "public", "Sony", "headphones"),
        ]
        self.assertEqual(model_code_edges(offers), [(0, 1), (0, 2)])

    def test_sizes_still_match_other_stores_through_the_full_model(self):
        """The guard removes the family tag only. A store that prints the
        series code AND the full model on each size is still matched,
        size by size, through the full model."""
        offers = [
            _listing('Samsung Neo QLED QN70H QE55QN70HATXXH 55"',
                     "kotsovolos", "Samsung", "tvs"),
            _listing('Samsung Neo QLED QN70H QE65QN70HATXXH 65"',
                     "kotsovolos", "Samsung", "tvs"),
            _listing('Samsung QE55QN70HATXXH Smart TV 55"',
                     "stephanis", "Samsung", "tvs"),
        ]
        self.assertEqual(model_code_edges(offers), [(0, 2)])


# ===========================================================================
# No-chaining test
# ===========================================================================

class TestNoChaining(unittest.TestCase):
    """Each offer contributes exactly ONE code, so overlapping secondary
    codes do not chain distinct products together."""

    def test_no_chaining_via_secondary_codes(self):
        """Offer A shares code X with offer B; offer B also has code Y shared
        with offer C. But since each offer contributes only one code, B cannot
        chain A to C unless A and C also share the SAME chosen code."""
        offers = [
            # A has a rare code "rare1" and a common code "common1".
            _offer(model_codes=("rare1", "common1"), category="phones",
                   effective_brand="Samsung", store="s1"),
            # B shares "common1" with A and "common2" with C.
            _offer(model_codes=("common1", "common2"), category="phones",
                   effective_brand="Samsung", store="s2"),
            # C has "common2" and a rare code "rare3".
            _offer(model_codes=("common2", "rare3"), category="phones",
                   effective_brand="Samsung", store="s3"),
        ]
        edges = model_code_edges(offers)

        # Each offer contributes the code carried by the most stores.
        # "rare1" and "rare3" are each printed by one store only, so A
        # picks "common1" (stores s1 and s2) and C picks "common2" (stores
        # s2 and s3). B carries both shared codes but can only contribute
        # ONE of them (the tie is broken by length, rarity, then alphabet),
        # so at most one of {A-B} or {B-C} is linked, never both.
        # This prevents A-B-C chaining.

        # Exactly one of the two possible links exists (B picked one side).
        self.assertEqual(len(edges), 1)

        # Verify no transitive chain: A and C should never be in the same edge.
        from matching.union_find import UnionFind
        uf = UnionFind(3)
        for a, b in edges:
            uf.union(a, b)
        # A (0) and C (2) should NOT be connected.
        self.assertFalse(uf.connected(0, 2))


# ===========================================================================
# Review signals tests
# ===========================================================================

class TestReviewSignals(unittest.TestCase):
    """review_signals flags non-trivial clusters (size >= 2) with unresolved
    suspicious brands or cross-effective-brand unions. Singletons are never
    flagged."""

    def test_unresolved_suspicious_brand_flagged(self):
        """A size>=2 cluster with a suspicious-brand member and no
        brand_from_title is flagged."""
        offers = [
            _offer(is_suspicious_brand=True, brand_from_title=None),
            _offer(is_suspicious_brand=False),
        ]
        cluster = [[0, 1]]
        flags = review_signals(offers, cluster)
        reasons = [r for _, r in flags]
        self.assertIn("unresolved_suspicious_brand", reasons)

    def test_resolved_suspicious_brand_not_flagged(self):
        """A suspicious-brand member WITH a brand_from_title is NOT flagged
        for unresolved_suspicious_brand."""
        offers = [
            _offer(is_suspicious_brand=True, brand_from_title="Samsung"),
            _offer(is_suspicious_brand=False),
        ]
        cluster = [[0, 1]]
        flags = review_signals(offers, cluster)
        reasons = [r for _, r in flags]
        self.assertNotIn("unresolved_suspicious_brand", reasons)

    def test_cross_effective_brand_flagged(self):
        """A size>=2 cluster with two distinct non-empty effective_brands
        is flagged as cross_effective_brand."""
        offers = [
            _offer(effective_brand="Samsung"),
            _offer(effective_brand="LG"),
        ]
        cluster = [[0, 1]]
        flags = review_signals(offers, cluster)
        reasons = [r for _, r in flags]
        self.assertIn("cross_effective_brand", reasons)

    def test_singleton_not_flagged(self):
        """A singleton (size 1) with empty effective_brand is NOT flagged —
        there is no union to review."""
        offers = [
            _offer(effective_brand="", is_suspicious_brand=True,
                   brand_from_title=None),
        ]
        cluster = [[0]]
        flags = review_signals(offers, cluster)
        self.assertEqual(flags, [])

    def test_clean_cluster_not_flagged(self):
        """A size>=2 cluster with no issues produces no flags."""
        offers = [
            _offer(effective_brand="Samsung", is_suspicious_brand=False),
            _offer(effective_brand="Samsung", is_suspicious_brand=False),
        ]
        cluster = [[0, 1]]
        flags = review_signals(offers, cluster)
        self.assertEqual(flags, [])


# ===========================================================================
# Cross-brand detector tests
# ===========================================================================

class TestCrossBrandDetector(unittest.TestCase):
    """The cross-brand detector should use effective_brand (the blocking key),
    not brand_norm. A cluster whose members share one effective_brand but
    differ in brand_norm is NOT cross-brand."""

    def test_same_effective_brand_different_brand_norm_not_flagged(self):
        """Members share effective_brand='Philips' but differ in brand_norm
        ('PHILIPSHUE' vs 'Philips'). NOT cross-brand."""
        offers = [
            _offer(effective_brand="Philips", brand_norm="PHILIPSHUE"),
            _offer(effective_brand="Philips", brand_norm="Philips"),
        ]
        cluster = [0, 1]
        # Cross-brand detection logic: distinct non-empty effective_brand values.
        brands = {offers[i].effective_brand for i in cluster
                  if offers[i].effective_brand}
        self.assertEqual(len(brands), 1)  # NOT cross-brand

    def test_two_distinct_effective_brands_flagged(self):
        """Members have two distinct effective_brands -> IS cross-brand."""
        offers = [
            _offer(effective_brand="Samsung", brand_norm="Samsung"),
            _offer(effective_brand="LG", brand_norm="LG"),
        ]
        cluster = [0, 1]
        brands = {offers[i].effective_brand for i in cluster
                  if offers[i].effective_brand}
        self.assertGreaterEqual(len(brands), 2)  # IS cross-brand

    def test_empty_effective_brand_ignored_in_cross_brand(self):
        """A member with empty effective_brand should not count as a distinct
        brand — only non-empty values matter."""
        offers = [
            _offer(effective_brand="Samsung", brand_norm="Samsung"),
            _offer(effective_brand="", brand_norm=""),
        ]
        cluster = [0, 1]
        brands = {offers[i].effective_brand for i in cluster
                  if offers[i].effective_brand}
        self.assertEqual(len(brands), 1)  # NOT cross-brand


# ===========================================================================
# Deterministic ordering tests
# ===========================================================================

class TestDeterministicOrdering(unittest.TestCase):
    """Same input produces identical groups and edges across calls."""

    def test_groups_deterministic(self):
        """model_code_groups returns identical output on repeated calls."""
        offers = [
            _offer(model_codes=("abc123",), category="phones",
                   effective_brand="Samsung", store="b"),
            _offer(model_codes=("abc123",), category="phones",
                   effective_brand="Samsung", store="a"),
            _offer(model_codes=("xyz789",), category="phones",
                   effective_brand="Samsung", store="c"),
            _offer(model_codes=("xyz789",), category="phones",
                   effective_brand="Samsung", store="d"),
        ]
        g1 = model_code_groups(offers)
        g2 = model_code_groups(offers)
        self.assertEqual(g1, g2)

    def test_edges_deterministic(self):
        """model_code_edges returns identical output on repeated calls."""
        offers = [
            _offer(model_codes=("abc123",), category="phones",
                   effective_brand="Samsung", store="b"),
            _offer(model_codes=("abc123",), category="phones",
                   effective_brand="Samsung", store="a"),
            _offer(model_codes=("xyz789",), category="phones",
                   effective_brand="Samsung", store="c"),
            _offer(model_codes=("xyz789",), category="phones",
                   effective_brand="Samsung", store="d"),
        ]
        e1 = model_code_edges(offers)
        e2 = model_code_edges(offers)
        self.assertEqual(e1, e2)


if __name__ == "__main__":
    unittest.main()
