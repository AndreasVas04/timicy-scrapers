"""
test_writer_representative.py
-----------------------------
Unit tests for the "from" price range the writer stores on a product
(designs AGG-MINPRICE): only available offers set the range, prices that are
missing or not above zero are ignored, and a product with nothing available
keeps its last known range.

Uses small synthetic offers and no database.

Run with:
    python -m pytest tests/test_writer_representative.py -v
"""

import unittest
from decimal import Decimal

from matching.load import EnrichedOffer
from matching.writer import _price_range, _select_representative


def _offer(price, available=True, store="teststore"):
    """Minimal offer with only the fields the price range reads."""
    return EnrichedOffer(
        store=store,
        store_product_id="sp-001",
        title="Test Product",
        vendor=None,
        category="smartphones",
        sku=None,
        price=None if price is None else Decimal(str(price)),
        available=available,
        image_url=None,
        product_url="https://example.com/p/1",
        mpn=None,
        mpn_root=None,
        ean=None,
        identifier_source="none",
    )


class PriceRangeTests(unittest.TestCase):
    def test_all_available(self):
        r = _price_range([_offer(100), _offer(150), _offer(120)])
        self.assertEqual(r, (Decimal(100), Decimal(150)))

    def test_cheapest_unavailable_min_moves_up(self):
        r = _price_range([_offer(80, available=False), _offer(100), _offer(150)])
        self.assertEqual(r, (Decimal(100), Decimal(150)))

    def test_dearest_unavailable_max_moves_down(self):
        r = _price_range([_offer(100), _offer(120), _offer(300, available=False)])
        self.assertEqual(r, (Decimal(100), Decimal(120)))

    def test_nothing_available_keeps_all_priced_offers(self):
        r = _price_range([_offer(80, available=False), _offer(200, available=False)])
        self.assertEqual(r, (Decimal(80), Decimal(200)))

    def test_zero_and_negative_prices_ignored(self):
        r = _price_range([_offer(0), _offer(-5), _offer(90), _offer(110)])
        self.assertEqual(r, (Decimal(90), Decimal(110)))

    def test_only_non_positive_prices_gives_none(self):
        self.assertEqual(_price_range([_offer(0), _offer(-1, available=False)]), (None, None))

    def test_no_prices_gives_none(self):
        self.assertEqual(_price_range([_offer(None), _offer(None, available=False)]), (None, None))
        self.assertEqual(_price_range([]), (None, None))

    def test_available_offer_without_price_falls_back_to_priced_unavailable(self):
        # The only available offer has no price, so nothing buyable is priced:
        # the old behaviour (all priced offers) applies.
        r = _price_range([_offer(None), _offer(70, available=False), _offer(90, available=False)])
        self.assertEqual(r, (Decimal(70), Decimal(90)))

    def test_single_available_offer(self):
        self.assertEqual(_price_range([_offer(55)]), (Decimal(55), Decimal(55)))


class RepresentativeUsesPriceRangeTests(unittest.TestCase):
    def test_representative_min_price_skips_unavailable(self):
        offers = [_offer(80, available=False, store="a"), _offer(100, store="b")]
        rep = _select_representative([0, 1], offers, "title", "k", set())
        self.assertEqual(rep["min_price"], Decimal(100))
        self.assertEqual(rep["max_price"], Decimal(100))
        self.assertTrue(rep["has_available_offer"])
        self.assertEqual(rep["offer_count"], 2)
        self.assertEqual(rep["store_count"], 2)


if __name__ == "__main__":
    unittest.main()
