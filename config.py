"""Configuration loading with environment-variable overrides.

youtproject talks to NO YouTube/Google API, so there is no OAuth, no token
file, no quota and no audit. The only secret is the optional free Gemini key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# (width, height) per format. Portrait is what YouTube shelves as Shorts.
FORMATS: dict[str, tuple[int, int]] = {
    "landscape": (1920, 1080),
    "portrait": (1080, 1920),
}

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
        "transition": 0.5,
        # Images per narrated scene. 3 is the default; each extra image
        # costs one more (free) image generation but cuts much denser.
        "images_per_scene": 3,
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
        "gemini_api_key": "",
        "gemini_model": "gemini-flash-latest",
        "image_provider": "pollinations",
        # Seconds to wait per image before giving up.
        "image_timeout": 90,
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
        return int(self.data["channel"]["target_seconds"])

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
        return int(self.data["video"]["fps"])

    @property
    def zoom(self) -> float:
        return float(self.data["video"]["zoom"])

    @property
    def transition(self) -> float:
        return float(self.data["video"]["transition"])

    @property
    def images_per_scene(self) -> int:
        return max(1, min(6, int(self.data["video"].get("images_per_scene", 2))))

    @property
    def thumb_width(self) -> int:
        return 1280 if self.format == "landscape" else 720

    @property
    def thumb_height(self) -> int:
        return 720 if self.format == "landscape" else 1280

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
    def gemini_api_key(self) -> str:
        return (
            os.environ.get("GEMINI_API_KEY")
            or str(self.data["ai"].get("gemini_api_key") or "")
        ).strip()

    @property
    def gemini_model(self) -> str:
        return str(self.data["ai"]["gemini_model"])

    @property
    def image_width(self) -> int:
        # Generated images match the video orientation.
        return 1280 if self.format == "landscape" else 720

    @property
    def image_height(self) -> int:
        return 720 if self.format == "landscape" else 1280

    @property
    def image_timeout(self) -> int:
        return int(self.data["ai"]["image_timeout"])

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

    data = DEFAULTS
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
        data = _deep_merge(DEFAULTS, user)

    fmt = str(data["video"].get("format", "landscape")).lower()
    if fmt not in FORMATS:
        raise ValueError(
            f"video.format is {fmt!r} — must be one of: {', '.join(FORMATS)}. "
            f"Fix it in {cfg_path}."
        )

    cfg = Config(root=root, data=data)
    cfg.ensure_dirs()
    return cfg
