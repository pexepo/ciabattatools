import unittest

from src.mtproto.gifts import Attribute, OwnerInfo, UniqueGift, slug_for
from src.tools.tracker.service import _load_filters
from src.tools.tracker.watcher import Filters, Watcher, matches


class TrackerFiltersTest(unittest.TestCase):
    def test_select_all_marker_does_not_restrict_to_market_catalogue(self):
        filters = _load_filters(
            '{"collection":["Astral Shard"],"collection_all":true}', None
        )
        gift = UniqueGift("NewGift-1", "New Gift", 1, 1)
        gift.owner = OwnerInfo(user_id=1)

        self.assertTrue(filters.collections_all)
        self.assertTrue(matches(gift, filters))

    def test_exact_collection_and_model_still_filter(self):
        gift = UniqueGift(
            "AstralShard-1",
            "Astral Shard",
            1,
            1,
            model=Attribute("model", "Black Hole"),
            owner=OwnerInfo(username="seller"),
        )

        self.assertTrue(
            matches(
                gift,
                Filters(collections={"astral shard"}, models={"black hole"}),
            )
        )
        self.assertFalse(matches(gift, Filters(models={"Moon"})))

    def test_only_reachable_owners_and_reputation_range_match(self):
        hidden = UniqueGift("Hidden-1", "Hidden", 1, 1)
        self.assertFalse(matches(hidden, Filters()))

        reachable = UniqueGift(
            "Reachable-1",
            "Reachable",
            1,
            1,
            owner=OwnerInfo(user_id=42, reputation_level=7),
        )
        self.assertTrue(
            matches(reachable, Filters(reputation_min=5, reputation_max=10))
        )
        self.assertFalse(matches(reachable, Filters(reputation_max=6)))


class TrackerCatalogueTest(unittest.IsolatedAsyncioTestCase):
    async def test_replacing_catalogue_preserves_existing_watermark(self):
        watcher = Watcher(object(), on_found=lambda _: None)
        watcher.replace_collections([("Astral Shard", "AstralShard-1")])
        watcher.collections["astral shard"].last_issued = 42

        watcher.replace_collections(
            [
                ("Astral Shard", "AstralShard-1"),
                ("B-Day Candle", "BDayCandle-1"),
            ]
        )

        self.assertEqual(watcher.collections["astral shard"].last_issued, 42)
        self.assertIsNone(watcher.collections["b-day candle"].last_issued)


class GiftSlugTest(unittest.TestCase):
    def test_telegram_punctuation_is_removed(self):
        self.assertEqual(slug_for("B-Day Candle", 7), "BDayCandle-7")
        self.assertEqual(slug_for("Durov’s Cap", 3), "DurovsCap-3")
        self.assertEqual(slug_for("Jack-in-the-Box", 9), "JackintheBox-9")


if __name__ == "__main__":
    unittest.main()
