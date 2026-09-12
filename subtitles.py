"""Word-timed subtitles: SRT sidecar + burn-in styling.

Timings come from edge-tts WordBoundary events, offset by each scene's start
time. If a scene has no usable timings (voice-engine hiccup, or the spoken
words don't match the text because e.g. numbers get expanded), words are
spread evenly across the narration instead — subtitles always exist.

Two outputs:
  1. out/<id>.srt — copied into the kit as captions.srt for manual upload
     in YouTube Studio (accessibility + SEO), and
  2. burn-in during the final mux, when this FFmpeg build has libass.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Max characters per cue line cluster, and max seconds per cue.
MAX_CHARS = {"landscape": 42, "portrait": 26}
MAX_CUE_SECONDS = 2.8
MIN_CUE_SECONDS = 0.6


@dataclass
class Cue:
    start: float
    end: float
    lines: tuple[str, ...]  # 1-2 lines


def srt_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis == 1000:
        secs += 1
        millis = 0
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _split_lines(text: str, max_chars: int) -> tuple[str, ...]:
    """One line if it fits comfortably, else two balanced lines."""
    words = text.split()
    if len(text) <= int(max_chars * 0.8) or len(words) < 2:
        return (text,)
    best: tuple[float, tuple[str, str]] | None = None
    for i in range(1, len(words)):
        first, second = " ".join(words[:i]), " ".join(words[i:])
        overflow = 0 if max(len(first), len(second)) <= max_chars else 1000
        score = abs(len(first) - len(second)) + overflow
        if best is None or score < best[0]:
            best = (score, (first, second))
    assert best is not None
    return best[1]


def _word_times(
    narration: str,
    timings: list[tuple[float, float]],
    scene_start: float,
    scene_seconds: float,
    head_tail: float,
) -> list[tuple[str, float, float]]:
    """Absolute (word, start, end) for one scene's narration.

    Uses real voice timings when they plausibly match the text, else spreads
    words evenly across the narration span.
    """
    words = narration.split()
    span = max(0.5, scene_seconds - 2 * head_tail)
    use_real = (
        timings
        and abs(len(timings) - len(words)) <= max(2, len(words) // 5)
    )
    if use_real:
        out = []
        for i, word in enumerate(words):
            t0, t1 = timings[min(i, len(timings) - 1)]
            out.append((word, scene_start + head_tail + t0, scene_start + head_tail + t1))
        return out
    step = span / max(1, len(words))
    return [
        (word, scene_start + head_tail + i * step, scene_start + head_tail + (i + 1) * step)
        for i, word in enumerate(words)
    ]


def build_cues(
    narrations: list[str],
    timings_per_scene: list[list[tuple[float, float]]],
    scene_starts: list[float],
    scene_durations: list[float],
    max_chars: int,
    head_tail: float,
) -> list[Cue]:
    """Group each scene's words into timed cues (never crossing scenes)."""
    cues: list[Cue] = []
    for narration, timings, start, duration in zip(
        narrations, timings_per_scene, scene_starts, scene_durations
    ):
        words = _word_times(narration, timings, start, duration, head_tail)
        if not words:
            continue
        group: list[tuple[str, float, float]] = []
        for item in words:
            trial = group + [item]
            text = " ".join(w for w, _, _ in trial)
            span = trial[-1][2] - trial[0][1]
            if group and (len(text) > max_chars or span > MAX_CUE_SECONDS):
                cues.append(_make_cue(group, max_chars))
                group = [item]
            else:
                group = trial
        if group:
            cues.append(_make_cue(group, max_chars))

    # Clamp overlaps left by rounding so cues never show two at once.
    for first, second in zip(cues, cues[1:]):
        if first.end > second.start - 0.04:
            first.end = max(first.start + 0.2, second.start - 0.04)
    return cues


def _make_cue(group: list[tuple[str, float, float]], max_chars: int) -> Cue:
    text = " ".join(w for w, _, _ in group)
    start = group[0][1]
    end = max(group[-1][2], start + MIN_CUE_SECONDS)
    return Cue(start=start, end=end, lines=_split_lines(text, max_chars))


def write_srt(cues: list[Cue], path: Path) -> Path:
    """Write cues in SubRip format."""
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = []
    for number, cue in enumerate(cues, start=1):
        blocks.append(
            f"{number}\n{srt_timestamp(cue.start)} --> {srt_timestamp(cue.end)}\n"
            + "\n".join(cue.lines)
        )
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return path


def max_chars_for(fmt: str) -> int:
    return MAX_CHARS.get(fmt, MAX_CHARS["landscape"])


def burn_style(video_format: str) -> str:
    """force_style value for the burn-in (libass). No surrounding quotes."""
    if video_format == "portrait":
        # Chunkier text sitting high enough to clear the Shorts UI overlay.
        fontsize, margin_v = 11, 330
    else:
        fontsize, margin_v = 14, 45
    return (
        f"FontName=Arial,FontSize={fontsize},"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,"
        "BackColour=&H80000000,BorderStyle=1,Outline=2,Shadow=0,"
        f"Alignment=2,MarginV={margin_v}"
    )


def esc_subs_path(path: Path) -> str:
    """Quote an .srt path for the subtitles filter (Windows-colon safe)."""
    safe = str(path.absolute()).replace("\\", "/").replace("'", "")
    return "'" + safe.replace(":", "\\:") + "'"
