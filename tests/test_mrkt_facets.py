"""Tests for MRKT model/backdrop batching."""

import unittest
from typing import Any

from src.markets.mrkt.client import (
    FACET_COLLECTION_BATCH_SIZE,
    MrktClient,
    MrktError,
)


class StubMrktClient(MrktClient):
    def __init__(self, maximum: int | None = None):
        self.maximum = maximum
        self.calls: list[tuple[str, list[str]]] = []

    async def _request(
        self,
        method: str,
        path: str,
        json: dict | None = None,
        **kwargs: Any,
    ) -> list[dict[str, str]]:
        assert method == "POST"
        assert json is not None
        collections = list(json["Collections"])
        self.calls.append((path, collections))
        if self.maximum is not None and len(collections) > self.maximum:
            raise MrktError(f"MRKT {path}: Too many gifts collections")
        return [{"name": name} for name in collections]


class TestFacetBatching(unittest.IsolatedAsyncioTestCase):
    async def test_models_are_loaded_in_bounded_batches(self):
        client = StubMrktClient()
        names = [f"Collection {index}" for index in range(45)]

        rows = await client.models(names)

        self.assertEqual([row["name"] for row in rows], names)
        self.assertEqual(
            [len(collections) for _, collections in client.calls],
            [FACET_COLLECTION_BATCH_SIZE] * 4 + [5],
        )
        self.assertTrue(all(path == "/gifts/models" for path, _ in client.calls))

    async def test_backdrops_deduplicate_collection_names(self):
        client = StubMrktClient()

        rows = await client.backdrops(["Collection A", "Collection A", "Collection B"])

        self.assertEqual(
            [row["name"] for row in rows], ["Collection A", "Collection B"]
        )
        self.assertEqual(
            client.calls,
            [("/gifts/backdrops", ["Collection A", "Collection B"])],
        )

    async def test_rejected_batch_is_split_again(self):
        client = StubMrktClient(maximum=3)
        names = [f"Collection {index}" for index in range(7)]

        rows = await client.models(names)

        self.assertEqual([row["name"] for row in rows], names)
        self.assertGreater(len(client.calls), 1)
        successful = [batch for _, batch in client.calls if len(batch) <= 3]
        self.assertTrue(successful)


if __name__ == "__main__":
    unittest.main(verbosity=2)
