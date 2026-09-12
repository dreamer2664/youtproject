#!/usr/bin/env python3
"""Free AI video generator with manual YouTube upload — no API, no audit.

    python main.py preflight     check setup before anything else
    python main.py generate      AI script -> images -> voice -> finished MP4
    python main.py queue         show queue status
    python main.py package       build upload-ready kits in upload/<id>/
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

    # 5. optional live Gemini check (one tiny free request)
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
    provider = get_provider(cfg)
    queue = Queue(cfg.state_file)
    count = args.count

    for number in range(1, count + 1):
        topic = args.topic or cfg.topic
        if count > 1:
            topic = f"{topic} (variation {number} of {count}: choose a different specific story each time)"
        print(f"[{number}/{count}] topic: {topic}")

        job = queue.add(topic)
        job_dir = cfg.work_dir / job.id
        job_dir.mkdir(parents=True, exist_ok=True)

        try:
            print("  1/4 script")
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

            print("  2/4 voiceover")
            from voiceover import generate_scene_audio

            audio_paths = generate_scene_audio(script, cfg, job_dir / "audio")

            print("  3/4 images")
            from images import generate_scene_images

            image_paths = generate_scene_images(script, cfg, job_dir / "images")

            print("  4/4 assembling video")
            from assembler import (
                assemble_video,
                build_segments,
                build_thumbnail,
                write_metadata,
            )

            segments, padded, durations = build_segments(
                image_paths, audio_paths, job_dir, cfg
            )
            out_path = cfg.out_dir / f"{job.id}.mp4"
            assemble_video(segments, padded, out_path, cfg, work_dir=job_dir)
            build_thumbnail(image_paths[0], script.title, out_path.with_suffix(".jpg"), cfg)
            meta_path = write_metadata(script, out_path, cfg)

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
    p.add_argument("--keep-work", action="store_true", help="keep intermediate files")
    p.add_argument("--keep-going", action="store_true", help="continue after a failure")
    p.add_argument("--verbose", action="store_true")

    p = sub.add_parser("package", help="build upload-ready kits")
    p.add_argument("--id", help="package only the job with this id (prefix ok)")
    p.add_argument("--limit", type=int, help="max kits to build this run")

    p = sub.add_parser("published", help="record a manual upload's URL")
    p.add_argument("id", help="job id (prefix ok)")
    p.add_argument("url", help="the YouTube URL, e.g. https://youtu.be/....")

    sub.add_parser("queue", help="show the queue")

    p = sub.add_parser("voices", help="list available voiceover voices")
    p.add_argument("--lang", default="en-", help="voice prefix filter, e.g. en-, it-, de-")

    args = parser.parse_args()
    cfg = load_config(args.config)

    handlers = {
        "preflight": cmd_preflight,
        "generate": cmd_generate,
        "package": cmd_package,
        "published": cmd_published,
        "queue": cmd_queue,
        "voices": cmd_voices,
    }
    return handlers[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
