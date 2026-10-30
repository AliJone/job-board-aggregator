"""Local API server: runs scripts/scraper.py on demand and streams the results
to the frontend as Server-Sent Events, one company at a time.

    pip install -r requirements.txt
    python server.py            # http://localhost:8000

POST /api/scrape               {"companies": {"greenhouse": ["gitlab", ...]}, "filters": ["swe", ...],
                                "search": {"title": "software engineer", ...}}  (search optional;
                                keys in job_filters.SEARCH_FIELDS)
GET  /api/scrape/<id>/events   SSE stream: log, company, jobs, done
GET  /api/jobs                 jobs from the latest run (live while it runs)
GET  /api/config               platforms, filters, default and picker company lists
GET  /api/companies?q=stri     search every company in data/ by name
GET  /api/my-list              My list, most matches first
POST /api/my-list              {"companies": {"greenhouse": ["gitlab"]}}  add with source "you"
DELETE /api/my-list/<platform>/<id>

POST /api/scrape also takes "roles" (list of job_filters.ROLES keys; empty = any role),
"auto_add" (bool, default true: add companies with
matching jobs to My list) and "discover" (0-200: also scrape that many companies
not on My list and not tried in the last 30 days).
"""
import html
import io
import json
import os
import re
import sys
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from flask import Flask, Response, abort, jsonify, request, send_from_directory

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT_DIR, "scripts"))
import scraper  # noqa: E402
import job_filters  # noqa: E402
import my_list  # noqa: E402

scraper.SOURCE_TYPE = "manual"

PLATFORMS = {
    "greenhouse": (scraper.fetch_company_jobs_greenhouse, "GREENHOUSE"),
    "lever": (scraper.fetch_company_jobs_lever, "LEVER"),
    "ashby": (scraper.fetch_company_jobs_ashby, "ASHBY"),
    "bamboohr": (scraper.fetch_company_jobs_bamboohr, "BAMBOOHR"),
    "workday": (scraper.fetch_company_jobs_workday, "WORKDAY"),
    "icims": (scraper.fetch_company_jobs_icims, "iCIMS"),
    "paylocity": (scraper.fetch_company_jobs_paylocity, "PAYLOCITY"),
}

DEFAULT_COMPANIES = {
    "greenhouse": [
        "gitlab", "canonical", "wikimedia", "mozilla", "elastic", "grafanalabs",
        "bitwarden", "consensys", "vercel", "netlify", "mattermost", "turing",
    ],
    "lever": ["binance"],
}

# Software/tech companies offered in the frontend picker. Each had a live job
# board with open roles when added; edit freely.
TECH_COMPANIES = {
    "greenhouse": [
        "affirm", "airbnb", "airtable", "anthropic", "asana", "bitwarden", "brex",
        "canonical", "chime", "circleci", "cloudflare", "cockroachlabs", "coinbase",
        "consensys", "databricks", "datadog", "discord", "dropbox", "duolingo",
        "elastic", "figma", "fivetran", "gitlab", "grafanalabs", "gusto", "honeycomb",
        "instacart", "intercom", "jetbrains", "launchdarkly", "lyft", "mattermost",
        "mongodb", "mozilla", "netlify", "newrelic", "okta", "pagerduty", "pinterest",
        "planetscale", "reddit", "robinhood", "samsara", "scaleai", "squarespace",
        "stripe", "sumologic", "tailscale", "toast", "turing", "twilio", "vercel",
        "wikimedia", "xai", "zscaler",
    ],
    "lever": ["binance", "outreach", "palantir", "shieldai", "spotify", "toptal", "zoox"],
}

COMPANY_FILES = {
    "greenhouse": scraper.GREENHOUSE_FILE, "lever": scraper.LEVER_FILE,
    "ashby": scraper.ASHBY_FILE, "bamboohr": scraper.BAMBOOHR_FILE,
    "workday": scraper.WORKDAY_FILE, "icims": scraper.ICIMS_FILE,
}
MAX_SEARCH_RESULTS = 50
MAX_DISCOVER = 200

# Saved in data/ (untracked via *.json in .gitignore); no workflow writes it.
MY_LIST = my_list.MyList(os.path.join(ROOT_DIR, "data", "my_list.json"), DEFAULT_COMPANIES)

FRONTEND_FIELDS = {
    "title", "company", "company_slug", "description", "location", "url", "ats",
    "skill_level", "is_recruiter", "workplaceType", "scraped_at", "remote",
    "coords", "salary", "updated_at", "posted_at", "first_seen", "lead_intent_score",
}

LIVE_JOBS_FILE = os.path.join(scraper.OUTPUT_DIR, "live_jobs.json")
STATIC_FILES = {"index.html", "styles.css", "favicon.ico"}
STATIC_DIRS = {"js", "docs"}


def load_salary_lookup():
    path = os.path.join(ROOT_DIR, "data", "salary", "salary_lookup.json")
    if not os.path.exists(path):
        return {}, {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("primary", {}), data.get("fallback", {})


SALARY_PRIMARY, SALARY_FALLBACK = load_salary_lookup()

_company_index = None


def company_index():
    """[(platform, slug, label)] for every company in data/, loaded on first search."""
    global _company_index
    if _company_index is None:
        index = []
        for platform, path in COMPANY_FILES.items():
            with open(path, encoding="utf-8") as f:
                index.extend((platform, slug, slug) for slug in json.load(f))
        with open(scraper.PAYLOCITY_FILE, encoding="utf-8") as f:
            index.extend(("paylocity", r["guid"], r.get("name") or r["guid"]) for r in json.load(f) if r.get("guid"))
        _company_index = index
    return _company_index


_discover_order = None


def discover_order():
    """Discovery candidates in a fixed shuffled order, built on first use."""
    global _discover_order
    if _discover_order is None:
        _discover_order = my_list.shuffled(
            [(p, s) for p, s, _ in company_index() if p in my_list.DISCOVER_PLATFORMS])
    return _discover_order


def dead_keys():
    return {my_list.key(p, s) for p in my_list.DISCOVER_PLATFORMS for s in scraper.load_dead_slugs(p)}


def parse_companies(raw):
    """Validate {platform: [ids]} from a request. Returns (companies, error)."""
    if not isinstance(raw, dict) or not raw:
        return None, "companies must be a non-empty object of {platform: [ids]}"
    companies = {}
    for platform, ids in raw.items():
        if platform not in PLATFORMS:
            return None, f"unknown platform '{platform}'"
        if not isinstance(ids, list) or not ids:
            return None, f"companies.{platform} must be a non-empty list of ids"
        for company_id in ids:
            if not isinstance(company_id, str) or not company_id.strip():
                return None, f"companies.{platform} has an empty or non-string id"
        companies[platform] = [i.strip() for i in ids]
    return companies, None


def to_frontend(job):
    """Same shaping save_results() applies before writing chunks."""
    company = (job.get("company") or "").lower().strip()
    title = (job.get("title") or "").lower().strip()
    level = job.get("skill_level", "mid")
    job["salary"] = SALARY_PRIMARY.get(f"{company}|{title}|{level}") or SALARY_FALLBACK.get(f"{title}|{level}")
    job["lead_intent_score"] = int(job.get("lead_intent_score", 0))

    slim = {k: job.get(k) for k in FRONTEND_FIELDS if k in job}
    if slim.get("description"):
        # Greenhouse content arrives HTML-escaped, so unescape before stripping tags.
        text = html.unescape(html.unescape(str(slim["description"])))
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
        slim["description"] = text[:1500]
    return slim


# ============================================================
# Runs
# ============================================================


class Run:
    def __init__(self, companies, enabled, search, auto_add=True, my_keys=(), new_keys=(), roles=()):
        self.id = uuid.uuid4().hex[:12]
        self.companies = companies
        self.enabled = enabled
        self.roles = list(roles)
        self.search = search
        self.auto_add = auto_add
        self.my_keys = set(my_keys)    # on My list when the run started
        self.new_keys = set(new_keys)  # picked by discovery
        self.added = 0
        self.events = []
        self.jobs = []
        self.done = False
        self.cond = threading.Condition()
        self.started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def emit(self, kind, data):
        with self.cond:
            self.events.append((kind, data))
            self.cond.notify_all()


class LogTee(io.TextIOBase):
    """Forwards scraper print() output to the run's event stream, line by line."""

    def __init__(self, run, original):
        self.run, self.original, self.buf = run, original, ""
        self.lock = threading.Lock()

    def write(self, s):
        self.original.write(s)
        with self.lock:
            self.buf += s
            *lines, self.buf = self.buf.split("\n")
        for line in lines:
            if line.strip():
                self.run.emit("log", {"line": line})
        return len(s)

    def flush(self):
        self.original.flush()


current_run = None
run_lock = threading.Lock()


def process_company(run, platform, slug, jobs, status_code, dropped):
    """Clean, score and filter one company's jobs, then push the matches."""
    scraped = len(jobs)
    cleaned = scraper.clean_job_data(jobs) if jobs else []
    matched = []
    for job in cleaned:
        role_ok, role_reason = job_filters.evaluate_role(job, run.roles)
        if not role_ok:
            continue
        result = job_filters.evaluate(job)
        if job_filters.passes(result, run.enabled):
            slim = to_frontend(job)
            if not job_filters.matches_search(slim, run.search):
                continue
            slim["filter_reasons"] = {k: result[k][1] for k in run.enabled}
            if run.roles:
                slim["filter_reasons"]["role"] = role_reason
            matched.append(slim)

    # A request error (no status) says nothing about the company, so it is not
    # recorded as tried; it may be picked again next run.
    added = False
    if status_code is not None or matched:
        added = MY_LIST.record_result(platform, slug, len(matched), run.auto_add)

    k = my_list.key(platform, slug)
    tag = "my list" if k in run.my_keys else "new" if k in run.new_keys else ""
    with run.cond:
        run.jobs.extend(matched)
        run.added += added
    run.emit("company", {
        "platform": platform, "slug": slug, "status": status_code, "tag": tag, "added": added,
        "scraped": scraped, "non_tech_dropped": dropped, "matched": len(matched),
    })
    if matched:
        run.emit("jobs", {"jobs": matched})


def scrape_platform(run, platform, slugs):
    fetcher, label = PLATFORMS[platform]

    def fetch_and_stream(slug):
        result = fetcher(slug)
        try:
            process_company(run, platform, *result)
        except Exception:
            print(f"  ! {platform}/{slug}: {traceback.format_exc(limit=1).strip()}")
        return result

    scraper.fetch_all_jobs(set(slugs), fetch_and_stream, label)


def execute(run):
    tee = LogTee(run, sys.stdout)
    sys.stdout = tee
    try:
        print(f"Run {run.id}: {sum(len(s) for s in run.companies.values())} companies, "
              f"roles: {', '.join(run.roles) or 'any'}, filters: {', '.join(run.enabled) or 'none'}, search: {json.dumps(run.search) if run.search else 'none'}")
        if run.companies.get("paylocity"):
            scraper.load_paylocity(scraper.PAYLOCITY_FILE)  # fills company names for GUIDs
        with ThreadPoolExecutor(max_workers=len(run.companies)) as pool:
            futures = [pool.submit(scrape_platform, run, p, s) for p, s in run.companies.items()]
            for f in futures:
                f.result()
        run.jobs.sort(key=lambda j: (-j.get("lead_intent_score", 0), (j.get("company") or "").lower()))
        scraper.write_json_file(LIVE_JOBS_FILE, {"last_updated": run.started_at, "jobs": run.jobs})
        print(f"Run {run.id} finished: {len(run.jobs)} matching jobs")
        print(f"{run.added} companies added to My list")
        run.emit("done", {"matched": len(run.jobs), "added": run.added, "my_list_size": len(MY_LIST.keys())})
    except Exception as e:
        traceback.print_exc()
        run.emit("done", {"matched": len(run.jobs), "error": str(e)})
    finally:
        sys.stdout = tee.original
        with run.cond:
            run.done = True
            run.cond.notify_all()


# ============================================================
# HTTP
# ============================================================

app = Flask(__name__)
app.json.sort_keys = False  # keep ROLES in their listed order for the dropdown


@app.get("/")
def index():
    return send_from_directory(ROOT_DIR, "index.html")


@app.get("/<path:path>")
def static_files(path):
    # Only the frontend is served; the repo root also holds .env and data.
    if path in STATIC_FILES or path.split("/", 1)[0] in STATIC_DIRS:
        return send_from_directory(ROOT_DIR, path)
    abort(404)


@app.get("/api/config")
def config():
    return jsonify({
        "platforms": list(PLATFORMS),
        "filters": job_filters.FILTERS,
        "roles": {k: label for k, (label, _) in job_filters.ROLES.items()},
        "default_companies": MY_LIST.companies(),
        "tech_companies": TECH_COMPANIES,
    })


@app.get("/api/companies")
def search_companies():
    q = request.args.get("q", "").strip().lower()
    if len(q) < 2:
        return jsonify({"error": "q must be at least 2 characters"}), 400
    hits = [c for c in company_index() if q in c[2].lower()]
    # Exact and prefix matches first, then shortest names.
    hits.sort(key=lambda c: (c[2].lower() != q, not c[2].lower().startswith(q), len(c[2])))
    return jsonify({
        "total": len(hits),
        "companies": [{"platform": p, "slug": s, "label": l} for p, s, l in hits[:MAX_SEARCH_RESULTS]],
    })


@app.post("/api/scrape")
def start_scrape():
    global current_run
    body = request.get_json(silent=True) or {}
    companies = {}
    my_keys = MY_LIST.keys()
    for platform, slugs in (body.get("companies") or MY_LIST.companies()).items():
        if platform not in PLATFORMS:
            return jsonify({"error": f"unknown platform '{platform}'"}), 400
        slugs = sorted({str(s).strip() for s in slugs if str(s).strip()})
        if slugs:
            companies[platform] = slugs
    auto_add = body.get("auto_add", True)
    if not isinstance(auto_add, bool):
        return jsonify({"error": "auto_add must be a boolean"}), 400
    discover = body.get("discover", 0)
    if not isinstance(discover, int) or isinstance(discover, bool) or not 0 <= discover <= MAX_DISCOVER:
        return jsonify({"error": f"discover must be a whole number from 0 to {MAX_DISCOVER}"}), 400

    scraping = {my_list.key(p, s) for p, slugs in companies.items() for s in slugs}
    _, tried = MY_LIST.snapshot()
    new = my_list.pick_new(discover_order(), discover, my_keys | scraping, tried, dead_keys())
    for platform, slug in new:
        companies.setdefault(platform, []).append(slug)
    if not companies:
        return jsonify({"error": "no companies given"}), 400
    filters = body.get("filters", list(job_filters.FILTERS))
    enabled = [k for k in filters if k in job_filters.FILTERS]
    search, error = job_filters.parse_search(body.get("search"))
    if error:
        return jsonify({"error": error}), 400
    roles, error = job_filters.parse_roles(body.get("roles"))
    if error:
        return jsonify({"error": error}), 400
    if "swe" in filters and "swe" not in roles:
        roles.append("swe")  # tabs opened before roles existed still send the old checkbox

    with run_lock:
        if current_run and not current_run.done:
            return jsonify({"error": "a scrape is already running", "run_id": current_run.id}), 409
        current_run = Run(companies, enabled, search, auto_add, my_keys,
                          [my_list.key(p, s) for p, s in new], roles)
        threading.Thread(target=execute, args=(current_run,), daemon=True).start()
    return jsonify({"run_id": current_run.id}), 202


@app.get("/api/my-list")
def get_my_list():
    entries, _ = MY_LIST.snapshot()
    return jsonify({"companies": entries})


@app.post("/api/my-list")
def add_to_my_list():
    companies, error = parse_companies((request.get_json(silent=True) or {}).get("companies"))
    if error:
        return jsonify({"error": error}), 400
    added = MY_LIST.add(companies, source="you")
    return jsonify({"added": added, "companies": MY_LIST.snapshot()[0]})


@app.delete("/api/my-list/<platform>/<path:company_id>")
def remove_from_my_list(platform, company_id):
    if platform not in PLATFORMS:
        return jsonify({"error": f"unknown platform '{platform}'"}), 400
    if not company_id.strip():
        return jsonify({"error": "company id must not be empty"}), 400
    if not MY_LIST.remove(platform, company_id):
        return jsonify({"error": f"{platform}/{company_id} is not on My list"}), 404
    return jsonify({"companies": MY_LIST.snapshot()[0]})


@app.get("/api/scrape/<run_id>/events")
def scrape_events(run_id):
    run = current_run
    if not run or run.id != run_id:
        abort(404)
    start = int(request.headers.get("Last-Event-ID") or 0)

    def stream():
        i = start
        while True:
            with run.cond:
                run.cond.wait_for(lambda: i < len(run.events) or run.done, timeout=15)
                batch, finished = run.events[i:], run.done
            if not batch:
                if finished:
                    return
                yield ": ping\n\n"
                continue
            for kind, data in batch:
                i += 1
                yield f"id: {i}\nevent: {kind}\ndata: {json.dumps(data)}\n\n"

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/jobs")
def jobs():
    run = current_run
    if run:
        with run.cond:
            return jsonify({"last_updated": run.started_at, "running": not run.done,
                            "run_id": run.id, "jobs": list(run.jobs)})
    if os.path.exists(LIVE_JOBS_FILE):
        with open(LIVE_JOBS_FILE, encoding="utf-8") as f:
            return jsonify({**json.load(f), "running": False})
    return jsonify({"last_updated": None, "running": False, "jobs": []})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
