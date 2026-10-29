#!/usr/bin/env python3
"""
Daily server run: outreach, credit top-up, scrape (only when the job cycle is
due), merge, enrich, ingest, then a summary email to the owner.

deploy/jba-daily.timer starts it at 18:00 Asia/Karachi. Every step is a child
process with its own log and timeout; a failed step never stops the summary.
The outreach, top-up and ingest steps are skipped until scripts/outreach.py
exists.

Usage:
    python scripts/daily_run.py                                   # the full run
    python scripts/daily_run.py --skip scrape,merge --no-email --print
    python scripts/daily_run.py --force-scrape                    # ignore the job cycle once
    python scripts/daily_run.py --test-email                      # check the SMTP settings
    python scripts/daily_run.py --resend --date 2026-10-28        # mail a stored summary again

Writes scripts/output/runs/YYYY-MM-DD.json (run record), .summary.txt and the
day's leads archive; logs go to scripts/output/logs/.
"""

import argparse
import fcntl
import html
import json
import logging
import os
import re
import shutil
import signal
import smtplib
import socket
import ssl
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from zoneinfo import ZoneInfo

from enrich_leads import ENV_FILE, JOBS_FILE, LEADS_CSV, LEADS_JSON, OUTPUT_DIR, ROOT_DIR, SCRIPT_DIR
from enrich_leads import load_dotenv, rank_companies
from lead_providers import QuotaTracker

RUNS_DIR = os.path.join(OUTPUT_DIR, "runs")
LOGS_DIR = os.path.join(OUTPUT_DIR, "logs")
LOCK_FILE = os.path.join(OUTPUT_DIR, "daily_run.lock")
METADATA_FILE = os.path.join(OUTPUT_DIR, "metadata.json")
OUTREACH_SCRIPT = os.path.join(SCRIPT_DIR, "outreach.py")

STEPS = ["outreach", "topup", "scrape", "merge", "enrich", "ingest"]
DEFAULT_TIMEOUT_MIN = {"outreach": 120, "topup": 30, "scrape": 300, "merge": 30, "enrich": 130, "ingest": 5}
# quota bucket -> the .env key that switches the provider on
QUOTA_KEYS = {"hunter": "HUNTER_API_KEY", "tomba": "TOMBA_API_KEY", "tomba_verify": "TOMBA_API_KEY",
              "apollo": "APOLLO_API_KEY", "zerobounce": "ZEROBOUNCE_API_KEY", "brave": "BRAVE_API_KEY",
              "openrouter": "OPENROUTER_API_KEY"}
PERIOD_WORD = {"month": "monthly", "day": "daily"}
BAD_STATUSES = ("failed", "timeout", "killed")
SHORTLIST_ROWS = 25
READY_ROWS = 25
TAIL_LINES = 40
KEEP_DAYS = 90
EXIT_LOCKED = 75
STOPPED_EARLY_RE = re.compile(r"^\s+(\w+): stopped early \((.+)\)\s*$")
DDG_RETRY_RE = re.compile(r"^\s+duckduckgo \w+; waiting \d+s")

STOP = threading.Event()
log = logging.getLogger("daily_run")


# ============================================================
# CONFIG
# ============================================================

@dataclass
class DailyConfig:
    run_date: str
    tz: str = "Asia/Karachi"
    skip: list = field(default_factory=list)
    force_scrape: bool = False
    send_email: bool = True
    print_summary: bool = False
    resend: bool = False
    test_email: bool = False
    max_jobs_per_provider: int = 1500
    cycle_days: int = 21
    cycle_min_days: int = 7
    timeouts_min: dict = field(default_factory=lambda: dict(DEFAULT_TIMEOUT_MIN))
    enrich_top: int | None = None  # None: 40 until outreach.py exists, then 0 (every company)
    enrich_min_score: int = 4
    enrich_people: int = 3
    enrich_min_confidence: int = 70
    enrich_cache_days: int = 21
    enrich_max_minutes: float = 110
    enrich_no_llm: bool = False
    ready_threshold: int = 90
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_pass: str = field(default="", repr=False)
    smtp_from: str = ""
    smtp_auth: str = "login"
    summary_to: list = field(default_factory=list)

    @property
    def email_configured(self):
        return bool(self.smtp_host and self.summary_to)

    def public(self):
        data = asdict(self)
        data.pop("smtp_pass", None)
        return data


def _split(raw):
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _env_number(env, key, default, kind=int):
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return kind(raw)
    except ValueError:
        raise SystemExit(f"{key} must be a number, got {raw!r}")


def _env_flag(env, key):
    return (env.get(key) or "").strip().lower() in ("1", "true", "yes", "on")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="run date YYYY-MM-DD in DAILY_TZ (default: today)")
    parser.add_argument("--skip", default="", help="comma list of steps to skip: " + ",".join(STEPS))
    parser.add_argument("--force-scrape", action="store_true", help="scrape today even if the job cycle is not due")
    parser.add_argument("--no-email", action="store_true", help="do not send the summary email")
    parser.add_argument("--print", dest="print_summary", action="store_true", help="print the text summary")
    parser.add_argument("--resend", action="store_true", help="send the stored summary for --date again; runs no steps")
    parser.add_argument("--test-email", action="store_true", help="send a one-line test message and exit")
    return parser.parse_args(argv)


def load_config(argv=None, env=None):
    """CLI over env over defaults. env=None reads the repo .env into os.environ first."""
    args = parse_args(argv)
    if env is None:
        load_dotenv(ENV_FILE)
        env = os.environ
    tz = (env.get("DAILY_TZ") or "Asia/Karachi").strip()
    if args.date:
        try:
            date.fromisoformat(args.date)
        except ValueError:
            raise SystemExit(f"--date must be YYYY-MM-DD, got {args.date!r}")
    skip = sorted(set(_split(env.get("DAILY_SKIP_STEPS")) + _split(args.skip)))
    unknown = [s for s in skip if s not in STEPS]
    if unknown:
        raise SystemExit(f"unknown step(s) to skip: {', '.join(unknown)} (steps: {', '.join(STEPS)})")
    return DailyConfig(
        run_date=args.date or datetime.now(ZoneInfo(tz)).date().isoformat(),
        tz=tz,
        skip=skip,
        force_scrape=args.force_scrape,
        send_email=not args.no_email,
        print_summary=args.print_summary,
        resend=args.resend,
        test_email=args.test_email,
        max_jobs_per_provider=_env_number(env, "DAILY_MAX_JOBS_PER_PROVIDER", 1500),
        cycle_days=_env_number(env, "CYCLE_DAYS", 21),
        cycle_min_days=_env_number(env, "CYCLE_MIN_DAYS", 7),
        timeouts_min={s: _env_number(env, f"DAILY_{s.upper()}_TIMEOUT_MIN", m, float)
                      for s, m in DEFAULT_TIMEOUT_MIN.items()},
        enrich_top=_env_number(env, "DAILY_ENRICH_TOP", None),
        enrich_min_score=_env_number(env, "DAILY_ENRICH_MIN_SCORE", 4),
        enrich_people=_env_number(env, "DAILY_ENRICH_PEOPLE", 3),
        enrich_min_confidence=_env_number(env, "DAILY_ENRICH_MIN_CONFIDENCE", 70),
        enrich_cache_days=_env_number(env, "DAILY_ENRICH_CACHE_DAYS", 21),
        enrich_max_minutes=_env_number(env, "DAILY_ENRICH_MAX_MINUTES", 110, float),
        enrich_no_llm=_env_flag(env, "DAILY_ENRICH_NO_LLM"),
        ready_threshold=_env_number(env, "READY_EMAIL_CONFIDENCE", 90),
        smtp_host=(env.get("SMTP_HOST") or "").strip(),
        smtp_port=_env_number(env, "SMTP_PORT", 465),
        smtp_user=(env.get("SMTP_USER") or "").strip(),
        smtp_pass=env.get("SMTP_PASS") or "",
        smtp_from=(env.get("SMTP_FROM") or "").strip(),
        smtp_auth=(env.get("SMTP_AUTH") or "login").strip().lower(),
        summary_to=_split(env.get("SUMMARY_TO")),
    )


# ============================================================
# STEPS
# ============================================================

@dataclass
class StepResult:
    name: str
    status: str = ""  # ok | failed | timeout | killed | skipped
    reason: str = ""
    returncode: int | None = None
    started_at: str = ""
    duration_s: float = 0.0
    cmd: list = field(default_factory=list)
    log: str = ""
    tail: str = ""


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tail_file(path, lines=TAIL_LINES):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(f.tell() - 65536, 0))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _terminate(proc, grace_s=30):
    proc.terminate()
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_step(name, cmd, cwd, timeout_s, log_path, stop_event=None, poll_s=1.0):
    """Run one step as a child process. Its output goes to its own log so the journal stays small."""
    result = StepResult(name, cmd=[str(c) for c in cmd], log=log_path, started_at=utc_now())
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    start = time.monotonic()
    with open(log_path, "ab") as out:
        out.write(f"\n=== {result.started_at} {' '.join(result.cmd)}\n".encode())
        out.flush()
        try:
            proc = subprocess.Popen(result.cmd, cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL)
        except OSError as exc:
            result.status, result.reason = "failed", f"could not start: {exc}"
            return result
        while True:
            try:
                proc.wait(timeout=poll_s)
                break
            except subprocess.TimeoutExpired:
                if stop_event is not None and stop_event.is_set():
                    _terminate(proc)
                    result.status, result.reason = "killed", "stop requested (SIGTERM)"
                    break
                if time.monotonic() - start > timeout_s:
                    _terminate(proc)
                    result.status, result.reason = "timeout", f"exceeded {timeout_s / 60:g} min"
                    break
    result.returncode = proc.returncode
    if not result.status:
        result.status = "ok" if proc.returncode == 0 else "failed"
        if result.status == "failed":
            result.reason = f"exit code {proc.returncode}"
    result.duration_s = round(time.monotonic() - start, 1)
    result.tail = tail_file(log_path)
    return result


def step_log(run_date, name):
    return os.path.join(LOGS_DIR, f"daily-{run_date}.{name}.log")


def enrich_args(cfg, discover):
    """Before the outreach module exists enrichment spends credits itself (top 40); after, it only discovers."""
    top = cfg.enrich_top if cfg.enrich_top is not None else (0 if discover else 40)
    args = ["--top", str(top), "--min-score", str(cfg.enrich_min_score), "--people", str(cfg.enrich_people),
            "--min-confidence", str(cfg.enrich_min_confidence),
            "--cache-max-age-days", str(cfg.enrich_cache_days), "--max-minutes", f"{cfg.enrich_max_minutes:g}"]
    if discover:
        args.append("--discover-only")
    if cfg.enrich_no_llm:
        args.append("--no-llm")
    return args


def build_commands(cfg, paths, has_outreach):
    """step -> (argv, cwd). merge_data.py uses relative paths, so it runs from the repo root."""
    py = [sys.executable, "-u"]
    day = cfg.run_date
    scrape = py + [os.path.join(SCRIPT_DIR, "scraper.py"), "--source", "automated"]
    if cfg.max_jobs_per_provider > 0:
        scrape += ["--max-jobs-per-provider", str(cfg.max_jobs_per_provider)]
    outreach = py + [OUTREACH_SCRIPT]
    return {
        "outreach": (outreach + ["run", "--today", day, "--stats-out", paths["outreach"]], ROOT_DIR),
        "topup": (outreach + ["topup", "--today", day, "--stats-out", paths["topup"]], ROOT_DIR),
        "scrape": (scrape, SCRIPT_DIR),
        "merge": (py + [os.path.join(SCRIPT_DIR, "merge_data.py")], ROOT_DIR),
        "enrich": (py + [os.path.join(SCRIPT_DIR, "enrich_leads.py")] + enrich_args(cfg, has_outreach), ROOT_DIR),
        "ingest": (outreach + ["ingest", "--leads", paths["leads_json"], "--jobs", JOBS_FILE, "--today", day,
                               "--stats-out", paths["ingest"], "--queue-csv", paths["queue_csv"]], ROOT_DIR),
    }


# ============================================================
# JOB CYCLE
# ============================================================

def last_successful_scrape(records):
    """Newest run date whose scrape step finished ok."""
    dates = [r.get("run_date") for r in records
             if any(s.get("name") == "scrape" and s.get("status") == "ok" for s in r.get("steps") or [])]
    return max((d for d in dates if d), default=None)


def cycle_due(last_scrape, today, pool_exhausted=False, cycle_days=21, min_days=7, force=False):
    """Scrape every cycle_days, sooner once the email queue is empty, but never within min_days."""
    if force:
        return True, "forced with --force-scrape"
    if not last_scrape:
        return True, "no earlier scrape on record"
    last = date.fromisoformat(last_scrape)
    age = (date.fromisoformat(today) - last).days
    if age >= cycle_days:
        return True, f"{age} days since the last scrape on {last_scrape}"
    if pool_exhausted:
        if age >= min_days:
            return True, f"the email queue is empty and the last scrape was {age} days ago"
        return False, f"the email queue is empty; next scrape allowed {last + timedelta(days=min_days)}"
    return False, f"cycle day {age} of {cycle_days}, next scrape {last + timedelta(days=cycle_days)}"


# ============================================================
# RUN RECORDS
# ============================================================

def run_paths(run_date, runs_dir=RUNS_DIR):
    base = os.path.join(runs_dir, run_date)
    return {"record": base + ".json", "summary": base + ".summary.txt",
            "leads_json": base + ".leads.json", "leads_csv": base + ".leads.csv", "queue_csv": base + ".queue.csv",
            "outreach": base + ".outreach.json", "topup": base + ".topup.json", "ingest": base + ".ingest.json"}


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def write_run_record(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def previous_records(run_date, runs_dir=RUNS_DIR, limit=60):
    """Run records dated before run_date, newest first."""
    try:
        names = os.listdir(runs_dir)
    except FileNotFoundError:
        return []
    dates = sorted((n[:-5] for n in names if len(n) == 15 and n.endswith(".json") and n[:-5] < run_date),
                   reverse=True)
    return [r for r in (read_json(os.path.join(runs_dir, d + ".json")) for d in dates[:limit]) if r]


def archive_leads(paths):
    """Copy today's leads next to the run record; ingest and resend read the copies."""
    files = {}
    for src, key in ((LEADS_JSON, "leads_json"), (LEADS_CSV, "leads_csv")):
        if os.path.exists(src):
            shutil.copy2(src, paths[key])
            files[key] = paths[key]
            files[key + "_updated_at"] = datetime.fromtimestamp(os.path.getmtime(src), timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
    return files


def prune_old(directories, days=KEEP_DAYS, now=None):
    cutoff = (now or time.time()) - days * 86400
    removed = 0
    for directory in directories:
        if not os.path.isdir(directory):
            continue
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    os.remove(entry.path)
                    removed += 1
    return removed


def git_rev():
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT_DIR, capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def last_section(path):
    """The part of a step log written by the most recent run (logs append per day)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    idx = text.rfind("\n=== ")
    return text[idx:] if idx >= 0 else text


# ============================================================
# SUMMARY (pure)
# ============================================================

def email_tier(confidence):
    if not confidence:
        return "none"
    if confidence >= 95:
        return "verified"
    return "high" if confidence >= 80 else "guessed"


def email_kind(email, domain):
    host = (email or "").rsplit("@", 1)[-1].lower()
    domain = (domain or "").lower()
    return "work" if domain and (host == domain or host.endswith("." + domain)) else "personal"


def jobs_section(jobs, metadata):
    companies = {j.get("company") or j.get("company_slug") for j in jobs} - {None, ""}
    by_ats = Counter(j.get("ats") or "unknown" for j in jobs)
    return {"total": len(jobs), "collected": metadata.get("collected_jobs"), "removed": metadata.get("removed_jobs"),
            "by_ats": dict(sorted(by_ats.items(), key=lambda kv: (-kv[1], kv[0]))),
            "recruiter_jobs": metadata.get("recruiter_jobs"), "companies": len(companies),
            "last_updated": metadata.get("last_updated")}


def shortlist_section(jobs, min_score, rows=SHORTLIST_ROWS):
    ranked = rank_companies(jobs, top=0, min_score=min_score)
    return {"min_score": min_score, "count": len(ranked),
            "companies": [{"company": c["company"], "ats": c["ats"], "max_score": c["max_score"],
                           "job_count": c["job_count"], "top_title": next((t for t in c["top_titles"] if t), "")}
                          for c in ranked[:rows]]}


def enrichment_section(leads):
    people = [p for c in leads for p in c.get("people") or []]
    tiers = Counter(email_tier(p.get("email_confidence") if p.get("email") else 0) for p in people)
    return {"companies": len(leads), "with_domain": sum(1 for c in leads if c.get("domain")),
            "errors": sum(1 for c in leads if c.get("error")), "people": len(people),
            "emails": {t: tiers.get(t, 0) for t in ("verified", "high", "guessed", "none")},
            "direct_phones": sum(1 for p in people if (p.get("phone_type") or "").startswith("direct")),
            "company_phones": sum(1 for c in leads if c.get("company_phones")),
            "personal_emails": sum(1 for p in people if p.get("personal_emails")),
            "pattern_detected": sum(1 for c in leads if c.get("email_pattern"))}


def ready_from_leads(leads, threshold, rows=READY_ROWS):
    """Before the outreach queue exists: confident addresses straight from leads.json."""
    ready = []
    for c in sorted(leads, key=lambda c: -(c.get("max_score") or 0)):
        for p in c.get("people") or []:
            if p.get("email") and (p.get("email_confidence") or 0) >= threshold:
                ready.append({"company": c.get("company"), "name": p.get("name"), "title": p.get("title"),
                              "email": p["email"], "email_kind": email_kind(p["email"], c.get("domain")),
                              "email_confidence": p.get("email_confidence")})
    return {"threshold": threshold, "count": len(ready), "people": ready[:rows], "source": "leads.json"}


def queue_section(outreach, topup, ingest):
    if not (topup or ingest):
        return None
    outreach, topup, ingest = outreach or {}, topup or {}, ingest or {}
    cap = sum(i.get("cap_today") or 0 for i in outreach.get("inboxes") or [])
    sendable = ingest.get("sendable", topup.get("sendable_after"))
    return {"people": ingest.get("queue"), "companies": ingest.get("queue_companies"), "new_today": ingest.get("new"),
            "sendable": sendable, "unresolved": ingest.get("unresolved", topup.get("unresolved_after")),
            "days_of_sends": round(sendable / cap, 1) if cap and sendable is not None else None,
            "pool_exhausted": bool(topup.get("pool_exhausted")),
            "topup": {k: topup.get(k) for k in ("attempted", "verified_work", "verified_personal", "no_address",
                                                 "quota_exhausted", "target")} if topup else None}


def parse_enrich_log(text):
    warnings, failed, search_errors, out_of_time = [], 0, 0, False
    for line in (text or "").splitlines():
        match = STOPPED_EARLY_RE.match(line)
        if match:
            warnings.append(f"{match.group(1)}: stopped early ({match.group(2)})")
        elif DDG_RETRY_RE.match(line):
            search_errors += 1
        elif line.strip().startswith("failed: "):
            failed += 1
        elif line.startswith("Time budget of"):
            out_of_time = True
    if search_errors:
        # cloud IPs often get throttled; each failure also costs a long back-off
        warnings.append(f"duckduckgo: {_plural(search_errors, 'failed search', 'failed searches')} "
                        "(rate limited or blocked); a BRAVE_API_KEY gives discovery a fallback")
    if failed:
        warnings.append(f"enrich: {failed} compan{'y' if failed == 1 else 'ies'} failed (see the enrich log)")
    return {"warnings": warnings, "out_of_time": out_of_time}


def compare_with_previous(by_ats, prev_by_ats, prev_date=None, min_jobs=20):
    """Flag an ATS whose job count fell by more than half since the last scrape (usually a broken fetcher)."""
    warnings = []
    for ats, before in sorted((prev_by_ats or {}).items()):
        now = by_ats.get(ats, 0)
        if before >= min_jobs and now < before * 0.5:
            when = f" on {prev_date}" if prev_date else ""
            warnings.append(f"{ats}: {now:,} jobs, down {100 - round(100 * now / before)}% from {before:,}{when}")
    return warnings


def quota_rows(summary, env):
    return [{"bucket": b, "used": info["used"], "cap": info["cap"], "period": info["period"],
             "configured": bool(env.get(QUOTA_KEYS.get(b, "")))} for b, info in summary.items()]


def build_summary(jobs, leads, metadata, quota, *, min_score, ready_threshold, cycle=None, steps=(),
                  previous=None, outreach=None, topup=None, ingest=None, enrich_log=""):
    """Every number in the evening email, from the day's files. No I/O."""
    metadata = metadata or {}
    jobs_info = jobs_section(jobs, metadata)
    shortlist = shortlist_section(jobs, min_score)
    enrich_info = parse_enrich_log(enrich_log)

    warnings = [f"{s['name']}: {s['status']} ({s.get('reason') or 'no detail'})"
                for s in steps if s.get("status") in BAD_STATUSES]
    warnings += enrich_info["warnings"]
    if not jobs:
        warnings.append("scripts/output/all_jobs.json is missing or empty")
    elif not shortlist["count"]:
        warnings.append(f"no company reached score {min_score}; lower DAILY_ENRICH_MIN_SCORE "
                        "or raise DAILY_MAX_JOBS_PER_PROVIDER")
    if previous and (cycle or {}).get("scraped_today"):
        warnings += compare_with_previous(jobs_info["by_ats"], previous.get("by_ats"), previous.get("run_date"))
    for row in quota:
        if row["configured"] and row["cap"] and row["used"] >= row["cap"]:
            warnings.append(f"{row['bucket']}: free {PERIOD_WORD.get(row['period'], row['period'])} allowance "
                            f"used up ({row['used']:g}/{row['cap']:g})")
    for inbox in (outreach or {}).get("paused_inboxes") or []:
        warnings.append(f"inbox {inbox.get('address')} is PAUSED: {inbox.get('reason')}")
    warnings += [f"outreach: {e}" for e in (outreach or {}).get("errors") or []]

    if ingest:
        ready = {"threshold": ready_threshold, "count": ingest.get("sendable") or 0,
                 "people": (ingest.get("next_up") or [])[:READY_ROWS], "source": "outreach queue"}
    else:
        ready = ready_from_leads(leads, ready_threshold)

    return {"jobs": jobs_info, "shortlist": shortlist,
            "enrichment": {**enrichment_section(leads), "out_of_time": enrich_info["out_of_time"]},
            "cycle": cycle or {}, "queue": queue_section(outreach, topup, ingest), "outreach": outreach or None,
            "ready_to_email": ready, "quota": list(quota), "warnings": list(dict.fromkeys(warnings)),
            "durations_s": {s["name"]: s.get("duration_s") for s in steps if s.get("status") != "skipped"}}


# ============================================================
# RENDERING
# ============================================================

def _plural(n, word, plural=None):
    return f"{n:,} {word if n == 1 else (plural or word + 's')}"


def _num(value):
    if value is None:
        return "-"
    return f"{value:,}" if isinstance(value, int) else f"{value:g}" if isinstance(value, float) else str(value)


def _pct(value):
    return "-" if value is None else f"{value * 100:.1f}%"


def fmt_duration(seconds):
    if seconds is None:
        return "-"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


def summary_blocks(summary, record):
    """The email as a list of blocks: ("alert"|"h"|"p", text), ("list", items), ("table", headers, rows)."""
    blocks = []
    add = blocks.append
    outreach = summary.get("outreach") or {}

    for inbox in outreach.get("paused_inboxes") or []:
        add(("alert", f"PAUSED: {inbox.get('address')} ({inbox.get('reason')}). Nothing is sent from it until "
                      f"you run: python scripts/outreach.py resume {inbox.get('address')}"))
    bad = [s for s in record.get("steps") or [] if s.get("status") in BAD_STATUSES]
    status = "all steps ok" if not bad else f"{_plural(len(bad), 'step')} need attention"
    add(("p", f"Daily run {record.get('run_date')} on {record.get('host')}: {status}, "
              f"took {fmt_duration(record.get('duration_s'))}."))

    if outreach:
        add(("h", "Outreach"))
        line = (f"Mode {outreach.get('mode', '?')}. Sent today: {_num(outreach.get('sent_first', 0))} first touches "
                f"and {_num(outreach.get('sent_followup', 0))} follow-ups.")
        if outreach.get("drafts_written"):
            line += (f" {_num(outreach['drafts_written'])} drafts are waiting for review in "
                     f"scripts/output/drafts/{record.get('run_date')}/.")
        add(("p", line))
        if outreach.get("sent"):
            add(("table", ["Name", "Company", "Kind", "Subject"],
                 [[m.get("name"), m.get("company"), m.get("kind"), m.get("subject")] for m in outreach["sent"]]))
        replies = outreach.get("replies") or []
        add(("p", f"Replies received: {len(replies)}."))
        if replies:
            add(("table", ["Name", "Company", "Title", "Received", "What they wrote"],
                 [[r.get("name"), r.get("company"), r.get("title"), r.get("at"), r.get("snippet")] for r in replies]))
        mine = outreach.get("owner_replies") or []
        if mine:
            add(("p", "You replied from your inbox to:"))
            add(("table", ["Name", "Company", "When"], [[r.get("name"), r.get("company"), r.get("at")] for r in mine]))
        add(("p", f"Bounces {_num(outreach.get('bounces', 0))}, unsubscribes {_num(outreach.get('unsubscribes', 0))}, "
                  f"out of office {_num(outreach.get('ooo', 0))}, complaints {_num(outreach.get('complaints', 0))}."))
        if outreach.get("inboxes"):
            add(("table", ["Inbox", "Status", "Warm-up day", "Cap today", "Used today", "Bounce rate 7d"],
                 [[i.get("address"), i.get("status"), i.get("warmup_day"), i.get("cap_today"), i.get("used_today"),
                   _pct(i.get("bounce_rate_7d"))] for i in outreach["inboxes"]]))
        periods = outreach.get("periods") or {}
        if periods:
            cols = [("today", "Today"), ("week", "Last 7 days"), ("month", "Month to date"), ("ytd", "Year to date")]
            metrics = [("first_sent", "First touches"), ("followup_sent", "Follow-ups"), ("replies", "Replies"),
                       ("reply_rate", "Reply rate"), ("owner_replies", "Your replies"), ("bounces", "Bounces"),
                       ("bounce_rate", "Bounce rate"), ("unsubscribes", "Unsubscribes"),
                       ("companies_contacted", "Companies contacted")]
            rows = []
            for key, label in metrics:
                values = [(periods.get(col) or {}).get(key) for col, _ in cols]
                rows.append([label] + [_pct(v) if key.endswith("_rate") else _num(v) for v in values])
            add(("table", ["Totals"] + [label for _, label in cols], rows))

    cycle = summary.get("cycle") or {}
    if cycle:
        add(("h", "Job cycle"))
        scrape = cycle.get("scrape_status")
        reason = cycle.get("reason", "")
        if scrape == "ok":
            add(("p", f"Scraped today ({reason})."))
        elif not cycle.get("due"):
            add(("p", f"No scrape today: {reason}."))
        elif scrape == "skipped":
            add(("p", f"A scrape was due ({reason}) but was skipped by request."))
        else:
            add(("p", f"A scrape was due ({reason}) but ended {scrape}; it is retried tomorrow."))
        if summary["enrichment"].get("out_of_time"):
            add(("p", "Discovery used its full time budget; the remaining companies continue tomorrow."))

    jobs = summary["jobs"]
    add(("h", "Scrape"))
    line = (f"{_plural(jobs['total'], 'job')} from {_plural(jobs['companies'], 'company', 'companies')} "
            "in the current job list")
    if jobs.get("last_updated"):
        line += f" (scraped {str(jobs['last_updated'])[:10]})"
    line += "."
    if jobs.get("collected") is not None:
        line += f" {jobs['collected']:,} were collected and {jobs.get('removed') or 0:,} removed by the filters."
    add(("p", line))
    if jobs["by_ats"]:
        add(("table", ["ATS", "Jobs"], [[k, v] for k, v in jobs["by_ats"].items()]))

    shortlist = summary["shortlist"]
    add(("h", "Shortlist"))
    shown = shortlist["companies"]
    add(("p", f"{_plural(shortlist['count'], 'company', 'companies')} scored {shortlist['min_score']} or more."
              + (f" Top {len(shown)} by score:" if shown else "")))
    if shown:
        add(("table", ["Company", "ATS", "Best score", "Jobs", "Top title"],
             [[c["company"], c["ats"], c["max_score"], c["job_count"], c["top_title"]] for c in shown]))

    en = summary["enrichment"]
    add(("h", "Enrichment"))
    add(("p", f"{_plural(en['companies'], 'company', 'companies')} discovered so far, {en['with_domain']:,} with "
              f"a website, {_plural(en['people'], 'decision maker')}"
              + (f", {en['errors']:,} failed" if en["errors"] else "") + "."))
    e = en["emails"]
    add(("table", ["Email quality", "People"], [["verified (95+)", e["verified"]], ["confident (80-94)", e["high"]],
                                                ["guessed (under 80)", e["guessed"]], ["none", e["none"]]]))
    add(("p", f"{en['direct_phones']:,} direct phones, {en['company_phones']:,} companies with a main line, "
              f"{en['personal_emails']:,} people with personal emails, "
              f"{en['pattern_detected']:,} companies with a known address pattern."))

    queue = summary.get("queue")
    if queue:
        add(("h", "Email queue"))
        line = (f"{_num(queue.get('people'))} people at {_num(queue.get('companies'))} companies "
                f"({_num(queue.get('new_today'))} new today). {_num(queue.get('sendable'))} ready to send")
        if queue.get("days_of_sends") is not None:
            line += f", about {queue['days_of_sends']:g} days at today's cap"
        add(("p", line + f"; {_num(queue.get('unresolved'))} waiting for verification credits."))
        top = queue.get("topup")
        if top:
            line = (f"Top-up today: {_num(top.get('attempted'))} tried, {_num(top.get('verified_work'))} work and "
                    f"{_num(top.get('verified_personal'))} personal addresses verified, "
                    f"{_num(top.get('no_address'))} with no address found.")
            if top.get("quota_exhausted"):
                line += " Out of credits: " + ", ".join(top["quota_exhausted"]) + "."
            add(("p", line))
        if queue.get("pool_exhausted"):
            add(("alert", "The email queue is empty. " + (f"Job cycle: {cycle.get('reason')}." if cycle else "")))

    ready = summary["ready_to_email"]
    add(("h", "Ready for tomorrow" if queue else "Ready to email"))
    people = ready["people"]
    add(("p", f"{ready['count']:,} people with an address at confidence {ready['threshold']} or higher."
              + (f" Next {len(people)} in line:" if people else "")))
    if people:
        add(("table", ["Name", "Title", "Company", "Email", "Kind", "Confidence"],
             [[p.get("name"), p.get("title"), p.get("company"), p.get("email"), p.get("email_kind"),
               p.get("email_confidence")] for p in people]))

    if summary["quota"]:
        add(("h", "Free-tier usage"))
        add(("table", ["Provider", "Used", "Allowance", "Per", "Key set"],
             [[r["bucket"], f"{r['used']:g}", f"{r['cap']:g}", r["period"], "yes" if r["configured"] else "no"]
              for r in summary["quota"]]))

    if summary["warnings"]:
        add(("h", "Warnings"))
        add(("list", summary["warnings"]))

    add(("h", "Steps"))
    add(("table", ["Step", "Status", "Took", "Note"],
         [[s.get("name"), s.get("status"), "-" if s.get("status") == "skipped" else fmt_duration(s.get("duration_s")),
           s.get("reason")] for s in record.get("steps") or []]))
    add(("p", f"Logs: scripts/output/logs/daily-{record.get('run_date')}.*.log. "
              f"Run record: scripts/output/runs/{record.get('run_date')}.json."))
    return blocks


def _cell(value, width=60):
    text = _num(value) if not isinstance(value, str) else value
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width - 3] + "..."


def text_table(headers, rows):
    cells = [[_cell(v) for v in row] for row in rows]
    widths = [max([len(str(h))] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]

    def line(values):
        return "  " + "  ".join(v.ljust(w) for v, w in zip(values, widths)).rstrip()

    return [line([str(h) for h in headers]), line(["-" * w for w in widths])] + [line(r) for r in cells]


def render_text(blocks):
    out = []
    for block in blocks:
        kind = block[0]
        if kind == "alert":
            out += ["", f"!!! {block[1]}", ""]
        elif kind == "h":
            out += ["", block[1].upper(), "-" * len(block[1])]
        elif kind == "p":
            out.append(block[1])
        elif kind == "list":
            out += [f"  - {item}" for item in block[1]]
        elif kind == "table":
            out += text_table(block[1], block[2])
    return "\n".join(out).strip() + "\n"


def render_html(blocks):
    esc = html.escape
    parts = ['<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;font-size:14px;'
             'color:#1a1a1a;max-width:820px">']
    cell = "padding:3px 12px 3px 0;text-align:left;vertical-align:top"
    for block in blocks:
        kind = block[0]
        if kind == "alert":
            parts.append(f'<p style="background:#b3261e;color:#fff;padding:10px 12px;font-weight:600">'
                         f'{esc(block[1])}</p>')
        elif kind == "h":
            parts.append(f'<h3 style="margin:22px 0 6px;font-size:15px">{esc(block[1])}</h3>')
        elif kind == "p":
            parts.append(f'<p style="margin:4px 0">{esc(block[1])}</p>')
        elif kind == "list":
            parts.append("<ul>" + "".join(f"<li>{esc(item)}</li>" for item in block[1]) + "</ul>")
        elif kind == "table":
            head = "".join(f'<th style="{cell};border-bottom:1px solid #999">{esc(str(h))}</th>' for h in block[1])
            body = "".join("<tr>" + "".join(f'<td style="{cell}">{esc(_cell(v, 300))}</td>' for v in row) + "</tr>"
                           for row in block[2])
            parts.append(f'<table style="border-collapse:collapse;font-size:13px"><tr>{head}</tr>{body}</table>')
    parts.append("</div>")
    return "\n".join(parts)


def build_subject(summary, record):
    outreach = summary.get("outreach") or {}
    parts = []
    if outreach:
        sent = (outreach.get("sent_first") or 0) + (outreach.get("sent_followup") or 0)
        parts += [f"{sent} sent", _plural(len(outreach.get("replies") or []), "reply", "replies")]
    parts += [f"{summary['ready_to_email']['count']:,} ready", f"{summary['shortlist']['count']:,} shortlisted"]
    if summary["warnings"]:
        parts.append(_plural(len(summary["warnings"]), "warning"))
    prefix = "PAUSED " if outreach.get("paused_inboxes") else ""
    return f"{prefix}[JBA] {record.get('run_date')}: " + ", ".join(parts)


# ============================================================
# EMAIL
# ============================================================

def csv_has_rows(path):
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for _, line in zip(range(2), f) if line.strip()) > 1
    except OSError:
        return False


def pick_attachment(paths):
    return paths["queue_csv"] if os.path.exists(paths["queue_csv"]) else paths["leads_csv"]


def build_email(summary, record, cfg, attachment=None):
    blocks = summary_blocks(summary, record)
    sender = cfg.smtp_from or cfg.smtp_user
    msg = EmailMessage()
    msg["Subject"] = build_subject(summary, record)
    msg["From"] = sender
    msg["To"] = ", ".join(cfg.summary_to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1].strip("> ") if "@" in sender else None)
    msg.set_content(render_text(blocks))
    msg.add_alternative(render_html(blocks), subtype="html")
    if attachment and csv_has_rows(attachment):
        kind = "queue" if attachment.endswith(".queue.csv") else "leads"
        with open(attachment, "rb") as f:
            msg.add_attachment(f.read(), maintype="text", subtype="csv",
                               filename=f"{kind}-{record.get('run_date')}.csv")
    return msg


def smtp_connect(cfg):
    context = ssl.create_default_context()
    if cfg.smtp_port == 465:
        return smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30, context=context)
    server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
    server.starttls(context=context)
    return server


def smtp_authenticate(server, cfg):
    # one place to add XOAUTH2 when Microsoft 365 drops basic auth
    if cfg.smtp_auth != "login":
        raise NotImplementedError(f"SMTP_AUTH={cfg.smtp_auth} is not supported yet; use login")
    if cfg.smtp_user:
        server.login(cfg.smtp_user, cfg.smtp_pass)


def send_email(msg, cfg, connect=smtp_connect):
    """Never raises: the result is stored in the run record."""
    if not cfg.email_configured:
        return {"status": "skipped", "to": cfg.summary_to, "error": "SMTP_HOST or SUMMARY_TO is not set"}
    try:
        server = connect(cfg)
        try:
            smtp_authenticate(server, cfg)
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:
                pass
        return {"status": "sent", "to": cfg.summary_to, "error": None}
    except Exception as exc:
        return {"status": "failed", "to": cfg.summary_to, "error": f"{type(exc).__name__}: {exc}"}


def send_test_email(cfg):
    msg = EmailMessage()
    msg["Subject"] = f"[JBA] test from {socket.gethostname()}"
    msg["From"] = cfg.smtp_from or cfg.smtp_user
    msg["To"] = ", ".join(cfg.summary_to)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(f"SMTP works: {cfg.smtp_host}:{cfg.smtp_port} as {cfg.smtp_user or '(no login)'}.\n")
    result = send_email(msg, cfg)
    print(f"test email: {result['status']}" + (f" ({result['error']})" if result.get("error") else ""))
    return 0 if result["status"] == "sent" else 1


def resend(cfg):
    paths = run_paths(cfg.run_date)
    record = read_json(paths["record"])
    if not record or not record.get("summary"):
        print(f"no run record with a summary at {paths['record']}")
        return 1
    msg = build_email(record["summary"], record, cfg, pick_attachment(paths))
    result = send_email(msg, cfg)
    record["email"] = {**result, "subject": str(msg["Subject"]), "resent_at": utc_now()}
    write_run_record(paths["record"], record)
    print(f"summary for {cfg.run_date}: {result['status']}" + (f" ({result['error']})" if result.get("error") else ""))
    return 0 if result["status"] == "sent" else 1


# ============================================================
# MAIN
# ============================================================

def acquire_lock(path=LOCK_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def setup_logging(run_date):
    os.makedirs(LOGS_DIR, exist_ok=True)
    log.setLevel(logging.INFO)
    log.propagate = False
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (logging.FileHandler(os.path.join(LOGS_DIR, f"daily-{run_date}.log"), encoding="utf-8"),
                    logging.StreamHandler(sys.stdout)):
        handler.setFormatter(fmt)
        log.addHandler(handler)


def file_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def crash_email(cfg, error_text):
    msg = EmailMessage()
    msg["Subject"] = f"[JBA] {cfg.run_date}: daily run crashed"
    msg["From"] = cfg.smtp_from or cfg.smtp_user
    msg["To"] = ", ".join(cfg.summary_to)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(f"The daily run on {socket.gethostname()} stopped before the summary was built.\n\n"
                    f"{error_text}\nLog: scripts/output/logs/daily-{cfg.run_date}.log\n")
    return msg


def main(argv=None):
    cfg = load_config(argv)
    if cfg.test_email:
        return send_test_email(cfg)
    if cfg.resend:
        return resend(cfg)

    lock = acquire_lock()
    if lock is None:
        print(f"another daily run holds {LOCK_FILE}; exiting")
        return EXIT_LOCKED
    os.makedirs(RUNS_DIR, exist_ok=True)
    setup_logging(cfg.run_date)
    signal.signal(signal.SIGTERM, lambda signum, frame: STOP.set())
    try:
        return daily_run(cfg)
    except Exception:
        # the evening email is the only alarm, so a crash still sends one
        error = traceback.format_exc()
        log.error("daily run crashed:\n%s", error)
        if cfg.send_email:
            result = send_email(crash_email(cfg, error), cfg)
            log.info("crash email: %s%s", result["status"], f" ({result['error']})" if result.get("error") else "")
        return 1


def daily_run(cfg):
    paths = run_paths(cfg.run_date)
    started = time.time()
    record = {"run_date": cfg.run_date, "started_at": utc_now(), "finished_at": None, "duration_s": None,
              "host": socket.gethostname(), "git_rev": git_rev(), "config": cfg.public(), "steps": [],
              "cycle": None, "files": {}, "summary": None, "outreach": None, "topup": None, "ingest": None,
              "email": None}
    history = previous_records(cfg.run_date)
    has_outreach = os.path.exists(OUTREACH_SCRIPT)
    commands = build_commands(cfg, paths, has_outreach)
    log.info("daily run %s on %s; outreach module %s", cfg.run_date, record["host"],
             "present" if has_outreach else "not installed")

    def step(name, skip_reason=None):
        if name in cfg.skip:
            result = StepResult(name, status="skipped", reason="skipped by request")
        elif skip_reason:
            result = StepResult(name, status="skipped", reason=skip_reason)
        elif STOP.is_set():
            result = StepResult(name, status="killed", reason="stop requested before the step started")
        else:
            cmd, cwd = commands[name]
            log.info("%s: starting", name)
            result = run_step(name, cmd, cwd, cfg.timeouts_min[name] * 60, step_log(cfg.run_date, name), STOP)
        log.info("%s: %s%s", name, result.status, f" ({result.reason})" if result.reason else "")
        record["steps"].append(asdict(result))
        return result

    not_installed = None if has_outreach else "scripts/outreach.py is not installed yet"
    step("outreach", not_installed)
    step("topup", not_installed)

    topup_stats = read_json(paths["topup"]) or {}
    last_scrape = last_successful_scrape(history)
    due, why = cycle_due(last_scrape, cfg.run_date, bool(topup_stats.get("pool_exhausted")),
                         cfg.cycle_days, cfg.cycle_min_days, cfg.force_scrape)
    scrape_started = time.time()
    scrape = step("scrape", None if due else why)
    if scrape.status == "ok" and file_mtime(JOBS_FILE) < scrape_started:
        scrape.status, scrape.reason = "failed", "exited 0 but all_jobs.json was not rewritten"
        record["steps"][-1] = asdict(scrape)
    step("merge", None if scrape.status == "ok" else "no new scrape today")
    step("enrich", None if os.path.exists(JOBS_FILE) else "no scripts/output/all_jobs.json yet")
    record["files"] = archive_leads(paths)
    step("ingest", not_installed or (None if record["files"].get("leads_json") else "no leads.json to ingest"))

    record["cycle"] = {"due": due, "reason": why, "scrape_status": scrape.status,
                       "scraped_today": scrape.status == "ok", "cycle_days": cfg.cycle_days,
                       "last_scrape": cfg.run_date if scrape.status == "ok" else last_scrape}
    for key in ("outreach", "topup", "ingest"):
        record[key] = read_json(paths[key])

    prev = next((r for r in history if any(s.get("name") == "scrape" and s.get("status") == "ok"
                                           for s in r.get("steps") or [])), None)
    previous = {"run_date": prev["run_date"],
                "by_ats": ((prev.get("summary") or {}).get("jobs") or {}).get("by_ats")} if prev else None
    summary = build_summary(
        read_json(JOBS_FILE, []) or [], read_json(LEADS_JSON, []) or [], read_json(METADATA_FILE, {}) or {},
        quota_rows(QuotaTracker().summary(), os.environ),
        min_score=cfg.enrich_min_score, ready_threshold=cfg.ready_threshold, cycle=record["cycle"],
        steps=record["steps"], previous=previous, outreach=record["outreach"], topup=record["topup"],
        ingest=record["ingest"], enrich_log=last_section(step_log(cfg.run_date, "enrich")))
    record["summary"] = summary
    record["finished_at"] = utc_now()
    record["duration_s"] = round(time.time() - started, 1)
    write_run_record(paths["record"], record)

    text = render_text(summary_blocks(summary, record))
    with open(paths["summary"], "w", encoding="utf-8") as f:
        f.write(text)
    if cfg.print_summary:
        print(text)

    if cfg.send_email:
        msg = build_email(summary, record, cfg, pick_attachment(paths))
        record["email"] = {**send_email(msg, cfg), "subject": str(msg["Subject"])}
    else:
        record["email"] = {"status": "skipped", "to": cfg.summary_to, "error": "--no-email", "subject": None}
    log.info("summary email: %s%s", record["email"]["status"],
             f" ({record['email']['error']})" if record["email"].get("error") else "")
    write_run_record(paths["record"], record)
    prune_old([RUNS_DIR, LOGS_DIR])

    failed = [s for s in record["steps"] if s["status"] in BAD_STATUSES]
    return 1 if failed or record["email"]["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
