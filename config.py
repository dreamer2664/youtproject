"""Configuration loading with environment-variable overrides.

youtproject talks to NO YouTube/Google API, so there is no OAuth, no token
file, no quota and no audit. The only secret is the optional free Gemini key.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Friendly guard: config.py is the first project import everywhere, so a
# missing venv shows up here as a scary traceback. Say the fix instead.
_missing = []
for _mod in ("yaml", "requests", "edge_tts"):
    try:
        __import__(_mod)
    except ImportError:
        _missing.append(_mod)
if _missing:
    raise SystemExit(
        f"Missing Python packages ({', '.join(_missing)}). You probably "
        f"forgot to activate the venv — run: .\\venv\\Scripts\\Activate.ps1 "
        f"(Windows) or source venv/bin/activate (Linux/Mac), then retry. "
        f"First time ever? Run setup.ps1 (Windows) / setup.sh (Linux/Mac), "
        f"or: pip install -r requirements.txt"
    )
import yaml

# (width, height) per format. Portrait is what YouTube shelves as Shorts.
FORMATS: dict[str, tuple[int, int]] = {
    "landscape": (1920, 1080),
    "portrait": (1080, 1920),
}

# Art directions (video.style). images.STYLES holds the prompt text for each.
STYLES = ("photoreal", "cartoon", "stickman")

# Image providers for images.py. The chain is ai.image_provider plus
# ai.image_fallbacks; providers without their key are skipped, never fatal.
# (Hugging Face was removed Sep 2026: they retired serverless image
# inference — api-inference DNS is dead and the router answers "model
# deprecated / not supported" for the FLUX/SDXL lanes.)
IMAGE_PROVIDERS = ("pollinations", "gemini")

# Buffer autopost targets (autopost.py). Order in config = posting order.
BUFFER_SERVICES = ("youtube", "tiktok", "instagram")

DEFAULTS: dict[str, Any] = {
    "channel": {
        "topic": "unusual true stories from maritime history",
        "tone": "fast, punchy, high-energy",
        "audience": "scrolling TikTok/Shorts viewers with short attention spans",
        "target_seconds": 65,
        "voice": "en-GB-RyanNeural",
        # Narration speed for edge-tts: "+40%" is brisk TikTok pacing (~180 wpm),
        # "+0%" is normal, "-10%" is slow. Range -50%..+100%.
        "speech_rate": "+40%",
        # ElevenLabs premium voice (https://elevenlabs.io/app/settings/api-keys):
        # used instead of edge-tts when keys + voice_id are set, with word
        # timings from the with-timestamps endpoint. Any failure falls back
        # to edge-tts automatically. Free tier is ~10k chars/month (~11
        # Shorts). Env: ELEVENLABS_KEYS (space/comma-separated) wins.
        "elevenlabs_api_keys": [],
        # Voice ID from the ElevenLabs voice library (e.g. Sarah:
        # EXAVITQu4vr4xnSDxMaL). Empty = ElevenLabs stays off.
        "elevenlabs_voice_id": "",
        "elevenlabs_model": "eleven_turbo_v2_5",
        "language": "English",
        "category_id": "22",
        "default_tags": [],
    },
    "video": {
        # portrait = 1080x1920 vertical (Shorts) is the default;
        # landscape = 1920x1080 regular video.
        "format": "portrait",
        "fps": 30,
        "zoom": 1.18,
        # Reserved for future scene transitions. Scenes currently cut hard
        # (with a whoosh SFX); this value is clamped, never fatal, if mis-typed.
        "transition": 0.5,
        # Images per narrated scene. 3 is the default; each extra image
        # costs one more (free) image generation but cuts much denser.
        "images_per_scene": 3,
        # Art direction for every generated image. One flag flips the genre:
        #   photoreal = cinematic documentary look (default)
        #   cartoon   = flat 2D vector toon
        #   stickman  = whiteboard stick-figure explainer (viral TikTok look)
        # One-off override:  python main.py generate --style stickman
        "style": "photoreal",
        # Video encoder. "cpu" = libx264 veryfast (always works).
        # "auto" = fastest hardware encoder this FFmpeg offers (NVENC >
        # QuickSync > VideoToolbox), falling back to libx264; or force one:
        # "nvenc", "qsv", "videotoolbox". Garbage -> "cpu".
        "encoder": "cpu",
    },
    "subtitles": {
        # Word-timed subtitles, burned into the video. A captions.srt is also
        # written for manual upload in YouTube Studio either way.
        "enabled": True,
    },
    "disclosure": {
        # Appended to description.txt at package time (meta.json stays clean).
        # YouTube also asks you to tick the "altered content" box in Studio —
        # see the per-video CHECKLIST.md. Both is the correct answer.
        "append_to_description": True,
        "text": (
            "Disclosure: the narration and visuals in this video "
            "were generated with AI tools."
        ),
    },
    "ai": {
        "provider": "gemini",
        # Legacy single key (kept working); prefer the list below.
        "gemini_api_key": "",
        # Free Gemini keys (https://aistudio.google.com/apikey) — one per
        # Google account. Rejected/rate-limited keys rotate automatically,
        # so five keys is roughly 5x the free quota. Env: GEMINI_API_KEYS
        # (space/comma-separated) wins, else GEMINI_API_KEY (single).
        "gemini_api_keys": [],
        "gemini_model": "gemini-flash-latest",
        # Free Groq keys (https://console.groq.com/keys) — script/topic/
        # factcheck fallback after Gemini, with key rotation spreading the
        # free-tier quota. Env: GROQ_API_KEYS (comma-separated) wins, else
        # GROQ_API_KEY (single), else this list.
        "groq_api_keys": [],
        "groq_model": "qwen/qwen3.8-27b",
        # Editorial passes after factcheck: punch-up (retention) then
        # decringe (taste veto). Skipped automatically without an LLM key.
        "editorial_passes": True,
        # OpenRouter free-model overflow lane (https://openrouter.ai/keys) —
        # last LLM resort before the offline template. Env: OPENROUTER_KEYS
        # (space/comma-separated) wins, else OPENROUTER_API_KEY (single).
        "openrouter_api_keys": [],
        "openrouter_model": "nvidia/nemotron-3-super-120b-a12b:free",
        # Azure OpenAI on the $100 student credit (no card, credit can't
        # overrun — exhausted credit disables services, no bill can appear).
        # The agent only ever holds the endpoint key (tokens only, no VMs).
        # Spend is triple-guarded: worst-case pre-pricing, USD caps below,
        # and a JSONL ledger (`python main.py costs`). Unknown azure_model =
        # lane refused. Env: AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_KEY /
        # AZURE_OPENAI_DEPLOYMENT / AZURE_OPENAI_MODEL win over these.
        "azure_endpoint": "",
        "azure_api_key": "",
        "azure_deployment": "",
        "azure_model": "",
        "azure_api_version": "2024-08-01-preview",
        "azure_max_usd_per_day": 1.0,
        "azure_max_usd_per_month": 5.0,
        # Primary image provider + ordered fallbacks (IMAGE_PROVIDERS).
        # Fallbacks missing their key are skipped automatically, never fatal.
        "image_provider": "pollinations",
        "image_fallbacks": ["gemini"],
        # Seconds to wait per image before giving up.
        "image_timeout": 90,
        # Pollinations model. "flux" is free and unlimited (recommended);
        # "turbo" is faster but metered — anonymous turbo is throttled hard,
        # so use it only with a pollinations_token set.
        "image_model": "flux",
        # Optional free token from https://auth.pollinations.ai — raises the
        # anonymous ~1 req/15s limit to ~1 req/5s and removes the watermark.
        # Or export POLLINATIONS_TOKEN.
        "pollinations_token": "",
        # Parallel image downloads. The built-in pacer spaces request starts
        # (~1/15s anonymous Pollinations, ~1/5s with token), so extra workers
        # only overlap download time. Keep at 1.
        "image_workers": 1,
    },
    "buffer": {
        # Free key from https://publish.buffer.com/settings/api — enables
        # `python main.py autopost`. Or export BUFFER_API_KEY.
        "api_key": "",
        # Connected channels to post to (order = posting order).
        "channels": ["youtube", "tiktok"],
        # YouTube upload defaults.
        "youtube_privacy": "public",
        "youtube_category_id": "27",
        "made_for_kids": False,
        # Free Cloudinary upload (no card): cloud name + EITHER an unsigned
        # upload_preset (simplest, no secret in code) OR cloudinary_api_key +
        # cloudinary_api_secret (signed uploads). Preset wins when both set.
        # Env: CLOUDINARY_CLOUD_NAME / CLOUDINARY_UPLOAD_PRESET /
        #      CLOUDINARY_API_KEY / CLOUDINARY_API_SECRET.
        "cloud_name": "",
        "upload_preset": "",
        "cloudinary_api_key": "",
        "cloudinary_api_secret": "",
        # Chain package + autopost (as Buffer drafts) after every generate.
        # The only manual step left is reviewing drafts in Buffer.
        "autopost_after_generate": False,
    },
    "telegram": {
        # Phone control: text the bot a topic, get back a finished video.
        # Free. Your PC must be on with `python main.py bot` running.
        "enabled": False,
        # From @BotFather on Telegram (/newbot). Or export TELEGRAM_BOT_TOKEN.
        "bot_token": "",
        # Your numeric Telegram user id from @userinfobot. The bot ONLY
        # talks to this account — everyone else is ignored.
        "owner_id": 0,
    },
    "music": {
        # Ducked music bed. Source: music.file > first audio in music/ >
        # built-in royalty-free loop (cached in work/_fx).
        "enabled": True,
        "file": "",
        "folder": "music",
        # Bed loudness in dB. -24 is subtle, -18 is clearly present.
        "level_db": -24,
        # Dip the bed while the narrator speaks. Falls back to a static
        # bed if this FFmpeg lacks sidechaincompress.
        "duck": True,
    },
    "sfx": {
        # Airy whoosh on every scene cut + soft pop on the end-card CTA.
        "whoosh": True,
        "whoosh_db": -12,
        "cta_pop": True,
    },
    "progress_bar": {
        # Retention bar that fills as the video plays (gold, bottom edge).
        "enabled": True,
        "color": "0xFFD700",
        "height": 12,
        "position": "bottom",  # top | bottom
    },
    "cta": {
        # Spoken + on-screen call to action. One gentle ask by default —
        # add more lines to rotate comment-bait once you have an audience
        # that answers (counter in cta_state.json).
        "enabled": True,
        "lines": [
            "Follow for more.",
        ],
        "overlay_lines": [
            "FOLLOW FOR MORE!",
        ],
        "overlay_seconds": 3.5,
    },
    "topics": {
        # Self-refilling idea queue. The scheduler pops one topic per video;
        # `python main.py topics --topup` asks Gemini to refill to target.
        "backlog_file": "topics/backlog.txt",
        "backlog_target": 24,
    },
    "factcheck": {
        # Second Gemini pass over every script before the voiceover.
        # Needs the free Gemini key; without one it skips silently and
        # never blocks a render on failure.
        "enabled": True,
    },
    "youtube": {
        # YouTube Data API v3 keys (https://console.cloud.google.com/apis/
        # library/youtube.googleapis.com) — one Cloud project per key, each
        # with its own free 10k-units/day pool. Powers `python main.py yt`:
        # video/channel stats (1 unit) and Shorts niche search (100 units).
        # Env: YOUTUBE_KEYS (space/comma-separated) wins.
        "api_keys": [],
    },
    "crew": {
        # Autonomous missions: `python main.py crew --days 3 --per-day 4`
        # (Telegram /crew, /stop). Drafts unless --live.
        "cycle_minutes": 10,  # loop cadence; the stop file is checked more often
        "digest_hour": 21,  # local hour for the nightly Herald digest
        "premium_voices": 1,  # ElevenLabs videos/day, rest use edge-tts
        "max_searches": 3,  # YouTube niche searches/day (100 units each)
    },
    "jarvis": {
        # Channel manager: `python main.py jarvis "..."` + Telegram /jarvis.
        # Drafts by default — flip auto_schedule once you trust it.
        "auto_schedule": False,
        # Hard cap per task (render_videos refuses more, with a note).
        "max_videos": 5,
    },
    "schedule": {
        # Unattended daily rendering (`python main.py schedule`).
        "per_day": 2,
        # Fixed clock times, e.g. "08:00,20:00". Empty = evenly spaced.
        "at": "",
    },
    "sentry": {
        # Sentry DSN for crash reporting (https://sentry.io — student pack).
        # Empty = reporting off. Env: SENTRY_DSN wins. The DSN is semi-public
        # (it only accepts error events) but still belongs in config.yaml,
        # never in code — this repo is public.
        "dsn": "",
        "environment": "production",
    },
    "paths": {
        "work_dir": "work",
        "out_dir": "out",
        "package_dir": "upload",
        "state_file": "state.json",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Config:
    root: Path
    data: dict[str, Any]

    # -- channel ---------------------------------------------------------
    @property
    def topic(self) -> str:
        return self.data["channel"]["topic"]

    @property
    def tone(self) -> str:
        return self.data["channel"]["tone"]

    @property
    def audience(self) -> str:
        return self.data["channel"]["audience"]

    @property
    def target_seconds(self) -> int:
        """Target length. Garbage in -> 65 (never crashes)."""
        try:
            return max(5, min(3600, int(self.data["channel"]["target_seconds"])))
        except (ValueError, TypeError):
            return 65

    @property
    def voice(self) -> str:
        return self.data["channel"]["voice"]

    @property
    def speech_rate_pct(self) -> int:
        """Speech speed as an int percent. Garbage in -> 0 (never crashes)."""
        raw = str(self.data["channel"].get("speech_rate", "+0%")).strip()
        try:
            pct = int(raw.lstrip("+").rstrip("%").strip() or "0")
        except ValueError:
            pct = 0
        return max(-50, min(100, pct))

    @property
    def speech_rate(self) -> str:
        """edge-tts rate string, e.g. '+25%'. Always valid."""
        return f"{self.speech_rate_pct:+d}%"

    @property
    def speech_rate_factor(self) -> float:
        """1.25 for '+25%' — scales script word budgets and estimates."""
        return 1.0 + self.speech_rate_pct / 100.0

    @property
    def elevenlabs_api_keys(self) -> list[str]:
        """ElevenLabs keys; env ELEVENLABS_KEYS wins (never crash)."""
        env = (os.environ.get("ELEVENLABS_KEYS") or "").strip()
        if env:
            return [part for chunk in env.split(",") for part in
                    (p.strip() for p in chunk.split()) if part]
        raw = self.data["channel"].get("elevenlabs_api_keys") or []
        if isinstance(raw, str):
            raw = [raw]
        return [str(k).strip() for k in raw if str(k).strip()]

    @property
    def elevenlabs_voice_id(self) -> str:
        return str(self.data["channel"].get("elevenlabs_voice_id") or "").strip()

    @property
    def elevenlabs_model(self) -> str:
        return str(self.data["channel"].get("elevenlabs_model")
                   or "eleven_turbo_v2_5").strip()

    @property
    def youtube_api_keys(self) -> list[str]:
        """YouTube Data keys; env YOUTUBE_KEYS wins (never crash)."""
        env = (os.environ.get("YOUTUBE_KEYS") or "").strip()
        if env:
            return [part for chunk in env.split(",") for part in
                    (p.strip() for p in chunk.split()) if part]
        raw = self.data.get("youtube", {}).get("api_keys") or []
        if isinstance(raw, str):
            raw = [raw]
        return [str(k).strip() for k in raw if str(k).strip()]

    @property
    def crew_cycle_minutes(self) -> int:
        try:
            return max(2, min(120, int(self.data.get("crew", {}).get(
                "cycle_minutes", 10))))
        except (ValueError, TypeError):
            return 10

    @property
    def crew_digest_hour(self) -> int:
        try:
            return max(0, min(23, int(self.data.get("crew", {}).get(
                "digest_hour", 21))))
        except (ValueError, TypeError):
            return 21

    @property
    def crew_premium_voices(self) -> int:
        try:
            return max(0, min(10, int(self.data.get("crew", {}).get(
                "premium_voices", 1))))
        except (ValueError, TypeError):
            return 1

    @property
    def crew_max_searches(self) -> int:
        try:
            return max(0, min(20, int(self.data.get("crew", {}).get(
                "max_searches", 3))))
        except (ValueError, TypeError):
            return 3

    @property
    def language(self) -> str:
        return self.data["channel"]["language"]

    @property
    def category_id(self) -> str:
        return str(self.data["channel"]["category_id"])

    @property
    def default_tags(self) -> list[str]:
        return list(self.data["channel"].get("default_tags") or [])

    # -- video -----------------------------------------------------------
    @property
    def format(self) -> str:
        return str(self.data["video"].get("format", "landscape")).lower()

    @property
    def width(self) -> int:
        return FORMATS[self.format][0]

    @property
    def height(self) -> int:
        return FORMATS[self.format][1]

    @property
    def fps(self) -> int:
        """Frames per second. Garbage in -> 30 (never crashes)."""
        try:
            return max(1, min(60, int(self.data["video"]["fps"])))
        except (ValueError, TypeError):
            return 30

    @property
    def zoom(self) -> float:
        """Ken Burns zoom. Garbage in -> 1.18 (never crashes)."""
        try:
            return max(1.0, min(2.0, float(self.data["video"]["zoom"])))
        except (ValueError, TypeError):
            return 1.18

    @property
    def transition(self) -> float:
        """Reserved transition length. Garbage in -> 0.5 (never crashes)."""
        try:
            return max(0.0, min(5.0, float(self.data["video"]["transition"])))
        except (ValueError, TypeError):
            return 0.5

    @property
    def images_per_scene(self) -> int:
        try:
            return max(1, min(6, int(self.data["video"].get("images_per_scene", 3))))
        except (ValueError, TypeError):
            return 3

    @property
    def style(self) -> str:
        """Art direction; garbage in -> 'photoreal' (never crashes)."""
        name = str(self.data["video"].get("style", "photoreal")).lower()
        return name if name in STYLES else "photoreal"

    @property
    def encoder(self) -> str:
        """Video encoder; garbage in -> 'cpu' (never crashes)."""
        name = str(self.data["video"].get("encoder", "cpu")).lower()
        return name if name in ("cpu", "auto", "nvenc", "qsv", "videotoolbox") else "cpu"

    @property
    def thumb_width(self) -> int:
        return 1280 if self.format == "landscape" else 720

    @property
    def thumb_height(self) -> int:
        return 720 if self.format == "landscape" else 1280

    # -- music / sfx ---------------------------------------------------
    @property
    def music_enabled(self) -> bool:
        return bool(self.data.get("music", {}).get("enabled", True))

    @property
    def music_file(self) -> str:
        return str(self.data.get("music", {}).get("file") or "")

    @property
    def music_folder(self) -> str:
        return str(self.data.get("music", {}).get("folder") or "music")

    @property
    def music_level_db(self) -> int:
        try:
            return int(self.data.get("music", {}).get("level_db", -24))
        except (ValueError, TypeError):
            return -24

    @property
    def music_duck(self) -> bool:
        return bool(self.data.get("music", {}).get("duck", True))

    @property
    def sfx_whoosh(self) -> bool:
        return bool(self.data.get("sfx", {}).get("whoosh", True))

    @property
    def sfx_whoosh_db(self) -> int:
        try:
            return int(self.data.get("sfx", {}).get("whoosh_db", -12))
        except (ValueError, TypeError):
            return -12

    @property
    def sfx_cta_pop(self) -> bool:
        return bool(self.data.get("sfx", {}).get("cta_pop", True))

    # -- progress bar --------------------------------------------------
    @property
    def progress_enabled(self) -> bool:
        return bool(self.data.get("progress_bar", {}).get("enabled", True))

    @property
    def progress_color(self) -> str:
        return str(self.data.get("progress_bar", {}).get("color") or "0xFFD700")

    @property
    def progress_height(self) -> int:
        try:
            return max(4, min(40, int(self.data.get("progress_bar", {}).get("height", 12))))
        except (ValueError, TypeError):
            return 12

    @property
    def progress_position(self) -> str:
        pos = str(self.data.get("progress_bar", {}).get("position") or "bottom").lower()
        return "top" if pos == "top" else "bottom"

    # -- cta -----------------------------------------------------------
    @property
    def cta_enabled(self) -> bool:
        return bool(self.data.get("cta", {}).get("enabled", True))

    @property
    def cta_lines(self) -> list[str]:
        return [str(x) for x in (self.data.get("cta", {}).get("lines") or [])]

    @property
    def cta_overlay_lines(self) -> list[str]:
        return [str(x) for x in (self.data.get("cta", {}).get("overlay_lines") or [])]

    @property
    def cta_overlay_seconds(self) -> float:
        try:
            return max(1.0, min(10.0, float(self.data.get("cta", {}).get("overlay_seconds", 3.5))))
        except (ValueError, TypeError):
            return 3.5

    # -- topics ----------------------------------------------------------
    @property
    def topics_backlog_file(self) -> Path:
        return self.root / str(self.data.get("topics", {}).get("backlog_file") or "topics/backlog.txt")

    @property
    def topics_backlog_target(self) -> int:
        try:
            return max(1, min(100, int(self.data.get("topics", {}).get("backlog_target", 24))))
        except (ValueError, TypeError):
            return 24

    # -- factcheck -------------------------------------------------------
    @property
    def factcheck_enabled(self) -> bool:
        return bool(self.data.get("factcheck", {}).get("enabled", True))

    @property
    def jarvis_auto_schedule(self) -> bool:
        return bool(self.data.get("jarvis", {}).get("auto_schedule", False))

    @property
    def jarvis_max_videos(self) -> int:
        """Hard cap per Jarvis task. Garbage in -> 5 (never crashes)."""
        try:
            return max(1, min(20, int(self.data.get("jarvis", {}).get("max_videos", 5))))
        except (ValueError, TypeError):
            return 5

    # -- schedule --------------------------------------------------------
    @property
    def schedule_per_day(self) -> int:
        try:
            return max(1, min(24, int(self.data.get("schedule", {}).get("per_day", 2))))
        except (ValueError, TypeError):
            return 2

    @property
    def schedule_times(self) -> list[str]:
        import re
        out: list[str] = []
        raw = str(self.data.get("schedule", {}).get("at") or "")
        for piece in raw.split(","):
            match = re.fullmatch(r"\s*([01]?\d|2[0-3]):([0-5]\d)\s*", piece)
            if match:
                out.append(f"{int(match.group(1)):02d}:{match.group(2)}")
        return sorted(set(out))

    # -- telegram --------------------------------------------------------
    @property
    def telegram_enabled(self) -> bool:
        return bool(self.data.get("telegram", {}).get("enabled", False))

    @property
    def telegram_token(self) -> str:
        return (
            os.environ.get("TELEGRAM_BOT_TOKEN")
            or str(self.data.get("telegram", {}).get("bot_token") or "")
        ).strip()

    @property
    def telegram_owner(self) -> int:
        try:
            return int(str(self.data.get("telegram", {}).get("owner_id", 0)))
        except (ValueError, TypeError):
            return 0

    # -- sentry ----------------------------------------------------------
    @property
    def sentry_dsn(self) -> str:
        return (
            os.environ.get("SENTRY_DSN")
            or str(self.data.get("sentry", {}).get("dsn") or "")
        ).strip()

    @property
    def sentry_environment(self) -> str:
        return (
            os.environ.get("SENTRY_ENVIRONMENT")
            or str(self.data.get("sentry", {}).get("environment") or "production")
        ).strip() or "production"

    # -- subtitles -------------------------------------------------------
    @property
    def subtitles_enabled(self) -> bool:
        return bool(self.data["subtitles"].get("enabled", True))

    # -- disclosure ------------------------------------------------------
    @property
    def disclosure_enabled(self) -> bool:
        return bool(self.data["disclosure"].get("append_to_description", True))

    @property
    def disclosure_text(self) -> str:
        return str(self.data["disclosure"].get("text") or "").strip()

    # -- ai --------------------------------------------------------------
    @property
    def ai_provider(self) -> str:
        return str(self.data["ai"]["provider"]).lower()

    @property
    def gemini_api_keys(self) -> list[str]:
        """All configured Gemini keys; env wins over config.yaml (never crash)."""
        env = (os.environ.get("GEMINI_API_KEYS") or "").strip()
        if env:
            return [part for chunk in env.split(",") for part in
                    (p.strip() for p in chunk.split()) if part]
        raw = self.data["ai"].get("gemini_api_keys") or []
        if isinstance(raw, str):
            raw = [raw]
        keys = [str(k).strip() for k in raw if str(k).strip()]
        legacy = (os.environ.get("GEMINI_API_KEY")
                  or str(self.data["ai"].get("gemini_api_key") or "")).strip()
        if legacy and legacy not in keys:
            keys.append(legacy)
        return keys

    @property
    def gemini_api_key(self) -> str:
        """First Gemini key (legacy single-key callers)."""
        keys = self.gemini_api_keys
        return keys[0] if keys else ""

    @property
    def gemini_model(self) -> str:
        return str(self.data["ai"]["gemini_model"])

    @property
    def groq_api_keys(self) -> list[str]:
        """All configured Groq keys; env wins over config.yaml (never crash)."""
        env = (os.environ.get("GROQ_API_KEYS")
               or os.environ.get("GROQ_API_KEY") or "").strip()
        if env:
            return [key.strip() for key in env.split(",") if key.strip()]
        raw = self.data["ai"].get("groq_api_keys") or []
        if isinstance(raw, str):
            raw = [raw]
        return [str(key).strip() for key in raw if str(key).strip()]

    @property
    def groq_model(self) -> str:
        return str(self.data["ai"].get("groq_model") or "qwen/qwen3.8-27b")

    @property
    def editorial_enabled(self) -> bool:
        return bool(self.data["ai"].get("editorial_passes", True))

    @property
    def openrouter_api_keys(self) -> list[str]:
        """All configured OpenRouter keys; env wins over config.yaml."""
        env = (os.environ.get("OPENROUTER_KEYS")
               or os.environ.get("OPENROUTER_API_KEY") or "").strip()
        if env:
            return [key for key in env.replace(",", " ").split() if key]
        raw = self.data["ai"].get("openrouter_api_keys") or []
        if isinstance(raw, str):
            raw = [raw]
        return [str(key).strip() for key in raw if str(key).strip()]

    @property
    def openrouter_model(self) -> str:
        return str(self.data["ai"].get("openrouter_model")
                   or "nvidia/nemotron-3-super-120b-a12b:free")

    @property
    def image_width(self) -> int:
        # Generated images match the video orientation.
        return 1280 if self.format == "landscape" else 720

    @property
    def image_height(self) -> int:
        return 720 if self.format == "landscape" else 1280

    @property
    def image_timeout(self) -> int:
        """Seconds to wait per image. Garbage in -> 90 (never crashes)."""
        try:
            return max(10, min(600, int(self.data["ai"]["image_timeout"])))
        except (ValueError, TypeError):
            return 90

    @property
    def azure_endpoint(self) -> str:
        return (
            os.environ.get("AZURE_OPENAI_ENDPOINT")
            or str(self.data["ai"].get("azure_endpoint") or "")
        ).strip()

    @property
    def azure_api_key(self) -> str:
        return (
            os.environ.get("AZURE_OPENAI_KEY")
            or str(self.data["ai"].get("azure_api_key") or "")
        ).strip()

    @property
    def azure_deployment(self) -> str:
        return (
            os.environ.get("AZURE_OPENAI_DEPLOYMENT")
            or str(self.data["ai"].get("azure_deployment") or "")
        ).strip()

    @property
    def azure_model(self) -> str:
        return (
            os.environ.get("AZURE_OPENAI_MODEL")
            or str(self.data["ai"].get("azure_model") or "")
        ).strip()

    @property
    def azure_api_version(self) -> str:
        return str(
            self.data["ai"].get("azure_api_version")
            or "2024-08-01-preview").strip()

    @property
    def azure_max_usd_per_day(self) -> float:
        try:
            value = float(self.data["ai"].get("azure_max_usd_per_day", 1.0))
        except (TypeError, ValueError):
            return 1.0
        return value if value > 0 else 1.0

    @property
    def azure_max_usd_per_month(self) -> float:
        try:
            value = float(self.data["ai"].get("azure_max_usd_per_month", 5.0))
        except (TypeError, ValueError):
            return 5.0
        return value if value > 0 else 5.0

    @property
    def image_provider(self) -> str:
        """Primary image provider; garbage in -> 'pollinations', never crash."""
        name = str(self.data["ai"].get("image_provider", "pollinations")).lower()
        return name if name in IMAGE_PROVIDERS else "pollinations"

    @property
    def image_fallbacks(self) -> list[str]:
        """Ordered fallback providers; junk names, dupes, primary dropped."""
        raw = self.data["ai"].get("image_fallbacks") or []
        if isinstance(raw, str):
            raw = [raw]
        out: list[str] = []
        for name in raw:
            clean = str(name).lower()
            if (clean in IMAGE_PROVIDERS and clean != self.image_provider
                    and clean not in out):
                out.append(clean)
        return out

    @property
    def image_model(self) -> str:
        return str(self.data["ai"].get("image_model") or "flux")

    @property
    def pollinations_token(self) -> str:
        return (os.environ.get("POLLINATIONS_TOKEN")
                or str(self.data["ai"].get("pollinations_token") or "")).strip()

    @property
    def buffer_api_key(self) -> str:
        return (os.environ.get("BUFFER_API_KEY")
                or str(self.data["buffer"].get("api_key") or "")).strip()

    @property
    def buffer_channels(self) -> list[str]:
        """Wanted autopost services; junk dropped, empty -> default pair."""
        raw = self.data["buffer"].get("channels") or []
        if isinstance(raw, str):
            raw = [raw]
        out = [str(name).lower() for name in raw if str(name).lower() in BUFFER_SERVICES]
        return out or ["youtube", "tiktok"]

    @property
    def youtube_privacy(self) -> str:
        value = str(self.data["buffer"].get("youtube_privacy", "public")).lower()
        return value if value in ("public", "unlisted", "private") else "public"

    @property
    def youtube_category_id(self) -> str:
        return str(self.data["buffer"].get("youtube_category_id") or "27")

    @property
    def buffer_made_for_kids(self) -> bool:
        return bool(self.data["buffer"].get("made_for_kids", False))

    @property
    def autopost_after_generate(self) -> bool:
        return bool(self.data["buffer"].get("autopost_after_generate", False))

    @property
    def cloudinary_cloud_name(self) -> str:
        return (os.environ.get("CLOUDINARY_CLOUD_NAME")
                or str(self.data["buffer"].get("cloud_name") or "")).strip()

    @property
    def cloudinary_preset(self) -> str:
        return (os.environ.get("CLOUDINARY_UPLOAD_PRESET")
                or str(self.data["buffer"].get("upload_preset") or "")).strip()

    @property
    def cloudinary_api_key(self) -> str:
        return (os.environ.get("CLOUDINARY_API_KEY")
                or str(self.data["buffer"].get("cloudinary_api_key") or "")).strip()

    @property
    def cloudinary_api_secret(self) -> str:
        return (os.environ.get("CLOUDINARY_API_SECRET")
                or str(self.data["buffer"].get("cloudinary_api_secret") or "")).strip()

    @property
    def image_workers(self) -> int:
        try:
            return max(1, min(6, int(self.data["ai"].get("image_workers", 1))))
        except (ValueError, TypeError):
            return 1

    # -- paths -----------------------------------------------------------
    @property
    def work_dir(self) -> Path:
        return self.root / self.data["paths"]["work_dir"]

    @property
    def out_dir(self) -> Path:
        return self.root / self.data["paths"]["out_dir"]

    @property
    def package_dir(self) -> Path:
        return self.root / self.data["paths"]["package_dir"]

    @property
    def state_file(self) -> Path:
        return self.root / self.data["paths"]["state_file"]

    def ensure_dirs(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.package_dir.mkdir(parents=True, exist_ok=True)


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load config.yaml, falling back to bundled defaults."""
    root = Path(path).resolve().parent if path else Path.cwd().resolve()
    cfg_path = Path(path) if path else root / "config.yaml"

    # Deep copies throughout: Config instances must never share (or mutate)
    # the module-level DEFAULTS, or one run's CLI overrides would leak into
    # the next — especially in long-lived processes (schedule/bot).
    data = copy.deepcopy(DEFAULTS)
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as handle:
                user = yaml.safe_load(handle) or {}
        except yaml.YAMLError as exc:
            raise ValueError(
                f"{cfg_path.name} has a syntax error: {exc}\n"
                f"Usually a missing quote or wrong indentation — "
                f"compare the flagged line with config.example.yaml."
            ) from exc
        data = _deep_merge(copy.deepcopy(DEFAULTS), user)

    fmt = str(data["video"].get("format", "landscape")).lower()
    if fmt not in FORMATS:
        raise ValueError(
            f"video.format is {fmt!r} — must be one of: {', '.join(FORMATS)}. "
            f"Fix it in {cfg_path}."
        )

    cfg = Config(root=root, data=data)
    cfg.ensure_dirs()
    return cfg
