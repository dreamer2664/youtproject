"""Assemble scene images + scene narration into one finished MP4.

Approach:
  1. Measure each narration clip's length.
  2. Build one video segment per scene at exactly that length, with a slow
     Ken Burns zoom (pre-upscaled first, which is what stops zoompan jitter).
  3. Concatenate segments, concatenate narration with matching gaps, mux.
  4. Build a thumbnail from the first scene image with the title on it.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from config import Config

# Scene gets this much silence before/after narration: enough that speech is
# never clipped, tight enough that the pacing stays snappy for Shorts.
HEAD_TAIL = 0.25
MIN_SCENE_SECONDS = 1.0


class AssemblyError(RuntimeError):
    pass


# Harmless per-run ffmpeg noise, hidden on success to keep the console
# readable. Failures still print the raw tail (see run), so no signal is lost.
_FFMPEG_NOISE = (
    "deprecated pixel format",
    "Error getting metadata for embedded font",
    "No charmap autodetected",
    "Error opening memory font",
    "does not contain an image sequence pattern",
    "Use a pattern such as %03d",
    "Estimating duration from bitrate",
    "Guessed Channel Layout",
    # Our amerge+pan mix intentionally merges identical stereo layouts —
    # these two notes appear on every mixed render and mean nothing.
    "No channel layout for input",
    "Input channel layouts overlap",
)


def run(cmd: list[str], what: str) -> None:
    """Run a command, raising a readable error on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-12:]
        raise AssemblyError(
            f"{what} failed (exit {result.returncode}):\n  " + "\n  ".join(tail)
        )
    # Surface warnings (libass/font issues hide here) instead of swallowing
    # them — minus known-harmless noise, capped so one chatty filter can't
    # flood the console.
    err = (result.stderr or "").strip()
    if err:
        shown = 0
        hidden = 0
        for line in err.splitlines():
            if any(snippet in line for snippet in _FFMPEG_NOISE):
                hidden += 1
                continue
            if shown < 15:
                print(f"    [ffmpeg:{what}] {line[:200]}")
                shown += 1
            else:
                hidden += 1
        if shown and hidden:
            print(f"    [ffmpeg:{what}] …({hidden} routine lines hidden)")


def ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise AssemblyError(f"ffprobe could not read {path.name}: {out.stderr.strip()}")
    try:
        return float(out.stdout.strip())
    except ValueError as exc:
        raise AssemblyError(f"ffprobe returned an unparseable duration for {path.name}") from exc


def _esc(text: str) -> str:
    """Escape text for use inside ffmpeg drawtext text='...'."""
    return text.replace("\\", "\\\\").replace("'", "\u2019").replace(":", "\\:").replace("%", "\\%")


def build_segments(
    images_by_scene: list[list[Path]],
    audio_paths: list[Path],
    work_dir: Path,
    cfg: Config,
) -> tuple[list[Path], list[Path], list[float]]:
    """Create video sub-segments and padded scene audio.

    Each scene is split into one sub-segment per image (see
    video.images_per_scene), so the picture cuts regularly under the
    narration. Returns (segments, padded_audio, scene_durations) — audio and
    durations stay per-scene, since concatenation only needs matching totals.
    """
    segments: list[Path] = []
    padded_audio: list[Path] = []
    scene_durations: list[float] = []

    total = len(audio_paths)
    pre_w, pre_h = cfg.width * 2, cfg.height * 2  # pre-upscale to kill zoompan jitter
    sub_index = 0

    for s_num, (scene_images, audio) in enumerate(zip(images_by_scene, audio_paths), start=1):
        narration_seconds = ffprobe_duration(audio)
        scene_seconds = max(MIN_SCENE_SECONDS, narration_seconds + 2 * HEAD_TAIL)
        scene_durations.append(scene_seconds)

        shots = scene_images or []
        if not shots:
            raise AssemblyError(f"scene {s_num} has no images")
        part_seconds = scene_seconds / len(shots)

        for slot, image in enumerate(shots, start=1):
            sub_index += 1
            frames = max(1, int(part_seconds * cfg.fps))
            zoom_delta = cfg.zoom - 1.0
            # Alternate push-in and push-out so the video does not feel repetitive.
            zoom_in = sub_index % 2 == 1

            if zoom_in:
                zoom_expr = f"min(1+{zoom_delta:.4f}*on/{frames},{cfg.zoom:.4f})"
                x_expr = "iw/2-(iw/zoom/2)"
                y_expr = "ih/2-(ih/zoom/2)"
            else:
                zoom_expr = f"max({cfg.zoom:.4f}-{zoom_delta:.4f}*on/{frames},1)"
                x_expr = "iw/2-(iw/zoom/2)"
                y_expr = "ih/2-(ih/zoom/2)"

            segment = work_dir / f"seg_{s_num:02d}_{slot}.mp4"
            vf = (
                f"scale={pre_w}:{pre_h}:force_original_aspect_ratio=increase,"
                f"crop={pre_w}:{pre_h},"
                f"zoompan=z='{zoom_expr}':x='{x_expr}':y='{y_expr}':"
                f"d={frames}:s={cfg.width}x{cfg.height}:fps={cfg.fps},"
                f"format=yuv420p"
            )
            run(
                [
                    "ffmpeg", "-y", "-loglevel", "warning",
                    "-loop", "1", "-i", str(image),
                    "-vf", vf,
                    "-t", f"{part_seconds:.3f}",
                    "-r", str(cfg.fps),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-pix_fmt", "yuv420p",
                    str(segment),
                ],
                f"segment scene {s_num}/{total} shot {slot}/{len(shots)}",
            )
            segments.append(segment)

        # Pad narration so its length matches the video segment exactly.
        pad = work_dir / f"pad_{s_num:02d}.wav"
        run(
            [
                "ffmpeg", "-y", "-loglevel", "warning",
                "-i", str(audio),
                "-af", f"adelay={int(HEAD_TAIL * 1000)}|{int(HEAD_TAIL * 1000)},"
                       f"apad=whole_dur={scene_seconds:.3f}",
                "-ar", "48000", "-ac", "2",
                str(pad),
            ],
            f"audio pad {s_num}/{total}",
        )
        padded_audio.append(pad)

    return segments, padded_audio, scene_durations


def assemble_video(
    segments: list[Path],
    padded_audio: list[Path],
    out_path: Path,
    cfg: Config,
    work_dir: Path | None = None,
    burn_path: Path | None = None,
    scene_bounds: list[float] | None = None,
    cta_overlay: tuple[str, float] | None = None,
) -> Path:
    """Concatenate segments, build the audio track, mux to the final MP4.

    When burn_path is given (.ass karaoke captions or .srt), subtitles are
    burned into the picture (requires a libass-enabled FFmpeg — the caller
    checks for the subtitles filter).

    scene_bounds are scene-boundary times in seconds (whooshes land here);
    cta_overlay is (text, seconds_on_screen) for the end-card CTA + pop.
    """
    if work_dir is None:
        work_dir = out_path.parent
    work_dir.mkdir(parents=True, exist_ok=True)
    concat_txt = work_dir / "concat.txt"
    concat_txt.write_text(
        "".join(f"file {shlex.quote(str(p))}\n" for p in segments), encoding="utf-8"
    )
    audio_txt = work_dir / "audio.txt"
    audio_txt.write_text(
        "".join(f"file {shlex.quote(str(p))}\n" for p in padded_audio), encoding="utf-8"
    )

    total_seconds = ffprobe_duration(segments[0])
    for seg in segments[1:]:
        total_seconds += ffprobe_duration(seg)

    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-f", "concat", "-safe", "0", "-i", str(concat_txt),
        "-f", "concat", "-safe", "0", "-i", str(audio_txt),
    ]

    # -- audio candy: music bed, cut whooshes, CTA pop -------------------
    from audiofx import ensure_assets, resolve_music

    cuts = [b for b in (scene_bounds or []) if 0.3 < b < total_seconds - 0.3]
    want_music = cfg.music_enabled
    want_whoosh = cfg.sfx_whoosh and bool(cuts)
    want_pop = cfg.sfx_cta_pop and cta_overlay is not None
    assets: dict[str, Path] = {}
    if want_music or want_whoosh or want_pop:
        assets = ensure_assets(cfg.work_dir / "_fx")

    music_idx: int | None = None
    whoosh_idx: list[int] = []
    pop_idx: int | None = None
    next_idx = 2
    if want_music:
        track = resolve_music(cfg, assets["music_loop"])
        cmd += ["-stream_loop", "-1", "-t", f"{total_seconds:.3f}",
                "-i", str(track)]
        music_idx = next_idx
        next_idx += 1
    if want_whoosh:
        for _ in cuts:
            cmd += ["-i", str(assets["whoosh"])]
            whoosh_idx.append(next_idx)
            next_idx += 1
    if want_pop:
        cmd += ["-i", str(assets["pop"])]
        pop_idx = next_idx
        next_idx += 1

    fade = f"afade=t=out:st={max(0.0, total_seconds - 1.0):.3f}:d=1.0"
    if music_idx is None and not whoosh_idx and pop_idx is None:
        # Legacy path: narration only, a straight volume+fade chain.
        audio_graph = f"[1:a]volume=1.0,{fade}[a]"
    else:
        # Every mixed input is padded/trimmed to exactly total_seconds, then
        # merged sample-accurately with amerge+pan — deliberately NOT amix,
        # whose input-sync buffering once ballooned past 1GB and OOM-killed
        # the mux on small machines.
        exact = (f"aresample=48000,aformat=channel_layouts=stereo,"
                 f"apad=whole_dur={total_seconds:.3f},"
                 f"atrim=end={total_seconds:.3f}")
        ducking = (music_idx is not None and cfg.music_duck
                   and _ffmpeg_has_filter("sidechaincompress"))
        if ducking:
            # A filter output can feed only ONE downstream filter, so the
            # narration is split: one leg keys the ducking, one joins the mix.
            parts = [f"[1:a]{exact},asplit[narr][nside]"]
        else:
            parts = [f"[1:a]{exact}[narr]"]
        mix = ["[narr]"]
        if music_idx is not None:
            parts.append(f"[{music_idx}:a]{exact},volume={cfg.music_level_db}dB[bed]")
            if ducking:
                parts.append("[bed][nside]sidechaincompress=threshold=0.125"
                             ":ratio=6:attack=20:release=400,"
                             "aformat=channel_layouts=stereo[duck]")
            else:
                parts.append("[bed]anull[duck]")
            mix.append("[duck]")
        for k, (idx, bound) in enumerate(zip(whoosh_idx, cuts)):
            ms = int(bound * 1000)
            parts.append(f"[{idx}:a]{exact},adelay={ms}|{ms},"
                         f"volume={cfg.sfx_whoosh_db}dB[w{k}]")
            mix.append(f"[w{k}]")
        if pop_idx is not None:
            assert cta_overlay is not None
            pop_at = max(0.5, total_seconds - cta_overlay[1])
            ms = int(pop_at * 1000)
            parts.append(f"[{pop_idx}:a]{exact},adelay={ms}|{ms},"
                         f"volume=-8dB[popx]")
            mix.append("[popx]")
        lanes = len(mix)
        left = "+".join(f"c{2 * i}" for i in range(lanes))
        right = "+".join(f"c{2 * i + 1}" for i in range(lanes))
        parts.append(f"{''.join(mix)}amerge=inputs={lanes},"
                     f"pan=stereo|c0={left}|c1={right},"
                     f"alimiter=limit=0.95,{fade}[a]")
        audio_graph = ";".join(parts)

    # -- picture: subtitles burn + progress bar + CTA end-card -----------
    vf_parts: list[str] = []
    if burn_path is not None:
        from subtitles import filter_args

        vf_parts.append(filter_args(burn_path, cfg.format))
    if cfg.progress_enabled:
        bar_h = cfg.progress_height
        bar_y = 0 if cfg.progress_position == "top" else f"ih-{bar_h}"
        vf_parts.append(
            f"drawbox=x=0:y={bar_y}:w='iw*t/{total_seconds:.3f}':h={bar_h}"
            f":c={cfg.progress_color}:t=fill"
        )
    if cta_overlay is not None and _ffmpeg_has_filter("drawtext"):
        font_arg = _drawtext_font_arg()
        if font_arg is not None:
            cta_text, cta_secs = cta_overlay
            cta_start = max(0.0, total_seconds - cta_secs)
            cta_fs = 54 if cfg.format == "portrait" else 44
            vf_parts.append(
                f"drawtext={font_arg}:text='{_esc(cta_text)}':fontsize={cta_fs}"
                f":fontcolor=white:borderw=3:bordercolor=black"
                f":x=(w-text_w)/2:y=h-320:enable='gte(t,{cta_start:.3f})'"
            )
    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]

    cmd += [
        "-filter_complex",
        audio_graph,
        "-map", "0:v", "-map", "[a]",
        # veryfast + capped threads: the medium preset's lookahead buffers
        # can OOM small machines on 2MP frames; visually identical here
        # since the segments were already encoded once at CRF 20.
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-threads", "4",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        # No -shortest: it balloons the mux queue past 1GB and OOM-kills
        # small machines. Audio and video match by construction anyway
        # (each scene's audio is padded to its exact length).
        str(out_path),
    ]
    run(cmd, "final mux")
    return out_path


def _ffmpeg_has_filter(name: str) -> bool:
    """True if this FFmpeg build includes the given filter (e.g. drawtext)."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return out.returncode == 0 and f" {name} " in f" {out.stdout} "


def _drawtext_font_arg() -> str | None:
    """A drawtext font selector that works on this machine, or None.

    Prefers the project's local fonts/ copies (Windows), else asks
    fontconfig for a bold workhorse (Linux/macOS). None means "no usable
    font" — the caller skips the CTA overlay instead of crashing the render.
    """
    from subtitles import system_fonts_dir

    try:
        fonts_dir = system_fonts_dir()
    except OSError:
        fonts_dir = None
    if fonts_dir is not None:
        for name in ("arialbd.ttf", "arial.ttf"):
            candidate = fonts_dir / name
            if candidate.exists():
                return f"fontfile='{str(candidate).replace(':', chr(92) + ':')}'"
    try:
        out = subprocess.run(
            ["fc-list", ":", "family"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    families = out.stdout.lower()
    for family in ("Arial", "Liberation Sans", "DejaVu Sans", "FreeSans"):
        if family.lower() in families:
            return f"font='{family} Bold'"
    return None


def _thumbnail_filters(title: str, cfg: Config) -> list[tuple[str, str]]:
    """(label, filter) variants to try in order, most to least capable.

    drawtext font handling differs across FFmpeg builds and OSes — Windows
    drive-letter colons are the classic trap — so instead of guessing one
    perfect spelling, try several: quoted fontfile with escaped colon, quoted
    fontfile with plain colon, fontconfig by name (two common fonts), then a
    textless fallback that cannot fail on fonts.
    """
    tw, th = cfg.thumb_width, cfg.thumb_height
    base = (f"scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},"
            "eq=brightness=-0.12:saturation=1.15")
    words = title.split()
    wrapped_raw = (" ".join(words[:8]) if len(words) > 8 else title)[:80]
    # Shrink the font for long titles so the text stays inside the frame,
    # scaled down further on narrow portrait thumbnails.
    if len(wrapped_raw) <= 30:
        fontsize = 72
    elif len(wrapped_raw) <= 45:
        fontsize = 56
    else:
        fontsize = 44
    fontsize = max(18, int(fontsize * tw / 1280))
    wrapped = _esc(wrapped_raw)
    text_args = (f"text='{wrapped}':fontsize={fontsize}:fontcolor=white:"
                 f"borderw=5:bordercolor=black@0.9:x=(w-text_w)/2:y=h-text_h-90")

    variants: list[tuple[str, str]] = []
    if _ffmpeg_has_filter("drawtext"):
        font_candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
        ]
        fontfile = next((f for f in font_candidates if Path(f).exists()), "")
        if fontfile:
            safe_font = fontfile.replace("\\", "/").replace("'", "")
            variants.append(
                ("titled",
                 f"{base},drawtext=fontfile='{safe_font.replace(':', '\\:')}':{text_args}"))
            variants.append(
                ("titled",
                 f"{base},drawtext=fontfile='{safe_font}':{text_args}"))
        variants.append(
            ("titled", f"{base},drawtext=font='Arial Bold':{text_args}"))
        variants.append(
            ("titled", f"{base},drawtext=font='DejaVu Sans Bold':{text_args}"))
    variants.append(("plain", base))
    return variants


def build_thumbnail(
    image_path: Path,
    title: str,
    out_path: Path,
    cfg: Config,
) -> Path:
    """Sized thumbnail for the video orientation, darkened, with the title on it.

    Tries several drawtext spellings because font handling varies by FFmpeg
    build and OS; falls back to a plain (textless) thumbnail rather than
    failing the whole video over a text overlay.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for label, vf in _thumbnail_filters(title, cfg):
        try:
            run(
                [
                    "ffmpeg", "-y", "-loglevel", "warning",
                    "-i", str(image_path),
                    "-vf", vf,
                    "-frames:v", "1",
                    str(out_path),
                ],
                "thumbnail",
            )
            if label == "plain":
                print("  [thumbnail] titled text unavailable — using a plain thumbnail.")
            return out_path
        except AssemblyError as exc:
            errors.append(str(exc))
            continue
    raise AssemblyError(errors[0] if errors else "thumbnail failed with no details")


def write_metadata(
    script,
    out_path: Path,
    cfg: Config,
    duration_seconds: float = 0.0,
    subtitle_file: str | None = None,
    karaoke_file: str | None = None,
    subtitles_burned_in: bool = False,
    cta: str | None = None,
) -> Path:
    """Sidecar JSON the packager reads to know title/description/tags."""
    meta = {
        "title": script.title,
        "description": script.description,
        "tags": (script.tags + cfg.default_tags)[:30],
        "categoryId": cfg.category_id,
        "language": cfg.language,
        "format": cfg.format,
        "width": cfg.width,
        "height": cfg.height,
        "duration_seconds": round(duration_seconds, 1),
        "images_per_scene": cfg.images_per_scene,
        "subtitles_burned_in": bool(subtitles_burned_in),
        "video_file": out_path.name,
        "thumbnail_file": out_path.with_suffix(".jpg").name,
        "subtitle_file": subtitle_file,
        "karaoke_file": karaoke_file,
        "provider": script.provider,
        "scenes": len(script.scenes),
        "cta": cta,
    }
    meta_path = out_path.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta_path
