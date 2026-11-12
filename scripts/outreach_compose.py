#!/usr/bin/env python3
"""
Outreach copy. compose() returns the subject and body only; full_text() adds the signature and the
opt-out line, so no composer can drop the compliance text. The copy is the approved Stage 4 set:
one posting, one person from the roster who fits it (outreach_match.py), one easy reply.
Every message passes lint_copy() and lint_message() before it is sent or drafted.
"""

import re
from collections import Counter
from email.utils import parseaddr

from outreach_match import SENIOR_YEARS

BUCKET_ORDER = ["technicalDebt", "hardSecurity", "highScale", "dataIntegrity", "manualArchitecture"]
# what the follow-up says the work involves; it reads after "the work involves"
PAIN_PHRASES = {
    "technicalDebt": "reworking systems you already have",
    "hardSecurity": "getting security and reliability right",
    "highScale": "systems that have to stay fast under load",
    "dataIntegrity": "data that has to stay correct",
    "manualArchitecture": "backend systems with a lot of moving parts",
}
# a design or QA posting can trip these buckets, and the sentence would be wrong for those roles
PAIN_FAMILIES = {"engineering", "mobile", "devops"}
# the rest of technicalDebt ("zero to one", "production-ready") is about building new things, not reworking
STRONG_DEBT_TERMS = {"technical debt", "trail of technical debt", "legacy monoliths", "modernize legacy", "migration",
                     "overhaul", "completely overhaul", "aggressively refactor", "refactor aggressively",
                     "spaghetti code", "code stagnation", "duct-tape fix", "rebuild the prototype",
                     "rewrite the prototype", "rewrite that buys", "unstable prototype"}
WEAK_TERMS = {
    "dataIntegrity": {"background jobs system", "database connections", "observability and logging",
                      "persistent connections", "real-world datasets", "state updates", "structured extraction",
                      "unstructured sources", "workflow automation", "workflow integrations"},
    "hardSecurity": {"asynchronous updates", "dedicated api routes", "edge optimization"},
    "highScale": {"state machines", "concurrent test suites"},
}
OFFER_LINES = {
    "skills": "We have {a} {role} with {years} of experience, including {skills} from your posting.",
    "qa": "We have {a} {role} with {years} of {skills}, which your posting asks for.",
    "plain": "We have {a} {role} with {years} of experience.",
    "pm": "We have {a} {role} with {years} of experience and multiple certifications.",
}
SLUG_SUFFIXES = {"ai": "AI", "hq": "HQ", "labs": "Labs"}
# startup domains like getzuma.com: the company is Zuma
LAUNCH_PREFIXES = ("get", "try", "use", "join", "hey", "hello", "meet", "with", "go")
# 1 to 3 capitalised words starting at every word, overlapping
NAME_SPANS_RE = re.compile(r"(?=(\b[A-Z][\w&.'-]*(?:[ \t]+[A-Z0-9][\w&.'-]*){0,2}))")
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


def soften_caps(text):
    """OPENLANE -> Openlane: the subject check blocks all-caps words of five letters or more."""
    return re.sub(r"(?<![A-Za-z])[A-Z]{5,}(?![A-Za-z])", lambda m: m.group(0).title(), text or "")


def _letters(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def name_from_posting(name, text):
    """The company as its own posting writes it: a phrase whose letters spell the slug ('Reflection AI' for
    reflectionai), or the slug without a launch prefix if the posting uses that at least twice (Zuma)."""
    key = _letters(name or "")
    if not key or not text:
        return None
    stripped = {key[len(p):] for p in LAUNCH_PREFIXES if key.startswith(p) and len(key) - len(p) >= 3}
    full, short = Counter(), Counter()
    for span in NAME_SPANS_RE.findall(text):
        words = span.split()
        for n in range(len(words), 0, -1):
            phrase = " ".join(words[:n]).rstrip(".,'")
            letters = _letters(phrase)
            if letters == key:
                full[phrase] += 1
            elif letters in stripped:
                short[phrase] += 1
    if full:
        return full.most_common(1)[0][0]
    if short and short.most_common(1)[0][1] >= 2:
        return short.most_common(1)[0][0]
    return None


def display_company(name, text=""):
    """The name as a person writes it: from the posting when it names itself, else a long slug-made name like
    'Reflectionai' becomes 'Reflection AI'; short ones ('Bonsai', 'Shanghai') are left alone."""
    from_posting = name_from_posting(name, text)
    if from_posting:
        return soften_caps(from_posting)
    name = (name or "").strip()
    match = re.fullmatch(r"([A-Za-z][a-z0-9]{3,}?)(ai|hq|labs)", name)
    if match and (match.group(2) != "ai" or len(name) >= 9):
        name = f"{match.group(1)[0].upper()}{match.group(1)[1:]} {SLUG_SUFFIXES[match.group(2)]}"
    return soften_caps(name)


def pain_bucket(job, family):
    """The first bucket the posting hits with a strong phrase; None outside engineering and DevOps."""
    if family not in PAIN_FAMILIES:
        return None
    text = (job.get("excerpt") or "").lower()
    for bucket in BUCKET_ORDER:
        terms = {t.lower() for t in (job.get("terms") or {}).get(bucket) or []}
        if bucket == "technicalDebt":
            terms &= STRONG_DEBT_TERMS
        elif bucket == "manualArchitecture":
            if "transaction" not in text:
                terms.discard("acid")  # "nucleic acid" in biotech postings
        else:
            terms -= WEAK_TERMS.get(bucket, set())
        if terms:
            return bucket
    return None


def join_words(items):
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def offer_line(fit):
    role = fit["role"]
    # "senior" only from five years up
    article = "a senior" if fit["years"] >= SENIOR_YEARS else ("an" if role[:1].lower() in "aeio" else "a")
    return OFFER_LINES[fit["line"]].format(a=article, role=role, years=fit["years_text"],
                                           skills=join_words(fit["skills"]) if fit["skills"] else "")


def compose(lead, job, inbox, kind, settings, parent_subject=None, fit=None, parent_body=None):
    """lead: the queue row; job: the posting; fit: outreach_match.best_fit() for it. A follow-up takes the
    title and the role from the first email, so it repeats what was said even if the roster changed."""
    first = first_name(lead.get("name"))
    title = soften_caps(short_title(job.get("title")))
    if kind == "followup":
        said = re.fullmatch(r"Your (.+) role", parent_subject or "")
        offered = re.search(r"We have an? (?:senior )?(.+?) with \d", parent_body or "")
        role = offered.group(1) if offered else (fit or {}).get("role") or "person"
        same_job = not said or said.group(1) == title
        pain = pain_bucket(job, (fit or {}).get("family")) if same_job else None
        pain_sentence = (f" From the posting, the work involves {PAIN_PHRASES[pain]}, which is hard to put on hold."
                         if pain else "")
        return {
            "subject": f"Re: {parent_subject}",
            "body": (f"Hi {first},\n\n"
                     f"One more thought on the {said.group(1) if said else title} role.{pain_sentence}\n\n"
                     "When a senior seat sits empty, the rest of the team picks up its work. "
                     f"The {role} I mentioned can take some of that on while you keep looking for the right hire, "
                     "so you do not have to rush it or settle.\n\n"
                     "Reply \"CV\" and I will send it over."),
        }
    if not fit:
        raise ValueError("a first email needs a roster fit")
    company = display_company(lead.get("company"), job.get("excerpt")) or "your company"
    return {
        "subject": f"Your {title} role",
        "body": (f"Hi {first},\n\n"
                 f"I saw the {title} role at {company}.\n\n"
                 f"{offer_line(fit)} They can join your team through us.\n\n"
                 "You skip the search. Read one CV, talk with them if it looks right, then decide.\n\n"
                 f"I run {settings.agency_name}, a software agency in Karachi.\n\n"
                 "Want me to send the CV?"),
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
