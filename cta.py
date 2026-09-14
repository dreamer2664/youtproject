"""Rotating end-of-video call to action (comment bait).

Each video gets the next line from cta.lines, spoken by the narrator and
shown as an end-card overlay. The counter lives in cta_state.json next to
state.json, so rotation survives restarts and works across generate/batch/bot.
"""

from __future__ import annotations

import json
from pathlib import Path

from config import Config

STATE_NAME = "cta_state.json"
DEFAULT_OVERLAY = "COMMENT BELOW!"


def _state_path(cfg: Config) -> Path:
    return cfg.state_file.parent / STATE_NAME


def _read_index(cfg: Config) -> int:
    try:
        return int(json.loads(_state_path(cfg).read_text(encoding="utf-8")).get("index", 0))
    except (OSError, ValueError, AttributeError):
        return 0


def _write_index(cfg: Config, index: int) -> None:
    try:
        _state_path(cfg).write_text(json.dumps({"index": index}), encoding="utf-8")
    except OSError:
        pass


def next_cta(cfg: Config, commit: bool = True) -> tuple[str | None, str | None, int]:
    """Return (voice_line, overlay_text, number), or (None, None, 0) if off.

    `number` is 1-based: the nth video to carry a CTA. With commit=False the
    counter is left alone — call commit_cta() once the video that carries
    the line actually renders, so a failed run does not eat a CTA line.
    """
    lines = cfg.cta_lines
    if not cfg.cta_enabled or not lines:
        return None, None, 0
    index = _read_index(cfg)
    overlays = cfg.cta_overlay_lines or [DEFAULT_OVERLAY]
    voice = lines[index % len(lines)]
    overlay = overlays[index % len(overlays)]
    if commit:
        _write_index(cfg, index + 1)
    return voice, overlay, index + 1


def commit_cta(cfg: Config) -> None:
    """Advance the rotation counter (call after a CTA-carrying render succeeds)."""
    if not cfg.cta_enabled or not cfg.cta_lines:
        return
    _write_index(cfg, _read_index(cfg) + 1)
