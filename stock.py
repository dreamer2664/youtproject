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

import keystats
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


# Alt text that announces unsafe content: Pexels alt usually names what is
# in frame, and a false positive only costs one candidate (free belt before
# the vision QC in vision.py).
_UNSAFE_ALT = ("nude", "naked", "sexy", "lingerie", "bikini", "erotic",
               "sensual", "topless", "nsfw", "porn")


def filter_unsafe(photos: list[dict]) -> list[dict]:
    """Drop photos whose alt text announces unsafe content (pure, tested)."""
    return [photo for photo in photos
            if not any(word in (photo.get("alt") or "").lower()
                       for word in _UNSAFE_ALT)]


def rank_photos(photos: list[dict], query: str) -> list[dict]:
    """All candidates best-first by alt-text relevance (pure, tested).

    Zero-overlap photos trail the ranked good ones only as a legacy
    fallback — the pexels_fetch walk tries them last (or never, when
    better ones exist), because they are the unrelated images this lane
    used to ship.
    """
    if not photos or not query:
        return list(photos)
    words = [w for w in re.sub(r"[^a-z0-9 ]", "", query.lower()).split()
             if len(w) > 2]
    if not words:
        return list(photos)
    scored = sorted(
        ((_alt_score(photo.get("alt") or "", words), i)
         for i, photo in enumerate(photos)),
        key=lambda t: (-t[0], t[1]))
    good = [photos[i] for score, i in scored if score > 0]
    rest = [photos[i] for score, i in scored if score <= 0]
    return good + rest or list(photos)


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
    ranked = rank_photos(photos, query)
    good = [photo for photo in ranked
            if _alt_score(photo.get("alt") or "", words) > 0]
    pool = good or ranked
    return pool[seed % len(pool)]


def fmt_image_timing(total: float, search: float, dl: float,
                     qc: float) -> str:
    """Timing breakdown for one image (pure, tested).

    Live chaos 2026-09-21 — "some images take 10s, some 40" — was
    undiagnosable because logs showed no timings. Now every image says
    where its seconds went.
    """
    return (f"{total:.1f}s (search {search:.1f} · dl {dl:.1f} "
            f"· qc {qc:.1f})")


def _is_image(body: bytes) -> bool:
    return len(body) >= 2000 and (body[:3] == b"\xff\xd8\xff"
                                  or body[:8] == b"\x89PNG\r\n\x1a\n")


def pexels_fetch(prompt: str, dest: Path, cfg, seed: int, attempts: int) -> Path:
    """Download one stock photo for an art prompt. Raises on total failure."""
    from director import plan_query

    keys = list(cfg.pexels_api_keys)
    if not keys:
        raise RuntimeError("no Pexels key")
    dest.parent.mkdir(parents=True, exist_ok=True)
    query = plan_query(prompt, cfg)
    portrait = cfg.format == "portrait"
    last_error = "unknown"
    t_search = t_dl = t_qc = 0.0
    t0 = time.time()
    for attempt in range(1, attempts + 1):
        # Page 1 first (most relevant); deeper pages only when earlier
        # attempts failed — seed-scattered pages shipped unrelated photos.
        page = 1 if attempt == 1 else (seed + attempt) % 4 + 1
        key = keys[0]
        t_s = time.time()
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
        t_search += time.time() - t_s
        keystats.bump("pexels", key, req=1)
        if response.status_code in (401, 403) and len(keys) > 1:
            keys.pop(0)  # bad key: the next one takes over
            continue
        if response.status_code == 429:
            last_error = "HTTP 429 (200/hour Pexels limit)"
            if len(keys) > 1:
                keys.append(keys.pop(0))  # rotate: another key's quota
                continue
            time.sleep(min(120.0, 15.0 * 2 ** (attempt - 1)))
            continue
        if response.status_code != 200:
            last_error = f"HTTP {response.status_code}: {response.text[:120]}"
            time.sleep(2 * attempt)
            continue
        try:
            photos = filter_unsafe(response.json().get("photos") or [])
        except ValueError:
            photos = []
        if not photos:
            if attempt == 1 and len(query.split()) > 1:
                # Over-specific query — retry once with just the first words.
                query = " ".join(query.split()[:2])
                continue
            raise RuntimeError(f"pexels: no photos for {query!r}")
        # Ranked candidates (best alt-text match first), rotated by seed so
        # repeat renders vary; vision QC then walks the top three — a
        # rejected photo means "next candidate", not "ship it anyway".
        candidates = rank_photos(photos, query)
        start = (seed + attempt) % len(candidates)
        ordered = candidates[start:] + candidates[:start]
        from vision import check_image
        rejected = ""
        for photo in ordered[:3]:
            src = photo.get("src") or {}
            url = (src.get("portrait") if portrait else src.get("landscape")) \
                or src.get("large") or src.get("medium") or src.get("original")
            if not url:
                continue
            t_d = time.time()
            try:
                body_resp = requests.get(url, timeout=60)
                body = body_resp.content
            except Exception as exc:
                last_error = f"download failed: {exc}"[:120]
                continue
            finally:
                t_dl += time.time() - t_d
            if not _is_image(body):
                last_error = f"download was not an image ({len(body)} bytes)"
                continue
            t_c = time.time()
            verdict = check_image(body, query, cfg,
                                  photo_key=str(photo.get("id")))
            t_qc += time.time() - t_c
            if not (verdict["safe"] and verdict["relevant"]):
                rejected = verdict["reason"] or "unsafe or off-topic"
                print(f"  [image] vision QC rejected photo {photo.get('id')} "
                      f"({rejected}) — next candidate")
                continue
            dest.write_bytes(body)
            credit = photo.get("photographer") or "unknown"
            print(f"  [image] pexels: {query!r} (photo {photo.get('id')} "
                  f"by {credit}) — "
                  f"{fmt_image_timing(time.time() - t0, t_search, t_dl, t_qc)}")
            return dest
        if rejected:
            last_error = f"vision QC rejected all candidates ({rejected})"
            continue
    raise RuntimeError(f"pexels failed after {attempts} attempts: {last_error}")
