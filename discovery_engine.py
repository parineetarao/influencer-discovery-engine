import argparse
import csv
import json
import math
import os
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List

import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE = "https://www.googleapis.com/youtube/v3"
DELAY_SECONDS = 2        # mandatory delay between requests (per the brief)
DAILY_QUOTA = 10000      # free-tier daily quota (verify in Google's docs)
COSTS = {"search": 100, "channels": 1, "playlistItems": 1}
MAX_RETRIES = 3          # retries for temporary errors (429 / 5xx / network)
RETRY_STATUSES = (429, 500, 502, 503, 504)

QUOTA_FILE = ".quota_state.json"   # remembers quota used today across runs
CACHE_FILE = ".audit_cache.json"   # remembers audited channels (resume + saves quota)
CACHE_KEYS = ["channel_name", "country", "subscribers", "total_views", "video_count",
              "recent_upload_dates", "last_upload_date"]

BATCH_SIZE = 50
NA = "N/A"
NOT_FOUND = "Date not found"
RESTRICTED = "Metric restricted"
CHANNEL_MISSING = "Error: Channel not found, deleted, or restricted"

FIELDNAMES = [
    "channel_id", "channel_name", "country", "subscribers", "total_views",
    "video_count", "recent_upload_dates", "last_upload_date",
    "days_since_last_upload", "avg_days_between_uploads", "activity",
    "avg_views_per_video", "potential_score", "status",
]


def pacific_date(now_utc=None):
    """Current date in US Pacific time (YouTube's quota resets at midnight Pacific)."""
    now_utc = now_utc or datetime.utcnow()

    def nth_sunday(month, n):
        first = datetime(now_utc.year, month, 1)
        first_sunday = first + timedelta(days=(6 - first.weekday()) % 7)
        return first_sunday + timedelta(weeks=n - 1)

    dst_start = nth_sunday(3, 2).replace(hour=10)   # 2:00 AM PST = 10:00 UTC
    dst_end = nth_sunday(11, 1).replace(hour=9)     # 2:00 AM PDT = 09:00 UTC
    offset = -7 if dst_start <= now_utc < dst_end else -8
    return (now_utc + timedelta(hours=offset)).date()


def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except OSError:
        pass


def cached_fields(cache, channel_id, recent_count, max_age_hours):
    """Return saved audit fields if fresh and gathered with the same --recent value."""
    entry = cache.get(channel_id)
    if not entry or entry.get("recent_count") != recent_count:
        return None
    try:
        fetched = datetime.strptime(entry["fetched_at"], "%Y-%m-%dT%H:%M:%S")
    except (KeyError, ValueError):
        return None
    age_hours = (datetime.utcnow() - fetched).total_seconds() / 3600.0
    if age_hours > max_age_hours:
        return None
    return entry.get("fields")


class QuotaExceeded(Exception):
    pass


class ApiError(Exception):
    pass


class YouTubeClient:
    """Wraps every request: adds auth, enforces the 2s delay, counts quota,
    and retries temporary errors with exponential backoff."""

    def __init__(self):
        self.api_key = os.getenv("YOUTUBE_API_KEY")
        self.oauth_token = os.getenv("YOUTUBE_OAUTH_TOKEN")  # optional
        if not self.api_key and not self.oauth_token:
            raise SystemExit("Error: set YOUTUBE_API_KEY in your .env file")
        self.quota_used = self._load_quota()  # units used today by this tool
        self.run_quota = 0                    # units used by this run
        self._last_request = 0.0

    def _load_quota(self):
        try:
            with open(QUOTA_FILE, encoding="utf-8") as f:
                state = json.load(f)
            if state.get("date") == str(pacific_date()):
                return int(state.get("used", 0))
        except (OSError, ValueError):
            pass
        return 0

    def _save_quota(self):
        try:
            with open(QUOTA_FILE, "w", encoding="utf-8") as f:
                json.dump({"date": str(pacific_date()), "used": self.quota_used}, f)
        except OSError:
            pass

    def get(self, endpoint, params):
        cost = COSTS[endpoint]
        params = dict(params)
        headers = {}
        if self.oauth_token:
            headers["Authorization"] = "Bearer " + self.oauth_token
        else:
            params["key"] = self.api_key

        resp = None
        for attempt in range(MAX_RETRIES + 1):
            if self.quota_used + cost > DAILY_QUOTA:
                raise QuotaExceeded("Error: daily quota would be exceeded")

            # enforce the 2-second gap before EVERY request, retries included
            wait = DELAY_SECONDS - (time.time() - self._last_request)
            if wait > 0:
                time.sleep(wait)

            try:
                resp = requests.get(
                    API_BASE + "/" + endpoint,
                    params=params, headers=headers, timeout=15,
                )
            except requests.RequestException as exc:
                self._last_request = time.time()
                if attempt < MAX_RETRIES:
                    time.sleep(2 ** (attempt + 1))
                    continue
                # only the error type: the full message contains the URL (and API key)
                raise ApiError("Error: network problem ({})".format(type(exc).__name__))

            self._last_request = time.time()
            self.quota_used += cost
            self.run_quota += cost
            self._save_quota()
            if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
                print("Retrying ({}/{}) after HTTP {}".format(
                    attempt + 1, MAX_RETRIES, resp.status_code))
                time.sleep(2 ** (attempt + 1))
                continue
            break

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


def _clamp(value):
    return max(0.0, min(1.0, value))


def compute_metrics(row, active_days=30, today=None):
    """Derived columns plus a transparent 0-100 potential score (see README).

    Score = recency (25) + upload consistency (15) + engagement proxy (40)
            + reach (20), halved if the channel is Inactive. Uses data we
            already fetched, so it costs no quota.
    Returns N/A for anything that cannot be computed.
    """
    metrics = {"days_since_last_upload": NA, "avg_days_between_uploads": NA,
               "activity": NA, "avg_views_per_video": NA, "potential_score": NA}
    today = today or datetime.utcnow().date()

    dates = []
    for part in str(row.get("recent_upload_dates", "")).split(","):
        try:
            dates.append(datetime.strptime(part.strip(), "%Y-%m-%d").date())
        except ValueError:
            pass

    days_since = None
    avg_gap = None
    if dates:
        dates.sort(reverse=True)
        days_since = max((today - dates[0]).days, 0)
        metrics["days_since_last_upload"] = days_since
        metrics["activity"] = "Active" if days_since <= active_days else "Inactive"
        if len(dates) >= 2:
            avg_gap = (dates[0] - dates[-1]).days / float(len(dates) - 1)
            metrics["avg_days_between_uploads"] = round(avg_gap, 1)

    views = row.get("total_views")
    videos = row.get("video_count")
    subs = row.get("subscribers")
    avg_views = None
    if isinstance(views, int) and isinstance(videos, int) and videos > 0:
        avg_views = views / float(videos)
        metrics["avg_views_per_video"] = int(round(avg_views))

    if (days_since is not None and avg_gap is not None and avg_views is not None
            and isinstance(subs, int) and subs > 0):
        recency = 25 * _clamp((60 - days_since) / 53.0)        # full marks within 7 days
        consistency = 15 * _clamp((30 - avg_gap) / 27.0)       # full marks at <=3 days apart
        engagement = 40 * _clamp((avg_views / float(subs)) / 0.25)  # avg views / subs, cap 25%
        reach = 20 * _clamp((math.log10(subs) - 3) / 3.0)      # 1K subs = 0, 1M+ = full
        score = recency + consistency + engagement + reach
        if metrics["activity"] == "Inactive":
            score = score * 0.5  # no recent uploads: halve the score
        metrics["potential_score"] = int(round(score))
    return metrics


def audit_channels(client, channels, recent_count=5, region=None, limit=None,
                   active_days=30, cache=None, cache_hours=24):
    """Audit every channel; one bad channel never stops the batch.

    If a cache dict is given, channels audited recently (and with the same
    recent_count) are reused without any API calls, so an interrupted batch
    can resume and repeat runs cost no quota.
    """
    by_id = {c["channel_id"]: c for c in channels}
    ids = [c["channel_id"] for c in channels]
    real_ids = [c["channel_id"] for c in channels if not c.get("error")]
    rows = []

    cached = {}
    if cache is not None:
        for cid in real_ids:
            fields = cached_fields(cache, cid, recent_count, cache_hours)
            if fields:
                cached[cid] = fields
        if cached:
            print("Cache: reusing {} of {} channels (no API calls needed)".format(
                len(cached), len(real_ids)))

    batch_error = None
    try:
        details = fetch_channel_stats(client, [c for c in real_ids if c not in cached])
    except (ApiError, QuotaExceeded) as exc:
        details = {}
        batch_error = str(exc)

    if region:
        before = len(ids)

        def keep(cid):
            if cid in cached:
                return cached[cid].get("country", "").upper() == region.upper()
            return matches_region(details.get(cid), region)

        ids = [c for c in ids if keep(c)]
        print("Region filter {}: kept {} of {} channels".format(
            region.upper(), len(ids), before))
    if limit:
        ids = ids[:limit]

    for channel_id in ids:
        source = by_id[channel_id]
        print("Processing: {}{}".format(source.get("input") or channel_id,
                                        " (cached)" if channel_id in cached else ""))
        row = {
            "channel_id": channel_id,
            "channel_name": source.get("title", ""),
            "country": "",
            "subscribers": RESTRICTED,
            "total_views": RESTRICTED,
            "video_count": RESTRICTED,
            "recent_upload_dates": NOT_FOUND,
            "last_upload_date": NOT_FOUND,
            "days_since_last_upload": NA,
            "avg_days_between_uploads": NA,
            "activity": NA,
            "avg_views_per_video": NA,
            "potential_score": NA,
            "status": "OK",
        }

        if channel_id in cached:
            row.update(cached[channel_id])
            row.update(compute_metrics(row, active_days))  # recomputed against today
            rows.append(row)
            continue

        item = details.get(channel_id)
        if source.get("error") or batch_error or item is None:
            row["status"] = source.get("error") or batch_error or CHANNEL_MISSING
            print(row["status"])
            for key in ("subscribers", "total_views", "video_count",
                        "recent_upload_dates", "last_upload_date"):
                row[key] = NA
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

        row.update(compute_metrics(row, active_days))
        if cache is not None and row["status"] == "OK":
            cache[channel_id] = {
                "fetched_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
                "recent_count": recent_count,
                "fields": dict((k, row[k]) for k in CACHE_KEYS),
            }
            save_cache(cache)
        rows.append(row)
    return rows


def sort_by_score(rows):
    """Highest potential score first; unscored rows and errors go last."""
    def key(row):
        score = row["potential_score"]
        return (0, -score) if isinstance(score, int) else (1, 0)
    return sorted(rows, key=key)


def writable_path(path):
    """Return path if it can be written, else a timestamped alternative.

    A file that is open in Excel is locked on Windows; saving to a new name
    keeps the run's results instead of crashing at the very last step.
    """
    try:
        open(path, "a").close()
        return path
    except OSError:
        base, ext = os.path.splitext(path)
        alternative = "{}_{}{}".format(base, datetime.now().strftime("%H%M%S"), ext)
        print("Warning: {} cannot be written (is it open in another program?). "
              "Saving to {} instead.".format(path, alternative))
        return alternative


def write_outputs(rows, json_path="output.json", csv_path="output.csv"):
    """Write the JSON and CSV reports. Returns the paths actually used."""
    json_path = writable_path(json_path)
    csv_path = writable_path(csv_path)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return json_path, csv_path


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
    parser.add_argument("--active-days", type=int, default=30,
                        help="a channel is 'Active' if it posted within this many days")
    parser.add_argument("--ids-file", default="channel_ids.json")
    parser.add_argument("--from-ids-file", action="store_true",
                        help="skip the search and reuse saved channel IDs (saves quota)")
    parser.add_argument("--no-cache", action="store_true",
                        help="ignore saved audits and fetch everything fresh")
    parser.add_argument("--cache-hours", type=float, default=24,
                        help="reuse a saved audit if it is newer than this many hours (default 24)")
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
                          limit=args.max_channels if args.strict_region else None,
                          active_days=args.active_days,
                          cache=None if args.no_cache else load_cache(),
                          cache_hours=args.cache_hours)
    rows = sort_by_score(rows)
    json_path, csv_path = write_outputs(rows)

    ok = sum(1 for r in rows if r["status"] == "OK")
    print("Done: {} succeeded, {} failed.".format(ok, len(rows) - ok))
    scored = [r for r in rows if isinstance(r["potential_score"], int)]
    if scored:
        print("Top creators by potential score:")
        for r in scored[:3]:
            print("  {} ({}/100) - {}".format(r["channel_name"], r["potential_score"], r["activity"]))
    print("Quota used: {} this run, {} today / {} (as tracked by this tool)".format(
        client.run_quota, client.quota_used, DAILY_QUOTA))
    print("Results saved to {} and {}".format(json_path, csv_path))


if __name__ == "__main__":
    main()