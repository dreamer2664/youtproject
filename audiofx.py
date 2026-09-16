"""Procedural audio candy: music bed loop, transition whooshes, CTA pop.

Everything here is synthesized with the Python standard library — no
downloads, no numpy, no licensing: the music bed is an original chord pad,
so it is 100% royalty-free and monetization-safe. To use your own track
instead, drop it in music/ (see music/README.txt) or set music.file.
"""

from __future__ import annotations

import math
import random
import struct
import wave
from array import array
from pathlib import Path

from config import Config

SAMPLE_RATE = 48000

LOOP_SECONDS = 12.0
WHOOSH_SECONDS = 0.5
POP_SECONDS = 0.3

# I–V–vi–IV in A major, Hz. Warm and neutral — sits under any narration.
_CHORDS = (
    (110.00, 138.59, 164.81),  # A major
    (82.41, 123.47, 164.81),  # E major
    (92.50, 110.00, 138.59),  # F# minor
    (73.42, 110.00, 146.83),  # D major
)
_CHORD_SECONDS = 3.0
_XFADE_SECONDS = 0.5

_TWO_PI = 2.0 * math.pi

_AUDIO_SUFFIXES = (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".opus")


def _write_stereo_wav(path: Path, left: list[float], right: list[float]) -> Path:
    """Write two float (-1..1) channels as 16-bit stereo WAV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ints: list[int] = []
    for sample_l, sample_r in zip(left, right):
        ints.append(int(max(-1.0, min(1.0, sample_l)) * 32767))
        ints.append(int(max(-1.0, min(1.0, sample_r)) * 32767))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(array("h", ints).tobytes())
    return path


def _normalise(samples: list[float], peak: float) -> list[float]:
    loudest = max(1e-6, max(abs(s) for s in samples))
    return [s / loudest * peak for s in samples]


def _render_music_loop() -> tuple[list[float], list[float]]:
    """12s seamless chord-pad loop (mono render, widened to stereo)."""
    sin = math.sin
    total = LOOP_SECONDS + _XFADE_SECONDS
    n = int(total * SAMPLE_RATE)
    mono = [0.0] * n
    blend_from = 1.0 - _XFADE_SECONDS / _CHORD_SECONDS
    for i in range(n):
        t = i / SAMPLE_RATE
        pos = t / _CHORD_SECONDS
        k = int(pos) % len(_CHORDS)
        frac = pos - math.floor(pos)
        if frac >= blend_from:
            # Raised-cosine crossfade into the next chord.
            g = (frac - blend_from) / (1.0 - blend_from)
            w = 0.5 - 0.5 * math.cos(math.pi * g)
            a, b = _CHORDS[k], _CHORDS[(k + 1) % len(_CHORDS)]
            s = 0.0
            for j in range(3):
                s += (1.0 - w) * sin(_TWO_PI * a[j] * t)
                s += w * sin(_TWO_PI * b[j] * t)
            s /= 3.0
        else:
            ch = _CHORDS[k]
            s = (sin(_TWO_PI * ch[0] * t)
                 + sin(_TWO_PI * ch[1] * t)
                 + sin(_TWO_PI * ch[2] * t)) / 3.0
        # Soft octave shimmer on the chord root + a slow 3-cycles-per-loop
        # swell (integer cycles, so it is loop-safe by construction).
        s += 0.12 * sin(_TWO_PI * _CHORDS[k][0] * 2.0 * t)
        mono[i] = s * (0.85 + 0.15 * sin(_TWO_PI * 0.25 * t))
    # Gentle lowpass to glue the chord transitions together.
    y = 0.0
    for i in range(n):
        y += 0.15 * (mono[i] - y)
        mono[i] = y
    # Fold the tail over the head so the loop point is click-free.
    m = int(LOOP_SECONDS * SAMPLE_RATE)
    f = int(_XFADE_SECONDS * SAMPLE_RATE)
    out = mono[:m]
    for i in range(f):
        w = i / f
        out[i] = mono[m + i] * (1.0 - w) + mono[i] * w
    out = _normalise(out, 0.22)
    # Subtle stereo width: right channel delayed 0.7 ms.
    delay = int(0.0007 * SAMPLE_RATE)
    return out, [0.0] * delay + out[: m - delay]


def _render_whoosh() -> tuple[list[float], list[float]]:
    """0.5s airy transition sweep (deterministic — same every run)."""
    n = int(WHOOSH_SECONDS * SAMPLE_RATE)
    rng = random.Random(7)
    out: list[float] = []
    y = 0.0
    phase = 0.0
    for i in range(n):
        p = i / n
        alpha = 0.03 + 0.50 * math.sin(math.pi * p)  # filter opens, then closes
        env = math.sin(math.pi * p) ** 2
        y += alpha * (rng.uniform(-1.0, 1.0) - y)
        freq = 250.0 + 700.0 * math.sin(math.pi * p)
        phase += _TWO_PI * freq / SAMPLE_RATE
        out.append((y * 0.9 + 0.10 * math.sin(phase)) * env)
    out = _normalise(out, 0.5)
    return out, list(out)


def _render_pop() -> tuple[list[float], list[float]]:
    """0.3s soft pop to mark the end-card CTA."""
    n = int(POP_SECONDS * SAMPLE_RATE)
    rng = random.Random(11)
    out: list[float] = []
    phase = 0.0
    for i in range(n):
        t = i / SAMPLE_RATE
        freq = 600.0 * (1000.0 / 600.0) ** (t / POP_SECONDS)  # glide up
        phase += _TWO_PI * freq / SAMPLE_RATE
        out.append(math.sin(phase) * math.exp(-t * 13.0))
    for i in range(min(200, n)):  # tiny click transient at the start
        out[i] += rng.uniform(-1.0, 1.0) * 0.25 * math.exp(-i / 30.0)
    out = _normalise(out, 0.4)
    return out, list(out)


def ensure_assets(cache_dir: Path) -> dict[str, Path]:
    """Generate the loop/whoosh/pop WAVs once, reuse them forever.

    Synthesis is deterministic, so the cache never goes stale — files are
    only rendered when missing. Returns {"music_loop", "whoosh", "pop"}.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    jobs = (
        ("music_loop.wav", _render_music_loop),
        ("whoosh.wav", _render_whoosh),
        ("pop.wav", _render_pop),
    )
    assets: dict[str, Path] = {}
    for name, render in jobs:
        path = cache_dir / name
        if not path.exists():
            left, right = render()
            _write_stereo_wav(path, left, right)
        assets[name.split(".")[0]] = path
    return assets


def resolve_music(cfg: Config, fallback_loop: Path) -> Path:
    """Pick the music bed: music.file > first audio in music/ > built-in."""
    custom = str(cfg.music_file or "").strip()
    if custom:
        candidate = Path(custom)
        if not candidate.is_absolute():
            candidate = cfg.root / candidate
        if candidate.exists():
            print(f"      music     : {candidate.name} (music.file)")
            return candidate
        print(f"      music     : {custom} not found — using built-in bed")
    folder = cfg.root / cfg.music_folder
    if folder.is_dir():
        found = sorted(
            p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in _AUDIO_SUFFIXES
        )
        if found:
            # Random rotation: every video gets its own vibe. The pick is
            # logged to <video>.music.txt at render time (license record).
            track = random.choice(found)
            print(f"      music     : {track.name} (from {cfg.music_folder}/)")
            return track
    print("      music     : built-in royalty-free loop")
    return fallback_loop
