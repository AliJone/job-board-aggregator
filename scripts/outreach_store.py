#!/usr/bin/env python3
"""
SQLite store for the outreach queue (scripts/output/outreach.db). The only module that knows SQL.

A lead is a person, not an address: the address can be found or replaced after discovery.
Duplicate protection sits here twice: contact_history() feeds the eligibility filter, and
partial unique indexes make the database itself refuse a second first email to the same
mailbox or person, or a second follow-up in one thread.
"""

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(SCRIPT_DIR, "output", "outreach.db")
SCHEMA_VERSION = 1
CONTACTED = ("sending", "sent")  # a crash mid-send leaves 'sending', which still counts as contacted
STOP_REASONS = ("replied", "unsubscribed", "bounced", "other")

SCHEMA = """
CREATE TABLE IF NOT EXISTS inboxes (
    id INTEGER PRIMARY KEY,
    address TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    smtp_host TEXT NOT NULL,
    smtp_port INTEGER NOT NULL,
    username TEXT NOT NULL,
    pass_env TEXT NOT NULL DEFAULT 'SMTP_PASS',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready', 'paused')),
    paused_reason TEXT,
    resumed_at TEXT,
    warmup_started_at TEXT,
    last_check_at TEXT,
    last_check_result TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    person_key TEXT NOT NULL UNIQUE,
    company_key TEXT NOT NULL,
    company TEXT,
    domain TEXT,
    company_pattern TEXT,
    company_pattern_source TEXT,
    company_score INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    title TEXT,
    seniority INTEGER NOT NULL DEFAULT -1,
    linkedin TEXT,
    linkedin_key TEXT,
    candidate_emails TEXT NOT NULL DEFAULT '[]',
    email TEXT,
    email_key TEXT,
    email_kind TEXT,
    email_confidence INTEGER NOT NULL DEFAULT 0,
    email_status TEXT,
    phone TEXT,
    phone_type TEXT,
    jobs_snapshot TEXT NOT NULL DEFAULT '[]',
    job_last_seen TEXT,
    resolve_attempted_at TEXT,
    resolve_note TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS leads_company ON leads(company_key);
CREATE INDEX IF NOT EXISTS leads_email ON leads(email_key);
CREATE INDEX IF NOT EXISTS leads_linkedin ON leads(linkedin_key);
CREATE INDEX IF NOT EXISTS leads_domain ON leads(domain);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL UNIQUE,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    inbox_id INTEGER NOT NULL REFERENCES inboxes(id),
    kind TEXT NOT NULL CHECK (kind IN ('first', 'followup')),
    parent_id INTEGER REFERENCES messages(id),
    to_email TEXT NOT NULL,
    email_key TEXT NOT NULL,
    person_key TEXT NOT NULL,
    company_key TEXT NOT NULL,
    domain TEXT,
    subject TEXT NOT NULL,
    body_text TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'sending', 'sent', 'failed')),
    day TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    followup_due TEXT,
    draft_path TEXT,
    error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_first_email_per_address ON messages(email_key)
    WHERE kind = 'first' AND status IN ('sending', 'sent');
CREATE UNIQUE INDEX IF NOT EXISTS one_first_email_per_person ON messages(person_key)
    WHERE kind = 'first' AND status IN ('sending', 'sent');
CREATE UNIQUE INDEX IF NOT EXISTS one_followup_per_thread ON messages(parent_id)
    WHERE kind = 'followup' AND status IN ('sending', 'sent');
CREATE INDEX IF NOT EXISTS messages_inbox_day ON messages(inbox_id, day, status);
CREATE INDEX IF NOT EXISTS messages_company ON messages(company_key, sent_at);
CREATE INDEX IF NOT EXISTS messages_domain ON messages(domain, sent_at);
CREATE INDEX IF NOT EXISTS messages_due ON messages(followup_due) WHERE followup_due IS NOT NULL;
CREATE TABLE IF NOT EXISTS suppression (
    value TEXT PRIMARY KEY,  -- a mailbox key, or '@domain' for a whole company
    reason TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,  -- replied | unsubscribed | bounced | other | unstopped
    value TEXT NOT NULL,
    lead_id INTEGER,
    message_id INTEGER,
    note TEXT,
    day TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    command TEXT NOT NULL,
    day TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats TEXT
);
"""

GMAIL_HOSTS = {"gmail.com", "googlemail.com"}
LINKEDIN_RE = re.compile(r"linkedin\.com/(?:in|pub)/([^/?#\s]+)", re.I)
JSON_FIELDS = ("candidate_emails", "jobs_snapshot")


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def email_key(email):
    """One key per mailbox: lowercase, no +tag, and no dots for Gmail, which ignores them."""
    email = (email or "").strip().lower()
    local, at, host = email.rpartition("@")
    if not at or not local or "." not in host or " " in email:
        return None
    local = local.split("+", 1)[0]
    if host in GMAIL_HOSTS:
        local, host = local.replace(".", ""), "gmail.com"
    return f"{local}@{host}" if local else None


def email_domain(email):
    return (email or "").rpartition("@")[2].strip().lower() or None


def linkedin_key(url):
    match = LINKEDIN_RE.search(url or "")
    return match.group(1).lower().rstrip("/") if match else None


# ============================================================
# CONNECTION
# ============================================================

@contextmanager
def connect(path=DB_FILE):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit; transaction() opens explicit ones
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    try:
        yield conn
    finally:
        conn.close()


def _migrate(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextmanager
def transaction(conn):
    """BEGIN IMMEDIATE takes the write lock up front, so a check and the insert it guards cannot interleave."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def backup(conn, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    dest = sqlite3.connect(path)
    try:
        conn.backup(dest)
    finally:
        dest.close()


def _row(row):
    if row is None:
        return None
    d = dict(row)
    for key in JSON_FIELDS:
        if key in d and isinstance(d[key], str):
            d[key] = json.loads(d[key] or "[]")
    return d


def _rows(cursor):
    return [_row(r) for r in cursor.fetchall()]


# ============================================================
# INBOXES
# ============================================================

def add_inbox(conn, address, display_name, smtp_host, smtp_port, username, pass_env="SMTP_PASS"):
    cur = conn.execute(
        "INSERT INTO inboxes (address, display_name, smtp_host, smtp_port, username, pass_env, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (address.strip().lower(), display_name, smtp_host, int(smtp_port), username, pass_env, utc_now()))
    return cur.lastrowid


def list_inboxes(conn):
    return _rows(conn.execute("SELECT * FROM inboxes ORDER BY id"))


def get_inbox(conn, address):
    return _row(conn.execute("SELECT * FROM inboxes WHERE address = ?", ((address or "").strip().lower(),)).fetchone())


def set_inbox_status(conn, inbox_id, status, reason=None):
    now = utc_now()
    current = _row(conn.execute("SELECT * FROM inboxes WHERE id = ?", (inbox_id,)).fetchone())
    fields = {"status": status, "paused_reason": reason if status == "paused" else None}
    if status == "ready" and not current["warmup_started_at"]:
        fields["warmup_started_at"] = now
    if status == "ready" and current["status"] == "paused":
        fields["resumed_at"] = now
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE inboxes SET {sets} WHERE id = ?", (*fields.values(), inbox_id))


def restart_warmup(conn, inbox_id):
    conn.execute("UPDATE inboxes SET warmup_started_at = ? WHERE id = ?", (utc_now(), inbox_id))


# ============================================================
# LEADS
# ============================================================

LEAD_COLUMNS = ("company_key", "company", "domain", "company_pattern", "company_pattern_source", "company_score",
                "name", "name_key", "title", "seniority", "linkedin", "linkedin_key", "candidate_emails", "email",
                "email_key", "email_kind", "email_confidence", "email_status", "phone", "phone_type",
                "jobs_snapshot", "job_last_seen")
ADDRESS_COLUMNS = ("email", "email_key", "email_kind", "email_confidence", "email_status")


def get_lead(conn, lead_id):
    return _row(conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone())


def all_leads(conn):
    return _rows(conn.execute("SELECT * FROM leads ORDER BY id"))


def upsert_lead(conn, person_key, fields, keep_at_or_above):
    """Insert or refresh a lead. Returns 'new' or 'updated'. A known address at keep_at_or_above or
    better is never replaced by a weaker one, so re-discovery cannot undo a verification."""
    fields = dict(fields)
    existing = _row(conn.execute("SELECT * FROM leads WHERE person_key = ?", (person_key,)).fetchone())
    now = utc_now()
    if existing:
        if (existing["email_confidence"] >= keep_at_or_above
                and (fields.get("email_confidence") or 0) < existing["email_confidence"]):
            for key in ADDRESS_COLUMNS:
                fields[key] = existing[key]
        seen = {c.get("email_key") for c in fields.get("candidate_emails") or []}
        fields["candidate_emails"] = list(fields.get("candidate_emails") or []) + [
            c for c in existing["candidate_emails"] if c.get("email_key") not in seen]
        if not fields.get("phone") and existing["phone"]:
            fields["phone"], fields["phone_type"] = existing["phone"], existing["phone_type"]
        if not fields.get("job_last_seen"):
            fields["job_last_seen"] = existing["job_last_seen"]
        if not fields.get("jobs_snapshot"):
            fields["jobs_snapshot"] = existing["jobs_snapshot"]
    values = [json.dumps(fields.get(c)) if c in JSON_FIELDS else fields.get(c) for c in LEAD_COLUMNS]
    if existing:
        sets = ", ".join(f"{c} = ?" for c in LEAD_COLUMNS)
        conn.execute(f"UPDATE leads SET {sets}, last_seen = ? WHERE id = ?", (*values, now, existing["id"]))
        return "updated"
    cols = ", ".join(LEAD_COLUMNS)
    marks = ", ".join("?" for _ in LEAD_COLUMNS)
    conn.execute(f"INSERT INTO leads (person_key, {cols}, first_seen, last_seen) VALUES (?, {marks}, ?, ?)",
                 (person_key, *values, now, now))
    return "new"


def mark_job_seen(conn, company_keys, scrape_date):
    for key in company_keys:
        conn.execute("UPDATE leads SET job_last_seen = ? WHERE company_key = ?", (scrape_date, key))


def latest_scrape(conn):
    return conn.execute("SELECT MAX(job_last_seen) FROM leads").fetchone()[0]


def update_lead(conn, lead_id, **fields):
    values = [json.dumps(v) if k in JSON_FIELDS else v for k, v in fields.items()]
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE leads SET {sets} WHERE id = ?", (*values, lead_id))


def find_leads_by_address(conn, key):
    """Leads whose chosen or candidate address is this mailbox key."""
    rows = _rows(conn.execute("SELECT * FROM leads WHERE email_key = ? OR candidate_emails LIKE ?",
                              (key, f'%"{key}"%')))
    return [r for r in rows if r["email_key"] == key or any(c.get("email_key") == key for c in r["candidate_emails"])]


# ============================================================
# MESSAGES AND CONTACT HISTORY
# ============================================================

MESSAGE_COLUMNS = ("message_id", "lead_id", "inbox_id", "kind", "parent_id", "to_email", "email_key", "person_key",
                   "company_key", "domain", "subject", "body_text", "status", "day", "followup_due", "draft_path")


def insert_message(conn, **fields):
    """Raises sqlite3.IntegrityError when a unique index says this would be a duplicate."""
    cols = [c for c in MESSAGE_COLUMNS if c in fields]
    cur = conn.execute(f"INSERT INTO messages ({', '.join(cols)}, created_at) VALUES "
                       f"({', '.join('?' for _ in cols)}, ?)", (*[fields[c] for c in cols], utc_now()))
    return cur.lastrowid


def update_message(conn, message_pk, **fields):
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE messages SET {sets} WHERE id = ?", (*fields.values(), message_pk))


def get_message(conn, message_pk):
    return _row(conn.execute("SELECT * FROM messages WHERE id = ?", (message_pk,)).fetchone())


class ContactHistory:
    """Everyone already emailed, by every key that identifies them, plus companies still cooling down."""

    def __init__(self, email_keys=(), person_keys=(), linkedin_keys=(), name_domains=(), recent_companies=(),
                 recent_domains=()):
        self.email_keys = set(email_keys)
        self.person_keys = set(person_keys)
        self.linkedin_keys = set(linkedin_keys)
        self.name_domains = set(name_domains)
        self.recent_companies = set(recent_companies)
        self.recent_domains = set(recent_domains)

    def add(self, lead, domain=None):
        self.email_keys.add(lead["email_key"])
        self.person_keys.add(lead["person_key"])
        if lead.get("linkedin_key"):
            self.linkedin_keys.add(lead["linkedin_key"])
        domain = domain or lead.get("domain")
        if domain:
            self.name_domains.add((lead["name_key"], domain))
            self.recent_domains.add(domain)
        self.recent_companies.add(lead["company_key"])


def contact_history(conn, cooldown_since):
    rows = conn.execute(
        "SELECT m.email_key, m.person_key, m.company_key, m.domain, m.sent_at, m.created_at, l.linkedin_key, "
        "l.name_key FROM messages m JOIN leads l ON l.id = m.lead_id WHERE m.status IN ('sending', 'sent')").fetchall()
    history = ContactHistory()
    for r in rows:
        history.email_keys.add(r["email_key"])
        history.person_keys.add(r["person_key"])
        if r["linkedin_key"]:
            history.linkedin_keys.add(r["linkedin_key"])
        if r["domain"]:
            history.name_domains.add((r["name_key"], r["domain"]))
        if (r["sent_at"] or r["created_at"]) >= cooldown_since:
            history.recent_companies.add(r["company_key"])
            if r["domain"]:
                history.recent_domains.add(r["domain"])
    return history


def used_today(conn, inbox_id, day):
    """Drafts count toward the cap, so a review run shows exactly what a send run would do."""
    return conn.execute("SELECT COUNT(*) FROM messages WHERE inbox_id = ? AND day = ? AND status != 'failed'",
                        (inbox_id, day)).fetchone()[0]


THREAD_SELECT = (
    "SELECT m.*, l.name, l.title, l.company, l.jobs_snapshot, l.email_kind FROM messages m "
    "JOIN leads l ON l.id = m.lead_id "
    "WHERE m.kind = 'first' AND m.status = 'sent' AND m.followup_due IS NOT NULL AND m.followup_due <= ? "
    "AND NOT EXISTS (SELECT 1 FROM messages f WHERE f.parent_id = m.id AND f.kind = 'followup' "
    "AND f.status IN ('sending', 'sent'))")


def followups_due(conn, inbox_id, day):
    return _rows(conn.execute(THREAD_SELECT + " AND m.inbox_id = ? ORDER BY m.followup_due, m.sent_at",
                              (day, inbox_id)))


def followups_upcoming(conn, until_day):
    return _rows(conn.execute(THREAD_SELECT + " ORDER BY m.followup_due, m.sent_at", (until_day,)))


def drafted_today(conn, day):
    """Leads and threads already drafted today, so a second review run the same day adds nothing new."""
    rows = conn.execute("SELECT lead_id, parent_id FROM messages WHERE status = 'draft' AND day = ?", (day,)).fetchall()
    return {r[0] for r in rows}, {r[1] for r in rows if r[1]}


def followup_still_due(conn, parent_pk):
    row = conn.execute("SELECT followup_due FROM messages WHERE id = ?", (parent_pk,)).fetchone()
    return bool(row and row[0])


def cancel_followups(conn, key=None, domain=None):
    """Clears the follow-up on every first email to this mailbox (or domain). Returns the threads touched."""
    if key:
        where, args = "email_key = ?", (key,)
    else:
        where, args = "(domain = ? OR email_key LIKE ?)", (domain, f"%@{domain}")
    rows = _rows(conn.execute(f"SELECT * FROM messages WHERE kind = 'first' AND followup_due IS NOT NULL AND {where}",
                              args))
    conn.execute(f"UPDATE messages SET followup_due = NULL WHERE kind = 'first' AND {where}", args)
    return rows


def latest_first_email(conn, key):
    return _row(conn.execute("SELECT * FROM messages WHERE kind = 'first' AND email_key = ? AND status IN "
                             "('sending', 'sent') ORDER BY id DESC LIMIT 1", (key,)).fetchone())


def stuck_sending(conn):
    return conn.execute("SELECT COUNT(*) FROM messages WHERE status = 'sending'").fetchone()[0]


def inbox_bounces(conn, inbox_id, since):
    """(sent, bounced) for an inbox since a UTC timestamp. Bounces are the ones reported with
    `stop --reason bounced`."""
    sent = conn.execute("SELECT COUNT(*) FROM messages WHERE inbox_id = ? AND status = 'sent' AND sent_at >= ?",
                        (inbox_id, since)).fetchone()[0]
    bounced = conn.execute(
        "SELECT COUNT(DISTINCT e.id) FROM events e JOIN messages m ON m.id = e.message_id "
        "WHERE e.kind = 'bounced' AND m.inbox_id = ? AND e.created_at >= ?", (inbox_id, since)).fetchone()[0]
    return sent, bounced


# ============================================================
# SUPPRESSION AND EVENTS
# ============================================================

def suppress(conn, value, reason, note=None):
    conn.execute("INSERT OR REPLACE INTO suppression (value, reason, note, created_at) VALUES (?, ?, ?, ?)",
                 (value, reason, note, utc_now()))


def unsuppress(conn, value):
    return conn.execute("DELETE FROM suppression WHERE value = ?", (value,)).rowcount > 0


def suppressed_values(conn):
    return {r[0] for r in conn.execute("SELECT value FROM suppression")}


def suppression_list(conn):
    return _rows(conn.execute("SELECT * FROM suppression ORDER BY created_at DESC"))


def is_suppressed(suppressed, key, *domains):
    return key in suppressed or any(d and f"@{d}" in suppressed for d in domains)


def add_event(conn, kind, value, day, lead_id=None, message_pk=None, note=None):
    conn.execute("INSERT INTO events (kind, value, lead_id, message_id, note, day, created_at) VALUES "
                 "(?, ?, ?, ?, ?, ?, ?)", (kind, value, lead_id, message_pk, note, day, utc_now()))


def events_since(conn, since):
    return _rows(conn.execute(
        "SELECT e.*, l.name, l.company FROM events e LEFT JOIN leads l ON l.id = e.lead_id "
        "WHERE e.created_at >= ? ORDER BY e.id", (since,)))


# ============================================================
# RUNS AND TOTALS
# ============================================================

def start_run(conn, command, day):
    return conn.execute("INSERT INTO runs (command, day, started_at) VALUES (?, ?, ?)",
                        (command, day, utc_now())).lastrowid


def finish_run(conn, run_id, stats):
    conn.execute("UPDATE runs SET finished_at = ?, stats = ? WHERE id = ?", (utc_now(), json.dumps(stats), run_id))


def previous_run_start(conn, command, before_id):
    row = conn.execute("SELECT started_at FROM runs WHERE command = ? AND id < ? ORDER BY id DESC LIMIT 1",
                       (command, before_id)).fetchone()
    return row[0] if row else None


def period_stats(conn, today):
    """Today, last 7 days, month to date and year to date. Days are local (DAILY_TZ) dates."""
    today = date.fromisoformat(today) if isinstance(today, str) else today
    windows = {"today": today, "week": today - timedelta(days=6), "month": today.replace(day=1),
               "ytd": today.replace(month=1, day=1)}
    end = today.isoformat()
    out = {}
    for name, start in windows.items():
        args = (start.isoformat(), end)
        counts = dict(conn.execute("SELECT kind, COUNT(*) FROM messages WHERE status = 'sent' AND day BETWEEN ? AND ? "
                                   "GROUP BY kind", args).fetchall())
        # only stops tied to an email we sent count; stopping someone never emailed is not a reply
        events = dict(conn.execute("SELECT kind, COUNT(*) FROM events WHERE message_id IS NOT NULL AND day BETWEEN ? "
                                   "AND ? GROUP BY kind", args).fetchall())
        companies = conn.execute("SELECT COUNT(DISTINCT company_key) FROM messages WHERE status = 'sent' AND "
                                 "day BETWEEN ? AND ?", args).fetchone()[0]
        first, followup = counts.get("first", 0), counts.get("followup", 0)
        replies, bounces = events.get("replied", 0), events.get("bounced", 0)
        out[name] = {"first_sent": first, "followup_sent": followup, "replies": replies,
                     "reply_rate": replies / first if first else None, "bounces": bounces,
                     "bounce_rate": bounces / (first + followup) if first + followup else None,
                     "unsubscribes": events.get("unsubscribed", 0), "companies_contacted": companies}
    return out
