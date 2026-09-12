"""AI image generation via Pollinations.

Verified free and keyless: a plain GET to image.pollinations.ai returns a JPEG
with no API key, no signup and no billing. There is no SLA, so every call is
retried and failures are handled rather than fatal.
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
from pathlib import Path

import requests

from config import Config
from scriptgen import Scene

ENDPOINT = "https://image.pollinations.ai/prompt/{prompt}"


def _safe_slug(text: str, limit: int = 40) -> str:
    slug = re.sub(r"-+", "-", "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-"))
    return slug[:limit] or "image"


def generate_image(
    prompt: str,
    dest: Path,
    cfg: Config,
    seed: int | None = None,
    attempts: int = 3,
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
        "model": "flux",
    }

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, params=params, timeout=cfg.image_timeout)
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            body = response.content
            # Pollinations returns an error payload with a 200 on some failures.
            if len(body) < 2000 or not (body[:3] == b"\xff\xd8\xff" or body[:8] == b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError(f"response was not an image ({len(body)} bytes)")
            dest.write_bytes(body)
            return dest
        except Exception as exc:  # noqa: BLE001 - retry anything
            last_error = exc
            print(f"  [image] attempt {attempt}/{attempts} failed: {exc}")
            time.sleep(3 * attempt)

    raise RuntimeError(f"image generation failed after {attempts} attempts: {last_error}")


def generate_scene_images(script, cfg: Config, out_dir: Path) -> list[Path]:
    """Generate one image per scene. Returns paths in scene order."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    total = len(script.scenes)

    for index, scene in enumerate(script.scenes, start=1):
        name = f"scene_{index:02d}_{_safe_slug(scene.image_prompt)}.jpg"
        dest = out_dir / name
        print(f"  [image] {index}/{total}: {scene.image_prompt[:70]}...")
        generate_image(scene.image_prompt, dest, cfg, seed=1000 + index)
        paths.append(dest)

    return paths
