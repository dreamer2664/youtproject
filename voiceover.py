"""Voiceover generation: ElevenLabs premium first, edge-tts fallback.

ElevenLabs (keys + voice_id in config) gives the more human voice, with word
timings from the with-timestamps endpoint. Anything goes wrong — no keys,
denied keys, spent quota, bad audio — and the run falls back to edge-tts
automatically, so voice can never block a render. Without ElevenLabs keys
this module behaves exactly as before: free edge-tts, no signup.

Word timings drive the subtitles. If timings are missing for a scene,
subtitles fall back to even word spacing — see subtitles.py.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import edge_tts
import requests

from config import Config
from editorial import scrub_narration

import keystats

TICKS_PER_SECOND = 10_000_000
ELEVEN_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"


@dataclass
class WordTiming:
    word: str
    start: float  # seconds from the start of this clip
    end: float


# Keys that answered 401 (revoked/dead) — dropped for the rest of the
# process so later scenes stop re-trying them (live log 2026-09-21: one
# dead key was retried every scene, 3 wasted calls per render). A 429 is
# quota, not death — those keys stay for the next day.
_DEAD_ELEVEN_KEYS: set[str] = set()


def _live_eleven_keys(cfg: Config) -> list[str]:
    """Configured ElevenLabs keys minus this run's dead ones (pure, tested)."""
    return [key for key in cfg.elevenlabs_api_keys
            if key not in _DEAD_ELEVEN_KEYS]


def _elevenlabs_available(cfg: Config) -> bool:
    return bool(_live_eleven_keys(cfg) and cfg.elevenlabs_voice_id)


def _today() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d")


def _budget_path(cfg: Config) -> Path:
    return Path(cfg.work_dir) / "voice_budget.json"


def _read_budget(path: Path, today: str) -> int:
    """Premium videos already spent today; stale or missing file = 0 (tested)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("used", 0)) if data.get("date") == today else 0
    except Exception:
        return 0


def premium_budget_allows(cfg: Config) -> bool:
    """True when today's ElevenLabs video budget is not spent (tested).

    ai.premium_voices caps how many videos per DAY use the premium voice
    in the generate/batch lanes; the rest render on edge-tts so a
    two-channel, 6-a-day cadence stays inside free-tier characters.
    """
    return _read_budget(_budget_path(cfg), _today()) < max(0, cfg.premium_voices)


def record_premium_use(cfg: Config) -> None:
    """Count one premium-voice video against today's budget."""
    path = _budget_path(cfg)
    used = _read_budget(path, _today()) + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": _today(), "used": used}),
                    encoding="utf-8")


def _elevenlabs_save(data: dict, dest: Path) -> list[WordTiming]:
    """Decode audio + char alignment into an MP3 and word timings."""
    audio = base64.b64decode(data.get("audio_base64") or "")
    if len(audio) <= 1000:
        raise RuntimeError("elevenlabs returned empty audio")
    dest.write_bytes(audio)
    align = data.get("alignment") or {}
    chars = align.get("characters") or []
    starts = align.get("character_start_times_seconds") or []
    ends = align.get("character_end_times_seconds") or []
    timings: list[WordTiming] = []
    word, wstart, wend = "", 0.0, 0.0
    for index, char in enumerate(chars):
        if str(char).isspace():
            if word:
                timings.append(WordTiming(word=word, start=wstart, end=wend))
                word = ""
        elif not word:
            word = str(char)
            wstart = float(starts[index]) if index < len(starts) else 0.0
            wend = float(ends[index]) if index < len(ends) else wstart
        else:
            word += str(char)
            wend = float(ends[index]) if index < len(ends) else wend
    if word:
        timings.append(WordTiming(word=word, start=wstart, end=wend))
    return timings


def _elevenlabs_synth(text: str, dest: Path, cfg: Config) -> list[WordTiming]:
    """Premium voice with word timings. Raises when every key fails."""
    last = "no keys tried"
    for key in _live_eleven_keys(cfg):
        try:
            response = requests.post(
                f"{ELEVEN_TTS_URL}/{cfg.elevenlabs_voice_id}/with-timestamps",
                headers={"xi-api-key": key, "Content-Type": "application/json"},
                json={"text": text, "model_id": cfg.elevenlabs_model,
                      "voice_settings": {"stability": cfg.elevenlabs_stability,
                                         "similarity_boost": cfg.elevenlabs_similarity,
                                         "style": cfg.elevenlabs_style,
                                         "use_speaker_boost": True}},
                timeout=180,
            )
        except Exception as exc:
            last = str(exc)[:120]
            continue
        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError(
                    "elevenlabs returned a non-JSON payload") from exc
            keystats.bump("elevenlabs", key, chars=len(text))
            return _elevenlabs_save(data, dest)
        last = f"HTTP {response.status_code}: {response.text[:150]}"
        if response.status_code in (401, 429):
            if response.status_code == 401:
                _DEAD_ELEVEN_KEYS.add(key)
                print(f"  [voice] elevenlabs key ...{key[-4:]} is dead — "
                      f"dropped for this run")
            else:
                print(f"  [voice] elevenlabs key ...{key[-4:]} failed "
                      f"(429 quota) — next key")
            continue
        # Any other status is the request/voice, not the key — stop.
        break
    raise RuntimeError(f"elevenlabs failed ({last})")


def split_sentences(text: str) -> list[str]:
    """Narration -> sentences (pure, tested)."""
    return [part for part in re.split(r"(?<=[.!?…])\s+", text.strip()) if part]


def build_ssml(text: str, voice: str, rate: str, pause_ms: int) -> str:
    """Narration -> SSML with a pause between sentences (pure, tested).

    The <break> tags kill edge-tts's machine-gun "bursts" delivery. The
    voice name rides along so the engine needs no extra configuration.
    """
    import xml.sax.saxutils as saxutils

    sentences = split_sentences(text) or [text]
    inner = f'<break time="{int(pause_ms)}ms"/>'.join(
        saxutils.escape(sentence) for sentence in sentences)
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        f'xml:lang="en-US"><voice name="{saxutils.escape(voice)}">'
        f'<prosody rate="{saxutils.escape(rate)}">{inner}</prosody>'
        "</voice></speak>"
    )


async def _synth(text: str, voice: str, rate: str, dest: Path) -> list[WordTiming]:
    communicate = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    timings: list[WordTiming] = []
    with open(dest, "wb") as handle:
        async for chunk in communicate.stream():
            kind = chunk.get("type")
            if kind == "audio":
                handle.write(chunk["data"])
            elif kind == "WordBoundary":
                offset = float(chunk.get("offset", 0))
                duration = float(chunk.get("duration", 0))
                timings.append(
                    WordTiming(
                        word=str(chunk.get("text", "")),
                        start=offset / TICKS_PER_SECOND,
                        end=(offset + duration) / TICKS_PER_SECOND,
                    )
                )
    return timings


def synthesise(
    text: str, dest: Path, cfg: Config, attempts: int = 3,
    allow_premium: bool = True,
) -> tuple[Path, list[WordTiming]]:
    """Render narration to an MP3 plus word timings.

    ElevenLabs first when configured, edge-tts otherwise (or on any
    ElevenLabs failure). Raises RuntimeError if every attempt fails.
    """
    text = scrub_narration(text)  # same scrub as captions: timings stay aligned
    dest.parent.mkdir(parents=True, exist_ok=True)
    if allow_premium and _elevenlabs_available(cfg):
        try:
            return dest, _elevenlabs_synth(text, dest, cfg)
        except Exception as exc:
            print(f"  [voice] elevenlabs failed ({exc}) — "
                  f"falling back to edge-tts")
    last_error: Exception | None = None
    use_ssml = cfg.sentence_pause_ms > 0

    for attempt in range(1, attempts + 1):
        try:
            if use_ssml:
                payload = build_ssml(text, cfg.voice, cfg.speech_rate,
                                   cfg.sentence_pause_ms)
            else:
                payload = text
            timings = asyncio.run(_synth(payload, cfg.voice, cfg.speech_rate, dest))
            if dest.exists() and dest.stat().st_size > 1000:
                return dest, timings
            raise RuntimeError("output file missing or empty")
        except Exception as exc:  # noqa: BLE001 - retry anything
            last_error = exc
            if use_ssml:
                # SSML rejected — drop to plain text and retry at once.
                use_ssml = False
                print(f"  [voice] SSML rejected ({exc}) — retrying as plain text")
                continue
            print(f"  [voice] attempt {attempt}/{attempts} failed: {exc}")
            time.sleep(3 * attempt)

    raise RuntimeError(f"voiceover failed after {attempts} attempts: {last_error}")


def generate_scene_audio(
    script, cfg: Config, out_dir: Path, allow_premium: bool = True,
) -> tuple[list[Path], list[list[tuple[float, float]]]]:
    """Render one MP3 per scene. Returns (paths, timings) in scene order.

    Timings are (start, end) seconds per word, relative to each clip's start.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    all_timings: list[list[tuple[float, float]]] = []
    total = len(script.scenes)
    if allow_premium and _elevenlabs_available(cfg):
        print(f"  [voice] elevenlabs {cfg.elevenlabs_model} "
              f"(voice {cfg.elevenlabs_voice_id[:8]}…)")
    else:
        print(f"  [voice] {cfg.voice} at {cfg.speech_rate}")

    for index, scene in enumerate(script.scenes, start=1):
        dest = out_dir / f"scene_{index:02d}.mp3"
        print(f"  [voice] {index}/{total}: {len(scene.narration.split())} words")
        _, timings = synthesise(scene.narration, dest, cfg,
                                allow_premium=allow_premium)
        paths.append(dest)
        all_timings.append([(t.start, t.end) for t in timings])

    return paths, all_timings


async def _list_voices(language_prefix: str) -> None:
    voices = await edge_tts.list_voices()
    for voice in sorted(voices, key=lambda v: v["ShortName"]):
        if voice["ShortName"].startswith(language_prefix):
            print(f"  {voice['ShortName']:<28} {voice.get('Gender', '?'):<8} {voice.get('Locale', '')}")


def list_voices(language_prefix: str = "en-") -> None:
    """Helper: python main.py voices"""
    asyncio.run(_list_voices(language_prefix))
