#!/usr/bin/env python3
"""
Simple test script to validate description collection logic for each ATS platform.
Uses actual company slugs and job IDs from the existing job listings to test real API endpoints.
"""

import json
import requests

# =============================
# CONFIGURATION (USE REAL VALUES FROM env)
# =============================
TEST_CASES = {
    "BambooHR": {
        "base_url": "https://api.bamboohr.com/api/gateway.php/{slug}/v1/candidate_portal/jobs/{job_id}",
        "headers": {"Accept": "application/json", "User-Agent": "JobBoard-Test/1.0"},
        "description_field": "description",
        "slug": "aras",
        "job_id": "328"  # Used job from env
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
        "tenant": "gaig",
        "wd_server": "wd1",
        "site_id": "gaig_external",
        "externalPath": "Lakeland-FL-USA/Business-Process-Consultant_R9268-1"
    },
    "iCIMS": {
        "base_url": "https://{slug}.icims.com/jobs/{job_id}/job?mode=json",
        "headers": {"Accept": "application/json", "User-Agent": "JobBoard-Test/1.0"},
        "description_field": "description",
        "slug": "brookhavenhospice",
        "job_id": "27738"
    }
}

# =============================
# TEST FUNCTIONS
# =============================
def test_api(url_template, headers_template, description_field, params):
    """Test a single API endpoint and report if it returns the expected description field."""
    try:
        # Build the URL with params
        url = url_template.format(**params)
        
        # Add a realistic Referer if not already present
        if "Referer" not in headers_template:
            headers_template["Referer"] = url

        print(f"Fetching: {url}")
        response = requests.get(url, headers=headers_template, timeout=15)
        
        if response.status_code != 200:
            print(f"  → Status {response.status_code}")
            return {"success": False, "status_code": response.status_code}
        
        # Try JSON first; fall back to plain text
        try:
            data = response.json()
        except json.JSONDecodeError:
            data = response.text

        # Check for the expected field at top level
        if description_field in data:
            snippet = str(data[description_field])[:200] + ("..." if len(str(data[description_field])) > 200 else "")
            print(f"  → Found '{description_field}' at top level: {snippet}")
            return {"success": True, "field": description_field, "snippet": snippet}
        
        # Check for nested fields (e.g., jobPostingInfo.jobDescription for Workday)
        if isinstance(data, dict):
            for key, val in data.items():
                if isinstance(val, dict) and description_field in val:
                    snippet = str(val[description_field])[:200] + ("..." if len(str(val[description_field])) > 200 else "")
                    print(f"  → Found '{description_field}' nested under '{key}': {snippet}")
                    return {"success": True, "field": f"{key}.{description_field}", "snippet": snippet}
        
        # Heuristic: plain text may contain the field name
        if isinstance(data, str) and description_field.lower() in data.lower():
            print(f"  → Found field by name in text")
            return {"success": True, "field": description_field, "snippet": data[:200]}
        
        # Show a small excerpt of the response for manual inspection
        snippet = str(data)[:200] + ("..." if len(str(data)) > 200 else "")
        print(f"  → No '{description_field}' field found. Sample response: {snippet}")
        return {"success": False, "unexpected_fields": list(data.keys())[:10] if isinstance(data, dict) else "text_response"}

    except Exception as e:
        print(f"  → Error: {e}")
        return {"success": False, "error": str(e)}


def main():
    results = {}
    for platform, cfg in TEST_CASES.items():
        print(f"\n=== Testing {platform} ===")
        result = test_api(
            cfg["base_url"],
            cfg["headers"],
            cfg["description_field"],
            {k: v for k, v in cfg.items() if k not in ["base_url", "headers", "description_field"]}
        )
        results[platform] = result
    
    print("\n=== TEST SUMMARY ===")
    print(json.dumps(results, indent=2))
    
    # Summary
    all_passed = all(r["success"] for r in results.values())
    if all_passed:
        print("\n✅ All tests PASSED! Description collection is working.")
    else:
        print("\n❌ Some tests FAILED. Description collection needs attention.")


if __name__ == "__main__":
    main()