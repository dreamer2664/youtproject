#!/usr/bin/env python3
"""Free AI video generator with manual YouTube upload — no upload API, no audit.

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
    python main.py clip --url L  turn a long video into subtitled vertical clips
    python main.py sheet             paste/list source links (the clip queue)
    python main.py meeting stats the AI boardroom reviews your channel numbers
    python main.py meeting pick  ...or argues over today's source video
    python main.py meeting act   the room decides today's move ITSELF (clip
                                 a queued source, or generate a video on a
                                 topic it writes) and carries it out
    python main.py nightbatch    one unattended run: clips -> boardroom ->
                                 report (say /go in the Telegram bot)
    python main.py wakeup        wake/boot task: run a pending /go, then
                                 hibernate again if nobody's at the PC
    python main.py meeting memory  every decision the board ever made
    python main.py meeting last  re-read what the room said (transcript)
    python main.py snap          daily channel stats + retitle alerts
    python main.py scout         validated topic ideas for the backlog
    python main.py keys          API key usage vs free-tier limits
    python main.py keys --advice  what each key lane powers + how to grow
    (full workflow: OPERATIONS.md)

Nothing here costs money and nothing here uploads through the YouTube API —
there is no upload path in the code at all (only `yt`/`snap` make read-only
public-data calls) — which is exactly why no audit or verification can ever be
required. You upload the
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
  Cost: 0. Audit required: none — no upload API, no OAuth.
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
            "the text-provider chain will use the remaining configured "
            "fallbacks. Free key: https://aistudio.google.com/apikey",
        )

    # 3b. full key-pool census (every lane the crew can use).
    from crew import _pools_line

    pools = _pools_line(cfg)
    if (cfg.gemini_api_keys or cfg.groq_llm_api_keys
            or cfg.openrouter_api_keys or cfg.deepseek_api_keys
            or cfg.azure_api_key):
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
    # No BANNER here: the only caller (cmd_batch) already printed it, and
    # printing it twice made the dry run read like two runs.
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
    # No BANNER here either — cmd_schedule printed it (see _dry_run_batch).
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


def _reclaim_interrupted(queue: Queue, backlog_path: Path,
                         work_dir: Path) -> list[str]:
    """Give topics of killed renders back to the backlog (tested).

    Only cmd_generate creates 'queued' jobs, the instant rendering starts,
    so any job still 'queued' when the next run begins is provably from a
    killed one (timeout, Ctrl-C, power cut — KeyboardInterrupt bypasses the
    except that would have marked it 'failed'). Left alone it is a dead
    letter: the table shows 'queued' forever while its topic counts as
    covered and can never be picked again. So: return the topic to the
    front of the backlog, archive the entry as 'reclaimed', and drop the
    ghost's partial work dir. Topics that rendered successfully later stay
    dead; duplicates collapse (live lesson 2026-09-19: ten stuck jobs,
    nine of them copies of one topic).
    """
    from topics import is_same_topic, load_backlog, save_backlog

    stuck = [job for job in queue.jobs
             if job.status == "queued" and str(job.topic or "").strip()]
    if not stuck:
        return []
    done = [job.topic for job in queue.jobs
            if job.status in ("generated", "packaged", "published")]
    backlog = load_backlog(backlog_path)
    returned: list[str] = []
    for job in stuck:
        topic = str(job.topic).strip()
        marker = " (variation "          # --count decoration on a dry backlog
        if marker in topic and topic.endswith(")"):
            topic = topic[:topic.rfind(marker)].strip() or topic
        if any(is_same_topic(topic, old) for old in done):
            note = "interrupted run; topic rendered later anyway"
        elif any(is_same_topic(topic, old) for old in backlog + returned):
            note = "interrupted run; duplicate — topic already back"
        else:
            returned.append(topic)
            note = "interrupted run; topic returned to the backlog"
        shutil.rmtree(work_dir / job.id, ignore_errors=True)
        queue.update(job, status="reclaimed", error=note)
        print(f"  [queue] {job.id} '{topic[:40]}' — {note}")
    if returned:
        save_backlog(backlog_path, returned + backlog)
    return returned


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
    if getattr(args, "image_provider", None):
        from config import resolve_image_route

        try:
            route = resolve_image_route(args.image_provider)
        except ValueError as exc:
            die(str(exc))
        cfg.data["ai"]["image_provider"] = route
        print(f"  [images] route: {args.image_provider} -> {route}\n")
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

    _reclaim_interrupted(queue, cfg.topics_backlog_file, cfg.work_dir)
    # 'reclaimed' entries are killed-run ghosts; their topics are back on
    # the backlog and must count as fresh, not covered.
    used = [job.topic for job in queue.jobs if job.status != "reclaimed"]
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
            from scriptgen import merge_incomplete_scenes

            merges = merge_incomplete_scenes(script)
            if merges:
                print(f"      editorial : merged {merges} scene(s) that "
                      f"split a sentence across scenes")
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
                        "title_alt": getattr(script, "title_alt", ""),
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
            from voiceover import (
                generate_scene_audio, premium_budget_allows, record_premium_use,
            )

            use_premium = premium_budget_allows(cfg)
            if not use_premium:
                reason = ("off (ai.premium_voices: 0)" if cfg.premium_voices == 0
                          else "today's budget is spent")
                print(f"  [voice] premium voice {reason} — edge-tts")
            audio_paths, timings = generate_scene_audio(
                script, cfg, job_dir / "audio", allow_premium=use_premium)
            if use_premium:
                record_premium_use(cfg)

            print("  3/5 images")
            from images import generate_scene_images

            images_by_scene = generate_scene_images(
                script, cfg, job_dir / "images", audio_paths=audio_paths)

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
                images_by_scene, audio_paths, job_dir, cfg,
                narrations=[s.narration for s in script.scenes],
                timings_per_scene=timings,
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
                    sub_style_from_cfg,
                    write_ass,
                    write_srt,
                )

                starts: list[float] = []
                running = 0.0
                for scene_len in durations:
                    starts.append(running)
                    running += scene_len
                narrations = [s.narration for s in script.scenes]
                if cfg.subtitles_mask_profanity:
                    from subtitles import mask_profanity

                    narrations = [mask_profanity(n) for n in narrations]
                cues = build_cues(narrations, timings, starts, durations,
                                  max_chars_for(cfg.format), HEAD_TAIL,
                                  caps=cfg.subtitles_caps)
                srt_path = write_srt(cues, out_path.with_suffix(".srt"))
                print(f"      subtitles : {len(cues)} cues -> {srt_path.name}")
                events = build_karaoke_events(narrations, timings, starts,
                                              durations, HEAD_TAIL,
                                              caps=cfg.subtitles_caps)
                progress_line = None
                if cfg.progress_enabled:
                    progress_line = progress_ass_line(
                        sum(durations), cfg.width, cfg.height,
                        cfg.progress_color, cfg.progress_height,
                        cfg.progress_position)
                ass_path = write_ass(events, out_path.with_suffix(".ass"),
                                     cfg.format, cfg.width, cfg.height,
                                     progress=progress_line,
                                     style=sub_style_from_cfg(cfg))
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
    from subtitles import filter_args, restyle_ass, sub_style_from_cfg

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
    # Current style knobs apply: .ass gets its Style line rebuilt (an
    # old file carries the placement it was generated with), .srt gets
    # force_style.
    style = sub_style_from_cfg(cfg)
    if sub_path.suffix == ".ass":
        sub_path = restyle_ass(sub_path, cfg.format, style,
                               cfg.work_dir / "reburn.ass")
    tmp = video_path.with_name(video_path.stem + "_reburn.mp4")
    print(f"Burning {sub_path.name} into {video_path.name} ...")
    try:
        run(["ffmpeg", "-y", "-loglevel", "warning",
             "-i", str(video_path),
             "-vf", filter_args(sub_path, cfg.format, style),
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
        image_provider=getattr(args, "image_provider", None),
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
    from topics import (_is_command_topic, load_backlog, pop_topic,
                         save_backlog, score_topic_interest,
                         top_up_backlog)

    path = cfg.topics_backlog_file
    if args.audit:
        topics = load_backlog(path)
        if not topics:
            print("backlog empty — run: python main.py topics --topup")
            return 0
        scored = sorted(((score_topic_interest(t), t) for t in topics),
                        key=lambda pair: pair[0][0], reverse=True)
        print(f"backlog audit: {path} "
              f"({len(topics)}/{cfg.topics_backlog_target})")
        tallies = {"STRONG": 0, "ok": 0, "weak": 0}
        for number, ((score, flags), topic) in enumerate(scored, 1):
            verdict = "STRONG" if score >= 3 else ("ok" if score >= 1
                                                   else "weak")
            tallies[verdict] += 1
            print(f"  {number}. [{score:+d}] {verdict:6} {topic} — "
                  f"{', '.join(flags)}")
        print(f"  {tallies['STRONG']} strong / {tallies['ok']} ok / "
              f"{tallies['weak']} weak — drop or sharpen the weak ones "
              f"before a sprint.")
        return 0
    if args.add:
        if _is_command_topic(args.add):
            print(f"  [topics] {args.add.strip()!r} is a command word, "
                  f"not a video topic — not added.")
            return 0
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
            image_provider=getattr(args, "image_provider", None),
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


def cmd_clip(cfg, args) -> int:
    """Clip lane: one or many source videos -> N subtitled vertical clips."""
    print(BANNER)
    from clipper import ClipError, plan_clip_sources, run_clip
    from sheet import mark_sheet, take_pending

    # --sheet N: pull queued sources from the sheet (board picks first)
    sheet_urls: dict[str, str] = {}
    if getattr(args, "sheet", None):
        explicit = {str(u).strip().rstrip("/") for u in (args.url or [])}
        for url, note in take_pending(cfg.sources_sheet, args.sheet):
            if url.rstrip("/") not in explicit:
                sheet_urls[url] = note
        for url in sheet_urls:
            print(f"  [sheet] taking {url} from the sheet"
                  + (f" ({sheet_urls[url]})" if sheet_urls[url] else ""))

    sources = plan_clip_sources(
        getattr(args, "target", ""), (args.url or []) + list(sheet_urls),
        args.file or [])
    if not sources:
        die("give me a source: clip <link-or-path>, or --url/--file "
            "(both repeatable for batch runs), or --sheet to take the "
            "next queued source")
    results: list[tuple[str, str, str]] = []  # (label, status, detail)
    for index, (url, file) in enumerate(sources, start=1):
        label = url or file
        if len(sources) > 1:
            print(f"\n  [clip] source {index}/{len(sources)}: {label}")
        try:
            run_clip(cfg, url=url, file=file,
                     max_clips=args.max_clips, min_len=args.min_len,
                     max_len=args.max_len, use_vision=not args.no_vision,
                     keep_work=args.keep_work,
                     out_dir=Path(args.out) if args.out else None,
                     sub_pos=getattr(args, "sub_pos", "default"),
                     top_count=getattr(args, "top", 0),
                     whole=getattr(args, "whole", None),
                     half=bool(getattr(args, "half", False)))
            results.append((label, "ok", ""))
        except ClipError as exc:
            results.append((label, "failed", str(exc)[:120]))
        except Exception as exc:  # noqa: BLE001 - one bad source kills
            results.append((label, "failed", f"unexpected: {exc}"[:120]))
    if len(sources) > 1:
        print("\n  clip batch summary:")
        for label, status, detail in results:
            mark = "ok " if status == "ok" else "FAIL"
            print(f"    [{mark}] {label}"
                  + (f" — {detail}" if detail else ""))
    elif results and results[0][1] != "ok":
        # single source: the failure message must still reach the user
        label, _, detail = results[0]
        print(f"\n  ❌ {label}" + (f" — {detail}" if detail else ""))
    for url in sheet_urls:
        by_label = {label: (status, detail)
                    for label, status, detail in results}
        status, detail = by_label.get(url, ("failed", "not reached"))
        if status == "ok":
            mark_sheet(cfg.sources_sheet, url, "clipped",
                       f"clips/ — {cfg.topic}")
        else:
            mark_sheet(cfg.sources_sheet, url, "failed",
                       f"clip failed: {detail}"[:200])
    return 0 if any(status == "ok" for _, status, _ in results) else 1


def cmd_parts(cfg, args) -> int:
    """Parts lane: one source video -> N 'Title - Part X' Shorts + kits."""
    print(BANNER)
    from clipper import ClipError, plan_clip_sources
    from parts import run_parts

    sources = plan_clip_sources(
        getattr(args, "target", ""), args.url or [], args.file or [])
    if not sources:
        die("give me a source: parts <link-or-path>, or --url/--file "
            "(both repeatable for batch runs)")
    results: list[tuple[str, str, str]] = []  # (label, status, detail)
    for index, (url, file) in enumerate(sources, start=1):
        label = url or file
        if len(sources) > 1:
            print(f"\n  [parts] source {index}/{len(sources)}: {label}")
        try:
            run_parts(cfg, url=url, file=file,
                      part_len=args.part_len, max_parts=args.max_parts,
                      sub_pos=getattr(args, "sub_pos", "bottom"),
                      header=False if args.no_header else None,
                      out_dir=Path(args.out) if args.out else None,
                      keep_work=args.keep_work, dry_run=args.dry_run,
                      whole=getattr(args, "whole", None),
                      half=bool(getattr(args, "half", False)))
            results.append((label, "ok", ""))
        except ClipError as exc:
            results.append((label, "failed", str(exc)[:120]))
        except Exception as exc:  # noqa: BLE001 - one bad source kills
            results.append((label, "failed", f"unexpected: {exc}"[:120]))
    if len(sources) > 1:
        print("\n  parts batch summary:")
        for label, status, detail in results:
            mark = "ok " if status == "ok" else "FAIL"
            print(f"    [{mark}] {label}"
                  + (f" — {detail}" if detail else ""))
    elif results and results[0][1] != "ok":
        # single source: the failure message must still reach the user
        label, _, detail = results[0]
        print(f"\n  ❌ {label}" + (f" — {detail}" if detail else ""))
    return 0 if any(status == "ok" for _, status, _ in results) else 1


def cmd_longform(cfg, args) -> int:
    """Long-form lane: one source video -> ONE landscape video + kit."""
    print(BANNER)
    from clipper import ClipError, plan_clip_sources
    from longform import run_longform

    sources = plan_clip_sources(
        getattr(args, "target", ""), args.url or [], args.file or [])
    if not sources:
        die("give me a source: longform <link-or-path>, or --url/--file "
            "(both repeatable for batch runs)")
    results: list[tuple[str, str, str]] = []  # (label, status, detail)
    for index, (url, file) in enumerate(sources, start=1):
        label = url or file
        if len(sources) > 1:
            print(f"\n  [longform] source {index}/{len(sources)}: {label}")
        try:
            run_longform(cfg, url=url, file=file,
                         minutes=args.minutes, start_at=args.start,
                         sub_pos=getattr(args, "sub_pos", "bottom"),
                         subs=not args.no_subs,
                         chapters=False if args.no_chapters else None,
                         top=getattr(args, "top", 0) or 0,
                         use_vision=not getattr(args, "no_vision", False),
                         dry_run=args.dry_run, keep_work=args.keep_work,
                         out_dir=Path(args.out) if args.out else None)
            results.append((label, "ok", ""))
        except ClipError as exc:
            results.append((label, "failed", str(exc)[:120]))
        except Exception as exc:  # noqa: BLE001 - one bad source kills
            results.append((label, "failed", f"unexpected: {exc}"[:120]))
    if len(sources) > 1:
        print("\n  longform batch summary:")
        for label, status, detail in results:
            mark = "ok " if status == "ok" else "FAIL"
            print(f"    [{mark}] {label}"
                  + (f" — {detail}" if detail else ""))
    elif results and results[0][1] != "ok":
        # single source: the failure message must still reach the user
        label, _, detail = results[0]
        print(f"\n  ❌ {label}" + (f" — {detail}" if detail else ""))
    return 0 if any(status == "ok" for _, status, _ in results) else 1


def cmd_sheet(cfg, args) -> int:
    """The source sheet: paste links, watch the lanes eat them."""
    from sheet import (SheetError, append_sheet, ensure_sheet,
                       load_sheet, sheet_table)

    if getattr(args, "add", None):
        added = 0
        for entry in args.add:
            url, _, note = entry.partition("|")
            try:
                if append_sheet(cfg.sources_sheet, url.strip(),
                                note.strip()):
                    added += 1
            except SheetError as exc:
                print(f"  [sheet] skipped: {exc}")
        print(f"  [sheet] +{added} source(s) -> {cfg.sources_sheet}")
    else:
        ensure_sheet(cfg.sources_sheet)
        print(f"  [sheet] {cfg.sources_sheet} — paste links as "
              "'url,note' rows (a bare link per line is fine too)")
    print()
    print(sheet_table(load_sheet(cfg.sources_sheet),
                      cfg.sources_sheet))
    return 0


def cmd_meeting(cfg, args) -> int:
    """The AI boardroom: agents meet on demand, decide, report."""
    from pathlib import Path as _P

    if args.kind == "last":
        import meeting as _m

        latest = _m.latest_minutes(cfg)
        if latest is None:
            die("no minutes yet — hold one first: "
                "python main.py meeting pick (or stats)")
        print(f"  [meeting] {latest}\n")
        print(latest.read_text(encoding="utf-8"), end="")
        return 0

    if args.kind == "memory":
        from meeting import memory_report

        print(memory_report(cfg))
        return 0

    if getattr(args, "add", None):
        from sheet import SheetError, append_sheet

        added = 0
        for entry in args.add:
            url, _, note = entry.partition("|")
            try:
                if append_sheet(cfg.sources_sheet, url.strip(),
                                note.strip()):
                    added += 1
            except SheetError as exc:
                print(f"  [sheet] skipped: {exc}")
        print(f"  [meeting] sheet: +{added} source(s) -> "
              f"{cfg.sources_sheet}")
        if args.kind == "pick" and not args.url:
            return 0
    if getattr(args, "dry_run", False):
        import meeting as m

        if args.kind == "pick":
            cands = m._pick_candidates(cfg, args.url or [])
            print(f"  [meeting] dry run: pick meeting, "
                  f"{len(cands)} candidate(s):")
            for url, note in cands:
                print(f"    - {url}" + (f" — {note}" if note else ""))
        elif args.kind == "act":
            cands = m._pick_candidates(cfg, args.url or [])
            print("  [meeting] dry run: act meeting — agenda preview:")
            print()
            print(m._act_agenda(cfg, cands))
        elif args.kind == "review":
            clips = m.collect_review_clips(
                cfg, since=_parse_since(getattr(args, "since", None)),
                ids=getattr(args, "clip", None))
            if not clips:
                print("  [meeting] dry run: review meeting — no new clips "
                      "to review (clip something first).")
                return 0
            print(f"  [meeting] dry run: review meeting — "
                  f"{len(clips)} clip(s) on the table:")
            print()
            print(m._review_agenda(clips, getattr(args, "top", 5)))
            print("  [meeting] the room still has to SPEAK (dry run "
                  "spends nothing).")
        else:
            print("  [meeting] dry run: stats meeting "
                  "(agenda = the last `snap` report)")
        rounds = 1 if args.kind == "review" else cfg.meeting_rounds
        print(f"  Room: {', '.join(r['name'] for r in m.ROLES)} + chair, "
              f"{rounds} round(s) — "
              f"~{len(m.ROLES) * rounds + 2} LLM calls. "
              "Nothing spent.")
        return 0
    from meeting import run_meeting

    # run_meeting calls back with ONE argument (the decision dict) — wrap
    # the (cfg, args) signature, or every board action dies inside its
    # try/except as "missing 1 required positional argument" (live bug,
    # found 2026-10-04: `meeting act` could never execute anything).
    executor = ((lambda decision: _execute_board_action(cfg, decision))
                if args.kind == "act" else None)
    captured: dict = {}

    def events(event):  # capture the decision + forward the live stream
        if event.get("type") == "decision":
            captured["decision"] = event.get("decision")
        if getattr(args, "events_json", False):
            print("##PANEL## " + json.dumps(event, ensure_ascii=False),
                  flush=True)

    clips = None
    if args.kind == "review":
        from meeting import collect_review_clips

        clips = collect_review_clips(
            cfg, since=_parse_since(getattr(args, "since", None)),
            ids=getattr(args, "clip", None))
        if not clips:
            print("  [meeting] no new clips to review — clip something "
                  "first (or widen --since). Nothing spent.")
            _write_decision_json(getattr(args, "json_out", None),
                                 {"summary": "no clips to review",
                                  "picks": []})
            return 0
    extra = {}
    if args.kind == "review":
        extra = {"clips": clips, "top": getattr(args, "top", 5)}
    summary = run_meeting(cfg, args.kind, urls=args.url or [],
                          rounds=args.rounds, render=args.render,
                          send=not args.no_send, executor=executor,
                          events=events, **extra)
    decision = captured.get("decision")
    if args.kind == "review" and decision and clips:
        # ids alone are useless on a phone — carry the titles with them
        by_id = {str(c.get("id")): c for c in clips}
        for pick in decision.get("picks", []):
            cand = by_id.get(pick["id"]) or {}
            pick["title"] = " ".join(
                str(cand.get("title") or "").split())[:120]
            pick["file"] = cand.get("file", "")
    _write_decision_json(getattr(args, "json_out", None), decision)
    print(summary)
    if args.kind == "review" and decision:
        print("\n  🎯 Post these today (strongest first):")
        for pick in decision["picks"]:
            print(f"    {pick['rank']}. {pick.get('title') or pick['id']} "
                  f"— {pick['why']}")
    return 0


def _parse_since(raw):
    """--since: ISO time or a bare date; None on anything unusable."""
    if not raw:
        return None
    from datetime import datetime as _dt

    text = str(raw).strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return _dt.strptime(text, fmt)
        except ValueError:
            continue
    print(f"  [meeting] ignoring unparseable --since {raw!r}")
    return None


def _write_decision_json(path_str, decision) -> None:
    """Best-effort sidecar for the night batch report (never fatal)."""
    if not path_str:
        return
    from pathlib import Path as _P

    try:
        target = _P(path_str)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(decision or {}, indent=2,
                                     ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        print(f"  [meeting] could not write {path_str}: {exc}")


def _execute_board_action(cfg, args) -> str:
    """Follow-through for `meeting act` (called by meeting.run_meeting).

    clip: render clips from the chosen source and book the sheet.
    generate: render one video from the board's own topic — the normal
    generate lane (script + stock photos); no b-roll path involved. This
    pipeline is EXEMPT from the meeting's call budget (it has its own
    quota headroom, see CAPACITY.md).
    """
    if args["action"] == "clip":
        from clipper import run_clip
        from meeting import _mark_sheet

        url = args["url"]
        print(f"  [act] clipping the board's pick: {url}")
        run_clip(cfg, url=url)
        _mark_sheet(cfg, url, "clipped",
                    f"clips/ — boardroom act ({cfg.topic})")
        return f"Clipped {url} -> clips/ (pregen --push parks it)."

    if args["action"] == "generate":
        import argparse

        topic = args["topic"]
        print(f"  [act] rendering the board's topic: {topic}")
        gen_args = argparse.Namespace(
            topic=topic, count=1, seconds=None, format=None,
            images_per_scene=None, no_subs=False, no_gemini=False,
            style=None, keep_work=False, keep_going=True, verbose=False)
        rc = cmd_generate(cfg, gen_args)
        if rc:
            return (f"Generate failed (exit {rc}) — see the log above; "
                    "the topic is not lost, it's in the minutes + memory.")
        return (f"Rendered the board's video on '{topic}' -> out/ "
                "(kit it with `python main.py package`).")
    return "(nothing to execute)"


def cmd_pregen(cfg, args) -> int:
    """Phone queue: list parked clips, show the best, or push new ones."""
    from pregen import (baseline_pending, best_of_day, bot_sender,
                        format_scorecard, load_manifest, pending_candidates,
                        push_pending, today_local)

    print(BANNER)
    if args.baseline:
        result = baseline_pending(cfg, day=args.date)
        print(f"Baselined {len(result['baselined'])} clip(s) — they stay on "
              f"the PC and will never push (re-render one and it becomes "
              f"pending again).")
        if result["tracked_already"]:
            print(f"{result['tracked_already']} already tracked, untouched.")
        return 0
    if args.push:
        if args.dry_run:
            # Prediction only: no channel, no token, no sends.
            report = push_pending(cfg, None, cfg.telegram_channel,  # type: ignore[arg-type]
                                  limit=args.limit or 0, dry_run=True,
                                  day=args.date)
            if not report["pending"]:
                print("Nothing unpushed — every finished clip is parked.")
            else:
                print(f"Would park {len(report['pending'])} clip(s): "
                      + ", ".join(report["pending"]))
                if report["best"]:
                    print(f"Predicted best: {report['best']}")
            return 0
        channel = cfg.telegram_channel
        if not channel:
            die("no Telegram channel — create a private channel, add your "
                "bot as admin, forward any channel post to @userinfobot "
                "for the id, then set telegram.channel_id in config.yaml.")
        if not cfg.telegram_token:
            die("no Telegram bot token — set telegram.bot_token in "
                "config.yaml (or export TELEGRAM_BOT_TOKEN).")
        report = push_pending(cfg, bot_sender(cfg), channel,
                              limit=args.limit or 0, day=args.date)
        if report["pushed"]:
            print(f"\nParked {len(report['pushed'])} clip(s) "
                  f"(best: {report['best']}).")
        else:
            print("\nNothing new to park.")
        for clip_id, reason in report["skipped"]:
            print(f"  skipped {clip_id}: {reason}")
        for error in report["errors"]:
            print(f"  ERROR {error}")
        return 1 if report["errors"] and not report["pushed"] else 0
    parked = load_manifest(cfg).get("clips") or []
    if args.best:
        day = args.date or today_local()
        best = best_of_day(parked, day)
        if best is None:
            print(f"No clips parked on {day} yet — pregen --push parks them.")
            return 0
        print(f"Best of {day}:\n\n{format_scorecard(best)}")
        return 0
    pushed = [e for e in parked if e.get("pushed_at")]
    if not pushed:
        print("No parked clips yet — pregen --push parks finished clips.")
    else:
        print(f"{len(pushed)} parked clip(s):")
        for entry in pushed[-20:]:
            print(f"  {entry.get('day')}  {entry.get('id')}  "
                  f"{int(entry.get('score') or 0)}/100  "
                  f"{(entry.get('title') or '')[:50]}")
    fresh = pending_candidates(cfg, {"clips": parked})
    ignored = sum(1 for e in parked
                  if e.get("ignored") and not e.get("pushed_at"))
    if ignored:
        print(f"{ignored} baselined (ignored) — only new renders will push.")
    if fresh:
        print(f"\n{len(fresh)} unpushed: "
              + ", ".join(f"{c['id']} ({c['score']})" for c in fresh))
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
        PhoneBot(cfg, seconds=args.seconds, fmt=args.format,
                 no_gemini=getattr(args, "no_gemini", False)).run_forever()
    except KeyboardInterrupt:
        print("\n  [bot] stopped. Bye!")
    return 0


# --------------------------------------------------------------------------
# published / queue / voices
# --------------------------------------------------------------------------
def cmd_scout(cfg, args) -> int:
    """Topic scout: propose -> validate interest -> rank -> append."""
    from topics import load_backlog, save_backlog, scout_topics

    path = Path(args.file) if args.file else cfg.topics_backlog_file
    existing = load_backlog(path)
    print(f"  [scout] backlog: {len(existing)} topics — proposing, "
          f"then checking Wikipedia interest...")
    try:
        found = scout_topics(cfg, max(1, min(20, args.count)), existing)
    except RuntimeError as exc:
        die(f"scout failed ({str(exc)[:140]})")
    if not found:
        print("  [scout] no topic passed the interest bar this run — "
              "try again (the LLM pool varies).")
        return 0
    print(f"  [scout] {len(found)} validated topic(s):")
    for topic, subject, views in found:
        print(f"    + {topic}  [{subject}: {views:,} views/wk]")
    if args.dry_run:
        print("  [scout] dry run — nothing added.")
        return 0
    backlog = existing + [topic for topic, _, _ in found]
    save_backlog(path, backlog)
    print(f"  [scout] backlog now {len(backlog)} topics -> {path}")
    return 0


def cmd_subpreview(cfg, args) -> int:
    """Subtitle style picker on a real frame — nothing is rendered."""
    from clipper import extract_frame
    from subpreview import SAMPLE_TEXT, build_app, probe_duration, save_trio, \
        style_yaml
    from subtitles import sub_style_from_cfg

    src = Path(args.target)
    if not src.exists():
        die(f"video not found: {src}")
    at = args.at if args.at is not None else max(
        0.5, probe_duration(src) / 2)
    work = cfg.work_dir / "subpreview"
    work.mkdir(parents=True, exist_ok=True)
    try:
        frame = extract_frame(src, at, work / "frame.jpg")
    except Exception as exc:  # noqa: BLE001 - decode failure = bad input
        die(f"could not read a frame at {at:.1f}s ({str(exc)[:100]})")
    style = sub_style_from_cfg(cfg)
    text = args.text or SAMPLE_TEXT
    try:
        return build_app(frame, style, text)
    except Exception:  # noqa: BLE001 - headless/SSH: no window possible
        for p in save_trio(src.parent, frame, style, text):
            print(f"  [subpreview] wrote {p}")
        print("  [subpreview] no display found — current style:\n")
        print(style_yaml(style))
        return 0


def cmd_snap(cfg, args) -> int:
    """Stats for every tracked channel via the public Data API.

    ~3 quota units per channel per run. --list / --remove are offline.
    """
    import channelstats as cs
    from youtube import load_snapshots, save_snapshots

    store = cs.store_path(cfg)
    history = load_snapshots(store)
    if args.list:
        channels = cs.tracked(history)
        if not channels:
            print("No channels tracked yet — python main.py snap --add @handle")
            return 0
        print(f"Tracking {len(channels)} channel(s):")
        for c in channels:
            print(f"  {c.get('title') or '?':<40} {c['id']}")
        return 0
    if args.remove:
        removed, problem = cs.remove_channel(history, args.remove)
        if problem == "none":
            die(f"no tracked channel matches {args.remove!r} — "
                f"python main.py snap --list")
        if problem == "many":
            names = ", ".join(c.get("title") or c["id"]
                              for c in cs.match_channels(history, args.remove))
            die(f"{args.remove!r} matches several channels ({names}) — "
                f"use more of the name or the UC… id")
        save_snapshots(store, history)
        print(f"  [snap] stopped tracking: {removed.get('title')} "
              f"(its history stays in snapshots.json)")
        return 0
    if not cfg.youtube_api_keys:
        die("no YouTube API key (youtube.api_keys) — enable YouTube Data API v3\n"
            "at https://console.cloud.google.com/apis/library/youtube.googleapis.com")
    client = cs.make_client(cfg)
    for ref in args.add or []:
        try:
            channel_id, title = cs.resolve_channel(client, ref)
        except (ValueError, RuntimeError) as exc:
            die(f"could not add {ref!r}: {exc}")
        is_new = cs.add_channel(history, channel_id, title)
        save_snapshots(store, history)
        print(f"  [snap] {'tracking' if is_new else 'already tracking'}: "
              f"{title}")
    if not cs.tracked(history):
        die("no channels tracked yet — add each channel once with:\n"
            "  python main.py snap --add @yourhandle\n"
            "  (a channel link or any video link from it works too)")
    try:
        fetch = max(1, min(500, args.fetch)) if args.fetch else None
        result = cs.run_snapshot(cfg, client, fetch_limit=fetch)
    except LookupError:
        die("no channels tracked yet — python main.py snap --add @yourhandle")
    history, today, prev = result["history"], result["today"], result["prev"]
    if args.json:
        import json as _json
        print(_json.dumps(next(d for d in history["days"]
                               if d["date"] == today), indent=1))
    else:
        recent = max(1, min(500, args.recent or cfg.snap_settings["recent"]))
        report, new_flags = cs.build_channel_report(
            history, today, prev, recent=recent, sort=args.sort,
            only=args.channel or "", errors=result["errors"])
        print(report)
        if new_flags:
            history["flagged"] = sorted(set(history["flagged"]) | set(new_flags))
            save_snapshots(store, history)
            print("  (retitle candidates flagged once — they will not "
                  "re-appear)")
    if args.export:
        paths = cs.export_snapshot(cfg, history, today, prev)
        print(f"\n  exported {paths['rows']} video row(s):")
        if paths["xlsx"]:
            print(f"    {paths['xlsx']}   <- open this one in Excel")
        else:
            print("    (no .xlsx: pip install openpyxl — Excel with EU "
                  "settings opens the CSV as one column)")
        print(f"    {paths['csv']}")
        print(f"    {paths['channels_csv']}")
    print(f"  quota: {client.spent} units spent of 10,000/day")
    return 0


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


def cmd_panel(cfg, args) -> int:
    """The click-only panel: one local page, big buttons, no typing."""
    from panel import serve

    return serve(cfg, host=args.host, port=args.port,
                 open_browser=not args.no_browser)


def cmd_wakeup(cfg, args) -> int:
    """Wake/boot task: check Telegram once, run a pending /go, hibernate."""
    from wakeup import run as wake_run

    return wake_run(cfg, sleep_after=args.sleep_after, no_inbox=args.no_inbox,
                    dry_run=args.dry_run, min_idle=args.min_idle)


def cmd_nightbatch(cfg, args) -> int:
    """One unattended run: clip the queue, generate, park, report."""
    print(BANNER)
    from nightbatch import run

    return run(cfg, clips=args.clips, count=args.count, seconds=args.seconds,
               style=args.style, image_provider=args.image_provider,
               max_clips=args.max_clips, push=not args.no_push,
               report=not args.no_report, fresh=args.fresh,
               force=args.force, dry_run=args.dry_run, date=args.date,
               meeting=args.meeting, review=args.review, top=args.top,
               if_requested=args.if_requested)


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


def cmd_keys(cfg, args) -> int:
    """API key dashboard: self-counted usage + free-tier refill times."""
    from keystats import advice_report, build_status, month_report, run_probes

    if getattr(args, "advice", False):
        print(advice_report())
        return 0
    if getattr(args, "probe", False):
        print(run_probes(cfg, sample=int(getattr(args, "sample", 0) or 0)))
        return 0
    if getattr(args, "month", False):
        print(month_report())
        return 0
    print(build_status(cfg))
    return 0


def main() -> int:
    from clipper import (MAX_CLIP_SECONDS, MAX_CLIPS_DEFAULT,
                         MIN_CLIP_SECONDS)

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
    p.add_argument("--image-provider", dest="image_provider", default=None,
                   metavar="ROUTE",
                   help="image route for this run: stock (real photos, "
                        "Pexels first), ai (Gemini images) or free "
                        "(Pollinations) — or a raw provider name "
                        "(pexels/pixabay/pollinations/gemini)")
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
    p.add_argument("--image-provider", dest="image_provider",
                   default=None,
                   help="stock | ai | free | a raw provider name")

    p = sub.add_parser("topics", help="view/refill the topic backlog")
    p.add_argument("--topup", action="store_true",
                   help="ask Gemini to refill the backlog")
    p.add_argument("--add", default=None, metavar="TEXT",
                   help="append one topic by hand")
    p.add_argument("--pop", action="store_true",
                   help="print and remove the first topic")
    p.add_argument("--audit", action="store_true",
                   help="score every backlog topic for scroll-potential")
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
    p = sub.add_parser("clip", help="turn a long video into subtitled vertical clips")
    p.add_argument("--sheet", type=int, nargs="?", const=1, metavar="N",
                   help="take the next N queued source(s) from the sheet "
                        "(default 1; the boardroom's pick goes first) — "
                        "the row is marked clipped/failed afterwards")
    p.add_argument("--url", action="append",
                   help="YouTube link of the source video (repeatable)")
    p.add_argument("--file", action="append",
                   help="local video file instead of a link (repeatable)")
    p.add_argument("target", nargs="?",
                   help="shortcut: a link or file path (same as --url/--file)")
    p.add_argument("--max-clips", type=int, default=MAX_CLIPS_DEFAULT,
                   help=f"how many clips to keep (default {MAX_CLIPS_DEFAULT})")
    p.add_argument("--min-len", type=int, default=MIN_CLIP_SECONDS,
                   help=f"minimum clip seconds (default {MIN_CLIP_SECONDS})")
    p.add_argument("--max-len", type=int, default=MAX_CLIP_SECONDS,
                   help=f"maximum clip seconds (default {MAX_CLIP_SECONDS})")
    p.add_argument("--no-vision", action="store_true",
                   help="skip the frame quality check")
    p.add_argument("--keep-work", action="store_true",
                   help="keep intermediate files (audio, frames, .ass)")
    p.add_argument("--out", default=None, help="output folder (default clips/) ")
    p.add_argument("--sub-pos", choices=["default", "auto", "top", "middle",
                                         "bottom"], default="default",
                   help="subtitle placement for this run (default: config "
                        "subtitles.position; auto = frame analysis picks "
                        "the calmest third, no API)")
    p.add_argument("--top", type=int, default=0, metavar="N",
                   help="also build ONE 'Top N moments' countdown video "
                        "from this run's best clips (cards + concat, no "
                        "extra API calls)")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--whole", dest="whole", action="store_const",
                       const=True, default=None,
                       help="never cut: the whole video becomes ONE short "
                            "(auto for sources under whole.under_seconds, 180)")
    group.add_argument("--no-whole", dest="whole", action="store_const",
                       const=False,
                       help="always cut, even a short source")
    group.add_argument("--half", action="store_true",
                       help="two near-equal halves, split at the sentence "
                            "end nearest the middle (Part 1 / Part 2)")

    p = sub.add_parser("parts", help="split a video into a 'Title - Part X' Shorts series")
    p.add_argument("--url", action="append",
                   help="YouTube link of the source video (repeatable)")
    p.add_argument("--file", action="append",
                   help="local video file instead of a link (repeatable)")
    p.add_argument("target", nargs="?",
                   help="shortcut: a link or file path (same as --url/--file)")
    p.add_argument("--part-len", type=float, default=None, metavar="SECONDS",
                   help="target seconds per part (default: config parts.part_len, 60)")
    p.add_argument("--max-parts", type=int, default=None, metavar="N",
                   help="episode cap; past it the target widens (default 50)")
    p.add_argument("--keep-work", action="store_true",
                   help="keep intermediate files (audio, .ass, frames)")
    p.add_argument("--no-header", action="store_true",
                   help="skip the fading top title header for this run")
    p.add_argument("--out", default=None, help="output folder (default parts/)")
    p.add_argument("--sub-pos", choices=["default", "auto", "top", "middle",
                                         "bottom"], default="bottom",
                   help="subtitle placement for this run (default bottom: "
                        "shorts-safe, never collides with the top header)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the part windows only: no renders, no LLM "
                        "editorial calls. The transcript is still ingested, "
                        "so a source with no YouTube captions spends Groq "
                        "Whisper minutes ONCE (cached for the real run)")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--whole", dest="whole", action="store_const",
                       const=True, default=None,
                       help="never cut: the whole video becomes ONE short "
                            "(auto for sources under whole.under_seconds, 180)")
    group.add_argument("--no-whole", dest="whole", action="store_const",
                       const=False,
                       help="always cut, even a short source")
    group.add_argument("--half", action="store_true",
                       help="two near-equal halves, split at the sentence "
                            "end nearest the middle (Part 1 / Part 2)")

    p = sub.add_parser("longform",
                       help="any video -> ONE landscape 16:9 long-form video "
                            "(subs + chapters + kit)")
    p.add_argument("--url", action="append",
                   help="YouTube link of the source video (repeatable)")
    p.add_argument("--file", action="append",
                   help="local video file instead of a link (repeatable)")
    p.add_argument("target", nargs="?",
                   help="shortcut: a link or file path (same as --url/--file)")
    p.add_argument("--minutes", type=float, default=None, metavar="N",
                   help="cut ONE episode of ~N minutes instead of the whole "
                        "source (sources within ~30s of N render whole)")
    p.add_argument("--start", type=float, default=0.0, metavar="SECONDS",
                   help="open the episode near this second (snapped to a "
                        "sentence end; default 0:00)")
    p.add_argument("--sub-pos", choices=["default", "auto", "top", "middle",
                                         "bottom"], default="bottom",
                   help="subtitle placement (default bottom: the long-form "
                        "standard)")
    p.add_argument("--no-subs", action="store_true",
                   help="skip the subtitle burn (the kit still ships "
                        "captions.srt)")
    p.add_argument("--no-chapters", action="store_true",
                   help="skip chapter timestamps in the description")
    p.add_argument("--top", type=int, default=0, metavar="N",
                   help="compilation mode: pick the best N chapters and "
                        "build ONE countdown video (cards, best last)")
    p.add_argument("--no-vision", action="store_true", dest="no_vision",
                   help="(--top) skip the frame quality check")
    p.add_argument("--dry-run", action="store_true",
                   help="print the window + chapters only: no render, no "
                        "LLM editorial calls. The transcript is still "
                        "ingested, so a source with no YouTube captions "
                        "spends Groq Whisper minutes ONCE (cached — the "
                        "real run afterwards is free)")
    p.add_argument("--keep-work", action="store_true",
                   help="keep intermediate files (.ass, srt, thumbnails)")
    p.add_argument("--out", default=None,
                   help="output folder (default longform/)")

    p = sub.add_parser("sheet",
                       help="the source sheet: paste links, the lanes "
                            "clip them (boardroom pick, clip --sheet)")
    p.add_argument("--add", action="append", metavar="LINK[|note]",
                   help="queue a source (repeatable); without it the "
                        "sheet is just listed")

    p = sub.add_parser(
        "meeting",
        help="the AI boardroom: stats review, source pick, or board action")
    p.add_argument("kind",
                   choices=["stats", "pick", "act", "review", "memory",
                            "last"],
                   help="stats = review channel numbers | pick = choose "
                        "today's source video | act = the room decides "
                        "today's move itself (clip a queued source, or "
                        "generate a video on a topic it writes) | memory "
                        "= the board's decision log | last = re-read what "
                        "the room said (the full transcript)")
    p.add_argument("--url", action="append", metavar="LINK",
                   help="candidate for a pick meeting (repeatable; the "
                        "source sheet sources/sheet.csv is always "
                        "included)")
    p.add_argument("--add", action="append", metavar="LINK[|note]",
                   help="add a source to the sheet, then stop (no "
                        "meeting)")
    p.add_argument("--rounds", type=int, default=None, metavar="N",
                   help="speaking rounds (default: config meeting.rounds, 2)")
    p.add_argument("--render", action="store_true",
                   help="(pick) render the winning video right after the "
                        "meeting")
    p.add_argument("--no-send", action="store_true", dest="no_send",
                   help="skip the Telegram summary")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="show the room + agenda, spend nothing")
    p.add_argument("--events-json", action="store_true", dest="events_json",
                   help="also print one machine-readable JSON line per "
                        "meeting event (prefixed ##PANEL##) — how the panel "
                        "renders the room live")
    p.add_argument("--since",
                   help="(review) only clips finished at/after this ISO "
                        "time, e.g. 2026-10-04T21:00:00")
    p.add_argument("--clip", action="append", metavar="ID|DIR",
                   help="(review) restrict to this clip id or kit folder "
                        "(repeatable; default: everything new)")
    p.add_argument("--top", type=int, default=5, metavar="N",
                   help="(review) how many clips the room should pick "
                        "(default 5)")
    p.add_argument("--json-out", dest="json_out", metavar="PATH",
                   help="write the chair's decision as JSON (used by the "
                        "night batch to carry picks into the report)")

    p = sub.add_parser("subpreview",
                       help="preview subtitle position/size/font on a real "
                            "frame, then copy the config block")
    p.add_argument("target", help="any video file (a clip, a rendered video)")
    p.add_argument("--at", type=float, default=None,
                   help="seconds into the video for the preview frame "
                        "(default: middle)")
    p.add_argument("--text", default=None,
                   help="sample caption line (default: a zebra example)")

    p = sub.add_parser("bot", help="render videos from your phone via Telegram")
    p.add_argument("--seconds", type=int, help="target length for bot renders")
    p.add_argument("--format", choices=["landscape", "portrait"],
                   help="orientation for bot renders")
    p.add_argument("--style", choices=["photoreal", "cartoon", "stickman"],
                   help="art direction for bot renders")
    p.add_argument("--no-gemini", action="store_true", dest="no_gemini",
                   help="bot renders skip Gemini (Groq/OpenRouter first)")

    p = sub.add_parser("pregen", help="phone queue: park scored clips on Telegram")
    p.add_argument("--push", action="store_true",
                   help="score unpushed clips and send them to your channel")
    p.add_argument("--best", action="store_true",
                   help="show the day's winner + scorecard (no sends)")
    p.add_argument("--date", default=None, metavar="YYYY-MM-DD",
                   help="day bucket for --best/--push (default: today)")
    p.add_argument("--limit", type=int, default=0, metavar="N",
                   help="park at most N clips (0 = all)")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="score + predict the push without sending anything")
    p.add_argument("--baseline", action="store_true",
                   help="mark the current backlog ignored (no sends) so the "
                        "phone queue starts from the next render")
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

    p = sub.add_parser("snap", help="stats for all your channels: views, likes, comments, subs, deltas")
    p.add_argument("--add", action="append", metavar="REF",
                   help="track a channel: @handle, channel link or any video "
                        "link from it (repeatable)")
    p.add_argument("--remove", metavar="NAME",
                   help="stop tracking a channel (name fragment or UC… id)")
    p.add_argument("--list", action="store_true",
                   help="show tracked channels (no API calls)")
    p.add_argument("--channel", metavar="NAME",
                   help="show only this channel (name fragment)")
    p.add_argument("--recent", type=int, default=None, metavar="N",
                   help="videos per channel (default: config snap.recent, 10)")
    p.add_argument("--sort", choices=["new", "views", "eng", "pace"],
                   default="new",
                   help="order within a channel (default new = newest first)")
    p.add_argument("--fetch", type=int, default=None, metavar="N",
                   help="uploads fetched per channel (default snap.fetch_limit, 50)")
    p.add_argument("--export", action="store_true",
                   help="also write out/stats/stats-DATE.xlsx + CSVs")
    p.add_argument("--json", action="store_true",
                   help="print the raw snapshot instead of the report")

    p = sub.add_parser("scout", help="topic scout: LLM proposes, Wikipedia pageviews validate interest")
    p.add_argument("--count", type=int, default=8,
                   help="how many validated topics to add (default 8)")
    p.add_argument("--dry-run", action="store_true",
                   help="show the validated topics without adding them")
    p.add_argument("--file", help="alternate backlog file (for testing)")

    p = sub.add_parser("errors", help="full text of recent failures (for debugging)")
    sub.add_parser("costs", help="Azure OpenAI spend vs caps")
    pk = sub.add_parser("keys", help="API key usage: requests spent, what's left, when quotas refill")
    pk.add_argument("--advice", action="store_true",
                   help="what each key lane powers + how to add capacity")
    pk.add_argument("--probe", action="store_true",
                    help="live-check every provider/key (each check is "
                         "ledgered as tag=probe — nothing spends off the books)")
    pk.add_argument("--month", action="store_true",
                    help="30-day spend rollup per provider, split by call tag")
    pk.add_argument("--sample", type=int, default=0,
                    help="with --probe: check only the first N keys per "
                         "provider (0 = every key)")
    p.add_argument("--count", type=int, default=3, help="how many failures to show")

    sub.add_parser("queue", help="show the queue")

    p = sub.add_parser("voices", help="list available voiceover voices")
    p.add_argument("--lang", default="en-", help="voice prefix filter, e.g. en-, it-, de-")

    p = sub.add_parser("panel",
                       help="click-only control panel in your browser")
    p.add_argument("--port", type=int, default=8765,
                   help="port (default 8765)")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (127.0.0.1 = this machine only)")
    p.add_argument("--no-browser", action="store_true", dest="no_browser",
                   help="don't open the browser window")

    p = sub.add_parser("nightbatch",
                       help="unattended nightly run: clip + generate + park + "
                            "report (Task Scheduler)")
    p.add_argument("--clips", type=int, default=2,
                   help="sheet sources to clip (each ~10 min, up to 10 clips; "
                        "default 2)")
    p.add_argument("--count", type=int, default=2,
                   help="videos to generate from the topic backlog "
                        "(default 2; 0 skips)")
    p.add_argument("--seconds", type=int, help="target length for generated "
                                               "videos, e.g. 45")
    p.add_argument("--style", choices=["photoreal", "cartoon", "stickman"])
    p.add_argument("--image-provider", dest="image_provider", default=None,
                   help="stock | ai | free | a raw provider name")
    p.add_argument("--max-clips", type=int, default=0, dest="max_clips",
                   help="cap clips per source (0 = the clip lane's default)")
    p.add_argument("--no-push", action="store_true", dest="no_push",
                   help="skip parking clips on Telegram")
    p.add_argument("--no-report", action="store_true", dest="no_report",
                   help="don't send the Telegram report (still prints)")
    p.add_argument("--fresh", action="store_true",
                   help="ignore today's journal and redo every step")
    p.add_argument("--force", action="store_true",
                   help="take the lock even if a run looks alive")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="print the plan and exit; runs nothing")
    p.add_argument("--date", help="journal bucket (default: today, local)")
    p.add_argument("--meeting", action="store_true",
                   help="open with the boardroom stats meeting")
    p.add_argument("--review", action="store_true",
                   help="after clipping, hold the boardroom clip review "
                        "(ranks the new clips by hook + quality)")
    p.add_argument("--top", type=int, default=5, metavar="N",
                   help="(review) how many clips the board picks (default 5)")
    p.add_argument("--if-requested", action="store_true", dest="if_requested",
                   help="run only if a /go request is pending; its options "
                        "win over the flags (what the bot + wake task call)")

    p = sub.add_parser("wakeup", help="wake/boot task: check Telegram once, "
                                      "run the pending /go, optionally "
                                      "hibernate again")
    p.add_argument("--sleep-after", action="store_true", dest="sleep_after",
                   help="hibernate when done — only if nobody touched the "
                        "keyboard for --min-idle seconds")
    p.add_argument("--min-idle", type=int, default=300, dest="min_idle",
                   metavar="SECONDS",
                   help="hibernate only after this much keyboard silence "
                        "(default 300)")
    p.add_argument("--no-inbox", action="store_true", dest="no_inbox",
                   help="skip the one-shot Telegram check")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="say what would happen; execute nothing")

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
    try:
        import keystats

        keystats.init(cfg)  # usage ledger for `python main.py keys`
    except Exception:  # noqa: BLE001 - bookkeeping must never break dispatch
        pass

    handlers = {
        "preflight": cmd_preflight,
        "generate": cmd_generate,
        "batch": cmd_batch,
        "topics": cmd_topics,
        "schedule": cmd_schedule,
        "clip": cmd_clip,
        "parts": cmd_parts,
        "longform": cmd_longform,
        "meeting": cmd_meeting,
        "sheet": cmd_sheet,
        "bot": cmd_bot,
        "pregen": cmd_pregen,
        "jarvis": cmd_jarvis,
        "stats": cmd_stats,
        "yt": cmd_yt,
        "crew": cmd_crew,
        "package": cmd_package,
        "reburn": cmd_reburn,
        "published": cmd_published,
        "snap": cmd_snap,
        "scout": cmd_scout,
        "subpreview": cmd_subpreview,
        "queue": cmd_queue,
        "errors": cmd_errors,
        "costs": cmd_costs,
        "keys": cmd_keys,
        "voices": cmd_voices,
        "autopost": cmd_autopost,
        "panel": cmd_panel,
        "nightbatch": cmd_nightbatch,
        "wakeup": cmd_wakeup,
    }
    return handlers[args.command](cfg, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\nStopped by you (Ctrl+C). Partial files stay in work/ — re-run to start fresh.")
        sys.exit(130)
