"""Pexels stock lane: real photos instead of AI renders (free API key).

Why: anonymous AI images look synthetic; real photography converts better,
which is exactly what a new channel needs. Pexels is free (200 req/hour,
20,000/month), commercial-use OK with no attribution required — a Pexels link
in the video description is appreciated but optional.

One search per scene slot, portrait orientation for Shorts. The director
brain (director.py) turns each art prompt into a short search query.
Signature matches the images.py provider table (prompt, dest, cfg, seed,
attempts) so the chain treats stock like any other provider.
"""

from __future__ import annotations

import random
import re
import time
from pathlib import Path

import requests

SEARCH_URL = "https://api.pexels.com/v1/search"


def pexels_params(query: str, *, portrait: bool, page: int, per_page: int = 3) -> dict:
    """Pure builder: search params for one query (tested, no network)."""
    return {"query": query,
            "orientation": "portrait" if portrait else "landscape",
            "size": "medium", "per_page": per_page, "page": max(1, page)}


def _alt_score(alt: str, query_words: list[str]) -> int:
    """Query words visible in the photo's alt text (plural-tolerant)."""
    tokens = [t for t in re.sub(r"[^a-z0-9 ]", "", (alt or "").lower()).split()
              if len(t) > 2]
    score = 0
    for word in query_words:
        if any(t == word or t.startswith(word) or word.startswith(t)
               for t in tokens):
            score += 1
    return score


def pick_photo(photos: list[dict], seed: int, query: str = "") -> dict | None:
    """Best alt-text match wins; the seed only varies across good ones.

    Zero-overlap photos are excluded while anything better exists (they are
    the unrelated images this lane used to ship); when nothing matches — or
    no query is given — legacy seed order applies, so this never blocks.
    Pure, tested.
    """
    if not photos:
        return None
    if not query:
        return photos[seed % len(photos)]
    words = [w for w in re.sub(r"[^a-z0-9 ]", "", query.lower()).split()
             if len(w) > 2]
    scored = [(_alt_score(photo.get("alt") or "", words), i)
              for i, photo in enumerate(photos)]
    ranked = [i for _, i in sorted(scored, key=lambda t: (-t[0], t[1]))]
    good = [i for i in ranked
            if _alt_score(photos[i].get("alt") or "", words) > 0] or ranked
    return photos[good[seed % len(good)]]


def _is_image(body: bytes) -> bool:
    return len(body) >= 2000 and (body[:3] == b"\xff\xd8\xff"
                                  or body[:8] == b"\x89PNG\r\n\x1a\n")


def pexels_fetch(prompt: str, dest: Path, cfg, seed: int, attempts: int) -> Path:
    """Download one stock photo for an art prompt. Raises on total failure."""
    from director import plan_query

    key = (cfg.pexels_api_key or "").strip()
    if not key:
        raise RuntimeError("no Pexels key")
    dest.parent.mkdir(parents=True, exist_ok=True)
    query = plan_query(prompt, cfg)
    portrait = cfg.format == "portrait"
    last_error = "unknown"
    for attempt in range(1, attempts + 1):
        # Page 1 first (most relevant); deeper pages only when earlier
        # attempts failed — seed-scattered pages shipped unrelated photos.
        page = 1 if attempt == 1 else (seed + attempt) % 4 + 1
        try:
            response = requests.get(
                SEARCH_URL, headers={"Authorization": key},
                params=pexels_params(query, portrait=portrait, page=page,
                                     per_page=5),
                timeout=30)
        except Exception as exc:
            last_error = str(exc)[:120]
            time.sleep(2 * attempt)
            continue
        if response.status_code == 429:
            last_error = "HTTP 429 (200/hour Pexels limit)"
            time.sleep(min(120.0, 15.0 * 2 ** (attempt - 1)))
            continue
        if response.status_code != 200:
            last_error = f"HTTP {response.status_code}: {response.text[:120]}"
            time.sleep(2 * attempt)
            continue
        try:
            photos = response.json().get("photos") or []
        except ValueError:
            photos = []
        if not photos:
            if attempt == 1 and len(query.split()) > 1:
                # Over-specific query — retry once with just the first words.
                query = " ".join(query.split()[:2])
                continue
            raise RuntimeError(f"pexels: no photos for {query!r}")
        photo = pick_photo(photos, seed + attempt, query)
        assert photo is not None
        src = photo.get("src") or {}
        url = (src.get("portrait") if portrait else src.get("landscape")) \
            or src.get("large") or src.get("medium") or src.get("original")
        if not url:
            last_error = "photo had no download URL"
            continue
        try:
            body_resp = requests.get(url, timeout=60)
            body = body_resp.content
        except Exception as exc:
            last_error = f"download failed: {exc}"[:120]
            continue
        if not _is_image(body):
            last_error = f"download was not an image ({len(body)} bytes)"
            continue
        dest.write_bytes(body)
        credit = photo.get("photographer") or "unknown"
        print(f"  [image] pexels: {query!r} (photo {photo.get('id')} by {credit})")
        return dest
    raise RuntimeError(f"pexels failed after {attempts} attempts: {last_error}")
