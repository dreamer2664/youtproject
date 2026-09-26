"""Series splitter: cut any video into "Title — Part X" Shorts.

    python main.py parts <youtube-link-or-file> [--part-len 60]

Mechanical, not editorial: fixed-length episodes snapped to sentence
boundaries (never mid-word), each with a persistent top header
("<title> / Part X") and karaoke subtitles at the bottom, plus one
upload kit per part. ~2 API calls per source total (transcript + fix),
then pure FFmpeg — the cheapest lane in the project.

Reuses the clip lane end to end (download, cached transcript, render,
vertical treatment); the only new machinery is the window planner and
the header overlay line.
"""

from __future__ import annotations

import shutil
from pathlib import Path

PART_LEN_DEFAULT = 60.0
PART_LEN_MIN = 15.0
PART_LEN_MAX = 600.0
PART_TAIL_MERGE = 15.0    # trailing tail shorter than this joins the previous part
PART_SNAP_WINDOW = 10.0   # sentence-snap search radius around each ideal cut
PART_WORD_SNAP = 2.0      # word-boundary fallback radius (no sentence nearby)
PARTS_MAX_DEFAULT = 50
PART_TITLE_LIMIT = 95     # YouTube title ceiling is 100; stay under it
HEADER_TITLE_CHARS = 44   # header title line, truncated to fit 1080px
HEADER_MARGIN_V = 110     # true pixels from the top (shorts-safe)


# ------------------------------------------------------------------ planning
def _float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def plan_parts(duration: float,
               spans: list[tuple[float, float]] | None,
               target_len: float = PART_LEN_DEFAULT,
               tail_merge: float = PART_TAIL_MERGE,
               snap_window: float = PART_SNAP_WINDOW,
               max_parts: int = PARTS_MAX_DEFAULT,
               word_starts: list[float] | None = None,
               ) -> list[tuple[float, float]]:
    """Episode (start, end) windows for a source (pure, tested).

    spans: sentence boundaries (clipper.sentence_spans). Each ideal cut
    (every target_len) snaps to the nearest sentence END within
    snap_window; with no sentence nearby it snaps to the nearest word
    start within PART_WORD_SNAP; else it cuts exactly (words stay atomic
    downstream — clip_words slices by word start). A trailing tail shorter
    than tail_merge merges into the previous part instead of standing
    alone. Past max_parts the target widens so nothing is silently
    dropped. Garbage in -> sane plan out, never a crash.
    """
    duration = _float(duration, 0.0)
    if duration <= 0:
        return []
    target = min(PART_LEN_MAX, max(PART_LEN_MIN,
                                   _float(target_len, PART_LEN_DEFAULT)))
    tail = max(0.0, _float(tail_merge, PART_TAIL_MERGE))
    window = max(0.0, _float(snap_window, PART_SNAP_WINDOW))
    cap = max(1, _int(max_parts, PARTS_MAX_DEFAULT))
    ends = sorted({float(e) for _, e in (spans or []) if e})
    starts = sorted({float(s) for s in (word_starts or []) if s})
    plan = _walk(duration, target, tail, window, ends, starts)
    if len(plan) > cap:
        plan = _walk(duration, duration / cap, tail, window, ends, starts)
    return plan


def _walk(duration: float, target: float, tail: float, window: float,
          ends: list[float], starts: list[float]) -> list[tuple[float, float]]:
    cuts = [0.0]
    while True:
        ideal = cuts[-1] + target
        if ideal >= duration - tail:
            break
        cuts.append(_snap(ideal, duration, window, ends, starts, cuts[-1]))
    cuts.append(duration)
    return [(a, b) for a, b in zip(cuts, cuts[1:]) if b - a > 0.5]


def _snap(ideal: float, duration: float, window: float,
          ends: list[float], starts: list[float], floor: float) -> float:
    if window > 0 and ends:
        near = [e for e in ends
                if floor < e < duration and abs(e - ideal) <= window]
        if near:
            return min(near, key=lambda e: abs(e - ideal))
    if starts:
        near = [s for s in starts
                if floor < s < duration and abs(s - ideal) <= PART_WORD_SNAP]
        if near:
            return min(near, key=lambda s: abs(s - ideal))
    return ideal


# -------------------------------------------------------------------- header
def esc_header(text: str, limit: int = HEADER_TITLE_CHARS) -> str:
    """One-line ASS-safe header text (pure, tested)."""
    clean = " ".join(str(text or "").split())
    if len(clean) > limit:
        clean = clean[:max(0, limit - 1)].rstrip() + "…"
    return clean.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def parts_header_line(title: str, index: int, total: int,
                      part_len: float,
                      margin_v: int = HEADER_MARGIN_V) -> str:
    """Full-duration Dialogue for the persistent top header (pure, tested).

    "" when the video is a single part (no header on solo episodes) or the
    title is blank. Mirrors subtitles.progress_ass_line: rides the same
    .ass burn as the karaoke, zero extra FFmpeg cost, restyleable later
    via reburn.
    """
    from subtitles import ass_timestamp, mask_profanity

    if total <= 1 or not str(title or "").strip():
        return ""
    head = esc_header(mask_profanity(str(title)))
    return (
        f"Dialogue: 0,0:00:00.00,{ass_timestamp(part_len)},"
        f"Karaoke,,0,0,{int(margin_v)},,"
        f"{{\\an8\\bord2\\shad0\\fs52\\c&H00FFFFFF&}}{head}\\N"
        f"{{\\fs78\\c&H0000D7FF&}}Part {int(index)}"
    )


# ----------------------------------------------------------------------- kit
def part_kit_title(source_title: str, index: int, total: int) -> str:
    """TITLE.txt: "<title> — Part X", plain title when solo (pure, tested)."""
    from subtitles import mask_profanity

    title = " ".join(str(source_title or "").split()) or f"Part {index}"
    if total > 1:
        title = f"{title} — Part {index}"
    if len(title) > PART_TITLE_LIMIT:
        title = title[:PART_TITLE_LIMIT - 3].rstrip() + "..."
    return mask_profanity(title)


def build_part_description(title: str, index: int, total: int,
                           source: dict, siblings: list[str]) -> str:
    """DESCRIPTION.txt with sibling-part index + source credit (pure, tested)."""
    lines = [title, ""]
    if total > 1:
        lines.append(f"Part {index} of {total}.")
        if siblings:
            lines.append("Series: " + ", ".join(siblings))
        lines.append("")
    lines += [f"Clipped from: {source.get('title', 'this video')} — "
              f"{source.get('channel', 'unknown channel')}",
              f"Source: {source.get('url', '')}",
              "Full credit to the original creator."]
    return "\n".join(lines)


def _parts_checklist() -> str:
    """Compact Studio walkthrough for a part upload (tested)."""
    return (
        "# Upload checklist — part\n\n"
        "- [ ] **Title** — paste from `TITLE.txt`\n"
        "- [ ] **Description** — paste from `DESCRIPTION.txt` **before "
        "publishing**.\n"
        "      It credits the source video — that attribution is what\n"
        "      separates a series part from a reupload if YouTube ever "
        "reviews the channel.\n"
        "- [ ] **Upload** — the .mp4 in this folder, exactly as rendered "
        "(subtitles burned in)\n"
        "- [ ] **Captions** — *Subtitles* → *Add* → *Upload file* → "
        "`captions.srt` (search + accessibility)\n"
        "- [ ] **Cross-post** — `tiktok.txt` / `reels.txt` captions are "
        "ready\n"
        "- [ ] Post parts in order, one per day — the series numbering "
        "only works as a sequence\n")


def build_part_srt(words_in_part: list[dict], part_len: float,
                   dest: Path) -> Path | None:
    """captions.srt for one part (single pseudo-scene over build_cues).

    None when the part has no words (a music-only stretch) — the kit
    simply ships without captions rather than with a degenerate file.
    """
    from subtitles import build_cues, max_chars_for, write_srt

    if not words_in_part:
        return None
    narration = " ".join(str(w.get("word") or "") for w in words_in_part)
    timings = [(float(w.get("start") or 0.0), float(w.get("end") or 0.0))
               for w in words_in_part]
    cues = build_cues([narration], [timings], [0.0], [float(part_len)],
                      max_chars_for("portrait"), 0.0)
    if not cues:
        return None
    return write_srt(cues, dest)


def write_part_kit(clip: Path, title: str, index: int, total: int,
                   source: dict, window: tuple[float, float],
                   siblings: list[str], srt_path: Path | None,
                   ass_path: Path | None, out_root: Path) -> Path:
    """Upload-style kit for one part: mp4 + titles + credit + captions."""
    from clipper import clip_platform_caption

    kit = out_root / clip.stem
    kit.mkdir(parents=True, exist_ok=True)
    shutil.copy2(clip, kit / clip.name)
    (kit / "TITLE.txt").write_text(title, encoding="utf-8")
    (kit / "DESCRIPTION.txt").write_text(
        build_part_description(title, index, total, source, siblings or []),
        encoding="utf-8")
    (kit / "CREDIT.txt").write_text(
        f"source: {source.get('url', '')}\n"
        f"channel: {source.get('channel', '')}\n"
        f"window: {window[0]:.1f}s - {window[1]:.1f}s\n"
        f"part: {index}/{total}\n",
        encoding="utf-8")
    (kit / "tiktok.txt").write_text(
        clip_platform_caption(title, "tiktok"), encoding="utf-8")
    (kit / "reels.txt").write_text(
        clip_platform_caption(title, "reels"), encoding="utf-8")
    if srt_path and Path(srt_path).exists():
        shutil.copy2(srt_path, kit / "captions.srt")
    if ass_path and Path(ass_path).exists():
        shutil.copy2(ass_path, kit / "part.ass")
    (kit / "CHECKLIST.md").write_text(_parts_checklist(), encoding="utf-8")
    return kit


# --------------------------------------------------------------------- lane
def run_parts(cfg, url: str = "", file: str = "",
              part_len: float | None = None,
              max_parts: int | None = None,
              sub_pos: str = "bottom",
              header: bool | None = None,
              out_dir: Path | None = None,
              keep_work: bool = False,
              dry_run: bool = False) -> int:
    """The whole lane. Returns process exit code."""
    from assembler import ffprobe_duration
    from clipper import (Candidate, ClipError, clip_words, download_source,
                         extract_audio, fix_transcript_words,
                         load_transcript_cache, new_clip_work_dir,
                         prune_stale_runs, render_clip, save_transcript_cache,
                         sentence_spans, transcript_cache_key,
                         transcript_cache_path, transcribe_words)

    if not url and not file:
        raise ClipError("give me --url <youtube link> or --file <local mp4>")
    target = PART_LEN_DEFAULT if part_len is None else part_len
    try:
        target = float(target)
    except (TypeError, ValueError):
        target = PART_LEN_DEFAULT
    cap = cfg.parts_max_parts if max_parts is None else max_parts
    want_header = cfg.parts_header if header is None else header
    out_root = Path(out_dir) if out_dir else cfg.root / "parts"
    work = new_clip_work_dir(cfg.work_dir)
    prune_stale_runs(cfg.work_dir, keep=work)
    work.mkdir(parents=True, exist_ok=True)

    if url:
        src, source = download_source(
            url, work,
            cookies_browser=cfg.clip_cookies_browser,
            cookies_file=cfg.clip_cookies_file)
    else:
        src = Path(file)
        if not src.exists():
            raise ClipError(f"file not found: {src}")
        source = {"title": src.stem, "channel": "local file", "url": "local"}

    duration = ffprobe_duration(src)
    print(f"  [parts] source: {src.name} ({duration / 60:.1f} min, "
          f"{source['channel']})")
    from scriptgen import get_provider

    provider = get_provider(cfg)
    source_key = transcript_cache_key(url, src)
    cache = transcript_cache_path(cfg, source_key)
    words = load_transcript_cache(cache)
    if words is not None:
        print(f"  [parts] transcript: cached ({len(words)} words)")
    else:
        audio = extract_audio(src, work)
        words = transcribe_words(audio, cfg, title=source["title"])
        if cfg.clip_transcript_fix:
            fixed = fix_transcript_words(words, source["title"], provider)
            changed = sum(1 for a, b in zip(words, fixed)
                          if a.get("word") != b.get("word"))
            if changed:
                print(f"  [parts] transcript fix: {changed} misheard "
                      f"word(s) corrected")
                words = fixed
        save_transcript_cache(cache, words)
        print(f"  [parts] transcript: {len(words)} words (cached for re-runs)")
    # Mask AFTER the cache (clip-lane rule): caches keep true words, every
    # consumer below — subs, header, titles — sees masked words.
    if cfg.subtitles_mask_profanity:
        from subtitles import mask_profanity

        words = [dict(w, word=mask_profanity(str(w.get("word") or "")))
                 for w in words]
    # Unlike clips, parts never need mineable moments: a thin transcript
    # just means time-based cuts and a header-only part, never a failure.
    spans = sentence_spans(words or [])
    word_starts = [float(w.get("start") or 0.0) for w in (words or [])]
    plan = plan_parts(duration, spans, target_len=target,
                      tail_merge=cfg.parts_tail_merge,
                      snap_window=cfg.parts_snap_window,
                      max_parts=cap, word_starts=word_starts)
    total = len(plan)
    print(f"  [parts] {total} part(s), ~{target:.0f}s each"
          + (" (single part — no header)" if total == 1 else ""))
    if dry_run:
        for num, (start, end) in enumerate(plan, start=1):
            print(f"    Part {num}: {start:.1f}s - {end:.1f}s ({end - start:.0f}s)")
        print("  [parts] dry run — nothing rendered, nothing spent.")
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)
        return 0

    out_root.mkdir(parents=True, exist_ok=True)
    stem = source_key[:8]
    siblings = [f"{stem}_part_{num:02d}.mp4" for num in range(1, total + 1)]
    kits = []
    for num, (start, end) in enumerate(plan, start=1):
        clip_path = out_root / siblings[num - 1]
        cand = Candidate(start=start, end=end, hook=f"Part {num}")
        length = end - start
        header_line = parts_header_line(
            str(source.get("title") or ""), num, total, length) \
            if want_header else ""
        # The clip renderer cuts, treats vertical, and burns — header rides
        # the same .ass, subs default to the bottom (shorts-safe).
        render_clip(src, cand, words or [], cfg, clip_path, work, sub_pos,
                    extra_ass=header_line or None)
        window = clip_words(words or [], start, end)
        srt_path = build_part_srt(window, length, work / f"part_{num:02d}.srt")
        ass_src = work / "clip.ass"
        title = part_kit_title(str(source.get("title") or ""), num, total)
        kits.append(write_part_kit(
            clip_path, title, num, total, source, (start, end), siblings,
            srt_path, ass_src if ass_src.exists() else None, out_root))
        print(f"  [parts] {clip_path.name}: {title!r}")
    print(f"\n  {len(kits)} part(s) + kits -> {out_root}")
    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)
    return 0
