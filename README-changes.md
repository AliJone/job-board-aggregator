# Fork changes: lead-intent scoring

This repo is a fork of [Feashliaa/job-board-aggregator](https://github.com/Feashliaa/job-board-aggregator).
The upstream project is a job board for job seekers: it scrapes seven ATS
platforms, classifies seniority, and serves the result as a static site.

This fork repurposes the same scraper as a **lead source**. Instead of asking
"which jobs could I apply to", it asks "which companies are hiring engineers to
solve the kind of problems a software agency can take on". Every job is scored
on its description for signals of hard backend work, security requirements, and
technical debt, and the output is sorted so the strongest leads come first.

Upstream's `README.md` is unchanged and still describes the base project. This
file covers only what was added or changed on top of it.

## Summary of changes

| Area | Change | Files |
| --- | --- | --- |
| Scraping | Only tech-role titles are kept, matched against a curated title list | `scripts/scraper.py`, `data/tech_job_titles_filtered.csv` |
| Scraping | Full job descriptions are collected from all seven ATS platforms | `scripts/scraper.py` |
| Scraping | iCIMS jobs now carry a real location instead of "Not specified" | `scripts/scraper.py` |
| Scoring | Lead-intent scoring with six keyword buckets, a veto bucket, and score-sorted output | `scripts/scraper.py` |
| Frontend | Scoring mirrored in the browser; vetoed jobs are hidden client-side | `js/lead_intent_scoring.js`, `js/filters.js` |
| Frontend data | `description` and `lead_intent_score` added to the chunk payload | `scripts/scraper.py` |
| Ops | `--max-jobs-per-provider` flag for short test runs | `scripts/scraper.py` |
| Ops | Atomic JSON writes that survive read-only or foreign-owned files | `scripts/scraper.py` |
| Ops | Progress logging reports kept vs dropped counts | `scripts/scraper.py` |
| Tooling | Throwaway scripts used while building description collection | `extract_ats.py`, `description_tester.py`, `test_description_collection.py` |
| Tests | Unit tests for scoring, write safety, and the provider cap | `scripts/tests/`, `tests/` |
| Repo | History rewritten to drop 6 GiB of committed scrape output; gitignore fixed | `.gitignore` |

## 1. Tech-title filter

**What.** `data/tech_job_titles_filtered.csv` holds 1,710 rows of
`SOC_Code,Occupation,Job_Title`. 41 of the occupation groups come from the
O*NET computer and mathematical occupations (Software Developers, Information
Security Analysts, Database Architects, Data Scientists, and so on). A 42nd
group, "Custom Added Tech Titles", holds 61 hand-added short forms such as
`Backend`, `SRE`, `DevSecOps`, `Tech Lead`, `CTO`, `Head of Data`.

At startup `load_tech_titles()` lowercases every occupation name and job title
into one set of 1,446 keywords. `is_tech_role(title)` returns true when any
keyword appears as a substring of the scraped title. Every ATS fetcher calls it
before normalising a job and counts the drops.

**Why.** Upstream indexes over a million jobs across every function. For lead
generation only engineering, data, security and IT roles matter, and the
description collection below costs one HTTP request per job on four of the
seven platforms. Filtering on title first cuts both the request volume and the
output size to the roles that can actually be scored.

**Behaviour to know.** The filter fails open: if the CSV is missing, every
title is kept. Matching is plain substring, so a short custom keyword like
`IT`, `AI` or `Data` will match inside unrelated words. Those three are the
loosest entries in the custom group.

## 2. Description collection per ATS

Upstream only stored titles and locations. Scoring needs the body text, so
each fetcher now populates a `description` field.

| Platform | How the description is fetched | Extra requests |
| --- | --- | --- |
| Greenhouse | `?content=true` added to the board API call; `content` field | none |
| Ashby | `descriptionPlain` added to the GraphQL selection | none |
| Lever | `descriptionPlain` plus every `lists[].content` block plus `additionalPlain`, joined | none |
| BambooHR | One call to `/careers/{id}/detail`; reads `description`, `content` or `jobDescription` | 1 per job |
| Workday | One call to the job's `externalPath` detail endpoint; reads `jobPostingInfo.jobDescription` | 1 per job |
| iCIMS | Fetches the job page with `?in_iframe=1` to skip the portal shell, slices the HTML between the `iCIMS_JobContent` div and the `iCIMS_JobOptions` footer, strips `<script>` and `<style>` | 1 per job |
| Paylocity | Fetches the detail page; tries JSON-LD `description`, then the `pageData` blob, then a `job-description` div | 1 per job |

Per-job requests sleep 0.1 to 0.3 seconds between calls so the extra traffic
does not trip the rate limits upstream already tuned worker counts for. All
detail fetches are wrapped in a bare `try/except` so a failed description never
drops the job.

**iCIMS location.** Upstream hard-coded iCIMS locations to "Not specified"
because the sitemap carries no location. Since the job page is now fetched
anyway, two regexes pull the "Job Location" value out of it and pass it through
`enrich_location()` for remote detection and coordinates.

## 3. Lead-intent scoring

`evaluate_lead_intent(job)` in `scripts/scraper.py` concatenates title,
description and company, normalises the text (HTML entities unescaped twice,
zero-width characters removed, tags replaced with spaces, whitespace collapsed,
lowercased) and runs it against six regex buckets.

| Bucket | Weight | Signal it captures | Examples |
| --- | --- | --- | --- |
| `highScale` | +2 | Concurrency, distributed systems, throughput | distributed systems, rate limiting, worker threads |
| `dataIntegrity` | +2 | Pipelines, sync, transactional guarantees | data ingestion, schema migrations, audit trail |
| `hardSecurity` | +3 | Auth, tenancy, webhook and identity controls | multi-tenant isolation, OAuth 2.0, row-level security |
| `technicalDebt` | +4 | Legacy, rewrites, urgency, founding-stage pressure | technical debt, legacy monoliths, zero to one |
| `manualArchitecture` | +2 | Single-word architecture terms | gRPC, Kafka, RBAC, ACID |
| `noiseFilter` | -10 and veto | Low-value or no-code work | Webflow, WordPress, landing page, thin wrapper, Zapier |

Per bucket the Python scorer adds `weight × min(hits, 3)`, so a description
that mentions three distinct technical-debt phrases scores 12 from that bucket
alone. The noise bucket adds a flat -10 and sets `is_excluded`.

`clean_job_data()` now runs the scorer on every job after the upstream
title/url/company checks. Jobs flagged by the noise bucket are dropped and
counted under a new `low_signal` reason. Survivors get two new fields,
`lead_intent_score` and `lead_intent_matches`, and both the full output and
the frontend chunks are sorted by score descending, then company, then title.

**Why these weights.** Technical debt is weighted highest because a company
admitting to a legacy rewrite is the most direct buying signal for outside
engineering help. Security is next because it is rarely optional and rarely
done in-house by small teams. Scale and data work are broad enough that they
earn points but do not decide the ranking on their own. The noise bucket is a
veto rather than a penalty because a Webflow or WordPress role indicates a
company that buys marketing sites, not systems work.

## 4. Frontend changes

- `js/lead_intent_scoring.js` is a browser-side copy of the buckets and
  weights, exporting `evaluateLeadIntent()`.
- `js/filters.js` imports it and drops any job whose title or description
  hits the noise bucket, in addition to the existing filters.
- `FRONTEND_FIELDS` in the scraper now includes `description` and
  `lead_intent_score`. Descriptions are tag-stripped and truncated to 1,500
  characters before chunking to limit payload growth.

The browser scorer is intentionally simpler than the Python one: it adds the
bucket weight once per matched bucket with no hit multiplier. The two therefore
return different numbers for the same job. See the test status below.

## 5. Pipeline and operational changes

- **`--max-jobs-per-provider N`.** `fetch_all_jobs()` stops collecting once a
  platform has yielded N jobs. Used for short local runs, since a full run with
  description fetching takes many hours.
- **`write_json_file()`.** Every JSON write in the scraper goes through a
  temp-file-then-`os.replace` helper. This was added after runs on the Ubuntu
  instance failed to overwrite output files that had been left read-only or
  owned by another user by an earlier run. The helper also raises a clear
  `RuntimeError` instead of a bare `PermissionError`.
- **Progress logging.** Per-company lines are gone. Progress is printed at
  powers of two, every 250 companies, and at the end, as a single line of kept
  tech jobs, dropped non-tech jobs and inactive companies. The final summary
  reports the same split.
- **Comment cleanup.** Stale browser-console snippets and inline comments were
  removed from the scraper while editing.

## 6. Throwaway scripts in the repo root

These were used while working out how each ATS exposes descriptions. They are
not part of the pipeline.

- `extract_ats.py` prints one sample job per ATS from the last
  `scripts/output/all_jobs.json`. The path is hard-coded to
  `/home/ubuntu/job-board-aggregator`.
- `description_tester.py` is a template for probing detail endpoints with
  placeholder slugs.
- `test_description_collection.py` is the same probe with real BambooHR,
  Workday and iCIMS examples filled in. The BambooHR and iCIMS endpoints it
  targets are not the ones the scraper ended up using.

## 7. Tests and current status

| Test | Result on 2026-10-11 | Note |
| --- | --- | --- |
| `scripts/tests/test_lead_intent_scoring.py` (2 tests) | pass | Run from `scripts/` with `requests` installed |
| `scripts/tests/test_scraper_io.py::test_overwrites_read_only_existing_file` | pass | |
| `scripts/tests/test_scraper_io.py::test_fetch_all_jobs_respects_per_provider_limit` | **fail** | The fake fetcher returns a 3-tuple, but fetchers now return 4 values including the dropped count |
| `tests/lead_intent_scoring.test.js` noise veto | pass | Needs `node --experimental-default-type=module --test`, or a `package.json` with `"type": "module"` |
| `tests/lead_intent_scoring.test.js` manual architecture +2 | **fail** | Expects 2, gets 5. The sample text also matches `role-based access` in `hardSecurity` (+3) |

Run the Python tests from the `scripts/` directory so `scraper` is importable:

```
cd scripts && python -m unittest tests.test_lead_intent_scoring tests.test_scraper_io
```

## 8. Repository housekeeping

The pipeline was run on an Ubuntu instance from July to October 2026 with a
local job that committed a daily "Trend snapshot". Those commits included
`data/all_jobs.json.gz`, which gzip cannot delta-compress, so 65 snapshots grew
the history to 6 GiB and GitHub refused the push. On 2026-10-11 the history
was rewritten with `git filter-repo` to remove `data/` from every commit, the
data-only snapshot commits were pruned, and the tracked inputs upstream relies
on (company lists, salary shards, `locations.json`, `trends/daily.jsonl`) were
re-added in a single commit. `.gitignore` now excludes `data/*.json.gz`.

## Running a scrape

```
pip install -r requirements.txt
python scripts/scraper.py --source manual --max-jobs-per-provider 200
```

Output lands in `scripts/output/`. `all_jobs.json` is sorted by
`lead_intent_score` descending, so the top of the file is the lead list.
