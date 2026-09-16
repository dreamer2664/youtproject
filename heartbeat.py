"""Honeybadger dead-man's switch: pings while the pipeline is alive.

Sentry reports crashes; this reports SILENCE. The crew loop pings every
cycle and every finished video pings once; if Honeybadger hears nothing for
the check's report period + grace, it emails you that the pipeline died.

Between missions, pause the check in the Honeybadger dashboard (one click) —
the mission start/end messages remind you, so a quiet PC never false-alarms.

Only the check ID (or full ping URL) lives in config — pinging is send-only,
so no API token is needed at runtime. Unconfigured = silent no-op, and
pinging never raises, so a dead monitoring service can never kill a render.
"""

from __future__ import annotations

import requests

PING_URL = "https://api.honeybadger.io/v1/check_in/{check_id}"


def check_id_from(cfg) -> str:
    """Bare check ID from config (accepts a full ping URL too)."""
    raw = (cfg.honeybadger_check or "").strip().rstrip("/")
    if "/check_in/" in raw:
        raw = raw.split("/check_in/", 1)[1]
    return raw.strip("/")


def ping(cfg) -> bool:
    """Ping the check-in. Never raises. True when Honeybadger heard us."""
    try:
        check_id = check_id_from(cfg)
    except Exception:  # noqa: BLE001 - monitoring must never break a run
        return False
    if not check_id:
        return False
    try:
        response = requests.get(PING_URL.format(check_id=check_id), timeout=10)
        return 200 <= response.status_code < 300
    except Exception:  # noqa: BLE001 - same as above
        return False
