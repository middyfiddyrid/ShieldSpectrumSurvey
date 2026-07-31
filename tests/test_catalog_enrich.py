import os
import tempfile
import unittest
from unittest.mock import patch

from survey.catalog_enrich import enrich_catalog


class FakeConfig:
    def __init__(self, root, enabled=True):
        self.path = os.path.join(root, "config.yaml")
        self.values = {
            ("catalog_enrichment", "enabled"): enabled,
            ("catalog_enrichment", "timeout_seconds"): 1.0,
            ("catalog_enrichment", "cache_hours"): 24,
            ("catalog_enrichment", "max_results"): 3,
            ("catalog_enrichment", "sources"): [{
                "name": "Signal Identification Wiki",
                "api_url": "https://www.sigidwiki.com/api.php",
                "page_base_url": "https://www.sigidwiki.com/wiki/",
            }],
            ("cache", "dir"): "cache",
        }

    def get(self, *keys, default=None):
        return self.values.get(keys, default)


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"query": {"search": [{
            "title": "POCSAG",
            "snippet": "A <b>paging</b> protocol",
        }]}}


class TestCatalogEnrichment(unittest.TestCase):
    def test_disabled_never_uses_network(self):
        with tempfile.TemporaryDirectory() as root, patch(
                "survey.catalog_enrich.requests.get") as get:
            result = enrich_catalog(152.24, [], FakeConfig(root, enabled=False))
        self.assertEqual(result["status"], "disabled")
        get.assert_not_called()

    def test_online_result_is_sanitized_and_cached(self):
        with tempfile.TemporaryDirectory() as root, patch(
                "survey.catalog_enrich.requests.get", return_value=FakeResponse()) as get:
            cfg = FakeConfig(root)
            result = enrich_catalog(152.24, [{"name": "POCSAG"}], cfg)
            cached = enrich_catalog(152.24, [{"name": "POCSAG"}], cfg)
        self.assertEqual(result["status"], "online")
        self.assertEqual(result["items"][0]["snippet"], "A paging protocol")
        self.assertTrue(result["items"][0]["url"].endswith("/POCSAG"))
        self.assertEqual(cached["status"], "cache")
        self.assertEqual(get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
