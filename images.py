"""AI image generation via Pollinations.

Verified free and keyless: a plain GET to image.pollinations.ai returns a JPEG
with no API key, no signup and no billing. There is no SLA, so every call is
retried and failures are handled rather than fatal.

Each narrated scene gets `video.images_per_scene` images (default 3), each
framed as a different cinematic shot so the video cuts regularly instead of
sitting on one picture for 20 seconds.
"""

from __future__ import annotations

import hashlib
import random
import re
import threading
import time
import urllib.parse
from pathlib import Path

import requests

from config import Config

ENDPOINT = "https://image.pollinations.ai/prompt/{prompt}"

# Cycled through when a scene needs several images. All framings are
# subject-agnostic on purpose — they must make sense appended to any prompt.
SHOT_STYLES = [
    "wide establishing shot",
    "medium shot from a new angle",
    "close-up detail",
    "dramatic low angle",
    "aerial view",
    "over-the-shoulder perspective",
    "extreme close-up",
    "high angle view",
    "side profile view",
    "symmetrical centered composition",
    "shallow depth of field",
    "moody cinematic lighting",
]


# Anonymous Pollinations allows roughly one request per 15 seconds — faster
# than that and the API answers HTTP 429 (or, sneakier, HTTP 200 with a
# placeholder image instead of yours). The pacer serialises request starts
# across all worker threads, so no setting can trip the limiter.
MIN_REQUEST_INTERVAL = 16.0
_pace_lock = threading.Lock()
_last_request_start = 0.0


def _wait_for_slot() -> None:
    """Block until MIN_REQUEST_INTERVAL has passed since the last request."""
    global _last_request_start
    with _pace_lock:
        wait = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_start)
        if wait > 0:
            if wait > 3:
                print(f"  [image] pacing: next request in {wait:.0f}s (rate limit)")
            time.sleep(wait)
        _last_request_start = time.monotonic()


# Known Pollinations rate-limit placeholder: HTTP 200 whose body is this
# image instead of yours (pollinations/pollinations#7207). A matching MD5
# is treated exactly like a 429 and retried with backoff.
_RATE_LIMIT_PLACEHOLDER_MD5 = "2090a5dc21c32952cbf8496339752bd1"


class _RateLimited(Exception):
    """Pollinations said slow down. Carries an optional Retry-After (seconds)."""

    def __init__(self, retry_after: float | None) -> None:
        super().__init__("HTTP 429")
        self.retry_after = retry_after


def _retry_after_seconds(response) -> float | None:
    """Parse a Retry-After response header (delta-seconds form)."""
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(1.0, float(str(raw).strip()))
    except ValueError:
        return None  # HTTP-date form — ignore, exponential backoff covers it


def _safe_slug(text: str, limit: int = 40) -> str:
    slug = re.sub(r"-+", "-", "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-"))
    return slug[:limit] or "image"


def generate_image(
    prompt: str,
    dest: Path,
    cfg: Config,
    seed: int | None = None,
    attempts: int = 6,
) -> Path:
    """Download one AI image. Raises RuntimeError if every attempt fails."""
    dest.parent.mkdir(parents=True, exist_ok=True)

    if seed is None:
        seed = int(hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:8], 16) % 100000

    quoted = urllib.parse.quote(prompt[:900])
    url = ENDPOINT.format(prompt=quoted)
    params = {
        "width": cfg.image_width,
        "height": cfg.image_height,
        "seed": seed,
        "nologo": "true",
        "model": cfg.image_model,
    }

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            _wait_for_slot()
            response = requests.get(url, params=params, timeout=cfg.image_timeout)
            if response.status_code == 429:
                raise _RateLimited(_retry_after_seconds(response))
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            body = response.content
            # Pollinations returns an error payload with a 200 on some failures.
            if len(body) < 2000 or not (body[:3] == b"\xff\xd8\xff" or body[:8] == b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError(f"response was not an image ({len(body)} bytes)")
            if hashlib.md5(body).hexdigest() == _RATE_LIMIT_PLACEHOLDER_MD5:
                # Stealth rate limit: HTTP 200 with a placeholder image.
                raise _RateLimited(None)
            if len(body) > 800_000:
                print(f"  [image] warning: unusually large response "
                      f"({len(body) // 1024} KB) — keeping it")
            dest.write_bytes(body)
            return dest
        except _RateLimited as exc:
            last_error = exc
            if attempt == attempts:
                break
            delay = exc.retry_after or min(120.0, 15.0 * 2 ** (attempt - 1))
            delay *= random.uniform(0.8, 1.2)
            print(f"  [image] rate-limited, retry in {delay:.0f}s "
                  f"(attempt {attempt}/{attempts})")
            time.sleep(delay)
        except Exception as exc:  # noqa: BLE001 - retry anything
            last_error = exc
            print(f"  [image] attempt {attempt}/{attempts} failed: {exc}")
            if attempt == attempts:
                break
            time.sleep(3 * attempt)

    raise RuntimeError(f"image generation failed after {attempts} attempts: {last_error}")


def generate_scene_images(script, cfg: Config, out_dir: Path) -> list[list[Path]]:
    """Generate images_per_scene images per scene. Returns paths grouped by scene.

    Downloads run concurrently (ai.image_workers threads) — Pollinations
    queues each request server-side, so 3 parallel requests finish roughly
    3x faster than one-at-a-time. Results are re-sorted into scene/slot
    order before returning, so callers see deterministic output.
    """
    import concurrent.futures

    out_dir.mkdir(parents=True, exist_ok=True)
    per_scene = cfg.images_per_scene
    jobs: list[tuple[int, int, str, Path, int]] = []
    for index, scene in enumerate(script.scenes, start=1):
        for slot in range(per_scene):
            if per_scene > 1:
                prompt = f"{scene.image_prompt}, {SHOT_STYLES[slot % len(SHOT_STYLES)]}"
            else:
                prompt = scene.image_prompt
            name = f"scene_{index:02d}_{slot + 1}of{per_scene}_{_safe_slug(prompt)}.jpg"
            jobs.append((index, slot, prompt, out_dir / name, 1000 + index * 100 + slot))

    total_scenes = len(script.scenes)
    workers = min(cfg.image_workers, len(jobs))
    print(f"  [image] generating {len(jobs)} images "
          f"({workers} parallel, {cfg.image_model})...")
    done = 0
    results: dict[tuple[int, int], Path] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_job = {
            pool.submit(generate_image, prompt, dest, cfg, seed): (index, slot)
            for index, slot, prompt, dest, seed in jobs
        }
        try:
            for future in concurrent.futures.as_completed(future_to_job):
                index, slot = future_to_job[future]
                results[(index, slot)] = future.result()  # raises on failure
                done += 1
                print(f"  [image] done {done}/{len(jobs)} "
                      f"(scene {index}/{total_scenes} shot {slot + 1}/{per_scene})")
        except BaseException:
            for future in future_to_job:
                future.cancel()
            raise

    return [[results[(index, slot)] for slot in range(per_scene)]
            for index in range(1, total_scenes + 1)]
