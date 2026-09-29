"""Channel stats tracker: every tracked channel, one command.

`python main.py snap` pulls the public numbers for all channels you track
(views, likes, comments per video; subs + total views per channel), stores
one snapshot per day in work/snapshots.json, and prints a per-channel
report with deltas since the previous snapshot. The Telegram bot's /stats
runs the same fetch and sends one compact message per channel.

Cost: channels.list (1 unit / 50 channels) + per channel one playlist page
per 50 uploads + one videos.list per 50 videos -> ~3 units per channel per
run at the default fetch_limit of 50, of 10,000/day per key. Scales to as
many channels as you add; one broken channel never sinks the others.

Public API limits (honest): no impressions, CTR, retention or traffic
sources — those need YouTube Studio / the OAuth Analytics API. Public
subscriber counts are rounded by YouTube (3 significant digits), so small
sub deltas can read 0 until a rounding step is crossed.
"""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from pathlib import Path

SHORT_MAX_S = 180          # YouTube Shorts ceiling (since Oct 2024)
PHONE_ROWS = 5             # videos per channel in a /stats message
PHONE_LIMIT = 4000         # Telegram caps messages at 4096 chars
SORTS = ("new", "views", "eng", "pace")


# ---------------------------------------------------------------- plumbing
def make_client(cfg):
    """Seam for tests: the bot and `snap` both build clients here."""
    from youtube import YouTubeClient

    return YouTubeClient(cfg.youtube_api_keys)


def store_path(cfg) -> Path:
    return Path(cfg.work_dir) / "snapshots.json"


def tracked(history: dict) -> list[dict]:
    """Tracked channels in add order, malformed entries dropped."""
    return [c for c in history.get("channels") or []
            if isinstance(c, dict) and c.get("id")]


def resolve_channel(client, ref: str) -> tuple[str, str]:
    """Any user reference -> (channel_id, title). 1 unit.

    Accepts a video link (watch / youtu.be / shorts), a channel URL, an
    @handle or a raw UC… id. Raises ValueError/RuntimeError with a
    readable message otherwise.
    """
    from youtube import channel_stats, extract_id, video_stats

    kind, ident = extract_id(ref)
    if kind == "video":
        info = video_stats(client, ident)
        if not info.get("channel_id"):
            raise RuntimeError("could not resolve that video's channel")
        return (info["channel_id"], info.get("channel") or "?")
    if kind == "handle":
        info = channel_stats(client, "@" + ident)
    else:
        info = channel_stats(client, ident)
    if not info.get("id"):
        raise RuntimeError(f"channel {ref!r} not found")
    return (info["id"], info.get("title") or "?")


def add_channel(history: dict, channel_id: str, title: str) -> bool:
    """Track a channel; True when new (a known id just refreshes its title)."""
    channels = tracked(history)
    for entry in channels:
        if entry["id"] == channel_id:
            entry["title"] = title or entry.get("title") or "?"
            history["channels"] = channels
            return False
    channels.append({"id": channel_id, "title": title or "?"})
    history["channels"] = channels
    return True


def match_channels(history: dict, needle: str) -> list[dict]:
    """Channels whose id equals, or title contains, needle (case-free)."""
    needle = (needle or "").strip().lower().lstrip("@")
    if not needle:
        return []
    exact = [c for c in tracked(history) if c["id"].lower() == needle]
    if exact:
        return exact
    return [c for c in tracked(history)
            if needle in str(c.get("title") or "").lower()]


def remove_channel(history: dict, needle: str) -> tuple[dict | None, str]:
    """Untrack one channel. Returns (removed, problem): none/many/''."""
    hits = match_channels(history, needle)
    if not hits:
        return (None, "none")
    if len(hits) > 1:
        return (None, "many")
    history["channels"] = [c for c in tracked(history)
                           if c["id"] != hits[0]["id"]]
    return (hits[0], "")


def prune_days(history: dict, today: str, keep_days: int) -> int:
    """Drop snapshots older than keep_days. Returns how many went."""
    try:
        cutoff = date.fromordinal(date.fromisoformat(today).toordinal()
                                  - keep_days).isoformat()
    except ValueError:
        return 0
    before = len(history.get("days") or [])
    history["days"] = [d for d in history.get("days") or []
                       if str(d.get("date") or "") >= cutoff]
    return before - len(history["days"])


# ------------------------------------------------------------------ fetch
def run_snapshot(cfg, client, today: str | None = None,
                 fetch_limit: int | None = None, log=print) -> dict:
    """Fetch every tracked channel, record today's snapshot, save.

    Returns {history, today, prev, errors, fetched}. Per-channel failures
    land in `errors` and the rest still record. Raises only when there is
    nothing tracked (caller explains how to --add).
    """
    from youtube import (channels_vitals, load_snapshots, playlist_video_ids,
                         previous_daysnapshot, record_snapshot,
                         save_snapshots, snapshot_videos, uploads_playlist_id)

    settings = cfg.snap_settings
    limit = fetch_limit or settings["fetch_limit"]
    today = today or date.today().isoformat()
    store = store_path(cfg)
    history = load_snapshots(store)
    channels = tracked(history)
    if not channels:
        raise LookupError("no channels tracked")
    errors: list[str] = []
    try:
        vitals = channels_vitals(client, [c["id"] for c in channels])
    except Exception as exc:  # vitals are a bonus; videos still flow
        errors.append(f"channel totals unavailable ({str(exc)[:120]})")
        vitals = {}
    videos: list[dict] = []
    seen: set[str] = set()
    fetched: dict[str, int] = {}
    for channel in channels:
        cid, title = channel["id"], channel.get("title") or "?"
        info = vitals.get(cid)
        if info and info.get("title"):
            channel["title"] = title = info["title"]  # renames follow along
        if vitals and info is None:
            errors.append(f"{title}: channel not found (deleted or "
                          f"terminated?) — python main.py snap --remove "
                          f"\"{title}\"")
            continue
        try:
            playlist = (info or {}).get("uploads") or \
                uploads_playlist_id(client, cid)
            ids = playlist_video_ids(client, playlist, limit=limit)
            fresh = [i for i in ids if i not in seen]
            seen.update(fresh)
            batch = snapshot_videos(client, fresh)
        except Exception as exc:
            errors.append(f"{title}: {str(exc)[:160]}")
            continue
        for video in batch:
            video["channel_id"] = video.get("channel_id") or cid
        videos.extend(batch)
        fetched[cid] = len(batch)
        log(f"  [snap] {title}: {len(batch)} video(s)")
    history["channels"] = channels
    channel_rows = {cid: {k: info[k] for k in
                          ("title", "handle", "subs", "subs_hidden",
                           "views", "videos")}
                    for cid, info in vitals.items()}
    record_snapshot(history, videos, today, channels=channel_rows)
    prune_days(history, today, settings["keep_days"])
    prev = previous_daysnapshot(history, today)
    save_snapshots(store, history)
    return {"history": history, "today": today, "prev": prev,
            "errors": errors, "fetched": fetched}


# ---------------------------------------------------------------- metrics
def _parse_ts(text: str) -> datetime | None:
    text = str(text or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def age_hours(video: dict, now: datetime, today: str) -> float:
    """Hours since upload: exact timestamp when stored, else by date."""
    stamp = _parse_ts(video.get("published_at", ""))
    if stamp is not None:
        return max(0.0, (now - stamp).total_seconds() / 3600)
    try:
        days = (date.fromisoformat(today)
                - date.fromisoformat(str(video.get("published", ""))[:10])).days
    except ValueError:
        return 0.0
    return max(0.0, days * 24.0)


def engagement(video: dict) -> float | None:
    """(likes + comments) / views as a percentage; None at 0 views."""
    views = int(video.get("views") or 0)
    if views <= 0:
        return None
    return 100.0 * (int(video.get("likes") or 0)
                    + int(video.get("comments") or 0)) / views


def pace(video: dict, hours: float) -> tuple[float, str]:
    """Views per hour under 48h (launch speed), per day after."""
    views = int(video.get("views") or 0)
    if hours < 48:
        return (views / max(hours, 1.0), "/h")
    return (views / (hours / 24.0), "/d")


def per_day(video: dict, hours: float) -> float:
    return int(video.get("views") or 0) / max(hours / 24.0, 1 / 24)


def compact(n: float) -> str:
    """1234 -> 1.2K, 1234567 -> 1.2M (phone-friendly)."""
    n = float(n)
    for unit, size in (("M", 1e6), ("K", 1e3)):
        if abs(n) >= size:
            text = f"{n / size:.1f}".rstrip("0").rstrip(".")
            return f"{text}{unit}"
    return f"{int(n)}"


def fmt_len(seconds: int) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "—"
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _signed(n: int | None) -> str:
    if not n:
        return ""
    return f"+{n:,}" if n > 0 else f"{n:,}"


# ------------------------------------------------------------- grouping
def group_rows(history: dict, today: str, prev: dict | None,
               now: datetime | None = None) -> list[dict]:
    """Today's snapshot grouped per tracked channel, metrics computed.

    Legacy rows without channel_id are matched by channel title; rows that
    match nothing land in an "other" group (never silently dropped).
    """
    now = now or datetime.now(timezone.utc)
    current = next((d for d in history.get("days") or []
                    if d.get("date") == today), None) or {}
    prev = prev or {}
    prev_videos = prev.get("videos") or {}
    vitals = current.get("channels") or {}
    prev_vitals = prev.get("channels") or {}
    channels = tracked(history)
    by_title = {str(c.get("title") or "").lower(): c["id"] for c in channels}
    groups: dict[str, dict] = {}
    for c in channels:
        groups[c["id"]] = {"id": c["id"], "title": c.get("title") or "?",
                           "vitals": vitals.get(c["id"]),
                           "prev_vitals": prev_vitals.get(c["id"]),
                           "videos": []}
    for video_id, v in (current.get("videos") or {}).items():
        cid = v.get("channel_id") or by_title.get(
            str(v.get("channel") or "").lower(), "")
        if cid not in groups:
            cid = "?"
            groups.setdefault("?", {"id": "?", "title": "other / untracked",
                                    "vitals": None, "prev_vitals": None,
                                    "videos": []})
        hours = age_hours(v, now, today)
        before = prev_videos.get(video_id)
        speed, unit = pace(v, hours)
        groups[cid]["videos"].append({
            **v, "id": video_id, "age_h": hours,
            "delta": (v["views"] - int(before.get("views") or 0))
            if before else None,
            "new": bool(prev_videos) and before is None,
            "eng": engagement(v), "pace": speed, "pace_unit": unit,
            "per_day": per_day(v, hours),
            "short": 0 < int(v.get("duration_s") or 0) <= SHORT_MAX_S})
    return list(groups.values())


def sort_videos(videos: list[dict], how: str) -> list[dict]:
    if how == "views":
        return sorted(videos, key=lambda v: -int(v.get("views") or 0))
    if how == "eng":
        return sorted(videos, key=lambda v: -(v.get("eng") or -1))
    if how == "pace":
        return sorted(videos, key=lambda v: -v.get("per_day", 0))
    return sorted(videos, key=lambda v: (v.get("published_at")
                                         or v.get("published") or ""),
                  reverse=True)


def _flag_new(videos: list[dict], flagged: set, new_flags: list) -> None:
    """Same one-shot retitle rule as build_report: >=2 days, <50 views."""
    for v in videos:
        if v["age_h"] >= 48 and v["views"] < 50 and v["id"] not in flagged:
            v["flag"] = "RETITLE?"
            new_flags.append(v["id"])
            flagged.add(v["id"])


def _channel_header(group: dict) -> str:
    vit, pv = group["vitals"], group["prev_vitals"]
    head = f"━━ {group['title']}"
    if not vit:
        return head
    if vit.get("handle"):
        head += f" ({vit['handle']})"
    subs = "hidden" if vit.get("subs_hidden") else f"{vit['subs']:,}"
    bits = [f"{subs} subs"]
    if pv and not vit.get("subs_hidden"):
        delta = vit["subs"] - int(pv.get("subs") or 0)
        if delta:
            bits[-1] += f" ({_signed(delta)})"
    views = f"{vit['views']:,} views"
    if pv:
        delta = vit["views"] - int(pv.get("views") or 0)
        if delta:
            views += f" ({_signed(delta)})"
    count = vit["videos"]
    bits += [views, f"{count:,} video{'' if count == 1 else 's'}"]
    return head + " — " + " · ".join(bits)


# ---------------------------------------------------------------- reports
def build_channel_report(history: dict, today: str, prev: dict | None,
                         recent: int = 10, sort: str = "new",
                         only: str = "", now: datetime | None = None,
                         errors: list[str] | None = None
                         ) -> tuple[str, list[str]]:
    """Per-channel terminal report + newly flagged ids (pure, tested)."""
    groups = group_rows(history, today, prev, now)
    if only:
        wanted = {c["id"] for c in match_channels(history, only)}
        groups = [g for g in groups if g["id"] in wanted]
        if not groups:
            return (f"No tracked channel matches {only!r}. "
                    f"python main.py snap --list", [])
    flagged = set(history.get("flagged") or [])
    new_flags: list[str] = []
    lines = [f"Channel stats — {today}"
             + (f"  (Δ vs {prev['date']})" if prev else "  (first snapshot "
                "— deltas start tomorrow)")]
    total_subs = total_views = total_delta = 0
    for group in groups:
        videos = group["videos"]
        _flag_new(videos, flagged, new_flags)
        lines.append("")
        lines.append(_channel_header(group))
        vit = group["vitals"]
        if vit:
            total_subs += 0 if vit.get("subs_hidden") else vit["subs"]
            total_views += vit["views"]
        total_delta += sum(v["delta"] or 0 for v in videos)
        if not videos:
            lines.append("  (no videos fetched)")
            continue
        shown = sort_videos(videos, sort)[:recent]
        lines.append(f"  {'VIDEO':<34} {'AGE':>4} {'LEN':>7} {'VIEWS':>8} "
                     f"{'Δ':>7} {'LIKES':>6} {'CMTS':>5} {'ENG':>6} "
                     f"{'PACE':>8}")
        for v in shown:
            title = str(v.get("title") or "?")
            label = (title[:32] + "…") if len(title) > 33 else title
            if v.get("new"):
                label = label[:33] + "*"
            age = (f"{int(v['age_h'])}h" if v["age_h"] < 48
                   else f"{int(v['age_h'] // 24)}d")
            eng = "—" if v["eng"] is None else f"{v['eng']:.1f}%"
            pace_s = f"{compact(round(v['pace']))}{v['pace_unit']}"
            row = (f"  {label:<34} {age:>4} {fmt_len(v.get('duration_s')):>7} "
                   f"{v['views']:>8,} {_signed(v['delta']):>7} "
                   f"{int(v.get('likes') or 0):>6,} "
                   f"{int(v.get('comments') or 0):>5,} {eng:>6} {pace_s:>8}")
            if v.get("flag"):
                row += f"  {v['flag']}"
            lines.append(row)
        views = [v["views"] for v in videos]
        median = sorted(views)[len(views) // 2]
        engs = [v["eng"] for v in videos if v["eng"] is not None]
        summary = (f"  showing {len(shown)} of {len(videos)} tracked · "
                   f"median {median:,} views")
        if engs:
            summary += f" · avg eng {sum(engs) / len(engs):.1f}%"
        best = max(videos, key=lambda v: v["delta"] or 0)
        if best["delta"]:
            summary += (f" · top mover: {str(best['title'])[:28]} "
                        f"({_signed(best['delta'])})")
        lines.append(summary)
    unseen = [v for g in groups for v in g["videos"]
              if v.get("flag") and v not in
              sort_videos(g["videos"], sort)[:recent]]
    if unseen:
        lines.append("")
        lines.append("Retitle candidates outside the view above:")
        lines += [f"  {str(v['title'])[:50]} — {v['views']} views, "
                  f"{int(v['age_h'] // 24)}d old" for v in unseen]
    if len(groups) > 1:
        lines.append("")
        lines.append(f"ALL {len(groups)} channels: {total_subs:,} subs · "
                     f"{total_views:,} views"
                     + (f" · {_signed(total_delta)} on tracked videos"
                        if total_delta else ""))
    if errors:
        lines.append("")
        lines += [f"⚠️ {e}" for e in errors]
    lines.append("  (* = new since last snapshot · PACE = views/hour under "
                 "48h, views/day after · ENG = (likes+comments)/views)")
    return ("\n".join(lines), new_flags)


def build_phone_report(history: dict, today: str, prev: dict | None,
                       rows: int = PHONE_ROWS, only: str = "",
                       now: datetime | None = None,
                       errors: list[str] | None = None) -> list[str]:
    """One compact Telegram message per channel, newest videos first."""
    groups = group_rows(history, today, prev, now)
    if only:
        wanted = {c["id"] for c in match_channels(history, only)}
        groups = [g for g in groups if g["id"] in wanted]
        if not groups:
            return [f"No tracked channel matches {only!r}."]
    messages = []
    for group in groups:
        vit, pv = group["vitals"], group["prev_vitals"]
        head = f"📊 {group['title']}"
        if vit:
            subs = "hidden" if vit.get("subs_hidden") else compact(vit["subs"])
            head += f"\n👥 {subs} subs"
            if pv and not vit.get("subs_hidden"):
                d = vit["subs"] - int(pv.get("subs") or 0)
                head += f" ({_signed(d)})" if d else ""
            head += f" · 👁 {compact(vit['views'])} views"
            if pv:
                d = vit["views"] - int(pv.get("views") or 0)
                head += f" ({_signed(d)})" if d else ""
        lines = [head]
        videos = sort_videos(group["videos"], "new")
        if not videos:
            lines.append("(no videos fetched)")
        for i, v in enumerate(videos[:rows], 1):
            eng = "" if v["eng"] is None else f" · {v['eng']:.1f}%"
            delta = f" ({_signed(v['delta'])})" if v["delta"] else ""
            age = (f"{int(v['age_h'])}h" if v["age_h"] < 48
                   else f"{int(v['age_h'] // 24)}d")
            lines.append(
                f"\n{i}. {str(v.get('title') or '?')[:70]}\n"
                f"   👁 {v['views']:,}{delta} · 👍 {int(v.get('likes') or 0):,}"
                f" · 💬 {int(v.get('comments') or 0):,}{eng} · "
                f"{compact(round(v['pace']))}{v['pace_unit']} · {age}")
        text = "\n".join(lines)
        messages.append(text if len(text) <= PHONE_LIMIT
                        else text[:PHONE_LIMIT - 1] + "…")
    if errors:
        messages.append("⚠️ " + "\n⚠️ ".join(errors))
    return messages


# ----------------------------------------------------------------- export
EXPORT_FIELDS = ["date", "channel", "channel_id", "video_id", "url", "title",
                 "published_at", "age_days", "length_s", "is_short", "views",
                 "views_delta", "likes", "comments", "engagement_pct",
                 "views_per_day"]
CHANNEL_FIELDS = ["date", "channel", "channel_id", "handle", "subs",
                  "subs_delta", "views", "views_delta", "videos"]


def export_rows(history: dict, today: str, prev: dict | None,
                now: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """(video rows, channel rows) for spreadsheets — all fetched videos."""
    videos, channels = [], []
    for group in group_rows(history, today, prev, now):
        vit, pv = group["vitals"], group["prev_vitals"]
        if vit:
            channels.append({
                "date": today, "channel": group["title"],
                "channel_id": group["id"], "handle": vit.get("handle", ""),
                "subs": "" if vit.get("subs_hidden") else vit["subs"],
                "subs_delta": (vit["subs"] - int(pv.get("subs") or 0))
                if pv and not vit.get("subs_hidden") else "",
                "views": vit["views"],
                "views_delta": (vit["views"] - int(pv.get("views") or 0))
                if pv else "",
                "videos": vit["videos"]})
        for v in sort_videos(group["videos"], "new"):
            videos.append({
                "date": today, "channel": group["title"],
                "channel_id": group["id"], "video_id": v["id"],
                "url": f"https://youtu.be/{v['id']}",
                "title": v.get("title", ""),
                "published_at": v.get("published_at") or v.get("published", ""),
                "age_days": round(v["age_h"] / 24, 1),
                "length_s": int(v.get("duration_s") or 0) or "",
                "is_short": "yes" if v["short"] else
                ("no" if v.get("duration_s") else ""),
                "views": v["views"],
                "views_delta": "" if v["delta"] is None else v["delta"],
                "likes": int(v.get("likes") or 0),
                "comments": int(v.get("comments") or 0),
                "engagement_pct": "" if v["eng"] is None else round(v["eng"], 2),
                "views_per_day": round(v["per_day"], 1)})
    return (videos, channels)


def write_csv(rows: list[dict], fields: list[str], path: Path) -> Path:
    """UTF-8 with BOM so Excel shows accents/emoji in titles correctly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_xlsx(videos: list[dict], channels: list[dict],
               path: Path) -> Path | None:
    """Two-sheet workbook (Videos, Channels). None if openpyxl is missing.

    Excel with Italian/EU regional settings opens comma-CSV as one column;
    the .xlsx always opens cleanly, so it's the one to double-click.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.utils import get_column_letter
    except ImportError:
        return None
    book = Workbook()
    for index, (name, rows, fields) in enumerate(
            (("Videos", videos, EXPORT_FIELDS),
             ("Channels", channels, CHANNEL_FIELDS))):
        sheet = book.active if index == 0 else book.create_sheet()
        sheet.title = name
        sheet.append(fields)
        for row in rows:
            sheet.append([row.get(f, "") for f in fields])
        sheet.freeze_panes = "A2"
        for col, field in enumerate(fields, 1):
            width = max([len(field)] + [len(str(r.get(field, "")))
                                        for r in rows[:200]])
            sheet.column_dimensions[get_column_letter(col)].width = \
                min(60, width + 2)
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
    return path


def export_snapshot(cfg, history: dict, today: str, prev: dict | None,
                    now: datetime | None = None) -> dict:
    """Write out/stats/stats-DATE.csv (+ channels CSV, + .xlsx if able)."""
    videos, channels = export_rows(history, today, prev, now)
    folder = Path(cfg.out_dir) / "stats"
    out = {"csv": write_csv(videos, EXPORT_FIELDS,
                            folder / f"stats-{today}.csv"),
           "channels_csv": write_csv(channels, CHANNEL_FIELDS,
                                     folder / f"channels-{today}.csv"),
           "xlsx": write_xlsx(videos, channels,
                              folder / f"stats-{today}.xlsx"),
           "rows": len(videos)}
    return out
