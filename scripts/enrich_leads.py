#!/usr/bin/env python3
"""
Lead enrichment step. Runs after scraper.py.

Reads scripts/output/all_jobs.json (sorted by lead_intent_score), groups jobs
by company, keeps the top-scoring companies and for each one finds the top
decision makers (CTO, founders, VP/Head of Engineering) with a direct email,
personal emails where a provider has them, and a phone number.

Sources, all free tier:
  - DuckDuckGo (and Brave, if keyed) for the company site and LinkedIn results
  - the company's own website for published addresses and phones
  - Apollo people search for names and titles at the domain
  - Hunter / Tomba domain search when search engines came up short
  - GitHub public profiles, Hunter / Tomba finders, Apollo enrichment for emails
  - ZeroBounce / Hunter / Tomba to verify anything below the confidence bar
  - OpenRouter (free model) once per company to filter candidates

Usage:
    cp .env.example .env   # then fill in the keys you have; every provider is optional
    python scripts/enrich_leads.py --top 40 --min-score 4 --people 3

Outputs scripts/output/leads.json and scripts/output/leads.csv.
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from collections import defaultdict
from html import unescape
from urllib.parse import urlparse

import requests

from lead_providers import (
    Apollo, BraveSearch, GitHub, Hunter, OpenRouterClient, QuotaTracker, Tomba, ZeroBounce,
    company_mentioned, company_tokens,
)

try:
    from ddgs import DDGS
except ImportError:  # pure helpers stay importable for tests without ddgs
    DDGS = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
JOBS_FILE = os.path.join(OUTPUT_DIR, "all_jobs.json")
LEADS_JSON = os.path.join(OUTPUT_DIR, "leads.json")
LEADS_CSV = os.path.join(OUTPUT_DIR, "leads.csv")
CACHE_FILE = os.path.join(OUTPUT_DIR, "enrich_cache.json")
ENV_FILE = os.path.join(ROOT_DIR, ".env")

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
]

# Confidence a source earns on its own; verification lifts anything to 95.
CONFIDENCE = {"verified": 95, "site": 90, "github": 85, "apollo": 80,
              "guess_confirmed": 60, "risky": 55, "guess_inferred": 40, "guess_default": 25}

SENIORITY_RULES = [
    (re.compile(r"\b(cto|chief technology|chief technical|chief information|cio|chief product|cpo|"
                r"ceo|chief executive|founder|co-?founder|owner|president)\b", re.I), 3),
    (re.compile(r"\b(vp|vice president|svp|evp)\b", re.I), 2),
    (re.compile(r"\b(head of|director)\b", re.I), 1),
    (re.compile(r"\b(engineering manager|principal engineer|staff engineer|architect|tech lead)\b", re.I), 0),
]
TECH_RE = re.compile(r"\b(engineering|engineer|technolog\w*|technical|software|platform|product|data|"
                     r"infrastructure|cto|cio|cpo|founder|ceo|architect|devops|security)\b", re.I)

# Hosts that can never be the company's own website.
NON_COMPANY_DOMAINS = {
    "linkedin.com", "glassdoor.com", "indeed.com", "crunchbase.com", "facebook.com",
    "twitter.com", "x.com", "instagram.com", "youtube.com", "wikipedia.org",
    "greenhouse.io", "lever.co", "ashbyhq.com", "bamboohr.com", "myworkdayjobs.com",
    "icims.com", "paylocity.com", "zoominfo.com", "bloomberg.com", "apollo.io",
    "rocketreach.co", "pitchbook.com", "g2.com", "ycombinator.com", "github.com",
    "medium.com", "builtin.com", "wellfound.com", "angel.co", "theorg.com",
    "signalhire.com", "contactout.com", "owler.com", "dnb.com", "duckduckgo.com",
}
SITE_PATHS = ["", "/contact", "/contact-us", "/about", "/about-us", "/team", "/leadership", "/company"]

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?)?\d{3,4}[\s.-]\d{3,4}(?:[\s.-]\d{2,4})?")
GENERIC_LOCAL_PARTS = {"info", "sales", "hello", "contact", "support", "careers", "jobs", "press",
                       "media", "hr", "admin", "team", "help", "office", "marketing", "privacy",
                       "legal", "security", "noreply", "no-reply", "partnerships", "billing"}

PATTERNS = {
    "first.last": lambda f, l: f"{f}.{l}",
    "flast": lambda f, l: f"{f[0]}{l}",
    "first": lambda f, l: f,
    "firstl": lambda f, l: f"{f}{l[0]}",
    "first_last": lambda f, l: f"{f}_{l}",
    "firstlast": lambda f, l: f"{f}{l}",
    "last.first": lambda f, l: f"{l}.{f}",
    "f.last": lambda f, l: f"{f[0]}.{l}",
    "first-last": lambda f, l: f"{f}-{l}",
    "lastf": lambda f, l: f"{l}{f[0]}",
    "last": lambda f, l: l,
}
GUESS_ORDER = ["first.last", "flast", "first", "firstl", "firstlast"]

CSV_FIELDS = ["company", "domain", "max_score", "job_count", "hiring_for", "name", "title", "linkedin",
              "email", "email_confidence", "email_status", "personal_emails", "phone", "phone_type",
              "company_phones", "company_emails", "job_urls"]


# ============================================================
# PURE HELPERS (covered by scripts/tests/test_enrich_leads.py)
# ============================================================

def humanize_company(raw, ats=""):
    """Turn an ATS slug into a searchable company name."""
    name = (raw or "").strip()
    if ats == "Workday" and "|" in name:
        name = name.split("|")[0]
    if re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", name):
        name = re.sub(r"[-_]+", " ", name).title()
    return name


def rank_companies(jobs, top=40, min_score=4):
    """Group jobs by company and rank by strongest lead signal."""
    groups = defaultdict(list)
    for job in jobs:
        key = job.get("company") or job.get("company_slug")
        if key:
            groups[key].append(job)

    ranked = []
    for key, items in groups.items():
        scores = [int(j.get("lead_intent_score") or 0) for j in items]
        best = max(scores)
        if best < min_score:
            continue
        items.sort(key=lambda j: -int(j.get("lead_intent_score") or 0))
        ranked.append({
            "company_key": key,
            "company": humanize_company(key, items[0].get("ats", "")),
            "ats": items[0].get("ats", ""),
            "max_score": best,
            "total_score": sum(scores),
            "job_count": len(items),
            "top_titles": [j.get("title") for j in items[:5]],
            "job_urls": [j.get("url") for j in items[:3] if j.get("url")],
        })

    ranked.sort(key=lambda c: (-c["max_score"], -c["total_score"], -c["job_count"], c["company"].lower()))
    return ranked[:top]


def registered_domain(url):
    """'https://www.acme.co.uk/about' -> 'acme.co.uk'."""
    host = (urlparse(url if "//" in url else f"https://{url}").hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "ac", "gov"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def pick_company_domain(company, results):
    """First result that is not a job board or social site; prefer one containing the company name."""
    tokens = company_tokens(company)
    fallback = None
    for r in results:
        dom = registered_domain(r.get("href") or "")
        if not dom or dom in NON_COMPANY_DOMAINS or any(dom.endswith("." + d) for d in NON_COMPANY_DOMAINS):
            continue
        if any(t in dom.replace("-", "") for t in tokens):
            return dom
        fallback = fallback or dom
    return fallback


def parse_linkedin_title(title):
    """'Jane Doe - CTO - Acme | LinkedIn' -> ('Jane Doe', 'CTO', 'Acme'), else None."""
    if not title:
        return None
    text = re.sub(r"\s*[|\-–—]\s*LinkedIn\s*$", "", title.strip(), flags=re.IGNORECASE)
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", text) if p.strip()]
    if len(parts) < 2:
        return None
    name, role = parts[0], parts[1]
    company = parts[2] if len(parts) > 2 else ""
    if not company and " at " in role:
        role, _, company = role.partition(" at ")
    if len(name.split()) < 2 or len(name) > 60:
        return None
    return name, role.strip(), company.strip()


def norm_name(name):
    return re.sub(r"[^a-z]", "", (name or "").lower())


def split_name(name):
    parts = [re.sub(r"[^a-z]", "", p.lower()) for p in (name or "").split()]
    parts = [p for p in parts if p and p not in {"jr", "sr", "ii", "iii", "phd", "mba", "dr"}]
    if len(parts) < 2:
        return None
    return parts[0], parts[-1]


def seniority(title):
    return max((level for rx, level in SENIORITY_RULES if rx.search(title or "")), default=-1)


def select_decision_makers(people, k=3):
    """Most senior technical buyers first; C-level kept regardless of function."""
    scored = []
    for p in people:
        level = seniority(p.get("title"))
        if level < 0:
            continue
        tech = 1 if TECH_RE.search(p.get("title") or "") else 0
        if level < 3 and not tech:
            continue  # a VP of Sales does not buy engineering work
        scored.append((level, tech, 1 if p.get("email") else 0, p))
    scored.sort(key=lambda t: (-t[0], -t[1], -t[2], t[3]["name"].lower()))
    return [t[3] for t in scored[:k]]


def dedupe_people(people):
    """Merge records of the same person from different sources, keeping every non-empty field."""
    merged = {}
    for p in people:
        key = norm_name(p.get("name"))
        if not key:
            continue
        if key not in merged:
            merged[key] = dict(p)
            merged[key]["sources"] = [p.get("source", "")]
            continue
        current = merged[key]
        for field in ("title", "linkedin", "email", "email_confidence", "email_status", "phone", "apollo_id"):
            if not current.get(field) and p.get(field):
                current[field] = p[field]
        current["sources"].append(p.get("source", ""))
    return list(merged.values())


def infer_email_pattern(emails, domain):
    """Guess the naming scheme from non-generic addresses on the company domain."""
    locals_ = []
    for e in emails:
        local, _, dom = e.lower().partition("@")
        if dom == domain and local not in GENERIC_LOCAL_PARTS:
            locals_.append(local)
    if not locals_:
        return None
    if any(l.count(".") == 1 and all(p.isalpha() for p in l.split(".")) for l in locals_):
        return "first.last"
    if any("_" in l for l in locals_):
        return "first_last"
    if all(l.isalpha() for l in locals_):
        return "flast" if all(len(l) > 6 for l in locals_) else "first"
    return None


def detect_pattern(names, emails, domain):
    """Confirm the pattern from a (name, email) match on the team page, else infer it."""
    locals_ = {e.lower().split("@")[0] for e in emails if e.lower().endswith("@" + domain)}
    for name in names:
        parts = split_name(name)
        if not parts:
            continue
        for pattern, build in PATTERNS.items():
            if build(*parts) in locals_:
                return pattern, "confirmed"
    inferred = infer_email_pattern(emails, domain)
    return inferred, ("inferred" if inferred else None)


def guess_candidates(name, domain, pattern=None):
    """Ordered, de-duplicated guesses: the known pattern first, then the common ones."""
    parts = split_name(name)
    if not parts or not domain:
        return []
    order = ([pattern] if pattern in PATTERNS else []) + GUESS_ORDER
    seen, guesses = set(), []
    for key in order:
        email = f"{PATTERNS[key](*parts)}@{domain}"
        if email not in seen:
            seen.add(email)
            guesses.append(email)
    return guesses


def find_known_email(name, emails):
    parts = split_name(name)
    if not parts:
        return None
    candidates = {build(*parts) for build in PATTERNS.values()}
    return next((e for e in emails if e.split("@")[0] in candidates), None)


def normalize_phone(raw):
    digits = re.sub(r"[^\d+]", "", raw or "")
    if "+" in digits[1:]:
        digits = digits[0] + digits[1:].replace("+", "")
    return digits


def strip_html(html_text):
    text = re.sub(r"<(?:script|style)[^>]*>.*?</(?:script|style)>", " ", html_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def parse_json_block(text):
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


def linkedin_query(company):
    return f'"{company}" (CTO OR "VP Engineering" OR "Head of Engineering" OR founder OR CEO) site:linkedin.com/in'


def people_from_results(results, company, source):
    people = []
    for r in results or []:
        parsed = parse_linkedin_title(r.get("title", ""))
        if not parsed:
            continue
        name, role, listed_company = parsed
        if seniority(role) < 0:
            continue
        if not company_mentioned(company, f"{role} {listed_company} {r.get('body', '')}"):
            continue
        people.append({"name": name, "title": role, "company_on_profile": listed_company,
                       "linkedin": r.get("href", ""), "snippet": (r.get("body") or "")[:300],
                       "source": source})
    return people


def merge_llm_people(original, llm_people):
    """Apply the model's filtering but keep emails and ids the providers already attached."""
    if not isinstance(llm_people, list):
        return original
    by_name = {norm_name(p["name"]): p for p in original if p.get("name")}
    merged = []
    for lp in llm_people:
        if not isinstance(lp, dict) or not lp.get("name"):
            continue
        base = by_name.get(norm_name(lp["name"]), {"name": lp["name"], "source": "site text via llm", "sources": ["llm"]})
        merged.append({**base, "title": lp.get("title") or base.get("title", ""),
                       "linkedin": base.get("linkedin") or lp.get("linkedin") or ""})
    return merged if merged else original


# ============================================================
# EMAIL AND PHONE WATERFALL
# ============================================================

def resolve_email(person, domain, company, site_emails, pattern, pattern_source, providers, min_conf):
    """Try sources from cheapest to most expensive; stop at the first one at or above min_conf."""
    name = person["name"]
    best = None

    def consider(email, conf, status, extra=None):
        nonlocal best
        if not email:
            return False
        candidate = {"email": email.lower(), "email_confidence": int(conf), "email_status": status, **(extra or {})}
        if best is None or candidate["email_confidence"] > best["email_confidence"]:
            best = candidate
        return candidate["email_confidence"] >= min_conf

    if consider(person.get("email"), person.get("email_confidence", 0), person.get("email_status", "from discovery")):
        return best
    if not domain:
        return best

    if consider(find_known_email(name, site_emails), CONFIDENCE["site"], "found on company site"):
        return best

    github = providers.get("github")
    if github and github.available(0):
        hit = github.find_email(name, company, domain)
        if hit and consider(hit["email"], hit["confidence"], hit["source"]):
            return best

    parts = split_name(name)
    if parts:
        first, last = parts
        for key in ("hunter", "tomba"):
            finder = providers.get(key)
            if finder and finder.available(1):
                hit = finder.email_finder(first, last, domain)
                if hit and consider(hit["email"], hit["confidence"], hit["source"],
                                    {"phone": hit.get("phone") or person.get("phone")}):
                    return best
        apollo = providers.get("apollo")
        if apollo and apollo.available(1):
            hit = apollo.enrich(person, domain)
            if hit and consider(hit["email"], hit["confidence"], hit["source"],
                                {"personal_emails": hit.get("personal_emails") or [],
                                 "phone": hit.get("phone") or person.get("phone")}):
                return best

    if best is None:
        guesses = guess_candidates(name, domain, pattern)
        if guesses:
            conf = {"confirmed": CONFIDENCE["guess_confirmed"], "provider": CONFIDENCE["guess_confirmed"],
                    "inferred": CONFIDENCE["guess_inferred"]}.get(pattern_source, CONFIDENCE["guess_default"])
            consider(guesses[0], conf, f"guessed ({pattern_source or 'default first.last'})",
                     {"alternates": guesses[1:4]})
    return best


def verify_email(result, providers):
    """Verify anything below the verified bar; a hard bounce moves on to the next guess."""
    if not result or result["email_confidence"] >= CONFIDENCE["verified"]:
        return result
    verifiers = [providers[k] for k in ("zerobounce", "hunter", "tomba") if k in providers]
    candidates = [result["email"]] + list(result.get("alternates") or [])
    bounced = []
    for email in candidates[:3]:
        verdict, used = None, None
        for v in verifiers:
            if v.available(v.verify_cost, v.verify_bucket):
                verdict = v.verify(email)
                if verdict:
                    used = v.name
                    break
        if not verdict:
            break  # no verifier left this period
        if verdict["status"] == "valid":
            return {**result, "email": email, "email_confidence": CONFIDENCE["verified"],
                    "email_status": f"{result['email_status']}; verified by {used}"}
        if verdict["status"] == "risky":
            return {**result, "email": email,
                    "email_confidence": max(result["email_confidence"], CONFIDENCE["risky"]),
                    "email_status": f"{result['email_status']}; catch-all domain ({used})"}
        if verdict["status"] == "invalid":
            bounced.append(email)
            continue
        break  # unknown: keep what we have
    if bounced and len(bounced) == len(candidates[:3]):
        return {**result, "email": None, "email_confidence": 0,
                "email_status": f"all guesses bounced ({', '.join(bounced)})"}
    if bounced:
        remaining = [e for e in candidates if e not in bounced]
        return {**result, "email": remaining[0], "email_confidence": min(result["email_confidence"], 30),
                "email_status": f"{result['email_status']}; {bounced[0]} bounced, unverified alternate"}
    return result


def resolve_phone(person, providers, company_phones):
    if person.get("phone"):
        return normalize_phone(person["phone"]), "direct"
    tomba = providers.get("tomba")
    if (tomba and person.get("email") and person.get("email_confidence", 0) >= CONFIDENCE["guess_confirmed"]
            and tomba.available(1)):
        found = tomba.phone_finder(person["email"])
        if found:
            return normalize_phone(found), "direct (tomba)"
    if company_phones:
        return company_phones[0], "company line"
    return None, "not found"


# ============================================================
# SEARCH, SITE, LLM
# ============================================================

def ddg_search(ddg, query, max_results=10, retries=2):
    for attempt in range(retries + 1):
        try:
            results = list(ddg.text(query, max_results=max_results) or [])
            time.sleep(random.uniform(2.0, 4.0))  # DDG blocks bursts
            return results
        except Exception as exc:
            wait = 30 * (attempt + 1)
            print(f"    duckduckgo {exc.__class__.__name__}; waiting {wait}s")
            time.sleep(wait)
    return []


def fetch_site_contacts(domain, session):
    emails, phones, texts = set(), set(), []
    for path in SITE_PATHS:
        try:
            resp = session.get(f"https://{domain}{path}", timeout=10,
                               headers={"User-Agent": random.choice(USER_AGENTS)})
        except requests.RequestException:
            continue
        if resp.status_code != 200 or "text/html" not in resp.headers.get("Content-Type", ""):
            continue
        html_text = resp.text
        for m in re.finditer(r'href=["\']mailto:([^"\'?]+)', html_text, re.IGNORECASE):
            emails.add(m.group(1).strip().lower())
        for m in re.finditer(r'href=["\']tel:([^"\']+)', html_text, re.IGNORECASE):
            phones.add(normalize_phone(m.group(1)))
        text = strip_html(html_text)
        emails.update(e.lower() for e in EMAIL_RE.findall(text)
                      if not e.lower().endswith((".png", ".jpg", ".svg", ".gif", ".webp")))
        phones.update(normalize_phone(p) for p in PHONE_RE.findall(text))
        texts.append(text[:4000])
        time.sleep(random.uniform(0.5, 1.5))

    base = domain.split(".")[0]
    emails = {e for e in emails if e.endswith("@" + domain) or base in e.split("@")[-1]}
    phones = {p for p in phones if 9 <= len(re.sub(r"\D", "", p)) <= 15}
    return sorted(emails), sorted(phones), " ".join(texts)


LLM_SYSTEM = (
    "You clean up B2B lead research. Use only the evidence given; never invent names, emails or "
    "phone numbers. Return strict JSON with keys: domain (string or null), people (list of "
    "{name, title, linkedin}), emails (business addresses on the company domain), phones "
    "(company phone numbers). Keep only people who currently hold a C-level, founder, VP, head or "
    "director role in engineering, technology or product at the named company. Add people named "
    "with such a title in the site text."
)


def llm_cleanup(client, company, domain, people, emails, phones, site_text, snippets):
    evidence = {
        "company": company,
        "candidate_domain": domain,
        "people_candidates": [{k: p.get(k) for k in ("name", "title", "linkedin", "company_on_profile", "snippet", "source")}
                              for p in people],
        "emails_found": emails,
        "phones_found": phones,
        "site_text_excerpt": site_text[:6000],
        "search_snippets": snippets[:15],
    }
    data = parse_json_block(client.chat(LLM_SYSTEM, json.dumps(evidence, ensure_ascii=False)))
    return data if isinstance(data, dict) else None


# ============================================================
# PIPELINE
# ============================================================

def discover_people(company, domain, searchers, providers, k):
    """Collect candidates from search engines, Apollo, then Hunter/Tomba domain search if still short."""
    people, pattern, pattern_source = [], None, None
    for label, search in searchers:
        people += people_from_results(search(linkedin_query(company)), company, label)
        if len(select_decision_makers(dedupe_people(people), k)) >= k:
            break

    apollo = providers.get("apollo")
    if domain and apollo:
        people += apollo.people_search(domain)

    if domain:
        for key in ("hunter", "tomba"):
            if len(select_decision_makers(dedupe_people(people), k)) >= k:
                break
            prov = providers.get(key)
            if prov and prov.available(1):
                found = prov.domain_search(domain)
                if found:
                    people += found["people"]
                    if found.get("pattern") and not pattern:
                        pattern, pattern_source = found["pattern"], "provider"
    return dedupe_people(people), pattern, pattern_source


def enrich_company(entry, ddg, session, providers, client, args):
    company = entry["company"]
    print(f"\n[{company}] score {entry['max_score']} | {entry['job_count']} jobs")

    searchers = [("duckduckgo", lambda q: ddg_search(ddg, q, 10))]
    brave = providers.get("brave")
    if brave:
        searchers.append(("brave", lambda q: brave.search(q)))

    site_results = searchers[0][1](f'"{company}" official website')
    if not site_results and brave:
        site_results = brave.search(f'"{company}" official website')
    domain = pick_company_domain(company, site_results)
    print(f"  domain: {domain or 'not found'}")

    emails, phones, site_text = fetch_site_contacts(domain, session) if domain else ([], [], "")
    people, pattern, pattern_source = discover_people(company, domain, searchers, providers, args.people)
    print(f"  candidates: {len(people)} | site: {len(emails)} emails, {len(phones)} phones")

    if client:
        snippets = [f"{r.get('title', '')} - {r.get('body', '')}" for r in site_results]
        cleaned = llm_cleanup(client, company, domain, people, emails, phones, site_text, snippets)
        if cleaned:
            llm_domain = (cleaned.get("domain") or "").lower().strip()
            if llm_domain and re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", llm_domain):
                domain = llm_domain
            people = merge_llm_people(people, cleaned.get("people"))
            emails = sorted(set(emails) | {str(e).lower() for e in cleaned.get("emails") or [] if EMAIL_RE.fullmatch(str(e))})
            phones = sorted(set(phones) | {normalize_phone(str(p)) for p in cleaned.get("phones") or []})

    leaders = select_decision_makers(people, args.people)
    if domain and not pattern:
        pattern, pattern_source = detect_pattern([p["name"] for p in people], emails, domain)

    for p in leaders:
        found = resolve_email(p, domain, company, emails, pattern, pattern_source, providers, args.min_confidence)
        found = verify_email(found, providers)
        p.update(found or {"email": None, "email_confidence": 0, "email_status": "not found"})
        p["phone"], p["phone_type"] = resolve_phone(p, providers, phones)
        p.pop("alternates", None)
        print(f"  {p['name']} | {p.get('title') or '?'} | {p.get('email') or '-'} [{p.get('email_confidence', 0)}]"
              f" | {p.get('phone') or '-'} ({p['phone_type']})")

    return {
        **entry,
        "domain": domain,
        "people": leaders,
        "candidates_seen": len(people),
        "company_emails": emails,
        "company_phones": phones,
        "email_pattern": pattern,
        "enriched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def load_dotenv(path):
    """Minimal KEY=VALUE loader so keys can live in the gitignored .env file."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


def build_providers(quota, session):
    env = os.environ.get
    providers = {}
    if env("HUNTER_API_KEY"):
        providers["hunter"] = Hunter(env("HUNTER_API_KEY"), quota, session)
    if env("TOMBA_API_KEY") and env("TOMBA_SECRET"):
        providers["tomba"] = Tomba(env("TOMBA_API_KEY"), env("TOMBA_SECRET"), quota, session)
    if env("APOLLO_API_KEY"):
        providers["apollo"] = Apollo(env("APOLLO_API_KEY"), quota, session)
    if env("ZEROBOUNCE_API_KEY"):
        providers["zerobounce"] = ZeroBounce(env("ZEROBOUNCE_API_KEY"), quota, session)
    if env("BRAVE_API_KEY"):
        providers["brave"] = BraveSearch(env("BRAVE_API_KEY"), quota, session)
    providers["github"] = GitHub(env("GITHUB_TOKEN"), quota, session)  # works without a token
    return providers


def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_cache(cache):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def write_outputs(results):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(LEADS_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    rows = 0
    with open(LEADS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for c in results:
            base = {
                "company": c["company"],
                "domain": c.get("domain") or "",
                "max_score": c["max_score"],
                "job_count": c["job_count"],
                "hiring_for": "; ".join(t for t in c.get("top_titles", []) if t),
                "company_phones": "; ".join(c.get("company_phones") or []),
                "company_emails": "; ".join(c.get("company_emails") or []),
                "job_urls": "; ".join(c.get("job_urls") or []),
            }
            for p in c.get("people") or [{}]:
                writer.writerow({
                    **base,
                    "name": p.get("name", ""),
                    "title": p.get("title", ""),
                    "linkedin": p.get("linkedin", ""),
                    "email": p.get("email") or "",
                    "email_confidence": p.get("email_confidence", ""),
                    "email_status": p.get("email_status", ""),
                    "personal_emails": "; ".join(p.get("personal_emails") or []),
                    "phone": p.get("phone") or "",
                    "phone_type": p.get("phone_type", ""),
                })
                rows += 1
    print(f"\nWrote {len(results)} companies / {rows} rows to {LEADS_CSV}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", default=JOBS_FILE, help="scored job list (default: scripts/output/all_jobs.json)")
    parser.add_argument("--top", type=int, default=40,
                        help="companies to enrich; OpenRouter free tier allows ~50 requests/day")
    parser.add_argument("--min-score", type=int, default=4, help="minimum best lead_intent_score per company")
    parser.add_argument("--people", type=int, default=3, help="decision makers to resolve per company")
    parser.add_argument("--min-confidence", type=int, default=70,
                        help="stop spending provider credits once an email reaches this confidence")
    parser.add_argument("--no-llm", action="store_true", help="skip the OpenRouter cleanup pass")
    parser.add_argument("--fresh", action="store_true", help="ignore enrich_cache.json and redo every company")
    args = parser.parse_args()

    if DDGS is None:
        sys.exit("ddgs is not installed: pip install ddgs")
    load_dotenv(ENV_FILE)

    with open(args.input, encoding="utf-8") as f:
        jobs = json.load(f)
    companies = rank_companies(jobs, top=args.top, min_score=args.min_score)
    print(f"{len(jobs):,} jobs -> {len(companies)} companies with best score >= {args.min_score}")

    quota = QuotaTracker()
    session = requests.Session()
    providers = build_providers(quota, session)
    print("providers: " + ", ".join(sorted(providers)))

    client = None
    if not args.no_llm:
        if os.environ.get("OPENROUTER_API_KEY"):
            client = OpenRouterClient(os.environ["OPENROUTER_API_KEY"], quota, os.environ.get("OPENROUTER_MODEL"), session)
            print(f"OpenRouter model: {client.model}")
        else:
            print("OPENROUTER_API_KEY not set; running without LLM cleanup")

    cache = {} if args.fresh else load_cache()
    results = []
    with DDGS() as ddg:
        for entry in companies:
            key = entry["company_key"]
            if key in cache:
                print(f"\n[{entry['company']}] cached")
                results.append(cache[key])
                continue
            try:
                enriched = enrich_company(entry, ddg, session, providers, client, args)
            except KeyboardInterrupt:
                print("\nInterrupted; progress is in enrich_cache.json, rerun to resume")
                break
            except Exception as exc:
                print(f"  failed: {exc}")
                enriched = {**entry, "domain": None, "people": [], "company_emails": [],
                            "company_phones": [], "error": str(exc)}
            cache[key] = enriched
            results.append(enriched)
            save_cache(cache)

    write_outputs(results)
    buckets = [p.name for p in providers.values()] + (["tomba_verify"] if "tomba" in providers else []) + (["openrouter"] if client else [])
    print("\nFree-tier usage this period:")
    for bucket, info in quota.summary(buckets).items():
        print(f"  {bucket:<13} {info['used']:>6g} / {info['cap']:g} per {info['period']}")
    for p in providers.values():
        if p.disabled_reason:
            print(f"  {p.name}: stopped early ({p.disabled_reason})")


if __name__ == "__main__":
    main()
