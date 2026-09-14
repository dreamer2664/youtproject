"""AI image generation with automatic provider fallbacks.

Primary: Pollinations (free, keyless anonymous tier; an optional free token
raises the rate limit and removes the watermark). Fallbacks, tried in order
when the primary fails an image: Gemini image generation (same free Gemini
key as the scripts — no new signup).

There is no SLA on any provider, so every call is retried, failures fall
through to the next provider, and a total failure raises rather than
producing a silent broken video.

Each narrated scene gets `video.images_per_scene` images (default 3), each
framed as a different cinematic shot so the video cuts regularly instead of
sitting on one picture for 20 seconds.
"""

from __future__ import annotations

import base64
import hashlib
import random
import re
import threading
import time
import urllib.parse
from pathlib import Path

import requests

from config import IMAGE_PROVIDERS as PROVIDERS, Config

ENDPOINT = "https://image.pollinations.ai/prompt/{prompt}"

GEMINI_IMAGE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Current lane first, legacy lane as backup (404 advances, like text models).
GEMINI_IMAGE_MODELS = ["gemini-3.1-flash-image-preview", "gemini-2.5-flash-image"]

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


# ---------------------------------------------------------------------------
# Art direction (video.style). One config flag flips the whole genre:
#   photoreal — cinematic documentary look (the default),
#   cartoon   — flat 2D vector toon (mascot/brand friendly),
#   stickman  — whiteboard stick-figure explainer (the viral TikTok look).
# `direction` is PREPENDED to every final image prompt, so it steers any
# script provider (Gemini or template) and survives Pollinations' 900-char
# prompt cutoff. `brief` tells the Gemini script writer what to aim for in
# its image_prompt fields. `shots` are the per-image framing variants —
# camera-lens terms like "shallow depth of field" make no sense on a
# whiteboard, so drawn styles use plain view changes.
# ---------------------------------------------------------------------------
STYLES: dict[str, dict] = {
    "photoreal": {
        "direction": "",
        "brief": "Photorealistic, cinematic, specific. No text or watermarks in image.",
        "shots": SHOT_STYLES,
    },
    "cartoon": {
        "direction": (
            "flat 2D vector cartoon illustration, bold clean outlines, "
            "vivid solid colors, simple expressive shapes, no text, no watermark"
        ),
        "brief": (
            "Flat 2D vector cartoon: bold outlines, vivid solid colors, simple "
            "expressive shapes, one clear scene per image. No text or watermarks."
        ),
        "shots": [
            "wide shot of the full scene",
            "medium shot",
            "close-up",
            "side view",
            "high angle view",
            "new angle",
            "centered composition",
        ],
    },
    "stickman": {
        # Wording tested against Pollinations: leading with "stick figure
        # drawing, simple black stickman with round head and line limbs"
        # yields the hand-drawn look; "whiteboard doodle" alone degenerates
        # into abstract marker scribbles.
        "direction": (
            "stick figure drawing, simple black stick figures with round "
            "heads and single-stroke line limbs in expressive poses, sparse "
            "hand-drawn props, hand-drawn marker on off-white paper texture, "
            "minimalist, no text, no watermark"
        ),
        "brief": (
            "Stick figure drawing style: a NEW simple full scene per beat — "
            "simple black stick figures with round heads and line limbs in "
            "expressive poses, one or two hand-drawn props, marker on off-white "
            "paper. No text or watermarks."
        ),
        "shots": [
            "wide shot of the full scene",
            "closer view",
            "side view",
            "new angle",
            "centered composition",
        ],
    },
}


def style_spec(name: str) -> dict:
    """The art-direction entry for a style name; unknown names -> photoreal."""
    return STYLES.get(str(name).lower(), STYLES["photoreal"])


def stylize(prompt: str, style: str) -> str:
    """Prepend the style's direction so the final image matches video.style."""
    direction = style_spec(style)["direction"]
    return f"{direction}, {prompt}" if direction else prompt


# --- provider chain ------------------------------------------------------

def resolve_chain(cfg: Config) -> list[str]:
    """Primary + fallbacks, de-duplicated, order preserved."""
    chain = [cfg.image_provider]
    for name in cfg.image_fallbacks:
        if name not in chain:
            chain.append(name)
    return chain


def provider_ready(name: str, cfg: Config) -> tuple[bool, str]:
    """(usable, human reason) for one provider."""
    if name == "pollinations":
        return True, "token" if cfg.pollinations_token else "anonymous"
    if name == "gemini":
        return (bool(cfg.gemini_api_key),
                "key present" if cfg.gemini_api_key else "no Gemini key")
    return False, "unknown provider"


def describe_chain(cfg: Config) -> str:
    """One-line summary of the provider chain, for preflight and run headers."""
    parts = []
    for name in resolve_chain(cfg):
        ok, reason = provider_ready(name, cfg)
        if name == "pollinations":
            parts.append(f"pollinations ({cfg.image_model}, {reason})")
        else:
            parts.append(name if ok else f"{name} ({reason} — skipped)")
    return " → ".join(parts)


# Anonymous Pollinations allows roughly one request per 15 seconds — faster
# than that and the API answers HTTP 429 (or, sneakier, HTTP 200 with a
# placeholder image instead of yours). The pacer serialises request starts
# across all worker threads, so no setting can trip the limiter. A free
# registered token (auth.pollinations.ai) raises the allowance to ~1 req/5s.
MIN_REQUEST_INTERVAL = 16.0
MIN_REQUEST_INTERVAL_TOKEN = 6.0
_pace_lock = threading.Lock()
_last_request_start = 0.0


def _wait_for_slot(interval: float = MIN_REQUEST_INTERVAL) -> None:
    """Block until `interval` seconds passed since the last request start."""
    global _last_request_start
    with _pace_lock:
        wait = interval - (time.monotonic() - _last_request_start)
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


# --- Pollinations --------------------------------------------------------

def upstream_throttled(error_text: str) -> bool:
    """True when Pollinations' GPU provider is throttling them (pure, tested).

    Surfaced as our HTTP 500 ("Gen Sana request failed with 429:
    Per-user limit of 300 RPM exceeded..."). The "user" is Pollinations
    itself — their congestion, not our quota — so hammering helps nobody.
    """
    lowered = error_text.lower()
    return "per-user limit" in lowered or "300 rpm" in lowered


def pollinations_request(prompt: str, cfg: Config, seed: int) -> tuple[str, dict, dict]:
    """Pure builder: (url, params, headers) for one Pollinations image."""
    quoted = urllib.parse.quote(prompt[:900])
    url = ENDPOINT.format(prompt=quoted)
    params = {
        "width": cfg.image_width,
        "height": cfg.image_height,
        "seed": seed,
        "nologo": "true",
        "model": cfg.image_model,
    }
    headers = {"Authorization": f"Bearer {cfg.pollinations_token}"} if cfg.pollinations_token else {}
    return url, params, headers


def _pollinations_fetch(
    prompt: str,
    dest: Path,
    cfg: Config,
    seed: int,
    attempts: int = 6,
) -> Path:
    """Download one AI image from Pollinations. Raises on total failure."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    url, params, headers = pollinations_request(prompt, cfg, seed)
    interval = MIN_REQUEST_INTERVAL_TOKEN if cfg.pollinations_token else MIN_REQUEST_INTERVAL

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            _wait_for_slot(interval)
            response = requests.get(url, params=params, headers=headers or None,
                                    timeout=cfg.image_timeout)
            if response.status_code == 429:
                raise _RateLimited(_retry_after_seconds(response))
            if response.status_code != 200:
                detail = (response.text or "").strip().replace("\n", " ")[:160]
                raise RuntimeError(f"HTTP {response.status_code}" + (f": {detail}" if detail else ""))
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
            if upstream_throttled(str(exc)):
                delay = 45.0
                print(f"  [image] Pollinations' upstream is throttled (their "
                      f"congestion, not your quota) — waiting {delay:.0f}s...")
            else:
                delay = 3 * attempt
            time.sleep(delay)

    raise RuntimeError(f"image generation failed after {attempts} attempts: {last_error}")


# --- Gemini images -------------------------------------------------------

def gemini_payload(prompt: str) -> dict:
    """Pure builder: generateContent body requesting IMAGE + TEXT output."""
    return {
        "contents": [{"parts": [{"text": prompt[:2000]}]}],
        "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
    }


def gemini_extract(data: dict) -> bytes:
    """Pull image bytes out of a generateContent response. Raises ValueError."""
    blocked = (data.get("promptFeedback") or {}).get("blockReason")
    if blocked:
        raise ValueError(f"prompt blocked ({blocked})")
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        raise ValueError(f"unexpected response shape: {str(data)[:200]}")
    for part in parts or []:
        inline = part.get("inlineData") or part.get("inline_data") or {}
        encoded = inline.get("data")
        if encoded:
            try:
                raw = base64.b64decode(encoded)
            except Exception:
                continue
            if len(raw) >= 2000 and (raw[:3] == b"\xff\xd8\xff"
                                     or raw[:8] == b"\x89PNG\r\n\x1a\n"):
                return raw
            raise ValueError(f"inline data was not an image ({len(raw)} bytes)")
    texts = " ".join(str(part.get("text") or "") for part in parts or [])[:160]
    raise ValueError(f"no image in response ({texts or 'empty response'})")


def _gemini_fetch(prompt: str, dest: Path, cfg: Config, seed: int, attempts: int) -> Path:
    # NOTE: seed is unused — the Gemini image API takes no seed. Kept for
    # signature parity with the other providers.
    del seed
    dest.parent.mkdir(parents=True, exist_ok=True)
    key = cfg.gemini_api_key
    last_error = "unknown"
    for model in GEMINI_IMAGE_MODELS:
        for attempt in range(1, attempts + 1):
            try:
                response = requests.post(
                    GEMINI_IMAGE_URL.format(model=model),
                    params={"key": key},
                    json=gemini_payload(prompt),
                    timeout=cfg.image_timeout,
                )
            except Exception as exc:
                last_error = str(exc)[:160]
                if attempt < attempts:
                    time.sleep(3 * attempt)
                continue
            if response.status_code == 200:
                try:
                    dest.write_bytes(gemini_extract(response.json()))
                    return dest
                except ValueError as exc:
                    # Parsed fine but unusable (block/refusal) — the next
                    # model has different tuning, so it may still pass.
                    last_error = str(exc)[:200]
                    break
            elif response.status_code == 404:
                last_error = f"{model}: HTTP 404"
                break  # retired model id — try the next one
            elif response.status_code in (429, 500, 502, 503, 504):
                last_error = f"{model}: HTTP {response.status_code}"
                if attempt < attempts:
                    delay = min(2 ** attempt + random.uniform(0, 1), 30.0)
                    print(f"  [image] gemini {model}: HTTP {response.status_code}, "
                          f"retry in {delay:.0f}s")
                    time.sleep(delay)
            else:
                # Other 4xx = key/permissions — retrying is pointless.
                raise RuntimeError(f"gemini images rejected "
                                   f"(HTTP {response.status_code}): {response.text[:160]}")
    raise RuntimeError(f"gemini image failed: {last_error}")


_FETCH = {
    "pollinations": _pollinations_fetch,
    "gemini": _gemini_fetch,
}


# --- chain driver --------------------------------------------------------

def generate_image(
    prompt: str,
    dest: Path,
    cfg: Config,
    seed: int | None = None,
    attempts: int = 6,
) -> Path:
    """Generate one image, walking the provider chain until one delivers.

    Providers missing their key are skipped silently; each failure prints a
    note and the next provider is tried. The first usable provider gets the
    full `attempts` budget, later ones get 3 quick tries each.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if seed is None:
        seed = int(hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:8], 16) % 100000
    chain = [name for name in resolve_chain(cfg) if provider_ready(name, cfg)[0]]
    skipped = [name for name in resolve_chain(cfg) if not provider_ready(name, cfg)[0]]
    if not chain:
        reasons = "; ".join(f"{name}: {provider_ready(name, cfg)[1]}"
                            for name in resolve_chain(cfg))
        raise RuntimeError(f"no image provider usable ({reasons})")
    failures: list[str] = []
    for index, name in enumerate(chain):
        tries = attempts if index == 0 else 3
        try:
            _FETCH[name](prompt, dest, cfg, seed, tries)
            if index > 0:
                print(f"  [image] {name} covered after {chain[index - 1]} failed")
            return dest
        except Exception as exc:
            failures.append(f"{name}: {exc}")
            if index < len(chain) - 1:
                print(f"  [image] {name} failed — trying {chain[index + 1]} "
                      f"({str(exc)[:110]})")
    if skipped:
        failures.append(f"skipped ({', '.join(skipped)}: no key)")
    raise RuntimeError("all image providers failed: " + " | ".join(failures))


def generate_scene_images(script, cfg: Config, out_dir: Path) -> list[list[Path]]:
    """Generate images_per_scene images per scene. Returns paths grouped by scene.

    Each image walks the provider chain (primary, then fallbacks) until one
    provider delivers. Downloads run on ai.image_workers threads, but request
    *starts* are serialised by the rate-limit pacer (~1 per 15s for anonymous
    use), so extra workers only overlap download time — they don't multiply
    speed. Results are re-sorted into scene/slot order before returning, so
    callers see deterministic output.
    """
    import concurrent.futures

    print(f"  [image] providers: {describe_chain(cfg)}")
    out_dir.mkdir(parents=True, exist_ok=True)
    per_scene = cfg.images_per_scene
    shots = style_spec(cfg.style)["shots"]
    jobs: list[tuple[int, int, str, Path, int]] = []
    for index, scene in enumerate(script.scenes, start=1):
        base = stylize(scene.image_prompt, cfg.style)
        for slot in range(per_scene):
            if per_scene > 1:
                prompt = f"{base}, {shots[slot % len(shots)]}"
            else:
                prompt = base
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
