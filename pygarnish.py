"""Garnish without FFmpeg filters: subtitles, progress bar, CTA, audio candy.

Some FFmpeg builds segfault inside the filter graph (seen: 9.x Windows dying
on -filter_complex and on every font filter via a broken fontconfig). This
module rebuilds every garnish OUTSIDE FFmpeg:

  * subtitles / CTA text -> transparent PNGs rendered with Pillow using the
    project-local fonts/arialbd.ttf (no fontconfig involved), overlaid with
    movie+overlay on the plain -vf path — the same path the Ken Burns
    segments already use successfully;
  * progress bar -> gold PNG + overlay with animated x (drawbox w/h\n    evaluate once, so drawbox can never animate — verified 7.0.2);
  * music bed, whooshes, CTA pop, ducking, fade-out -> pre-mixed in numpy to
    a single WAV that is muxed with -map (no -af, no -filter_complex).

The result is a mux command with NO -filter_complex, NO -af, NO subtitles,
NO drawtext — the four crash vectors — yet visually identical to the full
mix. Used as a ladder rung in assembler.assemble_video, after the native
mixes and before the bare emergency muxes. Pillow/numpy are optional: when
missing, the rung is skipped and the ladder behaves as before.
"""

from __future__ import annotations

import re
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE = 48000

_ASS_DIALOGUE = "dialogue:"
_TAG_RE = re.compile(r"\{[^}]*\}")
_YELLOW_TAG = "c&h00ffff&"
_WHITE_TAG = "c&hffffff&"


class PygarnishError(RuntimeError):
    """The python-garnish rung cannot be built (bad input, missing tool)."""


def available() -> bool:
    """True when Pillow + numpy are installed (rung usable)."""
    try:
        import PIL.Image  # noqa: F401
        import PIL.ImageDraw  # noqa: F401
        import PIL.ImageFont  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# Subtitle parsing (SRT + karaoke ASS -> timed word-highlighted events)
# ---------------------------------------------------------------------------

@dataclass
class PyEvent:
    start: float
    end: float
    lines: tuple[tuple[tuple[str, bool], ...], ...]  # lines of (word, highlighted)
    plain: tuple[str, ...] = ()  # words without styling (phrase grouping)


def _parse_ass_time(text: str) -> float:
    hours, minutes, rest = text.strip().split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(rest)


def _parse_srt_time(text: str) -> float:
    hours, minutes, rest = text.strip().replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(rest)


def _ass_words_tracked(text: str) -> tuple[tuple[tuple[str, bool], ...], ...]:
    """ASS body -> lines, honouring {\\c&H00FFFF&} / {\\c&HFFFFFF&} spans."""
    lines: list[list[tuple[str, bool]]] = [[]]
    highlighted = False
    token = ""
    i = 0
    while i < len(text):
        if text[i] == "{":
            end = text.find("}", i)
            if end == -1:
                token += text[i]
                i += 1
                continue
            # Flush first: words before the tag keep the previous colour.
            if token.strip():
                for word in token.split():
                    lines[-1].append((word, highlighted))
                token = ""
            tag = text[i + 1:end].lower()
            if _YELLOW_TAG in tag:
                highlighted = True
            elif _WHITE_TAG in tag:
                highlighted = False
            i = end + 1
            continue
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in "Nn":
            if token.strip():
                for word in token.split():
                    lines[-1].append((word, highlighted))
                token = ""
            lines.append([])
            i += 2
            continue
        token += text[i]
        i += 1
    if token.strip():
        for word in token.split():
            lines[-1].append((word, highlighted))
    return tuple(tuple(line) for line in lines if line)


def _parse_ass(path: Path) -> list[PyEvent]:
    events: list[PyEvent] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.lower().startswith(_ASS_DIALOGUE):
            continue
        fields = raw.split(",", 9)
        if len(fields) < 10:
            continue
        try:
            start = _parse_ass_time(fields[1])
            end = _parse_ass_time(fields[2])
        except ValueError:
            continue
        lines = _ass_words_tracked(fields[9])
        if not lines:
            continue
        if end <= start:
            end = start + 0.1
        plain = tuple(w for line in lines for w, _ in line)
        events.append(PyEvent(start=start, end=end, lines=lines, plain=plain))
    return events


def _parse_srt(path: Path) -> list[PyEvent]:
    events: list[PyEvent] = []
    for block in path.read_text(encoding="utf-8").split("\n\n"):
        rows = [row.strip() for row in block.splitlines() if row.strip()]
        if len(rows) < 3 or "-->" not in rows[1]:
            continue
        try:
            left, right = rows[1].split("-->")
            start = _parse_srt_time(left)
            end = _parse_srt_time(right)
        except ValueError:
            continue
        lines = tuple(
            tuple((word, False) for word in row.split())
            for row in rows[2:] if row.split()
        )
        if not lines:
            continue
        if end <= start:
            end = start + 0.1
        plain = tuple(w for line in lines for w, _ in line)
        events.append(PyEvent(start=start, end=end, lines=lines, plain=plain))
    return events


def parse_sub_file(path: Path) -> tuple[list[PyEvent], bool]:
    """Parse .srt/.ass -> (events, is_ass). Raises PygarnishError if empty."""
    if path.suffix.lower() == ".ass":
        events = _parse_ass(path)
        is_ass = True
    else:
        events = _parse_srt(path)
        is_ass = False
    if not events:
        raise PygarnishError(f"no subtitle events found in {path.name}")
    return events, is_ass


def merge_phrases(events: list[PyEvent]) -> list[PyEvent]:
    """Merge consecutive events showing the same words (karaoke -> caption).

    A 60s Short holds ~150 word-events; each needs its own overlay input, so
    past ~120 events the filter string gets unwieldy. Merging by phrase keeps
    every word on screen for its full phrase window (highlight dropped) with
    ~4x fewer inputs.
    """
    merged: list[PyEvent] = []
    for event in events:
        if merged and merged[-1].plain == event.plain and event.plain:
            prev = merged[-1]
            merged[-1] = PyEvent(
                start=prev.start, end=max(prev.end, event.end),
                lines=prev.lines, plain=prev.plain,
            )
        else:
            plain_lines = tuple(
                tuple((word, False) for word, _ in line)
                for line in event.lines
            )
            merged.append(PyEvent(
                start=event.start, end=event.end,
                lines=plain_lines, plain=event.plain,
            ))
    return merged


# ---------------------------------------------------------------------------
# Caption PNG rendering (Pillow, project-local font, no fontconfig)
# ---------------------------------------------------------------------------

_WHITE = (255, 255, 255, 255)
_YELLOW = (255, 255, 0, 255)
_BLACK = (0, 0, 0, 255)


def _find_font_bold() -> Path | None:
    try:
        from subtitles import system_fonts_dir

        fonts_dir = system_fonts_dir()
    except Exception:  # noqa: BLE001 - font lookup must never crash
        fonts_dir = None
    candidates: list[Path] = []
    if fonts_dir is not None:
        candidates += [fonts_dir / "arialbd.ttf", fonts_dir / "arial.ttf"]
    candidates += [
        Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ]
    return next((c for c in candidates if c.exists()), None)


def _load_font(size: int):
    from PIL import ImageFont

    found = _find_font_bold()
    if found is not None:
        try:
            return ImageFont.truetype(str(found), size)
        except OSError:
            pass
    return ImageFont.load_default()


def _line_width(draw, words, font) -> float:
    if not words:
        return 0.0
    total = sum(draw.textlength(word, font=font) for word, _ in words)
    return total + draw.textlength(" ", font=font) * (len(words) - 1)


def render_caption_png(
    path: Path,
    width: int,
    height: int,
    lines: tuple[tuple[tuple[str, bool], ...], ...],
    *,
    fontsize: int,
    centered: bool,
    bottom_margin: int,
) -> Path:
    """Render one full-frame transparent caption PNG."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _load_font(fontsize)
    # Shrink-to-fit so long lines never leave the frame.
    while fontsize > 18 and any(
        _line_width(draw, line, font) > width - 60 for line in lines
    ):
        fontsize -= 4
        font = _load_font(fontsize)
    stroke = max(2, round(fontsize / 22))
    space_w = draw.textlength(" ", font=font)
    line_h = int(fontsize * 1.25)
    block_h = line_h * len(lines)
    y = (height - block_h) // 2 if centered else height - bottom_margin - block_h
    for line in lines:
        x = (width - _line_width(draw, line, font)) / 2
        for word, highlighted in line:
            draw.text(
                (x, y), word, font=font,
                fill=_YELLOW if highlighted else _WHITE,
                stroke_width=stroke, stroke_fill=_BLACK,
            )
            x += draw.textlength(word, font=font) + space_w
        y += line_h
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def render_cta_png(
    path: Path, width: int, height: int, text: str, *, fontsize: int, top_y: int,
) -> Path:
    """Render one full-frame transparent end-card CTA PNG."""
    words = tuple((word, False) for word in text.split())
    if not words:
        raise PygarnishError("empty CTA text")
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _load_font(fontsize)
    while fontsize > 18 and _line_width(draw, words, font) > width - 80:
        fontsize -= 4
        font = _load_font(fontsize)
    stroke = max(2, round(fontsize / 18))
    space_w = draw.textlength(" ", font=font)
    x = (width - _line_width(draw, words, font)) / 2
    for word, _ in words:
        draw.text((x, top_y), word, font=font, fill=_WHITE,
                  stroke_width=stroke, stroke_fill=_BLACK)
        x += draw.textlength(word, font=font) + space_w
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


# ---------------------------------------------------------------------------
# Audio mixing (numpy, no FFmpeg filters)
# ---------------------------------------------------------------------------

def decode_to_wav(src: Path, dst: Path) -> Path:
    """Decode any audio file to 48 kHz stereo s16 WAV (no filters used)."""
    cmd = ["ffmpeg", "-y", "-loglevel", "warning", "-i", str(src),
           "-ar", str(SAMPLE_RATE), "-ac", "2", "-c:a", "pcm_s16le", str(dst)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        tail = (proc.stderr or "").strip().splitlines()[-4:]
        raise PygarnishError(
            f"could not decode {src.name}: " + " / ".join(tail))
    return dst


def _read_wav_f32(path: Path):
    """Read a 48 kHz s16 WAV as float32 (N, 2) in -1..1."""
    import numpy as np

    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    if width != 2 or rate != SAMPLE_RATE:
        raise PygarnishError(
            f"{path.name} is not 48kHz/s16 — decode it first")
    data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels == 1:
        data = np.repeat(data.reshape(-1, 1), 2, axis=1)
    elif channels == 2:
        data = data.reshape(-1, 2)
    else:
        raise PygarnishError(f"{path.name}: {channels} channels unsupported")
    return data


def _moving_avg(values, window: int):
    """Same-length moving average in O(n) via cumsum."""
    import numpy as np

    values = np.asarray(values, dtype=np.float64)
    if window <= 1 or len(values) <= window:
        return values.astype(np.float32)
    kernel = np.cumsum(np.concatenate([[0.0], values]))
    avg = (kernel[window:] - kernel[:-window]) / window
    pad_left = window // 2
    pad_right = len(values) - len(avg) - pad_left
    return np.pad(avg, (pad_left, pad_right), mode="edge").astype(np.float32)


def _db_to_lin(db: float) -> float:
    return 10.0 ** (db / 20.0)


def mix_audio_py(
    *,
    padded_audio: list[Path],
    out_path: Path,
    total_seconds: float,
    cuts: list[float],
    whoosh_path: Path | None,
    whoosh_db: float,
    pop_path: Path | None,
    pop_at: float | None,
    pop_db: float = -8.0,
    music_track: Path | None,
    music_level_db: float,
    music_duck: bool,
) -> Path:
    """Mix narration + music bed + whooshes + pop -> one WAV (numpy).

    Mirrors the native -filter_complex mix: looped bed at music_level_db with
    speech ducking, whooshes on the scene cuts, a pop under the end-card, a
    1s fade-out, and a 0.95 ceiling (the native alimiter=limit=0.95).
    """
    import numpy as np

    total_n = int(round(total_seconds * SAMPLE_RATE))
    if total_n <= 0:
        raise PygarnishError("total_seconds must be positive")
    narr = np.concatenate([_read_wav_f32(p) for p in padded_audio], axis=0)
    if len(narr) < total_n:
        narr = np.pad(narr, ((0, total_n - len(narr)), (0, 0)))
    else:
        narr = narr[:total_n].copy()
    mix = narr.copy()

    if music_track is not None:
        bed_src = music_track
        if music_track.suffix.lower() != ".wav":
            bed_src = decode_to_wav(music_track, out_path.parent / "pyg_music.wav")
        bed = _read_wav_f32(bed_src)
        if len(bed) == 0:
            raise PygarnishError(f"music bed {music_track.name} decoded empty")
        reps = int(np.ceil(total_n / len(bed)))
        bed = np.tile(bed, (reps, 1))[:total_n].copy()
        bed *= _db_to_lin(music_level_db)
        if music_duck and total_n > SAMPLE_RATE // 4:
            env = _moving_avg(np.mean(np.abs(narr), axis=1), SAMPLE_RATE // 4)
            target = np.where(env > 0.03, 0.25, 1.0).astype(np.float32)
            duck = _moving_avg(target, int(SAMPLE_RATE * 0.3))
            bed *= duck[:, None]
        mix += bed

    def _add_at(clip, at_seconds: float) -> None:
        start = int(round(at_seconds * SAMPLE_RATE))
        if start >= total_n:
            return
        stop = min(total_n, start + len(clip))
        mix[start:stop] += clip[:stop - start]

    if whoosh_path is not None:
        whoosh = _read_wav_f32(whoosh_path) * _db_to_lin(whoosh_db)
        for cut in cuts:
            _add_at(whoosh, cut)
    if pop_path is not None and pop_at is not None:
        _add_at(_read_wav_f32(pop_path) * _db_to_lin(pop_db), pop_at)

    fade_n = min(total_n, SAMPLE_RATE)  # 1s fade-out, like afade=t=out:d=1
    mix[total_n - fade_n:] *= np.linspace(1.0, 0.0, fade_n)[:, None].astype(np.float32)
    peak = float(np.max(np.abs(mix)))
    if peak > 0.95:
        mix *= 0.95 / peak
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(mix, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(out_path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())
    return out_path


# ---------------------------------------------------------------------------
# Rung assembly
# ---------------------------------------------------------------------------

def _parse_hex_color(value: str) -> tuple[int, int, int]:
    """0xRRGGBB-ish -> (r, g, b); gold on garbage (pure, tested)."""
    clean = str(value or "").strip().lower().removeprefix("0x").removeprefix("#")
    if len(clean) != 6:
        return (255, 215, 0)
    try:
        return (int(clean[0:2], 16), int(clean[2:4], 16), int(clean[4:6], 16))
    except ValueError:
        return (255, 215, 0)


def _save_bar_png(path: Path, width: int, bar_h: int, color: str) -> Path:
    """Solid gold (or configured) bar PNG for the overlay (needs Pillow)."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (max(1, int(width)), max(1, int(bar_h))),
              _parse_hex_color(color)).save(path)
    return path


def prepare_pygarnish(
    *,
    work_dir: Path,
    cfg,
    padded_audio: list[Path],
    total_seconds: float,
    cuts: list[float],
    assets: dict[str, Path],
    music_track: Path | None,
    burn_path: Path | None,
    cta_overlay: tuple[str, float] | None,
) -> dict:
    """Render PNGs + mixed WAV; returns kwargs for build_pygarnish_mux_cmd."""
    if not available():
        raise PygarnishError("Pillow/numpy not installed")
    work_dir.mkdir(parents=True, exist_ok=True)
    use_whoosh = bool(cfg.sfx_whoosh and cuts and "whoosh" in assets)
    use_pop = bool(cta_overlay is not None and cfg.sfx_cta_pop
                   and "pop" in assets)
    pop_at = max(0.5, total_seconds - cta_overlay[1]) if use_pop else None
    assert cta_overlay is not None or not use_pop
    mixed_wav = mix_audio_py(
        padded_audio=padded_audio, out_path=work_dir / "pyg_mix.wav",
        total_seconds=total_seconds, cuts=cuts if use_whoosh else [],
        whoosh_path=assets["whoosh"] if use_whoosh else None,
        whoosh_db=float(cfg.sfx_whoosh_db),
        pop_path=assets["pop"] if use_pop else None, pop_at=pop_at,
        music_track=music_track, music_level_db=float(cfg.music_level_db),
        music_duck=bool(cfg.music_duck),
    )

    overlays: list[tuple[Path, float, float]] = []
    if burn_path is not None and burn_path.exists():
        events, is_ass = parse_sub_file(burn_path)
        if len(events) > 120:
            events = merge_phrases(events)
        portrait = cfg.format == "portrait"
        if is_ass:
            fontsize = 88 if portrait else 52
            centered = portrait
            margin = 0 if portrait else 45
        else:
            fontsize = 73 if portrait else 52
            centered = False
            margin = 333 if portrait else 45
        png_dir = work_dir / "pyg_subs"
        for index, event in enumerate(events):
            png = png_dir / f"cue_{index:03d}.png"
            render_caption_png(png, cfg.width, cfg.height, event.lines,
                               fontsize=fontsize, centered=centered,
                               bottom_margin=margin)
            overlays.append((png, event.start, event.end))

    cta: tuple[Path, float] | None = None
    if cta_overlay is not None:
        cta_text, cta_secs = cta_overlay
        cta_start = max(0.0, total_seconds - cta_secs)
        cta_fs = 54 if cfg.format == "portrait" else 44
        cta_png = work_dir / "pyg_cta.png"
        render_cta_png(cta_png, cfg.width, cfg.height, cta_text,
                       fontsize=cta_fs, top_y=cfg.height - 320)
        cta = (cta_png, cta_start)

    bar_png = None
    if cfg.progress_enabled:
        bar_png = work_dir / "pyg_bar.png"
        _save_bar_png(bar_png, cfg.width, cfg.progress_height,
                      cfg.progress_color)
    return {"mixed_wav": mixed_wav, "overlays": overlays, "cta": cta,
            "progress_bar": bar_png}


def build_pygarnish_mux_cmd(
    *,
    concat_txt: Path,
    mixed_wav: Path,
    out_path: Path,
    cfg,
    total_seconds: float,
    overlays: list[tuple[Path, float, float]],
    cta: tuple[Path, float] | None,
    progress_bar: Path | None,
) -> list[str]:
    """Final-mux command with zero crash filters (pure constructor).

    movie+overlay chains on -vf (overlay x/y DO animate per frame),
    pre-mixed single audio input: no -filter_complex, no -af, no
    subtitles, no drawtext, no drawbox.
    """
    from assembler import resolve_encoder_args
    from subtitles import esc_subs_path

    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-f", "concat", "-safe", "0", "-i", str(concat_txt),
        "-i", str(mixed_wav),
    ]
    chain: list[str] = []
    label = "[in]"
    counter = 0

    def _overlay(png: Path, enable: str) -> None:
        nonlocal label, counter
        tag = f"[wm{counter}]"
        out = f"[v{counter}]"
        counter += 1
        chain.append(f"movie={esc_subs_path(png)}{tag}")
        chain.append(f"{label}{tag}overlay=0:0:enable='{enable}'{out}")
        label = out

    for png, start, end in overlays:
        _overlay(png, f"between(t,{start:.3f},{end:.3f})")
    if cta is not None:
        cta_png, cta_start = cta
        _overlay(cta_png, f"gte(t,{cta_start:.3f})")
    if progress_bar is not None:
        bar_y = 0 if cfg.progress_position == "top" else cfg.height - cfg.progress_height
        tag = f"[wm{counter}]"
        out = f"[v{counter}]"
        counter += 1
        chain.append(f"movie={esc_subs_path(progress_bar)}{tag}")
        chain.append(
            f"{label}{tag}overlay=x='-{cfg.width}+{cfg.width}*t/{total_seconds:.3f}'"
            f":y={bar_y}:enable='between(t,0,{total_seconds:.3f})'{out}")
        label = out
    if chain:
        cmd += ["-vf", ";".join(chain)]

    cmd += [
        "-map", "0:v", "-map", "1:a",
        *resolve_encoder_args(cfg.encoder),
        "-threads", "4",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    return cmd
