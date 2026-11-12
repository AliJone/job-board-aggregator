import json
import os
import tempfile
import time
import unittest
from unittest import mock

from enrich_leads import (
    CONFIDENCE,
    DDG_THROTTLED,
    ddg_search,
    dedupe_people,
    detect_pattern,
    guess_candidates,
    humanize_company,
    load_cache,
    parse_linkedin_title,
    pick_company_domain,
    rank_companies,
    registered_domain,
    resolve_email,
    select_decision_makers,
    split_providers,
    verify_email,
)
from lead_providers import OpenRouterClient, Provider, QuotaTracker, Tomba, normalize_verdict, template_to_pattern


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

    def test_a_finder_score_alone_never_counts_as_verified(self):
        tomba = FakeProvider("tomba", finder={"email": "kim@acme.com", "confidence": 99, "source": "tomba email finder"})
        found = resolve_email({"name": "Kim Lee"}, "acme.com", "Acme", [], None, None, {"tomba": tomba}, min_conf=90)
        self.assertEqual(found["email_confidence"], CONFIDENCE["verified"] - 1)
        verifier = FakeProvider("zerobounce", verdicts=["valid"])
        verified = verify_email(found, {"zerobounce": verifier})
        self.assertEqual((verifier.verify_calls, verified["email_confidence"]), (["kim@acme.com"], CONFIDENCE["verified"]))

    def test_verify_email_skips_already_verified(self):
        verifier = FakeProvider("zerobounce", verdicts=["invalid"])
        result = {"email": "jane@acme.com", "email_confidence": 95, "email_status": "verified"}
        self.assertEqual(verify_email(result, {"zerobounce": verifier}), result)
        self.assertEqual(verifier.verify_calls, [])


class DailyModeTests(unittest.TestCase):
    def test_top_zero_returns_every_company(self):
        jobs = [{"company": f"c{i}", "lead_intent_score": 5, "ats": "Lever", "title": "Dev", "url": f"u{i}"}
                for i in range(50)]
        self.assertEqual(len(rank_companies(jobs, top=0, min_score=4)), 50)
        self.assertEqual(len(rank_companies(jobs, top=10, min_score=4)), 10)

    def test_discover_only_keeps_credits_for_the_top_up(self):
        providers = {k: FakeProvider(k) for k in ("hunter", "tomba", "apollo", "zerobounce", "brave", "github")}
        discover, resolve = split_providers(providers, True)
        self.assertEqual(sorted(discover), ["apollo", "brave", "github", "zerobounce"])
        self.assertEqual(list(resolve), ["github"])
        self.assertIs(split_providers(providers, False)[1], providers)

    def test_discovery_resolution_spends_no_credits(self):
        hunter = FakeProvider("hunter", finder={"email": "jane@acme.com", "confidence": 93, "source": "hunter"})
        _, resolve = split_providers({"hunter": hunter}, True)
        result = resolve_email({"name": "Jane Doe"}, "acme.com", "Acme", [], "first.last", "confirmed",
                               resolve, min_conf=70)
        self.assertEqual(result["email"], "jane.doe@acme.com")
        self.assertEqual(hunter.finder_calls, 0)

    def test_cache_entries_past_the_age_are_redone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cache.json")
            old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30 * 86400))
            new = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"old": {"enriched_at": old}, "new": {"enriched_at": new}}, f)
            self.assertEqual(sorted(load_cache(21, path=path)), ["new"])
            self.assertEqual(sorted(load_cache(None, path=path)), ["new", "old"])


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


class FakeClock:
    def __init__(self):
        self.now, self.slept = 1000.0, []

    def __getattr__(self, name):
        return getattr(time, name)  # strftime and the rest stay real; only the clock is faked

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class FakeResponse:
    def __init__(self, status=200, body=None, text="", headers=None):
        self.status_code, self.body, self.headers = status, body, headers or {}
        self.text = text or json.dumps(body or {})

    def json(self):
        if self.body is None:
            raise ValueError("no body")
        return self.body


def answer(content):
    return FakeResponse(200, {"choices": [{"message": {"content": content}}]})


BUSY = FakeResponse(429, text='{"error":{"message":"Provider returned error"}}')


class FakeSession:
    """Replays scripted responses, per model for OpenRouter, in order otherwise; the last one repeats."""

    def __init__(self, replies):
        self.replies, self.calls = replies, []

    @staticmethod
    def _next(script):
        return script.pop(0) if len(script) > 1 else script[0]

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(json["model"])
        return self._next(self.replies[json["model"]])

    def request(self, method, url, **kwargs):
        self.calls.append(url)
        return self._next(self.replies)


class FakeDDG:
    def __init__(self, outcomes):
        self.outcomes, self.calls = outcomes, 0

    def text(self, query, max_results=10):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ClockTestCase(unittest.TestCase):
    clock_target = "lead_providers.time"

    def setUp(self):
        self.clock = FakeClock()
        patcher = mock.patch(self.clock_target, self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)


class OpenRouterChainTests(ClockTestCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.quota = QuotaTracker(os.path.join(tmp.name, "quota.json"), caps={"openrouter": (50, "day")})

    def client(self, replies):
        session = FakeSession(replies)
        return OpenRouterClient("key", self.quota, ",".join(replies), session), session

    def test_env_list_keeps_order_without_a_catalog_call(self):
        with mock.patch("lead_providers.requests.get", side_effect=AssertionError("catalog fetched")):
            client = OpenRouterClient("key", self.quota, " best, mid ,worst ", FakeSession({}))
        self.assertEqual(client.models, ["best", "mid", "worst"])
        self.assertEqual(client.model, "best")

    def test_busy_model_falls_through_and_best_returns_after_its_rest(self):
        client, session = self.client({"best": [BUSY, answer("from best")], "mid": [answer("from mid")]})
        self.assertEqual(client.chat("s", "u"), "from mid")
        self.assertEqual(client.chat("s", "u"), "from mid")  # best still resting
        self.clock.now += 60
        self.assertEqual(client.chat("s", "u"), "from best")
        self.assertEqual(session.calls, ["best", "mid", "mid", "best"])
        self.assertEqual(self.quota.used("openrouter"), 3)

    def test_repeated_failures_double_the_rest(self):
        client, session = self.client({"best": [BUSY], "mid": [answer("mid")]})
        client.chat("s", "u")  # best rests about 30 s
        self.clock.now += 40
        client.chat("s", "u")  # fails again, rests about 60 s
        self.clock.now += 40
        client.chat("s", "u")
        self.assertEqual(session.calls, ["best", "mid", "best", "mid", "mid"])

    def test_retired_model_sits_out_the_run_and_empty_answers_rest(self):
        client, session = self.client({"best": [FakeResponse(404, text="no endpoints")], "mid": [answer("")],
                                       "worst": [answer("from worst")]})
        self.assertEqual(client.chat("s", "u"), "from worst")
        self.clock.now += 3600
        self.assertEqual(client.chat("s", "u"), "from worst")
        self.assertEqual(session.calls, ["best", "mid", "worst", "mid", "worst"])

    def test_daily_account_limit_stops_every_model(self):
        limit = FakeResponse(429, text='{"error":{"message":"Rate limit exceeded: free-models-per-day"}}')
        client, session = self.client({"best": [limit], "mid": [answer("mid")]})
        self.assertIsNone(client.chat("s", "u"))
        self.assertIsNone(client.chat("s", "u"))
        self.assertEqual(session.calls, ["best"])
        self.assertIn("daily", client.disabled_reason)

    def test_waits_for_the_first_model_back_when_all_are_resting(self):
        client, session = self.client({"best": [BUSY, answer("after the wait")]})
        self.assertEqual(client.chat("s", "u"), "after the wait")
        self.assertEqual(session.calls, ["best", "best"])
        self.assertTrue(24 <= sum(self.clock.slept) <= 36)


class TombaResponseTests(ClockTestCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.quota = QuotaTracker(os.path.join(tmp.name, "quota.json"))

    def test_finder_reads_the_flat_email_record(self):
        found = FakeSession([FakeResponse(200, {"data": {"email": "Jane@Acme.com", "score": 97, "position": "CTO",
                                                         "phone_number": False}})])
        hit = Tomba("k", "s", self.quota, found).email_finder("Jane", "Doe", "acme.com")
        self.assertEqual((hit["email"], hit["confidence"], hit["phone"]), ("jane@acme.com", 97, None))
        empty = FakeSession([FakeResponse(200, {"data": {"email": None}})])
        self.assertIsNone(Tomba("k", "s", self.quota, empty).email_finder("Jane", "Doe", "acme.com"))


class RateLimitBackoffTests(ClockTestCase):
    def test_429_backs_off_exponentially_then_succeeds(self):
        p = Provider(None, FakeSession([FakeResponse(429), FakeResponse(429), FakeResponse(200, {"ok": True})]))
        self.assertEqual(p._get("https://api.example.com"), {"ok": True})
        self.assertIsNone(p.disabled_reason)
        self.assertEqual(len(self.clock.slept), 2)
        self.assertTrue(1.6 <= self.clock.slept[0] <= 2.4 and 3.2 <= self.clock.slept[1] <= 4.8)

    def test_short_retry_after_is_honoured(self):
        p = Provider(None, FakeSession([FakeResponse(429, headers={"Retry-After": "42"}), FakeResponse(200, {"ok": True})]))
        self.assertEqual(p._get("https://api.example.com"), {"ok": True})
        self.assertEqual(self.clock.slept, [42.0])

    def test_daily_limit_disables_without_waiting(self):
        p = Provider(None, FakeSession([FakeResponse(429, headers={"Retry-After": "26981"})]))
        self.assertIsNone(p._get("https://api.example.com"))
        self.assertEqual(self.clock.slept, [])
        self.assertEqual(p.disabled_reason, "rate limited, resets in 7.5h")

    def test_persistent_429_disables_after_five_retries(self):
        session = FakeSession([FakeResponse(429)])
        p = Provider(None, session)
        self.assertIsNone(p._get("https://api.example.com"))
        self.assertEqual(len(session.calls), 6)
        self.assertEqual(p.disabled_reason, "rate limited")

    def test_github_hourly_limit_is_not_reported_as_a_bad_key(self):
        p = Provider(None, FakeSession([FakeResponse(403, headers={"X-RateLimit-Remaining": "0"})]))
        p._get("https://api.github.com/search/users")
        self.assertEqual(p.disabled_reason, "rate limited (hourly allowance used)")


class DuckDuckGoBackoffTests(ClockTestCase):
    clock_target = "enrich_leads.time"

    def test_empty_page_is_retried_once(self):
        ddg = FakeDDG([Exception("No results found."), Exception("No results found.")])
        self.assertEqual(ddg_search(ddg, "q"), [])
        self.assertEqual(ddg.calls, 2)
        self.assertEqual(len(self.clock.slept), 1)

    @unittest.skipUnless(DDG_THROTTLED, "ddgs not installed")
    def test_rate_limit_backs_off_exponentially(self):
        throttled = DDG_THROTTLED[0]("ratelimit")
        ddg = FakeDDG([throttled, throttled, [{"href": "https://acme.com"}]])
        self.assertEqual(ddg_search(ddg, "q"), [{"href": "https://acme.com"}])
        self.assertTrue(12 <= self.clock.slept[0] <= 18 and 24 <= self.clock.slept[1] <= 36)


if __name__ == "__main__":
    unittest.main()
