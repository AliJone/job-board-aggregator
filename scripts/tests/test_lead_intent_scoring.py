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

    def test_terms_record_the_matched_phrases(self):
        result = evaluate_lead_intent({"title": "Engineer", "company": "Example",
                                       "description": "Pay down technical debt in our React app."})
        self.assertEqual(result["terms"]["technicalDebt"], ["technical debt"])
        self.assertEqual(result["terms"]["teamStack"], ["react"])
        self.assertNotIn("highScale", result["terms"])

    def test_team_stack_is_a_capped_fit_signal(self):
        result = evaluate_lead_intent({"title": "Senior Full Stack Engineer", "company": "Example",
                                       "description": "We build with React, Node.js, Next.js, Nuxt and Vue."})
        self.assertTrue(result["matches"]["teamStack"])
        self.assertEqual(result["score"], 6)  # 2 per hit, at most 3 hits

    def test_team_stack_ignores_lookalikes(self):
        result = evaluate_lead_intent({"title": "Account Executive", "company": "Example",
                                       "description": "Go to market with partners at example.net; C level buyers."})
        self.assertFalse(result["matches"]["teamStack"])
        self.assertEqual(result["score"], 0)

    def test_team_stack_reads_dotnet_go_and_qa(self):
        result = evaluate_lead_intent({"title": "SDET", "company": "Example",
                                       "description": "ASP.NET and C# services, golang workers, Playwright suites."})
        self.assertEqual(result["score"], 6)


if __name__ == "__main__":
    unittest.main()
