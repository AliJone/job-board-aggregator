#!/usr/bin/env python3
"""
Free-tier data providers for enrich_leads.py.

Every provider is optional: it is built only when its API key is present, and
it stops calling out once the free quota for the period is spent (tracked in
scripts/output/provider_quota.json) or the API answers 401/402/403/429.

Free tiers as of 2026-10 (check each vendor's pricing page before relying on them):
  Hunter      50 shared credits/month (find = 1, verify = 0.5), API included
  Tomba       25 search credits + 50 verifications/month, 1 req/s, API included
  Apollo      people search = 0 credits; enrichment ~75 credits/month, needs a work-email account
  ZeroBounce  100 verifications/month, business domain required
  GitHub      no key needed; 10 searches/min, 60 requests/hour unauthenticated
  Brave       $5 credit/month (about 1,000 searches), card on file
  OpenRouter  ~50 requests/day on :free models without purchased credits
"""

import json
import os
import re
import time

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
QUOTA_FILE = os.path.join(SCRIPT_DIR, "output", "provider_quota.json")

# bucket -> (free allowance, period). Override a cap with env CAP_<BUCKET>, e.g. CAP_HUNTER=100.
DEFAULT_CAPS = {
    "hunter": (50, "month"),
    "tomba": (25, "month"),
    "tomba_verify": (50, "month"),
    "apollo": (75, "month"),
    "zerobounce": (100, "month"),
    "brave": (1000, "month"),
    "openrouter": (50, "day"),
}

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
# Tried in order, but only if OpenRouter's live catalog still lists them as free.
PREFERRED_FREE_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openai/gpt-oss-120b:free",
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
]

STOPWORDS = {"the", "inc", "llc", "ltd", "corp", "corporation", "company", "group",
             "holdings", "technologies", "technology", "labs", "and"}

# Hunter and Tomba express address patterns as templates; map them to enrich_leads' keys.
TEMPLATE_TO_PATTERN = {
    "{first}.{last}": "first.last", "{first}": "first", "{f}{last}": "flast",
    "{first}{l}": "firstl", "{first}_{last}": "first_last", "{first}{last}": "firstlast",
    "{last}.{first}": "last.first", "{f}.{last}": "f.last", "{last}": "last",
    "{last}{f}": "lastf", "{first}-{last}": "first-last",
}


def company_tokens(company):
    return [t for t in re.findall(r"[a-z0-9]+", (company or "").lower()) if len(t) > 2 and t not in STOPWORDS]


def company_mentioned(company, text):
    tokens = company_tokens(company)
    return any(t in (text or "").lower() for t in tokens) if tokens else True


def template_to_pattern(template):
    if not template:
        return None
    return TEMPLATE_TO_PATTERN.get(str(template).strip().lower())


def normalize_verdict(status, result=None, score=None):
    """Collapse provider-specific verification answers into valid / invalid / risky / unknown."""
    status = (status or "").lower()
    result = (result or "").lower()
    if result == "deliverable" or status == "valid":
        verdict = "valid"
    elif result == "undeliverable" or status in {"invalid", "spamtrap", "abuse", "do_not_mail", "disposable"}:
        verdict = "invalid"
    elif result == "risky" or status in {"accept_all", "catch-all", "catch_all", "webmail"}:
        verdict = "risky"
    else:
        verdict = "unknown"
    return {"status": verdict, "raw_status": status or None, "raw_result": result or None, "score": score}


def _full_name(first, last, fallback=""):
    name = " ".join(p for p in (first, last) if p)
    return (name or fallback or "").strip()


class QuotaTracker:
    """Counts credit-spending calls per bucket per period so free tiers are never exceeded."""

    def __init__(self, path=QUOTA_FILE, caps=None):
        self.path = path
        self.caps = dict(DEFAULT_CAPS)
        self.caps.update(caps or {})
        for bucket in list(self.caps):
            override = os.environ.get(f"CAP_{bucket.upper()}")
            if override:
                self.caps[bucket] = (float(override), self.caps[bucket][1])
        try:
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (FileNotFoundError, ValueError):
            self.data = {}

    def _key(self, bucket):
        period = self.caps.get(bucket, (None, "month"))[1]
        return time.strftime("%Y-%m-%d" if period == "day" else "%Y-%m")

    def used(self, bucket):
        return self.data.get(bucket, {}).get(self._key(bucket), 0)

    def remaining(self, bucket):
        cap = self.caps.get(bucket)
        return None if cap is None else max(cap[0] - self.used(bucket), 0)

    def allow(self, bucket, cost=1):
        remaining = self.remaining(bucket)
        return remaining is None or remaining >= cost

    def spend(self, bucket, cost=1):
        self.data.setdefault(bucket, {})[self._key(bucket)] = self.used(bucket) + cost
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)

    def summary(self, buckets=None):
        return {b: {"used": self.used(b), "cap": self.caps[b][0], "period": self.caps[b][1]}
                for b in (buckets or self.caps) if b in self.caps}


class Provider:
    name = "base"
    verify_cost = 1
    verify_bucket = None

    def __init__(self, quota, session=None):
        self.quota = quota
        self.session = session or requests.Session()
        self.disabled_reason = None
        self.calls = 0

    def available(self, cost=1, bucket=None):
        if self.disabled_reason:
            return False
        if cost and not self.quota.allow(bucket or self.name, cost):
            self.disabled_reason = "free quota for this period is used up"
            print(f"    {self.name}: {self.disabled_reason}")
            return False
        return True

    def _request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", 20)
        try:
            resp = self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            print(f"    {self.name}: {exc.__class__.__name__}")
            return None
        if resp.status_code == 404:
            return {}
        if resp.status_code == 429:
            self.disabled_reason = "rate limited"
        elif resp.status_code in (401, 403):
            self.disabled_reason = f"HTTP {resp.status_code}, check the API key"
        elif resp.status_code == 402:
            self.disabled_reason = "out of credits"
        if self.disabled_reason:
            print(f"    {self.name}: {self.disabled_reason}, skipping for the rest of this run")
            return None
        if resp.status_code >= 400:
            print(f"    {self.name}: HTTP {resp.status_code} {resp.text[:120]}")
            return None
        self.calls += 1
        try:
            return resp.json()
        except ValueError:
            return None

    def _get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)

    def _post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)

    @staticmethod
    def _people_from_emails(entries, source):
        people = []
        for e in entries or []:
            name = _full_name(e.get("first_name"), e.get("last_name"))
            email = e.get("value") or e.get("email")
            if not name or not email:
                continue
            people.append({
                "name": name,
                "title": e.get("position") or "",
                "linkedin": e.get("linkedin") or e.get("linkedin_url") or "",
                "email": email.lower(),
                "email_confidence": int(e.get("confidence") or e.get("score") or 0),
                "email_status": f"{source} domain search",
                "phone": e.get("phone_number") or None,
                "source": source,
            })
        return people


class Hunter(Provider):
    name = "hunter"
    verify_cost = 0.5
    BASE = "https://api.hunter.io/v2"

    def __init__(self, api_key, quota, session=None):
        super().__init__(quota, session)
        self.key = api_key

    def domain_search(self, domain):
        """Senior people with emails plus the address pattern. 1 credit."""
        if not self.available(1):
            return None
        data = self._get(f"{self.BASE}/domain-search", params={
            "domain": domain, "api_key": self.key, "limit": 10,
            "seniority": "senior,executive", "department": "executive,it,management",
        })
        if data is None:
            return None
        self.quota.spend(self.name, 1)
        d = data.get("data") or {}
        return {"pattern": template_to_pattern(d.get("pattern")),
                "people": self._people_from_emails(d.get("emails"), self.name)}

    def email_finder(self, first, last, domain):
        """1 credit, free when nothing is found."""
        if not self.available(1):
            return None
        data = self._get(f"{self.BASE}/email-finder", params={
            "domain": domain, "first_name": first, "last_name": last, "api_key": self.key})
        if data is None:
            return None
        d = data.get("data") or {}
        if not d.get("email"):
            return None
        self.quota.spend(self.name, 1)
        return {"email": d["email"].lower(), "confidence": int(d.get("score") or 0),
                "phone": d.get("phone_number") or None, "title": d.get("position") or "",
                "source": "hunter email finder"}

    def verify(self, email):
        if not self.available(self.verify_cost):
            return None
        data = self._get(f"{self.BASE}/email-verifier", params={"email": email, "api_key": self.key})
        if data is None:
            return None
        self.quota.spend(self.name, self.verify_cost)
        d = data.get("data") or {}
        return normalize_verdict(d.get("status"), d.get("result"), d.get("score"))


class Tomba(Provider):
    name = "tomba"
    verify_bucket = "tomba_verify"
    BASE = "https://api.tomba.io/v1"

    def __init__(self, api_key, secret, quota, session=None):
        super().__init__(quota, session)
        self.headers = {"X-Tomba-Key": api_key, "X-Tomba-Secret": secret}

    def _paced(self, url, **kwargs):
        time.sleep(1.1)  # free plan allows 1 request per second
        return self._get(url, headers=self.headers, **kwargs)

    def domain_search(self, domain):
        if not self.available(1):
            return None
        data = self._paced(f"{self.BASE}/domain-search", params={"domain": domain, "limit": 10})
        if data is None:
            return None
        self.quota.spend(self.name, 1)
        d = data.get("data") or {}
        org = d.get("organization") or {}
        return {"pattern": template_to_pattern(org.get("pattern")),
                "people": self._people_from_emails(d.get("emails"), self.name)}

    def email_finder(self, first, last, domain):
        if not self.available(1):
            return None
        data = self._paced(f"{self.BASE}/email-finder", params={
            "domain": domain, "first_name": first, "last_name": last})
        if data is None:
            return None
        e = (data.get("data") or {}).get("email") or {}
        if not e.get("email"):
            return None
        self.quota.spend(self.name, 1)
        return {"email": e["email"].lower(), "confidence": int(e.get("score") or 0),
                "phone": e.get("phone_number") or None, "title": e.get("position") or "",
                "source": "tomba email finder"}

    def verify(self, email):
        if not self.available(1, self.verify_bucket):
            return None
        data = self._paced(f"{self.BASE}/email-verifier/{email}")
        if data is None:
            return None
        self.quota.spend(self.verify_bucket, 1)
        e = (data.get("data") or {}).get("email") or {}
        return normalize_verdict(e.get("status"), e.get("result"), e.get("score"))

    def phone_finder(self, email):
        """Direct number linked to a known address. 1 search credit."""
        if not self.available(1):
            return None
        data = self._paced(f"{self.BASE}/phone/{email}")
        if data is None:
            return None
        self.quota.spend(self.name, 1)
        d = data.get("data") or {}
        if not d.get("valid"):
            return None
        return d.get("e164_format") or d.get("intl_format") or d.get("local_format")


class Apollo(Provider):
    name = "apollo"
    BASE = "https://api.apollo.io/api/v1"
    TITLES = ["CTO", "Chief Technology Officer", "VP Engineering", "VP of Engineering",
              "Head of Engineering", "Director of Engineering", "Founder", "Co-Founder",
              "CEO", "Chief Product Officer", "Head of Platform", "VP Technology"]

    def __init__(self, api_key, quota, session=None):
        super().__init__(quota, session)
        self.headers = {"x-api-key": api_key, "Content-Type": "application/json",
                        "Cache-Control": "no-cache"}

    def people_search(self, domain):
        """Names and titles only. 0 credits; emails come back masked."""
        if not self.available(0):
            return []
        data = self._post(f"{self.BASE}/mixed_people/search", headers=self.headers, json={
            "q_organization_domains_list": [domain],
            "person_titles": self.TITLES,
            "person_seniorities": ["founder", "c_suite", "vp", "head", "director"],
            "per_page": 10,
        })
        people = []
        for p in (data or {}).get("people") or []:
            name = _full_name(p.get("first_name"), p.get("last_name"), p.get("name"))
            if not name:
                continue
            people.append({"name": name, "title": p.get("title") or "",
                           "linkedin": p.get("linkedin_url") or "", "apollo_id": p.get("id"),
                           "source": "apollo"})
        return people

    def enrich(self, person, domain):
        """Work email plus personal emails. 1 credit when something is revealed.
        Phone reveal needs a webhook URL and is not wired up here."""
        if not self.available(1):
            return None
        body = {"reveal_personal_emails": True, "domain": domain}
        if person.get("apollo_id"):
            body["id"] = person["apollo_id"]
        elif person.get("linkedin"):
            body["linkedin_url"] = person["linkedin"]
        else:
            parts = (person.get("name") or "").split()
            if len(parts) < 2:
                return None
            body.update(first_name=parts[0], last_name=parts[-1])
        data = self._post(f"{self.BASE}/people/match", headers=self.headers, json=body)
        p = (data or {}).get("person") or {}
        email = p.get("email") or ""
        if "email_not_unlocked" in email:
            email = ""
        personal = [e for e in p.get("personal_emails") or [] if e and "not_unlocked" not in e]
        if not email and not personal:
            return None
        self.quota.spend(self.name, 1)
        phones = [ph.get("sanitized_number") or ph.get("raw_number")
                  for ph in p.get("phone_numbers") or [] if isinstance(ph, dict)]
        return {"email": (email or personal[0]).lower(), "personal_emails": personal,
                "confidence": 80, "phone": next((ph for ph in phones if ph), None),
                "title": p.get("title") or "", "source": "apollo enrichment"}


class ZeroBounce(Provider):
    name = "zerobounce"
    BASE = "https://api.zerobounce.net/v2"

    def __init__(self, api_key, quota, session=None):
        super().__init__(quota, session)
        self.key = api_key

    def verify(self, email):
        if not self.available(1):
            return None
        data = self._get(f"{self.BASE}/validate", params={"api_key": self.key, "email": email, "ip_address": ""})
        if data is None:
            return None
        self.quota.spend(self.name, 1)
        return normalize_verdict(data.get("status"))


class GitHub(Provider):
    """Public profile emails. Free; a token lifts the 60/hour limit to 5,000."""
    name = "github"
    BASE = "https://api.github.com"

    def __init__(self, token, quota, session=None):
        super().__init__(quota, session)
        self.headers = {"Accept": "application/vnd.github+json", "User-Agent": "job-board-aggregator"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def find_email(self, name, company, domain):
        if not self.available(0):
            return None
        data = self._get(f"{self.BASE}/search/users", headers=self.headers,
                         params={"q": f"{name} in:name", "per_page": 5})
        for item in (data or {}).get("items") or []:
            user = self._get(item.get("url", ""), headers=self.headers) or {}
            blob = f"{user.get('company') or ''} {user.get('blog') or ''} {user.get('bio') or ''}"
            if (domain and domain in blob.lower()) or company_mentioned(company, blob):
                if user.get("email"):
                    return {"email": user["email"].lower(), "confidence": 85,
                            "source": f"github profile {user.get('html_url', '')}".strip()}
            time.sleep(1)
        return None


class BraveSearch(Provider):
    name = "brave"
    BASE = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key, quota, session=None):
        super().__init__(quota, session)
        self.headers = {"Accept": "application/json", "X-Subscription-Token": api_key}

    def search(self, query, count=10):
        if not self.available(1):
            return []
        data = self._get(self.BASE, headers=self.headers, params={"q": query, "count": count})
        if data is None:
            return []
        self.quota.spend(self.name, 1)
        return [{"title": r.get("title", ""), "href": r.get("url", ""), "body": r.get("description", "")}
                for r in ((data.get("web") or {}).get("results") or [])]


class OpenRouterClient(Provider):
    name = "openrouter"

    def __init__(self, api_key, quota, model=None, session=None):
        super().__init__(quota, session)
        self.key = api_key
        self.model = model or self._pick_free_model()

    def _pick_free_model(self):
        try:
            resp = requests.get(OPENROUTER_MODELS_URL, timeout=20)
            resp.raise_for_status()
            free = {
                m["id"] for m in resp.json().get("data", [])
                if m.get("id", "").endswith(":free")
                and float((m.get("pricing") or {}).get("prompt") or 0) == 0
                and float((m.get("pricing") or {}).get("completion") or 0) == 0
            }
        except (requests.RequestException, ValueError, KeyError):
            free = set()
        for candidate in PREFERRED_FREE_MODELS:
            if candidate in free:
                return candidate
        return sorted(free)[0] if free else PREFERRED_FREE_MODELS[0]

    def chat(self, system, user, retries=3):
        if not self.available(1):
            return None
        payload = {"model": self.model, "temperature": 0,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                   "HTTP-Referer": "https://github.com/AliJone/job-board-aggregator",
                   "X-Title": "job-board-aggregator lead enrichment"}
        for attempt in range(retries + 1):
            try:
                resp = self.session.post(OPENROUTER_CHAT_URL, json=payload, headers=headers, timeout=90)
            except requests.RequestException as exc:
                print(f"    openrouter: {exc.__class__.__name__}")
                time.sleep(10)
                continue
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After") or 20 * (attempt + 1))
                print(f"    openrouter: rate limited, waiting {wait}s")
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                print(f"    openrouter: HTTP {resp.status_code} {resp.text[:160]}")
                return None
            self.calls += 1
            self.quota.spend(self.name, 1)
            try:
                return resp.json()["choices"][0]["message"]["content"]
            except (KeyError, IndexError, ValueError):
                return None
        return None
