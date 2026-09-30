"""Long-form lane: any video -> ONE landscape 16:9 video with subtitles.

    python main.py longform <youtube-link-or-file>
    python main.py longform <link> --minutes 8      # cut an ~8-min episode
    python main.py longform <link> --start 300      # begin near 5:00
    python main.py longform <link> --dry-run        # plan only, $0

The clip lane makes vertical Shorts; this is its landscape sibling for
regular (long-form) uploads. Same ingest end to end (download once,
transcript once — the cache is shared with clip/parts, so cross-lane
re-runs are free), then:

  * geometry: landscape sources scale+pad to 1920x1080 (nothing is
    cropped away — the opposite of the clip lane's vertical crop);
    portrait sources get the blurred-fill treatment in a landscape
    frame (the mirror of the clip lane's fit filter).
  * cutting: default renders the WHOLE source. --minutes N cuts one
    episode of ~N minutes, opened at --start (default 0:00) and closed
    at a sentence end / pause — the same cutpoint machinery as parts
    and --half (the eos pass is cached next to the transcript).
  * subtitles: karaoke captions at the bottom, landscape sizing (the
    engine has shipped landscape styling since the generate lane).
    --no-subs skips the burn; the kit keeps the .srt either way.
  * chapters: YouTube chapter timestamps in DESCRIPTION.txt, marks at
    real pauses/sentence boundaries, labels = the first words actually
    spoken in each chapter (no API call). --no-chapters skips.
  * kit: TITLE/DESCRIPTION/CREDIT/captions.srt/longform.ass + three
    candidate thumbnail frames, all attribution-carrying like clip kits.

Deliberately NOT part of v1 (see API-REPORT.md): LLM moment-picking at
minute scale, multi-episode batches, title polish. The lane runs on
0 LLM calls when the transcript is cached or harvested from captions,
one cached eos pass (+ the transcript-fix pass for fresh Whisper
sources) otherwise.

Files land in longform/ (not clips/), so the Telegram phone queue
ignores them by design: long-form mp4s blow past Telegram's 50 MB bot
cap anyway.
"""

from __future__ import annotations

import shutil
from pathlib import Path

LONGFORM_MAX_MINUTES_DEFAULT = 20.0   # whole sources longer than this warn
LONGFORM_MIN_MINUTES = 1.0
LONGFORM_MAX_MINUTES_CAP = 120.0
LONGFORM_WHOLE_SLACK = 30.0           # source within this of the target: whole
LONGFORM_TAIL_MERGE = 15.0            # a remaining tail shorter than this joins the episode
LANDSCAPE_W, LANDSCAPE_H = 1920, 1080
WIDE_RATIO = 1.2                      # wider than this pads; narrower blurs-fill
CHAPTER_SECONDS_DEFAULT = 120.0       # ideal distance between chapter marks
CHAPTER_MIN_GAP_DEFAULT = 60.0        # two marks never closer than this
CHAPTER_MIN_COUNT = 3                 # YouTube engages chapters at 3+ marks
CHAPTER_LABEL_CHARS = 42              # chapter label width in the description
LONGFORM_TITLE_LIMIT = 95             # YouTube title ceiling is 100; stay under


# ---------------------------------------------------------------- geometry
def landscape_treatment(width: int, height: int) -> str:
    """How a source becomes landscape: pad | fit (pure, tested).

    pad: source already wide (>= 1.2:1) — scale to fit inside 1920x1080
    and pad the slivers. Nothing is ever cropped.
    fit: portrait/square source — the whole frame centered over a
    blurred, darkened copy of itself filling the 1920x1080 canvas.
    """
    if height <= 0:
        return "pad"
    return "pad" if width / height >= WIDE_RATIO else "fit"


def landscape_pad_filter() -> str:
    """Wide source -> 1920x1080, aspect kept, tiny pad bars (pure, tested)."""
    return (f"scale={LANDSCAPE_W}:{LANDSCAPE_H}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={LANDSCAPE_W}:{LANDSCAPE_H}:(ow-iw)/2:(oh-ih)/2")


def landscape_fit_filter() -> str:
    """Portrait source -> whole frame over blurred fill, landscape (tested).

    The mirror of the clip lane's fit_filter: background scaled to cover
    the 1920x1080 canvas + blur + darken, foreground scaled to full
    height, centered.
    """
    return (f"split=2[bg][fg];"
            f"[bg]scale={LANDSCAPE_W}:{LANDSCAPE_H}:"
            "force_original_aspect_ratio=increase,"
            f"crop={LANDSCAPE_W}:{LANDSCAPE_H},boxblur=20:2,"
            "eq=brightness=-0.15[bgf];"
            "[fg]scale=-2:1080[fgf];"
            "[bgf][fgf]overlay=(W-w)/2:(H-h)/2")


# ----------------------------------------------------------------- planning
def plan_longform_window(duration: float, target: float | None,
                         start_at: float = 0.0,
                         bounds: list | None = None,
                         word_starts: list | None = None
                         ) -> tuple[float, float, bool]:
    """(start, end, whole) for one long-form (pure, tested).

    whole (target None/<=0, or the source fits the target + slack):
    (0, duration). Otherwise one episode: start = --start clamped so the
    full target still fits (and snapped to a cutpoint when it isn't 0),
    end = the best cutpoint near start+target, bounded to 75-125% of the
    target, extended to the source end when only a sliver would remain.
    """
    duration = max(0.0, float(duration or 0.0))
    if duration <= 0:
        return (0.0, 0.0, True)
    if target is None or target <= 0 or duration <= target + LONGFORM_WHOLE_SLACK:
        return (0.0, duration, True)
    bounds = bounds or []
    latest_start = max(0.0, duration - target)
    desired = min(max(0.0, float(start_at or 0.0)), latest_start)
    if desired > 0 and bounds:
        # Open on a boundary near the requested start — never at 0,
        # which would drop the video's own opening words. The snap may
        # not carry start past latest_start, or the target no longer fits.
        from cutpoints import pick_cut

        start, _ = pick_cut(desired, max(0.0, desired - 15.0),
                            min(desired + 15.0, latest_start),
                            10.0, bounds, word_starts)
    else:
        start = desired
    lo = start + max(60.0, target * 0.75)
    hi = min(duration, start + target * 1.25)
    if lo >= hi:                      # degenerate remainder: take it all
        return (start, duration, False)
    from cutpoints import pick_cut

    end, _ = pick_cut(start + target, lo, hi, 30.0, bounds, word_starts)
    if duration - end < LONGFORM_TAIL_MERGE:
        end = duration                # a <15s tail isn't worth a cut
    return (start, end, False)


def _chapter_label(words: list[dict], after: float) -> str:
    """First words spoken after `after`, as a short label (pure, tested)."""
    picked: list[str] = []
    for word in words:
        if float(word.get("start") or 0.0) >= after - 1e-6:
            text = str(word.get("word") or "").strip(" ,.!?;:\"'“”’—-")
            if text:
                picked.append(text)
            if len(picked) >= 6:
                break
    if not picked:
        return ""
    label = " ".join(picked)
    if len(label) > CHAPTER_LABEL_CHARS:
        label = label[:CHAPTER_LABEL_CHARS - 1].rstrip() + "…"
    return label


def chapter_marks(duration: float, words: list[dict] | None,
                  bounds: list | None, title: str = "",
                  chapter_seconds: float = CHAPTER_SECONDS_DEFAULT,
                  min_gap: float = CHAPTER_MIN_GAP_DEFAULT
                  ) -> list[tuple[float, str]]:
    """YouTube chapter marks [(time, label)], [] when too few (pure, tested).

    First mark is always 0:00 (YouTube's rule), labelled with the title.
    Later marks sit on the best cutpoint near each chapter_seconds
    multiple, never closer together than min_gap, never in the last
    min_gap of the video. Labels are the first words spoken in each
    chapter — informative, and free. Fewer than CHAPTER_MIN_COUNT marks
    means YouTube would ignore them anyway: [].
    """
    duration = max(0.0, float(duration or 0.0))
    chapter_seconds = max(30.0, float(chapter_seconds or 0.0))
    min_gap = max(15.0, float(min_gap or 0.0))
    if duration < 2 * chapter_seconds or duration < 3 * min_gap:
        return []
    from cutpoints import pick_cut

    marks: list[tuple[float, str]] = [(0.0, _clean_title(title) or "Start")]
    words = words or []
    bounds = bounds or []
    starts = [float(w.get("start") or 0.0) for w in words]
    ideal = chapter_seconds
    while ideal < duration - min_gap:
        lo = marks[-1][0] + min_gap
        hi = duration - min_gap
        if lo >= hi:
            break
        cut, _ = pick_cut(ideal, lo, hi, min(30.0, chapter_seconds / 2),
                          bounds, starts)
        if cut <= marks[-1][0] + min_gap or cut > duration - min_gap:
            ideal += chapter_seconds
            continue
        label = _chapter_label(words, cut)
        if not label:
            ideal += chapter_seconds
            continue
        marks.append((round(cut, 3), label))
        ideal = max(ideal + chapter_seconds, cut + min_gap)
    return marks if len(marks) >= CHAPTER_MIN_COUNT else []


def fmt_timestamp(seconds: float) -> str:
    """YouTube chapter stamp: 0:00, 1:05, 1:02:05 (pure, tested)."""
    seconds = max(0, int(round(float(seconds or 0.0))))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# -------------------------------------------------------------------- title
def _clean_title(text: str) -> str:
    from parts import _clean_title as clean

    return clean(text)


def clean_long_title(title: str, limit: int = LONGFORM_TITLE_LIMIT) -> str:
    """Source title, bracket junk stripped, under the YouTube ceiling."""
    text = _clean_title(title or "")
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text or "Long-form cut"


# ---------------------------------------------------------------------- kit
def build_longform_description(title: str, chapters: list[tuple[float, str]],
                               source: dict, window: tuple[float, float],
                               cut: bool = False) -> str:
    """DESCRIPTION.txt: title, chapters, attribution (pure, tested)."""
    lines = [title, ""]
    if chapters:
        lines += [f"{fmt_timestamp(t)}  {label}"
                  for t, label in chapters] + [""]
    lines += [f"Clipped from: {_clean_title(source.get('title')) or 'this video'}"
              f" — {_clean_title(source.get('channel')) or 'unknown channel'}",
              f"Source: {source.get('url', '')}",
              "Full credit to the original creator."]
    if cut:
        lines.append(f"Window: {fmt_timestamp(window[0])} - "
                     f"{fmt_timestamp(window[1])} of the source.")
    return "\n".join(lines)


def _longform_checklist() -> str:
    """Studio walkthrough for a long-form upload (tested)."""
    return (
        "# Upload checklist — long-form\n\n"
        "- [ ] **This is NOT a Short** — upload exactly as rendered; YouTube\n"
        "      shelves it by the 16:9 file, nothing to switch in Studio.\n"
        "- [ ] **Title** — paste from `TITLE.txt`\n"
        "- [ ] **Description** — paste from `DESCRIPTION.txt` **before "
        "publishing**.\n      It carries the chapter timestamps AND the "
        "source attribution — both matter.\n"
        "- [ ] **Chapters** — already in the description (YouTube engages "
        "them at 3+\n      marks, starting 0:00). Leave the lines untouched.\n"
        "- [ ] **Captions** — in Studio, Subtitles -> Add -> upload file ->\n"
        "      `captions.srt` from this kit (the burn-in is already in the "
        "video; this\n      adds the searchable/closed-caption track).\n"
        "- [ ] **Thumbnail** — pick one of `THUMB_1/2/3.jpg` (frames from "
        "the source,\n      subtitle-free), or grab your own frame.\n"
        "- [ ] **Category** — match the source's own category (usually "
        "Entertainment\n      or Education).\n"
        "- [ ] `CREDIT.txt` keeps the source link and the exact window "
        "for your records\n")


def write_longform_kit(video: Path, title: str, source: dict,
                       window: tuple[float, float],
                       chapters: list[tuple[float, str]],
                       srt_path: Path | None, ass_path: Path | None,
                       thumbs: list[Path], out_root: Path,
                       cut: bool = False) -> Path:
    """Upload-style kit for one long-form video (tested)."""
    kit = out_root / video.stem
    kit.mkdir(parents=True, exist_ok=True)
    shutil.copy2(video, kit / video.name)
    (kit / "TITLE.txt").write_text(title, encoding="utf-8")
    (kit / "DESCRIPTION.txt").write_text(
        build_longform_description(title, chapters, source, window, cut=cut),
        encoding="utf-8")
    (kit / "CREDIT.txt").write_text(
        f"source: {source.get('url', '')}\n"
        f"channel: {source.get('channel', '')}\n"
        f"window: {window[0]:.1f}s - {window[1]:.1f}s "
        f"({window[1] - window[0]:.1f}s)\n",
        encoding="utf-8")
    if srt_path and Path(srt_path).exists():
        shutil.copy2(srt_path, kit / "captions.srt")
    if ass_path and Path(ass_path).exists():
        shutil.copy2(ass_path, kit / "longform.ass")
    for index, thumb in enumerate(thumbs, start=1):
        if thumb and Path(thumb).exists():
            shutil.copy2(thumb, kit / f"THUMB_{index}.jpg")
    (kit / "CHECKLIST.md").write_text(_longform_checklist(), encoding="utf-8")
    return kit


# ------------------------------------------------------------------ captions
def build_longform_srt(words_in_window: list[dict], length: float,
                       dest: Path, caps: bool = False) -> Path | None:
    """captions.srt for the kit, landscape line width (tested)."""
    from subtitles import build_cues, max_chars_for, write_srt

    if not words_in_window:
        return None
    narration = " ".join(str(w.get("word") or "") for w in words_in_window)
    timings = [(float(w.get("start") or 0.0), float(w.get("end") or 0.0))
               for w in words_in_window]
    cues = build_cues([narration], [timings], [0.0], [float(length)],
                      max_chars_for("landscape"), 0.0, caps=caps)
    if not cues:
        return None
    return write_srt(cues, dest)


# ------------------------------------------------------------------ rendering
def render_longform(src: Path, window: tuple[float, float],
                    words: list[dict], cfg, out_path: Path, work: Path,
                    sub_pos: str = "bottom", subs: bool = True) -> Path:
    """Cut + landscape-treat + burn subtitles -> one 16:9 video.

    cfg must already report format="landscape" (run_longform sets and
    restores it) so the .ass geometry and burn styling come out right.
    """
    import subprocess

    from assembler import resolve_encoder_args
    from clipper import build_clip_ass, clip_words, probe_dims
    from subtitles import filter_args, sub_style_from_cfg

    start, end = window
    length = end - start
    in_window = clip_words(words, start, end)
    if subs:
        style = sub_style_from_cfg(cfg, sub_pos)
        ass_path = build_clip_ass(in_window, length, cfg, work, style)
        sub_args = filter_args(ass_path, "landscape")
    else:
        ass_path = sub_args = None
    width, height = probe_dims(src)
    treatment = landscape_treatment(width, height)
    if treatment == "fit":
        print("  [longform] fit: portrait source — whole frame over "
              "blurred landscape fill")
        base = landscape_fit_filter()
    else:
        print("  [longform] pad: landscape source — scaled to 1920x1080, "
              "nothing cropped")
        base = landscape_pad_filter()
    vf = base + ("," + sub_args if sub_args else "")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(src),
        "-vf", vf,
        *resolve_encoder_args(cfg.encoder),
        "-threads", "4", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out_path


def grab_thumbnails(src: Path, window: tuple[float, float], work: Path
                    ) -> list[Path]:
    """Three subtitle-free frames (25/50/75%) as thumbnail candidates."""
    from clipper import extract_frame

    start, end = window
    length = end - start
    out: list[Path] = []
    for frac in (0.25, 0.5, 0.75):
        dest = work / f"thumb_{int(frac * 100)}.jpg"
        try:
            extract_frame(src, start + length * frac, dest)
            out.append(dest)
        except Exception:  # noqa: BLE001 - a thumbnail is a bonus, never fatal
            continue
    return out


# ---------------------------------------------------------------------- lane
def longform_stem(source_key: str, window: tuple[float, float],
                  duration: float) -> str:
    """File stem: whole sources _longform, cut windows _longform_5m00s."""
    start, end = window
    if start <= 0.01 and end >= duration - 0.01:
        return f"{source_key[:8]}_longform"
    mins, secs = divmod(int(round(start)), 60)
    return f"{source_key[:8]}_longform_{mins}m{secs:02d}s"


def _shift_bounds(bounds: list, offset: float) -> list:
    """Boundaries shifted into window-relative time (pure)."""
    return [(t - offset, tier) for t, tier in bounds if t > offset]


def run_longform(cfg, url: str = "", file: str = "",
                 minutes: float | None = None,
                 start_at: float = 0.0,
                 sub_pos: str = "bottom", subs: bool = True,
                 chapters: bool | None = None,
                 dry_run: bool = False, keep_work: bool = False,
                 out_dir: Path | None = None) -> int:
    """The whole lane. Returns process exit code."""
    from clipper import (ClipError, ingest_transcript, new_clip_work_dir,
                         prune_stale_runs, transcript_cache_key,
                         transcript_cache_path)
    from assembler import ffprobe_duration

    if not url and not file:
        raise ClipError("give me --url <youtube link> or --file <local mp4>")
    target: float | None = None
    if minutes is not None:
        try:
            target = float(minutes) * 60.0
        except (TypeError, ValueError):
            target = None
        if target is not None and target < LONGFORM_MIN_MINUTES * 60.0:
            raise ClipError(f"--minutes must be at least "
                            f"{LONGFORM_MIN_MINUTES:.0f}")
    start_at = max(0.0, float(start_at or 0.0))
    out_root = Path(out_dir) if out_dir else cfg.root / "longform"
    work = new_clip_work_dir(cfg.work_dir)
    prune_stale_runs(cfg.work_dir, keep=work)
    work.mkdir(parents=True, exist_ok=True)

    if url:
        from clipper import download_source

        src, source = download_source(
            url, work, cookies_browser=cfg.clip_cookies_browser,
            cookies_file=cfg.clip_cookies_file)
    else:
        src = Path(file)
        if not src.exists():
            raise ClipError(f"file not found: {src}")
        source = {"title": src.stem, "channel": "local file", "url": "local"}

    duration = ffprobe_duration(src)
    if duration <= 0:
        raise ClipError("could not read the source's duration")
    print(f"  [longform] source: {src.name} ({duration / 60:.1f} min, "
          f"{source['channel']})")
    if start_at >= duration:
        raise ClipError(f"--start {start_at:.0f}s is past the end of the "
                        f"source ({duration:.0f}s)")

    source_key = transcript_cache_key(url, src)
    cache = transcript_cache_path(cfg, source_key)
    words = ingest_transcript(cfg, src, source, url, work, lane="longform")
    if cfg.subtitles_mask_profanity:
        from subtitles import mask_profanity

        words = [dict(w, word=mask_profanity(str(w.get("word") or "")))
                 for w in words]

    # ---- plan the window
    bounds: list = []
    is_cut = False
    if target and duration > target + LONGFORM_WHOLE_SLACK:
        from cutpoints import (TIER_NAMES, build_boundaries, detect_silences,
                               ensure_sentence_ends)
        from scriptgen import get_provider

        provider = get_provider(cfg)
        words = ensure_sentence_ends(
            words or [], str(source.get("title") or ""), provider, cache=cache)
        bounds = build_boundaries(words, detect_silences(src, duration),
                                  duration)
        word_starts = [float(w.get("start") or 0.0) for w in (words or [])]
        start_w, end_w, _whole = plan_longform_window(
            duration, target, start_at, bounds, word_starts)
        window = (start_w, end_w)
        is_cut = window[1] < duration - 0.01 or window[0] > 0.01
        if window[1] < duration - 0.01:
            tier = dict(bounds).get(round(window[1], 3), 0)
            how = TIER_NAMES.get(tier, "fallback (no sentence end nearby)")
            print(f"  [longform] episode: {fmt_timestamp(window[0])} - "
                  f"{fmt_timestamp(window[1])} "
                  f"({(window[1] - window[0]) / 60:.1f} min), closing on a "
                  f"{how}")
        if start_at > 0:
            if window[0] <= 0.01:
                print("  [longform] note: --start clamped to 0 so the "
                      "episode fits")
            elif abs(window[0] - start_at) > 0.5:
                print(f"  [longform] opening snapped to a sentence end at "
                      f"{fmt_timestamp(window[0])}")
    else:
        window = (0.0, float(duration))
        if target:
            print(f"  [longform] {duration / 60:.1f} min source fits the "
                  f"{target / 60:.0f}-min target — kept whole")
        else:
            print(f"  [longform] whole source: {duration / 60:.1f} min"
                  + (f" (over the {cfg.longform_max_minutes:.0f}-min default "
                     "cap — fine to post, --minutes N to cut an episode)"
                     if duration > cfg.longform_max_minutes * 60 else ""))
    length = window[1] - window[0]

    # ---- chapters (cutpoint machinery already in bounds; $0, no LLM)
    want_chapters = cfg.longform_chapters if chapters is None else chapters
    marks: list[tuple[float, str]] = []
    if want_chapters and length >= 2 * cfg.longform_chapter_seconds:
        from clipper import clip_words

        if not bounds:
            from cutpoints import build_boundaries, detect_silences

            bounds = build_boundaries(
                words or [], detect_silences(src, duration), duration)
        in_window = clip_words(words or [], window[0], window[1])
        marks = chapter_marks(
            length, in_window, _shift_bounds(bounds, window[0]),
            title=clean_long_title(str(source.get("title") or "")),
            chapter_seconds=cfg.longform_chapter_seconds,
            min_gap=cfg.longform_chapter_min_gap)
        if marks:
            print(f"  [longform] {len(marks)} chapter marks "
                  f"(in DESCRIPTION.txt)")
    if dry_run:
        print(f"  [longform] dry run: would render "
              f"{fmt_timestamp(window[0])} - {fmt_timestamp(window[1])} "
              f"({length / 60:.1f} min, 1920x1080)")
        for t, label in marks:
            print(f"    {fmt_timestamp(t)}  {label}")
        print("  [longform] dry run — nothing rendered, nothing spent.")
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)
        return 0

    # ---- render (landscape cfg, restored even on failure)
    out_root.mkdir(parents=True, exist_ok=True)
    stem = longform_stem(source_key, window, duration)
    out_path = out_root / f"{stem}.mp4"
    video_cfg = cfg.data["video"]
    original_format = video_cfg.get("format")
    try:
        video_cfg["format"] = "landscape"
        from clipper import clip_words

        in_window = clip_words(words or [], window[0], window[1])
        render_longform(src, window, words or [], cfg, out_path, work,
                        sub_pos, subs=subs)
        srt_path = build_longform_srt(in_window, length,
                                      work / "longform.srt",
                                      caps=cfg.subtitles_caps)
    finally:
        if original_format is None:
            video_cfg.pop("format", None)
        else:
            video_cfg["format"] = original_format
    ass_path = work / "clip.ass" if subs and (work / "clip.ass").exists() \
        else None
    thumbs = grab_thumbnails(src, window, work)
    title = clean_long_title(str(source.get("title") or ""))
    kit = write_longform_kit(out_path, title, source, window, marks,
                             srt_path, ass_path, thumbs, out_root,
                             cut=is_cut)
    print(f"  [longform] {out_path.name}: {title!r} "
          f"({length / 60:.1f} min, 1920x1080)")
    print(f"\n  1 long-form + kit -> {kit}")
    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)
    return 0
