#!/usr/bin/env python3
"""Free AI video generator with manual YouTube upload — no API, no audit.

    python main.py preflight     check setup before anything else
    python main.py generate      AI script -> voice -> images -> subtitled MP4
    python main.py generate --format portrait --seconds 45
                                 vertical Short with burned-in subtitles
    python main.py queue         show queue status
    python main.py package       build upload-ready kits in upload/<id>/
    python main.py reburn <id>   burn subtitles into an already-made video
    python main.py published     record a manual upload's URL
    python main.py voices        list available voiceover voices

Nothing here costs money and nothing here touches the YouTube API, which is
exactly why no audit or verification can ever be required. You upload the
finished files yourself in ~3 minutes per video (each kit has a CHECKLIST.md).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import traceback
from pathlib import Path

from config import load_config
from jobqueue import Queue
from scriptgen import get_provider

BANNER = """\
======================================================================
  Free AI video generator (manual-upload edition)
  Cost: 0. Audit required: none — no YouTube API is used at all.
======================================================================
"""


def die(message: str, code: int = 1) -> None:
    print(f"\n\u274c {message}\n")
    sys.exit(code)


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------
def cmd_preflight(cfg, args) -> int:
    print(BANNER)
    print("Checking your setup. Everything here is free.\n")
    problems: list[str] = []
    warnings: list[str] = []

    def ok(label: str, detail: str = "") -> None:
        print(f"  \u2705 {label}{(' — ' + detail) if detail else ''}")

    def bad(label: str, detail: str = "") -> None:
        print(f"  \u274c {label}{(' — ' + detail) if detail else ''}")
        problems.append(label)

    def warn(label: str, detail: str = "") -> None:
        print(f"  \u26a0\ufe0f  {label}{(' — ' + detail) if detail else ''}")
        warnings.append(label)

    # 1. binaries
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool):
            ok(f"{tool} installed")
        else:
            bad(
                f"{tool} missing",
                "install with: brew install ffmpeg / sudo apt install ffmpeg / "
                "winget install Gyan.FFmpeg",
            )

    # 2. python deps
    for module in ("requests", "edge_tts", "yaml"):
        try:
            __import__(module)
            ok(f"python module {module}")
        except ImportError:
            bad(f"python module {module}", "run: pip install -r requirements.txt")

    # 3. script provider
    if cfg.ai_provider == "template":
        warn("script provider is 'template'", "offline, lower quality. Fine to start with.")
    elif cfg.gemini_api_key:
        ok(f"Gemini key present, model {cfg.gemini_model}")
    else:
        warn(
            "no Gemini API key",
            "will fall back to templates. Free key: https://aistudio.google.com/apikey",
        )

    # 4. folders are writable
    print()
    try:
        cfg.ensure_dirs()
        probe = cfg.out_dir / ".writetest"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        ok(f"output folders writable (out/, work/, {cfg.package_dir.name}/)")
    except OSError as exc:
        bad("output folders not writable", str(exc))

    # 5. effective video settings + subtitle burn-in
    print()
    ok(
        f"default video: {cfg.format} {cfg.width}x{cfg.height}, "
        f"{cfg.target_seconds}s target, {cfg.images_per_scene} images/scene, "
        f"subtitles {'on' if cfg.subtitles_enabled else 'off'}"
    )
    if shutil.which("ffmpeg"):
        from assembler import _ffmpeg_has_filter

        if _ffmpeg_has_filter("subtitles"):
            ok("subtitle burn-in available")
        else:
            warn(
                "subtitle burn-in unavailable in this FFmpeg build",
                "captions.srt will still be included in every kit for manual upload",
            )

    if shutil.which("ffmpeg"):
        from assembler import _ffmpeg_has_filter as _has_subs

        if _has_subs("subtitles"):
            if _subtitle_render_test(cfg):
                ok("subtitle render test passed (burned text is visible)")
            else:
                warn(
                    "subtitle burn-in produced no visible text",
                    "font lookup failed — videos will lack burned subs "
                    "(captions.srt still works)",
                )

    # 6. optional live Gemini check (one tiny free request)
    if args.live:
        print()
        if not cfg.gemini_api_key:
            warn("live check skipped — no Gemini key configured")
        else:
            print("  Live Gemini check (one tiny free request)...")
            try:
                import requests

                resp = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{cfg.gemini_model}:generateContent",
                    params={"key": cfg.gemini_api_key},
                    json={"contents": [{"parts": [{"text": "Reply with exactly: OK"}]}]},
                    timeout=60,
                )
                if resp.status_code == 200:
                    ok("Gemini key works")
                elif resp.status_code in (400, 401, 403):
                    bad("Gemini key rejected", resp.text[:140])
                else:
                    warn(f"Gemini returned HTTP {resp.status_code}", resp.text[:140])
            except Exception as exc:  # noqa: BLE001
                warn("Gemini check failed", str(exc).splitlines()[0][:140])

    print()
    if problems:
        print(f"\u274c {len(problems)} problem(s) to fix before this will work:")
        for problem in problems:
            print(f"   - {problem}")
        return 1
    print("\u2705 Setup looks good." + (" (with warnings above)" if warnings else ""))
    print("   Next: python main.py generate")
    return 0


# --------------------------------------------------------------------------
# generate
# --------------------------------------------------------------------------
def cmd_generate(cfg, args) -> int:
    print(BANNER)
    if args.seconds is not None:
        cfg.data["channel"]["target_seconds"] = args.seconds
    if args.format is not None:
        cfg.data["video"]["format"] = args.format
    if args.images_per_scene is not None:
        cfg.data["video"]["images_per_scene"] = args.images_per_scene
    if args.no_subs:
        cfg.data["subtitles"]["enabled"] = False

    print(
        f"Settings for this run: {cfg.format} {cfg.width}x{cfg.height}, "
        f"~{cfg.target_seconds}s, {cfg.images_per_scene} images/scene, "
        f"subtitles {'on' if cfg.subtitles_enabled else 'off'}\n"
    )

    provider = get_provider(cfg)
    queue = Queue(cfg.state_file)
    count = args.count

    for number in range(1, count + 1):
        topic = args.topic or cfg.topic
        if count > 1:
            topic = f"{topic} (variation {number} of {count}: choose a different specific story each time)"
        source = "--topic override" if args.topic else "from config.yaml"
        print(f"[{number}/{count}] topic: {topic}  ({source})")

        job = queue.add(topic)
        job_dir = cfg.work_dir / job.id
        job_dir.mkdir(parents=True, exist_ok=True)

        try:
            print("  1/5 script")
            script = provider.generate(cfg, args.topic)
            print(f"      title   : {script.title}")
            print(f"      scenes  : {len(script.scenes)}")
            print(f"      est. len: {script.estimated_seconds()}s (target {cfg.target_seconds}s)")
            (job_dir / "script.json").write_text(
                json.dumps(
                    {
                        "title": script.title,
                        "description": script.description,
                        "tags": script.tags,
                        "provider": script.provider,
                        "scenes": [
                            {"narration": s.narration, "image_prompt": s.image_prompt}
                            for s in script.scenes
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            print("  2/5 voiceover")
            from voiceover import generate_scene_audio

            audio_paths, timings = generate_scene_audio(script, cfg, job_dir / "audio")

            print("  3/5 images")
            from images import generate_scene_images

            images_by_scene = generate_scene_images(script, cfg, job_dir / "images")

            print("  4/5 cutting scenes")
            from assembler import (
                HEAD_TAIL,
                _ffmpeg_has_filter,
                assemble_video,
                build_segments,
                build_thumbnail,
                write_metadata,
            )

            segments, padded, durations = build_segments(
                images_by_scene, audio_paths, job_dir, cfg
            )
            out_path = cfg.out_dir / f"{job.id}.mp4"

            print("  5/5 subtitles + final video")
            srt_path = None
            burned_in = False
            if cfg.subtitles_enabled:
                from subtitles import build_cues, max_chars_for, write_srt

                starts: list[float] = []
                running = 0.0
                for scene_len in durations:
                    starts.append(running)
                    running += scene_len
                cues = build_cues(
                    [s.narration for s in script.scenes],
                    timings,
                    starts,
                    durations,
                    max_chars_for(cfg.format),
                    HEAD_TAIL,
                )
                srt_path = write_srt(cues, out_path.with_suffix(".srt"))
                print(f"      subtitles : {len(cues)} cues -> {srt_path.name}")
                if _ffmpeg_has_filter("subtitles"):
                    burned_in = True
                else:
                    print("      subtitles : burn-in unavailable in this FFmpeg build —")
                    print("                    captions.srt is still included in the kit")

            assemble_video(segments, padded, out_path, cfg, work_dir=job_dir,
                           srt_path=srt_path if burned_in else None)
            build_thumbnail(images_by_scene[0][0], script.title,
                            out_path.with_suffix(".jpg"), cfg)
            meta_path = write_metadata(
                script, out_path, cfg,
                duration_seconds=sum(durations),
                subtitle_file=srt_path.name if srt_path else None,
                subtitles_burned_in=burned_in,
            )

            total = sum(durations)
            size_mb = out_path.stat().st_size / (1024 * 1024)
            queue.update(
                job,
                status="generated",
                title=script.title,
                video_file=str(out_path),
                meta_file=str(meta_path),
            )
            print(f"  \u2705 {out_path.name}  {total:.0f}s  {size_mb:.1f} MB")

            if not args.keep_work:
                shutil.rmtree(job_dir, ignore_errors=True)

        except Exception as exc:  # noqa: BLE001
            queue.update(job, status="failed", error=str(exc)[:400])
            print(f"  \u274c failed: {exc}")
            if args.verbose:
                traceback.print_exc()
            if not args.keep_going:
                return 1

    print()
    print(queue.format_table())
    print("\nNext: python main.py package")
    return 0


# --------------------------------------------------------------------------
# package
# --------------------------------------------------------------------------
def cmd_package(cfg, args) -> int:
    print(BANNER)
    from package import build_package

    queue = Queue(cfg.state_file)
    pending = queue.pending()

    if args.id:
        job = queue.get(args.id)
        if job is None:
            die(f"no job matching '{args.id}'. Run: python main.py queue")
        if job.status != "generated":
            die(f"job {job.id} is '{job.status}', not 'generated' — nothing to package.")
        pending = [job]
    elif args.limit:
        pending = pending[: args.limit]

    if not pending:
        print("Nothing to package.")
        print("  generated videos waiting: 0 — run: python main.py generate")
        return 0

    print(f"Building upload kits for {len(pending)} video(s).\n")
    succeeded = 0
    for index, job in enumerate(pending, start=1):
        print(f"[{index}/{len(pending)}] {job.title or job.topic}")
        try:
            kit = build_package(job, cfg)
            queue.update(job, status="packaged", package_dir=str(kit))
            print(f"    kit \u2192 {kit}  (open CHECKLIST.md inside)")
            succeeded += 1
        except Exception as exc:  # noqa: BLE001
            queue.update(job, status="failed", error=str(exc)[:400])
            print(f"    \u274c {str(exc).splitlines()[0][:160]}")

    print(f"\n\u2705 {succeeded}/{len(pending)} packaged.")
    print(queue.format_table())
    if succeeded:
        print("\nNext: open upload/<id>/CHECKLIST.md and upload at https://youtube.com/upload")
    return 0


def _subtitle_render_test(cfg) -> bool:
    """Burn one test frame and check pixels actually changed. True if visible."""
    import subprocess

    from subtitles import filter_args

    test_dir = cfg.work_dir / ".subtest"
    try:
        test_dir.mkdir(parents=True, exist_ok=True)
        srt = test_dir / "t.srt"
        srt.write_text(
            "1\n00:00:00,000 --> 00:00:05,000\nSubtitle render test\n",
            encoding="utf-8",
        )
        bg = test_dir / "bg.jpg"
        # Test at the real configured resolution: subtitle size and margins
        # scale with frame size, so a tiny test frame would mis-position them.
        result = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", f"color=c=black:s={cfg.width}x{cfg.height}:d=1",
             "-frames:v", "1", str(bg)],
            capture_output=True,
        )
        if result.returncode != 0 or not bg.exists():
            return False
        plain = test_dir / "plain.png"
        burned = test_dir / "burned.png"
        for out, extra in ((plain, []), (burned, ["-vf", filter_args(srt, cfg.format)])):
            result = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1",
                 "-i", str(bg), *extra, "-frames:v", "1", str(out)],
                capture_output=True,
            )
            if result.returncode != 0 or not out.exists():
                return False
        return plain.read_bytes() != burned.read_bytes()
    except OSError:
        return False
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# reburn
# --------------------------------------------------------------------------
def cmd_reburn(cfg, args) -> int:
    """Burn subtitles into an already-generated video (no regeneration)."""
    from assembler import AssemblyError, _ffmpeg_has_filter, run
    from subtitles import filter_args

    queue = Queue(cfg.state_file)
    job = queue.get(args.id)
    if job is None:
        die(f"no job matching '{args.id}'. Run: python main.py queue")
    if job.status not in ("generated", "packaged"):
        die(f"job {job.id} is '{job.status}' — reburn needs a generated video.")
    video_path = Path(job.video_file)
    if not video_path.exists():
        die(f"video file missing: {video_path}")
    srt_path = video_path.with_suffix(".srt")
    if not srt_path.exists():
        die(f"no subtitle file next to the video ({srt_path.name}). "
            f"This job was made with subtitles off — regenerate instead.")
    if not _ffmpeg_has_filter("subtitles"):
        die("this FFmpeg build has no subtitles filter — can't burn in.")
    tmp = video_path.with_name(video_path.stem + "_reburn.mp4")
    print(f"Burning {srt_path.name} into {video_path.name} ...")
    try:
        run(["ffmpeg", "-y", "-loglevel", "warning",
             "-i", str(video_path),
             "-vf", filter_args(srt_path, cfg.format),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-c:a", "copy",
             str(tmp)], "reburn")
    except AssemblyError as exc:
        tmp.unlink(missing_ok=True)
        die(str(exc))
    tmp.replace(video_path)
    try:
        meta_path = Path(job.meta_file)
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["subtitle_file"] = srt_path.name
            meta["subtitles_burned_in"] = True
            meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError):
        pass
    if job.status == "packaged":
        queue.update(job, status="generated", package_dir="")
        print("Kit is stale now — re-run: python main.py package")
    print(f"\u2705 burned in. Watch it: {video_path}")
    return 0


# --------------------------------------------------------------------------
# published / queue / voices
# --------------------------------------------------------------------------
def cmd_published(cfg, args) -> int:
    queue = Queue(cfg.state_file)
    job = queue.get(args.id)
    if job is None:
        die(f"no job matching '{args.id}'. Run: python main.py queue")
    if job.status not in ("packaged", "generated", "published"):
        die(f"job {job.id} is '{job.status}' — package it first.")
    queue.update(job, status="published", published_url=args.url)
    print(f"\u2705 {job.id} marked as published: {args.url}")
    return 0


def cmd_queue(cfg, args) -> int:
    print(Queue(cfg.state_file).format_table())
    return 0


def cmd_voices(cfg, args) -> int:
    from voiceover import list_voices

    print(f"Voices starting with '{args.lang}':\n")
    list_voices(args.lang)
    print(f"\nCurrent voice in config: {cfg.voice}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Free AI video generator (manual-upload edition).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", help="path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("preflight", help="check your setup")
    p.add_argument("--live", action="store_true", help="also validate the Gemini key (one tiny free request)")

    p = sub.add_parser("generate", help="make videos")
    p.add_argument("--topic", help="override the configured channel topic")
    p.add_argument("--count", type=int, default=1, help="how many videos to make")
    p.add_argument("--seconds", type=int, help="target length, e.g. 45 for a Short")
    p.add_argument("--format", choices=["landscape", "portrait"],
                   help="landscape = regular video, portrait = Shorts (1080x1920)")
    p.add_argument("--images-per-scene", type=int, dest="images_per_scene",
                   help="pictures per narrated scene, 1-6 (default 2)")
    p.add_argument("--no-subs", action="store_true", help="skip subtitles for this run")
    p.add_argument("--keep-work", action="store_true", help="keep intermediate files")
    p.add_argument("--keep-going", action="store_true", help="continue after a failure")
    p.add_argument("--verbose", action="store_true")

    p = sub.add_parser("package", help="build upload-ready kits")
    p.add_argument("--id", help="package only the job with this id (prefix ok)")
    p.add_argument("--limit", type=int, help="max kits to build this run")

    p = sub.add_parser("reburn", help="burn subtitles into an existing video")
    p.add_argument("id", help="job id (prefix ok)")

    p = sub.add_parser("published", help="record a manual upload's URL")
    p.add_argument("id", help="job id (prefix ok)")
    p.add_argument("url", help="the YouTube URL, e.g. https://youtu.be/....")

    sub.add_parser("queue", help="show the queue")

    p = sub.add_parser("voices", help="list available voiceover voices")
    p.add_argument("--lang", default="en-", help="voice prefix filter, e.g. en-, it-, de-")

    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
    except ValueError as exc:
        die(str(exc))

    handlers = {
        "preflight": cmd_preflight,
        "generate": cmd_generate,
        "package": cmd_package,
        "reburn": cmd_reburn,
        "published": cmd_published,
        "queue": cmd_queue,
        "voices": cmd_voices,
    }
    return handlers[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
