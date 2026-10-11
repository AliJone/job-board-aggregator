import json

with open("/home/ubuntu/job-board-aggregator/scripts/output/all_jobs.json") as f:
    jobs = json.load(f)

ats_examples = {}
for job in jobs:
    ats = job.get("ats", "")
    if ats not in ats_examples:
        ats_examples[ats] = job

for ats, job in ats_examples.items():
    print(f"--- {ats} ---")
    print(f"  company_slug: {job.get('company_slug')}")
    print(f"  url: {job.get('url')}")
    print(f"  title: {job.get('title')}")
    print()
