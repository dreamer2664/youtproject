"""YouTube Data API v3: niche + competitor stats. Quota-aware, never silent.

Quota discipline is the whole game: videos.list / channels.list cost 1 unit,
but search.list costs 100 (of 10k/day per key). So lookups are free-flowing
while search lives behind an explicit flag and always reports its cost.
Keys rotate on quota/rate errors; each key is its own 10k/day pool.

Verified live against the real API (video + forHandle channel lookups).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import requests

import keystats

API = "https://www.googleapis.com/youtube/v3/{}"
# 403 reasons worth burning the next key on (quota, rate, or a project
# where the API was never enabled — another key means another project).
ROTATE_REASONS = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded",
                  "userRateLimitExceeded", "accessNotConfigured"}
COST_LIST = 1
COST_SEARCH = 100


def _num(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_duration(iso: str) -> int:
    """ISO 8601 (PT1H2M3S) -> seconds. Garbage in -> 0."""
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", str(iso or ""))
    if not match:
        return 0
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def extract_id(ref: str) -> tuple[str, str]:
    """User input -> (kind, id). Kinds: video, channel, handle.

    Accepts raw IDs, @handles, and watch / youtu.be / shorts / channel URLs.
    """
    text = (ref or "").strip()
    if not text:
        raise ValueError("give me a video/channel URL, ID, or @handle")
    if text.startswith("@") and " " not in text and "/" not in text:
        return ("handle", text[1:])
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return ("video", text)
    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", text):
        return ("channel", text)
    if "://" not in text:
        text = "https://" + text
    try:
        parsed = urlparse(text)
    except ValueError as exc:
        raise ValueError(f"could not understand {ref!r}") from exc
    host, path = (parsed.netloc or "").lower(), parsed.path or ""
    if "youtu" not in host:
        raise ValueError(f"not a YouTube link: {ref!r}")
    query = parse_qs(parsed.query)
    if query.get("v"):
        return ("video", query["v"][0])
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
        return ("video", parts[1])
    if len(parts) >= 2 and parts[0] == "channel":
        return ("channel", parts[1])
    if parts and parts[0].startswith("@"):
        return ("handle", parts[0][1:])
    if host == "youtu.be" and parts:
        return ("video", parts[0])
    raise ValueError(f"could not find a video or channel in {ref!r}")


class YouTubeClient:
    """Thin client with key rotation. Tracks quota spent this run."""

    def __init__(self, keys: list[str] | str) -> None:
        if isinstance(keys, str):
            keys = [keys]
        self.keys = [k.strip() for k in keys if k and k.strip()]
        if not self.keys:
            raise RuntimeError("No YouTube API keys. Enable YouTube Data API v3 at "
                               "https://console.cloud.google.com/apis/library/"
                               "youtube.googleapis.com, then put the key in "
                               "config.yaml (youtube.api_keys).")
        self.spent = 0

    @staticmethod
    def _reason(status: int, payload: dict, text: str) -> str:
        try:
            errors = payload.get("error", {}).get("errors", [])
            if errors and errors[0].get("reason"):
                return str(errors[0]["reason"])
        except (AttributeError, IndexError, TypeError):
            pass
        if status in (400,) and "API key not valid" in text:
            return "keyInvalid"
        return ""

    def _get(self, method: str, params: dict, cost: int = COST_LIST) -> dict:
        keys = list(self.keys)
        last = "no keys tried"
        while keys:
            key = keys.pop(0)
            try:
                response = requests.get(API.format(method),
                                        params={**params, "key": key},
                                        timeout=30)
            except Exception as exc:
                last = str(exc)[:150]
                continue
            if response.status_code == 200:
                self.spent += cost
                keystats.bump("youtube", key, units=cost)
                try:
                    return response.json()
                except ValueError as exc:
                    raise RuntimeError(
                        "YouTube returned a non-JSON payload") from exc
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            reason = self._reason(response.status_code, payload, response.text)
            last = f"{reason or response.status_code}: {response.text[:150]}"
            # Dead key -> drop it, next key. Bad parameter (malformed ID)
            # -> fatal at once, no key will fix it.
            if response.status_code == 400:
                if reason == "keyInvalid":
                    print(f"  [yt] key ...{key[-4:]} invalid — trying next key")
                    continue
                raise RuntimeError(f"YouTube rejected the request: {last}")
            if response.status_code == 403 and reason in ROTATE_REASONS:
                print(f"  [yt] key ...{key[-4:]} hit {reason} — trying next key")
                continue
            if response.status_code == 403:
                raise RuntimeError(f"YouTube refused the request: {last}")
            raise RuntimeError(f"YouTube HTTP {response.status_code}: {last}")
        raise RuntimeError(f"YouTube unavailable (all keys exhausted): {last}")


def video_stats(client: YouTubeClient, video_id: str) -> dict:
    """One video's vitals. 1 quota unit. Raises when not found/private."""
    data = client._get("videos", {"part": "snippet,statistics,contentDetails",
                                  "id": video_id})
    items = data.get("items") or []
    if not items:
        raise RuntimeError(f"video {video_id} not found (deleted or private?)")
    item, snippet, stats = items[0], {}, {}
    snippet = item.get("snippet", {}) or {}
    stats = item.get("statistics", {}) or {}
    duration = parse_duration((item.get("contentDetails", {}) or {}).get("duration"))
    return {"id": item.get("id", video_id),
            "title": snippet.get("title", "?"),
            "channel": snippet.get("channelTitle", "?"),
            "channel_id": snippet.get("channelId", ""),
            "published": (snippet.get("publishedAt", "") or "")[:10],
            "views": _num(stats.get("viewCount")),
            "likes": _num(stats.get("likeCount")),
            "comments": _num(stats.get("commentCount")),
            "duration_s": duration}


def channel_stats(client: YouTubeClient, ref: str) -> dict:
    """A channel's vitals by ID or @handle. 1 quota unit."""
    kind, ident = extract_id(ref) if not ref.startswith("UC") else ("channel", ref)
    if kind == "handle":
        params = {"part": "snippet,statistics", "forHandle": ident}
    elif kind == "channel":
        params = {"part": "snippet,statistics", "id": ident}
    else:
        raise RuntimeError(f"{ref!r} is a video, not a channel")
    data = client._get("channels", params)
    items = data.get("items") or []
    if not items:
        raise RuntimeError(f"channel {ref} not found")
    item = items[0]
    snippet = item.get("snippet", {}) or {}
    stats = item.get("statistics", {}) or {}
    return {"id": item.get("id", ""),
            "title": snippet.get("title", "?"),
            "handle": snippet.get("customUrl", "?"),
            "subs": _num(stats.get("subscriberCount")),
            "videos": _num(stats.get("videoCount")),
            "views": _num(stats.get("viewCount")),
            "created": (snippet.get("publishedAt", "") or "")[:10]}


def search_shorts(client: YouTubeClient, query: str,
                  max_results: int = 10) -> list[dict]:
    """Top Shorts for a niche query, by view count. 100 quota units!

    Search never returns view counts, so this is titles + IDs — follow up
    with video_stats (1 unit each) on whatever looks interesting.
    """
    data = client._get("search", {"part": "snippet", "type": "video",
                                  "videoDuration": "short", "order": "viewCount",
                                  "maxResults": max(1, min(50, max_results)),
                                  "q": query}, cost=COST_SEARCH)
    out = []
    for item in data.get("items") or []:
        snippet = item.get("snippet", {}) or {}
        video_id = (item.get("id", {}) or {}).get("videoId", "")
        if video_id:
            out.append({"id": video_id, "title": snippet.get("title", "?"),
                        "channel": snippet.get("channelTitle", "?"),
                        "published": (snippet.get("publishedAt", "") or "")[:10]})
    return out


# ---------------------------------------------------------- snapshots
def uploads_playlist_id(client: YouTubeClient, channel_id: str) -> str:
    """A channel's uploads playlist. 1 unit."""
    data = client._get("channels", {"part": "contentDetails", "id": channel_id})
    items = data.get("items") or []
    if not items:
        raise RuntimeError(f"channel {channel_id} not found")
    playlist = ((items[0].get("contentDetails") or {})
                .get("relatedPlaylists") or {}).get("uploads", "")
    if not playlist:
        raise RuntimeError(f"channel {channel_id} exposes no uploads playlist")
    return playlist


def playlist_video_ids(client: YouTubeClient, playlist_id: str,
                       limit: int = 200) -> list[str]:
    """Video IDs of a playlist, newest first. 1 unit per 50."""
    ids: list[str] = []
    token = ""
    while len(ids) < limit:
        params = {"part": "contentDetails", "playlistId": playlist_id,
                  "maxResults": 50}
        if token:
            params["pageToken"] = token
        data = client._get("playlistItems", params)
        for item in data.get("items") or []:
            video_id = ((item.get("contentDetails") or {})
                        .get("videoId") or "")
            if video_id:
                ids.append(video_id)
        token = data.get("nextPageToken") or ""
        if not token:
            break
    return ids[:limit]


def chunk_ids(ids: list[str], size: int = 50) -> list[list[str]]:
    """Batch IDs for one videos.list call each (pure, tested)."""
    return [ids[i:i + size] for i in range(0, len(ids), size)]


def snapshot_videos(client: YouTubeClient, ids: list[str]) -> list[dict]:
    """Vitals for many videos, 50 per call (1 unit per call, tested)."""
    out: list[dict] = []
    for batch in chunk_ids(ids):
        data = client._get("videos", {"part": "snippet,statistics",
                                      "id": ",".join(batch)})
        for item in data.get("items") or []:
            snippet = item.get("snippet", {}) or {}
            stats = item.get("statistics", {}) or {}
            out.append({"id": item.get("id", ""),
                        "title": snippet.get("title", "?"),
                        "channel": snippet.get("channelTitle", "?"),
                        "channel_id": snippet.get("channelId", ""),
                        "published": (snippet.get("publishedAt", "") or "")[:10],
                        "views": _num(stats.get("viewCount")),
                        "likes": _num(stats.get("likeCount")),
                        "comments": _num(stats.get("commentCount"))})
    return out


def video_age_days(published: str, today: str) -> int:
    """Whole days between two YYYY-MM-DD dates (pure, tested)."""
    from datetime import date

    try:
        then = date.fromisoformat((published or "")[:10])
        now = date.fromisoformat(today)
    except ValueError:
        return 0
    return max(0, (now - then).days)


def load_snapshots(path) -> dict:
    """Snapshot history: channels, days, retitle-flagged (corrupt-safe)."""
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"channels": [], "days": [], "flagged": []}
    if not isinstance(data, dict):
        return {"channels": [], "days": [], "flagged": []}
    data.setdefault("channels", [])
    data.setdefault("days", [])
    data.setdefault("flagged", [])
    return data


def save_snapshots(path, history: dict) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, indent=1), encoding="utf-8")


def record_snapshot(history: dict, videos: list[dict], today: str) -> dict:
    """Store today's numbers (re-running the same day replaces, tested)."""
    entry = {"date": today, "videos": {v["id"]: {
        "title": v["title"], "channel": v["channel"], "views": v["views"],
        "likes": v["likes"], "comments": v["comments"],
        "published": v["published"]} for v in videos}}
    history["days"] = [d for d in history["days"] if d.get("date") != today]
    history["days"].append(entry)
    history["days"].sort(key=lambda d: d.get("date") or "")
    return history


def previous_daysnapshot(history: dict, today: str) -> dict | None:
    """The most recent snapshot strictly before today (tested)."""
    earlier = [d for d in history["days"] if d.get("date") < today]
    return earlier[-1] if earlier else None


def build_report(history: dict, today: str,
                 prev: dict | None) -> tuple[str, list[str]]:
    """Human report + newly flagged video IDs (pure, tested).

    Rules (2026-09-20 operating decisions): retitle once when a video
    aged >= 2 days still sits under 50 views. Retention-based kill-list
    rules need the Analytics API (OAuth) — public data can't see them.
    """
    current = next((d for d in history["days"]
                    if d.get("date") == today), None)
    if current is None:
        return ("no snapshot for today", [])
    prev_videos = (prev or {}).get("videos") or {}
    flagged = set(history.get("flagged") or [])
    new_flags: list[str] = []
    lines = [f"Channel snapshot — {today}"
             + (f" (vs {prev['date']})" if prev else " (first snapshot)")]
    total_now = total_delta = 0
    rows = []
    for video_id, v in current["videos"].items():
        before = prev_videos.get(video_id) or {}
        delta = v["views"] - before.get("views", v["views"]) \
            if video_id in prev_videos else None
        total_now += v["views"]
        if delta is not None:
            total_delta += delta
        age = video_age_days(v.get("published", ""), today)
        flag = ""
        if age >= 2 and v["views"] < 50 and video_id not in flagged:
            flag = "RETITLE? (<50 views)"
            new_flags.append(video_id)
            flagged.add(video_id)
        rows.append((v.get("channel", "?"), v.get("title", "?"), v["views"],
                     delta, v.get("likes", 0), age, flag,
                     video_id not in prev_videos and bool(prev_videos)))
    lines.append(f"  {'VIDEO':<40} {'VIEWS':>7} {'+D':>6} {'LIKES':>6} "
                 f"{'AGE':>4}  FLAGS")
    for channel, title, views, delta, likes, age, flag, is_new in rows:
        label = title[:38] + ("*" if is_new else "")
        delta_s = f"+{delta}" if delta else ""
        lines.append(f"  {label:<40} {views:>7,} {delta_s:>6} {likes:>6,} "
                     f"{age:>3}d  {flag}".rstrip())
    lines.append(f"  TOTAL: {total_now:,} views"
                 + (f"  (+{total_delta:,} since last snap)" if total_delta else ""))
    return ("\n".join(lines), new_flags)
