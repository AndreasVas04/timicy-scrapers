"""
test_load.py
------------
Unit tests for matching/load.py, specifically the _build_offer function
that constructs EnrichedOffer instances from raw database row dicts.

Uses synthetic row dicts — does not hit the database.

Run with:
    python -m pytest tests/test_load.py -v
    # or
    python -m unittest tests.test_load -v
"""

import unittest

from matching.load import _build_offer


# ---------------------------------------------------------------------------
# Helper: build a minimal row dict matching the shape _build_offer expects.
# Only the fields under test need realistic values; everything else gets
# safe defaults that satisfy the function's expectations.
# ---------------------------------------------------------------------------

def _row(
    *,
    vendor: str | None = "TestVendor",
    title: str = "Test Product",
    category: str | None = "smartphones",
    mpn_root: str | None = None,
    mpn: str | None = None,
    ean: str | None = None,
    identifier_source: str = "none",
) -> dict:
    """Build a synthetic store_products row dict for _build_offer."""
    return {
        "store": "teststore",
        "store_product_id": "sp-001",
        "title": title,
        "vendor": vendor,
        "category": category,
        "sku": None,
        "current_price": None,
        "available": True,
        "image_url": None,
        "product_url": "https://example.com/p/1",
        "mpn": mpn,
        "mpn_root": mpn_root,
        "ean": ean,
        "identifier_source": identifier_source,
    }


# ===========================================================================
# _build_offer: effective_brand used for mpn_root_key / mpn_key
# ===========================================================================

class TestBuildOfferMpnKeysUseEffectiveBrand(unittest.TestCase):
    """mpn_root_key and mpn_key must be built from effective_brand (the
    trusted brand resolution), not the raw vendor-derived brand_norm.

    This ensures offers with missing or defective vendor data still get
    identity keys when a brand can be extracted from the title.
    """

    def test_null_vendor_brand_from_title(self):
        """vendor=None, but title contains a known brand ('iRobot').

        effective_brand should be resolved from the title, and mpn_root_key
        must be non-None and contain the resolved brand ('irobot').
        """
        row = _row(
            vendor=None,
            title="iROBOT L121040 Roomba Combo 205",
            mpn_root="L121040",
        )
        offer = _build_offer(row)
        # effective_brand should have been resolved from the title
        self.assertIsNotNone(offer.effective_brand)
        # mpn_root_key must exist and contain the resolved brand
        self.assertIsNotNone(offer.mpn_root_key)
        self.assertIn("irobot", offer.mpn_root_key)

    def test_typo_vendor_converges_with_correct_spelling(self):
        """vendor='PHLIPS' (typo) and vendor='Philips' must produce the
        same mpn_root_key for the same mpn_root value.

        The PHLIPS alias resolves to the Philips canonical, so both
        effective_brand values are 'Philips' and the keys converge.
        """
        typo_row = _row(vendor="PHLIPS", mpn_root="FC8243/09")
        correct_row = _row(vendor="Philips", mpn_root="FC8243/09")
        typo_offer = _build_offer(typo_row)
        correct_offer = _build_offer(correct_row)
        self.assertIsNotNone(typo_offer.mpn_root_key)
        self.assertEqual(typo_offer.mpn_root_key, correct_offer.mpn_root_key)

    def test_trustworthy_vendor_unchanged(self):
        """Regression guard: for a trustworthy vendor like 'Apple',
        effective_brand == brand_norm, so mpn_root_key is unchanged
        from the previous brand_norm-based behavior.
        """
        row = _row(vendor="Apple", mpn_root="MQDT3ZM/A")
        offer = _build_offer(row)
        # brand_norm and effective_brand should both be "Apple"
        self.assertEqual(offer.brand_norm, "Apple")
        self.assertEqual(offer.effective_brand, "Apple")
        # mpn_root_key must exist and contain the brand
        self.assertIsNotNone(offer.mpn_root_key)
        self.assertIn("apple", offer.mpn_root_key)


if __name__ == "__main__":
    unittest.main()
