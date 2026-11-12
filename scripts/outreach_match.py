#!/usr/bin/env python3
"""
Which of our people fits a posting, and whether the posting is worth the one email its company gets.

The roster is data/talent.json, kept out of git because the repo is public. An email names only skills
that are in both the posting and the roster, so nothing in it is made up. A posting nobody on the team
fits, one that asks for far more years than we have, or one that needs someone on site gets no email.
"""

import json
import os
import re

TALENT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "talent.json")
SENIOR_YEARS = 5
# skip a posting that asks for more than this many years above the person we would offer
MAX_YEARS_GAP = 3

# roster spelling -> how postings write it. Case matters for React, Node and Go: "react quickly",
# "each node" and "go to market" are not skills. React never matches inside React Native.
SKILL_PATTERNS = {
    "React Native": r"(?i:\breact[\s-]+native\b)",
    "React": r"(?i:\breact\.?js\b)|\bReact\b(?![\s-]+[Nn]ative|\s+(?:to|quickly|swiftly|fast)\b)",
    "Next.js": r"(?i:\bnext\.?js\b)",
    "Nuxt": r"(?i:\bnuxt)",
    "Vue": r"(?i:\bvue(?:\.?js)?\b)",
    "Node": r"(?i:\bnode\.?js\b|\bnodejs\b)|\bNode\b",
    "Go": (r"(?i:\bgolang\b)|(?:[,(/:]\s*|\b(?:in|and|or|with|using|like|en|[Dd]eveloper|[Ee]ngineer)\s+)Go\b"
           r"(?![-'’]|\s+(?:to|ahead|live|beyond|back)\b)|\bGo(?=\s+(?:developer|engineer|services|microservices|backend)\b)"),
    ".NET": r"(?i:(?<![a-z0-9])(?:asp\.net|\.net|dotnet)(?![a-z0-9]))",
    "Flutter": r"(?i:\bflutter\b)",
}
MOBILE_SKILLS = {"React Native", "Flutter"}
# a backend posting that mentions the company's React frontend is not asking for React
BACKEND_SKILLS = {"Node", "Go", ".NET"}
# a QA email claims one skill under one number of years, never both
QA_SKILLS = [
    ("test automation", re.compile(r"(?i)\b(test automation|automation testing|automated (tests?|testing)|qa automation"
                                   r"|automation frameworks?|sdet|selenium|cypress|playwright|appium"
                                   r"|webdriverio)\b")),
    ("manual testing", re.compile(r"(?i)\bmanual\s+(qa|testing|tests?|test cases|regression)\b")),
]

PM_RE = re.compile(r"\b(project manager|program manager|delivery manager|scrum master|project lead)\b")
# Product Manager falls here: a product manager is a different job from our project manager
MANAGEMENT_RE = re.compile(r"\b(manager|director|head of|vp|vice president|chief|founder)\b")
OTHER_RE = re.compile(r"\b(data|machine learning|ml|research(er)?|scientist|security|firmware|embedded|hardware"
                      r"|electrical|mechanical|silicon|asic|fpga|rtl|robotics|network(ing)?|sales|account|marketing"
                      r"|support|solutions|customer|success|recruit(er|ing)?|counsel|legal|finance|accountant"
                      r"|intern(ship)?|apprentice|writer|technician|aeronautical|aerospace|avionics|flight"
                      r"|manufacturing|supplier|civil|structural|chemical|clinical|laborator(y|ies))\b")
QA_RE = re.compile(r"\b(qa|quality assurance|quality engineer(ing)?|sdet|test(ing)? engineer|tester|test automation"
                   r"|automation engineer|test analyst)\b")
QA_TITLE_RE = re.compile(r"\b(qa|quality|test|tester|sdet)")
# graphic, interior and architectural designers are not UI/UX work
DESIGN_RE = re.compile(r"\b(ui|ux|ui/ux|ux/ui|product designer|product design|web designer|interaction designer)\b")
BACKEND_TITLE_RE = re.compile(r"\bback[\s-]?end\b")
# a project manager or a QA posting without these is usually construction, hardware or lab work
SOFTWARE_RE = re.compile(r"(?i)\b(software (development|engineering|products?)|saas|agile|scrum|jira|sprints?|developers"
                         r"|engineering teams?|product teams?|web applications?|mobile apps?)\b")
DEVOPS_RE = re.compile(r"\b(devops|dev ops|devsecops|site reliability|sre|infrastructure engineer|cloud engineer"
                       r"|release engineer|build engineer)\b")
MOBILE_RE = re.compile(r"\b(mobile|ios|android|react native|flutter)\b")
ENGINEERING_RE = re.compile(r"\b(full[\s-]?stack|front[\s-]?end|back[\s-]?end|software|developer|engineer"
                            r"|programmer|member of technical staff|web)\b")

YEARS_RE = re.compile(r"(?i)(\+\s*)?\b(\d{1,2})\s*(\+)?\s*(?:(?:-|–|to)\s*\d{1,2}\s*)?\+?\s*(?:years?|yrs?|años)\b"
                      r"([^.;\n]{0,80})")
# "a company with more than 14 years of experience" is about the company, not the hire
COMPANY_YEARS_RE = re.compile(r"(?i)\b(empresa|company|compañía|founded|since|somos|we are|we've been|history"
                              r"|trayectoria|track record)\b")
BLOCKER_RE = re.compile(r"(?i)\b(must (reside|live|be located)|residir en|clearance|ts/sci|commutable|on-?site only"
                        r"|in-office)\b")
HYBRID_RE = re.compile(r"(?i)\bhybrid\b")

_compiled = {}
_fit_cache = {}


def load_roster(path=TALENT_FILE):
    """The people list from talent.json; [] when the file is missing or broken (the run then reports it)."""
    try:
        with open(path, encoding="utf-8") as f:
            return list(json.load(f).get("people") or [])
    except FileNotFoundError:
        return []
    except (ValueError, AttributeError) as exc:
        print(f"{path} is not valid: {exc}")
        return []


def _skill_re(skill):
    if skill not in _compiled:
        _compiled[skill] = re.compile(SKILL_PATTERNS.get(skill)
                                      or rf"(?i:(?<![a-z0-9]){re.escape(skill)}(?![a-z0-9]))")
    return _compiled[skill]


def skills_in(text, skills):
    """The given roster skills that the text names, in the order the text first names them."""
    found = []
    for skill in skills:
        match = _skill_re(skill).search(text or "")
        if match:
            found.append((match.start(), skill))
    return [skill for _, skill in sorted(found)]


def role_family(title):
    """engineering, mobile, qa, devops, design or pm; None for a posting nobody on the team does."""
    t = (title or "").lower()
    if PM_RE.search(t):
        return "pm"
    if MANAGEMENT_RE.search(t) or OTHER_RE.search(t):
        return None
    if QA_RE.search(t):
        return "qa"
    if DESIGN_RE.search(t) and not re.search(r"\b(engineer|developer)\b", t):
        return "design"
    for family, rx in (("devops", DEVOPS_RE), ("mobile", MOBILE_RE), ("engineering", ENGINEERING_RE)):
        if rx.search(t):
            return family
    return None


def required_years(text):
    """The first 'N+ years' or 'N years ... experience' in the posting; '2-5 years' reads as 2."""
    text = text or ""
    for match in YEARS_RE.finditer(text):
        years = int(match.group(2))
        plus = "+" in match.group(0)[:match.start(4) - match.start(0)]
        sentence = re.split(r"[.!?\n]", text[max(0, match.start() - 80):match.start()])[-1]
        if COMPANY_YEARS_RE.search(sentence):
            continue
        if years <= 20 and (plus or "experienc" in match.group(4).lower()):
            return years
    return None


def location_blocker(text, location):
    if BLOCKER_RE.search(text or ""):
        return True
    return bool(HYBRID_RE.search(text or "")) and "remote" not in (location or "").lower()


def _person(family, roster):
    return next((p for p in roster if p.get("family") == family), None)


def _pick(job, family, text, roster):
    """(person, years, skills, line) or None."""
    if family in ("engineering", "mobile"):
        best = None
        for p in roster:
            if p.get("family") != "engineering":
                continue
            skills = skills_in(text, p.get("skills") or [])
            if BACKEND_TITLE_RE.search((job.get("title") or "").lower()):
                skills = [s for s in skills if s in BACKEND_SKILLS]
            if not skills or (family == "mobile" and not MOBILE_SKILLS & set(skills)):
                continue
            key = (len(skills), p.get("years") or 0)
            if best is None or key > best[0]:
                best = (key, p, skills)
        return best and (best[1], best[1].get("years") or 0, best[2][:3], "skills")
    if family == "qa":
        people = [p for p in roster if p.get("family") == "qa"]
        skill = next((name for name, rx in QA_SKILLS if rx.search(text)), None)
        if not people or (not skill and not (QA_TITLE_RE.search((job.get("title") or "").lower())
                                             and SOFTWARE_RE.search(text))):
            return None
        if skill:
            p = max(people, key=lambda p: (p.get("skill_years") or {}).get(skill, 0))
            years = (p.get("skill_years") or {}).get(skill, 0)
            if years:
                return p, years, [skill], "qa"
        p = max(people, key=lambda p: p.get("years") or 0)
        return p, p.get("years") or 0, [], "plain"
    p = _person(family, roster)
    if not p:
        return None
    if family == "pm":
        return (p, p.get("years") or 0, [], "pm") if SOFTWARE_RE.search(text) else None
    skills = skills_in(text, p.get("skills") or [])[:3]
    return p, p.get("years") or 0, skills, "skills" if skills else "plain"


def job_fit(job, roster):
    """(fit, None) or (None, reason) for one jobs_snapshot entry."""
    family = role_family(job.get("title"))
    text = f"{job.get('title') or ''}\n{job.get('excerpt') or ''}"
    picked = _pick(job, family, text, roster) if family else None
    if not picked:
        return None, "no_roster_match"
    person, years, skills, line = picked
    wanted = required_years(text)
    if wanted and wanted > years + MAX_YEARS_GAP:
        return None, "years_gap"
    if location_blocker(text, job.get("location")):
        return None, "location_blocker"
    return {"job": job, "family": family, "name": person.get("name"), "role": person.get("role") or "engineer",
            "years": years, "years_text": (person.get("years_text") if line != "qa" else None) or f"{years} years",
            "skills": skills, "line": line}, None


def best_fit(jobs, roster):
    """(fit, None) for the company's best posting, or (None, reason). Most matched skills wins, then the
    snapshot order (fitting jobs first, then score)."""
    key = (json.dumps(roster, sort_keys=True),
           tuple((j.get("url"), j.get("title"), j.get("location"), hash(j.get("excerpt") or "")) for j in jobs or []))
    if key not in _fit_cache:
        fits, reasons = [], []
        for job in jobs or []:
            fit, reason = job_fit(job, roster)
            if fit:
                fits.append(fit)
            else:
                reasons.append(reason)
        if fits:
            _fit_cache[key] = (max(fits, key=lambda f: len(f["skills"])), None)
        else:
            _fit_cache[key] = (None, next((r for r in reasons if r != "no_roster_match"), "no_roster_match"))
    return _fit_cache[key]
