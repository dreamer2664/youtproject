"""Crew: the autonomous channel team. Mission in, posted videos + reports out.

Four roles, one loop:
- MANAGER (LLM brain on the full fallback chain): owns the mission — ranks
  topics, replans around failures, writes the digest. It substitutes any
  role: no YouTube quota -> topics from its own knowledge; a render fails ->
  backup topic + one retry; posting down -> video stands, retried later.
- SCOUT: YouTube niche data + backlog topup -> fresh assignments. Searches
  are budgeted per day; degrades to backlog-only when APIs fail.
- MAKER: renders + packages through the normal pipeline (jarvis tools).
- HERALD: posts via Buffer, pings every post, audits usage, nightly digest.

`python main.py crew --days 3 --per-day 4 [--live]` runs a mission in the
foreground (Ctrl+C stops gracefully). Without --live everything lands as
Buffer drafts/scheduled: fully automatic, nothing public. Telegram /crew
starts a mission from your phone, /stop halts it via a crew_stop file the
loop checks constantly. State + usage ledger live in crew_state.json, so a
crash resumes instead of restarting.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta

import requests

from config import Config

STOP_FILE = "crew_stop"
STATE_FILE = "crew_state.json"
DAY_START_HOUR = 9
DAY_END_HOUR = 21
HEARTBEAT_MINUTES = 45
POST_DELAY_MINUTES = 15
# Honest estimates (free tiers = $0; the point is quota awareness).
EST_TOKENS_PER_VIDEO = 16000
EST_CHARS_PER_VIDEO = 900
LIVE_MARKERS = ("for real", "really post", "actually post", "go live",
                "publish", "--live")


def _say(say, message: str) -> None:
    try:
        say(message)
    except Exception:
        pass


def _tg_ping(cfg: Config, text: str) -> None:
    """Best-effort Telegram ping for CLI-run missions. Never raises."""
    try:
        if not (cfg.telegram_enabled and cfg.telegram_token
                and cfg.telegram_owner):
            return
        requests.post(
            f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage",
            json={"chat_id": cfg.telegram_owner, "text": text[:3500]},
            timeout=20,
        )
    except Exception:
        pass


def parse_mission(text: str) -> dict:
    """'manage yourself for 3 days, post 4x a day' -> mission spec.

    Drafts unless explicitly told to go live — automatic but unpublished
    is the safe default for a system that runs while you sleep.
    """
    text = (text or "").strip()
    days, per_day = 3, 2
    match = re.search(r"(\d+)\s*days?", text)
    if match:
        days = max(1, min(30, int(match.group(1))))
    match = re.search(r"(\d+)\s*(?:times?|videos?|posts?)?\s*(?:a|per)\s*day",
                      text)
    if match:
        per_day = max(1, min(10, int(match.group(1))))
    lowered = f" {text.lower()} "
    live = " live " in lowered or \
        any(marker in lowered for marker in LIVE_MARKERS)
    return {"days": days, "per_day": per_day, "live": live,
            "goal": text or "grow the channel"}


def _slots(per_day: int) -> list[int]:
    if per_day <= 1:
        return [12]
    span = DAY_END_HOUR - DAY_START_HOUR
    return [DAY_START_HOUR + round(span * i / (per_day - 1))
            for i in range(per_day)]


def _due_now(per_day: int, now: datetime, day_start_hour: int = 0) -> int:
    """Slots passed today (after day_start_hour on mission day 1)."""
    return sum(1 for slot in _slots(per_day)
               if day_start_hour < slot <= now.hour)


def _fresh_usage(today: str) -> dict:
    return {"day": today, "yt_units": 0, "eleven_chars": 0,
            "llm_tokens_est": 0, "searches_today": 0, "searches_total": 0,
            "topups_today": 0, "premium_today": 0, "made_today": 0}


def _load_state(cfg: Config) -> dict:
    try:
        return json.loads((cfg.root / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(cfg: Config, state: dict) -> None:
    try:
        state["log"] = state.get("log", [])[-100:]
        (cfg.root / STATE_FILE).write_text(json.dumps(state, ensure_ascii=False),
                                           encoding="utf-8")
    except OSError:
        pass


def _log(state: dict, message: str) -> None:
    entry = {"ts": datetime.now().astimezone().isoformat(),
             "msg": message[:300]}
    state.setdefault("log", []).append(entry)


def mission_active(cfg: Config) -> dict | None:
    """The live mission state, or None when free to start."""
    state = _load_state(cfg)
    mission = state.get("mission") or {}
    if not mission or state.get("ended"):
        return None
    try:
        heartbeat = datetime.fromisoformat(state.get("heartbeat", ""))
        ends = datetime.fromisoformat(mission.get("ends", ""))
    except (ValueError, TypeError):
        return None
    now = datetime.now().astimezone()
    if now - heartbeat > timedelta(minutes=HEARTBEAT_MINUTES):
        return None
    if now > ends:
        return None
    return state


class _LoggedBrain:
    """Manager brain: asks + counts estimated tokens into the ledger."""

    def __init__(self, provider, state: dict) -> None:
        self.provider = provider
        self.state = state

    def ask(self, prompt: str, tag: str = "crew") -> str:
        reply = self.provider.generate_text(prompt, tag=tag)
        usage = self.state["usage"]
        usage["llm_tokens_est"] += max(1, (len(prompt) + len(reply)) // 4)
        return reply


def _manager_brain(cfg: Config, state: dict):
    try:
        from jarvis import _brain

        provider = _brain(cfg)
    except Exception:
        provider = None
    return _LoggedBrain(provider, state) if provider else None


def _clean_title(line: str) -> str:
    return re.sub(r"^[\d\.\)\-\s]+", "", line.strip()).strip("\"' ")


def _manager_pick(brain, niche: str, candidates: list[str],
                  count: int) -> list[str]:
    """Manager ranks scouted titles. Fallback: original order."""
    if brain is None or len(candidates) <= count:
        return candidates[:count]
    try:
        reply = brain.ask(
            f"Pick the {count} best YouTube Shorts topics from this list for "
            f"a channel about {niche}. Reply with just the {count} titles, "
            f"one per line, nothing else:\n" +
            "\n".join(f"- {c}" for c in candidates[:12]), tag="crew-pick")
        picked = []
        for line in reply.splitlines():
            title = _clean_title(line)
            if not title:
                continue
            for cand in candidates:
                if cand not in picked and (title.lower() in cand.lower()
                                           or cand.lower() in title.lower()):
                    picked.append(cand)
                    break
        rest = [c for c in candidates if c not in picked]
        return (picked + rest)[:count]
    except Exception:
        return candidates[:count]


def _manager_backup_topic(brain, niche: str, error: str) -> str | None:
    """Manager substitutes the writer: fresh topic from its own knowledge."""
    if brain is None:
        return None
    try:
        reply = brain.ask(
            f"One backup YouTube Shorts topic for a channel about {niche} "
            f"(the planned video failed: {error}). Reply with just the "
            f"title, nothing else.", tag="crew-sub")
        title = _clean_title((reply.strip().splitlines() or [""])[0])
        return title if len(title) > 10 else None
    except Exception:
        return None


def _manager_digest(brain, numbers: str) -> str | None:
    if brain is None:
        return None
    try:
        return brain.ask(
            "Write a chatty 6-line channel-owner digest from these numbers "
            "(what posted, views/reach/engagement, quota used). Plain text, "
            "no headers:\n" + numbers, tag="crew-digest")
    except Exception:
        return None


QUERY_TEMPLATES = ["{} shorts", "{} explained", "{} facts"]


def scout_topics(cfg: Config, state: dict, brain, say) -> list[str]:
    """SCOUT: keep fresh topics flowing. Returns the backlog (may be short)."""
    from topics import (is_same_topic, load_backlog, save_backlog,
                        top_up_backlog)

    usage = state["usage"]
    niche = cfg.topic
    if usage["searches_today"] < cfg.crew_max_searches and cfg.youtube_api_keys:
        from youtube import YouTubeClient, search_shorts, video_stats

        client = YouTubeClient(cfg.youtube_api_keys)
        template = QUERY_TEMPLATES[usage["searches_total"] % len(QUERY_TEMPLATES)]
        query = template.format(niche)
        spent_before = client.spent
        try:
            results = search_shorts(client, query, max_results=10)
            usage["searches_today"] += 1
            usage["searches_total"] += 1
            for item in results[:3]:
                try:
                    video_stats(client, item["id"])  # what the niche watches
                except Exception:
                    pass
            picks = _manager_pick(brain, niche,
                                  [r["title"] for r in results],
                                  state["mission"]["per_day"])
            backlog = load_backlog(cfg.topics_backlog_file)
            added = [p for p in picks
                     if not any(is_same_topic(p, e) for e in backlog)]
            if added:
                save_backlog(cfg.topics_backlog_file, backlog + added)
            _say(say, f"🔭 Scout: '{query}' -> {len(added)} topics banked "
                      f"(+{client.spent - spent_before} YT units).")
        except Exception as exc:
            _say(say, f"🔭 Scout: niche scan failed ({str(exc)[:100]}) — "
                      f"backlog only.")
        finally:
            usage["yt_units"] += client.spent - spent_before
    backlog = load_backlog(cfg.topics_backlog_file)
    if len(backlog) < cfg.topics_backlog_target and usage["topups_today"] < 2:
        try:
            added, total = top_up_backlog(cfg)
            usage["topups_today"] += 1
            _say(say, f"🔭 Scout: topped up backlog (+{len(added)}, "
                      f"{total} total).")
        except Exception as exc:
            _say(say, f"🔭 Scout: topup failed ({str(exc)[:100]}).")
        backlog = load_backlog(cfg.topics_backlog_file)
    return backlog


def make_and_post(cfg: Config, state: dict, brain, say,
                  due_iso: str) -> dict | None:
    """MAKER renders, HERALD posts. Returns the posted entry or None."""
    from jarvis import render_videos, schedule_video

    mission = state["mission"]
    usage = state["usage"]
    premium = (usage["premium_today"] < cfg.crew_premium_voices
               and bool(cfg.elevenlabs_api_keys and cfg.elevenlabs_voice_id))
    saved_keys = None
    if not premium and cfg.elevenlabs_api_keys:
        # Voice budget spent for today: force edge-tts for this render.
        saved_keys = cfg.data["channel"]["elevenlabs_api_keys"]
        cfg.data["channel"]["elevenlabs_api_keys"] = []
    jobs: list[dict] = []
    error = "unknown"
    try:
        result = render_videos(cfg, 1, None, say)
        usage["llm_tokens_est"] += EST_TOKENS_PER_VIDEO
        jobs = result.get("jobs") or []
        if not jobs:
            failed = result.get("failed") or []
            error = (failed[0].get("error") if failed else "unknown")[:150]
            backup = _manager_backup_topic(brain, cfg.topic, error)
            if backup:
                _say(say, f"🧠 Manager: render failed, substituting topic: "
                          f"{backup[:80]}")
                result = render_videos(cfg, 1, backup, say)
                usage["llm_tokens_est"] += EST_TOKENS_PER_VIDEO
                jobs = result.get("jobs") or []
                if not jobs:
                    failed = result.get("failed") or []
                    error = (failed[0].get("error") if failed
                             else "unknown")[:150]
    finally:
        if saved_keys is not None:
            cfg.data["channel"]["elevenlabs_api_keys"] = saved_keys
    usage["made_today"] += 1
    if premium:
        usage["premium_today"] += 1
        usage["eleven_chars"] += EST_CHARS_PER_VIDEO
    if not jobs:
        _log(state, f"render failed twice: {error}")
        _say(say, f"🎬 Render failed ({error}) — slot skipped, next cycle.")
        return None
    job = jobs[0]
    post = schedule_video(cfg, job["job_id"], due_iso, say)
    if not post.get("posts"):
        _log(state, f"post failed for {job['job_id']}: {post.get('error', '?')}")
        _say(say, f"📮 Post failed ({str(post.get('error', '?'))[:120]}) — "
                  f"video kept, retrying next cycle.")
        return None
    entry = {"ts": datetime.now().astimezone().isoformat(),
             "job_id": job["job_id"], "title": job.get("title", "?")[:100],
             "mode": "live" if mission["live"] else "draft",
             "day": datetime.now().astimezone().date().isoformat()}
    state["posted"].append(entry)
    _say(say, f"📮 Hey, we posted: {entry['title']} "
              f"({'LIVE' if mission['live'] else 'draft'}, "
              f"{len(post['posts'])} channels).")
    return entry


def _usage_line(usage: dict) -> str:
    return (f"YT {usage['yt_units']:,} units · ElevenLabs "
            f"~{usage['eleven_chars']:,} chars · LLM "
            f"~{usage['llm_tokens_est']:,} tokens (all free tiers, $0)")


def daily_digest(cfg: Config, state: dict, brain, say) -> None:
    """HERALD: nightly numbers + usage audit, in plain words."""
    from analytics import channel_stats

    today = datetime.now().astimezone().date().isoformat()
    posted = [p for p in state["posted"] if p["day"] == today]
    live = sum(1 for p in posted if p["mode"] == "live")
    stats = channel_stats(cfg, days=1)
    if "error" in stats or "note" in stats:
        perf = stats.get("error") or stats.get("note") or "no data"
    else:
        parts = [f"{service}: {bucket['views']:,} views, "
                 f"{bucket['eng_rate_avg']}% eng"
                 for service, bucket in stats["services"].items()]
        perf = "; ".join(parts) if parts else "nothing sent yet"
        if stats.get("top"):
            perf += (f" | top: {stats['top']['service']} "
                     f"{stats['top']['views']:,} views")
    numbers = (f"posted today: {len(posted)} ({live} live, "
               f"{len(posted) - live} draft)\n"
               f"last-24h performance: {perf}\n"
               f"mission usage: {_usage_line(state['usage'])}")
    prose = _manager_digest(brain, numbers)
    _say(say, f"🌙 Nightly digest:\n{prose or numbers}")
    state["digests"].append(today)


def _cycle(cfg: Config, state: dict, brain, say) -> str:
    now = datetime.now().astimezone()
    today = now.date().isoformat()
    state["heartbeat"] = now.isoformat()
    usage = state["usage"]
    if usage.get("day") != today:
        for key in ("searches_today", "topups_today", "premium_today",
                    "made_today"):
            usage[key] = 0
        usage["day"] = today
    mission = state["mission"]
    if now >= datetime.fromisoformat(mission["ends"]):
        return "ended"
    scout_topics(cfg, state, brain, say)
    day_start = int(mission["started_day_hour"]) \
        if today == mission["started_day"] else 0
    due = _due_now(mission["per_day"], now, day_start)
    posted_today = sum(1 for p in state["posted"] if p["day"] == today)
    if posted_today < due and usage["made_today"] < mission["per_day"] + 1:
        due_iso = (now + timedelta(minutes=POST_DELAY_MINUTES)).isoformat()
        make_and_post(cfg, state, brain, say, due_iso)
    if now.hour >= cfg.crew_digest_hour and today not in state["digests"]:
        daily_digest(cfg, state, brain, say)
    _save_state(cfg, state)
    return "ok"


def _sleep_until_next_cycle(cfg: Config) -> bool:
    """Sleep cycle_minutes in 30 s chunks. True when /stop appeared."""
    stop = cfg.root / STOP_FILE
    for _ in range(max(1, cfg.crew_cycle_minutes * 2)):
        if stop.exists():
            return True
        time.sleep(30)
    return stop.exists()


def _end_mission(cfg: Config, state: dict, reason: str, say) -> None:
    state["ended"] = datetime.now().astimezone().isoformat()
    posted = state["posted"]
    _save_state(cfg, state)
    _say(say, f"🏁 Mission {reason}: {len(posted)} videos posted "
              f"({sum(1 for p in posted if p['mode'] == 'live')} live).\n"
              f"Total usage: {_usage_line(state['usage'])}")


def run_mission(cfg: Config, days: int, per_day: int, live: bool,
                goal: str, say=None) -> str:
    """Run a mission to completion. Returns the final status. Never raises."""
    say = say or print
    if mission_active(cfg):
        return "A mission is already running — /stop it first."
    old = _load_state(cfg)
    now = datetime.now().astimezone()
    if old.get("mission") and not old.get("ended"):
        state = old  # crashed mid-mission: resume, don't restart.
        mission = state["mission"]
        _say(say, f"🔄 Resuming {mission['days']}d × {mission['per_day']}/day "
                  f"mission ({len(state.get('posted', []))} already posted).")
    else:
        ends = (now + timedelta(days=max(1, days) - 1)).replace(
            hour=23, minute=59, second=0, microsecond=0)
        state = {"mission": {"goal": goal, "days": days, "per_day": per_day,
                             "live": live, "started": now.isoformat(),
                             "started_day": now.date().isoformat(),
                             "started_day_hour": now.hour,
                             "ends": ends.isoformat()},
                 "posted": [], "usage": _fresh_usage(now.date().isoformat()),
                 "digests": [], "heartbeat": now.isoformat(), "ended": None,
                 "log": []}
    stop = cfg.root / STOP_FILE
    if stop.exists():
        stop.unlink()
    cfg.data["jarvis"]["auto_schedule"] = live  # crew owns posting mode
    brain = _manager_brain(cfg, state)
    mission = state["mission"]
    if brain is None:
        _say(say, "🧠 No LLM keys — the Manager runs on procedure, "
                  "everything else works.")
    _say(say, f"🚀 Mission: {mission['days']} days × {mission['per_day']}/day "
              f"({'LIVE posting' if live else 'DRAFT mode — nothing public'}).\n"
              f"Manager{'+brain' if brain else ' (code-only)'} · Scout · "
              f"Maker · Herald on duty.\nGoal: {mission['goal'][:150]}")
    try:
        while True:
            if stop.exists():
                stop.unlink(missing_ok=True)
                _end_mission(cfg, state, "stopped by you", say)
                return "Stopped."
            try:
                outcome = _cycle(cfg, state, brain, say)
            except Exception as exc:
                _log(state, f"cycle crashed: {exc}")
                _say(say, f"⚠️ Cycle hiccup ({str(exc)[:120]}) — continuing.")
                _save_state(cfg, state)
                outcome = "ok"
            if outcome == "ended":
                _end_mission(cfg, state, "complete", say)
                return "Mission complete."
            if _sleep_until_next_cycle(cfg):
                stop.unlink(missing_ok=True)
                _end_mission(cfg, state, "stopped by you", say)
                return "Stopped."
    except KeyboardInterrupt:
        _end_mission(cfg, state, "stopped (Ctrl+C)", say)
        return "Stopped."
    except Exception as exc:  # noqa: BLE001 - run_mission never raises
        _save_state(cfg, state)
        return f"Crew crashed ({str(exc)[:150]}). State saved — rerun to resume."
