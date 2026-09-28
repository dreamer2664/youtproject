"""Pre-gen push: score finished clips and park them on Telegram.

Two phases, split by design: the PC renders + pushes while it is on; the
phone pulls from a private Telegram channel any time — including with
the PC off (Telegram's servers hold the files, not your PC).

  python main.py pregen --push     score + send new clips to the channel
  python main.py pregen --best     today's winner + scorecard (no sends)
  python main.py pregen            list what's parked (no sends)

Manifest: <root>/pregen.json — clip ids, scores, Telegram file_ids.
Delivery prefers file_id (Telegram's copy: works even after the PC file
is deleted) and falls back to the local mp4. One bot run reads; the push
command writes. Never touches the render path — finished outputs only.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_NAME = "pregen.json"
MAX_SEND_MB = 48        # Telegram bot files cap at 50 MB; stay under it
HOOK_SECONDS = 3.0      # hook signal = first 3s of the clip's transcript
SCORECARD_REASONS = 6   # reasons shown on the card (full list in manifest)

_LANES = (("clip", "clips", re.compile(r"^([0-9a-f]{8})_clip_(\d{2})\.mp4$")),
          ("part", "parts", re.compile(r"^([0-9a-f]{8})_part_(\d{2})\.mp4$")))
_WINDOW_RE = re.compile(r"window:\s*([\d.]+)\s*s?\s*-\s*([\d.]+)", re.I)
_CACHE_RE = re.compile(r"^[0-9a-f]{16}$")


def manifest_path(cfg) -> Path:
    return cfg.root / MANIFEST_NAME


def load_manifest(cfg) -> dict:
    """Parked-clip manifest (corrupt-safe: never raises, never lies).

    A corrupt file starts fresh — the channel still holds the videos,
    so the worst case is a re-push, never a crash or a phantom entry.
    """
    path = manifest_path(cfg)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {"clips": []}
    try:
        data = json.loads(raw)
    except ValueError:
        print(f"  [pregen] {path.name} is corrupt — starting fresh "
              f"(channel copies are unaffected).")
        return {"clips": []}
    if not isinstance(data, dict) or not isinstance(data.get("clips"), list):
        return {"clips": []}
    return data


def save_manifest(cfg, data: dict) -> None:
    manifest_path(cfg).write_text(json.dumps(data, indent=1),
                                  encoding="utf-8")


def parse_window(text: str) -> tuple[float, float] | None:
    """CREDIT.txt 'window: 12.3s - 45.6s' -> (start, end). Pure."""
    match = _WINDOW_RE.search(text or "")
    if not match:
        return None
    try:
        start, end = float(match.group(1)), float(match.group(2))
    except ValueError:
        return None
    if end <= start:
        return None
    return start, end


def today_local() -> str:
    """Phone-owner's calendar day (the PC's local date)."""
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _find_cache(cfg, src8: str):
    """Transcript cache for this source, if a re-run hasn't pruned it."""
    try:
        cached = sorted((cfg.work_dir / "clip_cache").glob(f"{src8}*.json"))
    except OSError:
        return None
    for path in cached:
        if _CACHE_RE.fullmatch(path.stem):
            return path
    return None


def collect_candidates(cfg) -> list[dict]:
    """Finished clips/parts on disk, with hook text for scoring.

    Reads top-level lane mp4s only (kit-dir copies and compilations never
    match the *_clip_NN / *_part_NN shape). One unreadable kit is skipped,
    never fatal. Durations come from CREDIT.txt windows — no ffprobe.
    """
    from clipper import clip_words, load_transcript_cache

    found: list[dict] = []
    for lane, subdir, pattern in _LANES:
        folder = cfg.root / subdir
        try:
            names = sorted(p.name for p in folder.iterdir() if p.is_file())
        except OSError:
            continue
        for name in names:
            match = pattern.fullmatch(name)
            if not match:
                continue
            src8, num = match.group(1), match.group(2)
            try:
                found.append(_collect_one(cfg, lane, folder, name, src8,
                                          num, clip_words,
                                          load_transcript_cache))
            except Exception as exc:  # noqa: BLE001 - one bad kit skips
                print(f"  [pregen] skipping {name} ({exc})")
    return found


def _collect_one(cfg, lane: str, folder: Path, name: str, src8: str,
                 num: str, clip_words, load_cache) -> dict:
    stem = name[:-4]
    kit = folder / stem
    window = parse_window(_read_text(kit / "CREDIT.txt"))
    start, end = window if window else (0.0, 0.0)
    duration = end - start if window else 0.0
    hook_text, word_count = "", 0
    if window:
        cache_path = _find_cache(cfg, src8)
        words = load_cache(cache_path) if cache_path else None
        if words:
            in_clip = clip_words(words, start, end)
            word_count = len(in_clip)
            hook = clip_words(words, start, min(start + HOOK_SECONDS, end))
            hook_text = " ".join(str(w.get("word") or "") for w in hook)
    has_caps = (kit / "captions.srt").exists() or (kit / "part.ass").exists()
    return {"id": f"{lane[0]}-{src8}-{num}", "lane": lane,
            "file": str(folder / name), "kit": str(kit),
            "title": _read_text(kit / "TITLE.txt"),
            "duration_s": duration, "hook_text": hook_text,
            "word_count": word_count,
            "wps": (word_count / duration
                    if word_count > 0 and duration > 0 else None),
            "caps": has_caps}


def score_candidates(cands: list[dict]) -> list[dict]:
    """Attach virality verdicts (new dicts; inputs untouched)."""
    from virality import score_clip

    scored = []
    for cand in cands:
        verdict = score_clip(cand.get("title") or "",
                             cand.get("hook_text") or "",
                             int(cand.get("word_count") or 0),
                             float(cand.get("duration_s") or 0.0))
        scored.append({**cand, **verdict})
    return scored


def format_scorecard(entry: dict) -> str:
    """The phone card: score + top reasons (pure)."""
    title = (entry.get("title") or "").strip() or str(entry.get("id"))
    seconds = float(entry.get("duration_s") or 0.0)
    words = int(entry.get("word_count") or 0)
    lines = [f"🎬 {title[:80]}",
             f"score {int(entry.get('score') or 0)}/100 · "
             f"{seconds:.0f}s · {words} words"]
    reasons = list(entry.get("reasons") or [])
    for reason in reasons[:SCORECARD_REASONS]:
        lines.append(f"  + {reason}"[:90])
    if len(reasons) > SCORECARD_REASONS:
        lines.append(f"  … +{len(reasons) - SCORECARD_REASONS} more")
    return "\n".join(lines)


def best_of_day(entries: list[dict], day: str) -> dict | None:
    """Highest score among clips pushed on `day` (pure)."""
    from virality import pick_best

    pushed = [e for e in entries
              if e.get("day") == day and e.get("pushed_at")]
    return pick_best(pushed)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def push_pending(cfg, sender, chat_id: int, limit: int = 0,
                 dry_run: bool = False, day: str | None = None,
                 now: str | None = None) -> dict:
    """Score unpushed clips and park them on Telegram.

    sender(kind, chat_id, payload, caption="") -> dict: "video" takes a
    Path and returns {"message_id", "file_id"}; "message" takes str. Any
    sender exception fails that clip only. The manifest saves after every
    clip, so a crash mid-push never double-sends on the next run.
    """
    from virality import pick_best

    day = day or today_local()
    manifest = load_manifest(cfg)
    parked = list(manifest.get("clips") or [])
    pushed_ids = {c.get("id") for c in parked if c.get("pushed_at")}
    fresh = [c for c in score_candidates(collect_candidates(cfg))
             if c.get("id") not in pushed_ids]
    if limit and limit > 0:
        fresh = fresh[:limit]
    report: dict = {"pushed": [], "skipped": [], "pending": [c["id"] for c in fresh],
                    "best": None, "errors": []}
    if dry_run:
        best = pick_best([e for e in parked if e.get("day") == day
                          and e.get("pushed_at")] + fresh)
        report["best"] = best["id"] if best else None
        return report

    for cand in fresh:
        path = Path(cand["file"])
        if not path.exists():
            report["skipped"].append((cand["id"], "file gone from disk"))
            continue
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_SEND_MB:
            report["skipped"].append(
                (cand["id"], f"{size_mb:.0f} MB over the {MAX_SEND_MB} MB cap"))
            continue
        try:
            sent = sender("video", chat_id, path,
                          caption=f"🎬 {(cand['title'] or cand['id'])[:60]}")
            sender("message", chat_id, format_scorecard(cand))
        except Exception as exc:  # noqa: BLE001 - one clip never stops a push
            report["errors"].append(f"{cand['id']}: {str(exc)[:150]}")
            continue
        record = {k: cand.get(k) for k in (
            "id", "lane", "title", "file", "duration_s", "word_count",
            "score", "reasons", "signals", "caps")}
        record.update({"pushed_at": now or _utcnow(), "day": day,
                       "chat_id": chat_id,
                       "message_id": (sent or {}).get("message_id"),
                       "file_id": (sent or {}).get("file_id")})
        parked.append(record)
        manifest["clips"] = parked
        save_manifest(cfg, manifest)  # incremental: crash-safe
        report["pushed"].append(cand["id"])
        print(f"  [pregen] parked {cand['id']} "
              f"({cand['score']}/100, {size_mb:.1f} MB)")

    pool = [e for e in parked if e.get("day") == day and e.get("pushed_at")]
    best = pick_best(pool)
    report["best"] = best["id"] if best else None
    # Announce the winner only when this run parked it — otherwise every
    # push re-spams a crown the phone already saw.
    if best and best["id"] in report["pushed"]:
        caption = ""
        try:
            from clipper import clip_platform_caption

            caption = clip_platform_caption(best.get("title") or "", "tiktok")
        except Exception:  # noqa: BLE001 - caption is a bonus
            caption = ""
        text = "🏆 Today's pick:\n\n" + format_scorecard(best)
        if caption:
            text += f"\n\nCaption (copy-paste):\n\n{caption}"
        try:
            sender("message", chat_id, text)
        except Exception as exc:  # noqa: BLE001 - announced or not, clips are parked
            report["errors"].append(f"best-announce: {str(exc)[:150]}")
    return report


def bot_sender(cfg):
    """A push_pending sender backed by the real Telegram bot."""
    from bot import PhoneBot

    bot = PhoneBot(cfg)

    def sender(kind: str, chat_id: int, payload, caption: str = "") -> dict:
        if kind == "video":
            result = bot.send_video_file(chat_id, Path(payload),
                                         caption=caption)
            video = (result or {}).get("video") or {}
            return {"message_id": (result or {}).get("message_id"),
                    "file_id": video.get("file_id")}
        if kind == "message":
            result = bot.send_message(chat_id, str(payload))
            return {"message_id": (result or {}).get("message_id")}
        raise ValueError(f"unknown sender kind: {kind}")

    return sender
