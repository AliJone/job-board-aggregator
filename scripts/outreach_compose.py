#!/usr/bin/env python3
"""
Outreach copy. compose() returns the subject and body only; full_text() adds the signature and the
opt-out line, so no composer can drop the compliance text. The v1 template is a placeholder for
review mode and tests; the Hormozi-grounded copy (Stage 4) replaces it before OUTREACH_MODE=send.
Every message passes lint_copy() and lint_message() before it is sent or drafted.
"""

import re
from email.utils import parseaddr

BUCKET_ORDER = ["technicalDebt", "hardSecurity", "highScale", "dataIntegrity", "manualArchitecture"]
BUCKET_PHRASES = {
    "technicalDebt": "moving off an older stack",
    "hardSecurity": "security work that has to be right the first time",
    "highScale": "systems that have to hold up at scale",
    "dataIntegrity": "data that has to stay correct",
    "manualArchitecture": "replacing manual processes with software",
}
WORD_LIMITS = {"first": (50, 120), "followup": (20, 120)}
MAX_SUBJECT_WORDS = 6

SPAM_PHRASES = ["for free", "free trial", "free consultation", "guarantee", "guaranteed", "act now", "limited time",
                "no obligation", "risk-free", "risk free", "100%", "$$$", "click here", "urgent", "winner",
                "earn money", "best price", "cash"]
# from outreach-suite's checks.js
BANNED_WORDS = ["delve", "delves", "delving", "delved", "testament", "unwavering", "navigating", "navigate",
                "tapestry", "game-changer", "game changer", "synergy", "I hope this message finds you well"]
GENERIC_SUBJECT_RES = [re.compile(p, re.I) for p in (
    r"\bconnect(?:ing|ion)?\b", r"\bintro(?:duction)?\b", r"\breaching out\b", r"\bquick (?:note|question|chat)\b",
    r"\bpartnership\b", r"\bopportunit\w*\b", r"^(?:hi|hello|hey)\b")]
DASH_RE = re.compile(r"[—–]|(?<!-)--(?!-)")
URL_RE = re.compile(r"https?://|www\.", re.I)
PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")
ALLOWED_HTML_TAGS = {"p", "br"}


def first_name(name):
    parts = (name or "").split()
    return parts[0].title() if parts else "there"


def short_title(title, max_words=4):
    title = re.split(r"[,(\[|/]| - ", title or "", maxsplit=1)[0].strip()
    words = title.split()
    return " ".join(words[:max_words]) or "engineering"


def top_bucket(job):
    buckets = job.get("buckets") or []
    return next((b for b in BUCKET_ORDER if b in buckets), None)


def compose(lead, job, inbox, kind, settings, parent_subject=None):
    """Template v1. lead: the queue row; job: the best entry of lead['jobs_snapshot'] (may be empty)."""
    first = first_name(lead.get("name"))
    company = lead.get("company") or "your team"
    title = short_title(job.get("title"))
    if kind == "followup":
        return {
            "subject": f"Re: {parent_subject}",
            "body": (f"Hi {first},\n\n"
                     f"Circling back on the {title} role at {company}. If it is still open, I can send the two "
                     "profiles today, and you can decide in five minutes whether either is worth a call."),
        }
    phrase = BUCKET_PHRASES.get(top_bucket(job))
    mentions = f", and the posting mentions {phrase}" if phrase else ""
    return {
        "subject": f"{title} candidates",
        "body": (f"Hi {first},\n\n"
                 f"I saw {company} is hiring for the {title} role{mentions}.\n\n"
                 f"I run {settings.agency_name}, and we place senior engineers who have done this kind of work at "
                 "other companies. I have two people in mind whose track records match what the posting asks for.\n\n"
                 "Would it help if I sent over their profiles this week?"),
    }


def full_text(body, settings, inbox):
    lines = [settings.sender_name or inbox["display_name"], settings.agency_name, settings.sender_address_line]
    signature = "\n".join(line for line in lines if line)
    unsubscribe = settings.unsubscribe_address or inbox["address"]
    return (f"{body.rstrip()}\n\n-- \n{signature}\n"
            f"Not the right person, or rather not hear from me? Reply \"unsubscribe\" or write to {unsubscribe} "
            "and I will not email you again.\n")


def _words(text):
    return len((text or "").split())


def lint_copy(subject, body, kind):
    """(level, issue) pairs for the copy before the signature; any 'error' blocks the message."""
    issues = []

    def error(text):
        issues.append(("error", text))

    if PLACEHOLDER_RE.search(subject) or PLACEHOLDER_RE.search(body):
        error("unfilled {placeholder}")
    low, high = WORD_LIMITS[kind]
    if not low <= _words(body) <= high:
        error(f"body is {_words(body)} words; {kind} emails need {low}-{high}")
    if _words(subject) > MAX_SUBJECT_WORDS + (1 if kind == "followup" else 0):
        error(f"subject is longer than {MAX_SUBJECT_WORDS} words")
    if "!" in subject:
        error("exclamation mark in the subject")
    if any(len(w) >= 5 and w.isalpha() and w.isupper() for w in re.findall(r"[A-Za-z]+", subject)):
        error("all-caps word in the subject")
    if kind == "first" and subject.lower().startswith("re:"):
        error("first email subject starts with Re:")
    if kind == "followup" and not subject.startswith("Re: "):
        error("follow-up subject must start with 'Re: '")
    if len(URL_RE.findall(body)) > 1:
        error("more than one link")
    text = f"{subject}\n{body}".lower()
    for phrase in SPAM_PHRASES:
        if re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text):
            error(f"spam trigger: {phrase}")
    for phrase in BANNED_WORDS:
        if re.search(rf"(?<![a-z0-9]){re.escape(phrase.lower())}(?![a-z0-9])", text):
            error(f"banned word: {phrase}")
    if DASH_RE.search(subject) or DASH_RE.search(body):
        error("em dash, en dash or double hyphen")
    if body.count("!") > 1:
        issues.append(("warn", "more than one exclamation mark"))
    if kind == "first" and any(rx.search(subject) for rx in GENERIC_SUBJECT_RES):
        issues.append(("warn", "generic subject"))
    return issues


def _text_parts(msg):
    return [p for p in msg.walk() if not p.is_multipart()]


def lint_message(msg, kind):
    """Errors in the finished message: headers, parts, HTML and compliance text."""
    errors = []
    if not parseaddr(msg.get("From", ""))[0]:
        errors.append("From has no display name")
    if not msg.get("Message-ID"):
        errors.append("no Message-ID")
    if not msg.get("List-Unsubscribe"):
        errors.append("no List-Unsubscribe header")
    if kind == "followup" and not (msg.get("In-Reply-To") and msg.get("References")):
        errors.append("follow-up is missing In-Reply-To/References")
    parts = _text_parts(msg)
    if [p.get_content_type() for p in parts] != ["text/plain", "text/html"]:
        errors.append("message must be exactly text/plain + text/html")
        return errors
    plain, markup = parts[0].get_content(), parts[1].get_content()
    if "\n-- \n" not in plain or "unsubscribe" not in plain.lower():
        errors.append("signature or opt-out line missing")
    tags = {t.lower() for t in re.findall(r"<\s*/?\s*([a-zA-Z0-9]+)", markup)}
    if tags - ALLOWED_HTML_TAGS:
        errors.append(f"HTML has tags other than p/br: {', '.join(sorted(tags - ALLOWED_HTML_TAGS))}")
    if len(URL_RE.findall(plain)) > 1:
        errors.append("more than one link")
    return errors
