import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from scraper import write_json_file, fetch_all_jobs, fetch_company_jobs_ashby


class ScraperWriteJsonFileTests(unittest.TestCase):
    def test_fetch_all_jobs_respects_per_provider_limit(self):
        def fake_fetcher(slug):
            return slug, [{"title": f"job-{slug}-{i}"} for i in range(5)], 200, 0

        active, jobs = fetch_all_jobs(["a", "b"], fake_fetcher, platform="TEST", max_jobs_per_provider=7)

        self.assertEqual(len(jobs), 7)
        self.assertEqual(sorted(active.values()), [2, 5])  # whichever board answers first keeps all 5

    def test_limit_cancels_boards_still_queued(self):
        calls = []

        def fake_fetcher(slug):
            calls.append(slug)
            time.sleep(0.01)
            return slug, [{"title": f"job-{slug}"}], 200, 0

        active, jobs = fetch_all_jobs([f"s{i}" for i in range(200)], fake_fetcher, platform="TEST",
                                      max_jobs_per_provider=3)

        self.assertEqual(len(jobs), 3)
        self.assertLess(len(calls), 100)  # 30 workers: only boards already running finish

    def test_ashby_reads_the_public_posting_api(self):
        board = {"jobs": [
            {"id": "1", "title": " Senior Backend Engineer", "location": "Remote (US)", "isListed": True,
             "descriptionPlain": "Go, Postgres, Kafka", "jobUrl": "https://jobs.ashbyhq.com/acme/1"},
            {"id": "2", "title": "Staff Engineer", "isListed": False},
            {"id": "3", "title": "Office Manager", "isListed": True},
        ]}
        resp = mock.Mock(status_code=200)
        resp.json.return_value = board
        with mock.patch("scraper.requests.get", return_value=resp) as get, mock.patch("scraper.time.sleep"):
            slug, jobs, status, dropped = fetch_company_jobs_ashby("acme")

        self.assertIn("api.ashbyhq.com/posting-api/job-board/acme", get.call_args[0][0])
        self.assertEqual((status, dropped, len(jobs)), (200, 1, 1))
        self.assertEqual(jobs[0]["title"], "Senior Backend Engineer")
        self.assertEqual(jobs[0]["location"], "Remote (US)")
        self.assertEqual(jobs[0]["description"], "Go, Postgres, Kafka")
        self.assertEqual(jobs[0]["url"], "https://jobs.ashbyhq.com/acme/1")

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
