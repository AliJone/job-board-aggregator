import unittest

from job_filters import matches_search, parse_search

JOB = {
    "title": "Senior Software Engineer",
    "company": "mozilla",
    "location": "Remote",
    "salary": {"median": 148925},
    "ats": "Greenhouse",
    "skill_level": "senior",
    "is_recruiter": False,
}


class SearchFilterTests(unittest.TestCase):
    def test_title_matches_whole_words_case_insensitive(self):
        self.assertTrue(matches_search(JOB, {"title": "software engineer"}))
        self.assertTrue(matches_search(JOB, {"title": "SOFTWARE"}))
        self.assertFalse(matches_search(JOB, {"title": "data engineer"}))
        self.assertFalse(matches_search(JOB, {"title": "soft"}))

    def test_exclude_keyword_drops_job(self):
        self.assertFalse(matches_search(JOB, {"exclude": "intern, senior"}))
        self.assertTrue(matches_search(JOB, {"exclude": "intern, staff"}))

    def test_minimum_salary(self):
        self.assertTrue(matches_search(JOB, {"salary": 100000}))
        self.assertFalse(matches_search(JOB, {"salary": 200000}))
        self.assertFalse(matches_search({**JOB, "salary": None}, {"salary": 1}))

    def test_no_search_keeps_every_job(self):
        self.assertTrue(matches_search(JOB, {}))
        self.assertEqual(parse_search(None), ({}, None))

    def test_parse_search_validates_types_and_drops_unknown_keys(self):
        search, error = parse_search({"title": " Software Engineer ", "location": "", "nope": 1, "salary": 0})
        self.assertIsNone(error)
        self.assertEqual(search, {"title": "software engineer"})
        self.assertEqual(parse_search({"salary": "100k"})[1], "search.salary must be a number")
        self.assertEqual(parse_search({"remoteOnly": "yes"})[1], "search.remoteOnly must be a boolean")
        self.assertEqual(parse_search({"posted": True})[1], "search.posted must be a number")
        self.assertEqual(parse_search(["title"])[1], "search must be an object")


if __name__ == "__main__":
    unittest.main()
