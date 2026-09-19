"""API key usage ledger + quota dashboard (`python main.py keys`).

Free APIs don't expose "tokens remaining" — so this module counts what WE
spend: every provider call site bumps the ledger (work/usage_ledger.json),
and the dashboard renders per-key usage against each provider's free-tier
limit with refill times (Gemini/YouTube reset midnight US Pacific, Groq/
OpenRouter midnight UTC, Pexels hourly, ElevenLabs monthly).

Design rules:
  * bump() NEVER raises and never slows a call — bookkeeping is optional.
  * Keys are stored MASKED (last 4 chars); full key material never lands
    on disk here.
  * Limits are documented constants (see CAPACITY.md for sources), not
    live API reads — display them as "~" approximations.
  * The ledger is self-counted: it says what this machine spent, which is
    the number that matters for planning renders.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ORDER = ["gemini", "groq", "openrouter", "elevenlabs", "pexels",
         "youtube", "pollinations"]

# Free-tier limits, verified Sep 2026 (sources in CAPACITY.md).
LIMITS = {
    "gemini": {
        "title": "GEMINI",
        "day": 1000, "unit": "requests",
        "rule": "~1,000 requests/day per key · resets midnight US Pacific",
        "tz": "pac"},
    "groq": {
        "title": "GROQ",
        "day": 14400, "unit": "requests", "shared": True,
        "rule": "~14,400 requests/day, ALL KEYS ONE POOL · resets midnight UTC",
        "tz": "utc"},
    "openrouter": {
        "title": "OPENROUTER",
        "day": 50, "unit": "requests", "shared": True,
        "rule": "50 requests/day on :free models (20/minute) · resets midnight UTC",
        "tz": "utc"},
    "elevenlabs": {
        "title": "ELEVENLABS",
        "month": 10000, "unit": "characters",
        "rule": "~10,000 characters/month per key (free tier resets on your signup day, not the 1st)"},
    "pexels": {
        "title": "PEXELS",
        "hour": 200, "month": 20000, "unit": "requests",
        "rule": "200 requests/hour + 20,000/month per key"},
    "youtube": {
        "title": "YOUTUBE DATA API",
        "day": 10000, "unit": "units",
        "rule": "10,000 quota units/day per key · resets midnight US Pacific",
        "tz": "pac"},
    "pollinations": {
        "title": "POLLINATIONS",
        "unit": "requests",
        "rule": "anonymous, no hard daily cap (paced ~1 request/15s)"},
}

LEDGER_NAME = "usage_ledger.json"
KEEP_DAYS = 35
_path: Path | None = None


def init(cfg) -> None:
    """Point the ledger at this config's work dir (called once by main)."""
    global _path
    try:
        _path = Path(cfg.work_dir) / LEDGER_NAME
    except Exception:  # noqa: BLE001 - never break a command for this
        _path = None


def _mask(key: str) -> str:
    key = (key or "").strip()
    if not key or key == "anonymous":
        return "anonymous"
    return "..." + key[-4:]


def _load(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001 - unreadable ledger = empty ledger
        return []


def _write(path: Path, events: list[dict]) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    kept = []
    for event in events:
        try:
            when = datetime.fromisoformat(str(event.get("t")))
        except ValueError:
            kept.append(event)
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            kept.append(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(kept), encoding="utf-8")


def bump(provider: str, key: str, req: int = 0, tok: int = 0,
         chars: int = 0, units: int = 0) -> None:
    """Record one provider call. Never raises; no-op until init()."""
    if _path is None or not (req or tok or chars or units):
        return
    event = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "p": provider, "k": _mask(key), "req": int(req),
             "tok": int(tok), "chars": int(chars), "units": int(units)}
    try:
        events = _load(_path)
        events.append(event)
        _write(_path, events)
    except Exception:  # noqa: BLE001 - bookkeeping must never break a call
        pass


def window_sum(events: list[dict], provider: str,
               since: datetime) -> dict[str, dict]:
    """Per-key sums for one provider since a cutoff (pure, tested)."""
    sums: dict[str, dict] = {}
    for event in events:
        if event.get("p") != provider:
            continue
        try:
            when = datetime.fromisoformat(str(event.get("t")))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when < since:
            continue
        key = str(event.get("k") or "anonymous")
        row = sums.setdefault(key, {"req": 0, "tok": 0, "chars": 0, "units": 0})
        for field in ("req", "tok", "chars", "units"):
            row[field] += int(event.get(field) or 0)
    return sums


def _zone_pacific():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/Los_Angeles")
    except Exception:  # noqa: BLE001 - Windows without tzdata: rough offset
        return timezone(timedelta(hours=-8), "US-Pacific(approx)")


def _window(provider: str, now: datetime) -> tuple[datetime, datetime, str]:
    """(window start, next reset, reset label) per provider reset rule."""
    spec = LIMITS[provider]
    if spec.get("tz") == "pac":
        pac = _zone_pacific()
        local = now.astimezone(pac)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        reset = start + timedelta(days=1)
        return (start.astimezone(timezone.utc), reset,
                reset.astimezone().strftime("%H:%M"))
    if provider == "elevenlabs":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if now.month == 12:
            reset = start.replace(year=start.year + 1, month=1)
        else:
            reset = start.replace(month=start.month + 1)
        return (start, reset, reset.strftime("%d %b"))
    if provider == "pexels":
        start = now.replace(minute=0, second=0, microsecond=0)
        reset = start + timedelta(hours=1)
        return (start, reset, reset.strftime("%H:%M"))
    # groq / openrouter / pollinations: calendar day, UTC
    start = (now.astimezone(timezone.utc)
             ).replace(hour=0, minute=0, second=0, microsecond=0)
    reset = start + timedelta(days=1)
    return (start, reset, reset.astimezone().strftime("%H:%M"))


def _configured_keys(cfg, provider: str) -> list[str]:
    try:
        if provider == "gemini":
            return list(cfg.gemini_api_keys)
        if provider == "groq":
            return list(cfg.groq_api_keys)
        if provider == "openrouter":
            return list(cfg.openrouter_api_keys)
        if provider == "elevenlabs":
            return list(cfg.elevenlabs_api_keys)
        if provider == "pexels":
            return [cfg.pexels_api_key] if cfg.pexels_api_key else []
        if provider == "youtube":
            return list(cfg.youtube_api_keys)
        if provider == "pollinations":
            return ["anonymous"]
    except Exception:  # noqa: BLE001 - show ledger data even without cfg
        pass
    return []


def _fmt_delta(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins:02d}m" if hours else f"{mins}m"


def _fmt_num(value: int) -> str:
    return f"{value:,}"


def build_status(cfg, now: datetime | None = None) -> str:
    """The dashboard text (pure-ish: reads the ledger file + config keys)."""
    now = now or datetime.now(timezone.utc)
    events = _load(_path) if _path else []
    lines = [f"API keys & quotas — {now.astimezone().strftime('%a %d %b %Y, %H:%M')}",
             "(self-counted: what this machine spent; providers don't expose "
             "remaining quota)", ""]
    for provider in ORDER:
        spec = LIMITS[provider]
        keys = [k for k in _configured_keys(cfg, provider) if k]
        masks = [_mask(k) for k in keys]
        day = window_sum(events, provider, _window(provider, now)[0])
        if provider == "pexels":
            hour = window_sum(events, provider,
                              now.replace(minute=0, second=0, microsecond=0))
        for mask in day:  # ledger-only keys (rotated out of config) still show
            if mask not in masks:
                masks.append(mask)
        if not masks:
            continue
        lines.append(f"{spec['title']}  ·  {spec['rule']}")
        for mask in masks:
            row = day.get(mask) or {"req": 0, "tok": 0, "chars": 0, "units": 0}
            if provider == "elevenlabs":
                used, limit = row["chars"], spec["month"]
                left = max(0, limit - used)
                _, reset, label = _window(provider, now)
                lines.append(
                    f"  {mask:<12} {_fmt_num(used)} chars this month   "
                    f"{_fmt_num(left)} left   resets ~{label}")
            elif provider == "pexels":
                hour_row = (hour.get(mask) or {"req": 0})["req"]
                hour_left = max(0, spec["hour"] - hour_row)
                month_used = row["req"]
                month_left = max(0, spec["month"] - month_used)
                _, reset, label = _window(provider, now)
                lines.append(
                    f"  {mask:<12} {hour_row} this hour "
                    f"({_fmt_num(hour_left)} left) · "
                    f"{_fmt_num(month_used)} this month "
                    f"({_fmt_num(month_left)} left) · "
                    f"hour resets {label}")
            elif provider == "pollinations":
                lines.append(f"  {mask:<12} {row['req']} requests today")
            else:
                unit = "units" if provider == "youtube" else "requests"
                used, limit = (row["units"] if provider == "youtube"
                               else row["req"]), spec["day"]
                _, reset, label = _window(provider, now)
                lines.append(
                    f"  {mask:<12} {used} {unit} today   "
                    f"{_fmt_num(max(0, limit - used))} left   "
                    f"resets {label} (in {_fmt_delta(reset - now)})")
                if row["tok"]:
                    tok = row["tok"]
                    tok_txt = (f"~{tok // 1000}k" if tok >= 1000
                               else f"~{tok}")
                    lines[-1] += f"   {tok_txt} tok est"
        if spec.get("shared"):
            total = sum((day.get(m) or {}).get("req", 0) for m in masks)
            _, reset, label = _window(provider, now)
            lines.append(f"  {'pool total':<12} {total} / "
                         f"{_fmt_num(spec['day'])} today   "
                         f"resets {label} (in {_fmt_delta(reset - now)})")
        lines.append("")
    if len(lines) <= 4:
        lines.append("No keys configured and nothing spent yet — set keys "
                     "in config.yaml, then run any command.")
    lines.append(f"Ledger: {_path if _path else 'work/' + LEDGER_NAME}"
                 " (masked keys, pruned after "
                 f"{KEEP_DAYS} days)")
    return "\n".join(lines)


def cmd_keys(cfg, args) -> int:
    """`python main.py keys` — the dashboard."""
    print(build_status(cfg))
    return 0
