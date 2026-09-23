"""Word-timed subtitles: SRT sidecar + burn-in styling.

Timings come from edge-tts WordBoundary events, offset by each scene's start
time. If a scene has no usable timings (voice-engine hiccup, or the spoken
words don't match the text because e.g. numbers get expanded), words are
spread evenly across the narration instead — subtitles always exist.

Three outputs:
  1. out/<id>.srt — copied into the kit as captions.srt for manual upload
     in YouTube Studio (accessibility + SEO),
  2. out/<id>.ass — TikTok-style karaoke captions (word-by-word yellow
     highlight), burned into the picture during the final mux when this
     FFmpeg build has libass, and
  3. the burn-in itself.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from editorial import scrub_narration

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
    words = scrub_narration(narration).split()  # same scrub as TTS: aligned
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


def burn_style(video_format: str, style: dict | None = None) -> str:
    """force_style value for the burn-in (libass). No surrounding quotes.

    NOTE: FontSize AND margins are in PlayRes units (libass default
    PlayResY=288) and get scaled up to video size — portrait x6.67,
    landscape x3.75. Margins must be tiny numbers here or the text lands
    off-screen (MarginV=330 on portrait = 2200px = invisible!).

    style: {"position": top|middle|bottom, "font", "scale", "outline"} —
    see sub_style_from_cfg. "default" keeps the historic bottom burn.
    """
    style = style or {}
    pos = str(style.get("position", "default"))
    font = str(style.get("font", "Arial") or "Arial")
    scale = float(style.get("scale", 1.0) or 1.0)
    outline = int(style.get("outline", 0) or 0)
    if video_format == "portrait":
        # ~73px text sitting ~333px up — clears the Shorts UI overlay.
        fontsize, margin_v = 11, 50
        top_margin = 40          # PlayRes units — ~267px on a portrait frame
    else:
        # ~52px text, ~45px from the bottom.
        fontsize, margin_v = 14, 12
        top_margin = 12
    align, margin = {
        "default": (2, margin_v), "bottom": (2, margin_v),
        "middle": (5, 0), "top": (8, top_margin),
    }.get(pos, (2, margin_v))
    return (
        f"FontName={font},FontSize={max(4, int(round(fontsize * scale)))},"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,"
        "BackColour=&H80000000,BorderStyle=1,"
        f"Outline={outline or 2},Shadow=0,"
        f"Alignment={align},MarginV={margin}"
    )


def esc_subs_path(path: Path) -> str:
    """Quote an .srt path for the subtitles filter (Windows-colon safe)."""
    safe = str(path.absolute()).replace("\\", "/").replace("'", "")
    return "'" + safe.replace(":", "\\:") + "'"


def system_fonts_dir() -> Path | None:
    """Font directory for libass font lookup.

    Prefers a project-local fonts/ dir holding just Arial: libass scans
    every file in fontsdir, and pointing it at all of C:\\Windows\\Fonts
    means hundreds of failed .fon probes plus slow init on every burn.
    The local dir is populated from the Windows fonts on first use (the
    user already owns that license; nothing is redistributed). Falls back
    to C:\\Windows\\Fonts itself, or None off-Windows, where fontconfig
    already handles lookup.
    """
    local = Path(__file__).resolve().parent / "fonts"
    try:
        local.mkdir(parents=True, exist_ok=True)
        if not any(local.iterdir()):
            for name in ("arial.ttf", "arialbd.ttf"):
                win_font = Path("C:/Windows/Fonts") / name
                if win_font.exists():
                    try:
                        shutil.copy2(win_font, local / name)
                    except OSError:
                        pass
        if any(entry.suffix.lower() in (".ttf", ".otf", ".ttc")
               for entry in local.iterdir()):
            return local
    except OSError:
        pass
    cand = Path("C:/Windows/Fonts")
    return cand if cand.is_dir() else None


# Whole-word profanity masker (live request 2026-09-23: "youtube might get
# mad, better safe than sorry"). Keeps the first letter, asterisks the rest
# (f*ck) — recognizable to humans, invisible to literal-word matching.
PROFANITY = frozenset({
    "fuck", "fucks", "fucked", "fucker", "fuckers", "fucking",
    "motherfucker", "motherfucking", "mf",
    "shit", "shits", "shitted", "shitting", "shitty",
    "bitch", "bitches", "bitching", "bastard", "bastards",
    "ass", "asses", "asshole", "assholes", "arsehole", "arseholes",
    "dick", "dicks", "dickhead", "cock", "cocks", "pussy", "cunt", "cunts",
    "twat", "prick", "wanker", "bollocks", "whore", "whores", "slut", "sluts",
    "nigger", "niggers", "nigga", "niggas", "faggot", "faggots", "fag",
    "retard", "retards", "retarded", "spic", "chink", "kike", "wetback",
    "tranny", "trannies",
})


def mask_profanity(text: str) -> str:
    """Mask profane words, vowel-starring style: fuck -> f*ck. Tested.

    Word count is never changed — karaoke timing depends on it. Compound
    words (class, bass, assume) never match: lookup is whole-word only.
    Case is preserved (SHIT -> SH*T).
    """
    import re

    def _mask(match) -> str:
        word = match.group(0)
        if word.lower() in PROFANITY:
            return re.sub(r"[aeiouAEIOU]", "*", word)
        return word

    return re.sub(r"[A-Za-z]+", _mask, text or "")


def filter_args(sub_path: Path, video_format: str,
                style: dict | None = None) -> str:
    """Full `subtitles=...` filter argument value (without -vf quotes)."""
    parts = [f"subtitles={esc_subs_path(sub_path)}"]
    fonts = system_fonts_dir()
    if fonts is not None:
        parts.append(f"fontsdir={esc_subs_path(fonts)}")
    if sub_path.suffix.lower() != ".ass":
        # SRT carries no styling; ASS files style themselves.
        parts.append(f"force_style='{burn_style(video_format, style)}'")
    return ":".join(parts)


def sub_style_from_cfg(cfg, cli_pos: str = "default") -> dict:
    """Subtitle style knobs: --sub-pos CLI > config > engine defaults.

    "default" means each engine's historic placement (karaoke portrait
    dead-center, everything else bottom). "auto" is resolved by the clip
    lane (frame analysis) before it reaches a style builder; if it leaks
    through, builders treat it as "default".
    """
    pos = cli_pos if cli_pos in ("top", "middle", "bottom", "auto") \
        else str(getattr(cfg, "subtitles_position", "default") or "default")
    if pos not in ("top", "middle", "bottom", "auto"):
        pos = "default"
    return {
        "position": pos,
        "font": str(getattr(cfg, "subtitles_font", "Arial") or "Arial"),
        "scale": float(getattr(cfg, "subtitles_font_scale", 1.0) or 1.0),
        "outline": int(getattr(cfg, "subtitles_outline", 0) or 0),
    }


def pick_sub_band(energies) -> str:
    """Calmest horizontal third for subtitles, from (top, middle, bottom).

    Bottom wins within 10% (captions read best at the bottom); top beats
    middle on near-ties. Pure, tested.
    """
    top, middle, bottom = (float(x) for x in energies)
    calmest = min(top, middle, bottom)
    if bottom <= calmest * 1.10:
        return "bottom"
    if top <= calmest * 1.10:
        return "top"
    return "middle"


def frame_band_energies(frame: bytes):
    """(top, middle, bottom) edge-energy of one JPEG/PNG frame.

    Pillow + numpy only — no API, no network, deterministic. None on any
    decode failure so the caller can fall back to the configured position.
    """
    try:
        import io

        from PIL import Image
        import numpy as np

        img = Image.open(io.BytesIO(frame)).convert("L")
        arr = np.asarray(img, dtype=np.float32)
        rows = np.abs(np.diff(arr, axis=0)).sum(axis=1)  # per-row busyness
        third = max(1, arr.shape[0] // 3)
        return (float(rows[:third].sum()),
                float(rows[third:2 * third].sum()),
                float(rows[2 * third:].sum()))
    except Exception:  # noqa: BLE001 - unreadable frame -> caller falls back
        return None


# ---------------------------------------------------------------------------
# Karaoke captions (TikTok style): one event per word, active word highlighted.
# The .ass sets its own PlayRes = video pixels, so sizes and margins below
# are TRUE pixels — no PlayRes scaling math needed (see burn_style's warning).
# ---------------------------------------------------------------------------
KARAOKE_MAX_WORDS = 5
KARAOKE_MAX_CHARS = 30
KARAOKE_GAP_SPLIT = 0.30
KARAOKE_HOLD_AFTER = 0.25
KARAOKE_WHITE = r"{\c&HFFFFFF&}"
KARAOKE_YELLOW = r"{\c&H00FFFF&}"


@dataclass
class KaraokeEvent:
    start: float
    end: float
    text: str  # ASS-formatted (highlight overrides + \N breaks baked in)


def ass_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis == 100:
        secs += 1
        centis = 0
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _esc_ass(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _balance_break(words: list[str]) -> int:
    """Word index where line 2 starts (0 = single line). Balanced, <=2 lines."""
    text = " ".join(words)
    if len(words) < 2 or len(text) <= 14:
        return 0
    best: float | None = None
    best_i = 0
    for i in range(1, len(words)):
        score = abs(len(" ".join(words[:i])) - len(" ".join(words[i:])))
        if best is None or score < best:
            best, best_i = score, i
    return best_i


def build_karaoke_events(
    narrations: list[str],
    timings_per_scene: list[list[tuple[float, float]]],
    scene_starts: list[float],
    scene_durations: list[float],
    head_tail: float,
    max_words: int = KARAOKE_MAX_WORDS,
    max_chars: int = KARAOKE_MAX_CHARS,
) -> list[KaraokeEvent]:
    """One highlighted-word event per word; phrases never cross scenes.

    Words are chunked into short phrases (natural pauses, punctuation, or
    size limits). Every event in a phrase shows the same two lines with a
    different word highlighted, so the layout never jumps mid-phrase.
    """
    events: list[KaraokeEvent] = []
    for narration, timings, start, duration in zip(
        narrations, timings_per_scene, scene_starts, scene_durations
    ):
        words = _word_times(narration, timings, start, duration, head_tail)
        if not words:
            continue
        scene_end = start + duration
        # Chunk into phrases.
        phrases: list[list[tuple[str, float, float]]] = []
        current: list[tuple[str, float, float]] = []
        for index, item in enumerate(words):
            word, t0, _ = item
            gap = t0 - words[index - 1][2] if current else 0.0
            trial = " ".join([w for w, _, _ in current] + [word])
            if current and (
                gap > KARAOKE_GAP_SPLIT
                or len(current) >= max_words
                or len(trial) > max_chars
            ):
                phrases.append(current)
                current = []
            current.append(item)
            if word and word[-1] in ".!?\u2026" and len(current) >= 3:
                phrases.append(current)
                current = []
        if current:
            phrases.append(current)
        # One event per word.
        phrase_starts = [ph[0][1] for ph in phrases]
        for pos, phrase in enumerate(phrases):
            plain = [w for w, _, _ in phrase]
            break_at = _balance_break(plain)
            next_start = phrase_starts[pos + 1] if pos + 1 < len(phrases) else None
            last_phrase = pos == len(phrases) - 1
            for wpos, (_, t0, t1) in enumerate(phrase):
                if wpos + 1 < len(phrase):
                    end = phrase[wpos + 1][1]
                elif next_start is not None:
                    end = min(next_start - 0.03, t1 + KARAOKE_HOLD_AFTER)
                else:
                    end = t1 + KARAOKE_HOLD_AFTER + 0.15
                if last_phrase and wpos == len(phrase) - 1:
                    end = min(end, scene_end)
                end = max(end, t0 + 0.08)
                parts = []
                for j, raw in enumerate(plain):
                    safe = _esc_ass(raw)
                    if j == wpos:
                        parts.append(f"{KARAOKE_YELLOW}{safe}{KARAOKE_WHITE}")
                    else:
                        parts.append(safe)
                if break_at:
                    text = " ".join(parts[:break_at]) + "\\N" + " ".join(parts[break_at:])
                else:
                    text = " ".join(parts)
                events.append(KaraokeEvent(start=t0, end=end, text=text))

    # Clamp overlaps left by rounding so words never double-render.
    for first, second in zip(events, events[1:]):
        if first.end > second.start:
            first.end = max(first.start + 0.04, second.start - 0.01)
    return events


def _karaoke_style(video_format: str, style: dict | None = None) -> str:
    """Style line for karaoke .ass files (true pixels; see note above).

    style: {"position": top|middle|bottom, "font", "scale", "outline"} —
    see sub_style_from_cfg. "default" keeps the historic placement
    (portrait dead-center, landscape bottom).
    """
    style = style or {}
    pos = str(style.get("position", "default"))
    font = str(style.get("font", "Arial") or "Arial")
    scale = float(style.get("scale", 1.0) or 1.0)
    outline = int(style.get("outline", 0) or 0)
    if video_format == "portrait":
        # Big centered captions = the TikTok look; center also dodges the
        # Shorts UI (bottom ~300px + right rail). TRUE pixels: PlayRes is set
        # to the video resolution in write_ass.
        fontsize, align, margin_v = 88, 5, 0
        bottom_margin, top_margin = 300, 140
    else:
        fontsize, align, margin_v = 52, 2, 45
        bottom_margin, top_margin = 45, 45
    if pos in ("top", "middle", "bottom"):
        align = {"top": 8, "middle": 5, "bottom": 2}[pos]
        margin_v = {"top": top_margin, "middle": 0,
                    "bottom": bottom_margin}[pos]
    return (
        f"Style: Karaoke,{font},"
        f"{max(12, int(round(fontsize * scale)))},"
        f"&H00FFFFFF,&H00001919,&H80000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline or 3},0,"
        f"{align},60,60,{margin_v},1"
    )


def ass_colour(hex_color: str) -> str:
    """0xRRGGBB (or #RRGGBB / RRGGBB) -> ASS &HAABBGGRR& (pure, tested)."""
    clean = hex_color.strip().lower().removeprefix("0x").removeprefix("#")
    if len(clean) != 6 or any(c not in "0123456789abcdef" for c in clean):
        clean = "ffd700"  # gold fallback — never crashes on garbage
    return f"&H00{clean[4:6]}{clean[2:4]}{clean[0:2]}&".upper()


def progress_ass_line(duration: float, width: int, height: int,
                      color: str = "0xFFD700", bar_h: int = 12,
                      position: str = "bottom") -> str:
    """Full-duration Dialogue drawing the animated progress bar (pure, tested).

    drawbox width/height are evaluated ONCE (verified on FFmpeg 7.0.2: a
    w='iw*t/D' bar renders full-width and static), so the bar rides the
    subtitle burn-in instead: a full-width gold rect sliding in from the
    left via \\move across the whole duration. Same pixels as intended.
    """
    if duration <= 0 or width <= 0:
        return ""
    total_ms = max(1, int(round(duration * 1000)))
    y = 0 if position == "top" else max(0, height - bar_h)
    return (
        f"Dialogue: 0,0:00:00.00,{ass_timestamp(duration)},Karaoke,,0,0,0,,"
        f"{{\\an7\\bord0\\shad0\\move({-width},{y},0,{y},0,{total_ms})"
        f"\\p1\\c{ass_colour(color)}}}"
        f"m 0 0 l {width} 0 l {width} {bar_h} l 0 {bar_h}{{\\p0}}"
    )


def write_ass(
    events: list[KaraokeEvent],
    path: Path,
    video_format: str,
    width: int,
    height: int,
    progress: str | None = None,
    style: dict | None = None,
) -> Path:
    """Write karaoke events as an .ass file styled for the video size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        _karaoke_style(video_format, style),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for event in events:
        lines.append(
            f"Dialogue: 0,{ass_timestamp(event.start)},{ass_timestamp(event.end)},"
            f"Karaoke,,0,0,0,,{event.text}"
        )
    if progress:
        lines.append(progress)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
