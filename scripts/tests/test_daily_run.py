import os
import sys
import tempfile
import threading
import unittest

import daily_run
from daily_run import (
    DailyConfig,
    build_commands,
    build_email,
    build_subject,
    build_summary,
    compare_with_previous,
    cycle_due,
    email_tier,
    enrich_args,
    last_successful_scrape,
    load_config,
    parse_enrich_log,
    previous_records,
    read_json,
    render_text,
    run_step,
    send_email,
    summary_blocks,
    write_run_record,
)

JOBS = [
    {"company": "acme", "ats": "Lever", "title": "Platform Engineer", "lead_intent_score": 9, "url": "u1"},
    {"company": "acme", "ats": "Lever", "title": "SRE", "lead_intent_score": 4, "url": "u2"},
    {"company": "globex", "ats": "Greenhouse", "title": "Data Engineer", "lead_intent_score": 6, "url": "u3"},
    {"company": "globex", "ats": "Greenhouse", "title": "Backend Engineer", "lead_intent_score": 2, "url": "u4"},
    {"company": "initech", "ats": "Ashby", "title": "Frontend Engineer", "lead_intent_score": 2, "url": "u5"},
    {"company": "initech", "ats": "Ashby", "title": "QA Engineer", "lead_intent_score": 0, "url": "u6"},
]
LEADS = [
    {"company_key": "acme", "company": "Acme", "max_score": 9, "domain": "acme.com", "email_pattern": "first.last",
     "company_phones": ["+1 555 0100"], "people": [
         {"name": "Jane Doe", "title": "CTO", "email": "jane.doe@acme.com", "email_confidence": 95,
          "phone": "+1 555 0101", "phone_type": "direct"},
         {"name": "John Roe", "title": "VP Engineering", "email": "john@gmail.com", "email_confidence": 90,
          "personal_emails": ["john@gmail.com"], "phone": "+1 555 0100", "phone_type": "company line"},
     ]},
    {"company_key": "globex", "company": "Globex", "max_score": 6, "domain": "globex.com", "people": [
        {"name": "Ann Lee", "title": "Founder", "email": "ann@globex.com", "email_confidence": 80},
        {"name": "Bo Chan", "title": "Head of Engineering", "email": "bo.chan@globex.com", "email_confidence": 25},
        {"name": "Cy Diaz", "title": "CTO", "email": None, "email_confidence": 0},
    ]},
    {"company_key": "initech", "company": "Initech", "max_score": 2, "domain": None, "people": [], "error": "timeout"},
]
METADATA = {"last_updated": "2026-10-29T13:00:00Z", "collected_jobs": 10, "removed_jobs": 4, "recruiter_jobs": 1}
QUOTA = [
    {"bucket": "hunter", "used": 50, "cap": 50, "period": "month", "configured": True},
    {"bucket": "apollo", "used": 3, "cap": 75, "period": "month", "configured": True},
    {"bucket": "tomba", "used": 25, "cap": 25, "period": "month", "configured": False},
]
RECORD = {"run_date": "2026-10-29", "host": "jba-1", "duration_s": 3725.0,
          "steps": [{"name": "scrape", "status": "ok", "reason": "", "duration_s": 3600.0}]}


def summary(**overrides):
    kwargs = {"min_score": 4, "ready_threshold": 90}
    kwargs.update(overrides)
    return build_summary(JOBS, LEADS, METADATA, QUOTA, **kwargs)


def config(**overrides):
    values = {"run_date": "2026-10-29", "smtp_host": "smtp.zoho.com", "smtp_port": 465,
              "smtp_user": "ali@example.com", "smtp_pass": "secret", "smtp_from": "Ali <ali@example.com>",
              "summary_to": ["ali@example.com"]}
    values.update(overrides)
    return DailyConfig(**values)


class CycleTests(unittest.TestCase):
    def test_cycle_rules(self):
        self.assertTrue(cycle_due(None, "2026-10-29")[0])
        self.assertFalse(cycle_due("2026-10-09", "2026-10-29")[0])  # day 20
        self.assertTrue(cycle_due("2026-10-08", "2026-10-29")[0])  # day 21
        due, why = cycle_due("2026-10-24", "2026-10-29", pool_exhausted=True)  # day 5, under the floor
        self.assertFalse(due)
        self.assertIn("2026-10-31", why)
        self.assertTrue(cycle_due("2026-10-21", "2026-10-29", pool_exhausted=True)[0])  # day 8
        self.assertTrue(cycle_due("2026-10-28", "2026-10-29", force=True)[0])

    def test_last_successful_scrape_ignores_skips_and_timeouts(self):
        records = [
            {"run_date": "2026-10-28", "steps": [{"name": "scrape", "status": "skipped"}]},
            {"run_date": "2026-10-20", "steps": [{"name": "scrape", "status": "ok"}]},
            {"run_date": "2026-10-19", "steps": [{"name": "scrape", "status": "timeout"}]},
        ]
        self.assertEqual(last_successful_scrape(records), "2026-10-20")
        self.assertIsNone(last_successful_scrape([]))


class SummaryTests(unittest.TestCase):
    def test_job_counts(self):
        jobs = summary()["jobs"]
        self.assertEqual((jobs["total"], jobs["companies"]), (6, 3))
        self.assertEqual(jobs["by_ats"], {"Ashby": 2, "Greenhouse": 2, "Lever": 2})
        self.assertEqual((jobs["collected"], jobs["removed"]), (10, 4))

    def test_shortlist_follows_rank_order(self):
        shortlist = summary()["shortlist"]
        self.assertEqual(shortlist["count"], 2)
        self.assertEqual([c["company"] for c in shortlist["companies"]], ["Acme", "Globex"])
        self.assertEqual(shortlist["companies"][0]["top_title"], "Platform Engineer")

    def test_enrichment_tiers_and_counts(self):
        en = summary()["enrichment"]
        self.assertEqual(en["emails"], {"verified": 1, "high": 2, "guessed": 1, "none": 1})
        self.assertEqual((en["companies"], en["with_domain"], en["errors"], en["people"]), (3, 2, 1, 5))
        self.assertEqual((en["direct_phones"], en["personal_emails"]), (1, 1))

    def test_ready_from_leads_honours_threshold_and_kind(self):
        ready = summary()["ready_to_email"]
        self.assertEqual(ready["count"], 2)
        self.assertEqual([(p["name"], p["email_kind"]) for p in ready["people"]],
                         [("Jane Doe", "work"), ("John Roe", "personal")])

    def test_ready_comes_from_the_queue_once_ingest_runs(self):
        ingest = {"sendable": 7, "next_up": [{"name": "X"}], "queue": 40, "queue_companies": 12, "unresolved": 20}
        ready = summary(ingest=ingest)["ready_to_email"]
        self.assertEqual((ready["count"], ready["source"]), (7, "outreach queue"))

    def test_empty_inputs_do_not_raise(self):
        result = build_summary([], [], {}, [], min_score=4, ready_threshold=90)
        self.assertEqual(result["shortlist"]["count"], 0)
        self.assertIsNone(result["jobs"]["collected"])
        self.assertIn("scripts/output/all_jobs.json is missing or empty", result["warnings"])

    def test_warnings_cover_steps_and_used_up_quota(self):
        steps = [{"name": "scrape", "status": "timeout", "reason": "exceeded 300 min", "duration_s": 18000.0}]
        warnings = summary(steps=steps)["warnings"]
        self.assertIn("scrape: timeout (exceeded 300 min)", warnings)
        self.assertIn("hunter: free monthly allowance used up (50/50)", warnings)
        self.assertFalse(any(w.startswith("tomba") for w in warnings))  # no key set

    def test_compare_with_previous_flags_large_drops(self):
        warnings = compare_with_previous({"Lever": 0, "Ashby": 90}, {"Lever": 1500, "Ashby": 100, "Tiny": 5},
                                         "2026-10-08")
        self.assertEqual(warnings, ["Lever: 0 jobs, down 100% from 1,500 on 2026-10-08"])

    def test_parse_enrich_log(self):
        text = ("  hunter: stopped early (quota used up)\n  failed: boom\n  failed: bang\n"
                "    duckduckgo DDGSException; waiting 30s\n    duckduckgo DDGSException; waiting 60s\n"
                "Time budget of 110 minutes reached; rerun resumes from enrich_cache.json\n")
        parsed = parse_enrich_log(text)
        self.assertEqual(parsed["warnings"], [
            "hunter: stopped early (quota used up)",
            "duckduckgo: 2 failed searches (rate limited or blocked); a BRAVE_API_KEY gives discovery a fallback",
            "enrich: 2 companies failed (see the enrich log)",
        ])
        self.assertTrue(parsed["out_of_time"])

    def test_email_tier(self):
        self.assertEqual([email_tier(c) for c in (None, 0, 25, 80, 94, 95)],
                         ["none", "none", "guessed", "high", "high", "verified"])


class RecordTests(unittest.TestCase):
    def test_round_trip_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "2026-10-29.json")
            write_run_record(path, {"run_date": "2026-10-29", "n": 1})
            write_run_record(path, {"run_date": "2026-10-29", "n": 2})
            self.assertEqual(read_json(path)["n"], 2)
            self.assertEqual(os.listdir(tmp), ["2026-10-29.json"])

    def test_previous_records_are_earlier_and_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            for day in ("2026-10-27", "2026-10-28", "2026-10-29"):
                write_run_record(os.path.join(tmp, day + ".json"), {"run_date": day})
            write_run_record(os.path.join(tmp, "2026-10-28.leads.json"), [])
            dates = [r["run_date"] for r in previous_records("2026-10-29", runs_dir=tmp)]
            self.assertEqual(dates, ["2026-10-28", "2026-10-27"])


class EmailTests(unittest.TestCase):
    def test_email_has_text_html_and_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "2026-10-29.leads.csv")
            with open(path, "w", encoding="utf-8") as f:
                f.write("company,name\nAcme,Jane Doe\n")
            msg = build_email(summary(), RECORD, config(), path)
        self.assertIn("Acme", msg.get_body(("plain",)).get_content())
        self.assertIn("<table", msg.get_body(("html",)).get_content())
        self.assertEqual([a.get_filename() for a in msg.iter_attachments()], ["leads-2026-10-29.csv"])

    def test_header_only_csv_is_not_attached(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "2026-10-29.leads.csv")
            with open(path, "w", encoding="utf-8") as f:
                f.write("company,name\n")
            msg = build_email(summary(), RECORD, config(), path)
        self.assertEqual(list(msg.iter_attachments()), [])

    def test_subject_counts(self):
        self.assertEqual(build_subject(summary(), RECORD), "[JBA] 2026-10-29: 2 ready, 2 shortlisted, 1 warning")

    def test_paused_inbox_and_replies_are_loud(self):
        outreach = {"mode": "send", "sent_first": 3, "sent_followup": 2,
                    "replies": [{"name": "Jane Doe", "company": "Acme", "snippet": "Yes, send the profiles"}],
                    "paused_inboxes": [{"address": "ali@example.com", "reason": "bounce_rate 8.6% (3/35)"}],
                    "periods": {"today": {"first_sent": 3, "reply_rate": 0.333}, "ytd": {"first_sent": 120}}}
        result = summary(outreach=outreach)
        self.assertTrue(build_subject(result, RECORD).startswith("PAUSED [JBA] 2026-10-29: 5 sent, 1 reply"))
        text = render_text(summary_blocks(result, RECORD))
        self.assertIn("PAUSED: ali@example.com", text)
        self.assertIn("Yes, send the profiles", text)
        self.assertIn("33.3%", text)

    def test_send_email_never_raises(self):
        calls = []

        class FakeSMTP:
            def login(self, user, password):
                calls.append(("login", user))

            def send_message(self, msg):
                calls.append(("send", msg["To"]))

            def quit(self):
                pass

        msg = build_email(summary(), RECORD, config())
        self.assertEqual(send_email(msg, config(), connect=lambda c: FakeSMTP())["status"], "sent")
        self.assertEqual(calls, [("login", "ali@example.com"), ("send", "ali@example.com")])

        def refused(c):
            raise OSError("connection refused")

        failed = send_email(msg, config(), connect=refused)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("connection refused", failed["error"])
        self.assertEqual(send_email(msg, config(summary_to=[]))["status"], "skipped")


class ConfigTests(unittest.TestCase):
    def test_defaults_with_an_empty_env(self):
        cfg = load_config([], env={})
        self.assertEqual(cfg.tz, "Asia/Karachi")
        self.assertFalse(cfg.email_configured)
        self.assertEqual((cfg.cycle_days, cfg.cycle_min_days, cfg.max_jobs_per_provider), (21, 7, 1500))
        self.assertIsNone(cfg.enrich_top)
        self.assertEqual(len(cfg.run_date), 10)

    def test_env_and_cli_overrides(self):
        env = {"SUMMARY_TO": "a@x.com, b@x.com", "SMTP_HOST": "smtp.zoho.com", "SMTP_PORT": "587",
               "DAILY_SKIP_STEPS": "scrape", "DAILY_ENRICH_TOP": "0", "DAILY_SCRAPE_TIMEOUT_MIN": "0.5"}
        cfg = load_config(["--skip", "merge", "--date", "2026-10-29", "--no-email"], env=env)
        self.assertEqual(cfg.summary_to, ["a@x.com", "b@x.com"])
        self.assertEqual((cfg.smtp_port, cfg.skip, cfg.enrich_top), (587, ["merge", "scrape"], 0))
        self.assertEqual(cfg.timeouts_min["scrape"], 0.5)
        self.assertTrue(cfg.email_configured)
        self.assertFalse(cfg.send_email)
        self.assertNotIn("smtp_pass", cfg.public())

    def test_unknown_step_and_bad_date_are_rejected(self):
        with self.assertRaises(SystemExit):
            load_config(["--skip", "scrap"], env={})
        with self.assertRaises(SystemExit):
            load_config(["--date", "29-10-2026"], env={})


class CommandTests(unittest.TestCase):
    def test_enrichment_switches_to_discovery_once_outreach_exists(self):
        self.assertEqual(enrich_args(config(), False)[:2], ["--top", "40"])
        args = enrich_args(config(), True)
        self.assertEqual(args[:2], ["--top", "0"])
        self.assertIn("--discover-only", args)
        self.assertEqual(args[args.index("--cache-max-age-days") + 1], "21")

    def test_scrape_cap_of_zero_drops_the_flag(self):
        paths = daily_run.run_paths("2026-10-29")
        cmd, _ = build_commands(config(max_jobs_per_provider=0), paths, False)["scrape"]
        self.assertNotIn("--max-jobs-per-provider", cmd)
        cmd, _ = build_commands(config(), paths, False)["scrape"]
        self.assertEqual(cmd[cmd.index("--max-jobs-per-provider") + 1], "1500")


class RunStepTests(unittest.TestCase):
    def run_python(self, code, timeout_s=30, stop=None):
        with tempfile.TemporaryDirectory() as tmp:
            return run_step("demo", [sys.executable, "-c", code], tmp, timeout_s, os.path.join(tmp, "step.log"),
                            stop, poll_s=0.1)

    def test_failure_keeps_the_log_tail(self):
        result = self.run_python("print('hello from the child'); raise SystemExit(3)")
        self.assertEqual((result.status, result.returncode), ("failed", 3))
        self.assertIn("hello from the child", result.tail)

    def test_success(self):
        self.assertEqual(self.run_python("pass").status, "ok")

    def test_timeout_stops_the_child(self):
        result = self.run_python("import time; time.sleep(30)", timeout_s=0.5)
        self.assertEqual(result.status, "timeout")
        self.assertLess(result.duration_s, 10)

    def test_stop_request_kills_the_child(self):
        stop = threading.Event()
        stop.set()
        self.assertEqual(self.run_python("import time; time.sleep(30)", stop=stop).status, "killed")


if __name__ == "__main__":
    unittest.main()
