"""Jarvis: the channel manager. Plain-language tasks -> pipeline runs.

Interfaces: `python main.py jarvis "make 3 videos..."` (one-shot, foreground)
and Telegram `/jarvis ...` (background worker, progress + report via chat).
The brain is the script-chain LLM (Groq first, OpenRouter backup) called with
tool definitions; every action tool is capped, validated, and logged to
jarvis_log.jsonl. Scheduling defaults to Buffer drafts until
jarvis.auto_schedule is true — the trust ramp.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from config import Config

MAX_TURNS = 10

SYSTEM = """You manage a faceless YouTube Shorts/TikTok channel. Act via tools, then report.
Now (local): {now} — Europe/Rome unless the user says otherwise.
TOOLS: render_videos(count, topic_hint?) | schedule_video(job_id, due_at_iso) | channel_report(days?)
RULES:
- due_at_iso is full ISO WITH timezone, e.g. 2026-09-16T09:00:00+02:00. Compute staggered slots yourself.
- Render before scheduling; never invent job_ids — use render_videos results.
- Keep to the requested count; if capped, say so.
- If a tool errors on config (no Buffer key, no host), report what's missing instead of retrying.
- Final message: short report — made/scheduled/failed + times. No fluff."""

TOOLS = [
    {"type": "function", "function": {
        "name": "render_videos",
        "description": "Render count videos (fresh auto topics unless topic_hint). Slow (~4 min each). Packages each on success.",
        "parameters": {"type": "object", "properties": {
            "count": {"type": "integer", "description": "videos to render"},
            "topic_hint": {"type": "string", "description": "optional explicit topic; omit for auto-pick"}}},
        "required": ["count"]}},
    {"type": "function", "function": {
        "name": "schedule_video",
        "description": "Send one rendered video to Buffer at an explicit ISO time (draft or scheduled per policy).",
        "parameters": {"type": "object", "properties": {
            "job_id": {"type": "string"},
            "due_at_iso": {"type": "string", "description": "full ISO with timezone"}}},
        "required": ["job_id", "due_at_iso"]}},
    {"type": "function", "function": {
        "name": "channel_report",
        "description": "Recent videos, failures, backlog level, scheduled posts. Read-only.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "lookback window, default 7"}}},
        "required": []}},
]


def _brain(cfg: Config):
    if cfg.groq_api_keys:
        from groq import GroqProvider

        return GroqProvider(cfg.groq_api_keys, cfg.groq_model)
    if cfg.openrouter_api_keys:
        from openrouter import OpenRouterProvider

        return OpenRouterProvider(cfg.openrouter_api_keys, cfg.openrouter_model)
    return None


def _log(cfg: Config, entry: dict) -> None:
    try:
        path = cfg.root / "jarvis_log.jsonl"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _say(say, message: str) -> None:
    try:
        say(message)
    except Exception:
        pass


def run_task(cfg: Config, text: str, say=None) -> str:
    """Execute a task. Returns the final summary. Never raises."""
    say = say or (lambda message: None)
    provider = _brain(cfg)
    if provider is None:
        return "No LLM keys (need Groq or OpenRouter keys) — task not started."
    now = datetime.now().astimezone()
    messages = [{"role": "system", "content": SYSTEM.format(now=now.isoformat())},
                {"role": "user", "content": text}]
    try:
        for _ in range(MAX_TURNS):
            try:
                reply, calls = provider.chat_with_tools(messages, TOOLS, tag="jarvis")
            except Exception as exc:
                return f"Brain failed ({str(exc)[:150]}). Partial work (if any) stands."
            if not calls:
                return (reply or "").strip() or "(the brain said nothing)"
            messages.append({"role": "assistant", "content": reply or "",
                             "tool_calls": [call["raw"] for call in calls]})
            for call in calls:
                _say(say, f"🔧 {call['name']}…")
                result = _dispatch(cfg, call["name"], call["args"], say)
                _log(cfg, {"tool": call["name"], "args": call["args"],
                          "result": str(result)[:500]})
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps(result, ensure_ascii=False)[:4000]})
        return "Ran out of turns (10) — partial work stands. Ask me to continue."
    except Exception as exc:  # noqa: BLE001 - run_task never raises
        return f"Task crashed ({str(exc)[:150]}). Partial work (if any) stands."


def _dispatch(cfg: Config, name: str, args: dict, say) -> dict:
    try:
        if name == "render_videos":
            return render_videos(cfg, int(args.get("count", 0)),
                                 args.get("topic_hint"), say)
        if name == "schedule_video":
            return schedule_video(cfg, str(args.get("job_id", "")),
                                  str(args.get("due_at_iso", "")), say)
        if name == "channel_report":
            return channel_report(cfg, int(args.get("days", 7) or 7))
        return {"error": f"unknown tool: {name}"}
    except Exception as exc:  # noqa: BLE001 - tools report, never raise
        return {"error": f"{name} failed: {str(exc)[:200]}"}


def render_videos(cfg: Config, count: int, topic_hint: str | None, say) -> dict:
    """Render up to cap videos via the normal pipeline. Packages each."""
    from main import cmd_generate

    from jobqueue import Queue

    from package import build_package

    cap = cfg.jarvis_max_videos
    capped = max(0, min(count, cap))
    if capped <= 0:
        return {"error": f"count must be 1..{cap} (got {count})"}
    before = {job.id for job in Queue(cfg.state_file).jobs}
    gen_args = argparse.Namespace(
        topic=topic_hint or None, count=capped, seconds=None, format=None,
        images_per_scene=None, no_subs=False, style=None,
        keep_work=False, keep_going=True, verbose=False,
    )
    _say(say, f"🎬 Rendering {capped} video(s)…")
    try:
        cmd_generate(cfg, gen_args)
    except SystemExit as exc:
        return {"error": f"render exited early (code {exc.code})"}
    new = [job for job in Queue(cfg.state_file).jobs if job.id not in before]
    jobs, failed = [], []
    for job in new:
        if job.status == "generated":
            try:
                kit = build_package(job, cfg)
                Queue(cfg.state_file).update(job, status="packaged",
                                             package_dir=str(kit))
                jobs.append({"job_id": job.id, "title": job.title or job.topic,
                             "status": "packaged"})
            except Exception as exc:
                jobs.append({"job_id": job.id, "title": job.title or job.topic,
                             "status": "generated (packaging failed)"})
        else:
            failed.append({"job_id": job.id, "topic": job.topic,
                           "error": (job.error or "unknown")[:200]})
    _say(say, f"🎬 Rendered {len(jobs)}, failed {len(failed)}.")
    return {"requested": count, "capped_to": capped, "jobs": jobs, "failed": failed}


def schedule_video(cfg: Config, job_id: str, due_at_iso: str, say) -> dict:
    """Schedule one rendered video to Buffer at an explicit time."""
    from autopost import (BufferClient, BufferError, build_post_input,
                          match_channels, read_sidecar, resolve_mode,
                          upload_video)

    from jobqueue import Queue

    job = Queue(cfg.state_file).get(job_id)
    if job is None:
        return {"error": f"no job matching '{job_id}'"}
    video = Path(job.video_file) if job.video_file else None
    if video is None or not video.exists():
        return {"error": f"job {job.id} has no video file yet"}
    try:
        _, due_at, _ = resolve_mode(at=due_at_iso)
    except BufferError as exc:
        return {"error": str(exc)}
    draft = not cfg.jarvis_auto_schedule
    mode = "addToQueue" if draft else "customScheduled"
    if not cfg.buffer_api_key:
        return {"error": "no Buffer API key (buffer.api_key)"}
    try:
        if cfg.cloudinary_preset:
            video_url = upload_video(video, cfg.cloudinary_cloud_name,
                                     cfg.cloudinary_preset)
        elif cfg.cloudinary_api_key and cfg.cloudinary_api_secret:
            from autopost import upload_video_signed

            video_url = upload_video_signed(
                video, cfg.cloudinary_cloud_name,
                cfg.cloudinary_api_key, cfg.cloudinary_api_secret)
        else:
            return {"error": "no video host (buffer.cloud_name + preset or api secret)"}
        client = BufferClient(cfg.buffer_api_key)
        orgs = client.organizations()
        if not orgs:
            return {"error": "no organizations on this Buffer account"}
        picked = match_channels(client.channels(orgs[0]["id"]), cfg.buffer_channels)
        meta = read_sidecar(video)
        posts = []
        for channel in picked:
            post = build_post_input(
                channel["id"], channel["service"], video_url,
                meta.get("title") or video.stem,
                meta.get("description") or "", meta.get("tags") or [], cfg,
                save_to_draft=draft, mode=mode, due_at=due_at)
            created = client.create_post(post)
            posts.append({"service": channel["service"],
                          "status": created.get("status"),
                          "id": created.get("id"), "due": created.get("dueAt")})
    except BufferError as exc:
        return {"error": f"Buffer: {str(exc)[:200]}"}
    _say(say, f"📅 {video.name} → {'draft' if draft else 'scheduled'} {due_at} "
              f"({len(posts)} channels).")
    return {"job_id": job.id, "due_at": due_at, "draft": draft, "posts": posts}


def channel_report(cfg: Config, days: int = 7) -> dict:
    """Recent videos, failures, backlog level, scheduled posts. Read-only."""
    import time

    from jobqueue import Queue

    from topics import load_backlog

    cutoff = time.time() - max(1, days) * 86400
    jobs = [job for job in Queue(cfg.state_file).jobs if job.created_at >= cutoff]
    made = [{"job_id": job.id, "title": (job.title or job.topic)[:80],
             "status": job.status} for job in jobs]
    failed = [{"job_id": job.id, "error": (job.error or "?")[:150]}
              for job in jobs if job.status == "failed"]
    backlog = load_backlog(cfg.topics_backlog_file)
    scheduled = []
    try:
        lines = (cfg.root / "jarvis_log.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines[-50:]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("tool") == "schedule_video" and "error" not in str(entry.get("result", "")):
            scheduled.append(entry)
    return {"days": days, "made": len(made), "failed": len(failed),
            "videos": made[-10:], "failures": failed[-5:],
            "backlog": f"{len(backlog)}/{cfg.topics_backlog_target}",
            "scheduled_recent": scheduled[-10:]}
