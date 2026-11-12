import csv
import os
import smtplib
import sqlite3
import tempfile
import unittest
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import outreach
import outreach_mail as mail
import outreach_match as match
import outreach_store as store
from outreach import Settings
from outreach_compose import (compose, display_company, full_text, join_words, lint_copy, lint_message, offer_line,
                              pain_bucket)

INBOX = "ali@drawnonward.org"
# made-up people; the real roster stays in the gitignored data/talent.json
ROSTER = [
    {"name": "Sam Go", "family": "engineering", "role": "full-stack engineer", "years": 7,
     "skills": ["Go", "React", "Node", "Vue"]},
    {"name": "Kim Net", "family": "engineering", "role": "full-stack engineer", "years": 8,
     "skills": ["Node", "React", ".NET"]},
    {"name": "Mo Bile", "family": "engineering", "role": "mobile and full-stack engineer", "years": 8,
     "skills": ["React Native", "Flutter", "Node", "React"]},
    {"name": "Jo New", "family": "engineering", "role": "full-stack engineer", "years": 4, "skills": ["React", "Node"]},
    {"name": "Ann Auto", "family": "qa", "role": "QA engineer", "years": 5,
     "skill_years": {"test automation": 5, "manual testing": 2}},
    {"name": "Max Hand", "family": "qa", "role": "QA engineer", "years": 5,
     "skill_years": {"manual testing": 5, "test automation": 3}},
    {"name": "Pat Ops", "family": "devops", "role": "DevOps engineer", "years": 9, "skills": []},
    {"name": "Dee Sign", "family": "design", "role": "UI/UX designer", "years": 5, "years_text": "5+ years"},
    {"name": "Pam Plan", "family": "pm", "role": "project manager", "years": 10},
]


def weekday_now(offset_days=0):
    """A weekday afternoon (New York) at least offset_days ahead, so weekend holds never interfere."""
    now = datetime.now(timezone.utc).replace(hour=15, minute=0, second=0, microsecond=0) + timedelta(days=offset_days)
    while now.astimezone(ZoneInfo("America/New_York")).weekday() >= 5:
        now += timedelta(days=1)
    return now


def saturday_now():
    now = datetime.now(timezone.utc).replace(hour=15, minute=0, second=0, microsecond=0)
    while now.astimezone(ZoneInfo("America/New_York")).weekday() != 5:
        now += timedelta(days=1)
    return now


def person(name, title="CTO", email=None, conf=95, linkedin="", personal=None):
    return {"name": name, "title": title, "email": email, "email_confidence": conf if email else 0,
            "email_status": "verified by zerobounce" if conf >= 95 else "guessed", "linkedin": linkedin,
            "personal_emails": personal or []}


def company(key, people, score=10, domain=None):
    return {"company_key": key, "company": key.title(), "max_score": score, "domain": domain or f"{key}.com",
            "people": people, "email_pattern": "first.last", "email_pattern_source": "provider"}


def job(key, score=10, title="Staff Engineer"):
    return {"company": key, "title": title, "url": f"https://jobs.example/{key}", "location": "Remote",
            "lead_intent_score": score, "lead_intent_matches": {"highScale": True},
            "description": "<p>Scale it with React and Node.</p>"}


class FakeSMTP:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send_message(self, msg):
        if self.fail:
            raise smtplib.SMTPServerDisconnected("connection closed")
        self.sent.append(msg)

    def quit(self):
        pass


class FakeProvider:
    """Finder and verifier with a credit budget; quota checks read the same budget."""

    def __init__(self, name, finds=None, verdicts=None, credits=10):
        self.name, self.verify_cost, self.verify_bucket = name, 1, None
        self.finds, self.verdicts, self.credits = finds or {}, verdicts or {}, credits
        self.disabled_reason, self.calls, self.quota = None, [], self

    def allow(self, bucket, cost=1):
        return self.credits >= cost

    def available(self, cost=1, bucket=None):
        if self.disabled_reason:
            return False
        if cost and self.credits < cost:
            self.disabled_reason = "free quota for this period is used up"
            return False
        return True

    def email_finder(self, first, last, domain):
        self.calls.append(("find", first, last))
        self.credits -= 1
        email = self.finds.get(f"{first} {last}")
        return {"email": email, "confidence": 70, "source": f"{self.name} finder"} if email else None

    def verify(self, email):
        self.calls.append(("verify", email))
        self.credits -= 1
        return {"status": self.verdicts.get(email, "invalid")}


class OutreachCase(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.tmp = stack.enter_context(tempfile.TemporaryDirectory())
        self.conn = stack.enter_context(store.connect(os.path.join(self.tmp, "outreach.db")))
        self.settings = Settings(mode="send", gap=(0, 0), sender_name="Ali Jone", topup_days=1,
                                 sender_address_line="Karachi, Pakistan", own_domains={"drawnonward.org"},
                                 roster=ROSTER)
        self.now = weekday_now()
        self.today = self.now.date().isoformat()
        self.sleeps = []
        self.smtp = FakeSMTP()

    def add_inbox(self, status="ready", address=INBOX):
        inbox_id = store.add_inbox(self.conn, address, "Ali Jone", "smtp.zoho.com", 465, address)
        if status != "pending":
            store.set_inbox_status(self.conn, inbox_id, "ready")
        if status == "paused":
            store.set_inbox_status(self.conn, inbox_id, "paused", "by hand")
        return inbox_id

    def ingest(self, companies, jobs=None, scrape_date=None):
        jobs = [job(c["company_key"]) for c in companies] if jobs is None else jobs
        meta = {"last_updated": f"{scrape_date or self.today}T00:00:00Z"}
        return outreach.ingest_command(self.conn, companies, jobs, meta, self.today, self.settings, now=self.now,
                                       backup_dir=None)

    def send(self, now=None, mode=None, smtp=None):
        now = now or self.now
        if mode:
            self.settings.mode = mode
        if smtp:
            self.smtp = smtp
        return outreach.run_command(self.conn, self.settings, now.date().isoformat(), {"SMTP_PASS": "x"}, now=now,
                                    sleep=self.sleeps.append, connect_smtp=lambda inbox, env: self.smtp,
                                    drafts_dir=os.path.join(self.tmp, "drafts"))

    def sent_to(self):
        return [str(m["To"]) for m in self.smtp.sent]


class KeyTests(unittest.TestCase):
    def test_email_key_merges_one_mailbox(self):
        self.assertEqual(store.email_key(" John.Doe+jobs@GoogleMail.com "), "johndoe@gmail.com")
        self.assertEqual(store.email_key("J.Doe+x@Acme.com"), "j.doe@acme.com")  # only Gmail ignores dots
        self.assertIsNone(store.email_key("not an email"))
        self.assertIsNone(store.email_key("@acme.com"))

    def test_linkedin_key(self):
        self.assertEqual(store.linkedin_key("https://www.linkedin.com/in/Jane-Doe/?trk=x"), "jane-doe")
        self.assertIsNone(store.linkedin_key("https://example.com/in/jane"))


class ReadyCommandTests(unittest.TestCase):
    def test_ready_starts_the_warmup(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "outreach.db")
            with store.connect(db) as conn:
                store.add_inbox(conn, INBOX, "Ali Jone", "smtp.zoho.com", 465, INBOX)
            self.assertEqual(outreach.main(["--db", db, "ready", INBOX, "--no-check"]), 0)
            with store.connect(db) as conn:
                inbox = store.get_inbox(conn, INBOX)
            self.assertEqual(inbox["status"], "ready")
            self.assertTrue(inbox["warmup_started_at"])
            self.assertEqual(outreach.main(["--db", db, "ready", "nobody@example.com", "--no-check"]), 1)


class WarmupTests(unittest.TestCase):
    def test_parse_warmup(self):
        self.assertEqual(outreach.parse_warmup("5:7,10:7,20"), [(5, 7), (10, 7), (20, None)])
        self.assertEqual(outreach.parse_warmup("5:7"), [(5, 7), (5, None)])

    def test_cap_by_day(self):
        steps = outreach.parse_warmup("5:7,10:7,20")
        inbox = {"warmup_started_at": "2026-11-01T10:00:00Z"}
        cases = {"2026-11-01": (5, 1), "2026-11-07": (5, 7), "2026-11-08": (10, 8), "2026-11-14": (10, 14),
                 "2026-11-15": (20, 15), "2027-01-01": (20, 62)}
        for day, expected in cases.items():
            self.assertEqual(outreach.warmup_cap(inbox, day, steps), expected, day)
        self.assertEqual(outreach.warmup_cap({"warmup_started_at": None}, "2026-11-20", steps), (5, 1))


class DeduplicationTests(OutreachCase):
    def test_nobody_gets_a_second_first_email(self):
        self.add_inbox()
        self.ingest([company("acme", [person("Jane Doe", email="jane@acme.com")]),
                     company("beta", [person("Bo Kim", email="bo@beta.com")])])
        self.assertEqual(self.send()["sent_first"], 2)
        later = self.send(now=weekday_now(2))
        self.assertEqual(later["sent_first"], 0)
        self.assertEqual(later["skipped_reasons"].get("already_contacted"), 2)

    def test_same_mailbox_under_another_company_is_blocked(self):
        self.add_inbox()
        self.ingest([company("acme", [person("Jane Doe", email="jane.doe@gmail.com")], score=20),
                     company("acmelabs", [person("J Doe", email="Jane.Doe+work@gmail.com")], score=10)])
        self.send()
        self.assertEqual(self.sent_to(), ['Jane Doe <jane.doe@gmail.com>'])

    def test_same_linkedin_profile_at_another_company_is_blocked(self):
        self.add_inbox()
        url = "https://www.linkedin.com/in/jane-doe"
        self.ingest([company("acme", [person("Jane Doe", email="jane@acme.com", linkedin=url)], score=20),
                     company("newco", [person("Jane Doe", email="jane@newco.io", linkedin=url + "/")], score=10)])
        stats = self.send()
        self.assertEqual(stats["sent_first"], 1)
        self.assertEqual(self.send(now=weekday_now(1))["skipped_reasons"].get("already_contacted"), 2)

    def test_same_name_at_the_same_domain_after_the_cooldown_is_blocked(self):
        self.add_inbox()
        self.ingest([company("coreweave", [person("Jane Doe", email="jane@coreweave.com")], domain="coreweave.com")])
        self.send()
        self.ingest([company("coreweaveu", [person("Jane Doe", email="jdoe@coreweave.com")], domain="coreweave.com")])
        later = weekday_now(100)  # past the 90-day company cooldown
        self.conn.execute("UPDATE leads SET job_last_seen = ?", (later.date().isoformat(),))
        stats = self.send(now=later)
        self.assertEqual(stats["sent_first"], 0)
        self.assertGreaterEqual(stats["skipped_reasons"].get("already_contacted", 0), 1)

    def test_engineering_buyer_first_at_equal_seniority(self):
        self.add_inbox()
        self.ingest([company("acme", [person("Al Brown", title="Chief Information Security Officer",
                                             email="al@acme.com"),
                                      person("Zed Young", title="Chief Technology Officer", email="zed@acme.com")])])
        self.send()
        self.assertEqual(self.sent_to(), ['Zed Young <zed@acme.com>'])

    def test_company_cooldown_holds_other_people_at_the_company(self):
        self.add_inbox()
        self.ingest([company("acme", [person("Jane Doe", email="jane@acme.com"),
                                      person("Bo Kim", title="VP Engineering", email="bo@acme.com")])])
        self.assertEqual(self.send()["sent_first"], 1)
        self.assertEqual(self.sent_to(), ['Jane Doe <jane@acme.com>'])  # the CTO, most senior
        later = self.send(now=weekday_now(3))
        self.assertEqual(later["sent_first"], 0)
        self.assertEqual(later["skipped_reasons"].get("company_cooldown"), 1)

    def test_database_refuses_duplicates(self):
        inbox_id = self.add_inbox()
        self.ingest([company("acme", [person("Jane Doe", email="jane@acme.com")])])
        lead = store.all_leads(self.conn)[0]
        base = dict(lead_id=lead["id"], inbox_id=inbox_id, kind="first", to_email="jane@acme.com",
                    email_key="jane@acme.com", person_key=lead["person_key"], company_key="acme", subject="s",
                    body_text="b", status="sent", day=self.today)
        first = store.insert_message(self.conn, message_id="<1@x>", **base)
        with self.assertRaises(sqlite3.IntegrityError):
            store.insert_message(self.conn, message_id="<2@x>", **base)
        with self.assertRaises(sqlite3.IntegrityError):
            store.insert_message(self.conn, message_id="<3@x>", **{**base, "email_key": "other@acme.com"})
        follow = {**base, "kind": "followup", "parent_id": first}
        store.insert_message(self.conn, message_id="<4@x>", **follow)
        with self.assertRaises(sqlite3.IntegrityError):
            store.insert_message(self.conn, message_id="<5@x>", **follow)
        store.insert_message(self.conn, message_id="<6@x>", **{**base, "status": "draft"})  # drafts never block

    def test_review_drafts_do_not_count_as_contacted(self):
        self.add_inbox()
        self.ingest([company("acme", [person("Jane Doe", email="jane@acme.com")])])
        self.assertEqual(self.send(mode="review")["drafts_written"], 1)
        self.assertEqual(self.send(now=weekday_now(1), mode="send")["sent_first"], 1)

    def test_role_and_own_addresses_are_never_emailed(self):
        self.add_inbox()
        self.ingest([company("acme", [person("Info Desk", email="info@acme.com")]),
                     company("drawnonward", [person("Ali Jone", email="ali@drawnonward.org")],
                             domain="drawnonward.org")])
        stats = self.send()
        self.assertEqual(stats["sent_first"], 0)
        self.assertEqual(stats["skipped_reasons"].get("role_address"), 1)
        self.assertEqual(stats["skipped_reasons"].get("own_domain"), 1)


class FollowupTests(OutreachCase):
    def test_one_followup_after_seven_days_to_the_same_address(self):
        self.add_inbox()
        self.ingest([company("acme", [person("Jane Doe", email="jane@acme.com")])])
        self.send()
        first = self.smtp.sent[0]
        self.assertEqual(self.send(now=weekday_now(3))["sent_followup"], 0)  # not due yet
        # re-discovery finds a different verified address; the thread keeps the original one
        self.ingest([company("acme", [person("Jane Doe", email="jane.doe@acme.com", conf=96)])])
        stats = self.send(now=weekday_now(8))
        self.assertEqual(stats["sent_followup"], 1)
        follow = self.smtp.sent[-1]
        self.assertEqual(str(follow["To"]), 'Jane Doe <jane@acme.com>')
        self.assertEqual(follow["In-Reply-To"], first["Message-ID"])
        self.assertEqual(follow["Subject"], f"Re: {first['Subject']}")
        self.assertEqual(self.send(now=weekday_now(10))["sent_followup"], 0)

    def test_followups_go_first_and_use_the_cap(self):
        self.settings.warmup = [(1, None)]
        self.add_inbox()
        self.ingest([company("acme", [person("Jane Doe", email="jane@acme.com")], score=20),
                     company("beta", [person("Bo Kim", email="bo@beta.com")], score=10)])
        self.send()
        stats = self.send(now=weekday_now(8))
        self.assertEqual((stats["sent_followup"], stats["sent_first"]), (1, 0))

    def test_summary_lists_the_next_runs_followups(self):
        self.add_inbox()
        self.ingest([company("acme", [person("Jane Doe", email="jane@acme.com")])])
        self.send()
        stats = self.send(now=self.now + timedelta(days=6))
        self.assertEqual([f["email"] for f in stats["followups_next"]], ["jane@acme.com"])

    def test_weekend_holds_every_send(self):
        self.add_inbox()
        self.ingest([company("acme", [person("Jane Doe", email="jane@acme.com")])])
        saturday = saturday_now()
        self.conn.execute("UPDATE leads SET job_last_seen = ?", (saturday.date().isoformat(),))
        stats = self.send(now=saturday)
        self.assertEqual(stats["sent_first"], 0)
        self.assertTrue(stats["weekend"])


class StopTests(OutreachCase):
    def setUp(self):
        super().setUp()
        self.add_inbox()
        self.ingest([company("acme", [person("Jane Doe", email="jane@acme.com", personal=["jane.d@gmail.com"])],
                             score=20),
                     company("beta", [person("Bo Kim", email="bo@beta.com")], score=10)])

    def test_stop_cancels_the_followup_and_is_listed(self):
        self.send()
        lines = outreach.stop_command(self.conn, ["Jane@Acme.com"], "replied", None, self.today)
        self.assertIn("follow-up due", lines[0])
        stats = self.send(now=weekday_now(8))
        self.assertEqual(stats["sent_followup"], 1)  # only Bo's
        self.assertEqual([s["email"] for s in stats["stopped_today"]], ["jane@acme.com"])
        self.assertEqual(len(stats["replies"]), 1)

    def test_stop_before_any_email_blocks_the_person_and_their_other_addresses(self):
        outreach.stop_command(self.conn, ["jane@acme.com"], "unsubscribed", None, self.today)
        self.assertIn("janed@gmail.com", store.suppressed_values(self.conn))  # Gmail ignores dots
        self.assertEqual(store.period_stats(self.conn, self.today)["today"]["unsubscribes"], 0)  # never emailed
        stats = self.send()
        self.assertEqual(self.sent_to(), ['Bo Kim <bo@beta.com>'])
        self.assertEqual(stats["skipped_reasons"].get("suppressed"), 1)

    def test_stop_from_a_file_with_domains_and_comments(self):
        path = os.path.join(self.tmp, "stop.txt")
        with open(path, "w") as f:
            f.write("# replied this week\nbo@beta.com\n\n@acme.com\nnot-an-email\n")
        lines = outreach.stop_command(self.conn, outreach.read_values([], path), "replied", None, self.today)
        self.assertEqual(len(lines), 3)
        self.assertIn("skipped not-an-email", lines[2])
        self.assertEqual(self.send()["sent_first"], 0)

    def test_stop_during_a_run_wins(self):
        self.send()
        parent = store.followups_due(self.conn, 1, weekday_now(8).date().isoformat())[0]
        outreach.stop_command(self.conn, [parent["to_email"]], "replied", None, self.today)
        stats = {"skipped_reasons": Counter(), "errors": [], "_sent_any": False}
        delivery = outreach.Delivery(self.conn, store.list_inboxes(self.conn)[0], self.settings, self.today,
                                     {"SMTP_PASS": "x"}, lambda i, e: self.smtp, self.tmp, self.sleeps.append, stats)
        self.assertFalse(delivery.followup(parent))
        self.assertEqual(stats["skipped_reasons"]["stopped"], 1)

    def test_unstop(self):
        outreach.stop_command(self.conn, ["bo@beta.com"], "other", None, self.today)
        lines = outreach.unstop_command(self.conn, ["bo@beta.com"], self.today)
        self.assertIn("may be emailed again", lines[0])
        self.assertEqual(self.send()["sent_first"], 2)

    def test_reported_replies_and_bounces_feed_the_totals(self):
        self.send()
        outreach.stop_command(self.conn, ["jane@acme.com"], "replied", None, self.today)
        outreach.stop_command(self.conn, ["bo@beta.com"], "bounced", None, self.today)
        today = store.period_stats(self.conn, self.today)["today"]
        self.assertEqual((today["first_sent"], today["replies"], today["bounces"]), (2, 1, 1))
        self.assertEqual(today["reply_rate"], 0.5)

    def test_three_reported_bounces_pause_the_inbox(self):
        self.ingest([company("gamma", [person("Cy Lu", email="cy@gamma.com")], score=5)])
        self.send()
        for address in ("jane@acme.com", "bo@beta.com", "cy@gamma.com"):
            outreach.stop_command(self.conn, [address], "bounced", None, self.today)
        self.ingest([company("delta", [person("Di Wu", email="di@delta.com")], score=5)])
        stats = self.send(now=weekday_now(1))
        self.assertEqual(stats["sent_first"], 0)
        self.assertEqual(stats["paused_inboxes"][0]["address"], INBOX)


class AddressLineTests(OutreachCase):
    def test_no_real_send_without_a_postal_address(self):
        self.add_inbox()
        self.ingest([company("acme", [person("Jane Doe", email="jane@acme.com")])])
        self.settings.sender_address_line = ""
        stats = self.send()
        self.assertEqual((stats["sent_first"], self.smtp.sent), (0, []))
        self.assertIn("postal address", " ".join(stats["errors"]))
        self.assertEqual(self.send(mode="review")["drafts_written"], 1)


class RunModeTests(OutreachCase):
    def setUp(self):
        super().setUp()
        self.ingest([company(k, [person(f"{k.title()} Lee", email=f"lee@{k}.com")], score=s)
                     for k, s in (("acme", 30), ("beta", 20), ("gamma", 10))])

    def test_review_writes_valid_drafts_once_a_day(self):
        self.add_inbox(status="pending")
        stats = self.send(mode="review")
        self.assertEqual(stats["drafts_written"], 3)
        with open(stats["drafts"][0], "rb") as f:
            msg = message_from_bytes(f.read(), policy=policy.default)
        self.assertEqual(msg["From"], f"Ali Jone <{INBOX}>")
        self.assertIn("mailto:", msg["List-Unsubscribe"])
        self.assertEqual(lint_message(msg, "first"), [])
        self.assertEqual(self.send(mode="review")["drafts_written"], 0)

    def test_off_mode_sends_nothing_but_still_reports(self):
        self.add_inbox()
        stats = self.send(mode="off")
        self.assertEqual((stats["sent_first"], stats["drafts_written"]), (0, 0))
        self.assertIn("periods", stats)

    def test_pending_inbox_cannot_send(self):
        self.add_inbox(status="pending")
        stats = self.send()
        self.assertEqual(stats["sent_first"], 0)
        self.assertEqual(stats["skipped_reasons"].get("inbox_not_ready"), 1)

    def test_warmup_cap_and_gap_between_sends(self):
        self.settings.warmup = [(2, None)]
        self.add_inbox()
        stats = self.send()
        self.assertEqual(stats["sent_first"], 2)
        self.assertEqual(len(self.sleeps), 1)
        self.assertEqual(self.send()["sent_first"], 0)  # same day: cap already used

    def test_smtp_failure_stops_the_inbox_and_allows_a_retry(self):
        self.add_inbox()
        stats = self.send(smtp=FakeSMTP(fail=True))
        self.assertTrue(stats["send_failed"])
        self.assertEqual(stats["sent_first"], 0)
        self.assertEqual(self.conn.execute("SELECT status FROM messages").fetchall()[0][0], "failed")
        self.assertEqual(self.send(now=weekday_now(1), smtp=FakeSMTP())["sent_first"], 3)


class MatchTests(unittest.TestCase):
    def fit(self, title, text="", location="Remote"):
        return match.job_fit({"title": title, "excerpt": text, "location": location}, ROSTER)

    def test_role_family_from_the_title(self):
        cases = {"Product Manager": None, "Senior Project Manager": "pm", "Engineering Manager": None,
                 "Graphic Designer": None, "Designer II - Interiors": None, "Aeronautical Test Engineer": None,
                 "QA Automation Senior (contractor)": "qa", "Senior Product Designer": "design",
                 "UX Engineer": "engineering", "Site Reliability Engineer": "devops", "Senior iOS Engineer": "mobile",
                 "Member of Technical Staff": "engineering", "Data Engineer": None, "Solutions Engineer": None}
        for title, family in cases.items():
            self.assertEqual(match.role_family(title), family, title)

    def test_skills_in_posting_order_without_lookalikes(self):
        text = "Our stack: Go, React Native and React. Node.js services, some ASP.NET."
        self.assertEqual(match.skills_in(text, ["React", ".NET", "Node", "React Native", "Go"]),
                         ["Go", "React Native", "React", "Node", ".NET"])
        noise = "Go to market with partners at example.net. React quickly; each node in the graph is reactive."
        self.assertEqual(match.skills_in(noise, ["React", ".NET", "Node", "Go"]), [])

    def test_most_matched_skills_then_years(self):
        fit, _ = self.fit("Member of Technical Staff", "React, TypeScript, gRPC, FastAPI and Go")
        self.assertEqual((fit["name"], fit["skills"], fit["line"]), ("Sam Go", ["React", "Go"], "skills"))
        self.assertEqual(self.fit("Frontend Engineer", "We use React.")[0]["name"], "Kim Net")
        self.assertEqual(self.fit("Senior Mobile Engineer", "Flutter and Node")[0]["name"], "Mo Bile")
        self.assertEqual(self.fit("Senior iOS Engineer", "Swift and React")[1], "no_roster_match")
        self.assertEqual(self.fit("Backend Engineer", "Python and Rust")[1], "no_roster_match")
        self.assertEqual(self.fit("Product Manager", "React")[1], "no_roster_match")
        self.assertEqual(self.fit("Senior Project Manager", "Run sprints with our developers")[0]["name"], "Pam Plan")
        self.assertEqual(self.fit("Project Manager", "Site schedules and subcontractors")[1], "no_roster_match")
        self.assertEqual(self.fit("Software Engineer, Backend", "Our React app talks to Node services")[0]["skills"],
                         ["Node"])
        self.assertEqual(self.fit("Developer Go / Integraciones API", "Senior")[0]["skills"], ["Go"])

    def test_qa_names_one_skill_with_its_own_years(self):
        fit, _ = self.fit("QA Engineer", "Cypress and Playwright suites, plus manual testing")
        self.assertEqual((fit["name"], fit["years_text"], fit["skills"], fit["line"]),
                         ("Ann Auto", "5 years", ["test automation"], "qa"))
        self.assertEqual(self.fit("QA Engineer", "Manual testing of releases")[0]["name"], "Max Hand")
        self.assertEqual(self.fit("QA Engineer", "Help our developers ship")[0]["line"], "plain")
        self.assertEqual(self.fit("Test Engineer", "Thermal chambers and fixtures")[1], "no_roster_match")
        self.assertEqual(self.fit("Automation Engineer", "PLC and SCADA")[1], "no_roster_match")

    def test_years_and_location_gates(self):
        self.assertEqual(match.required_years("Requirements: 2-5 years of experience"), 2)
        self.assertEqual(match.required_years("Founded 12 years ago. 6+ years building APIs"), 6)
        self.assertEqual(match.required_years("Somos una empresa con más de 14 años de experiencia. "
                                              "Esperamos +5 años de experiencia en AWS"), 5)
        self.assertEqual(self.fit("Senior Product Designer", "10+ years of product design")[1], "years_gap")
        self.assertTrue(self.fit("Staff Engineer", "8+ years with React")[0])
        self.assertEqual(self.fit("QA Engineer", "Selenium. Must reside in Chile")[1], "location_blocker")
        self.assertEqual(self.fit("Staff Engineer", "React, hybrid in our office", "New York")[1], "location_blocker")
        self.assertTrue(self.fit("Staff Engineer", "React, hybrid or remote", "Remote - US")[0])
        self.assertEqual(self.fit("Staff Engineer", "React; TS/SCI clearance required")[1], "location_blocker")

    def test_best_fit_takes_a_fitting_posting_and_names_the_blocker(self):
        jobs = [{"title": "Data Scientist", "excerpt": "Python"}, {"title": "Full Stack Engineer", "excerpt": "React"}]
        self.assertEqual(match.best_fit(jobs, ROSTER)[0]["job"]["title"], "Full Stack Engineer")
        blocked = [{"title": "Data Scientist", "excerpt": ""}, {"title": "Staff Engineer", "excerpt": "React. 15+ years"}]
        self.assertEqual(match.best_fit(blocked, ROSTER), (None, "years_gap"))
        self.assertEqual(match.best_fit([], ROSTER), (None, "no_roster_match"))
        self.assertEqual(match.best_fit(jobs, []), (None, "no_roster_match"))


class ComposeTests(unittest.TestCase):
    settings = Settings(sender_name="Ali Jone", sender_address_line="Karachi, Pakistan")
    inbox = {"address": INBOX, "display_name": "Ali Jone"}
    lead = {"name": "Jane Doe", "company": "Acme"}
    job = {"title": "Staff Software Engineer, Platform (Remote)", "excerpt": "React and Go", "location": "Remote",
           "terms": {"technicalDebt": ["zero to one"], "dataIntegrity": ["schema migrations"]}}

    def test_copy_passes_every_check(self):
        fit = match.best_fit([self.job], ROSTER)[0]
        first = compose(self.lead, fit["job"], self.inbox, "first", self.settings, fit=fit)
        self.assertEqual(first["subject"], "Your Staff Software Engineer role")
        self.assertIn("We have a senior full-stack engineer with 7 years of experience, including React and Go from "
                      "your posting. They can join your team through us.", first["body"])
        follow = compose(self.lead, fit["job"], self.inbox, "followup", self.settings,
                         parent_subject=first["subject"], fit=fit, parent_body=first["body"])
        self.assertEqual(follow["subject"], "Re: Your Staff Software Engineer role")
        self.assertIn("role. From the posting, the work involves data that has to stay correct,", follow["body"])
        self.assertIn("The full-stack engineer I mentioned", follow["body"])
        self.assertEqual(lint_copy(first["subject"], first["body"], "first"), [])
        self.assertEqual(lint_copy(follow["subject"], follow["body"], "followup"), [])

    def test_offer_lines_by_role(self):
        def line(**fit):
            return offer_line({"skills": [], "line": "plain", "years_text": f"{fit['years']} years", **fit})
        self.assertEqual(line(role="full-stack engineer", years=4, skills=["React"], line="skills"),
                         "We have a full-stack engineer with 4 years of experience, including React from your posting.")
        self.assertEqual(line(role="QA engineer", years=5, skills=["test automation"], line="qa"),
                         "We have a senior QA engineer with 5 years of test automation, which your posting asks for.")
        self.assertEqual(line(role="UI/UX designer", years=5, years_text="5+ years"),
                         "We have a senior UI/UX designer with 5+ years of experience.")
        self.assertEqual(line(role="project manager", years=10, line="pm"),
                         "We have a senior project manager with 10 years of experience and multiple certifications.")
        self.assertEqual(join_words(["Go", "React", "Node"]), "Go, React and Node")

    def test_follow_up_line_uses_strong_phrases_only(self):
        weak = {**self.job, "excerpt": "React and Go. Nucleic acid assays.",
                "terms": {"technicalDebt": ["zero to one", "production-ready"], "manualArchitecture": ["acid"]}}
        self.assertIsNone(pain_bucket(weak, "engineering"))
        debt = {**weak, "terms": {"technicalDebt": ["technical debt"]}}
        self.assertEqual(pain_bucket(debt, "engineering"), "technicalDebt")
        self.assertIsNone(pain_bucket(debt, "qa"))
        self.assertEqual(pain_bucket({**weak, "excerpt": "ACID transactions"}, "engineering"), "manualArchitecture")
        follow = compose(self.lead, weak, self.inbox, "followup", self.settings,
                         parent_subject="Your Staff Software Engineer role", fit=match.best_fit([weak], ROSTER)[0],
                         parent_body="We have a senior full-stack engineer with 7 years")
        self.assertIn("One more thought on the Staff Software Engineer role.\n\n", follow["body"])

    def test_company_names_read_like_a_person_wrote_them(self):
        cases = {"OPENLANE": "Openlane", "Reflectionai": "Reflection AI", "redbee": "redbee", "Bonsai": "Bonsai",
                 "Shanghai": "Shanghai", "Acme Labs": "Acme Labs", "Futurelabs": "Future Labs"}
        for raw, shown in cases.items():
            self.assertEqual(display_company(raw), shown)
        posting = "About Zuma Zuma is pioneering agentic AI. Zuma is scaling rapidly."
        self.assertEqual(display_company("Getzuma", posting), "Zuma")
        self.assertEqual(display_company("Reflectionai", "At Reflection AI, we build frontier models."), "Reflection AI")
        self.assertEqual(display_company("Gettyimages", "Getty Images is hiring. Ty works here."), "Getty Images")
        self.assertEqual(display_company("Getzuma", "Zuma once."), "Getzuma")

    def test_full_text_adds_signature_and_opt_out(self):
        text = full_text("Hi Jane,\n\nShort note.", self.settings, self.inbox)
        self.assertIn("\n-- \nAli Jone\ndrawnonward\nKarachi, Pakistan\n", text)
        self.assertIn(f"write to {INBOX}", text)

    def test_lint_rules(self):
        body = " ".join(["word"] * 60)
        cases = {
            ("Hello {first}", body): "placeholder",
            ("Short subject", "too short"): "words",
            ("one two three four five six seven", body): "longer than",
            ("Big news!", body): "exclamation",
            ("URGENT hire", body): "all-caps",
            ("Re: staff engineer", body): "Re:",
            ("Staff engineer", body + " https://a.com https://b.com"): "link",
            ("Staff engineer", body + " this is risk-free"): "spam trigger",
            ("Staff engineer", body + " let us delve in"): "banned word",
            ("Staff engineer", body + " — really"): "dash",
        }
        for (subject, text), fragment in cases.items():
            errors = " | ".join(t for level, t in lint_copy(subject, text, "first") if level == "error")
            self.assertIn(fragment, errors, subject)
        self.assertIn("must start with", " ".join(t for _, t in lint_copy("Staff engineer", body, "followup")))

    def test_lint_message(self):
        text = full_text(" ".join(["word"] * 60), self.settings, self.inbox)
        msg = mail.build_message(self.inbox, "Jane Doe", "jane@acme.com", "Staff engineer", text, "<1@x>", INBOX)
        self.assertEqual(lint_message(msg, "first"), [])
        self.assertIn("follow-up is missing In-Reply-To/References", lint_message(msg, "followup"))
        bad = EmailMessage()
        bad["From"] = INBOX
        bad.set_content(text)
        bad.add_alternative('<p>hi</p><img src="https://t.example/p.gif">', subtype="html")
        errors = lint_message(bad, "first")
        self.assertTrue(any("display name" in e for e in errors))
        self.assertTrue(any("img" in e for e in errors))
        self.assertTrue(any("List-Unsubscribe" in e for e in errors))


class IngestTests(OutreachCase):
    def test_upsert_keeps_first_seen_and_a_verified_address(self):
        self.ingest([company("acme", [person("Jane Doe", email="jane@acme.com", conf=95)])])
        before = store.all_leads(self.conn)[0]
        stats = self.ingest([company("acme", [person("Jane Doe", email="j.doe@acme.com", conf=40)])])
        after = store.all_leads(self.conn)[0]
        self.assertEqual((stats["new"], stats["updated"]), (0, 1))
        self.assertEqual(after["email"], "jane@acme.com")
        self.assertEqual(after["first_seen"], before["first_seen"])
        self.assertEqual({c["email"] for c in after["candidate_emails"]}, {"jane@acme.com", "j.doe@acme.com"})

    def test_job_snapshot_and_closed_jobs(self):
        self.add_inbox()
        self.ingest([company("acme", [person("Jane Doe", email="jane@acme.com")]),
                     company("beta", [person("Bo Kim", email="bo@beta.com")])])
        lead = store.all_leads(self.conn)[0]
        self.assertEqual(lead["jobs_snapshot"][0]["buckets"], ["highScale"])
        self.assertEqual(lead["jobs_snapshot"][0]["excerpt"], "Scale it with React and Node.")
        # the next scrape no longer lists beta's job
        newer = (self.now + timedelta(days=21)).date().isoformat()
        self.ingest([], jobs=[job("acme")], scrape_date=newer)
        stats = self.send(now=weekday_now(21))
        self.assertEqual(self.sent_to(), ['Jane Doe <jane@acme.com>'])
        self.assertEqual(stats["skipped_reasons"].get("job_closed"), 1)
        self.assertEqual(store.all_leads(self.conn)[1]["jobs_snapshot"][0]["title"], "Staff Engineer")

    def test_a_fitting_posting_comes_first_and_unfit_companies_are_skipped(self):
        self.add_inbox()
        science = {**job("acme", score=30, title="Data Scientist"), "description": "<p>Python</p>"}
        self.ingest([company("acme", [person("Jane Doe", email="jane@acme.com")]),
                     company("beta", [person("Bo Kim", email="bo@beta.com")])],
                    jobs=[science, job("acme", score=4, title="Full Stack Engineer"),
                          {**job("beta", title="Data Scientist"), "description": "<p>Python</p>"}])
        acme = [l for l in store.all_leads(self.conn) if l["company_key"] == "acme"][0]
        self.assertEqual(acme["jobs_snapshot"][0]["title"], "Full Stack Engineer")
        stats = self.send()
        self.assertEqual(self.sent_to(), ["Jane Doe <jane@acme.com>"])
        self.assertEqual(stats["skipped_reasons"].get("no_roster_match"), 1)
        self.assertEqual(stats["sent"][0]["offered"], "Kim Net")

    def test_queue_csv_and_backups(self):
        self.ingest([company("acme", [person("Jane Doe", email="jane@acme.com"),
                                      person("Bo Kim", title="Director of Engineering", email="b@acme.com", conf=40)])])
        path = os.path.join(self.tmp, "queue.csv")
        backups = os.path.join(self.tmp, "backups")
        os.makedirs(backups)
        for day in range(1, 17):
            open(os.path.join(backups, f"outreach-2020-01-{day:02d}.db"), "w").close()
        outreach.ingest_command(self.conn, [], [job("acme")], {"last_updated": self.today}, self.today, self.settings,
                                now=self.now, queue_csv=path, backup_dir=backups)
        with open(path, newline="") as f:
            rows = {r["name"]: r["status"] for r in csv.DictReader(f)}
        self.assertEqual(rows, {"Jane Doe": "ready", "Bo Kim": "unresolved"})
        files = sorted(os.listdir(backups))
        self.assertEqual(len(files), 14)
        self.assertEqual(files[-1], f"outreach-{self.today}.db")


class TopupTests(OutreachCase):
    def topup(self, providers):
        return outreach.topup_command(self.conn, self.settings, self.today, providers, now=self.now)

    def test_spends_nothing_on_a_company_nobody_fits(self):
        self.add_inbox()
        self.ingest([company("beta", [person("Bo Kim", email="bo.kim@beta.com", conf=40)])],
                    jobs=[{**job("beta", title="Data Scientist"), "description": "<p>Python</p>"}])
        zerobounce = FakeProvider("zerobounce", verdicts={"bo.kim@beta.com": "valid"})
        stats = self.topup({"zerobounce": zerobounce})
        self.assertEqual((stats["attempted"], zerobounce.calls), (0, []))

    def test_verifies_the_most_senior_person_per_company_until_the_target(self):
        self.settings.warmup = [(2, None)]
        self.add_inbox()
        self.ingest([company(k, [person("Ann Lee", email=f"ann.lee@{k}.com", conf=40),
                                 person("Bob Ray", title="Director of Engineering", email=f"bob.ray@{k}.com", conf=40)],
                             score=s) for k, s in (("acme", 30), ("beta", 20), ("gamma", 10))])
        zerobounce = FakeProvider("zerobounce", verdicts={"ann.lee@acme.com": "valid", "ann.lee@beta.com": "valid"})
        stats = self.topup({"zerobounce": zerobounce})
        self.assertEqual((stats["target"], stats["attempted"], stats["verified_work"]), (2, 2, 2))
        emailable = {l["email"] for l in store.all_leads(self.conn) if l["email_confidence"] >= 90}
        self.assertEqual(emailable, {"ann.lee@acme.com", "ann.lee@beta.com"})
        self.assertFalse(stats["pool_exhausted"])

    def test_next_person_is_tried_after_a_dead_end(self):
        self.add_inbox()
        self.ingest([company("acme", [person("Ann Lee", conf=0),
                                      person("Bob Ray", title="Director of Engineering", conf=0)])])
        zerobounce = FakeProvider("zerobounce", verdicts={"bob.ray@acme.com": "valid"})
        first = self.topup({"zerobounce": zerobounce})
        self.assertEqual((first["attempted"], first["no_address"]), (1, 1))
        second = self.topup({"zerobounce": zerobounce})
        self.assertEqual(second["verified_work"], 1)
        self.assertEqual(self.send()["sent_first"], 1)

    def test_personal_address_when_work_guesses_bounce(self):
        self.add_inbox()
        self.ingest([company("acme", [person("Ann Lee", email="ann.lee@acme.com", conf=40,
                                             personal=["ann.lee.dev@gmail.com"])])])
        zerobounce = FakeProvider("zerobounce", verdicts={"ann.lee.dev@gmail.com": "valid"})
        stats = self.topup({"zerobounce": zerobounce})
        lead = store.all_leads(self.conn)[0]
        self.assertEqual((stats["verified_personal"], lead["email"], lead["email_kind"]),
                         (1, "ann.lee.dev@gmail.com", "personal"))

    def test_closed_jobs_and_contacted_companies_are_not_worth_credits(self):
        self.add_inbox()
        self.ingest([company("acme", [person("Ann Lee", email="ann@acme.com"),
                                      person("Bob Ray", title="VP Engineering", conf=0)]),
                     company("beta", [person("Cy Lu", conf=0)])], jobs=[job("acme")])
        self.conn.execute("UPDATE leads SET job_last_seen = '2020-01-01' WHERE company_key = 'beta'")
        self.send()
        zerobounce = FakeProvider("zerobounce")
        stats = self.topup({"zerobounce": zerobounce})
        self.assertEqual(stats["attempted"], 0)
        self.assertEqual(zerobounce.calls, [])
        self.assertTrue(stats["pool_exhausted"])

    def test_stops_when_every_provider_is_out_of_credits(self):
        self.add_inbox()
        self.ingest([company("acme", [person("Ann Lee", conf=0)])])
        stats = self.topup({"zerobounce": FakeProvider("zerobounce", credits=0)})
        self.assertEqual(stats["attempted"], 0)
        self.assertEqual(stats["quota_exhausted"], ["zerobounce (free quota used up)"])
        self.assertFalse(stats["pool_exhausted"])

    def test_off_mode_spends_nothing(self):
        self.settings.mode = "off"
        self.ingest([company("acme", [person("Ann Lee", conf=0)])])
        zerobounce = FakeProvider("zerobounce")
        self.assertIn("skipped", self.topup({"zerobounce": zerobounce}))
        self.assertEqual(zerobounce.calls, [])


if __name__ == "__main__":
    unittest.main()
