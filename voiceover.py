"""Voiceover generation with edge-tts.

edge-tts is free and needs no API key. It talks to an unofficial Microsoft
endpoint, so it has no SLA — every call is retried and the caller is told
clearly if voice is unavailable.

Word timings come from WordBoundary events (100ns ticks) and drive the
subtitles. If timings are missing for a scene, subtitles fall back to even
word spacing — see subtitles.py.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import edge_tts

from config import Config

TICKS_PER_SECOND = 10_000_000


@dataclass
class WordTiming:
    word: str
    start: float  # seconds from the start of this clip
    end: float


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
    text: str, dest: Path, cfg: Config, attempts: int = 3
) -> tuple[Path, list[WordTiming]]:
    """Render narration to an MP3 plus word timings.

    Raises RuntimeError if every attempt fails.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            timings = asyncio.run(_synth(text, cfg.voice, cfg.speech_rate, dest))
            if dest.exists() and dest.stat().st_size > 1000:
                return dest, timings
            raise RuntimeError("output file missing or empty")
        except Exception as exc:  # noqa: BLE001 - retry anything
            last_error = exc
            print(f"  [voice] attempt {attempt}/{attempts} failed: {exc}")
            time.sleep(3 * attempt)

    raise RuntimeError(f"voiceover failed after {attempts} attempts: {last_error}")


def generate_scene_audio(
    script, cfg: Config, out_dir: Path
) -> tuple[list[Path], list[list[tuple[float, float]]]]:
    """Render one MP3 per scene. Returns (paths, timings) in scene order.

    Timings are (start, end) seconds per word, relative to each clip's start.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    all_timings: list[list[tuple[float, float]]] = []
    total = len(script.scenes)
    print(f"  [voice] {cfg.voice} at {cfg.speech_rate}")

    for index, scene in enumerate(script.scenes, start=1):
        dest = out_dir / f"scene_{index:02d}.mp3"
        print(f"  [voice] {index}/{total}: {len(scene.narration.split())} words")
        _, timings = synthesise(scene.narration, dest, cfg)
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
