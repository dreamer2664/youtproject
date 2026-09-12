"""Voiceover generation with edge-tts.

edge-tts is free and needs no API key. It talks to an unofficial Microsoft
endpoint, so it has no SLA — every call is retried and the caller is told
clearly if voice is unavailable.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import edge_tts

from config import Config


async def _synth(text: str, voice: str, dest: Path) -> None:
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(dest))


def synthesise(text: str, dest: Path, cfg: Config, attempts: int = 3) -> Path:
    """Render narration to an MP3. Raises RuntimeError if every attempt fails."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            asyncio.run(_synth(text, cfg.voice, dest))
            if dest.exists() and dest.stat().st_size > 1000:
                return dest
            raise RuntimeError("output file missing or empty")
        except Exception as exc:  # noqa: BLE001 - retry anything
            last_error = exc
            print(f"  [voice] attempt {attempt}/{attempts} failed: {exc}")
            time.sleep(3 * attempt)

    raise RuntimeError(f"voiceover failed after {attempts} attempts: {last_error}")


def generate_scene_audio(script, cfg: Config, out_dir: Path) -> list[Path]:
    """Render one MP3 per scene. Returns paths in scene order."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    total = len(script.scenes)

    for index, scene in enumerate(script.scenes, start=1):
        dest = out_dir / f"scene_{index:02d}.mp3"
        print(f"  [voice] {index}/{total}: {len(scene.narration.split())} words")
        synthesise(scene.narration, dest, cfg)
        paths.append(dest)

    return paths


async def _list_voices(language_prefix: str) -> None:
    voices = await edge_tts.list_voices()
    for voice in sorted(voices, key=lambda v: v["ShortName"]):
        if voice["ShortName"].startswith(language_prefix):
            print(f"  {voice['ShortName']:<28} {voice.get('Gender', '?'):<8} {voice.get('Locale', '')}")


def list_voices(language_prefix: str = "en-") -> None:
    """Helper: python -c 'import voiceover; voiceover.list_voices()'"""
    asyncio.run(_list_voices(language_prefix))
