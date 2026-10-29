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
| Leads | Post-scrape enrichment: top decision makers per company with email, personal emails and phone through a chain of free-tier providers | `scripts/enrich_leads.py`, `scripts/lead_providers.py` |
| Ops | Daily server run at 18:00 Karachi time with a 3-week job cycle and an evening summary email | `scripts/daily_run.py`, `deploy/` |
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

## 9. Lead enrichment step

`scripts/enrich_leads.py` runs after the scraper and turns the scored job list
into a contact list of decision makers. It groups `all_jobs.json` by company,
ranks companies by best `lead_intent_score`, then total score, then job count,
and enriches the top N (default 40 with a best score of 4 or more). For each
company it keeps the top `--people` (default 3) decision makers: C-level and
founders regardless of function, plus VP, head and director roles in
engineering, technology or product.

### Providers

All optional. A provider is used only when its key is in `.env`, and every
credit-spending call is counted in `scripts/output/provider_quota.json` so the
free allowance is never exceeded across runs. A 401, 402, 403 or 429 disables
that provider for the rest of the run.

| Provider | Env key | Free tier (Oct 2026) | Used for |
| --- | --- | --- | --- |
| DuckDuckGo | none | unlimited, rate-limited | company site, LinkedIn results |
| Brave Search | `BRAVE_API_KEY` | $5 credit/mo, about 1,000 queries | same, when DDG blocks |
| Apollo | `APOLLO_API_KEY` | people search free; about 75 enrichment credits/mo | names and titles at the domain; work and personal emails |
| Hunter | `HUNTER_API_KEY` | 50 credits/mo, verify = 0.5 | domain search with pattern, email finder, verifier |
| Tomba | `TOMBA_API_KEY` + `TOMBA_SECRET` | 25 searches + 50 verifications/mo | domain search, email finder, verifier, phone finder |
| ZeroBounce | `ZEROBOUNCE_API_KEY` | 100 verifications/mo | verifying guesses |
| GitHub | `GITHUB_TOKEN` optional | 60 req/h, 5,000 with token | public profile emails |
| OpenRouter | `OPENROUTER_API_KEY` | about 50 req/day on free models | filtering candidates, reading team pages |

Snov.io, Lusha, RocketReach and Kaspr were considered and left out: none of
them offers API access on a free plan.

### Per company

1. Website domain from a search, skipping job boards, social sites and data
   brokers.
2. Candidates from DuckDuckGo and Brave LinkedIn results, Apollo people
   search, and Hunter or Tomba domain search when fewer than `--people`
   decision makers were found so far. Records for the same person are merged.
3. Published emails and phones from the company's home, contact, about, team
   and leadership pages.
4. One OpenRouter call to drop non-buyers and add people named with a senior
   technical title in the site text. Emails and ids already attached by
   providers are kept through this step.
5. For each decision maker, the email waterfall stops at the first source at
   or above `--min-confidence` (default 70):

   | Source | Confidence |
   | --- | --- |
   | verified by ZeroBounce, Hunter or Tomba | 95 |
   | exact address on the company site | 90 |
   | GitHub public profile | 85 |
   | Hunter or Tomba finder | provider score |
   | Apollo enrichment, with personal emails | 80 |
   | pattern guess, pattern confirmed or from a provider | 60 |
   | pattern guess, inferred from site addresses | 40 |
   | pattern guess, default `first.last` | 25 |

   Anything below 95 is then verified if a verifier has quota left. A valid
   result becomes 95, a catch-all domain becomes 55, a hard bounce moves to
   the next pattern guess, up to three.
6. Phone: a direct number from the provider record, else Tomba's phone finder
   when the email is at least a confirmed-pattern guess, else the company
   line, labelled `company line`.

### Output

`scripts/output/leads.csv`, one row per person, with `email_confidence`,
`email_status` (which sources and verifiers were involved), `personal_emails`,
`phone` and `phone_type`. `leads.json` has the same plus candidate counts and
the detected address pattern. Progress is cached in `enrich_cache.json`;
rerunning resumes, `--fresh` starts over. The run ends with a per-provider
usage table against the free caps.

### What free tiers can and cannot do

Direct mobile numbers are not available through any free API. What arrives is
direct lines that Hunter, Tomba or Apollo happen to carry, Tomba phone-finder
hits, and otherwise the company switchboard. Hunter's 50 credits cover about
15 companies of finder calls a month; after that, emails come from search,
GitHub, Apollo and verified pattern guesses. Expect 40 to 60 decision makers
with a credible email per month on free tiers, and a company line for every
company. Caps can be raised with `CAP_<PROVIDER>` once credits are bought.

```
pip install ddgs
cp .env.example .env    # fill in what you have
python scripts/enrich_leads.py --top 40 --min-score 4 --people 3
```

Tests: `scripts/tests/test_enrich_leads.py` covers ranking, parsing, decision
maker selection, pattern guessing, the waterfall and verification logic with
fake providers, and the quota tracker. No network needed.

## 10. Daily run on the server

`scripts/daily_run.py` runs the pipeline once a day on the Oracle instance and
emails a summary. systemd starts it at 18:00 Asia/Karachi
(`deploy/jba-daily.timer`), which is 8 or 9 AM in New York. The full plan,
including the outreach stages still to come, is in `docs/outreach-plan.md`.

Each step is a child process with its own log and timeout. A failed step never
stops the summary, and a crash still sends a short email with the error.

| Step | What it does | When |
| --- | --- | --- |
| outreach | sends the day's emails from the outreach queue | once `scripts/outreach.py` exists |
| topup | spends finder and verifier credits on the next leads in line | once `scripts/outreach.py` exists |
| scrape | `scraper.py --source automated --max-jobs-per-provider 1500` | when the job cycle is due |
| merge | `merge_data.py` | after a successful scrape |
| enrich | `enrich_leads.py`, resuming from its cache | every day |
| ingest | loads the day's leads into the outreach queue | once `scripts/outreach.py` exists |

**Job cycle.** A scrape runs when the last successful one is 21 days old
(`CYCLE_DAYS`), or sooner when the email queue has nothing left to send or
verify, but never within 7 days of the previous one (`CYCLE_MIN_DAYS`).
`--force-scrape` overrides it once.

**Enrichment.** Until the outreach module exists, enrichment runs as before:
the top 40 companies, spending provider credits itself. Once it exists,
enrichment runs `--discover-only --top 0`: every company at score 4 or more,
free sources only, at most 110 minutes a day, resuming the next evening from
its cache. Credits are then spent by the outreach top-up, just ahead of
sending.

**Summary email.** Steps and timings, the job cycle, the scrape per ATS, the
shortlist, enrichment results, people ready to email, free-tier usage and
warnings, with the day's leads CSV attached. SMTP settings and the recipient
live in `.env` (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`,
`SMTP_FROM`, `SUMMARY_TO`); all other settings are listed in `.env.example`.

**Files**, all under the gitignored `scripts/output/`:

- `runs/YYYY-MM-DD.json`: the run record (steps, cycle, summary, email status)
- `runs/YYYY-MM-DD.summary.txt`: the summary as text, written even without SMTP
- `runs/YYYY-MM-DD.leads.json` and `.leads.csv`: the day's leads
- `logs/daily-YYYY-MM-DD.log` and one `logs/daily-YYYY-MM-DD.<step>.log` per step

Runs and logs older than 90 days are deleted.

Install on the server (Ubuntu, Python 3.11 or newer), as the normal user:

```
git pull
bash deploy/install.sh
nano .env                                   # SMTP_* and SUMMARY_TO
.venv/bin/python scripts/daily_run.py --test-email
sudo systemctl start jba-daily.service      # first run now; the timer takes over after
journalctl -u jba-daily -f
```

Other commands:

```
python scripts/daily_run.py --skip scrape,merge --no-email --print   # quick run on existing data
python scripts/daily_run.py --resend --date 2026-10-28               # mail a stored summary again
systemctl list-timers jba-daily.timer                                # next run time
```

Smaller changes that came with it: `scraper.py` writes `collected_jobs` and
`removed_jobs` to `metadata.json`. `enrich_leads.py` gained `--top 0` for every
company, `--discover-only`, `--cache-max-age-days` and `--max-minutes`, and
records `email_pattern_source` per company.

Tests: `scripts/tests/test_daily_run.py` covers the cycle rule, summary
numbers, run records, email building, config parsing and the step runner, and
`test_enrich_leads.py` gained cases for the new flags. No network needed.

## Running a scrape

```
pip install -r requirements.txt
python scripts/scraper.py --source manual --max-jobs-per-provider 200
```

Output lands in `scripts/output/`. `all_jobs.json` is sorted by
`lead_intent_score` descending, so the top of the file is the lead list.
