import os
import tempfile
import unittest

from enrich_leads import (
    CONFIDENCE,
    dedupe_people,
    detect_pattern,
    guess_candidates,
    humanize_company,
    parse_linkedin_title,
    pick_company_domain,
    rank_companies,
    registered_domain,
    resolve_email,
    select_decision_makers,
    verify_email,
)
from lead_providers import QuotaTracker, normalize_verdict, template_to_pattern


class FakeProvider:
    def __init__(self, name, finder=None, verdicts=None):
        self.name = name
        self.verify_cost = 1
        self.verify_bucket = None
        self.finder = finder
        self.verdicts = list(verdicts or [])
        self.finder_calls = 0
        self.verify_calls = []

    def available(self, cost=1, bucket=None):
        return True

    def email_finder(self, first, last, domain):
        self.finder_calls += 1
        return self.finder

    def verify(self, email):
        self.verify_calls.append(email)
        return normalize_verdict(self.verdicts.pop(0)) if self.verdicts else None


class RankingTests(unittest.TestCase):
    def test_humanize_company(self):
        self.assertEqual(humanize_company("acme-corp", "Greenhouse"), "Acme Corp")
        self.assertEqual(humanize_company("kohls|wd1|kohlscareers", "Workday"), "Kohls")
        self.assertEqual(humanize_company("Great American Insurance", "Paylocity"), "Great American Insurance")

    def test_rank_companies_orders_by_best_score(self):
        jobs = [
            {"company": "low", "lead_intent_score": 2, "ats": "Lever", "title": "Dev", "url": "u1"},
            {"company": "high", "lead_intent_score": 9, "ats": "Lever", "title": "Platform Eng", "url": "u2"},
            {"company": "high", "lead_intent_score": 4, "ats": "Lever", "title": "SRE", "url": "u3"},
        ]
        ranked = rank_companies(jobs, top=5, min_score=4)
        self.assertEqual([c["company"] for c in ranked], ["High"])
        self.assertEqual(ranked[0]["job_count"], 2)


class ParsingTests(unittest.TestCase):
    def test_parse_linkedin_title(self):
        self.assertEqual(parse_linkedin_title("Jane Doe - CTO - Acme | LinkedIn"), ("Jane Doe", "CTO", "Acme"))
        self.assertEqual(parse_linkedin_title("John Smith - VP Engineering at Acme | LinkedIn"),
                         ("John Smith", "VP Engineering", "Acme"))
        self.assertIsNone(parse_linkedin_title("Acme | LinkedIn"))

    def test_registered_domain(self):
        self.assertEqual(registered_domain("https://www.acme.com/about"), "acme.com")
        self.assertEqual(registered_domain("https://careers.acme.co.uk/jobs"), "acme.co.uk")

    def test_pick_company_domain_skips_job_boards(self):
        results = [
            {"href": "https://www.linkedin.com/company/acme"},
            {"href": "https://boards.greenhouse.io/acme"},
            {"href": "https://www.acme.com/about"},
        ]
        self.assertEqual(pick_company_domain("Acme", results), "acme.com")

    def test_template_to_pattern(self):
        self.assertEqual(template_to_pattern("{first}.{last}"), "first.last")
        self.assertEqual(template_to_pattern("{f}{last}"), "flast")
        self.assertIsNone(template_to_pattern("{weird}"))


class DecisionMakerTests(unittest.TestCase):
    def test_select_prefers_senior_technical_roles(self):
        people = [
            {"name": "A Sales", "title": "VP Sales"},
            {"name": "B Manager", "title": "Engineering Manager"},
            {"name": "C Tech", "title": "CTO"},
            {"name": "D Head", "title": "Head of Engineering"},
        ]
        chosen = select_decision_makers(people, k=2)
        self.assertEqual([p["name"] for p in chosen], ["C Tech", "D Head"])

    def test_dedupe_merges_fields_from_sources(self):
        people = [
            {"name": "Jane Doe", "title": "CTO", "linkedin": "https://linkedin.com/in/jd", "source": "duckduckgo"},
            {"name": "jane doe", "title": "", "email": "jane@acme.com", "email_confidence": 92, "source": "hunter"},
        ]
        merged = dedupe_people(people)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["email"], "jane@acme.com")
        self.assertEqual(merged[0]["linkedin"], "https://linkedin.com/in/jd")
        self.assertEqual(merged[0]["sources"], ["duckduckgo", "hunter"])


class EmailPatternTests(unittest.TestCase):
    def test_detect_pattern_confirmed_from_team_page(self):
        pattern, confidence = detect_pattern(["Jane Doe"], ["jane.doe@acme.com", "info@acme.com"], "acme.com")
        self.assertEqual((pattern, confidence), ("first.last", "confirmed"))

    def test_guess_candidates_puts_known_pattern_first(self):
        guesses = guess_candidates("John Smith", "acme.com", "flast")
        self.assertEqual(guesses[0], "jsmith@acme.com")
        self.assertEqual(guesses[1], "john.smith@acme.com")
        self.assertEqual(len(guesses), len(set(guesses)))


class WaterfallTests(unittest.TestCase):
    def test_resolve_email_stops_at_first_confident_provider(self):
        hunter = FakeProvider("hunter", finder={"email": "Jane.Doe@acme.com", "confidence": 93, "source": "hunter email finder"})
        tomba = FakeProvider("tomba", finder={"email": "jane@acme.com", "confidence": 90, "source": "tomba email finder"})
        result = resolve_email({"name": "Jane Doe"}, "acme.com", "Acme", [], None, None,
                               {"hunter": hunter, "tomba": tomba}, min_conf=70)
        self.assertEqual(result["email"], "jane.doe@acme.com")
        self.assertEqual(result["email_confidence"], 93)
        self.assertEqual(tomba.finder_calls, 0)

    def test_resolve_email_falls_back_to_pattern_guess(self):
        hunter = FakeProvider("hunter", finder=None)
        result = resolve_email({"name": "Jane Doe"}, "acme.com", "Acme", [], "first.last", "confirmed",
                               {"hunter": hunter}, min_conf=70)
        self.assertEqual(result["email"], "jane.doe@acme.com")
        self.assertEqual(result["email_confidence"], CONFIDENCE["guess_confirmed"])
        self.assertTrue(result["email_status"].startswith("guessed"))

    def test_verify_email_moves_to_next_guess_on_bounce(self):
        verifier = FakeProvider("zerobounce", verdicts=["invalid", "valid"])
        guessed = {"email": "jane.doe@acme.com", "email_confidence": 25, "email_status": "guessed (default first.last)",
                   "alternates": ["jdoe@acme.com", "jane@acme.com"]}
        verified = verify_email(guessed, {"zerobounce": verifier})
        self.assertEqual(verified["email"], "jdoe@acme.com")
        self.assertEqual(verified["email_confidence"], CONFIDENCE["verified"])
        self.assertEqual(verifier.verify_calls, ["jane.doe@acme.com", "jdoe@acme.com"])

    def test_verify_email_skips_already_verified(self):
        verifier = FakeProvider("zerobounce", verdicts=["invalid"])
        result = {"email": "jane@acme.com", "email_confidence": 95, "email_status": "verified"}
        self.assertEqual(verify_email(result, {"zerobounce": verifier}), result)
        self.assertEqual(verifier.verify_calls, [])


class QuotaTrackerTests(unittest.TestCase):
    def test_spend_and_remaining_within_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            quota = QuotaTracker(os.path.join(tmp, "quota.json"), caps={"demo": (2, "month")})
            self.assertTrue(quota.allow("demo"))
            quota.spend("demo")
            quota.spend("demo", 0.5)
            self.assertEqual(quota.remaining("demo"), 0.5)
            self.assertFalse(quota.allow("demo", 1))
            self.assertTrue(quota.allow("demo", 0.5))
            reloaded = QuotaTracker(os.path.join(tmp, "quota.json"), caps={"demo": (2, "month")})
            self.assertEqual(reloaded.used("demo"), 1.5)


if __name__ == "__main__":
    unittest.main()
