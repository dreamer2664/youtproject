"""Build upload-ready kits for manual YouTube upload.

`python main.py package` turns each generated video into a folder:

    upload/<job-id>/
        video.mp4          drag this into youtube.com/upload
        thumbnail.jpg      upload as the custom thumbnail
        title.txt          copy-paste (100 chars max, enforced)
        description.txt    copy-paste (AI-tools note appended when enabled)
        tags.txt           copy-paste (comma-separated, <= 500 chars)
        captions.srt       upload under Subtitles in Studio (when present)
        CHECKLIST.md       YouTube Studio steps + human review reminders

This local packaging step does not touch YouTube's API or upload anything.
Manual upload through Studio uses no API upload client; any optional browser
automation is a separate access method and is not made policy-approved by
this package builder.
"""

from __future__ import annotations

import json
import ntpath
import re
import shutil
import uuid
from pathlib import Path

from config import Config
from jobqueue import Job

YOUTUBE_TITLE_LIMIT = 100
YOUTUBE_TAG_LIMIT = 500  # total characters across all tags

CATEGORY_NAMES = {
    "1": "Film & Animation",
    "2": "Autos & Vehicles",
    "10": "Music",
    "15": "Pets & Animals",
    "17": "Sports",
    "19": "Travel & Events",
    "20": "Gaming",
    "22": "People & Blogs",
    "23": "Comedy",
    "24": "Entertainment",
    "25": "News & Politics",
    "26": "Howto & Style",
    "27": "Education",
    "28": "Science & Technology",
}


def fit_tags(tags: list[str], limit: int = YOUTUBE_TAG_LIMIT) -> list[str]:
    """Drop trailing tags until the comma-joined string fits YouTube's limit."""
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = YOUTUBE_TAG_LIMIT
    if isinstance(tags, str):
        tags = [tag.strip() for tag in tags.split(",")]
    if not isinstance(tags, (list, tuple)):
        return []
    tags = [str(tag).strip() for tag in tags if tag is not None and str(tag).strip()]
    while tags and len(", ".join(tags)) > limit:
        tags.pop()
    return tags


def build_description(meta: dict, cfg: Config) -> str:
    """Video description plus the optional AI-tools transparency note."""
    description = str(meta.get("description") or "").strip()
    if cfg.disclosure_enabled and cfg.disclosure_text:
        if cfg.disclosure_text not in description:
            description = f"{description}\n\n---\n{cfg.disclosure_text}".strip()
    return description


def _format_line(meta: dict) -> str:
    fmt = str(meta.get("format", "landscape"))
    width = meta.get("width", "")
    height = meta.get("height", "")
    try:
        duration = float(meta.get("duration_seconds", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0
    dims = f"{width}x{height}" if width and height else fmt
    return f"{fmt} {dims}, {duration:.0f}s"


def build_checklist(job: Job, meta: dict, cfg: Config, kit: Path) -> str:
    """Per-video Studio walkthrough with this video's values prefilled."""
    title = str(meta.get("title", "") or "")
    title_alt = str(meta.get("title_alt") or "").strip()
    tags = fit_tags(meta.get("tags") or [])
    category = CATEGORY_NAMES.get(str(meta.get("categoryId", "")), "see dropdown in Studio")
    description = build_description(meta, cfg)
    fmt = str(meta.get("format", "landscape"))
    try:
        duration = float(meta.get("duration_seconds") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    burned_in = bool(meta.get("subtitles_burned_in"))
    has_srt = bool(meta.get("subtitle_file"))
    has_thumbnail = (kit / "thumbnail.jpg").is_file()

    is_short = fmt == "portrait" and duration <= 180
    shorts_note = ""
    if is_short:
        shorts_note = (
            "\n> This video is vertical and under 3 minutes, so YouTube shelves it\n"
            "> as a **Short** automatically. Shorts thumbnails are picked from a\n"
            "> freeze-frame in the mobile app — `thumbnail.jpg` is still used\n"
            "> anywhere the video shows as a regular video.\n"
        )

    if burned_in:
        subs_note = (
            "Subtitles are **burned into the picture** already. Uploading the\n"
            "`.srt` as well still helps: viewers can turn captions off, and\n"
            "YouTube indexes the text for search."
        )
    elif has_srt:
        subs_note = (
            "Subtitles are **not** burned into this video (your FFmpeg build\n"
            "lacks the subtitle filter), so uploading the `.srt` below is what\n"
            "gives viewers captions. Do not skip it."
        )
    else:
        subs_note = "This video was generated with subtitles off — nothing to do here."

    if has_thumbnail:
        thumbnail_step = (
            "- [ ] **Thumbnail** — Upload File → pick `thumbnail.jpg` from this folder.\n"
            "      (Custom thumbnails need a verified phone number on the channel — one-time,\n"
            "      free, at <https://youtube.com/verify>.)"
        )
    else:
        thumbnail_step = (
            "- [ ] **Thumbnail** — no custom thumbnail was included in this kit;\n"
            "      choose a suitable Studio frame or create one before publishing."
        )

    captions_step = ""
    if has_srt:
        captions_step = (
            "## 4. Captions\n\n"
            f"{subs_note}\n\n"
            "- [ ] In Studio's left menu open **Subtitles** → pick this video →\n"
            "      *Add* → *Upload file* → choose `captions.srt` from this folder.\n\n"
        )
    ab_note = ""
    if title_alt:
        ab_note = (
            "\n> **A/B title test (two channels):** upload to channel 1 with\n"
            "> `title.txt`, and the same video to channel 2 with `title-b.txt`:\n"
            f"> - A: {title}\n"
            f"> - B: {title_alt}\n"
            "> After 72h run `python main.py snap` and keep whichever title\n"
            "> earned more views on both channels.\n"
        )

    n_aud = 5 if has_srt else 4
    n_after = 6 if has_srt else 5

    return f"""# Upload checklist — {job.id}

Video: **{title}**
Format: {_format_line(meta)}
{shorts_note}{ab_note}
Follow top to bottom. Takes about 3 minutes.

## 1. Upload the file

1. Open <https://youtube.com/upload> (logged into your channel).
2. Drag in `{job.id}/video.mp4` from this folder.
3. While it processes, fill in the fields below — copy from the `.txt` files
   in this folder, they are already within YouTube's limits.

## 2. Details tab

- [ ] **Title** — paste from `title.txt` ({len(title)}/100 characters).
- [ ] **Description** — paste from `description.txt`. Its AI-tools note is an
      optional transparency note; it does not replace the Studio AI-use answer.
- [ ] Review the script and video against reliable sources. Any AI review in
      this project is not independent source verification; confirm names, dates,
      numbers, quotations, and whether the visuals match the narration.
{thumbnail_step}
- [ ] **Tags** — paste from `tags.txt` ({len(tags)} tags,
      {len(", ".join(tags))}/500 characters). Tags live under *Show more*.
- [ ] **Language** — `{meta.get("language", cfg.language)}`.
- [ ] **Category** — `{category}` (under *Show more*).

## 3. YouTube Studio's AI-use setting — decide for THIS video

Answer based on the finished video, not simply because an AI tool was used.
YouTube requires disclosure when AI generates or meaningfully alters realistic
content that could mislead viewers (for example, a realistic scene that did not
happen, altered footage of a real event/place, or a real person made to appear
to say or do something they did not). Clearly fantastical/stylized content and
minor production assistance may not require it. Review YouTube's current help
page and select **Yes** only when the video's content meets its criteria.

- [ ] Check the complete video and choose Yes/No in Studio's **AI use**
      attribute (or the equivalent label shown in your Studio version).

Full guidance: see `UPLOAD-GUIDE.md` and YouTube's official disclosure help.

{captions_step}## {n_aud}. Audience + visibility

- [ ] **Audience** — *No, it's not made for kids* (unless it genuinely is —
      see UPLOAD-GUIDE.md before answering Yes).
- [ ] **Visibility** — Public + *Publish now*, or Schedule it. Scheduling one
      video per day at the same hour beats dumping five at once.

## {n_after}. After publishing

Back in the terminal, record the URL so the queue stays accurate:

```
python main.py published {job.id} https://youtu.be/PASTE-ID-HERE
```

---

<details><summary>What YouTube sees (for reference)</summary>

**Title**
```
{title}
```

**Description**
```
{description}
```

**Tags**
```
{", ".join(tags)}
```

</details>
"""


_HASH_TITLE = re.compile(r"^[0-9a-f]{6,64}$")
_HASHTAG = re.compile(r"#\S+")


def clean_title(raw: str, topic: str) -> str:
    """Final YouTube title: no hashtags, never a bare hash/ID (pure, tested).

    The LLM loves appending "#facts #viral" to titles (they then get cut
    off mid-tag by the 100-char limit), and one 2026-09-14 video shipped
    with a bare job-id hash as its title. Both are packaging malpractice,
    so both are fixed here: hashtags stripped, hash-like / empty titles
    rebuilt from the topic. Callers print when the guard triggers.
    """
    title = _HASHTAG.sub("", raw or "").strip()
    title = re.sub(r"\s+", " ", title)
    if (not title or title.lower() == "untitled"
            or _HASH_TITLE.match(title.replace(" ", ""))):
        fallback = re.sub(r"\s+", " ", (topic or "")).strip()
        if fallback:
            fallback = fallback[0].upper() + fallback[1:]
        return fallback[:YOUTUBE_TITLE_LIMIT] or "Untitled"
    return title[:YOUTUBE_TITLE_LIMIT]


def _safe_job_id(value: str) -> str:
    """Reject path-bearing IDs before using them as a package directory."""
    job_id = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
        raise ValueError(f"unsafe job id for upload kit: {job_id!r}")
    return job_id


def _recover_kit(kit: Path) -> None:
    """Restore a previous kit if a process stopped during its final rename."""
    backups = sorted(
        kit.parent.glob(f".{kit.name}.backup-*"),
        key=lambda path: path.stat().st_mtime_ns if path.exists() else 0,
    )
    if not kit.exists() and backups:
        # The newest backup is the immediately preceding complete kit.
        for backup in reversed(backups):
            try:
                backup.rename(kit)
                break
            except OSError:
                continue
        if not kit.exists():
            raise OSError(f"could not recover previous upload kit from {backups[-1]}")
    # A visible final kit is authoritative; leftovers are from an interrupted
    # cleanup after the new directory was committed.
    if kit.exists():
        for backup in backups:
            if backup.exists():
                _remove_path(backup)


def _remove_path(path: Path) -> None:
    """Best-effort cleanup for a generated staging/backup path."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _commit_kit(stage: Path, kit: Path) -> None:
    """Replace a kit only after every new file has been written successfully."""
    backup = None
    try:
        if kit.exists():
            backup = kit.with_name(f".{kit.name}.backup-{uuid.uuid4().hex}")
            kit.rename(backup)
        stage.rename(kit)
    except BaseException:
        if backup is not None and backup.exists() and not kit.exists():
            try:
                backup.rename(kit)
            except OSError as restore_error:
                raise RuntimeError(
                    "upload-kit replacement failed and the previous kit could "
                    f"not be restored automatically; it is preserved at {backup}"
                ) from restore_error
        raise
    if backup is not None:
        _remove_path(backup)


def build_package(job: Job, cfg: Config) -> Path:
    """Create/replace a manual YouTube upload kit without losing a good prior kit."""
    job_id = _safe_job_id(job.id)
    video_path = Path(job.video_file)
    meta_path = Path(job.meta_file)
    if not video_path.is_file():
        raise FileNotFoundError(f"video file missing: {video_path}")
    if video_path.stat().st_size == 0:
        raise ValueError(f"video file is empty: {video_path}")
    if not meta_path.is_file():
        raise FileNotFoundError(f"metadata file missing: {meta_path}")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise ValueError(f"metadata must be a JSON object: {meta_path}")
    raw_title = str(meta.get("title", "") or "")
    title = clean_title(raw_title, job.topic)
    if title != raw_title.strip()[:YOUTUBE_TITLE_LIMIT]:
        print(f"  [package] ⚠️ title guard: {raw_title!r} -> {title!r}")
    meta["title"] = title
    raw_alt = str(meta.get("title_alt") or "").strip()
    title_alt = clean_title(raw_alt, job.topic) if raw_alt else ""
    if title_alt == title:
        title_alt = ""
    meta["title_alt"] = title_alt
    raw_tags = meta.get("tags") or []
    if isinstance(raw_tags, str):
        raw_tags = [tag.strip() for tag in raw_tags.split(",")]
    if not isinstance(raw_tags, (list, tuple)):
        raw_tags = []
    tags = fit_tags(raw_tags)

    kit = cfg.package_dir / job_id
    kit.parent.mkdir(parents=True, exist_ok=True)
    _recover_kit(kit)
    stage = kit.with_name(f".{kit.name}.stage-{uuid.uuid4().hex}")
    stage.mkdir()
    try:
        shutil.copy2(video_path, stage / "video.mp4")

        thumb_src = video_path.with_suffix(".jpg")
        if thumb_src.is_file() and thumb_src.stat().st_size > 0:
            shutil.copy2(thumb_src, stage / "thumbnail.jpg")

        srt_name = str(meta.get("subtitle_file") or "").strip()
        if srt_name:
            # Metadata must not be able to make a kit copy files outside the
            # video's directory (including on Windows with backslash paths).
            srt_basename = Path(srt_name).name == srt_name
            windows_basename = ntpath.basename(srt_name) == srt_name
            srt_src = meta_path.parent / srt_name
            try:
                same_parent = srt_src.resolve().parent == meta_path.parent.resolve()
            except OSError:
                same_parent = False
            if (srt_basename and windows_basename and same_parent
                    and srt_src.is_file()):
                shutil.copy2(srt_src, stage / "captions.srt")
            else:
                meta["subtitle_file"] = None  # keep the checklist honest
        else:
            meta["subtitle_file"] = None

        (stage / "title.txt").write_text(title, encoding="utf-8")
        if title_alt:
            (stage / "title-b.txt").write_text(title_alt, encoding="utf-8")
        (stage / "description.txt").write_text(
            build_description(meta, cfg), encoding="utf-8")
        (stage / "tags.txt").write_text(", ".join(tags), encoding="utf-8")
        (stage / "CHECKLIST.md").write_text(
            build_checklist(job, meta, cfg, stage), encoding="utf-8")

        _commit_kit(stage, kit)
        return kit
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
