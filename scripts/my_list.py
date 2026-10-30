"""My list: the saved company list the live-scrape panel scrapes by default.

Stored as one JSON file (data/my_list.json, untracked):

    {"companies": [{"platform", "id", "source": "you"|"auto", "matched_total",
                    "last_matched_at", "added_at"}, ...],
     "tried": {"platform:id": {"last_checked_at"}, ...}}

"tried" records companies that were scraped with no matching job, so
discovery skips them for TRY_AGAIN_DAYS. Every write happens under one lock
and goes through scraper.write_json_file (temp file + rename)."""
import json
import os
import random
import threading
from datetime import datetime, timedelta, timezone

from scraper import write_json_file

TRY_AGAIN_DAYS = 30
# Ashby is left out until fetch_company_jobs_ashby works again: its GraphQL
# query is rejected, so every Ashby company would be recorded as "no match".
DISCOVER_PLATFORMS = ("greenhouse", "lever")
DISCOVER_SEED = 20261031  # fixed, so the shuffled order is the same every run


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def key(platform, company_id):
    return f"{platform}:{company_id}"


def shuffled(candidates, seed=DISCOVER_SEED):
    """Fixed shuffle of (platform, id) pairs, so later runs keep moving forward."""
    order = sorted(candidates)
    random.Random(seed).shuffle(order)
    return order


def pick_new(order, n, on_list, tried, dead, now=None):
    """First n companies in order that are not on My list, not dead, and not
    tried within TRY_AGAIN_DAYS. on_list/dead hold "platform:id" keys."""
    if n <= 0:
        return []
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=TRY_AGAIN_DAYS)
    picked = []
    for platform, company_id in order:
        k = key(platform, company_id)
        if k in on_list or k in dead:
            continue
        last = (tried.get(k) or {}).get("last_checked_at")
        if last and _parse(last) > cutoff:
            continue
        picked.append((platform, company_id))
        if len(picked) == n:
            break
    return picked


class MyList:
    def __init__(self, path, defaults):
        self.path = path
        self.defaults = defaults
        self.lock = threading.Lock()

    # Callers hold self.lock for _load/_save.
    def _load(self):
        if not os.path.exists(self.path):
            # Missing file: start from the defaults. A corrupt file raises
            # instead, so a bad read never overwrites the user's list.
            data = {"companies": [], "tried": {}}
            ts = now_iso()
            for platform, ids in self.defaults.items():
                for company_id in ids:
                    data["companies"].append(self._entry(platform, company_id, "you", ts))
            self._save(data)
            return data
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("companies", [])
        data.setdefault("tried", {})
        return data

    def _save(self, data):
        write_json_file(self.path, data)

    @staticmethod
    def _entry(platform, company_id, source, ts, matched=0):
        return {"platform": platform, "id": company_id, "source": source,
                "matched_total": matched, "last_matched_at": ts if matched else None, "added_at": ts}

    @staticmethod
    def _find(data, platform, company_id):
        return next((e for e in data["companies"] if e["platform"] == platform and e["id"] == company_id), None)

    def snapshot(self):
        """(sorted entries, tried) read under the lock."""
        with self.lock:
            data = self._load()
        # Most matches first; among equal counts, most recent match first.
        entries = sorted(data["companies"], key=lambda e: e["last_matched_at"] or "", reverse=True)
        entries.sort(key=lambda e: e["matched_total"], reverse=True)
        return entries, data["tried"]

    def companies(self):
        """{platform: [ids]} in list order, for the scrape request and the picker."""
        out = {}
        for e in self.snapshot()[0]:
            out.setdefault(e["platform"], []).append(e["id"])
        return out

    def keys(self):
        return {key(e["platform"], e["id"]) for e in self.snapshot()[0]}

    def add(self, companies, source="you"):
        """Add {platform: [ids]}; returns how many were new."""
        added = 0
        with self.lock:
            data = self._load()
            ts = now_iso()
            for platform, ids in companies.items():
                for company_id in ids:
                    if not self._find(data, platform, company_id):
                        data["companies"].append(self._entry(platform, company_id, source, ts))
                        data["tried"].pop(key(platform, company_id), None)
                        added += 1
            if added:
                self._save(data)
        return added

    def remove(self, platform, company_id):
        with self.lock:
            data = self._load()
            entry = self._find(data, platform, company_id)
            if not entry:
                return False
            data["companies"].remove(entry)
            self._save(data)
            return True

    def record_result(self, platform, company_id, matched, auto_add):
        """Update the list after one company was scraped. Returns True when the
        company was newly added. Never removes anything."""
        with self.lock:
            data = self._load()
            ts = now_iso()
            entry = self._find(data, platform, company_id)
            added = False
            if matched > 0:
                data["tried"].pop(key(platform, company_id), None)
                if entry:
                    entry["matched_total"] += matched
                    entry["last_matched_at"] = ts
                elif auto_add:
                    data["companies"].append(self._entry(platform, company_id, "auto", ts, matched))
                    added = True
            else:
                data["tried"][key(platform, company_id)] = {"last_checked_at": ts}
            self._save(data)
            return added
