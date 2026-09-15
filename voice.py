"""Voice notes in, text out. Telegram file download + Groq Whisper.

Verified live: whisper-large-v3-turbo transcribed a 5 s clip word-perfect
("Testing 1, 2, 3, make a video about black holes."). No new dependencies
(requests only — the bot sends voice notes here, CLI doesn't need them).
"""

from __future__ import annotations

from pathlib import Path

import requests

from config import Config

WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-large-v3-turbo"


def download_telegram_voice(bot_token: str, file_id: str, dest_dir: Path) -> Path:
    """getFile + download. Returns the local .ogg path."""
    info = requests.get(f"https://api.telegram.org/bot{bot_token}/getFile",
                        params={"file_id": file_id}, timeout=30).json()
    if not info.get("ok"):
        raise RuntimeError(f"Telegram getFile failed: {str(info)[:150]}")
    remote = info["result"]["file_path"]
    dest = Path(dest_dir) / f"voice_{file_id[:16]}.ogg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(f"https://api.telegram.org/file/bot{bot_token}/{remote}",
                      timeout=120, stream=True) as response:
        response.raise_for_status()
        with open(dest, "wb") as handle:
            for chunk in response.iter_content(65536):
                handle.write(chunk)
    return dest


def transcribe(cfg: Config, path: Path) -> str:
    """Whisper via Groq keys in order (429/401/403 -> next key)."""
    keys = cfg.groq_api_keys
    if not keys:
        raise RuntimeError("no Groq API keys")
    last = "no keys tried"
    for key in keys:
        try:
            with open(path, "rb") as handle:
                response = requests.post(
                    WHISPER_URL,
                    headers={"Authorization": f"Bearer {key}"},
                    files={"file": (Path(path).name, handle, "audio/ogg")},
                    data={"model": WHISPER_MODEL},
                    timeout=120,
                )
        except Exception as exc:
            last = str(exc)[:120]
            continue
        if response.status_code == 200:
            try:
                return (response.json().get("text") or "").strip()
            except ValueError:
                return ""
        last = f"HTTP {response.status_code}: {response.text[:120]}"
        if response.status_code not in (429, 401, 403):
            break
    raise RuntimeError(f"transcription failed ({last})")
