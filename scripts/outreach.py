#!/usr/bin/env python3
"""
Outreach queue and sender: the steps daily_run.py calls (ingest, topup, run) and the commands you run
by hand (stop, unstop, status, add-inbox, list-inboxes, pause, resume).

Zoho's free plan sends over SMTP but has no IMAP, so nothing reads the inbox. When someone replies,
unsubscribes or bounces, run `outreach.py stop EMAIL --reason ...`: any pending follow-up is cancelled
and the person is never emailed again. The evening summary lists the next run's follow-ups so you can
stop them first.

Nobody gets a second first email. Before one is sent, the mailbox (one key per mailbox), the person,
their LinkedIn profile and their name at the company domain are checked against everything already
sent, and the database refuses a second first email to the same mailbox or person. A follow-up goes
only to the address of its first email, at most once.

Modes (OUTREACH_MODE): off sends nothing and spends no credits; review writes .eml drafts to
scripts/output/drafts/YYYY-MM-DD/ and spends credits on top-ups; send delivers through ready inboxes.
"""

import argparse
import csv
import fcntl
import json
import os
import random
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import outreach_mail as mail
import outreach_store as store
from enrich_leads import (
    CONFIDENCE, ENV_FILE, GENERIC_LOCAL_PARTS, JOBS_FILE, LEADS_JSON, OUTPUT_DIR, build_providers, load_dotenv,
    norm_name, resolve_email, resolve_phone, seniority, strip_html, verify_email,
)
from lead_providers import QuotaTracker
from outreach_compose import BUCKET_ORDER, compose, full_text, lint_copy, lint_message

DRAFTS_DIR = os.path.join(OUTPUT_DIR, "drafts")
BACKUP_DIR = os.path.join(OUTPUT_DIR, "backups")
METADATA_FILE = os.path.join(OUTPUT_DIR, "metadata.json")
LOCK_FILE = os.path.join(OUTPUT_DIR, "outreach.lock")
KEEP_BACKUPS = 14
EXIT_LOCKED = 75
ISO = "%Y-%m-%dT%H:%M:%SZ"
CREDIT_PROVIDERS = ("hunter", "tomba", "apollo", "zerobounce")
VERIFIERS = ("zerobounce", "hunter", "tomba")
# at equal seniority the engineering buyer comes first: a CTO before a CISO or CFO
ENGINEERING_BUYER_RE = re.compile(r"\b(cto|chief technology|chief technical|engineering|software|platform)\b", re.I)
QUEUE_FIELDS = ["company", "company_score", "name", "title", "status", "email", "email_kind", "email_confidence",
                "phone", "linkedin", "first_emailed", "job_last_seen"]


# ============================================================
# SETTINGS
# ============================================================

@dataclass
class Settings:
    mode: str = "off"
    min_confidence: int = 90
    warmup: list = field(default_factory=lambda: [(5, 7), (10, 7), (20, None)])
    gap: tuple = (60, 180)
    followup_days: int = 7
    cooldown_days: int = 90
    bounce_pause_pct: float = 3.0
    bounce_min_sent: int = 20
    allow_personal: bool = True
    topup_days: int = 3
    send_tz: str = "America/New_York"
    local_tz: str = "Asia/Karachi"
    sender_name: str = ""
    agency_name: str = "drawnonward"
    sender_address_line: str = ""
    unsubscribe_address: str = ""
    own_domains: set = field(default_factory=set)


def parse_warmup(spec):
    """'5:7,10:7,20' -> [(5, 7), (10, 7), (20, None)]: 5 a day for 7 days, 10 for 7, then 20."""
    steps = []
    for part in (spec or "").split(","):
        cap, _, days = part.strip().partition(":")
        if cap:
            steps.append((int(cap), int(days) if days else None))
    if not steps or steps[-1][1] is not None:
        steps.append((steps[-1][0] if steps else 5, None))
    return steps


def load_settings(env):
    def num(key, default, kind=int):
        raw = (env.get(key) or "").strip()
        return kind(raw) if raw else default

    low, _, high = (env.get("OUTREACH_GAP") or "60-180").partition("-")
    mode = (env.get("OUTREACH_MODE") or "off").strip().lower()
    own = {store.email_domain(env.get(k)) for k in ("SMTP_USER", "SMTP_FROM") if env.get(k)}
    return Settings(
        mode=mode if mode in ("off", "review", "send") else "off",
        min_confidence=num("OUTREACH_MIN_CONFIDENCE", 90),
        warmup=parse_warmup(env.get("OUTREACH_WARMUP") or "5:7,10:7,20"),
        gap=(float(low), float(high or low)),
        followup_days=num("OUTREACH_FOLLOWUP_DAYS", 7),
        cooldown_days=num("OUTREACH_COMPANY_COOLDOWN_DAYS", 90),
        bounce_pause_pct=num("OUTREACH_BOUNCE_PAUSE_PCT", 3.0, float),
        bounce_min_sent=num("OUTREACH_BOUNCE_MIN_SENT", 20),
        allow_personal=(env.get("OUTREACH_ALLOW_PERSONAL") or "1").strip() not in ("0", "false", "no"),
        topup_days=num("OUTREACH_TOPUP_DAYS", 3),
        send_tz=(env.get("OUTREACH_DEFAULT_TZ") or "America/New_York").strip(),
        local_tz=(env.get("DAILY_TZ") or "Asia/Karachi").strip(),
        sender_name=(env.get("SENDER_NAME") or "").strip(),
        agency_name=(env.get("AGENCY_NAME") or "drawnonward").strip(),
        sender_address_line=(env.get("SENDER_ADDRESS_LINE") or "").strip(),
        unsubscribe_address=(env.get("UNSUBSCRIBE_ADDRESS") or "").strip(),
        own_domains={d for d in own if d},
    )


def local_today(settings):
    return datetime.now(ZoneInfo(settings.local_tz)).date().isoformat()


def warmup_cap(inbox, today, steps):
    """(cap, warm-up day) for today, counted from the day the inbox became ready; day 1 before that."""
    started = inbox.get("warmup_started_at")
    elapsed = max((date.fromisoformat(today) - date.fromisoformat(started[:10])).days, 0) if started else 0
    day = elapsed
    for cap, days in steps:
        if days is None or day < days:
            return cap, elapsed + 1
        day -= days
    return steps[-1][0], elapsed + 1


def email_kind(email, domain):
    host = store.email_domain(email) or ""
    domain = (domain or "").lower()
    return "work" if domain and (host == domain or host.endswith("." + domain)) else "personal"


# ============================================================
# ELIGIBILITY AND DEDUPLICATION
# ============================================================

def queue_order(lead):
    return (-(lead.get("company_score") or 0), -(lead.get("seniority") or -1),
            0 if ENGINEERING_BUYER_RE.search(lead.get("title") or "") else 1, -(lead.get("email_confidence") or 0),
            (lead.get("name") or "").lower())


def person_blocker(lead, history, suppressed, latest, settings):
    """Why this person cannot be emailed, whatever address they end up with."""
    domain = lead.get("domain")
    if domain and domain in settings.own_domains:
        return "own_domain"
    if domain and f"@{domain}" in suppressed:
        return "suppressed"
    if (lead["person_key"] in history.person_keys
            or (lead.get("linkedin_key") and lead["linkedin_key"] in history.linkedin_keys)
            or (domain and (lead["name_key"], domain) in history.name_domains)):
        return "already_contacted"
    if lead["company_key"] in history.recent_companies or (domain and domain in history.recent_domains):
        return "company_cooldown"
    if latest and lead.get("job_last_seen") != latest:
        return "job_closed"
    return None


def skip_reason(lead, history, suppressed, latest, settings):
    """None when a first email may go to this lead now."""
    if not lead.get("email") or (lead.get("email_confidence") or 0) < settings.min_confidence:
        return person_blocker(lead, history, suppressed, latest, settings) or "unresolved"
    key, host = lead["email_key"], store.email_domain(lead["email"])
    if lead["email"].split("@")[0].lower() in GENERIC_LOCAL_PARTS:
        return "role_address"
    if lead.get("email_kind") == "personal" and not settings.allow_personal:
        return "personal_not_allowed"
    if host in settings.own_domains:
        return "own_domain"
    if store.is_suppressed(suppressed, key, host):
        return "suppressed"
    if key in history.email_keys:
        return "already_contacted"
    return person_blocker(lead, history, suppressed, latest, settings)


def filter_eligible(leads, history, suppressed, latest, settings):
    """Sendable leads in queue order, one per company, and a count of why the rest were skipped."""
    eligible, skipped, companies = [], Counter(), set()
    for lead in sorted(leads, key=queue_order):
        reason = skip_reason(lead, history, suppressed, latest, settings)
        keys = {lead["company_key"], lead.get("domain")} - {None, ""}
        if not reason and keys & companies:
            reason = "company_dup_in_run"
        if reason:
            skipped[reason] += 1
            continue
        companies |= keys
        eligible.append(lead)
    return eligible, skipped


def cooldown_since(now, settings):
    return (now - timedelta(days=settings.cooldown_days)).strftime(ISO)


# ============================================================
# INGEST
# ============================================================

def jobs_snapshot(company_jobs, limit=3):
    """The JD stays with the lead after the posting closes or the next scrape replaces all_jobs.json."""
    best = sorted(company_jobs, key=lambda j: -int(j.get("lead_intent_score") or 0))[:limit]
    return [{"title": j.get("title") or "", "url": j.get("url") or "", "location": j.get("location") or "",
             "score": int(j.get("lead_intent_score") or 0),
             "buckets": [b for b in BUCKET_ORDER if (j.get("lead_intent_matches") or {}).get(b)],
             "excerpt": strip_html(j.get("description") or "")[:1500]} for j in best]


def candidate(email, confidence, status, domain):
    return {"email": email.lower(), "email_key": store.email_key(email), "kind": email_kind(email, domain),
            "confidence": int(confidence or 0), "status": status or ""}


def lead_fields(company, person, snapshot, job_seen):
    """(person_key, fields) for one person of a leads.json company record, or None without a usable name."""
    name = (person.get("name") or "").strip()
    name_key = norm_name(name)
    if not name_key:
        return None
    domain = company.get("domain")
    email = (person.get("email") or "").strip().lower() or None
    if email and not store.email_key(email):
        email = None
    confidence = int(person.get("email_confidence") or 0) if email else 0
    candidates = [candidate(email, confidence, person.get("email_status"), domain)] if email else []
    for address in person.get("personal_emails") or []:
        key = store.email_key(address)
        if key and key not in {c["email_key"] for c in candidates}:
            candidates.append(candidate(address, 0, "unverified personal address", domain))
    return f"{company['company_key']}|{name_key}", {
        "company_key": company["company_key"], "company": company.get("company"), "domain": domain,
        "company_pattern": company.get("email_pattern"), "company_pattern_source": company.get("email_pattern_source"),
        "company_score": int(company.get("max_score") or 0), "name": name, "name_key": name_key,
        "title": person.get("title") or "", "seniority": seniority(person.get("title")),
        "linkedin": person.get("linkedin") or "", "linkedin_key": store.linkedin_key(person.get("linkedin")),
        "candidate_emails": candidates, "email": email, "email_key": store.email_key(email),
        "email_kind": email_kind(email, domain) if email else None, "email_confidence": confidence,
        "email_status": person.get("email_status") if email else None,
        "phone": person.get("phone"), "phone_type": person.get("phone_type"),
        "jobs_snapshot": snapshot, "job_last_seen": job_seen,
    }


def queue_rows(conn, settings, now):
    """Every lead with its queue status, the eligible list, and the skip counts."""
    leads = store.all_leads(conn)
    history = store.contact_history(conn, cooldown_since(now, settings))
    suppressed = store.suppressed_values(conn)
    latest = store.latest_scrape(conn)
    eligible, skipped = filter_eligible(leads, history, suppressed, latest, settings)
    return leads, history, suppressed, latest, eligible, skipped


def next_up(eligible, limit=10):
    return [{"company": l.get("company"), "name": l["name"], "title": l.get("title"), "email": l["email"],
             "email_kind": l.get("email_kind"), "email_confidence": l.get("email_confidence")}
            for l in eligible[:limit]]


def write_queue_csv(path, conn, leads, eligible, history, suppressed, latest, settings):
    first = dict(conn.execute("SELECT lead_id, MIN(day) FROM messages WHERE kind = 'first' AND status IN "
                              "('sending', 'sent') GROUP BY lead_id").fetchall())
    ready = {l["id"] for l in eligible}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=QUEUE_FIELDS)
        writer.writeheader()
        for lead in sorted(leads, key=queue_order):
            if lead["id"] in first:
                status = "emailed"
            elif lead["id"] in ready:
                status = "ready"
            else:
                status = skip_reason(lead, history, suppressed, latest, settings) or "another person at this company"
            writer.writerow({"company": lead.get("company"), "company_score": lead.get("company_score"),
                             "name": lead["name"], "title": lead.get("title"), "status": status,
                             "email": lead.get("email") or "", "email_kind": lead.get("email_kind") or "",
                             "email_confidence": lead.get("email_confidence") or "", "phone": lead.get("phone") or "",
                             "linkedin": lead.get("linkedin") or "", "first_emailed": first.get(lead["id"], ""),
                             "job_last_seen": lead.get("job_last_seen") or ""})


def backup_db(conn, today, backup_dir=BACKUP_DIR, keep=KEEP_BACKUPS):
    """Nightly copy: outreach.db is what stops anyone being emailed twice, so it must survive a bad disk day."""
    store.backup(conn, os.path.join(backup_dir, f"outreach-{today}.db"))
    old = sorted(f for f in os.listdir(backup_dir) if f.startswith("outreach-") and f.endswith(".db"))
    for name in old[:-keep]:
        os.remove(os.path.join(backup_dir, name))


def ingest_command(conn, leads, jobs, metadata, today, settings, now=None, queue_csv=None, backup_dir=BACKUP_DIR):
    now = now or datetime.now(timezone.utc)
    scrape_date = str((metadata or {}).get("last_updated") or "")[:10] or today
    by_company = defaultdict(list)
    for job in jobs or []:
        key = job.get("company") or job.get("company_slug")
        if key:
            by_company[key].append(job)
    counts = Counter()
    with store.transaction(conn):
        for company in leads or []:
            if company.get("error") or not company.get("company_key"):
                continue
            key = company["company_key"]
            snapshot = jobs_snapshot(by_company.get(key) or [])
            for person in company.get("people") or []:
                built = lead_fields(company, person, snapshot, scrape_date if key in by_company else None)
                if built:
                    counts[store.upsert_lead(conn, *built, keep_at_or_above=settings.min_confidence)] += 1
        store.mark_job_seen(conn, list(by_company), scrape_date)

    all_rows, history, suppressed, latest, eligible, skipped = queue_rows(conn, settings, now)
    unresolved = len(topup_candidates(all_rows, history, suppressed, latest, settings))
    if queue_csv:
        write_queue_csv(queue_csv, conn, all_rows, eligible, history, suppressed, latest, settings)
    if backup_dir:
        backup_db(conn, today, backup_dir)
    return {"new": counts["new"], "updated": counts["updated"], "queue": len(all_rows),
            "queue_companies": len({l["company_key"] for l in all_rows}), "sendable": len(eligible),
            "unresolved": unresolved, "skipped_reasons": dict(skipped), "next_up": next_up(eligible),
            "scrape_date": scrape_date}


# ============================================================
# TOP-UP: spend finder and verifier credits just ahead of sending
# ============================================================

def topup_candidates(leads, history, suppressed, latest, settings):
    """Most senior not-yet-tried person per company that has nobody sendable, best companies first."""
    by_company = defaultdict(list)
    for lead in leads:
        by_company[lead["company_key"]].append(lead)
    picks = []
    for people in by_company.values():
        reasons = [(skip_reason(l, history, suppressed, latest, settings), l) for l in people]
        if any(r is None for r, _ in reasons):
            continue
        open_ = [l for r, l in reasons if r == "unresolved" and not l.get("resolve_attempted_at")]
        if open_:
            picks.append(min(open_, key=queue_order))
    return sorted(picks, key=queue_order)


def credits_left(providers):
    usable = []
    for name in CREDIT_PROVIDERS:
        p = providers.get(name)
        if not p or p.disabled_reason:
            continue
        buckets = [p.name] + ([p.verify_bucket] if p.verify_bucket else [])
        if any(p.quota.allow(b, 0.5) for b in buckets):
            usable.append(name)
    return usable


def verify_address(address, providers):
    """('valid' | 'invalid' | 'risky' | 'unknown', verifier) or (None, None) when no verifier has credits."""
    for name in VERIFIERS:
        v = providers.get(name)
        if v and v.available(v.verify_cost, v.verify_bucket):
            verdict = v.verify(address)
            if verdict:
                return verdict["status"], name
    return None, None


def resolve_lead(lead, providers, settings):
    """Run the enrichment waterfall for one queued person; returns the fields to write back."""
    person = {"name": lead["name"], "title": lead.get("title"), "linkedin": lead.get("linkedin")}
    if lead.get("email") and (lead.get("email_confidence") or 0) >= CONFIDENCE["apollo"]:
        # a real address from discovery (GitHub, Apollo) is kept; pattern guesses are rebuilt with alternates
        person.update(email=lead["email"], email_confidence=lead["email_confidence"],
                      email_status=lead.get("email_status"))
    found = resolve_email(person, lead.get("domain"), lead.get("company"), [], lead.get("company_pattern"),
                          lead.get("company_pattern_source"), providers, settings.min_confidence)
    found = verify_email(found, providers)
    best = found if found and found.get("email") else None
    candidates = list(lead.get("candidate_emails") or [])
    seen = {c.get("email_key") for c in candidates}

    def remember(email, confidence, status):
        key = store.email_key(email)
        if key and key not in seen:
            seen.add(key)
            candidates.append(candidate(email, confidence, status, lead.get("domain")))

    if best:
        remember(best["email"], best["email_confidence"], best["email_status"])
    for address in (found or {}).get("personal_emails") or []:
        remember(address, 0, "unverified personal address (apollo)")

    note = (best or found or {}).get("email_status") or "no address found"
    if (not best or best["email_confidence"] < settings.min_confidence) and settings.allow_personal:
        for c in [c for c in candidates if c.get("kind") == "personal"]:
            verdict, used = verify_address(c["email"], providers)
            if verdict is None:
                note += "; no verifier credits left for personal addresses"
                break
            c["status"] = f"{verdict} ({used})"
            if verdict == "valid":
                c["confidence"] = CONFIDENCE["verified"]
                best = {"email": c["email"], "email_confidence": CONFIDENCE["verified"],
                        "email_status": f"personal address verified by {used}"}
                note += f"; personal address verified by {used}"
                break

    # Tomba's phone finder would spend the same scarce credits the email search needs
    phone_providers = {k: v for k, v in providers.items() if k != "tomba"}
    phone, phone_type = resolve_phone({"phone": lead.get("phone")}, phone_providers, [])
    fields = {"candidate_emails": candidates, "resolve_attempted_at": store.utc_now(), "resolve_note": note[:500],
              "phone": phone or lead.get("phone"), "phone_type": phone_type if phone else lead.get("phone_type")}
    if best and best["email_confidence"] >= (lead.get("email_confidence") or 0):
        fields.update(email=best["email"], email_key=store.email_key(best["email"]),
                      email_kind=email_kind(best["email"], lead.get("domain")),
                      email_confidence=best["email_confidence"], email_status=best["email_status"])
    return fields


def topup_target(inboxes, today, settings):
    usable = [i for i in inboxes if i["status"] == "ready" or (settings.mode == "review" and i["status"] == "pending")]
    caps = [warmup_cap(i, today, settings.warmup)[0] for i in usable] or [settings.warmup[0][0]]
    return settings.topup_days * sum(caps)


def topup_command(conn, settings, today, providers, now=None):
    now = now or datetime.now(timezone.utc)
    if settings.mode == "off":
        return {"skipped": "OUTREACH_MODE=off, so no credits are spent", "pool_exhausted": False}
    target = topup_target(store.list_inboxes(conn), today, settings)
    leads, history, suppressed, latest, eligible, _ = queue_rows(conn, settings, now)
    stats = {"target": target, "sendable_before": len(eligible), "attempted": 0, "verified_work": 0,
             "verified_personal": 0, "no_address": 0, "quota_exhausted": []}
    sendable = len(eligible)
    for lead in topup_candidates(leads, history, suppressed, latest, settings):
        if sendable >= target:
            break
        if not credits_left(providers):
            break
        fields = resolve_lead(lead, providers, settings)
        store.update_lead(conn, lead["id"], **fields)
        stats["attempted"] += 1
        updated = {**lead, **fields}
        if skip_reason(updated, history, suppressed, latest, settings) is None:
            sendable += 1
            stats["verified_work" if updated.get("email_kind") == "work" else "verified_personal"] += 1
        elif (updated.get("email_confidence") or 0) < settings.min_confidence:
            stats["no_address"] += 1
        print(f"  {lead['name']} ({lead.get('company')}): {updated.get('email') or '-'} "
              f"[{updated.get('email_confidence') or 0}] {fields['resolve_note']}")
    usable = credits_left(providers)
    stats["quota_exhausted"] = [f"{name} ({providers[name].disabled_reason or 'free quota used up'})"
                                for name in CREDIT_PROVIDERS if name in providers and name not in usable]
    leads, history, suppressed, latest, eligible, _ = queue_rows(conn, settings, now)
    remaining = topup_candidates(leads, history, suppressed, latest, settings)
    stats.update(sendable_after=len(eligible), unresolved_after=len(remaining),
                 pool_exhausted=not eligible and not remaining)
    return stats


# ============================================================
# RUN: follow-ups first, then first emails, within each inbox's warm-up cap
# ============================================================

class SendFailed(Exception):
    pass


class Delivery:
    """Drafts or sends one inbox's messages. A real send is claimed in the database before SMTP sees it."""

    def __init__(self, conn, inbox, settings, today, env, connect_smtp, drafts_dir, sleep, stats):
        self.conn, self.inbox, self.settings, self.today, self.env = conn, inbox, settings, today, env
        self.connect_smtp, self.drafts_dir, self.sleep, self.stats = connect_smtp, drafts_dir, sleep, stats
        self.server = None
        self.review = settings.mode == "review"

    def first(self, lead):
        job = (lead.get("jobs_snapshot") or [{}])[0]
        return self._deliver(lead, "first", compose(lead, job, self.inbox, "first", self.settings), parent=None)

    def followup(self, parent):
        lead = store.get_lead(self.conn, parent["lead_id"])
        job = (lead.get("jobs_snapshot") or [{}])[0]
        content = compose(lead, job, self.inbox, "followup", self.settings, parent_subject=parent["subject"])
        return self._deliver(lead, "followup", content, parent=parent)

    def _skip(self, reason):
        self.stats["skipped_reasons"][reason] += 1
        return False

    def _deliver(self, lead, kind, content, parent):
        to_email = parent["to_email"] if parent else lead["email"]  # a follow-up never changes address
        text = full_text(content["body"], self.settings, self.inbox)
        message_id = mail.new_message_id(self.inbox["address"])
        msg = mail.build_message(self.inbox, lead["name"], to_email, content["subject"], text, message_id,
                                 self.settings.unsubscribe_address or self.inbox["address"],
                                 in_reply_to=parent["message_id"] if parent else None)
        errors = [t for level, t in lint_copy(content["subject"], content["body"], kind) if level == "error"]
        errors += lint_message(msg, kind)
        if errors:
            self.stats["errors"].append(f"{lead['name']} ({kind}) not sent: {'; '.join(errors)}")
            return self._skip("lint_failed")
        fields = dict(message_id=message_id, lead_id=lead["id"], inbox_id=self.inbox["id"], kind=kind,
                      parent_id=parent["id"] if parent else None, to_email=to_email,
                      email_key=store.email_key(to_email), person_key=lead["person_key"],
                      company_key=lead["company_key"], domain=lead.get("domain"), subject=content["subject"],
                      body_text=text, day=self.today)
        if self.review:
            folder = os.path.join(self.drafts_dir, self.today)
            seq = len(os.listdir(folder)) + 1 if os.path.isdir(folder) else 1
            slug = re.sub(r"[^a-z0-9]+", "-", lead["company_key"].lower()).strip("-")[:40]
            path = mail.save_draft(msg, os.path.join(folder, f"{seq:02d}-{slug}-{kind}.eml"))
            store.insert_message(self.conn, **fields, status="draft", draft_path=path)
            self.stats["drafts_written"] += 1
            self.stats["drafts"].append(path)
            return True

        if self.stats["_sent_any"]:
            self.sleep(random.uniform(*self.settings.gap))
        try:
            with store.transaction(self.conn):
                # re-read right before the send so a `stop` run during this job still wins
                if parent and not store.followup_still_due(self.conn, parent["id"]):
                    return self._skip("stopped")
                if store.is_suppressed(store.suppressed_values(self.conn), fields["email_key"],
                                       store.email_domain(to_email), lead.get("domain")):
                    return self._skip("suppressed")
                pk = store.insert_message(self.conn, **fields, status="sending")
        except sqlite3.IntegrityError:
            return self._skip("duplicate_blocked")
        try:
            if self.server is None:
                self.server = self.connect_smtp(self.inbox, self.env)
            self.server.send_message(msg)
        except Exception as exc:
            store.update_message(self.conn, pk, status="failed", error=f"{exc.__class__.__name__}: {exc}"[:500])
            raise SendFailed(f"{exc.__class__.__name__}: {exc}") from exc
        due = (date.fromisoformat(self.today) + timedelta(days=self.settings.followup_days)).isoformat()
        store.update_message(self.conn, pk, status="sent", sent_at=store.utc_now(),
                             followup_due=due if kind == "first" else None)
        if parent:
            store.update_message(self.conn, parent["id"], followup_due=None)
        self.stats["_sent_any"] = True
        self.stats["sent_first" if kind == "first" else "sent_followup"] += 1
        self.stats["sent_by_kind"][lead.get("email_kind") or "work"] += 1
        self.stats["sent"].append({"name": lead["name"], "company": lead.get("company"), "kind": kind,
                                   "subject": content["subject"], "email": to_email})
        return True

    def close(self):
        if self.server is not None:
            try:
                self.server.quit()
            except Exception:
                pass


def bounce_gate(conn, inbox, now, settings):
    """(7-day bounce rate, pause reason or None). Counts only bounces you reported with `stop --reason bounced`."""
    since = max((now - timedelta(days=7)).strftime(ISO), inbox.get("resumed_at") or "")
    sent, bounced = store.inbox_bounces(conn, inbox["id"], since)
    rate = bounced / sent if sent else None
    if bounced >= 3 or (sent >= settings.bounce_min_sent and rate and rate * 100 > settings.bounce_pause_pct):
        return rate, f"{bounced} bounces reported in 7 days out of {sent} sent"
    return rate, None


def stopped_rows(conn, since):
    return [{"email": e["value"], "name": e.get("name"), "company": e.get("company"), "reason": e["kind"],
             "at": e["created_at"]} for e in store.events_since(conn, since) if e["kind"] in store.STOP_REASONS]


def run_command(conn, settings, today, env, now=None, sleep=time.sleep, connect_smtp=mail.smtp_connect,
                drafts_dir=DRAFTS_DIR):
    started = time.time()
    now = now or datetime.now(timezone.utc)
    run_id = store.start_run(conn, "run", today)
    since = store.previous_run_start(conn, "run", run_id) or (now - timedelta(days=1)).strftime(ISO)
    inboxes = store.list_inboxes(conn)
    settings.own_domains |= {store.email_domain(i["address"]) for i in inboxes}
    stats = {"mode": settings.mode, "sent_first": 0, "sent_followup": 0, "drafts_written": 0, "drafts": [],
             "sent": [], "inboxes": [], "paused_inboxes": [], "skipped_reasons": Counter(),
             "sent_by_kind": Counter(), "errors": [], "weekend": False, "send_failed": False, "_sent_any": False}
    leads, history, suppressed, latest, eligible, skipped = queue_rows(conn, settings, now)
    stats["eligible_pool"] = len(eligible)
    stats["skipped_reasons"].update(skipped)
    queue = list(eligible)
    drafted_leads, drafted_threads = store.drafted_today(conn, today) if settings.mode == "review" else (set(), set())
    queue = [l for l in queue if l["id"] not in drafted_leads]
    weekend = now.astimezone(ZoneInfo(settings.send_tz)).weekday() >= 5

    for inbox in inboxes:
        row = {"address": inbox["address"], "status": inbox["status"], "warmup_day": None, "cap_today": 0,
               "used_today": store.used_today(conn, inbox["id"], today), "bounce_rate_7d": None}
        stats["inboxes"].append(row)
        rate, pause_reason = bounce_gate(conn, inbox, now, settings)
        row["bounce_rate_7d"] = rate
        if pause_reason and inbox["status"] != "paused":
            store.set_inbox_status(conn, inbox["id"], "paused", pause_reason)
            inbox = {**inbox, "status": "paused", "paused_reason": pause_reason}
            row["status"] = "paused"
        if inbox["status"] == "paused":
            stats["paused_inboxes"].append({"address": inbox["address"], "reason": inbox["paused_reason"]})
            continue
        if settings.mode == "off":
            continue
        if settings.mode == "send" and inbox["status"] != "ready":
            stats["skipped_reasons"]["inbox_not_ready"] += 1
            continue
        row["cap_today"], row["warmup_day"] = warmup_cap(inbox, today, settings.warmup)
        remaining = row["cap_today"] - row["used_today"]
        if remaining <= 0:
            continue
        if settings.mode == "send" and weekend:
            stats["weekend"] = True
            continue
        delivery = Delivery(conn, inbox, settings, today, env, connect_smtp, drafts_dir, sleep, stats)
        try:
            for parent in store.followups_due(conn, inbox["id"], today):
                if remaining <= 0:
                    break
                if parent["id"] in drafted_threads:
                    continue
                if delivery.followup(parent):
                    remaining -= 1
            while remaining > 0 and queue:
                lead = queue.pop(0)
                reason = skip_reason(lead, history, store.suppressed_values(conn), latest, settings)
                if reason:
                    stats["skipped_reasons"][reason] += 1
                    continue
                if delivery.first(lead):
                    remaining -= 1
                    if not delivery.review:
                        history.add(lead)
        except SendFailed as exc:
            stats["send_failed"] = True
            stats["errors"].append(f"{inbox['address']}: SMTP send failed, nothing more from this inbox today ({exc})")
        finally:
            delivery.close()
        row["used_today"] = store.used_today(conn, inbox["id"], today)

    tomorrow = (date.fromisoformat(today) + timedelta(days=1)).isoformat()
    stats["stopped_today"] = stopped_rows(conn, since)
    stats["replies"] = [s for s in stats["stopped_today"] if s["reason"] == "replied"]
    stats["followups_next"] = [{"name": m["name"], "company": m["company"], "email": m["to_email"],
                                "first_sent": m["day"], "due": m["followup_due"]}
                               for m in store.followups_upcoming(conn, tomorrow)]
    stats["periods"] = store.period_stats(conn, today)
    stats["unknown_state"] = store.stuck_sending(conn)
    if stats["unknown_state"]:
        stats["errors"].append(f"{stats['unknown_state']} message(s) stuck in 'sending' after a crash; they count as "
                               "sent and are never retried")
    if not inboxes and settings.mode != "off":
        stats["errors"].append("no sending inbox yet: python scripts/outreach.py add-inbox --address ... "
                               "--display-name ...")
    stats["companies_in_pool"] = len({l["company_key"] for l in eligible})
    stats["duration_s"] = round(time.time() - started, 1)
    stats.pop("_sent_any")
    stats["skipped_reasons"] = dict(stats["skipped_reasons"])
    stats["sent_by_kind"] = dict(stats["sent_by_kind"])
    store.finish_run(conn, run_id, {k: v for k, v in stats.items() if k != "periods"})
    return stats


# ============================================================
# MANUAL COMMANDS
# ============================================================

def stop_command(conn, values, reason, note, today):
    """Cancel follow-ups and block addresses for good. '@domain' blocks a whole company."""
    lines = []
    for raw in values:
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        if value.startswith("@"):
            domain = value[1:].lower()
            threads = store.cancel_followups(conn, domain=domain)
            store.suppress(conn, f"@{domain}", reason, note)
            store.add_event(conn, reason, f"@{domain}", today, note=note)
            lines.append(f"stopped @{domain}: nobody there is emailed again; {len(threads)} follow-up(s) cancelled")
            continue
        key = store.email_key(value)
        if not key:
            lines.append(f"skipped {value}: not an email address")
            continue
        first = store.latest_first_email(conn, key)
        matches = store.find_leads_by_address(conn, key)
        lead = store.get_lead(conn, first["lead_id"]) if first else (matches[0] if matches else None)
        threads = store.cancel_followups(conn, key=key)
        store.suppress(conn, key, reason, note)
        for other in {c.get("email_key") for c in (lead or {}).get("candidate_emails") or []} | {
                (lead or {}).get("email_key")}:
            if other and other != key:
                store.suppress(conn, other, reason, f"same person as {key}")
        store.add_event(conn, reason, key, today, lead_id=lead["id"] if lead else None,
                        message_pk=first["id"] if first else None, note=note)
        who = f" ({lead['name']}, {lead.get('company')})" if lead else ""
        if threads:
            lines.append(f"stopped {value}{who}: follow-up due {threads[0]['followup_due']} cancelled; "
                         "never emailed again")
        elif first:
            lines.append(f"stopped {value}{who}: no follow-up was pending; never emailed again")
        else:
            lines.append(f"stopped {value}{who}: not emailed yet, and now it never will be")
    return lines


def unstop_command(conn, values, today):
    lines = []
    for raw in values:
        value = raw.strip()
        if not value:
            continue
        key = f"@{value[1:].lower()}" if value.startswith("@") else store.email_key(value)
        if not key:
            lines.append(f"skipped {value}: not an email address")
            continue
        removed = store.unsuppress(conn, key)
        conn.execute("DELETE FROM suppression WHERE note = ?", (f"same person as {key}",))
        if removed:
            store.add_event(conn, "unstopped", key, today)
            lines.append(f"unstopped {value}: may be emailed again (a cancelled follow-up is not restored)")
        else:
            lines.append(f"{value} was not stopped")
    return lines


def read_values(values, path):
    items = list(values or [])
    if path:
        with open(path, encoding="utf-8") as f:
            items += [line.strip() for line in f]
    return [v for v in items if v and not v.startswith("#")]


def text_table(headers, rows):
    cells = [[str(v if v is not None else "-") for v in row] for row in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]
    line = lambda vals: "  ".join(v.ljust(w) for v, w in zip(vals, widths)).rstrip()
    return "\n".join([line(headers), line(["-" * w for w in widths])] + [line(r) for r in cells])


def status_command(conn, settings, today, show_stopped=False, now=None):
    now = now or datetime.now(timezone.utc)
    out = [f"Mode: {settings.mode}"]
    inboxes = store.list_inboxes(conn)
    if inboxes:
        rows = []
        for i in inboxes:
            cap, day = warmup_cap(i, today, settings.warmup)
            rows.append([i["address"], i["status"], day, cap, store.used_today(conn, i["id"], today),
                         i["paused_reason"] or ""])
        out += ["", text_table(["Inbox", "Status", "Warm-up day", "Cap", "Used today", "Note"], rows)]
    else:
        out.append("No sending inbox yet (add-inbox).")
    leads, history, suppressed, latest, eligible, skipped = queue_rows(conn, settings, now)
    unresolved = len(topup_candidates(leads, history, suppressed, latest, settings))
    companies = len({l["company_key"] for l in leads})
    out += ["", f"Queue: {len(leads)} people at {companies} compan{'y' if companies == 1 else 'ies'}; "
                f"{len(eligible)} ready to email (one per company); {unresolved} waiting for verification credits."]
    if skipped:
        out.append("Not emailable now: " + ", ".join(f"{k} {v}" for k, v in skipped.most_common()))
    week = (date.fromisoformat(today) + timedelta(days=7)).isoformat()
    due = store.followups_upcoming(conn, week)
    out += ["", f"Follow-ups due in the next 7 days: {len(due)}"]
    if due:
        out.append(text_table(["Due", "Name", "Company", "Email"],
                              [[m["followup_due"], m["name"], m["company"], m["to_email"]] for m in due]))
    periods = store.period_stats(conn, today)
    metrics = [("first_sent", "First emails"), ("followup_sent", "Follow-ups"), ("replies", "Replies reported"),
               ("reply_rate", "Reply rate"), ("bounces", "Bounces reported"), ("unsubscribes", "Unsubscribes"),
               ("companies_contacted", "Companies")]
    cols = ["today", "week", "month", "ytd"]
    fmt = lambda k, v: "-" if v is None else (f"{v * 100:.1f}%" if k.endswith("_rate") else str(v))
    out += ["", text_table(["Totals", "Today", "Last 7 days", "Month", "Year"],
                           [[label] + [fmt(k, periods[c][k]) for c in cols] for k, label in metrics])]
    if show_stopped:
        rows = [[s["value"], s["reason"], s["created_at"][:10], s["note"] or ""] for s in store.suppression_list(conn)]
        out += ["", f"Stopped: {len(rows)}", text_table(["Address", "Reason", "Since", "Note"], rows) if rows else ""]
    return "\n".join(out)


# ============================================================
# CLI
# ============================================================

@contextmanager
def run_lock(path=LOCK_FILE):
    """ingest, topup and run never overlap; stop/unstop/status do not take the lock and work mid-run."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_stats(path, stats):
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, default=str)
    os.replace(tmp, path)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=store.DB_FILE, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="load the day's leads into the queue (daily_run step)")
    p.add_argument("--leads", default=LEADS_JSON)
    p.add_argument("--jobs", default=JOBS_FILE)
    p.add_argument("--today")
    p.add_argument("--stats-out")
    p.add_argument("--queue-csv")

    for name, text in (("topup", "spend finder and verifier credits on the next leads in line (daily_run step)"),
                       ("run", "send or draft today's follow-ups and first emails (daily_run step)")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--today")
        p.add_argument("--stats-out")

    p = sub.add_parser("stop", help="cancel follow-ups and never email these addresses again")
    p.add_argument("emails", nargs="*", help="addresses, or @domain.com for a whole company")
    p.add_argument("--file", help="a text file with one address per line")
    p.add_argument("--reason", choices=store.STOP_REASONS, default="replied")
    p.add_argument("--note")

    p = sub.add_parser("unstop", help="undo a stop")
    p.add_argument("emails", nargs="+")

    p = sub.add_parser("status", help="inboxes, queue, follow-ups due and totals")
    p.add_argument("--stopped", action="store_true", help="also list every stopped address")

    p = sub.add_parser("add-inbox", help="register a sending inbox (starts as pending)")
    p.add_argument("--address", required=True)
    p.add_argument("--display-name", required=True)
    p.add_argument("--host", help="default SMTP_HOST, else smtp.zoho.com")
    p.add_argument("--port", type=int, help="default SMTP_PORT, else 465")
    p.add_argument("--username", help="default: the address")
    p.add_argument("--pass-env", default="SMTP_PASS", help=".env key holding the password")
    p.add_argument("--no-check", action="store_true", help="skip the SMTP login test")

    sub.add_parser("list-inboxes", help="list sending inboxes")
    p = sub.add_parser("pause", help="stop sending from an inbox")
    p.add_argument("address")
    p.add_argument("--reason", default="paused by hand")
    p = sub.add_parser("resume", help="send from a paused inbox again")
    p.add_argument("address")
    p.add_argument("--restart-warmup", action="store_true", help="start the 5/10/20 ramp again")
    return parser


def main(argv=None):
    load_dotenv(ENV_FILE)
    args = build_parser().parse_args(argv)
    settings = load_settings(os.environ)
    today = getattr(args, "today", None) or local_today(settings)

    with store.connect(args.db) as conn:
        if args.command in ("ingest", "topup", "run"):
            with run_lock() as locked:
                if not locked:
                    print("another outreach step is running; try again later")
                    return EXIT_LOCKED
                if args.command == "ingest":
                    stats = ingest_command(conn, read_json(args.leads, []), read_json(args.jobs, []),
                                           read_json(METADATA_FILE, {}), today, settings, queue_csv=args.queue_csv)
                    print(f"ingest: {stats['new']} new, {stats['updated']} updated; {stats['queue']} people in the "
                          f"queue, {stats['sendable']} companies ready, {stats['unresolved']} waiting for credits")
                elif args.command == "topup":
                    providers = build_providers(QuotaTracker(), None)
                    stats = topup_command(conn, settings, today, providers)
                    print(f"topup: {json.dumps(stats, default=str)}")
                else:
                    stats = run_command(conn, settings, today, os.environ)
                    print(f"run ({settings.mode}): {stats['sent_first']} first emails, {stats['sent_followup']} "
                          f"follow-ups, {stats['drafts_written']} drafts")
                    for error in stats["errors"]:
                        print(f"  {error}")
                write_stats(args.stats_out, stats)
                return 1 if stats.get("send_failed") else 0

        if args.command == "stop":
            values = read_values(args.emails, args.file)
            if not values:
                print("give at least one address, @domain or --file")
                return 2
            print("\n".join(stop_command(conn, values, args.reason, args.note, today)))
        elif args.command == "unstop":
            print("\n".join(unstop_command(conn, args.emails, today)))
        elif args.command == "status":
            print(status_command(conn, settings, today, args.stopped))
        elif args.command == "add-inbox":
            host = args.host or os.environ.get("SMTP_HOST") or "smtp.zoho.com"
            port = args.port or int(os.environ.get("SMTP_PORT") or 465)
            try:
                store.add_inbox(conn, args.address, args.display_name, host, port, args.username or args.address,
                                args.pass_env)
            except sqlite3.IntegrityError:
                print(f"{args.address} is already registered")
                return 1
            if not args.no_check:
                try:
                    mail.smtp_connect(store.get_inbox(conn, args.address), os.environ).quit()
                    print("SMTP login: ok")
                except Exception as exc:
                    print(f"SMTP login failed: {exc.__class__.__name__}: {exc}")
            print(f"added {args.address} as pending: it drafts in review mode and sends only once the inbox "
                  "checks mark it ready")
        elif args.command == "list-inboxes":
            rows = [[i["address"], i["display_name"], f"{i['smtp_host']}:{i['smtp_port']}", i["status"],
                     (i["warmup_started_at"] or "")[:10], i["paused_reason"] or ""] for i in store.list_inboxes(conn)]
            print(text_table(["Address", "Name", "SMTP", "Status", "Warm-up since", "Note"], rows) if rows
                  else "no inboxes")
        elif args.command in ("pause", "resume"):
            inbox = store.get_inbox(conn, args.address)
            if not inbox:
                print(f"no inbox {args.address}")
                return 1
            if args.command == "pause":
                store.set_inbox_status(conn, inbox["id"], "paused", args.reason)
                print(f"paused {args.address}")
            else:
                status = "ready" if inbox["warmup_started_at"] else "pending"
                store.set_inbox_status(conn, inbox["id"], status)
                if args.restart_warmup and status == "ready":
                    store.restart_warmup(conn, inbox["id"])
                print(f"{args.address} is {status} again")
    return 0


if __name__ == "__main__":
    sys.exit(main())
