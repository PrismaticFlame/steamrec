"""
One-time (long-running, resumable) script to populate the `apps` collection
with every Steam app of type "game", using Valve's Web APIs:

  - https://api.steampowered.com/IStoreService/GetAppList/v1/
      Full list of apps Valve classifies as "games" (requires STEAM_KEY,
      paginated via last_appid). ~170k entries.

  - https://store.steampowered.com/api/appdetails?appids=<id>
      Per-app details (type, genres, header image, dlc, etc).
      Unofficial rate limit is roughly 1 request per 1.5s per IP, so a full
      run over ~170k appids takes multiple days. Progress is checkpointed in
      Mongo (the `<MONGO_APPS>_scrape_status` collection), so the script can
      be killed (Ctrl+C) and re-run to pick up where it left off.

Usage:
  uv run python scripts/populate_db.py [--delay 1.5] [--max N] [--cc us]
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pymongo import MongoClient, UpdateOne

APP_LIST_URL = "https://api.steampowered.com/IStoreService/GetAppList/v1/"
APP_DETAILS_URL = "https://store.steampowered.com/api/appdetails"

# Steam content_descriptor IDs that indicate adult/NSFW content.
# 3 = Adult Only Sexual Content, 4 = Frequent Nudity or Sexual Content
NSFW_CONTENT_DESCRIPTOR_IDS = {3, 4}


def load_env_file(path: Path) -> None:
    """Populate os.environ from a simple KEY=VALUE .env file (without overriding existing vars)."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        os.environ.setdefault(key, value)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_app_list(client: httpx.Client, steam_key: str) -> list[dict]:
    """Paginate through every app Valve classifies as a "game" (excludes DLC,
    software, videos, and hardware at the list level)."""
    apps, last_appid = [], 0
    while True:
        res = client.get(
            APP_LIST_URL,
            params={
                "key": steam_key,
                "max_results": 50000,
                "last_appid": last_appid,
                "include_games": "true",
                "include_dlc": "false",
                "include_software": "false",
                "include_videos": "false",
                "include_hardware": "false",
            },
            timeout=30.0,
        )
        res.raise_for_status()
        data = res.json()["response"]
        apps.extend(data.get("apps", []))
        if not data.get("have_more_results"):
            break
        last_appid = data["last_appid"]

    # De-dupe and drop entries with no name (placeholders/unreleased apps).
    seen, deduped = set(), []
    for app in apps:
        appid, name = app["appid"], app["name"].strip()
        if not name or appid in seen:
            continue
        seen.add(appid)
        deduped.append({"appid": appid, "name": name})
    return deduped


def is_nsfw(data: dict) -> bool:
    if data.get("required_age", 0) and int(data["required_age"]) >= 18:
        return True
    descriptor_ids = set(data.get("content_descriptors", {}).get("ids", []))
    return bool(descriptor_ids & NSFW_CONTENT_DESCRIPTOR_IDS)


def to_document(appid: int, data: dict, timestamp: str) -> dict:
    return {
        "_id": appid,
        "added_date": timestamp,
        "updated_date": timestamp,
        "description": data.get("short_description", ""),
        "dlc": data.get("dlc", []),
        "drm": bool(data.get("drm_notice")),
        "header_image": data.get("header_image", ""),
        "name": data.get("name", ""),
        "description": data.get("short_description", ""),
        "nsfw": is_nsfw(data),
        "tags": [g["description"] for g in data.get("genres", [])],
        "type": data.get("type", ""),
    }


def fetch_app_details(client: httpx.Client, appid: int, cc: str, delay: float) -> dict | None:
    """Returns the appdetails 'data' dict, or None if the app isn't a usable entry.

    Retries with backoff on rate-limit (429) responses.
    """
    backoff = 60.0
    while True:
        try:
            res = client.get(
                APP_DETAILS_URL,
                params={"appids": appid, "cc": cc, "filters": "basic,genres,dlc,content_descriptors"},
                timeout=15.0,
            )
        except httpx.RequestError:
            time.sleep(delay)
            return None

        if res.status_code == 429:
            print(f"  rate limited, backing off {backoff:.0f}s...", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 600.0)
            continue

        if res.status_code != 200:
            time.sleep(delay)
            return None

        entry = res.json().get(str(appid), {})
        time.sleep(delay)
        if not entry.get("success"):
            return None
        return entry.get("data")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay", type=float, default=1.5, help="seconds to wait between appdetails requests")
    parser.add_argument("--max", type=int, default=None, help="stop after processing this many new apps (for testing)")
    parser.add_argument("--cc", default="us", help="Steam store country code for pricing/region data")
    args = parser.parse_args()

    load_env_file(Path(__file__).resolve().parent.parent.parent / ".env")

    mongo_uri = os.environ["MONGO_URI"]
    db_name = os.environ["MONGO_DB"]
    apps_collection_name = os.environ["MONGO_APPS"]
    steam_key = os.environ["STEAM_KEY"]

    mongo = MongoClient(mongo_uri)
    db = mongo[db_name]
    apps = db[apps_collection_name]
    progress = db[f"{apps_collection_name}_scrape_status"]

    with httpx.Client() as client:
        print("Fetching full Steam app list...")
        all_apps = fetch_app_list(client, steam_key)
        print(f"Got {len(all_apps)} candidate apps.")

        already_done = {doc["_id"] for doc in progress.find({}, {"_id": 1})}
        todo = [a for a in all_apps if a["appid"] not in already_done]
        print(f"{len(already_done)} already processed, {len(todo)} remaining.")

        processed_this_run = 0
        games_found = 0
        start = time.monotonic()

        try:
            for app in todo:
                if args.max is not None and processed_this_run >= args.max:
                    break

                appid, name = app["appid"], app["name"]
                data = fetch_app_details(client, appid, args.cc, args.delay)

                if data and data.get("type") == "game":
                    doc = to_document(appid, data, now_iso())
                    apps.replace_one({"_id": appid}, doc, upsert=True)
                    progress.replace_one({"_id": appid}, {"_id": appid, "status": "game"}, upsert=True)
                    games_found += 1
                else:
                    progress.replace_one({"_id": appid}, {"_id": appid, "status": "skipped"}, upsert=True)

                processed_this_run += 1
                if processed_this_run % 50 == 0:
                    elapsed = time.monotonic() - start
                    rate = processed_this_run / elapsed
                    remaining = len(todo) - processed_this_run
                    eta_hours = (remaining / rate) / 3600 if rate else float("inf")
                    print(
                        f"[{processed_this_run}/{len(todo)}] games found: {games_found} "
                        f"| last: {name!r} | ~{eta_hours:.1f}h remaining"
                    )
        except KeyboardInterrupt:
            print("\nInterrupted, progress saved. Re-run the script to resume.")

    print(f"Done this run. Processed {processed_this_run} apps, found {games_found} games.")


if __name__ == "__main__":
    main()
