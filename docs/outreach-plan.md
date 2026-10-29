# Plan: daily lead pipeline + cold-email outreach on the Oracle server

> Status: approved 2026-10-29. Stage 0 is done (commit 94389d0). Stage 1 adds the daily run (`scripts/daily_run.py`, `deploy/`). Stages 2 to 4 are still to come.

## Context

The job-board-aggregator fork now scrapes jobs, scores lead intent, and (approved, written, still uncommitted) enriches the top companies with decision-maker emails and phones. Ali wants this to become a hands-off email marketing setup:

1. Run every day at 18:00 Asia/Karachi on the Oracle Ubuntu instance: scrape, score, enrich.
2. Email Ali a summary: everything scraped, the companies shortlisted by score, the people enriched and ready to email.
3. Cold-email those decision makers (copy grounded in Alex and Leila Hormozi's material, tied to the JD they posted), one follow-up a week later, warm-up ramp 5/day for a week, then 10/day, then 20/day.
4. Whenever a new sending inbox is added, run basic and advanced deliverability checks before it may send.

The email copy itself is a separate task (Ali: "extreme attention"). This plan builds the pipeline, scheduling, state, sending, warm-up, follow-up, reply/bounce handling and inbox checks, with the composer as a pluggable seam that is specified here but written and approved in its own session.

## What exists today (verified in the repo)

- The only daily run is `.github/workflows/scrape-jobs.yml`: `cd scripts && python scraper.py --source automated` → `python scripts/merge_data.py` (repo root) → jq trend snapshot → gzip → GitHub release. Enrichment is not wired in.
- `scripts/scraper.py`: args `--source automated|manual`, `--max-jobs-per-provider N`. Writes `scripts/output/all_jobs.json` (full records sorted by `lead_intent_score` desc; `lead_intent_matches` is `{bucket: bool}`; `description` is raw HTML), `metadata.json` (`total_jobs`, `recruiter_jobs`, `platforms`, …), company lists, chunks. A full run "takes many hours" (workflow timeout 360 min). In `save_results`, `original_count` (line 1450) and `cleaned_count` (1456) are in scope at the metadata dict (1569–1577). Importing `scraper.py` has side effects (loads `locations.json`, prints), so nothing new imports it.
- `scripts/merge_data.py`: no args, relative paths, must run from the repo root.
- `scripts/enrich_leads.py` (uncommitted): `--input --top 40 --min-score 4 --people 3 --min-confidence 70 --no-llm --fresh`. Writes `scripts/output/leads.json` (per company: `company_key` = the job's `company` field, `company, ats, max_score, total_score, job_count, top_titles, job_urls, domain, people[], company_emails, company_phones, email_pattern, enriched_at`, or `error`), `leads.csv`, `enrich_cache.json`, `provider_quota.json`. Person: `name, title, linkedin, sources, email, email_confidence (95 verified, 90 site, 85 github, 80 apollo, 60/40/25 guesses), email_status, personal_emails?, phone, phone_type`. Reusable, side-effect-free imports: `load_dotenv`, `ENV_FILE`, `OUTPUT_DIR`, `JOBS_FILE`, `LEADS_JSON`, `LEADS_CSV`, `rank_companies`, `build_providers`, `norm_name`, `split_name`, `seniority`, `strip_html`. `QuotaTracker.summary(buckets)` in `scripts/lead_providers.py` returns `{bucket: {used, cap, period}}`. `OpenRouterClient.__init__` makes a network call; the orchestrator must not construct it.
- `.gitignore`: `scripts/output/`, `.env`, `*.log`, and a global `*.json` rule with tracked exceptions under `data/`. Consequences: `data/talent.json` (Stage 4) stays private automatically, and test fixtures must be inline Python, not `.json` files.
- `requirements.txt`: `requests`, `pandas==3.0.2`, `ddgs`. No Python file imports pandas; pandas 3 needs Python ≥ 3.11, which matters for the install script.
- No logging, SMTP, IMAP, DNS or scheduler code anywhere.

Reusable patterns from sibling projects (copy shapes, not paths):
- `MyWork/leadgen-pipeline/leadgen/reminders.py`: stdlib smtplib + `EmailMessage`, `SMTP_SSL` on 465 else STARTTLS, `SMTP_HOST/PORT/USER/PASS`.
- `MyWork/leadgen-pipeline/leadgen/store.py`: SQLite `conn()` context manager, `executescript(SCHEMA)` + `_migrate` on `PRAGMA user_version`, `usage(day, kind)` cap table.
- `MyWork/outreach-suite/outreach-drafter/src/checks.js`: dependency-free copy checks (banned AI words, dash regex, generic-subject regexes) to port into the content lint.
- Hormozi material for Stage 4: `MyWork/leadgen-pipeline/docs/script-rationale.md` (25 sources: $100M Leads cold-outreach transcript links, Leila Hormozi Build ep. 223, value equation, "big fast value", third-grade reading level), `docs/offer-recommendations.md`, `config/script.yaml`. No roster of Ali's engineers exists anywhere yet.

External facts checked 2026-10-28:
- Oracle Cloud blocks outbound TCP 25 for tenancies created after 2021-06-23; authenticated submission on 587/465 is the normal path (`nc -zv smtp.zoho.com 465` on the box to confirm egress).
- Google Workspace app passwords still work with 2-Step Verification on; Google has no removal date. Microsoft 365 basic SMTP AUTH is unchanged until end of Dec 2026, then disabled by default, OAuth after. So: password auth now, one small function to swap in XOAUTH2 later.
- Zoho Mail (Ali's inbox): IMAP/POP/SMTP client access is not on the Forever Free plan; it needs Mail Lite or higher, and IMAP access must also be switched on per user (Zoho Mail → Settings → Mail Accounts → IMAP Access). With two-factor on, an app-specific password is needed (Zoho Accounts → Security → App Passwords). Hosts for the US data center: `smtp.zoho.com` 465 SSL or 587 STARTTLS, `imap.zoho.com` 993; some custom-domain orgs are on `smtppro.zoho.com`/`imappro.zoho.com`, and EU/IN accounts use `.eu`/`.in`; the exact hosts are shown in Zoho Mail → Settings → Mail Accounts. SPF is `v=spf1 include:zohomail.com ~all` (older `include:zoho.com`; `include:one.zoho.com` when several Zoho services send). The DKIM selector is whatever was chosen in the Zoho admin console (commonly `zmail`), and signing must be turned on after the record verifies. Zoho's IMAP folder names are `Sent` and `Spam`. Zoho's own outbound limit is reputation-based, 50–500 external mails per hour per the admin help, far above 20 a day.
- Gmail/Yahoo/Outlook: SPF + DKIM + DMARC alignment required; RFC 8058 one-click unsubscribe is mandatory only above 5,000/day; complaint rate must stay under 0.3%.
- systemd `OnCalendar` accepts a timezone suffix (`*-*-* 18:00:00 Asia/Karachi`); verify with `systemd-analyze calendar` on the instance.

## Architecture

```
systemd timer 18:00 Asia/Karachi  (= 08:00/09:00 New York, 13:00/14:00 London)
  └─ scripts/daily_run.py  (one process; steps as subprocesses; flock; per-step logs; always ends with the summary)
       1. outreach   python scripts/outreach.py run --today D --stats-out runs/D.outreach.json     ← sends FIRST, from the queue in outreach.db
       2. topup      python scripts/outreach.py topup --today D --stats-out runs/D.topup.json       ← spends finder/verifier credits on the next leads in line, just in time
       3. scrape     cd scripts && python scraper.py --source automated --max-jobs-per-provider N  ← ONLY when the job cycle is due (see below)
       4. merge      python scripts/merge_data.py                                                   ← only after a scrape
       5. enrich     python scripts/enrich_leads.py --discover-only --top 0 --min-score 4 --people 3 --cache-max-age-days 21 --max-minutes 110   ← daily, free sources only, resumes from cache
       6. ingest     python scripts/outreach.py ingest --leads runs/D.leads.json --jobs scripts/output/all_jobs.json --today D --stats-out runs/D.ingest.json
       7. summary    built in-process → SMTP → Ali, leads-D.csv attached; run record written
```

Two queues, two clocks:
- **Job queue (every 3 weeks, or sooner when the email queue runs dry).** The scrape runs when the last successful scrape is 21 days old (`CYCLE_DAYS`), or earlier when today's top-up reports the email queue exhausted (no sendable lead left and no unresolved lead with an open job left to verify), but never less than 7 days after the previous scrape (`CYCLE_MIN_DAYS`, so an empty week does not hammer the ATS sites daily). Everything the scrape finds that meets the score floor is enriched, not just 40 companies. Discovery costs nothing: DuckDuckGo, Brave when configured (its own capped quota), Apollo's people search (0 credits), the company site, GitHub, LinkedIn title parsing, pattern inference, and the OpenRouter cleanup for the first 50 companies a day (its free limit). So `enrich_leads.py --discover-only --top 0` runs every evening inside a 110-minute budget and the per-company cache lets it resume where it stopped, finishing a 400-company cycle in two to four evenings. Discovered people land in the queue with whatever address quality the free sources gave (site 90, GitHub 85, pattern guess 25–60).
- **Email queue (daily).** `outreach.db` is the durable queue; it outlives any scrape. The `topup` step is the only thing that spends the limited credits (Hunter, Tomba, Apollo enrichment, ZeroBounce): each day it takes the next leads in queue order (best company score first, then the most senior person) whose job was still open in the latest scrape and who have no address at 90 or above, and runs the existing finder-and-verify waterfall on them until the queue holds three days of sends at the current cap (`OUTREACH_TOPUP_DAYS`) or every provider is out of credits for the period. It verifies one person per company at a time, because only one person per company is mailed per 90 days. Spending credits at the pace of sending, on the best leads first, stretches the free tiers (about 300 finder and verifier credits a month in total) as far as they go; after warm-up they, not jobs, are the likely limit (decision 15).

Until Stage 2 exists there is no queue, so the enrich step runs as it does today (`--top 40`, credits spent inside enrichment) and only the 21-day clock drives the scrape. Once `scripts/outreach.py` exists the orchestrator switches enrichment to `--discover-only --top 0`.

## Where the data lives

| What | Where | Lifetime |
|---|---|---|
| Scraped jobs | `scripts/output/all_jobs.json` (plus merged `data/chunks/`) | replaced at every scrape |
| Per-company discovery (people, site emails, phones, pattern) | `scripts/output/enrich_cache.json` | entries reused for 21 days, so a cycle resumes after any interruption |
| The cycle's discovered companies | `scripts/output/leads.json` / `leads.csv`, archived daily as `runs/YYYY-MM-DD.leads.{json,csv}` | rewritten every evening; archives pruned after 90 days |
| **The email queue**: one row per person with every candidate address and its verification, the chosen sending address, phone, a snapshot of the job posting, and the full contact history (sends, replies, bounces, unsubscribes, your own replies) | `scripts/output/outreach.db` (SQLite, a few MB even at thousands of leads) | permanent; survives every scrape; copied nightly with SQLite's online backup to `scripts/output/backups/outreach-YYYY-MM-DD.db`, 14 kept |
| Free-tier usage | `scripts/output/provider_quota.json` | per provider period |
| Nightly run record and summary | `scripts/output/runs/YYYY-MM-DD.json`, `.summary.txt`, `.queue.csv` | 90 days |

Everything is under the gitignored `scripts/output/`. `outreach.db` is the one file that must never be lost, because it is what stops anyone being emailed twice; the nightly backup covers it, and copying `scripts/output/backups/` off the box occasionally covers the instance itself.

Outreach runs first because the scrape takes hours and cold email should land at a fixed weekday morning hour. 18:00 Karachi is 8–9 AM on the US East Coast and early afternoon in the UK. The summary therefore reports "sent today", the queue's health, and "ready for tomorrow".

| Stage | Delivers | New files |
|---|---|---|
| 0 | Commit the enrichment step already written | none (command is on Ali's clipboard) |
| 1 | Orchestrator with the 3-week job cycle, run record, summary email, systemd timer, Oracle install script; four small flags and one output field added to `enrich_leads.py` | `scripts/daily_run.py`, `scripts/tests/test_daily_run.py`, `deploy/jba-daily.service`, `deploy/jba-daily.timer`, `deploy/install.sh`, `docs/outreach-plan.md` (this plan, saved in the repo) |
| 2 | Outreach queue + sender: ingest, just-in-time credit top-up, eligibility, warm-up caps, first touch + 7-day follow-up, IMAP reply/bounce/unsubscribe handling, auto-pause, off/review/send modes, template composer + content lint | `scripts/outreach.py`, `scripts/outreach_store.py`, `scripts/outreach_mail.py`, `scripts/outreach_compose.py`, `scripts/tests/test_outreach.py` |
| 3 | Inbox onboarding: `add-inbox` runs basic + advanced checks (DNS, blocklists, domain age, site, SMTP/IMAP login, seed-inbox placement); weekly re-check; only `ready` inboxes send | `scripts/inbox_check.py`, tests added to `test_outreach.py` |
| 4 | Hormozi-grounded LLM composer, roster of Ali's engineers, approved copy; separate session | `outreach_compose.py` LLM path, `data/talent.json`, `docs/email-rationale.md` |

Every stage is previewed file by file and approved before anything is written (CLAUDE.md). Each stage is independently useful: Stage 1 alone gives the nightly summary.

## Stage 0: commit the enrichment step

`daily_run.py` imports from `enrich_leads.py`, so the uncommitted enrichment files go in first. Command already on the clipboard:
`cd ~/Documents/MyWork/job-board-aggregator && git add .env.example requirements.txt README-changes.md scripts/enrich_leads.py scripts/lead_providers.py scripts/tests/test_enrich_leads.py && git commit -m "Add lead enrichment step with free-tier provider chain" && git push`

## Stage 1: `scripts/daily_run.py` (orchestrator + summary)

Decisions:
- One stdlib-only module (`subprocess`, `fcntl`, `logging`, `smtplib`, `email`, `zoneinfo`, `dataclasses`, `json`). Imports `load_dotenv`, `rank_companies`, `build_providers` and the path constants from `enrich_leads.py`; `QuotaTracker` from `lead_providers.py`.
- Steps are `subprocess.Popen([sys.executable, "-u", ...])` with child stdout+stderr redirected to `scripts/output/logs/daily-YYYY-MM-DD.<step>.log`; the run record keeps the last 40 lines per step. Per-step timeouts via env `DAILY_*_TIMEOUT_MIN` (outreach 120, topup 30, scrape 300, merge 30, enrich 130, ingest 5). `enrich` stops itself at `--max-minutes` and writes `leads.json`; its timeout is only the backstop, and even a kill loses nothing because the cache is saved after every company. A SIGTERM handler kills the running child, marks remaining steps `killed`, and still writes the record and sends the summary.
- Job cycle: `cycle_due(previous_runs, topup_stats, today, cfg) -> (bool, reason)` is pure. Due when there is no earlier successful scrape, or the last one is ≥ `CYCLE_DAYS` (21) old, or today's top-up (which runs just before) reports `pool_exhausted` and the last scrape is ≥ `CYCLE_MIN_DAYS` (7) old. When not due the scrape and merge steps are `skipped` with the reason ("cycle day 9 of 21", "queue empty, next scrape allowed 2026-11-04") and the summary shows it. `--force-scrape` overrides.
- Dependencies: merge runs only if scrape exited 0 *and* `all_jobs.json`'s mtime is newer than the run start (a timed-out scraper never reaches `save_results`). Enrich and ingest run every day against the current `all_jobs.json`. Outreach, topup and ingest are `skipped` (not failed) while `scripts/outreach.py` does not exist. The summary always runs.
- Run record `scripts/output/runs/YYYY-MM-DD.json`, atomic (`.tmp` + `os.replace`): `run_date, started_at, finished_at, host, git_rev, config, steps[], files{leads_json, leads_csv, log}, summary{}, outreach{} | null, ingest{} | null, email{status, to, subject, error}`. Written before and after the email step. The day's `leads.json`/`leads.csv` are archived as `runs/YYYY-MM-DD.leads.{json,csv}`. Runs and logs pruned after 90 days. Sender state lives in SQLite (`scripts/output/outreach.db`, Stage 2); the two are coupled only through the stats files.
- Drop the jq trend snapshot and `check_anomalies.py` on the server (nothing consumes `data/trends/daily.jsonl` there). `compare_with_previous(by_ats, prev_by_ats)` warns when any ATS count falls more than 50% or to zero versus the previous run record.
- Scrape bound: `DAILY_MAX_JOBS_PER_PROVIDER` default 1500 plus the 300-minute timeout; calibrate from the first cycle's run record.
- Enrichment runs daily as `--discover-only --top 0 --cache-max-age-days 21 --max-minutes 110`. Small diffs in `enrich_leads.py`, each a few lines:
  - `--top 0` means every company at or above `--min-score`: `rank_companies` returns `ranked[:top] if top else ranked`.
  - `--discover-only`: in `enrich_company`, `discover_people` gets every provider except hunter and tomba (their domain searches cost credits; Apollo's people search is free and stays), and `resolve_email`/`verify_email`/`resolve_phone` get only `github`. A company then yields people, site and GitHub emails, the inferred pattern and a pattern guess, and spends nothing.
  - `--cache-max-age-days N`: `load_cache` skips entries whose `enriched_at` is older than N days, so each cycle rediscovers its companies.
  - `--max-minutes N`: the company loop stops at the deadline the same way the existing KeyboardInterrupt branch does, then writes `leads.json` and prints the usage table; the next evening resumes from the cache.
  - One extra field in the company record, `email_pattern_source`, so the top-up's pattern guesses get the right confidence.
  - With every company cached the step is a quick no-op that rewrites `leads.json`.
  - Tests added to `test_enrich_leads.py`: `rank_companies(top=0)` returns all; cache entries past the age are skipped; in discover-only mode a fake Hunter is never called.
- Two-line scraper diff in `save_results`: add `"collected_jobs": original_count, "filtered_jobs": cleaned_count` to the metadata dict so the summary can show scraped vs kept without parsing logs.
- Overlap lock: non-blocking `fcntl.flock` on `scripts/output/daily_run.lock`; exit 75 if held.

Functions (pure first; every one testable without network):
- `load_config(argv, env) -> DailyConfig`: `.env` via `load_dotenv`, env over defaults, CLI over env. CLI: `--date`, `--skip outreach,scrape,merge,enrich,ingest`, `--no-email`, `--resend --date D`, `--test-email`, `--print`.
- `run_step(name, cmd, cwd, timeout_s, log_path, stop_event) -> StepResult(name, status ok|failed|timeout|killed|skipped, returncode, started_at, duration_s, cmd, log, tail, skip_reason)`.
- `collect_inputs(cfg, steps)`: reads `all_jobs.json`, `metadata.json`, `leads.json`, quota summary (bucket list = provider names from `build_providers(QuotaTracker(), None)` plus `tomba_verify`/`openrouter` when configured), `runs/D.outreach.json`, `runs/D.topup.json`, `runs/D.ingest.json`, and the previous run records for the cycle; a missing file becomes a warning.
- `build_summary(jobs, leads, metadata, quota, *, min_score, top, ready_threshold, excluded_emails, previous, step_results) -> dict` (pure): `jobs{total, collected, filtered_out, by_ats, recruiter_jobs, companies_in_jobs, last_updated}`, `shortlist{min_score, count, companies[{company, ats, max_score, job_count, top_title}]}` from `rank_companies` so it matches what enrichment saw, `enrichment{companies, with_domain, errors, people, emails{verified ≥95, high 80–94, guessed <80, none}, phones, personal_emails_found, pattern_detected}`, `ready_to_email{threshold 90, count, people[{company, name, title, email, email_kind, email_confidence}]}` taken from the ingest stats' `next_up` once Stage 2 exists (before that, computed from `leads.json` at ≥ 90), `cycle{day, days, last_scrape, next_due, reason, companies_total, companies_discovered}`, `queue{sendable, unresolved, exhausted, days_of_sends_at_cap}`, `quota{}`, `warnings[]`, `durations_s{}`.
- `parse_enrich_warnings(log_text)` lifts the "quota used up / stopped early" lines; `email_tier(conf)`.
- `render_text`, `render_html` (escaped, three small tables, no CSS framework), `build_subject` → `[JBA] 2026-10-28: 5 sent, 30 ready, 40 shortlisted, 2 warnings`, `build_email(...) -> EmailMessage` (plain + HTML alternative; CSV attached when non-empty), `smtp_connect(cfg)` (465 → `SMTP_SSL`, else STARTTLS, timeout 30), `smtp_authenticate(server, cfg)` dispatching on `SMTP_AUTH=login|xoauth2` (xoauth2 raises NotImplemented in v1), `send_email(msg, cfg) -> {status, to, error}` that never raises. Without SMTP config the text summary is still written to `runs/YYYY-MM-DD.summary.txt`.
- `main()`: config → lock → logging (`scripts/output/logs/daily-YYYY-MM-DD.log` + stdout for journald) → SIGTERM handler → outreach → topup → (scrape → merge, when the cycle is due) → enrich → archive leads → ingest → inputs → summary → record → email → record → prune. Exit 0 if every non-skipped step succeeded and the email did not fail, else 1 (shows in `systemctl --failed`).

Summary email, top to bottom:
1. Date, total duration, exit status; a loud `PAUSED` banner when the sender paused an inbox.
2. Scrape: collected vs kept after the noise filter, per-ATS table, drop warnings vs the previous run.
3. Shortlist: how many companies scored ≥ 4, and the top 25 by score in a table (company, ATS, best score, job count, top title); the full list is in the attachment.
4. Job cycle and queue health: cycle day N of 21 and when the next scrape is due (or why it ran today); companies discovered so far out of the shortlist; people in the queue; sendable leads (a verified address, work or personal) and how many days of sends that covers at the current cap; unresolved candidates still waiting for credits; today's top-up (attempted, verified, which providers are out of credits until when); phones found.
5. Outreach: first touches and follow-ups sent today (person, company, subject); every reply received, with the person, company, title and the first 300 characters of what they wrote; the leads you answered by hand from Zoho ("you replied to"); bounces and unsubscribes; warm-up day and cap; drafts awaiting review in `review` mode; inbox status table. Then the running totals table with columns today / last 7 days / month to date / year to date and rows first touches, follow-ups, replies, reply rate, your replies, bounces, bounce rate, unsubscribes, companies contacted.
6. Ready for tomorrow: the next leads in queue order with a verified address (name, title, company, address kind), and a loud line when the queue is empty and the scrape is not yet allowed.
7. Free-tier usage table, warnings, step durations. Attachment: `queue-YYYY-MM-DD.csv` (every person in the queue with company, score, title, chosen address, address kind, confidence, phone and status: sendable, unresolved, contacted, replied, bounced, unsubscribed, no address) once Stage 2 exists; `leads-YYYY-MM-DD.csv` before that.

`deploy/`:
- `jba-daily.service`: `Type=oneshot`, `User=__USER__`, `WorkingDirectory=__REPO__`, `ExecStart=__REPO__/.venv/bin/python -u scripts/daily_run.py`, `TimeoutStartSec=20h`, `TimeoutStopSec=180`, `KillMode=mixed`, `Nice=10`, `After=network-online.target`. No `EnvironmentFile`: the script reads `.env`.
- `jba-daily.timer`: `OnCalendar=*-*-* 18:00:00 Asia/Karachi`, `Persistent=true`, `WantedBy=timers.target`. A run still active at the next 18:00 is skipped by systemd (logged); a second run on the same day is harmless because the sender's cap is per day.
- `install.sh` (idempotent, `set -euo pipefail`): picks `python3.12` if present (prints the apt hint otherwise), creates `.venv`, installs `requirements.txt`, copies `.env.example` to `.env` if missing and `chmod 600`, creates `scripts/output/{runs,logs,drafts}`, seds `__REPO__`/`__USER__` into `/etc/systemd/system/`, `daemon-reload`, `enable --now jba-daily.timer`, prints `systemctl list-timers jba-daily.timer`.

Tests `scripts/tests/test_daily_run.py` (unittest, inline fixtures): `cycle_due` (no history → due; day 20 → not due; day 21 → due; queue exhausted on day 5 → not due; exhausted on day 8 → due; `--force-scrape`); `rank_companies(top=0)` returns everything; `build_summary` on a 6-job / 3-company fixture (by_ats, shortlist order and `top_title`, tiers, ready count honoring threshold and excluded set, empty leads and `error` records give zeros, missing metadata gives `collected=None`); `compare_with_previous` flags a 100% drop; `parse_enrich_warnings`; run-record round trip leaves no `.tmp`; `build_email` yields plain+HTML and the CSV attachment with the right filename, none when the CSV is empty; `load_config` defaults under a scrubbed env; `run_step` with `sys.executable -c` exiting 3 → `failed` with tail, a 5 s sleep under a 0.5 s timeout → `timeout`.

## Stage 2: outreach store, sender, follow-ups, reply handling, composer seam

Files under `scripts/` (five, because the composer must be replaceable in isolation and only one file should know SQL):
- `outreach.py`: CLI (`ingest`, `topup`, `run`, `poll`, `status`, `add-inbox`, `check-inbox`, `list-inboxes`, `pause`, `resume [--restart-warmup]`, `suppress`), `run_step()`, `topup_step()`, `filter_eligible()`, `warmup_cap()`, send loop, stats.
- `outreach_store.py`: `connect(path)` context manager (copy of the leadgen `conn()` shape), `SCHEMA`, `_migrate`, query functions.
- `outreach_mail.py`: `smtp_login(inbox)`, `imap_login(inbox)` (the one auth seam), `new_message_id()`, `build_message()`, `send()`, `poll_inbox()`, `classify()`.
- `outreach_compose.py`: `compose()` dispatcher, v1 template, `full_text()` signature, `lint_copy()`, `lint_message()`.
- Reuses `load_dotenv, ENV_FILE, OUTPUT_DIR, norm_name, split_name, seniority, strip_html` from `enrich_leads.py`, and for the top-up its existing waterfall `resolve_email`, `verify_email`, `resolve_phone`, `build_providers` plus `QuotaTracker` from `lead_providers.py`. No enrichment logic is duplicated.

Credentials: the `inboxes` row stores hosts, ports, username and `pass_env` (the name of the `.env` variable holding the password, default `SMTP_PASS`). Passwords never enter the DB.

SQLite `scripts/output/outreach.db`:
- `inboxes(id, address UNIQUE, display_name, provider zoho|google|m365|other, smtp_host, smtp_port, imap_host, imap_port, username, pass_env, auth, dkim_selector, sent_folder, spam_folder, status pending|ready|paused, paused_reason, resumed_at, warmup_started_at, last_check_at, last_check_result JSON, imap_uidvalidity, imap_last_uid, sent_uidvalidity, sent_last_uid, created_at)`. `add-inbox --provider zoho` fills `smtp.zoho.com:465`, `imap.zoho.com:993`, `Sent`, `Spam` and `DKIM_SELECTOR`; `SMTP_HOST`/`IMAP_HOST` in `.env` override the hosts for the pro/EU/IN variants.
- `leads(id PK, person_key UNIQUE = company_key|name_key, company_key, company, domain, company_pattern, company_pattern_source, company_score, name, name_key, title, linkedin, seniority, candidate_emails JSON [{email, kind work|personal, confidence, status, source}], email (the one chosen sending address), email_kind, email_confidence, email_status, phone, phone_type, job_location, jobs_snapshot JSON, job_last_seen (date of the latest scrape that still listed this company), resolve_attempted_at, resolve_note, owner_contacted_at, first_seen, last_seen)`; indexes on `company_key`, `email`, and a partial index on `(resolve_attempted_at IS NULL)` for the top-up. A lead is a person, not an address, because the address can be found or change after discovery.
- `messages(message_id PK, lead_id, lead_email (the address actually used), company_key, inbox_id, kind first|followup, parent_id, subject, body_text, status draft|sending|sent|failed, outcome reply|bounce|ooo|unsubscribe|complaint|'', day (Karachi date, the cap key), sent_at, followup_due, draft_path, error)`; indexes on `(inbox_id, day, status)`, `lead_email`, `(company_key, sent_at)`, partial on `followup_due`.
- `events(id, inbox_id, kind reply|owner_reply|bounce|ooo|unsubscribe|complaint, message_id, lead_email, from_addr, subject, snippet, detail, imap_message_id, imap_uid, received_at, created_at, UNIQUE(inbox_id, imap_message_id))`.
- `suppression(value PK: 'a@b.com' or '@b.com', reason, source_event, created_at)`, `checks(id, inbox_id, ran_at, level, verdict, result JSON)`, `runs(day, started_at, finished_at, mode, stats JSON)`.
- Key queries: contacted emails = `messages.status IN ('sending','sent')`; contacted `(company_key, name_key)` pairs via join; company cooldown = companies with a sent message in the last 90 days; follow-ups due = `kind='first' AND status='sent' AND followup_due <= today` minus suppressed; cap used = `COUNT(*) WHERE inbox_id AND day=today AND status != 'failed'`; 7-day bounce rate = bounce events since `max(now-7d, resumed_at)` over sent messages in 7 days.

`ingest --leads --jobs --today --stats-out`: one pass over `leads.json`; for each company record without `error` and each person: `name_key = norm_name(name)`, `seniority(title)`, `company_pattern` from the record's `email_pattern`, `candidate_emails` = the work email (with its confidence and status) plus every `personal_emails` entry, `jobs_snapshot` = up to 3 of that company's jobs from `all_jobs.json` (title, url, location, true buckets in weight order technicalDebt 4 > hardSecurity 3 > highScale/dataIntegrity/manualArchitecture 2, `strip_html(description)[:1500]`), upsert by `person_key` keeping `first_seen` and never downgrading an address the top-up already verified. People with no address at all are stored too (the top-up's finders may produce one). Then `job_last_seen` is set to the scrape date from `metadata.json` for every lead whose company appears in the current `all_jobs.json`, including companies not yet rediscovered this cycle. Ingest also writes `runs/D.queue.csv` and takes the nightly `outreach.db` backup. Stats: `{new, updated, queue, queue_companies, sendable, unresolved, next_up[10 next sendable leads in queue order]}`. Storing the snapshot at ingest means `run` and `topup` never read `all_jobs.json` (memory on the free shape) and still have the JD when the job later disappears.

`topup --today --stats-out` (the only step that spends credits):
- Providers: `build_providers` from `.env` with the shared `QuotaTracker` (same `provider_quota.json`, so enrichment and top-up never exceed a free tier together).
- Target: `OUTREACH_TOPUP_DAYS (3) × today's cap` summed over ready inboxes, using the first warm-up step (5) in `review` mode. With `OUTREACH_MODE=off` the top-up is skipped, so no credit is spent before sending starts.
- Candidates, in queue order (company score desc, then seniority desc): job still open (`job_last_seen` equals the latest scrape date), no address at 90 or above, `resolve_attempted_at IS NULL`, company neither contacted nor cooling down, and only the most senior such person per company. The next person at a company becomes a candidate only when the previous one ended with no sendable address.
- Per candidate: drop the discovery-time guess so `resolve_email(person, domain, company, [], company_pattern, company_pattern_source, providers, min_conf=90)` runs Hunter, Tomba and Apollo and regenerates the pattern guess with its alternates; then `verify_email` walks up to three candidates. Apollo's enrichment already returns a personal address as the email when there is no work email (`Apollo.enrich` in `lead_providers.py`); if the chosen address still ends below 90, the remaining `personal_emails` are verified in order and the first `valid` one is taken. Then `resolve_phone`.
- `email_kind(email, company_domain)`: work when the address's registered domain matches the company's, otherwise personal.
- Write back `candidate_emails`, the chosen `email`/`email_kind`/`email_confidence`/`email_status`, `phone`, `resolve_attempted_at`, `resolve_note` (for example "hunter: quota used up; guessed first.last bounced; personal gmail verified").
- Stop early when every finder and verifier reports `disabled_reason` or `quota.allow` is false; `quota_exhausted[]` names them and the summary says when each comes back.
- Stats: `{attempted, verified_work, verified_personal, no_address, quota_exhausted[], target, sendable_after, unresolved_after, pool_exhausted}`. `pool_exhausted` is true only when no sendable lead and no unresolved open-job lead remain; leads merely waiting for next month's credits do not count as exhausted, because a new scrape would not help them.

`run --today --stats-out` (mode from `OUTREACH_MODE`):
1. Per inbox: `pending` → `inbox_not_ready`, no poll, no send. If `last_check_at` is older than 7 days, run the basic check level; a `fail` flips the inbox back to `pending` and lists it under `paused_inboxes` with reason `check_failed:<name>`.
2. IMAP poll (runs in every mode, including `off`): `SELECT INBOX` read-only, reset `imap_last_uid` if `UIDVALIDITY` changed, `UID SEARCH` above the last UID (first run: `SINCE` 14 days), `BODY.PEEK[]`, `classify()` → events (UNIQUE dedupes), then apply: reply → `outcome='reply'`, `followup_due=NULL`; bounce → suppress address (hard 5.x.x only), cancel follow-up; out-of-office → `followup_due += 7`; unsubscribe → suppress address (plus `@domain` when `OUTREACH_SUPPRESS_DOMAIN_ON_UNSUB=1`), cancel follow-up; complaint (`report-type=feedback-report`) → suppress and pause the inbox immediately. Matching: every `<...>` in `In-Reply-To`/`References` looked up in `messages` (our IDs look like `<24hex@sending-domain>`, also regexed out of DSN bodies), falling back to sender address → lead. Reply snippets are the first 300 characters of the reply with quoted text and signatures stripped, so the summary shows what the person actually wrote. If IMAP raises, that inbox sends nothing this run (`imap_down`): never send blind.
   Sent-folder poll (same connection, own UID cursor): any message in the inbox's `Sent` folder whose `Message-ID` is not ours and whose To/Cc contains a lead address is Ali replying by hand from Zoho. It becomes an `owner_reply` event, clears that lead's `followup_due`, sets `leads.owner_contacted_at`, and the lead leaves the automated sequence for good (eligibility reason `owner_contacted`). The summary lists these under "you replied to".
3. Bounce gate: pause when `bounced >= 3` or (`sent >= 20` and `bounced/sent > 3%`); `paused_reason` carries the numbers; paused inboxes are re-listed every run until `resume`.
4. `mode == off` → stop here for this inbox. Otherwise `cap_today = warmup_cap(inbox, today)` from `OUTREACH_WARMUP=5:7,10:7,20` walked cumulatively from `warmup_started_at`; `remaining = cap − used today` (so a second run the same day is a no-op).
5. Weekday gate in `OUTREACH_DEFAULT_TZ` (America/New_York; at 18:00 Karachi that is 8–9 AM there). No per-lead timezone guessing in v1; `job_location` is stored for later.
6. Follow-ups first, oldest due first, while `remaining > 0`: `compose(kind="followup")` → `lint_copy` → insert `followup` row with `parent_id` → `build_message(in_reply_to=parent)` → `lint_message` → send (`sending` → `sent`) or write `.eml` in `review` → clear the parent's `followup_due` → sleep `random.uniform(60, 180)` between real sends. An SMTP exception marks the row `failed` and stops this inbox's loop (a dead SMTP must not burn the cap).
7. First touches: pure `filter_eligible(leads, contacted_emails, contacted_names, recent_companies, suppression, today, settings) -> (eligible, skipped_reasons)` with reasons `unresolved (no verified address yet; the top-up will get to it) | role_address | suppressed | already_contacted | owner_contacted | company_cooldown | own_domain | job_closed (company absent from the latest scrape) | company_dup_in_run`. Each person has exactly one sending address, `leads.email`: the verified work address when there is one, otherwise the first personal address that passed verification; nobody is mailed at two addresses. Order by company `max_score` desc, then `seniority` desc, then `email_confidence` desc; one person per company. For each while `remaining > 0`: `compose(kind="first")` from `jobs_snapshot` → lint → insert `first` row with `followup_due = today + 7` → send or draft → sleep.
8. Return and write stats: `mode, sent_first, sent_followup, drafts_written, drafts[], replies[{name, company, title, email, subject, snippet, at, kind first|followup}], owner_replies[{name, company, email, at}], bounces, unsubscribes, ooo, complaints, paused_inboxes[{address, reason, since}], inboxes[{address, status, warmup_day, cap_today, used_today, bounce_rate_7d, imap_ok}], eligible_pool, companies_in_pool, skipped_reasons{}, sent_by_kind{work, personal}, unknown_state (rows stuck in 'sending' by a crash), periods{}, errors[], duration_s`. No inboxes configured → `inboxes: []`, no error (the summary says "Outreach: not configured").
   `periods` is `period_stats(store, today)`: one row each for `today`, `week` (last 7 days), `month` (month to date) and `ytd` (year to date), each with `first_sent, followup_sent, replies, reply_rate (replies ÷ first touches sent in the period), owner_replies, bounces, bounce_rate, unsubscribes, companies_contacted`, computed from `messages` and `events`. `python scripts/outreach.py status` prints the same table on demand, and `status --replies --since 2026-01-01` lists every reply received with its snippet.

Modes: `OUTREACH_MODE=off` (default; poll only), `review` (compose and write `.eml` drafts to `scripts/output/drafts/YYYY-MM-DD/NN-<company_key>-<kind>.eml`; drafts count toward the day's cap but not toward contact history, so a review week does not burn companies), `send`. `send` is flipped only after Stage 4's copy is approved; the v1 template exists for review mode and tests.

Headers per message: `Message-ID: <token_hex(12)@sending-domain>`, `Date`, `From: "Display Name" <inbox>`, `To: "Name" <email>`, `Subject`, `multipart/alternative` with `text/plain` and an HTML part that is the plain text in `<p>` tags only, `List-Unsubscribe: <mailto:UNSUBSCRIBE_ADDRESS?subject=unsubscribe>`. Deliberately absent: `Precedence: bulk`, `X-Mailer`, tracking pixels or links, `Reply-To`, `List-Unsubscribe-Post` (RFC 8058 needs an HTTPS endpoint; mailto-only is honest and Gmail still shows the unsubscribe link). Follow-ups: new `Message-ID`, `In-Reply-To` and `References` = parent, `Subject: Re: <parent subject>`.

Composer seam: `compose(person, company, job, inbox, kind, settings) -> {subject, body}` returns copy only; `full_text(body, settings)` appends the RFC 3676 `-- ` delimiter and the signature (`SENDER_NAME`, `AGENCY_NAME`, `SENDER_ADDRESS_LINE`, opt-out sentence naming `UNSUBSCRIBE_ADDRESS`), so no composer can drop compliance text. `OUTREACH_COMPOSER=template|llm` picks the implementation; v1 ships `template` only. v1 shape (placeholders, not final copy): first touch = greeting by first name, one hook line naming the JD title and the top matched bucket, one line of what drawnonward offers, one low-friction question; follow-up = two sentences bumping the thread. The later LLM composer gets a `ComposeInput` (`person{name, title}`, `company`, `job{title, url, location}`, `buckets`, `excerpt`, `kind`, `parent_subject`, `settings`) and its output passes the same lint, falling back to the template on failure (`llm_lint_fallback` counted).

`lint_copy(subject, body, kind) -> issues` (blocks on any error): unfilled `{...}`; 50–120 words for `first`, 20–120 for `followup`; subject ≤ 6 words, no all-caps word, no `!`, no `Re:` on first touch; ≤ 1 URL; spam words (free, guarantee(d), act now, limited time, no obligation, risk-free, 100%, $$$, click here, urgent, winner); em/en dashes; banned AI words ported from checks.js; opt-out line and address line present after `-- `; warn on generic subjects (connect, intro, reaching out, quick question, partnership, opportunity). `lint_message(msg)`: `From` has a display name, exactly plain + HTML parts, HTML contains only `<p>`/`<br>` and text (no `<img>`, no `<script>`, at most the one link), `List-Unsubscribe` present, follow-ups carry `In-Reply-To`.

Tests `scripts/tests/test_outreach.py` with fakes (`FakeSMTP` recorder, `FakeIMAP` fed raw RFC822 bytes, injected `now`/`today`/`sleep`, tempfile DB) and inline byte fixtures (Gmail DSN with `Status: 5.1.1`, auto-reply with `Auto-Submitted: auto-replied`, a normal reply with `In-Reply-To`, an "unsubscribe" subject, a feedback-report): `filter_eligible` every reason, one-per-company most senior first; `warmup_cap` on days 0/6/7/13/14 and env parsing; follow-up due at +7, cancelled by reply/bounce/unsubscribe, +14 after OOO; `classify` on each fixture, DSN beats `Auto-Submitted`, unsubscribe beats reply; a Sent-folder message to a lead becomes `owner_reply`, cancels the follow-up and sets `owner_contacted_at`; `period_stats` over a seeded DB gives today/week/month/ytd counts and rates; reply snippet stripping removes quoted text and signatures; auto-pause at 3 bounces, none under the 20-sent floor, no re-pause after `resume`; `lint_copy` one assertion per rule; `lint_message`; threading headers and the Message-ID shape; `run_step` end to end in `review` writes N drafts, a second call the same day writes zero, IMAP exception → zero sends and `imap_down` counted, `mode=off` still ingests a reply fixture; `ingest` upsert keeps `first_seen`, builds `jobs_snapshot` and never downgrades a verified address; `topup` with the `FakeProvider` pattern from `test_enrich_leads.py`: stops at the target, resolves only the most senior person per company and moves to the next person after a no-address result, skips closed jobs, falls through to a personal address when the work guesses bounce, records `quota_exhausted` when every provider is disabled, reports `pool_exhausted` only when nothing sendable or resolvable is left, and never touches already-attempted or contacted leads; `ingest` sets `job_last_seen` for companies present in the jobs file and leaves absent ones alone.

Edge cases handled: `leads.json` missing (poll and follow-ups still run); the same address appearing under two companies (the contacted-address check mails it once); re-enrichment changing an email (the `(company_key, name_key)` check blocks a second touch); reboot mid-send (a `sending` row counts as contacted and is reported as `unknown_state`); soft bounces (`4.x.x`) cancel the follow-up but do not suppress; the provider rewriting our `Message-ID` (detected by the seed test; the sender-address fallback still classifies replies); replies landing in the sending inbox's Spam folder are not polled in v1.

## Stage 3: inbox onboarding checks (`scripts/inbox_check.py`)

DNS helpers: `doh(name, rtype)` via Cloudflare `https://cloudflare-dns.com/dns-query` (JSON) with Google `https://dns.google/resolve` as fallback, for MX/TXT (follows CNAME for DKIM). `dnsbl(name)` via `socket.gethostbyname` on the system resolver because Spamhaus refuses public resolvers; its `127.255.255.254` answer is reported as `warn: resolver refused`. No new dependency.

| Check | Method | Verdict |
|---|---|---|
| `check_mx` | DoH MX | none → fail |
| `check_spf(domain, provider)` | DoH TXT `v=spf1` | ≠ 1 record, missing provider include (zoho: any of `zohomail.com`, `zoho.com`, `one.zoho.com` and their `.eu`/`.in` variants; google `_spf.google.com`; m365 `spf.protection.outlook.com`), not ending `~all`/`-all`, > 10 lookups (one level of include recursion) → fail |
| `check_dkim(domain, selector)` | DoH TXT `<selector>._domainkey`; selector from `DKIM_SELECTOR` (Zoho: the name chosen in the admin console, `zmail` if unset; google `google`; m365 `selector1`+`selector2`) | no `p=` → fail |
| `check_dmarc` | DoH TXT `_dmarc` | missing → fail; `p=none` → warn |
| `check_domain_age` | RDAP `https://rdap.org/domain/<domain>` registration event | < 30 days → fail; RDAP error → warn |
| `check_site` | HTTPS then HTTP GET, no redirects followed | not 200/301/302 → fail |
| `check_domain_bl` | socket: `dbl.spamhaus.org`, `multi.surbl.org`, `multi.uribl.com` | listed → fail |
| `check_ip_bl(ip)` | socket: reversed IP at `zen.spamhaus.org`, `b.barracudacentral.org`, `bl.spamcop.net` | listed → fail (warn on Google/M365 shared IPs) |
| `check_smtp_login`, `check_imap_login` | `outreach_mail.smtp_login/imap_login` + NOOP/SELECT | exception → fail |
| `check_content` | compose both kinds for a fixture person, `lint_copy` + `lint_message` | any error → fail |
| `seed_test` (advanced) | below | any seed in spam or any auth ≠ pass → fail; one seed in spam → warn |

Seed test: `SEED_INBOXES="addr:imap_host:PASS_ENV,..."` (Ali's own Gmail, Outlook and Yahoo accounts). Send one production-shaped message per seed through `build_message` with subject `Deliverability check <token>`, wait 90 s, log in to each seed over IMAP, search the token in INBOX then in the junk folder (`[Gmail]/Spam`, `Junk Email`, `Bulk Mail`, `Spam`), up to 3 attempts 60 s apart; parse the top `Authentication-Results` for `spf=/dkim=/dmarc=`, take the sending IP from `Received` and run `check_ip_bl`, record whether our `Message-ID` survived. `--mail-tester <address>` sends one message and prints the result URL for Ali to open (free, 3/day; not scraped).

Flow: `add-inbox --address --display-name --provider [--pass-env]` inserts the row and immediately runs the advanced level, as Ali asked. `ready` requires basic pass/warn **and** the seed test, unless `--basic-only` is passed explicitly (for example before `SEED_INBOXES` exists). `warmup_started_at` is set the first time an inbox becomes `ready`. `run_checks(inbox, level) -> {ran_at, level, verdict, checks[{name, status, detail}], seeds[{seed, folder, spf, dkim, dmarc, ip, ip_listed, message_id_kept}]}` is stored in `checks` and on the inbox row and printed as a pass/warn/fail table. The daily `run` repeats the basic level weekly; Ali runs `check-inbox --advanced` monthly or after any bounce spike.

Tests (same file): `check_spf` on two records / missing include / `+all` / 11 lookups via a recursive fake, `check_dmarc` `p=none` warn and missing fail, `check_dkim` via CNAME, blocklist "listed" vs `gaierror`, RDAP age parsing, seed result parsing from a fixture header block.

## Stage 4: Hormozi-grounded composer (separate session; design only)

Inputs: `ComposeInput` from Stage 2; a roster `data/talent.json` (gitignored by the `*.json` rule; copied to the server like `.env`) with per-engineer `name_or_alias, skills[], shipped[] (one-line proofs), seniority, availability`; Hormozi sources already listed in `MyWork/leadgen-pipeline/docs/script-rationale.md` plus any books or transcripts Ali drops into `docs/sources/`.
Principles to encode (from $100M Leads cold outreach and $100M Offers): personal first line that proves we read the JD; "big fast value" in one sentence (two matching profiles this week, no commitment); value equation for the offer line (dream outcome = seat filled with someone who has shipped this; likelihood = proof lines from the roster; time = this week; effort = reply with one word); third-grade reading level; one call to action; follow-up adds new value instead of repeating. Leila Hormozi's hiring content frames the problem (cost of an open senior seat, talent density).
Mechanics: JD → roster match by skill overlap picks the two profiles the email alludes to; 2–3 subject and body variants, tracked in a `messages.variant` column for reply-rate comparison; every output passes `lint_copy`; template fallback on failure; the LLM never sees anything outside `ComposeInput` and the roster, so it cannot invent facts. Model: `OpenRouterClient` (free, quota bucket `openrouter`) is the zero-cost default; Claude Haiku or Sonnet via the Anthropic API is the quality recommendation at 20 emails a day (cents per day). Process: rationale doc → variants → Ali approves the copy → one `review` week → `send`.

## Config: `.env` keys (all optional; `.env.example` lists them with empty values)

```
# summary + sending inbox (shared)
SUMMARY_TO=                       # comma list; the nightly summary goes here
SMTP_HOST=smtp.zoho.com  SMTP_PORT=465  SMTP_USER=  SMTP_PASS=     # Zoho app-specific password; use the smtppro/.eu/.in host if Zoho shows one
IMAP_HOST=imap.zoho.com  IMAP_PORT=993  SENT_FOLDER=Sent  SPAM_FOLDER=Spam
SMTP_FROM=  SMTP_AUTH=login
# orchestrator
DAILY_TZ=Asia/Karachi  DAILY_MAX_JOBS_PER_PROVIDER=1500  DAILY_SKIP_STEPS=
CYCLE_DAYS=21  CYCLE_MIN_DAYS=7        # scrape every 3 weeks, or sooner (but not under 7 days) when the email queue is empty
DAILY_OUTREACH_TIMEOUT_MIN=120  DAILY_TOPUP_TIMEOUT_MIN=30  DAILY_SCRAPE_TIMEOUT_MIN=300  DAILY_MERGE_TIMEOUT_MIN=30  DAILY_ENRICH_TIMEOUT_MIN=130  DAILY_INGEST_TIMEOUT_MIN=5
DAILY_ENRICH_TOP=0  DAILY_ENRICH_MIN_SCORE=4  DAILY_ENRICH_PEOPLE=3  DAILY_ENRICH_CACHE_DAYS=21  DAILY_ENRICH_MAX_MINUTES=110  DAILY_ENRICH_NO_LLM=0
READY_EMAIL_CONFIDENCE=90
# outreach
OUTREACH_MODE=off                 # off|review|send
OUTREACH_MIN_CONFIDENCE=90  OUTREACH_WARMUP=5:7,10:7,20  OUTREACH_GAP=60-180  OUTREACH_FOLLOWUP_DAYS=7
OUTREACH_COMPANY_COOLDOWN_DAYS=90
OUTREACH_BOUNCE_PAUSE_PCT=3  OUTREACH_BOUNCE_MIN_SENT=20
OUTREACH_ALLOW_PERSONAL=1  OUTREACH_TOPUP_DAYS=3  OUTREACH_SUPPRESS_DOMAIN_ON_UNSUB=0  OUTREACH_COMPOSER=template  OUTREACH_DEFAULT_TZ=America/New_York
SENDER_NAME=  AGENCY_NAME=drawnonward  SENDER_ADDRESS_LINE=  UNSUBSCRIBE_ADDRESS=   # alias of the sending inbox so one poll sees it
DKIM_SELECTOR=                    # the selector name shown in the Zoho admin console (often zmail)
SEED_INBOXES=                     # addr:imap_host:PASS_ENV,...
SEED_PASS_1=  SEED_PASS_2=
```

## Docs

- `README-changes.md`: section 10 "Daily run on the server" (what runs when, the summary, install steps, where logs and run records live, how to re-send a summary) and section 11 "Outreach and inbox checks" (modes, warm-up, follow-up, what the poller does, `add-inbox`/`check-inbox`, how to pause/resume/suppress, how the 3-week job cycle and the daily credit top-up work, what is deliberately not sent to: role addresses and unverified addresses).
- `.env.example`: the keys above.
- `requirements.txt`: unchanged (stdlib + requests). Optional one-line cleanup, flagged below: drop the unused `pandas` pin.

## Verification

Local (repo root unless noted):
1. `cd scripts && python -m unittest tests.test_daily_run tests.test_outreach tests.test_enrich_leads -v` in the scratch venv (`requests`, `ddgs` installed).
2. Small fixture: `cd scripts && python scraper.py --source manual --max-jobs-per-provider 30`, then `python scripts/daily_run.py --skip outreach,scrape,merge --no-email --print`; inspect `scripts/output/runs/<date>.json`, `.summary.txt`, `.leads.csv`, the step logs.
3. SMTP vars set: `python scripts/daily_run.py --test-email`, then `--resend --date <date>`; open in Gmail (HTML) and as plain text; confirm the CSV attachment.
4. Failure path: `DAILY_SCRAPE_TIMEOUT_MIN=0.2 python scripts/daily_run.py --skip outreach --no-email` → scrape `timeout`, merge/enrich/ingest `skipped`, exit 1. Overlap: two runs at once → the second exits 75.
5. Outreach: `OUTREACH_MODE=review python scripts/outreach.py run --today <date>` against a fixture pool with a fake inbox row; open the `.eml` drafts in Mail (thread, plain/HTML, signature, List-Unsubscribe). `python scripts/inbox_check.py dns <your sending domain> --provider zoho` passes SPF/DKIM/DMARC once Zoho's records are published; a parked domain fails age/site/DKIM.
6. `git status` shows nothing new under `scripts/output/`.

Oracle instance:
1. `free -m` (if ≤ 1 GB RAM, start with `DAILY_MAX_JOBS_PER_PROVIDER=500`), `python3 --version`, `nc -vz smtp.zoho.com 465`.
2. `git pull && bash deploy/install.sh`; `systemctl cat jba-daily.service`; `systemctl list-timers jba-daily.timer` shows the next 18:00 PKT; `systemd-analyze calendar "*-*-* 18:00:00 Asia/Karachi"`.
3. Zoho: confirm the plan is Mail Lite or above, enable IMAP access (Zoho Mail → Settings → Mail Accounts), create an app-specific password, copy the SMTP/IMAP hosts Zoho shows there; `nc -vz smtp.zoho.com 465`; fill `.env`; `.venv/bin/python scripts/daily_run.py --test-email`.
4. First run by hand: `sudo systemctl start jba-daily.service`, `journalctl -u jba-daily -f`, `tail -f scripts/output/logs/daily-*.scrape.log`; read `runs/<date>.json` durations and tune the cap and timeouts. Day two: confirm the scrape was skipped ("cycle day 1 of 21"), `enrich` resumed from the cache, and `topup` verified at least three days of sends or named the providers that are out of credits.
5. Inbox: `add-inbox --address ... --provider zoho` and read the check table (DMARC `p=none` is an acceptable warn in week one; anything red blocks); `--mail-tester` once; `list-inboxes` shows `ready` and `warmup_started_at`.
6. One week in `review` reading the drafts in the summary; after Stage 4 copy approval flip `OUTREACH_MODE=send`; day one `status` shows `used_today <= 5` and no `send_error`; day eight confirm a follow-up threads correctly in a seed inbox added as a fixture lead.

## Decisions for Ali (defaults taken; say so to change any)

1. **Sending inbox: your existing Zoho inbox** (your call, taken as given). Requirements: a paid Zoho plan (Mail Lite or above; the free plan has no IMAP/SMTP client access), IMAP access switched on for the user, an app-specific password, DKIM enabled in the Zoho admin console. Zoho's own outbound limit (50–500 external mails per hour, reputation-based) is far above our 20 a day. Caution: this puts the main domain's reputation on the line, so the verified-only floor, the caps and the auto-pause are not optional. If you later want to shield the main domain, a secondary domain on Zoho is one `add-inbox` away after 2–4 weeks of DNS aging (optionally with a paid warm-up service first; the seed check measures placement, it does not warm).
2. **Personal emails are fair game** (your call, taken as given). One address per person: the verified work address when there is one, otherwise the first personal address that passes verification. One sentence of caution: consumer inboxes report spam more readily and UK/EU personal addresses carry more legal exposure than US ones, so the complaint auto-pause stays non-negotiable. The summary shows sends by address kind so you can compare reply rates.
3. **Confidence floor 90 for every address, work or personal.** Only verified or site-confirmed addresses are mailed; guesses never are. Bounce rate is the single biggest deliverability lever.
4. **Outreach before the scrape** at 18:00 PKT (8–9 AM New York). Moving the send hour later means a second timer; worth it only if reply data says so.
5. **Follow-ups count toward the daily cap** and go first; weekdays only; 60–180 s between sends.
6. **One person per company, 90-day company cooldown, and only jobs still open in the latest scrape.** A second person after silence is a later rule once reply rates exist.
7. **Bounce pause** at 3 bounces, or > 3% after 20 sent; complaints pause immediately. `resume` is manual.
8. **Unsubscribe** is mailto-only (`List-Unsubscribe`) plus a plain opt-out sentence and the physical address. RFC 8058 one-click would need a 10-line HTTPS endpoint on the agency site; add later if wanted.
9. **Summary goes through the sending inbox** by default (one real message a day to a reader who opens it helps warm-up). `SUMMARY_TO` decides the recipient.
10. **Drop the jq trend snapshot** on the server; the run records are the history.
11. **Optional cleanup:** remove `pandas==3.0.2` from `requirements.txt` (nothing imports it; it blocks installs on Python 3.10).
12. **Stage 4 model:** OpenRouter free (zero cost) vs Claude via the Anthropic API (better copy, cents a day). Decide when Stage 4 starts.
13. **Job cycle: 21 days, or sooner when the email queue is empty, never under 7 days** (your rule, taken as given with the 7-day floor added). "Empty" means no sendable lead left and no unresolved candidate left for the top-up to verify. With 20 sends a day a 400-company cycle lasts roughly a month, so the 21-day clock will usually fire first.
14. **Enrich everything, verify just in time.** Every company at or above score 4 is discovered with free sources over the first evenings of a cycle; the limited credits are spent by the daily top-up on the next leads in queue order, three days ahead of sending. If you later buy credits, raise `CAP_<PROVIDER>` and `OUTREACH_TOPUP_DAYS` and nothing else changes.
15. **Capacity after warm-up (estimate).** At 20 a day with follow-ups first, the steady state is about 10 first touches a day, roughly 220 a month. The free tiers hold about 300 finder and verifier credits a month, and a verified lead costs one to three of them, so they cover roughly 100 to 250 sendable leads a month. Expect verification credits, not jobs, to be the limit from about week three; the summary's "days of sends left" line shows it early, and one paid verifier tier is the cheapest fix.
16. **Finder order unchanged:** Hunter, then Tomba, then Apollo. Work addresses are tried first because they reply better and bounce less; Apollo, the only personal-email source, runs when the first two come up empty.

## Implementation order and working rules

Stage 0 (Ali runs the commit, or says "you do it") → Stage 1 → Stage 2 → Stage 3, then Stage 4 in its own session. For each stage: full preview of every file and every edit to existing files (`scraper.py` two lines, the `enrich_leads.py` flags and their tests, `README-changes.md`, `.env.example`) → explicit approval → write → run the unit tests → commit message `Add <stage>` drafted and copied to the clipboard; no `git add`/`git commit` by me unless told "you do it". Nothing under `scripts/output/`, `.env` or `data/talent.json` is ever committed.
