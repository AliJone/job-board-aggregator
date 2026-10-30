import os
import sys
import unittest
from datetime import datetime, timezone

from job_filters import evaluate_role, matches_search, parse_roles
from scraper import _lever_posted_at

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import server  # noqa: E402


def role(title, roles):
    return evaluate_role({"title": title}, roles)[0]


class RoleTests(unittest.TestCase):
    def test_each_preset(self):
        self.assertTrue(role("Backend Engineer", ["swe"]))
        self.assertTrue(role("Machine Learning Engineer", ["ai_ml"]))
        self.assertTrue(role("Research Engineer, Agents", ["ai_ml"]))
        self.assertTrue(role("Analytics Engineer", ["data"]))
        self.assertTrue(role("Site Reliability Engineer", ["devops"]))
        self.assertFalse(role("Machine Learning Engineer", ["swe"]))
        self.assertFalse(role("Maintenance Engineer", ["ai_ml"]))  # "ai" only as a whole word

    def test_developer_titles_in_software_engineer(self):
        for title in ("Python Developer", "Senior React Native Engineer", ".NET Developer", "C++ Engineer",
                      "Engineer, Backend", "Member of Technical Staff"):
            self.assertTrue(role(title, ["swe"]), title)
        self.assertFalse(role("Go-To-Market Engineer", ["swe"]))

    def test_specialist_presets(self):
        self.assertTrue(role("Systems Engineer, SSL/TLS Team", ["systems"]))
        self.assertFalse(role("IT Systems Engineer", ["systems"]))
        self.assertTrue(role("Forward-Deployed Engineer", ["fde"]))
        self.assertTrue(role("Senior Identity Security Engineer", ["security"]))
        self.assertTrue(role("QA Automation Engineer", ["qa"]))
        self.assertFalse(role("Manufacturing Quality Engineer", ["qa"]))
        self.assertTrue(role("Firmware Engineer", ["embedded"]))
        for title in ("Customer Engineer", "Sales Engineer", "Technical Support Engineer"):
            self.assertFalse(role(title, list(server.job_filters.ROLES)), title)

    def test_any_selected_role_passes(self):
        self.assertTrue(role("Machine Learning Engineer", ["swe", "ai_ml"]))
        self.assertFalse(role("Account Executive", ["swe", "ai_ml", "data", "devops"]))

    def test_no_roles_means_any_and_seniority_is_excluded(self):
        self.assertTrue(role("Account Executive", []))
        self.assertFalse(role("Staff Machine Learning Engineer", ["ai_ml"]))

    def test_parse_roles(self):
        self.assertEqual(parse_roles(None), ([], None))
        self.assertEqual(parse_roles(["ai_ml", "swe", "ai_ml"]), (["ai_ml", "swe"], None))
        self.assertIsNotNone(parse_roles(["nope"])[1])
        self.assertIsNotNone(parse_roles("swe")[1])

    def test_api_rejects_bad_roles(self):
        res = server.app.test_client().post(
            "/api/scrape", json={"companies": {"greenhouse": ["gitlab"]}, "roles": ["nope"]})
        self.assertEqual(res.status_code, 400)
        self.assertIn("roles must be a list of", res.get_json()["error"])


class PostedDateTests(unittest.TestCase):
    NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)

    def test_posted_at_wins_over_updated_at(self):
        # Greenhouse: edited 2 days ago, first published 2 months ago.
        job = {"title": "x", "posted_at": "2026-09-03T13:30:34-04:00", "updated_at": "2026-10-29T10:42:09-04:00"}
        self.assertFalse(matches_search(job, {"posted": 7}, now=self.NOW))
        self.assertTrue(matches_search(job, {"posted": 60}, now=self.NOW))

    def test_lever_created_at_is_converted(self):
        self.assertEqual(_lever_posted_at(1792767640768), "2026-10-23T15:00:40.768000Z")
        self.assertIsNone(_lever_posted_at(None))


if __name__ == "__main__":
    unittest.main()
