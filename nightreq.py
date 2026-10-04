"""The /go request: what the phone asked the PC to do next time it's up.

The bot writes a request when it sees `/go`. The request survives the PC
being powered off — Telegram holds the message, this file holds the
*job* — and is only marked done when a night batch has actually run for
it. That is what makes the phone flow work without the PC: you message
before sleeping, the PC wakes (or boots) whenever it does, and the work
happens then.

One file, tiny, atomic writes. Never raises: a corrupt request is
treated as absent (worst case: you send /go again).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

DEFAULT_OPTIONS = {
    "clips": 3,      # sheet sources to clip (each source -> up to 10 clips)
    "count": 0,      # videos to generate from the backlog
    "meeting": True,   # boardroom stats discussion first
    "review": True,    # boardroom ranks the finished clips afterwards
    "top": 5,        # how many clips the room is asked to pick
}


def request_path(cfg) -> Path:
    return Path(cfg.work_dir) / "nightrun" / "request.json"


def normalize_options(options: dict | None) -> dict:
    """Bounded, predictable options (pure, tested). Bad values go default."""
    src = dict(DEFAULT_OPTIONS)
    for key, value in (options or {}).items():
        if key in ("clips", "count", "top"):
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            caps = {"clips": (0, 6), "count": (0, 10), "top": (1, 20)}
            low, high = caps[key]
            src[key] = max(low, min(high, number))
        elif key in ("meeting", "review"):
            src[key] = bool(value)
    return src


def write_request(cfg, options: dict | None = None,
                  source: str = "bot") -> dict:
    """Queue a request (status=pending). Returns the record written."""
    record = {
        "status": "pending",
        "source": source,
        "created": datetime.now().isoformat(timespec="seconds"),
        "options": normalize_options(options),
    }
    path = request_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # the bot still replies; the CLI prints its own warning
    return record


def read_request(cfg) -> dict | None:
    try:
        data = json.loads(request_path(cfg).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - absent/corrupt both mean "none"
        return None
    return data if isinstance(data, dict) else None


def pending(cfg) -> bool:
    data = read_request(cfg)
    return bool(data and data.get("status") == "pending")


def mark_done(cfg, failed: int = 0, note: str = "") -> None:
    """Finish the current request. Failed steps still finish it — the
    report says what failed; auto-retrying forever is worse than telling
    the owner (send /go again to retry)."""
    data = read_request(cfg) or {}
    data.update({
        "status": "done",
        "finished": datetime.now().isoformat(timespec="seconds"),
        "failed_steps": int(failed),
        "note": note[:300],
    })
    path = request_path(cfg)
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def status_line(cfg) -> str:
    """One line for /go status (pure-ish, never raises)."""
    data = read_request(cfg)
    if not data:
        return "No /go request yet. Send /go to queue tonight's run."
    if data.get("status") == "pending":
        return (f"🕐 /go pending since {data.get('created', '?')} — it runs "
                "as soon as the PC is awake (wake task or next boot).")
    failed = int(data.get("failed_steps") or 0)
    tail = f" ({failed} step(s) failed — see the report)" if failed else ""
    return (f"✅ Last /go finished {data.get('finished', '?')}{tail}. "
            "Send /go again to run another batch.")


def clear(cfg) -> None:
    try:
        request_path(cfg).unlink()
    except OSError:
        pass
