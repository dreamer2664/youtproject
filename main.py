#!/usr/bin/env python3
"""Free AI video generator with manual YouTube upload — no API, no audit.

    python main.py preflight     check setup before anything else
    python main.py generate      AI script -> voice -> images -> subtitled MP4
    python main.py generate --format portrait --seconds 45
                                 vertical Short with burned-in subtitles
    python main.py queue         show queue status
    python main.py package       build upload-ready kits in upload/<id>/
    python main.py batch --topics topics.txt   render a whole batch of videos
    python main.py bot                       render videos from your phone via Telegram
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
import os
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

    # 3b. full key-pool census (every lane the crew can use).
    from crew import _pools_line

    pools = _pools_line(cfg)
    if cfg.gemini_api_keys or cfg.groq_api_keys or cfg.openrouter_api_keys:
        ok(pools)
    else:
        warn("no LLM keys at all",
             "crew runs code-only with template scripts. " + pools)

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
        f"style {cfg.style}, subtitles {'on' if cfg.subtitles_enabled else 'off'}"
    )
    from images import describe_chain

    ok(f"images: {describe_chain(cfg)}")
    if shutil.which("ffmpeg"):
        from assembler import _ffmpeg_has_filter, drawtext_selftest

        if _ffmpeg_has_filter("subtitles"):
            ok("subtitle burn-in available")
            if _subtitle_render_test(cfg):
                ok("subtitle render test passed (burned text is visible)")
            else:
                warn(
                    "subtitle burn-in produced no visible text",
                    "font lookup failed — videos will lack burned subs "
                    "(captions.srt still works)",
                )
        else:
            warn(
                "subtitle burn-in unavailable in this FFmpeg build",
                "captions.srt will still be included in every kit for manual upload",
            )

        drawtext_ok = drawtext_selftest(cfg)
        if drawtext_ok is True:
            ok("end-card text overlay available")
        elif drawtext_ok is False:
            warn(
                "end-card text overlay fails on this FFmpeg build",
                "videos will render without the end-card text "
                "(the spoken CTA is unaffected)",
            )

    # 6. phone control (only when enabled)
    if cfg.telegram_enabled:
        print()
        if not cfg.telegram_token or not cfg.telegram_owner:
            warn("Telegram enabled but token/owner missing",
                 "see telegram: in config.yaml")
        else:
            try:
                from bot import check_token
                ok(f"Telegram bot reachable (@{check_token(cfg)})")
            except Exception as exc:  # noqa: BLE001
                warn("Telegram bot unreachable", str(exc).splitlines()[0][:140])

    # 7. optional live Gemini check (one tiny free request)
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
def _estimate_render_seconds(cfg) -> tuple[float, str]:
    """(avg seconds per video, basis note) from job history, else a default."""
    from jobqueue import Queue

    spans = [job.updated_at - job.created_at for job in Queue(cfg.state_file).jobs
             if job.status in ("generated", "packaged")
             and 0 < job.updated_at - job.created_at < 10800]
    if spans:
        return sum(spans) / len(spans), f"avg of {len(spans)} past video(s)"
    return 240.0, "default (no history yet)"


def _auto_chain(cfg, new_ids: list[str]) -> None:
    """Package this run's new videos and draft them to Buffer. Never raises."""
    import argparse

    from jobqueue import Queue

    from package import build_package

    queue = Queue(cfg.state_file)
    for job_id in new_ids:
        job = queue.get(job_id)
        if job is None or job.status != "generated":
            continue
        print(f"  [auto] packaging {job.title or job.topic}...")
        try:
            kit = build_package(job, cfg)
            queue.update(job, status="packaged", package_dir=str(kit))
        except Exception as exc:  # noqa: BLE001 - the chain never fails the run
            print(f"  [auto] package failed ({str(exc)[:120]}) — skipping post.")
            continue
        if not (cfg.buffer_api_key and cfg.cloudinary_cloud_name):
            print("  [auto] Buffer/Cloudinary not configured — packaged, not posted.")
            continue
        print("  [auto] drafting to Buffer (review drafts to release)...")
        try:
            cmd_autopost(cfg, argparse.Namespace(
                file=str(Path(job.video_file)) if job.video_file else None,
                publish=False, schedule=False, at=None, channels=None,
                video_url=None))
        except SystemExit:
            print("  [auto] autopost skipped (see message above).")
        except Exception as exc:  # noqa: BLE001 - the chain never fails the run
            print(f"  [auto] autopost failed ({str(exc)[:120]}).")


def _fresh_preview(cfg, limit: int) -> tuple[list[str], int]:
    """(up to `limit` fresh backlog topics, total fresh). Read-only, no API."""
    from jobqueue import Queue

    from topics import is_same_topic, load_backlog

    used = [job.topic for job in Queue(cfg.state_file).jobs]
    fresh = [t for t in load_backlog(cfg.topics_backlog_file)
             if not any(is_same_topic(t, old) for old in used)]
    return fresh[:limit], len(fresh)


def _dry_run_batch(cfg, topics: list) -> int:
    """Report what batch WOULD render. No API calls, no backlog writes."""
    print(BANNER)
    avg, basis = _estimate_render_seconds(cfg)
    explicit = [t for t in topics if t]
    auto_count = len(topics) - len(explicit)
    print(f"DRY RUN — {len(topics)} video(s), ~{avg * len(topics) / 60:.0f} min total "
          f"(~{avg / 60:.1f} min each, {basis}).")
    if explicit:
        print("explicit topics:")
        for topic in explicit:
            print(f"  - {topic}")
    if auto_count:
        preview, total = _fresh_preview(cfg, auto_count)
        print(f"auto-pick ({auto_count} needed, {total} fresh in backlog):")
        for topic in preview:
            print(f"  - {topic}")
        if total < auto_count:
            print("  ... shortfall would come from top-up or the channel topic.")
    print("\nNothing rendered, nothing spent. Drop --dry-run to go.")
    return 0


def _dry_run_schedule(cfg, times: list[str], per_day: int) -> int:
    """Report what schedule WOULD do. No API calls, no backlog writes, no sleep."""
    print(BANNER)
    avg, basis = _estimate_render_seconds(cfg)
    slots = ", ".join(times) if times else f"evenly spaced x{per_day}"
    print(f"DRY RUN — {per_day} video(s)/day at {slots} "
          f"(~{avg * per_day / 60:.0f} min/day, ~{avg / 60:.1f} min each, {basis}).")
    preview, total = _fresh_preview(cfg, per_day)
    print(f"next topics preview ({total} fresh in backlog):")
    for topic in preview:
        print(f"  - {topic}")
    if total < per_day:
        print("  ... shortfall would come from top-up or the channel topic.")
    print("\nNothing rendered, nothing spent. Drop --dry-run to go.")
    return 0


def _pick_fresh_topic(cfg, used: list[str]) -> tuple[str, str]:
    """Next backlog topic the channel hasn't covered; channel topic last resort."""
    from topics import load_backlog, pop_fresh_topic, top_up_backlog

    path = cfg.topics_backlog_file
    if len(load_backlog(path)) < cfg.topics_backlog_target:
        top_up_backlog(cfg, used=used)
    topic = pop_fresh_topic(path, used)
    if topic is not None:
        return topic, "from backlog"
    return cfg.topic, "channel topic (backlog dry)"


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
    if args.style is not None:
        cfg.data["video"]["style"] = args.style
    if getattr(args, "no_gemini", False):
        # Drop Gemini from every lane this run. Env vars beat config
        # edits (config.py property order), so all three sources go.
        os.environ.pop("GEMINI_API_KEYS", None)
        os.environ.pop("GEMINI_API_KEY", None)
        cfg.data["ai"]["gemini_api_key"] = ""
        cfg.data["ai"]["gemini_api_keys"] = []
        print("  [chain] gemini skipped (--no-gemini)\n")

    print(
        f"Settings for this run: {cfg.format} {cfg.width}x{cfg.height}, "
        f"~{cfg.target_seconds}s, {cfg.images_per_scene} images/scene, "
        f"style {cfg.style}, subtitles {'on' if cfg.subtitles_enabled else 'off'}\n"
    )

    provider = get_provider(cfg)
    queue = Queue(cfg.state_file)
    count = args.count
    if count < 1:
        die(f"--count must be at least 1 (got {count}).")

    from topics import is_same_topic

    used = [job.topic for job in queue.jobs]
    new_ids: list[str] = []
    for number in range(1, count + 1):
        if args.topic:
            topic, source = args.topic, "--topic override"
            if any(is_same_topic(topic, old) for old in used):
                print("  heads-up: a past video already covers this — "
                      "rendering anyway (--topic is explicit).")
        else:
            topic, source = _pick_fresh_topic(cfg, used)
            if count > 1 and source != "from backlog":
                topic = (f"{topic} (variation {number} of {count}: choose a "
                         f"different specific story each time)")
        used.append(topic)
        print(f"[{number}/{count}] topic: {topic}  ({source})")

        job = queue.add(topic)
        new_ids.append(job.id)
        job_dir = cfg.work_dir / job.id
        job_dir.mkdir(parents=True, exist_ok=True)

        try:
            print("  1/5 script")
            # Pass the (possibly variation-decorated) topic, not the raw CLI
            # value, so --count N really yields N different scripts.
            script = provider.generate(cfg, topic)
            from factcheck import check_script

            fact_report = check_script(script, cfg)
            from editorial import polish_script

            polish_script(script, cfg, provider)
            from cta import commit_cta, next_cta

            # commit=False: the rotation counter only advances once this
            # render succeeds (see commit_cta below), so failures waste no CTA.
            cta_voice, cta_overlay_text, cta_num = next_cta(cfg, commit=False)
            if cta_voice:
                script.scenes[-1].narration = (
                    f"{script.scenes[-1].narration} {cta_voice}"
                )
            est_seconds = script.estimated_seconds(130.0 * cfg.speech_rate_factor)
            print(f"      title   : {script.title}")
            print(f"      scenes  : {len(script.scenes)}")
            print(f"      est. len: {est_seconds}s (target {cfg.target_seconds}s)")
            if est_seconds < cfg.target_seconds * 0.7:
                print(f"      warning   : script is short ({est_seconds:.0f}s vs {cfg.target_seconds}s target) —")
                print("                    the fallback model undershoots; re-run later for a full-length video")
            elif est_seconds > cfg.target_seconds * 1.3:
                print(f"      warning   : script is long ({est_seconds:.0f}s vs {cfg.target_seconds}s target) —")
                print("                    pacing will feel slow; consider --seconds or regenerating")
            if cta_voice:
                print(f"      cta #{cta_num}    : {cta_voice[:62]}")
            (job_dir / "script.json").write_text(
                json.dumps(
                    {
                        "title": script.title,
                        "description": script.description,
                        "tags": script.tags,
                        "provider": script.provider,
                        "factcheck": fact_report,
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
            from bot import slugify

            out_path = cfg.out_dir / f"{slugify(script.title, 'video')}-{job.id}.mp4"

            print("  5/5 subtitles + final video")
            burn_path = None
            srt_path = None
            karaoke_name = None
            burned_in = False
            if cfg.subtitles_enabled:
                from subtitles import (
                    build_cues,
                    build_karaoke_events,
                    max_chars_for,
                    progress_ass_line,
                    write_ass,
                    write_srt,
                )

                starts: list[float] = []
                running = 0.0
                for scene_len in durations:
                    starts.append(running)
                    running += scene_len
                narrations = [s.narration for s in script.scenes]
                cues = build_cues(narrations, timings, starts, durations,
                                  max_chars_for(cfg.format), HEAD_TAIL)
                srt_path = write_srt(cues, out_path.with_suffix(".srt"))
                print(f"      subtitles : {len(cues)} cues -> {srt_path.name}")
                events = build_karaoke_events(narrations, timings, starts,
                                              durations, HEAD_TAIL)
                progress_line = None
                if cfg.progress_enabled:
                    progress_line = progress_ass_line(
                        sum(durations), cfg.width, cfg.height,
                        cfg.progress_color, cfg.progress_height,
                        cfg.progress_position)
                ass_path = write_ass(events, out_path.with_suffix(".ass"),
                                     cfg.format, cfg.width, cfg.height,
                                     progress=progress_line)
                karaoke_name = ass_path.name
                print(f"      karaoke   : {len(events)} word events -> {ass_path.name}")
                if _ffmpeg_has_filter("subtitles"):
                    burn_path = ass_path
                    burned_in = True
                else:
                    print("      subtitles : burn-in unavailable in this FFmpeg build —")
                    print("                    captions.srt is still included in the kit")

            bounds: list[float] = []
            running_bound = 0.0
            for scene_len in durations[:-1]:
                running_bound += scene_len
                bounds.append(running_bound)
            cta_card = (
                (cta_overlay_text, cfg.cta_overlay_seconds)
                if cta_voice else None
            )
            assemble_video(segments, padded, out_path, cfg, work_dir=job_dir,
                           burn_path=burn_path, scene_bounds=bounds,
                           cta_overlay=cta_card)
            build_thumbnail(images_by_scene[0][0], script.title,
                            out_path.with_suffix(".jpg"), cfg)
            meta_path = write_metadata(
                script, out_path, cfg,
                duration_seconds=sum(durations),
                subtitle_file=srt_path.name if srt_path else None,
                karaoke_file=karaoke_name,
                subtitles_burned_in=burned_in,
                cta=cta_voice,
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
            if cta_voice:
                commit_cta(cfg)  # rotation advances only on success
            print(f"  \u2705 {out_path.name}  {total:.0f}s  {size_mb:.1f} MB")

            try:
                from heartbeat import ping

                ping(cfg)  # finished video = pipeline alive
            except Exception:  # noqa: BLE001 - monitoring never breaks a run
                pass

            if not args.keep_work:
                shutil.rmtree(job_dir, ignore_errors=True)

        except Exception as exc:  # noqa: BLE001
            queue.update(job, status="failed", error=str(exc)[:400])
            print(f"  \u274c failed: {exc}")
            if args.verbose:
                traceback.print_exc()
            if not args.keep_going:
                return 1

    if cfg.autopost_after_generate and new_ids:
        _auto_chain(cfg, new_ids)

    print()
    print(queue.format_table())
    if cfg.autopost_after_generate and new_ids:
        print("\nAuto-chain on: packaged + drafted to Buffer (review drafts to release).")
    else:
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
    """Burn one karaoke test frame and check pixels changed. True if visible."""
    import subprocess

    from subtitles import KaraokeEvent, filter_args, write_ass

    test_dir = cfg.work_dir / ".subtest"
    try:
        test_dir.mkdir(parents=True, exist_ok=True)
        subs = write_ass(
            [KaraokeEvent(0.0, 5.0,
                          r"{\c&H00FFFF&}Subtitle{\c&HFFFFFF&} render test")],
            test_dir / "t.ass", cfg.format, cfg.width, cfg.height,
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
        for out, extra in ((plain, []), (burned, ["-vf", filter_args(subs, cfg.format)])):
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
    sub_path = video_path.with_suffix(".ass")
    if not sub_path.exists():
        sub_path = video_path.with_suffix(".srt")
    if not sub_path.exists():
        die(f"no subtitle file next to the video ({video_path.stem}.ass/.srt). "
            f"This job was made with subtitles off — regenerate instead.")
    if not _ffmpeg_has_filter("subtitles"):
        die("this FFmpeg build has no subtitles filter — can't burn in.")
    tmp = video_path.with_name(video_path.stem + "_reburn.mp4")
    print(f"Burning {sub_path.name} into {video_path.name} ...")
    try:
        run(["ffmpeg", "-y", "-loglevel", "warning",
             "-i", str(video_path),
             "-vf", filter_args(sub_path, cfg.format),
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
            if sub_path.suffix == ".ass":
                meta["karaoke_file"] = sub_path.name
            else:
                meta["subtitle_file"] = sub_path.name
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
# batch — render a whole queue of videos unattended
# --------------------------------------------------------------------------
def cmd_batch(cfg, args) -> int:
    """Render many videos in one run: topics file or N auto-variations."""
    import time

    print(BANNER)
    topics: list[str] = []
    if args.topics:
        topics_path = Path(args.topics)
        if not topics_path.exists():
            die(f"topics file not found: {topics_path}")
        topics = [line.strip() for line in
                  topics_path.read_text(encoding="utf-8").splitlines()
                  if line.strip() and not line.strip().startswith("#")]
        if not topics:
            die(f"no topics in {topics_path} (one per line, # = comment).")
    count = args.count or (len(topics) if topics else 3)
    if not topics:
        topics = [None] * count  # type: ignore[list-item] - auto-pick below
    topics = topics[:count]
    if args.dry_run:
        return _dry_run_batch(cfg, topics)

    gen_args = argparse.Namespace(
        topic=None, count=1, seconds=args.seconds, format=args.format,
        images_per_scene=args.images_per_scene, no_subs=args.no_subs,
        no_gemini=args.no_gemini, style=args.style,
        keep_work=args.keep_work, keep_going=True, verbose=args.verbose,
    )
    results: list[tuple[str, str, str]] = []  # topic, status, detail
    for number, topic in enumerate(topics, start=1):
        print(f"\n===== batch {number}/{len(topics)}: {topic or 'auto-pick from backlog'} =====")
        before = {job.id for job in Queue(cfg.state_file).jobs}
        gen_args.topic = topic
        try:
            cmd_generate(cfg, gen_args)
        except SystemExit as exc:
            results.append((topic or "auto", "failed", f"exit {exc.code}"))
            break
        except Exception as exc:  # noqa: BLE001 - batch never dies on one video
            print(f"  \u274c failed: {exc}")
            results.append((topic or "auto", "failed", str(exc)[:120]))
            continue
        new = [j for j in Queue(cfg.state_file).jobs if j.id not in before]
        if not new:
            results.append((topic or "auto", "failed", "no job was created"))
        else:
            job = new[-1]
            results.append((job.topic or topic or "auto", job.status,
                            job.title or job.error[:100]))
        if number < len(topics) and args.sleep > 0:
            print(f"  sleeping {args.sleep}s before the next video...")
            time.sleep(args.sleep)

    print("\n===== batch summary =====")
    ok = sum(1 for _, status, _ in results if status == "generated")
    for topic, status, detail in results:
        mark = "\u2705" if status == "generated" else "\u274c"
        print(f"  {mark} [{status}] {topic[:60]} — {detail[:70]}")
    print(f"\n{ok}/{len(results)} videos generated.")
    if ok:
        print("Next: python main.py package")
    return 0 if ok == len(results) else 1


# --------------------------------------------------------------------------
# bot — render videos from your phone via Telegram
# --------------------------------------------------------------------------
def _notify_telegram(cfg, text: str) -> None:
    """Best-effort Telegram DM. Never raises — the schedule must survive it."""
    if not (cfg.telegram_enabled and cfg.telegram_token and cfg.telegram_owner):
        return
    try:
        from bot import PhoneBot
        PhoneBot(cfg).send_message(cfg.telegram_owner, text)
    except Exception as exc:  # noqa: BLE001
        print(f"  [schedule] telegram notify failed: {exc}")


def _next_slot(times: list[str], now):
    """Next run time: the upcoming HH:MM today, else the first one tomorrow."""
    from datetime import timedelta
    if not times:
        return now
    cands = [now.replace(hour=int(t[:2]), minute=int(t[3:]), second=0, microsecond=0)
             for t in times]
    upcoming = [c for c in cands if c > now]
    if upcoming:
        return min(upcoming)
    return min(cands) + timedelta(days=1)


def cmd_topics(cfg, args) -> int:
    """View and refill the topic backlog."""
    from topics import load_backlog, pop_topic, save_backlog, top_up_backlog

    path = cfg.topics_backlog_file
    if args.add:
        topics = load_backlog(path)
        if args.add.strip().lower() in {t.lower() for t in topics}:
            print("already in backlog.")
        else:
            topics.append(args.add.strip())
            save_backlog(path, topics)
            print(f"added ({len(topics)} in backlog).")
        return 0
    if args.pop:
        topic = pop_topic(path)
        print(topic if topic else "(backlog empty)")
        return 0
    if args.topup:
        added, total = top_up_backlog(cfg)
        if added:
            print(f"added {len(added)} topics ({total} in backlog):")
            for topic in added:
                print(f"  + {topic}")
        else:
            print(f"backlog unchanged ({total}/{cfg.topics_backlog_target}).")
        return 0
    topics = load_backlog(path)
    print(f"backlog: {path} ({len(topics)}/{cfg.topics_backlog_target})")
    for number, topic in enumerate(topics, 1):
        print(f"  {number}. {topic}")
    if not topics:
        print("empty — run: python main.py topics --topup")
    return 0


def cmd_schedule(cfg, args) -> int:
    """Render videos unattended: N per day, topics from the backlog."""
    import time as _time
    from datetime import datetime, timedelta

    from topics import load_backlog, pop_fresh_topic, top_up_backlog

    print(BANNER)
    per_day = args.per_day or cfg.schedule_per_day
    per_day = max(1, min(24, per_day))
    raw_times = args.at.split(",") if args.at else cfg.schedule_times
    times: list[str] = []
    for raw in raw_times:
        import re
        match = re.fullmatch(r"\s*([01]?\d|2[0-3]):([0-5]\d)\s*", raw.strip())
        if match:
            times.append(f"{int(match.group(1)):02d}:{match.group(2)}")
        elif raw.strip():
            print(f"  ignoring bad time {raw.strip()!r} (use HH:MM).")
    times = sorted(set(times))
    backlog = cfg.topics_backlog_file

    def fire() -> None:
        used = [job.topic for job in Queue(cfg.state_file).jobs]
        if args.topup and len(load_backlog(backlog)) < cfg.topics_backlog_target:
            top_up_backlog(cfg, used=used)
        topic = pop_fresh_topic(backlog, used)
        if topic is None:
            topic = cfg.topic
            print("  backlog dry — using channel topic")
        else:
            print(f"  topic from backlog: {topic}")
        gen_args = argparse.Namespace(
            topic=topic, count=1, seconds=args.seconds, format=args.format,
            images_per_scene=args.images_per_scene, no_subs=args.no_subs,
            no_gemini=args.no_gemini, style=args.style,
            keep_work=args.keep_work, keep_going=True, verbose=args.verbose,
        )
        before = {job.id for job in Queue(cfg.state_file).jobs}
        try:
            cmd_generate(cfg, gen_args)
        except SystemExit as exc:
            print(f"  run ended early (exit {exc.code})")
        except Exception as exc:  # noqa: BLE001 - the schedule never dies on one video
            print(f"  \u274c failed: {exc}")
        new = [j for j in Queue(cfg.state_file).jobs if j.id not in before]
        if new and new[-1].status == "generated":
            _notify_telegram(cfg, f"\u2705 scheduled video done: {new[-1].title}")
        else:
            detail = ((new[-1].error or "")[:100] if new else "") or "unknown"
            _notify_telegram(cfg, f"\u274c scheduled video failed: {detail}")

    if args.dry_run:
        return _dry_run_schedule(cfg, times, per_day)
    if args.once:
        fire()
        return 0
    if times:
        print(f"Schedule: {len(times)}x daily at {', '.join(times)} (backlog: {backlog})")
        next_run = _next_slot(times, datetime.now())
    else:
        print(f"Schedule: {per_day}x daily, evenly spaced (backlog: {backlog})")
        next_run = datetime.now()
    print("Ctrl+C stops the scheduler.\n")
    idle_ticks = 0
    try:
        while True:
            now = datetime.now()
            if now >= next_run:
                print(f"\n===== scheduled run at {now:%H:%M} =====")
                try:
                    fire()
                except Exception as exc:  # noqa: BLE001
                    print(f"  \u274c run crashed: {exc}")
                if times:
                    next_run = _next_slot(times, datetime.now() + timedelta(seconds=60))
                else:
                    next_run = datetime.now() + timedelta(hours=24 / per_day)
                print(f"  next run: {next_run:%a %H:%M}")
                idle_ticks = 0
            else:
                _time.sleep(min(60, max(1, (next_run - now).total_seconds())))
                idle_ticks += 1
                if idle_ticks % 60 == 0:
                    print(f"  [schedule] idle, next run {next_run:%a %H:%M}")
    except KeyboardInterrupt:
        print("\nscheduler stopped.")
        return 0


def cmd_crew(cfg, args) -> int:
    from crew import _tg_ping, run_mission

    if args.stop:
        (cfg.root / "crew_stop").write_text("stop", encoding="utf-8")
        print("Stop requested — halting after the current video.")
        return 0
    if args.status:
        from crew import mission_log_tail, mission_status_text

        print(mission_status_text(cfg) or "No mission yet.")
        print()
        print(mission_log_tail(cfg, count=5))
        return 0

    def say(message: str) -> None:
        print(message)
        _tg_ping(cfg, message)

    print(run_mission(cfg, args.days, args.per_day, args.live,
                      args.goal or "grow the channel", say=say))
    return 0


def cmd_yt(cfg, args) -> int:
    from youtube import (YouTubeClient, channel_stats, extract_id,
                         search_shorts, video_stats)

    if not cfg.youtube_api_keys:
        die("no YouTube API key (youtube.api_keys) — enable YouTube Data API v3\n"
            "at https://console.cloud.google.com/apis/library/youtube.googleapis.com")
    client = YouTubeClient(cfg.youtube_api_keys)
    if args.search:
        print(f"  [yt] niche search costs ~100 quota units "
              f"(key has 10k/day).")
        results = search_shorts(client, args.search)
        for pos, item in enumerate(results, start=1):
            print(f"  {pos}. {item['title'][:70]} — {item['channel'][:30]} "
                  f"({item['id']})")
        print(f"  quota used this run: ~{client.spent} units.")
        return 0
    if not args.ref:
        die('give me something to look up: python main.py yt "@handle"')
    try:
        kind, ident = extract_id(args.ref)
    except ValueError as exc:
        die(str(exc))
    if kind == "video":
        info = video_stats(client, ident)
        minutes, seconds = divmod(info["duration_s"], 60)
        print(f"{info['title']}\n  {info['channel']} · {info['published']} · "
              f"{minutes}:{seconds:02d}\n  {info['views']:,} views · "
              f"{info['likes']:,} likes · {info['comments']:,} comments")
    else:
        info = channel_stats(client, args.ref)
        print(f"{info['title']} ({info['handle']})\n  "
              f"{info['subs']:,} subs · {info['videos']:,} videos · "
              f"{info['views']:,} views · since {info['created']}")
    print(f"  quota used this run: ~{client.spent} units.")
    return 0


def cmd_stats(cfg, args) -> int:
    from analytics import channel_stats

    stats = channel_stats(cfg, days=args.days)
    if "error" in stats:
        die(stats["error"])
    print(f"Channel — last {stats['days']} days")
    print(f"  sent: {stats['posts']} | scheduled: {stats['scheduled']}")
    for service, bucket in stats["services"].items():
        print(f"  {service}: {bucket['posts']} posts, {bucket['views']} views, "
              f"reach {bucket['reach']}, eng {bucket['eng_rate_avg']}%")
    if stats["top"]:
        print(f"  top: {stats['top']['service']} {stats['top']['views']} views "
              f"({stats['top']['text']})")
    elif not stats["posts"]:
        print("  nothing sent in this window yet.")
    return 0


def cmd_jarvis(cfg, args) -> int:
    from jarvis import run_task

    text = " ".join(args.task).strip()
    if not text:
        die('give me a task in quotes: python main.py jarvis "make 2 videos..."')
    print(run_task(cfg, text, say=print))
    return 0


def cmd_bot(cfg, args) -> int:
    """Poll Telegram for topics; render each; send back the finished video."""
    print(BANNER)
    if not cfg.telegram_token:
        die("no Telegram bot token — message @BotFather for one, then set "
            "telegram.bot_token in config.yaml (or export TELEGRAM_BOT_TOKEN).")
    if not cfg.telegram_owner:
        die("no Telegram owner id — message @userinfobot, then set "
            "telegram.owner_id in config.yaml.")
    if not cfg.telegram_enabled:
        print("NOTE: telegram.enabled is false — starting anyway. "
              "Set it true to silence this.\n")
    if args.style is not None:
        cfg.data["video"]["style"] = args.style
    print(f"Video settings for bot renders: {cfg.format} {cfg.width}x{cfg.height}, "
          f"~{cfg.target_seconds}s, {cfg.images_per_scene} images/scene, "
          f"style {cfg.style}.\n")
    from bot import PhoneBot
    try:
        PhoneBot(cfg, seconds=args.seconds, fmt=args.format).run_forever()
    except KeyboardInterrupt:
        print("\n  [bot] stopped. Bye!")
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


def cmd_costs(cfg, args) -> int:
    """Azure OpenAI spend vs caps (the $100-credit trust dashboard)."""
    from azure_openai import costs_report, ledger_path_for

    path = ledger_path_for(cfg)
    report = costs_report(path)
    print(f"Azure OpenAI spend  (ledger: {path})")
    print(f"  today : ${report['today_usd']:.4f} ({report['calls_today']} calls) — "
          f"cap ${cfg.azure_max_usd_per_day:.2f}/day")
    print(f"  month : ${report['month_usd']:.4f} ({report['calls_total']} calls total) — "
          f"cap ${cfg.azure_max_usd_per_month:.2f}/month")
    if not cfg.azure_api_key or not cfg.azure_endpoint:
        print("  lane  : not configured (set ai.azure_endpoint/api_key/deployment/model)")
    else:
        print(f"  lane  : {cfg.azure_model} on {cfg.azure_deployment} — "
              f"caps enforced, overruns fall back to free lanes")
    return 0

def cmd_errors(cfg, args) -> int:
    from datetime import datetime

    failed = [job for job in Queue(cfg.state_file).jobs
              if job.status == "failed"][-args.count:]
    if not failed:
        print("No failed jobs.")
        return 0
    for job in failed:
        when = datetime.fromtimestamp(job.created_at).strftime("%m-%d %H:%M")
        print(f"=== {job.id} · {when} · {job.title or job.topic} ===")
        print(job.error or "(no error text)")
        print()
    print(f"({len(failed)} shown. Full ffmpeg logs: work/mux_debug_*.log)")
    return 0


def cmd_queue(cfg, args) -> int:
    print(Queue(cfg.state_file).format_table())
    return 0


def cmd_autopost(cfg, args) -> int:
    from autopost import (BufferClient, BufferError, build_post_input,
                          match_channels, newest_video, read_sidecar,
                          resolve_mode, upload_video)

    from config import BUFFER_SERVICES

    if not cfg.buffer_api_key:
        die("no Buffer API key. Get one free at https://publish.buffer.com/settings/api "
            "and set buffer.api_key (or export BUFFER_API_KEY).")
    video = Path(args.file) if args.file else newest_video(cfg.out_dir)
    if video is None:
        die(f"no .mp4 in {cfg.out_dir} — generate one first.")
    if not video.exists():
        die(f"video not found: {video}")
    try:
        mode, due_at, save_draft = resolve_mode(
            publish=args.publish, schedule=args.schedule, at=args.at)
    except BufferError as exc:
        die(str(exc))
    if args.channels:
        wanted = [name.strip().lower() for name in args.channels.split(",")]
        junk = [name for name in wanted if name not in BUFFER_SERVICES]
        if junk:
            die(f"unknown channel(s) {junk} — want: {', '.join(BUFFER_SERVICES)}")
    else:
        wanted = cfg.buffer_channels
    print(f"Autoposting {video.name} ({video.stat().st_size // 1024} KB) "
          f"to {', '.join(wanted)}...")
    try:
        if args.video_url:
            video_url = args.video_url
            print(f"  [autopost] using provided video URL")
        else:
            if not cfg.cloudinary_cloud_name:
                die("no video host configured. Set buffer.cloud_name plus an "
                    "upload_preset (unsigned) or cloudinary_api_key/_secret "
                    "(signed) — see config.example.yaml — or pass --video-url.")
            print(f"  [autopost] uploading to Cloudinary...")
            if cfg.cloudinary_preset:
                video_url = upload_video(video, cfg.cloudinary_cloud_name,
                                         cfg.cloudinary_preset)
            elif cfg.cloudinary_api_key and cfg.cloudinary_api_secret:
                from autopost import upload_video_signed

                video_url = upload_video_signed(
                    video, cfg.cloudinary_cloud_name,
                    cfg.cloudinary_api_key, cfg.cloudinary_api_secret)
            else:
                die("buffer.cloud_name is set but no credentials: add "
                    "upload_preset (unsigned) or cloudinary_api_key/_secret.")
            print(f"  [autopost] hosted: {video_url}")
        client = BufferClient(cfg.buffer_api_key)
        orgs = client.organizations()
        if not orgs:
            raise BufferError("no organizations on this Buffer account")
        picked = match_channels(client.channels(orgs[0]["id"]), wanted)
        meta = read_sidecar(video)
        title = args.title or meta.get("title") or video.stem
        description = args.desc or meta.get("description") or ""
        tags = meta.get("tags") or []
        for channel in picked:
            post = build_post_input(
                channel["id"], channel["service"], video_url, title,
                description, tags, cfg, save_to_draft=save_draft,
                mode=mode, due_at=due_at)
            created = client.create_post(post)
            when = f" due {created['dueAt']}" if created.get("dueAt") else ""
            print(f"  ✅ {channel['service']} ({channel['name']}): "
                  f"{created['status']} id={created['id']}{when}")
    except BufferError as exc:
        print(f"\n❌ autopost failed: {exc}")
        return 1
    if save_draft:
        print("\nDrafts saved — review & release them in your Buffer dashboard.")
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
                   help="pictures per narrated scene, 1-6 (default 3)")
    p.add_argument("--style", choices=["photoreal", "cartoon", "stickman"],
                   help="art direction: photoreal (default), cartoon, or "
                        "stickman whiteboard explainer")
    p.add_argument("--no-subs", action="store_true", help="skip subtitles for this run")
    p.add_argument("--no-gemini", action="store_true", dest="no_gemini",
                   help="skip Gemini this run (scripts fall straight to Groq/OpenRouter — faster when Gemini keys are flaky)")
    p.add_argument("--keep-work", action="store_true", help="keep intermediate files")
    p.add_argument("--keep-going", action="store_true", help="continue after a failure")
    p.add_argument("--verbose", action="store_true")

    p = sub.add_parser("batch", help="render many videos unattended")
    p.add_argument("--topics", help="text file with one topic per line (# = comment)")
    p.add_argument("--count", type=int, default=0,
                   help="how many videos (default: all topics, or 3)")
    p.add_argument("--sleep", type=int, default=5,
                   help="seconds between videos (default 5)")
    p.add_argument("--seconds", type=int, help="target length, e.g. 45 for a Short")
    p.add_argument("--format", choices=["landscape", "portrait"])
    p.add_argument("--images-per-scene", type=int, dest="images_per_scene")
    p.add_argument("--style", choices=["photoreal", "cartoon", "stickman"])
    p.add_argument("--no-subs", action="store_true")
    p.add_argument("--no-gemini", action="store_true", dest="no_gemini")
    p.add_argument("--keep-work", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would render, then stop")

    p = sub.add_parser("topics", help="view/refill the topic backlog")
    p.add_argument("--topup", action="store_true",
                   help="ask Gemini to refill the backlog")
    p.add_argument("--add", default=None, metavar="TEXT",
                   help="append one topic by hand")
    p.add_argument("--pop", action="store_true",
                   help="print and remove the first topic")
    p = sub.add_parser("schedule", help="render videos unattended every day")
    p.add_argument("--per-day", type=int, default=None,
                   help="videos per day (default: schedule.per_day)")
    p.add_argument("--at", default=None, metavar="HH:MM,...",
                   help='fixed clock times, e.g. "08:00,20:00"')
    p.add_argument("--seconds", type=int, default=None)
    p.add_argument("--format", default=None, choices=["landscape", "portrait"])
    p.add_argument("--images-per-scene", type=int, default=None)
    p.add_argument("--style", default=None, choices=["photoreal", "cartoon", "stickman"])
    p.add_argument("--no-subs", action="store_true")
    p.add_argument("--no-gemini", action="store_true", dest="no_gemini")
    p.add_argument("--keep-work", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--once", action="store_true",
                   help="render one backlog topic now and exit")
    p.add_argument("--no-topup", dest="topup", action="store_false", default=True,
                   help="never auto-refill the backlog")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would render, then stop")
    p = sub.add_parser("bot", help="render videos from your phone via Telegram")
    p.add_argument("--seconds", type=int, help="target length for bot renders")
    p.add_argument("--format", choices=["landscape", "portrait"],
                   help="orientation for bot renders")
    p.add_argument("--style", choices=["photoreal", "cartoon", "stickman"],
                   help="art direction for bot renders")

    p = sub.add_parser("crew", help="autonomous mission: the crew posts for N days")
    p.add_argument("--days", type=int, default=3, help="mission length")
    p.add_argument("--per-day", type=int, default=4, help="videos per day")
    p.add_argument("--live", action="store_true",
                   help="really publish (default: drafts)")
    p.add_argument("--goal", default="grow the channel",
                   help="mission goal in plain words")
    p.add_argument("--status", action="store_true",
                   help="show mission progress + recent chatter, don't start")
    p.add_argument("--stop", action="store_true",
                   help="halt the running mission")

    p = sub.add_parser("yt", help="YouTube stats: video/channel lookup or Shorts niche search")
    p.add_argument("ref", nargs="?", help="video/channel URL, ID, or @handle (1 quota unit)")
    p.add_argument("--search", metavar="QUERY", default="",
                   help="top Shorts for a niche query (~100 quota units)")

    p = sub.add_parser("stats", help="channel performance from Buffer")
    p.add_argument("--days", type=int, default=30, help="lookback window")

    p = sub.add_parser("jarvis", help="give the channel manager a task")
    p.add_argument("task", nargs="+", help="plain-language task in quotes")

    p = sub.add_parser("package", help="build upload-ready kits")
    p.add_argument("--id", help="package only the job with this id (prefix ok)")
    p.add_argument("--limit", type=int, help="max kits to build this run")

    p = sub.add_parser("reburn", help="burn subtitles into an existing video")
    p.add_argument("id", help="job id (prefix ok)")

    p = sub.add_parser("published", help="record a manual upload's URL")
    p.add_argument("id", help="job id (prefix ok)")
    p.add_argument("url", help="the YouTube URL, e.g. https://youtu.be/....")

    p = sub.add_parser("errors", help="full text of recent failures (for debugging)")
    sub.add_parser("costs", help="Azure OpenAI spend vs caps")
    p.add_argument("--count", type=int, default=3, help="how many failures to show")

    sub.add_parser("queue", help="show the queue")

    p = sub.add_parser("voices", help="list available voiceover voices")
    p.add_argument("--lang", default="en-", help="voice prefix filter, e.g. en-, it-, de-")

    p = sub.add_parser("autopost", help="post a finished video via Buffer")
    p.add_argument("file", nargs="?", help="video to post (default: newest .mp4 in out/)")
    p.add_argument("--channels", help="comma list, e.g. youtube,tiktok (default: config)")
    p.add_argument("--title", help="override the sidecar title")
    p.add_argument("--desc", help="override the sidecar description")
    p.add_argument("--video-url", help="skip Cloudinary, use this public mp4 URL")
    p.add_argument("--publish", action="store_true", help="post immediately (default: draft)")
    p.add_argument("--schedule", action="store_true", help="add to Buffer queue slots")
    p.add_argument("--at", help="schedule ISO time, e.g. 2026-09-15T18:00")

    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
    except ValueError as exc:
        die(str(exc))

    try:
        from sentry_util import init_sentry

        init_sentry(cfg, args.command)
    except Exception:  # noqa: BLE001 - reporting must never break dispatch
        pass

    handlers = {
        "preflight": cmd_preflight,
        "generate": cmd_generate,
        "batch": cmd_batch,
        "topics": cmd_topics,
        "schedule": cmd_schedule,
        "bot": cmd_bot,
        "jarvis": cmd_jarvis,
        "stats": cmd_stats,
        "yt": cmd_yt,
        "crew": cmd_crew,
        "package": cmd_package,
        "reburn": cmd_reburn,
        "published": cmd_published,
        "queue": cmd_queue,
        "errors": cmd_errors,
        "costs": cmd_costs,
        "voices": cmd_voices,
        "autopost": cmd_autopost,
    }
    return handlers[args.command](cfg, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\nStopped by you (Ctrl+C). Partial files stay in work/ — re-run to start fresh.")
        sys.exit(130)
