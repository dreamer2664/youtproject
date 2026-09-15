"""Pexels B-roll client: portrait stock clips for a future real-footage style.

Pure fetch layer only — NOT wired into the render path yet. search_clips
returns direct mp4 URLs (hotlinkable, free license, no watermark); the
assembler integration (trim-to-scene + caption burn) comes later.
"""

from __future__ import annotations

import requests

VIDEO_SEARCH_URL = "https://api.pexels.com/videos/search"
REQUEST_TIMEOUT = 30


def search_clips(api_key: str, query: str, per_page: int = 5) -> list[dict]:
    """Top portrait mp4 clips for `query`, best file each. Raises RuntimeError."""
    try:
        response = requests.get(
            VIDEO_SEARCH_URL,
            headers={"Authorization": api_key},
            params={"query": query, "per_page": max(1, min(20, per_page)),
                    "orientation": "portrait"},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:
        raise RuntimeError(f"Pexels unreachable: {exc}") from exc
    if response.status_code in (401, 403):
        raise RuntimeError("Pexels key rejected (401/403) — check the key.")
    if response.status_code != 200:
        raise RuntimeError(f"Pexels HTTP {response.status_code}: {response.text[:160]}")
    try:
        videos = response.json().get("videos", [])
    except ValueError:
        raise RuntimeError("Pexels returned non-JSON.")
    clips = []
    for video in videos:
        picked = _pick_file(video)
        if picked:
            clips.append({
                "id": video.get("id"),
                "page": video.get("url", ""),
                "file": picked["link"],
                "width": picked.get("width", 0),
                "height": picked.get("height", 0),
                "fps": picked.get("fps", 30),
                "duration": video.get("duration", 0),
                "preview": video.get("image", ""),
            })
    return clips


def _pick_file(video: dict) -> dict | None:
    """Best mp4: portrait first, else largest mp4, else None."""
    files = [item for item in (video.get("video_files") or [])
             if item.get("file_type") == "video/mp4" and item.get("link")]
    if not files:
        return None
    portrait = [item for item in files
                if (item.get("height") or 0) >= (item.get("width") or 0)]
    pool = portrait or files
    return max(pool, key=lambda item: (item.get("height") or 0) * (item.get("width") or 0))
