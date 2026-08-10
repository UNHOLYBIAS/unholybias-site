"""
Fetches premarket gainers/losers from stockanalysis.com and upserts them
into the Supabase `premarket_movers` table using the plain REST API
(PostgREST) — no Supabase CLI or SDK needed, just `requests`.

Reads config from environment variables (set as GitHub Actions secrets):
    SUPABASE_URL              e.g. https://xxxx.supabase.co
    SUPABASE_SERVICE_ROLE_KEY the service_role secret key (NOT the anon key)
"""

import os
import sys
import requests
from datetime import datetime, timezone

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

SCRAPE_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def resolve(arr, i, cache=None):
    """Rebuild devalue-encoded (SvelteKit __data.json) array into normal objects."""
    if cache is None:
        cache = {}
    if i in cache:
        return cache[i]
    val = arr[i]
    if isinstance(val, list):
        result = []
        cache[i] = result
        for item in val:
            result.append(resolve(arr, item, cache) if isinstance(item, int) else item)
        return result
    if isinstance(val, dict):
        result = {}
        cache[i] = result
        for k, v in val.items():
            result[k] = resolve(arr, v, cache) if isinstance(v, int) else v
        return result
    return val


def find_stock_list(obj, seen=None):
    """Search resolved data for the list of stock-row dicts (has keys 's' and 'n')."""
    if seen is None:
        seen = set()
    obj_id = id(obj)
    if obj_id in seen:
        return None
    seen.add(obj_id)

    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) and "s" in x and "n" in x for x in obj):
            return obj
        for item in obj:
            found = find_stock_list(item, seen)
            if found:
                return found
    elif isinstance(obj, dict):
        for v in obj.values():
            found = find_stock_list(v, seen)
            if found:
                return found
    return None


def fetch_movers(page_path: str):
    url = f"https://stockanalysis.com/{page_path}/__data.json"
    resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=15)
    resp.raise_for_status()
    payload = resp.json()

    for node in payload.get("nodes", []):
        arr = node.get("data")
        if node.get("type") == "data" and isinstance(arr, list) and arr:
            root = resolve(arr, 0)
            found = find_stock_list(root)
            if found:
                return found
    raise RuntimeError(f"Couldn't find stock row list for {page_path}")


def replace_rows(rows, list_type: str):
    if not rows:
        print(f"[{list_type}] no rows to write")
        return

    now = datetime.now(timezone.utc).isoformat()
    payload = []
    for r in rows:
        payload.append({
            "symbol": r.get("s"),
            "name": r.get("n"),
            "pct_change": r.get("premarketChangePercent", r.get("chg")),
            "price": r.get("premarketPrice", r.get("close")),
            "volume": r.get("premarketVolume"),
            "market_cap": r.get("marketCap"),
            "list_type": list_type,
            "updated_at": now,
        })

    common_headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }

    # Wipe today's stale rows for this list_type first, so tickers that
    # dropped off the movers list don't linger forever.
    del_resp = requests.delete(
        f"{SUPABASE_URL}/rest/v1/premarket_movers?list_type=eq.{list_type}",
        headers={**common_headers, "Prefer": "return=minimal"},
        timeout=15,
    )
    if not del_resp.ok:
        print(f"[{list_type}] delete FAILED: {del_resp.status_code} {del_resp.text}")
        del_resp.raise_for_status()

    # Insert the current fresh batch.
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/premarket_movers",
        headers={**common_headers, "Prefer": "return=minimal"},
        json=payload,
        timeout=15,
    )

    if not resp.ok:
        print(f"[{list_type}] upsert FAILED: {resp.status_code} {resp.text}")
        resp.raise_for_status()

    print(f"[{list_type}] upserted {len(payload)} rows")


def main():
    import time

    loop_seconds = int(os.environ.get("LOOP_SECONDS", "270"))
    interval_seconds = int(os.environ.get("INTERVAL_SECONDS", "30"))

    start = time.time()
    iteration = 0
    total_errors = 0

    while time.time() - start < loop_seconds:
        iteration += 1
        print(f"--- iteration {iteration} ---", flush=True)

        for list_type in ("gainers",):
            try:
                rows = fetch_movers(f"markets/premarket/{list_type}")
                rows = rows[:20]
                replace_rows(rows, list_type)
            except Exception as e:
                total_errors += 1
                print(f"[{list_type}] ERROR: {type(e).__name__}: {e}", flush=True)

        remaining = loop_seconds - (time.time() - start)
        if remaining <= interval_seconds:
            break
        time.sleep(interval_seconds)

    print(f"=== done: {iteration} iterations, {total_errors} errors ===", flush=True)
    if total_errors > 0 and total_errors == iteration * 2:
        # every single fetch across every iteration failed — hard fail the job
        print("ALL fetches failed — failing the job so it shows red, not green.", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
