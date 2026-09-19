"""Vision QC: a model looks at every stock photo before it ships.

Why: the viewer's eye lands on the image before the words — a great script
over unrelated or unsafe photos loses to a mediocre script over perfect
ones (channel analytics, 2026-09-19). Patch #9 ranks candidates by Pexels
alt text; this module adds eyes: the chosen photo is checked for safety
(nudity, gore, shock) and relevance (does it actually show the query?).

Cost: ~350-500 tokens per image, ~21 images per video = ~9k tokens —
about half a script's worth, on the same free Gemini keys. The daily
video ceiling (script-LLM requests + render time) is untouched.

Philosophy: fail-open, never block. Any error (no key, network, quota,
bad JSON) passes the image unchecked — the render always continues. A
circuit breaker stops calling after consecutive total failures so a dead
host adds seconds, not minutes. Verdicts are cached by (photo, query) so
repeated candidates across videos cost nothing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading

import requests

from config import Config

URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
       "{model}:generateContent")
# Fast multimodal IDs — QC needs eyes, not brains. Order mirrors the
# script chain's live survey (scriptgen, 2026-09-16): lite always answers,
# previews/3.5 catch lite outages.
MODELS = ["gemini-flash-lite-latest", "gemini-3-flash-preview",
          "gemini-3.5-flash"]
MAX_REQUESTS = 4   # best-effort: a render never waits on QC retries
BREAKER_TRIP = 3   # consecutive total failures -> stop calling this run
TIMEOUT = 20

PROMPT = """You are the safety and relevance filter for a family-friendly facts channel.
This photo is a candidate for a video about: "{query}".
Reply ONLY with JSON: {{"safe": true or false, "relevant": true or false, "reason": "max 8 words"}}
safe = false for: nudity or suggestive clothing or poses, gore, shocking content, or anything unsuitable for a child.
relevant = false only if the photo shows something clearly different from the query subject.
When unsure, reply true — a wrong rejection wastes a good image."""

PASS = {"safe": True, "relevant": True, "reason": ""}

_state = {"fails": 0}
_lock = threading.Lock()


def _coerce(data) -> dict:
    """Model reply -> trusted-shape verdict (pure, tested)."""
    if not isinstance(data, dict):
        return dict(PASS)

    def flag(name: str) -> bool:
        value = data.get(name, True)
        if isinstance(value, str):
            return value.strip().lower() not in ("false", "no", "0")
        return bool(value)

    return {"safe": flag("safe"), "relevant": flag("relevant"),
            "reason": str(data.get("reason") or "")[:80]}


def _load_cache(cfg: Config) -> dict:
    path = cfg.work_dir / "vision_cache.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - cache is best-effort
        return {}


def _save_cache(cfg: Config, cache: dict) -> None:
    try:
        path = cfg.work_dir / "vision_cache.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:  # noqa: BLE001 - cache is best-effort
        pass


def check_image(body: bytes, query: str, cfg: Config,
                photo_key: str = "") -> dict:
    """Verdict on one candidate image: {safe, relevant, reason}.

    Never raises; fails open on every error path. Cached by photo+query.
    """
    if not cfg.vision_qc or not body:
        return dict(PASS)
    keys = [k for k in cfg.gemini_api_keys if k]
    if not keys:
        return dict(PASS)
    digest = hashlib.sha1(
        f"{photo_key}|{query}".encode("utf-8")).hexdigest()
    with _lock:
        if _state["fails"] >= BREAKER_TRIP:
            return dict(PASS)
        cached = _load_cache(cfg).get(digest)
        if cached is not None:
            return _coerce(cached)

    mime = "image/png" if body[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    payload = {
        "contents": [{"parts": [
            {"text": PROMPT.format(query=query)},
            {"inline_data": {"mime_type": mime,
                             "data": base64.b64encode(body).decode("ascii")}},
        ]}],
        "generationConfig": {"temperature": 0.0,
                             "response_mime_type": "application/json"},
    }
    verdict: dict | None = None
    spent = 0
    for model in MODELS:
        for key in keys:
            if spent >= MAX_REQUESTS:
                break
            spent += 1
            try:
                resp = requests.post(URL.format(model=model),
                                     params={"key": key}, json=payload,
                                     timeout=TIMEOUT)
            except requests.RequestException:
                continue
            if resp.status_code in (400, 401, 403):  # key problem: next key
                continue
            if resp.status_code == 404:              # dead ID: next model
                break
            if resp.status_code != 200:              # 429/5xx: next key
                continue
            try:
                text = (resp.json()["candidates"][0]["content"]["parts"][0]
                        .get("text") or "")
            except (ValueError, KeyError, IndexError):
                continue
            from scriptgen import extract_json
            try:
                verdict = _coerce(extract_json(text))
            except Exception:  # noqa: BLE001 - unparseable: next key
                continue
            break
        if verdict is not None or spent >= MAX_REQUESTS:
            break

    with _lock:
        if verdict is None:
            _state["fails"] += 1
            return dict(PASS)
        _state["fails"] = 0
        cache = _load_cache(cfg)
        cache[digest] = verdict
        _save_cache(cfg, cache)
        return dict(verdict)
