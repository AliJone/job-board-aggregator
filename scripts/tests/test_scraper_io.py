import json
import os
import tempfile
import unittest
from pathlib import Path

from scraper import write_json_file, fetch_all_jobs


class ScraperWriteJsonFileTests(unittest.TestCase):
    def test_fetch_all_jobs_respects_per_provider_limit(self):
        def fake_fetcher(slug):
            return slug, [{"title": f"job-{slug}-{i}"} for i in range(5)], 200

        active, jobs = fetch_all_jobs(["a", "b"], fake_fetcher, platform="TEST", max_jobs_per_provider=7)

        self.assertEqual(len(jobs), 7)
        self.assertEqual(active["a"], 5)
        self.assertEqual(active["b"], 2)

    def test_overwrites_read_only_existing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "cache.json"
            target.write_text('{"old": true}\n', encoding="utf-8")
            os.chmod(target, 0o444)

            payload = {"new": True, "items": [1, 2, 3]}
            write_json_file(target, payload)

            self.assertTrue(target.exists())
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), payload)


if __name__ == "__main__":
    unittest.main()
