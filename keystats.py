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

ORDER = ["gemini", "groq", "openrouter", "elevenlabs", "pexels", "pixabay",
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
    "pixabay": {
        "title": "PIXABAY",
        "hour": 6000, "unit": "requests",
        "rule": "~100 requests/minute per key · no published monthly cap"},
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
         chars: int = 0, units: int = 0, tag: str = "") -> None:
    """Record one provider call. Never raises; no-op until init().

    tag = the call's origin ("script", "clipfix", "vision", "probe", ...)
    so `keys --month` can show what the tokens were FOR. Agent-run
    diagnostics go through the probe helpers below, which log themselves
    — nothing spends off the books.
    """
    if _path is None or not (req or tok or chars or units):
        return
    event = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "p": provider, "k": _mask(key), "req": int(req),
             "tok": int(tok), "chars": int(chars), "units": int(units)}
    if tag:
        event["tag"] = str(tag)[:24]
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
    if provider in ("pexels", "pixabay"):
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
        if provider == "pixabay":
            return list(cfg.pixabay_api_keys)
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
        if provider in ("pexels", "pixabay"):
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
            elif provider in ("pexels", "pixabay"):
                hour_row = (hour.get(mask) or {"req": 0})["req"]
                hour_left = max(0, spec["hour"] - hour_row)
                _, reset, label = _window(provider, now)
                line = (f"  {mask:<12} {hour_row} this hour "
                        f"({_fmt_num(hour_left)} left)")
                if spec.get("month"):
                    month_used = row["req"]
                    month_left = max(0, spec["month"] - month_used)
                    line += (f" · {_fmt_num(month_used)} this month "
                             f"({_fmt_num(month_left)} left)")
                lines.append(line + f" · hour resets {label}")
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


# --------------------------------------------------------------- month view
def month_totals(events: list[dict], now: datetime | None = None) -> dict:
    """Last-30-day spend per provider, split by call tag (pure, tested).

    {"gemini": {"req": 12, "tok": 3400, "chars": 0, "units": 0,
                "tags": {"script": {"req": 10, "tok": 3200},
                         "probe": {"req": 2, "tok": 200}}}, ...}
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    out: dict = {}
    for event in events:
        try:
            when = datetime.fromisoformat(str(event.get("t")))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when < cutoff:
            continue
        prov = str(event.get("p") or "?")
        row = out.setdefault(prov, {"req": 0, "tok": 0, "chars": 0,
                                    "units": 0, "tags": {}})
        for field in ("req", "tok", "chars", "units"):
            row[field] += int(event.get(field) or 0)
        tag = str(event.get("tag") or "(untagged)")
        trow = row["tags"].setdefault(tag, {"req": 0, "tok": 0})
        trow["req"] += int(event.get("req") or 0)
        trow["tok"] += int(event.get("tok") or 0)
    return out


def month_report() -> str:
    """`python main.py keys --month` — what the last 30 days cost, by lane."""
    events = _load(_path) if _path else []
    totals = month_totals(events)
    if not totals:
        return "Ledger is empty (last 30 days): nothing spent yet."
    lines = ["Spend, last 30 days (self-counted ledger)", ""]
    for provider in ORDER:
        if provider not in totals:
            continue
        row = totals[provider]
        spent = (f"{row['req']} req · {row['tok']:,} tok"
                 if provider in ("gemini", "groq", "openrouter", "deepseek")
                 else f"{row['req']} req"
                 + (f" · {row['chars']:,} chars" if row["chars"] else "")
                 + (f" · {row['units']} units" if row["units"] else ""))
        lines.append(f"{LIMITS[provider]['title']}: {spent}")
        for tag, trow in sorted(row["tags"].items(),
                                key=lambda kv: -kv[1]["req"]):
            extra = f" · {trow['tok']:,} tok" if trow["tok"] else ""
            lines.append(f"    {tag:<12} {trow['req']} req{extra}")
    return "\n".join(lines)


# ------------------------------------------------------------- live probes
def probe_gemini(key: str, model: str) -> tuple[bool, str]:
    """One tiny Gemini call; logs itself. (ok?, detail) Never raises."""
    import requests

    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent",
            params={"key": key},
            json={"contents": [{"parts": [{"text": "reply with: ok"}]}],
                  "generationConfig": {"maxOutputTokens": 128}},
            timeout=20)
    except Exception as exc:  # noqa: BLE001
        return False, f"unreachable ({type(exc).__name__})"
    bump("gemini", key, req=1, tag="probe")
    if r.status_code == 200:
        return True, "ok"
    return False, f"HTTP {r.status_code}"


def probe_chat(name: str, api_url: str, key: str, model: str,
               extra_body: dict | None = None) -> tuple[bool, str]:
    """One tiny OpenAI-compatible chat call; logs itself. Never raises."""
    import requests

    body = {"model": model, "max_tokens": 16, "temperature": 0.0,
            "messages": [{"role": "user", "content": "reply with: ok"}]}
    if extra_body:
        body.update(extra_body)
    try:
        r = requests.post(api_url, json=body, timeout=30,
                          headers={"Authorization": f"Bearer {key}"})
    except Exception as exc:  # noqa: BLE001
        return False, f"unreachable ({type(exc).__name__})"
    tok = 0
    if r.status_code == 200:
        try:
            tok = int(r.json().get("usage", {}).get("total_tokens") or 0)
        except (ValueError, AttributeError):
            tok = 0
    bump(name, key, req=1, tok=tok, tag="probe")
    if r.status_code == 200:
        return True, "ok"
    return False, f"HTTP {r.status_code}: {r.text[:60]}"


def probe_get(name: str, url: str, key: str, headers: dict | None = None,
              params: dict | None = None, units: int = 0) -> tuple[bool, str]:
    """One GET with the key (ElevenLabs / Pexels / Pixabay / YouTube)."""
    import requests

    try:
        r = requests.get(url, headers=headers or {}, params=params or {},
                         timeout=20)
    except Exception as exc:  # noqa: BLE001
        return False, f"unreachable ({type(exc).__name__})"
    bump(name, key, req=1, units=units, tag="probe")
    if r.status_code == 200:
        return True, "ok"
    return False, f"HTTP {r.status_code}"


def run_probes(cfg) -> str:
    """`python main.py keys --probe` — every lane, per key, logged as probe."""
    lines = ["Live probe — every check below is ledgered (tag=probe)", ""]

    def section(title: str, results: list) -> None:
        lines.append(title)
        for mask, ok, detail in results:
            mark = "✅" if ok else "❌"
            lines.append(f"  {mark} ...{mask[-4:]}: {detail}")
        lines.append("")

    keys = [k for k in _configured_keys(cfg, "gemini") if k]
    section("GEMINI",
            [(_mask(k), *probe_gemini(k, cfg.gemini_model)) for k in keys]
            or [("none", False, "not configured")])
    keys = [k for k in _configured_keys(cfg, "groq") if k]
    section("GROQ",
            [(_mask(k), *probe_chat(
                "groq", "https://api.groq.com/openai/v1/chat/completions",
                k, cfg.groq_model,
                {"reasoning_format": "hidden"}
                if "gpt-oss" in cfg.groq_model else None)) for k in keys]
            or [("none", False, "not configured")])
    # OpenRouter: 50/day SHARED pool — one key tests the lane; the next
    # key is only spent if the previous one failed.
    results = []
    for k in [k for k in _configured_keys(cfg, "openrouter") if k]:
        if results and results[-1][1]:
            break
        results.append((_mask(k), *probe_chat(
            "openrouter", "https://openrouter.ai/api/v1/chat/completions",
            k, cfg.openrouter_model)))
    section("OPENROUTER (shared pool — probed until first success)",
            results or [("none", False, "not configured")])
    keys = [k for k in _configured_keys(cfg, "elevenlabs") if k]
    section("ELEVENLABS (quota-free /user check)",
            [(_mask(k), *probe_get(
                "elevenlabs", "https://api.elevenlabs.io/v1/user", k,
                headers={"xi-api-key": k})) for k in keys]
            or [("none", False, "not configured")])
    pex = _configured_keys(cfg, "pexels")
    section("PEXELS",
            [(_mask(pex[0]), *probe_get(
                "pexels", "https://api.pexels.com/v1/search",
                pex[0], headers={"Authorization": pex[0]},
                params={"query": "ocean", "per_page": 1}))]
            if pex and pex[0] else [("none", False, "not configured")])
    pix = [k for k in _configured_keys(cfg, "pixabay") if k]
    section("PIXABAY",
            [(_mask(k), *probe_get(
                "pixabay", "https://pixabay.com/api/", k,
                params={"key": k, "q": "ocean", "per_page": 3}))
             for k in pix[:1]]
            or [("none", False, "not configured")])
    yt = [k for k in _configured_keys(cfg, "youtube") if k]
    section("YOUTUBE (1 quota unit)",
            [(_mask(k), *probe_get(
                "youtube", "https://www.googleapis.com/youtube/v3/videos",
                k, params={"part": "id", "chart": "mostPopular",
                           "maxResults": 1, "regionCode": "US", "key": k},
                units=1)) for k in yt[:1]]
            or [("none", False, "not configured")])
    lines.append("Every probe above is in the ledger: "
                 "python main.py keys --month")
    return "\n".join(lines)
