"""Candidate-side job filters: globally remote, hires from Pakistan, and
3-4 years of experience, plus role presets matched on the job title.

evaluate(job) returns {filter_key: (passed, reason)} for every filter, and
evaluate_role(job, roles) the same pair for the selected roles, so callers
can log why a job was kept or dropped. Run them on the raw scraped job,
before the description is trimmed for the frontend.

parse_search() and matches_search() apply the frontend Filters panel on the
server, mirroring filterJobs() in js/filters.js. Run them on the
frontend-shaped job, after salary has been attached."""
import html
import re
from datetime import datetime, timezone

FILTERS = {
    "global_remote": "Globally remote",
    "pakistan": "Hires from Pakistan",
    "yoe_3_4": "3-4 years experience",
}

GLOBAL = re.compile(r"\b(worldwide|anywhere in the world|work from anywhere|remote[\s\-–,(/:]*(global|anywhere)|globally remote|fully distributed)\b")
PK_REGION = re.compile(r"\b(pakistan|apac|asia|south asia|asia[\s-]pacific)\b")
RESTRICT = re.compile(
    r"(must (be|reside|live)[^.]{0,30}\b(in|within) (the )?(us|u\.s\.|united states|usa|canada|uk|united kingdom|eu|europe)\b"
    r"|authori[sz]ed to work in (the )?(us|u\.s\.|united states|usa|canada|uk)"
    r"|\b(us|u\.s\.)[\s-]based\b|remote[\s\-–,(]*(us|usa|u\.s\.|united states|canada|uk|emea|europe|americas|latam)\b)"
)
# Role presets, matched on the title. A job passes when any selected role matches.
ROLES = {
    "swe": ("Software engineer", re.compile(
        r"\b(software|backend|back[\s-]end|frontend|front[\s-]end|full[\s-]?stack|web|mobile|ios|android|platform|product|application)\s+(engineer|developer)\b"
        r"|\bsoftware development engineer\b|\bsde\b|\bmember of technical staff\b"
        # Language and framework titles: "Python Developer", "React Native Engineer".
        r"|\b(python|java|golang|go|rust|ruby|rails|ruby on rails|react|react native|node|node\.js|typescript|javascript"
        r"|kotlin|swift|scala|php|elixir|flutter|django|laravel|vue|angular|unity|game|blockchain|smart contract|web3)\s+(engineer|developer)\b"
        r"|(\.net|c#|c\+\+)\s+(engineer|developer)\b"
        # Reversed: "Engineer, Backend", "Developer - Frontend".
        r"|\b(engineer|developer)\s*[,\-–(]\s*(backend|back[\s-]end|frontend|front[\s-]end|full[\s-]?stack|mobile|web|platform|product)\b")),
    "ai_ml": ("AI / ML engineer", re.compile(
        r"\b(ai|ml|machine learning|deep learning|llm|nlp|computer vision|genai|generative ai)\b[^,|]{0,25}?\b(engineer|developer|scientist|researcher)\b"
        r"|\b(research|applied) (engineer|scientist)\b|\bapplied ai\b")),
    "data": ("Data engineer", re.compile(
        r"\b(data|analytics|etl|big data|data platform|data infrastructure)\s+(engineer|developer)\b")),
    "devops": ("DevOps / SRE", re.compile(
        r"\b(devops|devsecops|site reliability|infrastructure|cloud|reliability|platform)\s+(engineer|developer)\b|\bsre\b|\bdevops\b")),
    # Systems engineer is the SWE title at some companies (Cloudflare, Atlassian);
    # IT, AV, physical-security and business systems roles are left out.
    "systems": ("Systems engineer", re.compile(
        r"(?<!\bit )(?<!\bav )(?<!\bsecurity )(?<!\bbusiness )\b(distributed )?systems? (software )?(engineer|developer)\b")),
    "fde": ("Forward deployed engineer", re.compile(r"\bforward[\s-]deployed\b")),
    "security": ("Security engineer", re.compile(
        r"\bsecurity (software |operations )?engineer\b|\bappsec\b|\bpenetration tester\b|\bsecurity researcher\b")),
    "qa": ("QA / test automation", re.compile(
        r"\b(qa|quality assurance|test|testing|test automation|automation test)\s+(automation\s+)?(engineer|developer)\b"
        r"|\bsdet\b|\bsoftware (development )?engineer in test\b")),
    "embedded": ("Embedded / firmware", re.compile(
        r"\b(embedded|firmware|kernel|driver|compiler|performance|low[\s-]level)\b[^,|]{0,20}?\b(engineer|developer)\b")),
}
# Applies to every role: senior-track and non-engineering titles.
# "Member of Technical Staff" is an engineer title, not a staff-level one.
SENIORITY_EXCLUDE = re.compile(r"(?<!technical )\bstaff\b|\b(principal|director|manager|head|vp|intern|internship|architect|distinguished)\b")
YEARS = re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*(\d{1,2})\s*\+?\s*)?(?:years?|yrs)\b[^.]{0,60}?\bexperience")


def _clean(s):
    s = html.unescape(html.unescape(str(s or "")))
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).lower()


def evaluate(job):
    loc = _clean(job.get("location"))
    text = _clean(job.get("description"))
    title = _clean(job.get("title"))
    both = f"{loc} {text}"
    is_remote = str(job.get("remote")) == "True" or "remote" in loc or "distributed" in loc

    m = GLOBAL.search(both)
    restricted = RESTRICT.search(both)
    if not is_remote:
        g = (False, "not remote")
    elif m and not restricted:
        g = (True, m.group(0))
    elif restricted:
        g = (False, f"restricted: {restricted.group(0)[:30]}")
    else:
        g = (False, "remote, region unclear")

    pk = PK_REGION.search(both)
    if pk and is_remote:
        p = (True, pk.group(0))
    elif pk:
        p = (True, f"{pk.group(0)} (onsite)")
    elif g[0]:
        p = (True, "worldwide, no country limit")
    else:
        p = (False, "no PK/global signal")

    y = YEARS.search(text)
    if y:
        lo, hi = int(y.group(1)), int(y.group(2) or y.group(1))
        ok = 2 <= lo <= 4 and (hi >= 3 if y.group(2) else True)
        e = (ok, f"{lo}-{hi}y" if y.group(2) else f"{lo}+y")
    elif job.get("skill_level") == "mid":
        e = (True, "no yrs; tier mid")
    else:
        e = (False, f"no yrs; tier {job.get('skill_level')}")

    return {"global_remote": g, "pakistan": p, "yoe_3_4": e}


def evaluate_role(job, roles):
    """(passed, reason): the title fits any of the selected role keys.
    No roles selected means any role passes."""
    if not roles:
        return True, "any role"
    title = _clean(job.get("title"))
    excluded = SENIORITY_EXCLUDE.search(title)
    if excluded:
        return False, f"excluded: {excluded.group(0)}"
    for key in roles:
        label, pattern = ROLES[key]
        m = pattern.search(title)
        if m:
            return True, f"{label}: {m.group(0)}"
    return False, "no selected role in title"


def parse_roles(raw):
    """Validate the roles list from the API. Returns (roles, error)."""
    if raw is None:
        return [], None
    if not isinstance(raw, list) or not all(isinstance(r, str) and r in ROLES for r in raw):
        return None, f"roles must be a list of: {', '.join(ROLES)}"
    return list(dict.fromkeys(raw)), None


def passes(result, enabled):
    """True when every enabled filter passed."""
    return all(result[k][0] for k in enabled)


# Filters panel keys, as read by readFilterInputs() in js/filters.js.
# status and hideApplied are left out: they depend on browser-only state.
SEARCH_FIELDS = {
    "title": str, "company": str, "location": str, "ats": str, "skill_level": str,
    "exclude": str, "include": str, "salary": int, "posted": int,
    "hideRecruiters": bool, "remoteOnly": bool,
}


def parse_search(raw):
    """Validate a search object from the API. Returns (search, error).
    Unknown keys are ignored; empty strings and zero numbers are dropped."""
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, "search must be an object"
    search = {}
    for key, kind in SEARCH_FIELDS.items():
        if key not in raw:
            continue
        value = raw[key]
        # bool is a subclass of int, so rule it out for the number fields.
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            return None, f"search.{key} must be a {'string' if kind is str else 'number' if kind is int else 'boolean'}"
        if kind is int and value < 0:
            return None, f"search.{key} must not be negative"
        if kind is str:
            value = value.lower().strip()
        if value:
            search[key] = value
    return search, None


def _word(term):
    # JS \b is ASCII-only, so match that rather than Python's Unicode default.
    return re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE | re.ASCII)


def _terms(text):
    return [t.strip() for t in text.split(",") if t.strip()]


def matches_search(job, search, now=None):
    """True when the job passes every field in search (see filterJobs)."""
    if not search:
        return True
    if search.get("hideRecruiters") and job.get("is_recruiter") is True:
        return False

    title = (job.get("title") or "").lower()
    company = (job.get("company") or job.get("company_slug") or "").lower()
    location = job.get("location") or ""
    location = (location.get("name") or "" if isinstance(location, dict) else str(location)).lower()

    if search.get("salary"):
        median = (job.get("salary") or {}).get("median")
        if not median or median < search["salary"]:
            return False

    if search.get("remoteOnly"):
        if "remote" not in location and (job.get("workplaceType") or "").lower() != "remote":
            return False

    if search.get("ats") and (job.get("ats") or "").lower() != search["ats"]:
        return False
    if search.get("skill_level") and (job.get("skill_level") or "").lower() != search["skill_level"]:
        return False

    if search.get("posted"):
        raw = job.get("posted_at") or job.get("updated_at") or job.get("first_seen")
        try:
            posted = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return False  # no date = excluded when a date filter is active
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        age_days = ((now or datetime.now(timezone.utc)) - posted).total_seconds() / 86400
        if age_days > search["posted"]:
            return False

    if "exclude" in search and any(t in title for t in _terms(search["exclude"])):
        return False
    if "include" in search and not any(t in title for t in _terms(search["include"])):
        return False

    return all(
        _word(search[key]).search(text)
        for key, text in (("title", title), ("company", company), ("location", location))
        if key in search
    )
