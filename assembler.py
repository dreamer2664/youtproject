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

# Scene gets this much silence before/after narration so speech is not clipped.
HEAD_TAIL = 0.35
MIN_SCENE_SECONDS = 1.0


class AssemblyError(RuntimeError):
    pass


def run(cmd: list[str], what: str) -> None:
    """Run a command, raising a readable error on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-12:]
        raise AssemblyError(
            f"{what} failed (exit {result.returncode}):\n  " + "\n  ".join(tail)
        )


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
    image_paths: list[Path],
    audio_paths: list[Path],
    work_dir: Path,
    cfg: Config,
) -> tuple[list[Path], list[Path], list[float]]:
    """Create per-scene video segments and padded audio. Returns (segments, audio, durations)."""
    segments: list[Path] = []
    padded_audio: list[Path] = []
    durations: list[float] = []

    total = len(image_paths)
    pre_w, pre_h = cfg.width * 2, cfg.height * 2  # pre-upscale to kill zoompan jitter

    for index, (image, audio) in enumerate(zip(image_paths, audio_paths), start=1):
        narration_seconds = ffprobe_duration(audio)
        scene_seconds = max(MIN_SCENE_SECONDS, narration_seconds + 2 * HEAD_TAIL)
        durations.append(scene_seconds)

        frames = max(1, int(scene_seconds * cfg.fps))
        zoom_delta = cfg.zoom - 1.0
        # Alternate push-in and push-out so the video does not feel repetitive.
        zoom_in = index % 2 == 1

        if zoom_in:
            zoom_expr = f"min(1+{zoom_delta:.4f}*on/{frames},{cfg.zoom:.4f})"
            x_expr = "iw/2-(iw/zoom/2)"
            y_expr = "ih/2-(ih/zoom/2)"
        else:
            zoom_expr = f"max({cfg.zoom:.4f}-{zoom_delta:.4f}*on/{frames},1)"
            x_expr = "iw/2-(iw/zoom/2)"
            y_expr = "ih/2-(ih/zoom/2)"

        segment = work_dir / f"seg_{index:02d}.mp4"
        vf = (
            f"scale={pre_w}:{pre_h}:force_original_aspect_ratio=increase,"
            f"crop={pre_w}:{pre_h},"
            f"zoompan=z='{zoom_expr}':x='{x_expr}':y='{y_expr}':"
            f"d={frames}:s={cfg.width}x{cfg.height}:fps={cfg.fps},"
            f"format=yuv420p"
        )
        run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-loop", "1", "-i", str(image),
                "-vf", vf,
                "-t", f"{scene_seconds:.3f}",
                "-r", str(cfg.fps),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p",
                str(segment),
            ],
            f"segment {index}/{total}",
        )
        segments.append(segment)

        # Pad narration so its length matches the video segment exactly.
        pad = work_dir / f"pad_{index:02d}.wav"
        run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(audio),
                "-af", f"adelay={int(HEAD_TAIL * 1000)}|{int(HEAD_TAIL * 1000)},"
                       f"apad=whole_dur={scene_seconds:.3f}",
                "-ar", "48000", "-ac", "2",
                str(pad),
            ],
            f"audio pad {index}/{total}",
        )
        padded_audio.append(pad)

    return segments, padded_audio, durations


def assemble_video(
    segments: list[Path],
    padded_audio: list[Path],
    out_path: Path,
    cfg: Config,
    work_dir: Path | None = None,
) -> Path:
    """Concatenate segments, build the audio track, mux to the final MP4."""
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

    silence_video = work_dir / "silence.mp4"
    run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-t", "1", "-c:a", "aac", "-b:a", "192k",
            str(silence_video),
        ],
        "silence track",
    )

    total_seconds = ffprobe_duration(segments[0])
    for seg in segments[1:]:
        total_seconds += ffprobe_duration(seg)

    run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(concat_txt),
            "-f", "concat", "-safe", "0", "-i", str(audio_txt),
            "-i", str(silence_video),
            "-filter_complex",
            "[1:a]volume=1.15,afade=t=out:st="
            f"{max(0.0, total_seconds - 1.0):.3f}:d=1.0[narr];"
            "[narr][2:a]amix=inputs=2:duration=first:dropout_transition=0[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            "-shortest",
            str(out_path),
        ],
        "final mux",
    )
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


def build_thumbnail(
    image_path: Path,
    title: str,
    out_path: Path,
    cfg: Config,
) -> Path:
    """1280x720 thumbnail: scene image, darkened, with the title on it.

    Falls back to a plain (textless) thumbnail if this FFmpeg build has no
    drawtext filter — a missing text overlay must never fail a whole video.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    base_vf = "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720," \
              "eq=brightness=-0.12:saturation=1.15"

    if _ffmpeg_has_filter("drawtext"):
        words = title.split()
        wrapped = " ".join(words[:8]) if len(words) > 8 else title
        wrapped = _esc(wrapped[:80])

        # Font that exists on most systems; ffmpeg falls back to a default if absent.
        font_candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
        ]
        fontfile = next((f for f in font_candidates if Path(f).exists()), "")
        font_arg = f"fontfile={_esc(fontfile)}:" if fontfile else ""

        vf = (
            f"{base_vf},"
            f"drawtext={font_arg}text='{wrapped}':"
            f"fontsize=64:fontcolor=white:borderw=5:bordercolor=black@0.9:"
            f"x=(w-text_w)/2:y=h-text_h-90"
        )
    else:
        print("  [thumbnail] this FFmpeg build has no drawtext — using a plain thumbnail.")
        vf = base_vf

    run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(image_path),
            "-vf", vf,
            "-frames:v", "1",
            str(out_path),
        ],
        "thumbnail",
    )
    return out_path


def write_metadata(script, out_path: Path, cfg: Config) -> Path:
    """Sidecar JSON the packager reads to know title/description/tags."""
    meta = {
        "title": script.title,
        "description": script.description,
        "tags": (script.tags + cfg.default_tags)[:30],
        "categoryId": cfg.category_id,
        "language": cfg.language,
        "video_file": out_path.name,
        "thumbnail_file": out_path.with_suffix(".jpg").name,
        "provider": script.provider,
        "scenes": len(script.scenes),
    }
    meta_path = out_path.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta_path
