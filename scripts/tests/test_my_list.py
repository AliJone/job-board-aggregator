import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import my_list
from my_list import MyList, pick_new

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import server  # noqa: E402

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)


def ago(days):
    return (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")


class PickNewTests(unittest.TestCase):
    ORDER = [("greenhouse", "a"), ("greenhouse", "b"), ("lever", "c"), ("greenhouse", "d"), ("lever", "e")]

    def test_skips_my_list_recent_tries_and_dead(self):
        picked = pick_new(
            self.ORDER, 10,
            on_list={"greenhouse:a"},
            tried={"greenhouse:b": {"last_checked_at": ago(3)}, "lever:c": {"last_checked_at": ago(45)}},
            dead={"greenhouse:d"},
            now=NOW,
        )
        # b was tried 3 days ago (skipped); c was tried 45 days ago (due again).
        self.assertEqual(picked, [("lever", "c"), ("lever", "e")])

    def test_takes_first_n_in_order(self):
        self.assertEqual(pick_new(self.ORDER, 2, set(), {}, set(), NOW), self.ORDER[:2])
        self.assertEqual(pick_new(self.ORDER, 0, set(), {}, set(), NOW), [])

    def test_shuffle_is_fixed(self):
        candidates = [("greenhouse", str(i)) for i in range(50)]
        self.assertEqual(my_list.shuffled(candidates), my_list.shuffled(list(reversed(candidates))))


class MyListFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "my_list.json")
        self.lst = MyList(self.path, {"greenhouse": ["gitlab"]})

    def tearDown(self):
        self.dir.cleanup()

    def entry(self, platform, company_id):
        return next((e for e in self.lst.snapshot()[0] if (e["platform"], e["id"]) == (platform, company_id)), None)

    def test_missing_file_starts_from_defaults(self):
        self.assertEqual(self.lst.companies(), {"greenhouse": ["gitlab"]})
        self.assertEqual(self.entry("greenhouse", "gitlab")["source"], "you")
        os.remove(self.path)
        self.assertEqual(self.lst.companies(), {"greenhouse": ["gitlab"]})

    def test_match_adds_company_as_auto(self):
        self.assertTrue(self.lst.record_result("lever", "spotify", 3, auto_add=True))
        e = self.entry("lever", "spotify")
        self.assertEqual((e["source"], e["matched_total"]), ("auto", 3))
        self.assertIsNotNone(e["last_matched_at"])

    def test_match_bumps_existing_company(self):
        self.assertFalse(self.lst.record_result("greenhouse", "gitlab", 2, auto_add=True))
        self.lst.record_result("greenhouse", "gitlab", 1, auto_add=True)
        e = self.entry("greenhouse", "gitlab")
        self.assertEqual((e["source"], e["matched_total"]), ("you", 3))

    def test_no_match_records_tried_and_keeps_list(self):
        self.assertFalse(self.lst.record_result("greenhouse", "gitlab", 0, auto_add=True))
        self.assertFalse(self.lst.record_result("lever", "nomatch", 0, auto_add=True))
        entries, tried = self.lst.snapshot()
        self.assertEqual([e["id"] for e in entries], ["gitlab"])   # never auto-removed
        self.assertIn("lever:nomatch", tried)

    def test_auto_add_off_does_not_add(self):
        self.assertFalse(self.lst.record_result("lever", "spotify", 3, auto_add=False))
        self.assertIsNone(self.entry("lever", "spotify"))

    def test_saved_file_is_valid_json(self):
        self.lst.add({"lever": ["spotify"]})
        with open(self.path) as f:
            self.assertEqual(len(json.load(f)["companies"]), 2)


class MyListApiTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.saved = server.MY_LIST
        server.MY_LIST = MyList(os.path.join(self.dir.name, "my_list.json"), {"greenhouse": ["gitlab"]})
        self.client = server.app.test_client()

    def tearDown(self):
        server.MY_LIST = self.saved
        self.dir.cleanup()

    def scrape_error(self, **extra):
        res = self.client.post("/api/scrape", json={"companies": {"greenhouse": ["gitlab"]}, **extra})
        return res.status_code, res.get_json().get("error")

    def test_rejects_bad_discover(self):
        for bad in (-1, 201, "20", 2.5, True):
            self.assertEqual(self.scrape_error(discover=bad)[0], 400, bad)
        self.assertEqual(self.scrape_error(auto_add="yes")[0], 400)

    def test_rejects_bad_post(self):
        for bad in (None, {}, {"nope": ["x"]}, {"greenhouse": "gitlab"}, {"greenhouse": []},
                    {"greenhouse": [""]}, {"greenhouse": ["  "]}, {"greenhouse": [5]}):
            res = self.client.post("/api/my-list", json={"companies": bad})
            self.assertEqual(res.status_code, 400, bad)

    def test_post_and_delete(self):
        res = self.client.post("/api/my-list", json={"companies": {"lever": ["spotify"]}})
        self.assertEqual((res.status_code, res.get_json()["added"]), (200, 1))
        self.assertEqual(self.client.delete("/api/my-list/nope/spotify").status_code, 400)
        self.assertEqual(self.client.delete("/api/my-list/lever/%20").status_code, 400)
        self.assertEqual(self.client.delete("/api/my-list/lever/missing").status_code, 404)
        self.assertEqual(self.client.delete("/api/my-list/lever/spotify").status_code, 200)
        self.assertEqual(server.MY_LIST.companies(), {"greenhouse": ["gitlab"]})


if __name__ == "__main__":
    unittest.main()
