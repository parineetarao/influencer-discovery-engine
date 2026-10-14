import argparse
import csv
import json
import os
import re
import time
from datetime import datetime
from typing import Dict, List

import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE = "https://www.googleapis.com/youtube/v3"
DELAY_SECONDS = 2        # mandatory delay between requests (per the brief)
DAILY_QUOTA = 10000      # free-tier daily quota (verify in Google's docs)
COSTS = {"search": 100, "channels": 1, "playlistItems": 1}

BATCH_SIZE = 50
NOT_FOUND = "Date not found"
RESTRICTED = "Metric restricted"
CHANNEL_MISSING = "Error: Channel not found, deleted, or restricted"


class QuotaExceeded(Exception):
    pass


class ApiError(Exception):
    pass


class YouTubeClient:
    """Wraps every request: adds auth, enforces the 2s delay, counts quota."""

    def __init__(self):
        self.api_key = os.getenv("YOUTUBE_API_KEY")
        self.oauth_token = os.getenv("YOUTUBE_OAUTH_TOKEN")  # optional
        if not self.api_key and not self.oauth_token:
            raise SystemExit("Error: set YOUTUBE_API_KEY in your .env file")
        self.quota_used = 0
        self._last_request = 0.0

    def get(self, endpoint, params):
        cost = COSTS[endpoint]
        if self.quota_used + cost > DAILY_QUOTA:
            raise QuotaExceeded("Error: daily quota would be exceeded")

        # enforce the 2-second gap between requests
        wait = DELAY_SECONDS - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)

        params = dict(params)
        headers = {}
        if self.oauth_token:
            headers["Authorization"] = "Bearer " + self.oauth_token
        else:
            params["key"] = self.api_key

        try:
            resp = requests.get(
                API_BASE + "/" + endpoint,
                params=params, headers=headers, timeout=15,
            )
        except requests.RequestException as exc:
            self._last_request = time.time()
            # only the error type: the full message contains the URL (and API key)
            raise ApiError("Error: network problem ({})".format(type(exc).__name__))

        self._last_request = time.time()
        self.quota_used += cost

        if resp.status_code != 200:
            try:
                message = resp.json()["error"]["message"]
            except Exception:
                message = resp.text[:100]
            raise ApiError("Error: {}".format(message))
        return resp.json()


def search_channels(client, keyword, region=None, language=None, max_channels=10):
    """Find channels for a keyword. Returns [{'channel_id':..., 'title':...}]."""
    found: List[Dict[str, str]] = []
    seen = set()
    page_token = None

    while len(found) < max_channels:
        params = {
            "part": "snippet",
            "type": "channel",
            "q": keyword,
            "maxResults": min(50, max_channels - len(found)),
            # only return the fields we need, to avoid data overhead
            "fields": "nextPageToken,items(id/channelId,snippet/title)",
        }
        if region:
            params["regionCode"] = region
        if language:
            params["relevanceLanguage"] = language
        if page_token:
            params["pageToken"] = page_token

        data = client.get("search", params)
        for item in data.get("items", []):
            channel_id = item["id"]["channelId"]
            if channel_id not in seen:
                seen.add(channel_id)
                found.append({"channel_id": channel_id,
                              "title": item["snippet"]["title"]})
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return found[:max_channels]


def parse_channel_input(text):
    """Classify one input line. Returns (kind, value); kind is None if unsupported.

    Supported: channel ID (UC...), @handle, youtube.com/channel/UC...,
    youtube.com/@handle, youtube.com/user/name.
    """
    text = text.strip()
    if re.match(r"^UC[\w-]{22}$", text):
        return "id", text
    if re.match(r"^@[\w.\-]+$", text):
        return "handle", text
    match = re.search(r"youtube\.com/channel/(UC[\w-]{22})", text)
    if match:
        return "id", match.group(1)
    match = re.search(r"youtube\.com/(@[\w.\-]+)", text)
    if match:
        return "handle", match.group(1)
    match = re.search(r"youtube\.com/user/([\w.\-]+)", text)
    if match:
        return "user", match.group(1)
    return None, "Error: unsupported or malformed channel URL"


def resolve_inputs(client, lines):
    """Turn input lines (URLs, @handles, IDs) into channel dicts.

    Lines that cannot be resolved are kept with an 'error' key so they
    appear as error rows in the output instead of being dropped or crashing.
    """
    channels = []
    seen = set()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        kind, value = parse_channel_input(line)
        if kind is None:
            channels.append({"channel_id": line, "title": "",
                             "input": line, "error": value})
            continue

        if kind == "id":
            channel_id = value
        else:
            print("Resolving: {}".format(line))
            param = "forHandle" if kind == "handle" else "forUsername"
            try:
                data = client.get("channels",
                                  {"part": "id", param: value, "fields": "items(id)"})
            except (ApiError, QuotaExceeded) as exc:
                channels.append({"channel_id": line, "title": "",
                                 "input": line, "error": str(exc)})
                continue
            items = data.get("items", [])
            if not items:
                channels.append({"channel_id": line, "title": "",
                                 "input": line, "error": CHANNEL_MISSING})
                continue
            channel_id = items[0]["id"]

        if channel_id in seen:
            continue
        seen.add(channel_id)
        channels.append({"channel_id": channel_id, "title": "", "input": line})
    return channels


def normalize_date(raw):
    """'2026-10-10T12:30:00Z' -> '2026-10-10' (UTC); 'Date not found' if unusable."""
    if not raw:
        return NOT_FOUND
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return NOT_FOUND


def fetch_channel_stats(client, channel_ids):
    """One API call per 50 channels. Returns {channel_id: channel_data}."""
    details = {}
    for start in range(0, len(channel_ids), BATCH_SIZE):
        batch = channel_ids[start:start + BATCH_SIZE]
        data = client.get("channels", {
            "part": "snippet,statistics,contentDetails",
            "id": ",".join(batch),
            "maxResults": BATCH_SIZE,
            "fields": ("items(id,snippet(title,country),"
                       "statistics(subscriberCount,viewCount,videoCount,hiddenSubscriberCount),"
                       "contentDetails/relatedPlaylists/uploads)"),
        })
        for item in data.get("items", []):
            details[item["id"]] = item
    return details


def fetch_recent_dates(client, uploads_playlist_id, count):
    """Latest upload dates (newest first) as YYYY-MM-DD. Metadata only, no media."""
    data = client.get("playlistItems", {
        "part": "contentDetails",
        "playlistId": uploads_playlist_id,
        "maxResults": count,
        "fields": "items(contentDetails/videoPublishedAt)",
    })
    return [normalize_date(i.get("contentDetails", {}).get("videoPublishedAt"))
            for i in data.get("items", [])]


def matches_region(item, region):
    if item is None:  # channel not found: keep it so the error row is reported
        return True
    return item.get("snippet", {}).get("country", "").upper() == region.upper()


def audit_channels(client, channels, recent_count=5, region=None, limit=None):
    """Audit every channel; one bad channel never stops the batch."""
    by_id = {c["channel_id"]: c for c in channels}
    ids = [c["channel_id"] for c in channels]
    real_ids = [c["channel_id"] for c in channels if not c.get("error")]
    rows = []

    batch_error = None
    try:
        details = fetch_channel_stats(client, real_ids)
    except (ApiError, QuotaExceeded) as exc:
        details = {}
        batch_error = str(exc)

    if region:
        before = len(ids)
        ids = [c for c in ids if matches_region(details.get(c), region)]
        print("Region filter {}: kept {} of {} channels".format(
            region.upper(), len(ids), before))
    if limit:
        ids = ids[:limit]

    for channel_id in ids:
        source = by_id[channel_id]
        print("Processing: {}".format(source.get("input") or channel_id))
        row = {
            "channel_id": channel_id,
            "channel_name": source.get("title", ""),
            "country": "",
            "subscribers": RESTRICTED,
            "total_views": RESTRICTED,
            "video_count": RESTRICTED,
            "recent_upload_dates": NOT_FOUND,
            "last_upload_date": NOT_FOUND,
            "status": "OK",
        }
        item = details.get(channel_id)

        if source.get("error") or batch_error or item is None:
            row["status"] = source.get("error") or batch_error or CHANNEL_MISSING
            print(row["status"])
            for key in ("subscribers", "total_views", "video_count",
                        "recent_upload_dates", "last_upload_date"):
                row[key] = "N/A"
            rows.append(row)
            continue

        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        row["channel_name"] = snippet.get("title", row["channel_name"])
        row["country"] = snippet.get("country", "Not specified")
        if not stats.get("hiddenSubscriberCount") and "subscriberCount" in stats:
            row["subscribers"] = int(stats["subscriberCount"])
        if "viewCount" in stats:
            row["total_views"] = int(stats["viewCount"])
        if "videoCount" in stats:
            row["video_count"] = int(stats["videoCount"])

        uploads = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if uploads:
            try:
                dates = fetch_recent_dates(client, uploads, recent_count)
                if dates:
                    row["recent_upload_dates"] = ", ".join(dates)
                    row["last_upload_date"] = dates[0]
            except (ApiError, QuotaExceeded) as exc:
                row["status"] = str(exc)
                print(exc)
        rows.append(row)
    return rows


def write_outputs(rows, json_path="output.json", csv_path="output.csv"):
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
    if rows:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Influencer discovery and audit tool")
    parser.add_argument("--keyword", help='niche keyword, e.g. "finance"')
    parser.add_argument("--region", help="2-letter country code, e.g. IN, US")
    parser.add_argument("--strict-region", action="store_true",
                        help="keep only channels whose declared country matches --region")
    parser.add_argument("--language", help="language code, e.g. en, hi")
    parser.add_argument("--max-channels", type=int, default=10)
    parser.add_argument("--recent", type=int, default=5, choices=range(5, 11),
                        metavar="5-10", help="how many recent uploads to audit")
    parser.add_argument("--ids-file", default="channel_ids.json")
    parser.add_argument("--from-ids-file", action="store_true",
                        help="skip the search and reuse saved channel IDs (saves quota)")
    parser.add_argument("--input-file",
                        help="text file with one channel URL, @handle or channel ID per line")
    args = parser.parse_args()

    if args.strict_region and not args.region:
        parser.error("--strict-region requires --region")

    client = YouTubeClient()

    if args.input_file:
        try:
            with open(args.input_file, encoding="utf-8-sig") as f:
                lines = f.read().splitlines()
        except OSError as exc:
            print("Error: could not read {} ({})".format(args.input_file, exc.strerror))
            return
        channels = resolve_inputs(client, lines)
        print("Loaded {} channels from {}".format(len(channels), args.input_file))
    elif args.from_ids_file:
        try:
            with open(args.ids_file, encoding="utf-8") as f:
                channels = json.load(f)
        except (OSError, ValueError) as exc:
            print("Error: could not read {} ({})".format(args.ids_file, exc))
            return
        print("Loaded {} channel IDs from {}".format(len(channels), args.ids_file))
    else:
        if not args.keyword:
            parser.error("--keyword is required unless --input-file or --from-ids-file is used")
        print('Searching YouTube for "{}"...'.format(args.keyword))
        try:
            pool = 50 if args.strict_region else args.max_channels
            channels = search_channels(client, args.keyword, args.region,
                                       args.language, pool)
        except (ApiError, QuotaExceeded) as exc:
            print(exc)
            return
        with open(args.ids_file, "w", encoding="utf-8") as f:
            json.dump(channels, f, indent=2, ensure_ascii=False)
        print("Found {} channels. Saved IDs to {}".format(len(channels), args.ids_file))

    rows = audit_channels(client, channels, args.recent,
                          region=args.region if args.strict_region else None,
                          limit=args.max_channels if args.strict_region else None)
    write_outputs(rows)

    ok = sum(1 for r in rows if r["status"] == "OK")
    print("Done: {} succeeded, {} failed.".format(ok, len(rows) - ok))
    print("Quota used: {} / {}".format(client.quota_used, DAILY_QUOTA))
    print("Results saved to output.json and output.csv")


if __name__ == "__main__":
    main()