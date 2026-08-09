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


def upsert_rows(rows, list_type: str):
    if not rows:
        print(f"[{list_type}] no rows to upsert")
        return

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
        })

    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/premarket_movers?on_conflict=symbol,list_type",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        json=payload,
        timeout=15,
    )

    if not resp.ok:
        print(f"[{list_type}] upsert FAILED: {resp.status_code} {resp.text}")
        resp.raise_for_status()

    print(f"[{list_type}] upserted {len(payload)} rows")


def main():
    had_error = False
    for list_type in ("gainers", "losers"):
        try:
            rows = fetch_movers(f"markets/premarket/{list_type}")
            upsert_rows(rows, list_type)
        except Exception as e:
            had_error = True
            print(f"[{list_type}] ERROR: {e}")

    if had_error:
        sys.exit(1)


if __name__ == "__main__":
    main()
