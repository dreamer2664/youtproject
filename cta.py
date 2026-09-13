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


def next_cta(cfg: Config) -> tuple[str | None, str | None, int]:
    """Return (voice_line, overlay_text, number), or (None, None, 0) if off.

    `number` is 1-based: the nth video to carry a CTA. The counter only
    advances when a line is actually handed out.
    """
    lines = cfg.cta_lines
    if not cfg.cta_enabled or not lines:
        return None, None, 0
    index = 0
    try:
        index = int(json.loads(_state_path(cfg).read_text(encoding="utf-8")).get("index", 0))
    except (OSError, ValueError, AttributeError):
        index = 0
    overlays = cfg.cta_overlay_lines or [DEFAULT_OVERLAY]
    voice = lines[index % len(lines)]
    overlay = overlays[index % len(overlays)]
    try:
        _state_path(cfg).write_text(json.dumps({"index": index + 1}), encoding="utf-8")
    except OSError:
        pass
    return voice, overlay, index + 1
