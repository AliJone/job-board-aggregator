import unittest

from scraper import clean_job_data, evaluate_lead_intent


class LeadIntentScoringTests(unittest.TestCase):
    def test_noise_filter_excludes_low_signal_jobs(self):
        result = evaluate_lead_intent(
            {
                "title": "Webflow Developer",
                "description": "Build a landing page and simple wrapper for a no-code app.",
                "company": "Example",
            }
        )
        self.assertTrue(result["is_excluded"])
        self.assertLess(result["score"], 0)

    def test_clean_job_data_keeps_scored_jobs_and_drops_noise(self):
        jobs = [
            {
                "title": "Senior Platform Engineer",
                "description": "Designing a multi-tenant system with gRPC and role-based access control.",
                "company": "Example",
                "url": "https://example.com/job/1",
            },
            {
                "title": "Webflow Developer",
                "description": "Build a landing page and simple wrapper for a no-code app.",
                "company": "Example",
                "url": "https://example.com/job/2",
            },
        ]
        cleaned = clean_job_data(jobs)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["title"], "Senior Platform Engineer")


if __name__ == "__main__":
    unittest.main()
