"""Redact credentials from user-facing errors and persisted diagnostics."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from typing import Any

_REDACTED = "[REDACTED]"
_SECRET_NAME = re.compile(r"(?:api[-_]?keys?|access[-_]?tokens?|tokens?|secrets?|passwords?|credentials?|pats?)$", re.I)
_PATTERNS = (
    # Bearer credentials, even when embedded in an HTTP exception.
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}={0,2}"),
    # Telegram's bot URL embeds the bot token in the path.
    re.compile(r"(?i)(/bot)[0-9]{6,}:[A-Za-z0-9_-]{20,}"),
    # Common provider key formats.
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bgsk_[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}\b"),
    # Sensitive query parameters and key=value / JSON-style diagnostics.
    re.compile(
        r"(?i)([?&](?:api[-_]?key|key|access[-_]?token|token|client[-_]?secret|secret|password)=)"
        r"([^&#\s\"'<>]+)"
    ),
    re.compile(
        r"(?i)([\"']?(?:api[-_]?key|key|access[-_]?token|token|client[-_]?secret|secret|password)"
        r"[\"']?\s*[:=]\s*[\"']?)([^\s,;&\"'<>]+)"
    ),
    # URL basic-auth credentials.
    re.compile(r"(?i)(https?://)([^:/@\s]+):([^/@\s]+)@"),
)


def configured_secrets(config: Any = None, extra: Iterable[str] = ()) -> list[str]:
    """Collect secret-like values from config/env without exposing them.

    This is best-effort redaction only; the structured patterns below also
    handle common credential formats when an exception omits config context.
    """
    found: set[str] = {str(value) for value in extra if value and len(str(value)) >= 8}

    def visit(value: Any, name: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, str(key))
        elif isinstance(value, (list, tuple, set)):
            for child in value:
                visit(child, name)
        elif isinstance(value, str) and _SECRET_NAME.search(name) and len(value) >= 8:
            found.add(value)

    if config is not None:
        data = getattr(config, "data", None)
        if isinstance(data, dict):
            visit(data)
        attrs = getattr(config, "__dict__", None)
        if isinstance(attrs, dict):
            visit(attrs)
        # A few credentials are provided by environment-backed properties,
        # rather than appearing in config.data.
        for name in (
            "gemini_api_keys", "groq_api_keys", "openrouter_api_keys",
            "youtube_api_keys", "buffer_api_key", "cloudinary_api_secret",
            "telegram_bot_token", "elevenlabs_api_keys", "pexels_api_keys",
            "pixabay_api_keys",
        ):
            try:
                visit(getattr(config, name, None), name)
            except Exception:  # noqa: BLE001 - redaction must never break a command
                continue

    for name, value in os.environ.items():
        if _SECRET_NAME.search(name) and value and len(value) >= 8:
            found.add(value)
    return sorted(found, key=len, reverse=True)


def redact_error(message: Any, secrets: Iterable[str] = ()) -> str:
    """Return a readable error with known and recognizable credentials masked."""
    text = str(message or "")
    values = sorted({str(value) for value in secrets if value},
                    key=len, reverse=True)
    for value in values:
        text = text.replace(value, _REDACTED)
    for pattern in _PATTERNS:
        if pattern.groups >= 2 and pattern.pattern.startswith("(?i)(https?"):
            text = pattern.sub(r"\1[REDACTED]@", text)
        elif pattern.groups >= 2:
            text = pattern.sub(r"\1" + _REDACTED, text)
        else:
            text = pattern.sub(_REDACTED, text)
    return text
