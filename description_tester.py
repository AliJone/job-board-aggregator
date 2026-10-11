#!/usr/bin/env python3
"""
Simple test script to validate description collection logic for each ATS platform.
Replace the placeholder company slugs / IDs with real ones from your environment.
"""

import json
import requests

# =============================
# CONFIGURATION (EDIT THESE)
# =============================
# Use real company slugs or job IDs from your own accounts
TEST_CASES = {
    "BambooHR": {
        "base_url": "https://api.bamboohr.com/api/gateway.php/{slug}/v1/candidate_portal/jobs/{job_id}",
        "headers": {"Accept": "application/json", "User-Agent": "JobBoard-Test/1.0"},
        "description_field": "description",
        "sample_job_id": "12345"  # <-- replace with a real job ID
    },
    "Workday": {
        "base_url": "https://{tenant}.{wd_server}.myworkdayjobs.com/wday/cxs/{tenant}/{site_id}/job/{externalPath}",
        "headers": {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "JobBoard-Test/1.0",
            "Referer": "https://{tenant}.{wd_server}.myworkdayjobs.com/{site_id}"
        },
        "description_field": "jobDescription",
        "sample_externalPath": "software-engineer-1"  # <-- replace with a real externalPath
    },
    "iCIMS": {
        "base_url": "https://{slug}.icims.com/jobs/{job_id}/job?mode=json",
        "headers": {"Accept": "application/json", "User-Agent": "JobBoard-Test/1.0"},
        "description_field": "description",
        "sample_job_id": "98765"  # <-- replace with a real job ID
    }
}

# =============================
# TEST FUNCTIONS
# =============================
def test_api(url_template, headers_template, description_field, sample_value):
    """Test a single API endpoint and report if it returns the expected description field."""
    try:
        # Build the URL with sample values
        url = url_template.format(slug="test-slug", job_id="123")  # Placeholder – replace as needed
        # You can customize this URL further based on your actual IDs
        full_url = url.replace("{slug}", "examplecompany")\
                         .replace("{job_id}", "001")\
                         .replace("{tenant}", "examplewd")\
                         .replace("{wd_server}", "wd3")\
                         .replace("{site_id}", "site1")\
                         .replace("{externalPath}", "software-engineer")\
                         .replace("{job_id}", "job-789")
        
        # Add a realistic Referer if not already present
        if "Referer" not in headers_template:
            headers_template["Referer"] = full_url

        print(f"Fetching: {full_url}")
        response = requests.get(full_url, headers=headers_template, timeout=15)
        
        if response.status_code != 200:
            print(f"  → Status {response.status_code}")
            return {"success": False, "status_code": response.status_code}
        
        # Try JSON first; fall back to plain text
        try:
            data = response.json()
        except json.JSONDecodeError:
            data = response.text

        # Check for the expected field
        if description_field in data:
            snippet = data[description_field][:200] + ("..." if len(data[description_field]) > 200 else "")
            print(f"  → Found '{description_field}': {snippet}")
            return {"success": True, "field": description_field, "snippet": snippet}
        elif isinstance(data, str) and description_field.lower() in data.lower():
            # Heuristic: plain text may contain the field name
            print(f"  → Found field by name in text")
            return {"success": True, "field": description_field, "snippet": data[:200]}
        else:
            # Show a small excerpt of the response for manual inspection
            snippet = data[:200] + ("..." if len(data) > 200 else "")
            print(f"  → No '{description_field}' field found. Sample response: {snippet}")
            return {"success": False, "unexpected_fields": list(data.keys())[:10]}

    except Exception as e:
        print(f"  → Error: {e}")
        return {"success": False, "error": str(e)}


def main():
    results = {}
    for platform, cfg in TEST_CASES.items():
        print(f"\n=== Testing {platform} ===")
        if platform.lower() == "bamboohr":
            result = test_api(
                cfg["base_url"],
                cfg["headers"],
                cfg["description_field"],
                cfg["sample_job_id"]
            )
        elif platform.lower() == "workday":
            result = test_api(
                cfg["base_url"],
                cfg["headers"],
                cfg["description_field"],
                cfg["sample_externalPath"]
            )
        elif platform.lower() == "icims":
            result = test_api(
                cfg["base_url"],
                cfg["headers"],
                cfg["description_field"],
                cfg["sample_job_id"]
            )
        else:
            result = {"success": False, "error": "Unknown platform"}
        results[platform] = result
    
    print("\n=== TEST SUMMARY ===")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()