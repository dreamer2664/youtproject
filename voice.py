"""Voice notes in, text out. Telegram file download + Groq Whisper.

Verified live: whisper-large-v3-turbo transcribed a 5 s clip word-perfect
("Testing 1, 2, 3, make a video about black holes."). No new dependencies
(requests only — the bot sends voice notes here, CLI doesn't need them).
"""

from __future__ import annotations

from pathlib import Path

import requests

import keypool
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
    """Whisper via Groq keys in order (429/401/403 -> next key).

    Ledgered like the clip lane's transcription: Groq Whisper audio-minutes
    are the stack's binding constraint (org-level ~8 h/day pool), so a
    voice note spent off the books would make `keys` / `keys --month`
    under-report the one number that actually gates the day. The audio
    length comes from the local file (ffprobe when available, 0 otherwise —
    bookkeeping must never fail a transcription).
    """
    keys = keypool.live("groq", cfg.groq_api_keys)
    if not keys:
        raise RuntimeError("no Groq API keys")
    import keystats

    seconds = _audio_seconds(path)
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
        # The request reached Groq: count it exactly once per key, before
        # any parsing that could raise and skip the bookkeeping.
        keystats.bump("groq", key, req=1, audio=max(0, int(seconds)),
                      tag="voicenote")
        if response.status_code == 200:
            try:
                return (response.json().get("text") or "").strip()
            except ValueError:
                return ""
        last = f"HTTP {response.status_code}: {response.text[:120]}"
        if response.status_code in (401, 403):
            keypool.dead("groq", key)  # revoked: skip it in every lane
        if response.status_code not in (429, 401, 403):
            break
    raise RuntimeError(f"transcription failed ({last})")


def _audio_seconds(path: Path) -> float:
    """Duration of a local audio file via ffprobe; 0.0 when unknown.

    Never raises: the ledger is best-effort by design (keystats.bump is a
    no-op on failure), and a missing ffprobe must not break a voice note.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30)
        return max(0.0, float((out.stdout or "0").strip() or 0.0))
    except Exception:  # noqa: BLE001 - ffprobe missing / unreadable file
        return 0.0
