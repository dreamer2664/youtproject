"""Build upload-ready kits for manual YouTube upload.

`python main.py package` turns each generated video into a folder:

    upload/<job-id>/
        video.mp4          drag this into youtube.com/upload
        thumbnail.jpg      upload as the custom thumbnail
        title.txt          copy-paste (100 chars max, enforced)
        description.txt    copy-paste (AI-disclosure footer appended)
        tags.txt           copy-paste (comma-separated, <= 500 chars)
        captions.srt       upload under Subtitles in Studio (when present)
        tiktok.txt         caption + hashtags for TikTok (portrait videos)
        reels.txt          caption + hashtags for Instagram Reels (portrait videos)
        CHECKLIST.md       step-by-step Studio walkthrough for THIS video

Nothing here touches the YouTube API, so there is no quota, no OAuth and no
audit involved. The 3 minutes of manual uploading is what buys you freedom
from all of that.
"""

from __future__ import annotations

import json
import re
import shutil
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
    tags = [t.strip() for t in tags if t and t.strip()]
    while tags and len(", ".join(tags)) > limit:
        tags.pop()
    return tags


def build_description(meta: dict, cfg: Config) -> str:
    """Video description plus the AI-disclosure footer (if enabled)."""
    description = (meta.get("description") or "").strip()
    if cfg.disclosure_enabled and cfg.disclosure_text:
        if cfg.disclosure_text not in description:
            description = f"{description}\n\n---\n{cfg.disclosure_text}".strip()
    return description


def _format_line(meta: dict) -> str:
    fmt = str(meta.get("format", "landscape"))
    width = meta.get("width", "")
    height = meta.get("height", "")
    duration = meta.get("duration_seconds", 0)
    dims = f"{width}x{height}" if width and height else fmt
    return f"{fmt} {dims}, {duration:.0f}s"


def build_checklist(job: Job, meta: dict, cfg: Config, kit: Path) -> str:
    """Per-video Studio walkthrough with this video's values prefilled."""
    title = meta.get("title", "")
    tags = fit_tags(meta.get("tags") or [])
    category = CATEGORY_NAMES.get(str(meta.get("categoryId", "")), "see dropdown in Studio")
    description = build_description(meta, cfg)
    fmt = str(meta.get("format", "landscape"))
    duration = float(meta.get("duration_seconds") or 0)
    burned_in = bool(meta.get("subtitles_burned_in"))
    has_srt = bool(meta.get("subtitle_file"))

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

    captions_step = ""
    if has_srt:
        captions_step = (
            "## 4. Captions\n\n"
            f"{subs_note}\n\n"
            "- [ ] In Studio's left menu open **Subtitles** → pick this video →\n"
            "      *Add* → *Upload file* → choose `captions.srt` from this folder.\n\n"
        )
    n_aud = 5 if has_srt else 4
    n_after = 6 if has_srt else 5

    if fmt == "portrait":
        crosspost_section = f"""## {n_after + 1}. Cross-post to TikTok + Reels (same file)

Your `video.mp4` is already 1080x1920 with centered karaoke captions, so it
meets TikTok and Reels specs as-is. Post within a day of the YouTube upload:

**TikTok**
- [ ] Open TikTok → **+** → **Upload** → pick `video.mp4`.
- [ ] Paste the caption from `tiktok.txt` (title + hashtags).
- [ ] Keep the **original sound** (your narration) — sounds carry your voice.
- [ ] Post. Reply to the first comments within an hour if you can.

**Instagram Reels**
- [ ] Open Instagram → **+** → **Reel** → pick `video.mp4`.
- [ ] Paste the caption from `reels.txt`.
- [ ] Post. Share the reel to your Story for the first-hour boost.

Safe zones are already handled: captions sit centered, clear of TikTok's
right rail and bottom bar — do not add TikTok's auto-captions on top.

"""
    else:
        crosspost_section = f"""## {n_after + 1}. TikTok + Reels

This video is landscape, so it will letterbox on TikTok/Reels. It still works
in a pinch (`tiktok.txt` / `reels.txt` have captions ready), but for these
platforms re-run the same topic in portrait instead.

"""

    return f"""# Upload checklist — {job.id}

Video: **{title}**
Format: {_format_line(meta)}
{shorts_note}
Follow top to bottom. Takes about 3 minutes.

## 1. Upload the file

1. Open <https://youtube.com/upload> (logged into your channel).
2. Drag in `{kit.name}/video.mp4` from this folder.
3. While it processes, fill in the fields below — copy from the `.txt` files
   in this folder, they are already within YouTube's limits.

## 2. Details tab

- [ ] **Title** — paste from `title.txt` ({len(title)}/100 characters).
- [ ] **Description** — paste from `description.txt`. It already ends with the
      AI-disclosure line. Do not delete that line.
- [ ] **Thumbnail** — Upload File → pick `thumbnail.jpg` from this folder.
      (Custom thumbnails need a verified phone number on the channel — one-time,
      free, at <https://youtube.com/verify>.)
- [ ] **Tags** — paste from `tags.txt` ({len(tags)} tags,
      {len(", ".join(tags))}/500 characters). Tags live under *Show more*.
- [ ] **Language** — `{meta.get("language", cfg.language)}`.
- [ ] **Category** — `{category}` (under *Show more*).

## 3. "Altered content" — REQUIRED for AI videos

YouTube asks: *"Is this content altered or synthetic?"*

- [ ] Select **Yes**. Then tick: *"Generates realistic scenes that didn't happen."*

This matches the disclosure footer in the description. Skipping it risks
removal of the video — it takes five seconds, just do it.

Full steps with screenshots context: see `UPLOAD-GUIDE.md` in the project root.

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

{crosspost_section}---

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


PLATFORM_TAGS = {"tiktok": ["fyp"], "reels": ["reels"]}
HASHTAG_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "your",
    "about", "which", "their", "there", "what", "when", "were", "have",
    "video", "videos", "short", "shorts", "part",
}


def _hashtag(word: str) -> str:
    """Sanitise one word into a hashtag body (alphanumeric, lowercase)."""
    return "".join(c for c in word.lower() if c.isalnum())


def platform_hashtags(tags: list[str], platform: str, limit: int = 5) -> list[str]:
    """Up to `limit` hashtags from the video tags plus the platform staple."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in tags:
        for piece in re.split(r"\s+", raw):
            tag = _hashtag(piece)
            if len(tag) >= 3 and tag not in seen and tag not in HASHTAG_STOPWORDS:
                seen.add(tag)
                out.append(tag)
            if len(out) >= limit - 1:
                break
        if len(out) >= limit - 1:
            break
    for extra in PLATFORM_TAGS.get(platform, []):
        if extra not in seen:
            out.append(extra)
    return out[:limit]


def build_platform_caption(meta: dict, platform: str) -> str:
    """Title + hashtags, ready to paste into TikTok / Reels."""
    title = str(meta.get("title", "") or "").strip()
    tags = platform_hashtags(meta.get("tags") or [], platform)
    caption = title
    if tags:
        caption += "\n\n" + " ".join(f"#{t}" for t in tags)
    if str(meta.get("format", "")) != "portrait":
        caption += ("\n\n(NOTE: this video is landscape — TikTok/Reels prefer "
                    "1080x1920 portrait. Re-run with format: portrait for best results.)")
    return caption.strip() + "\n"


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


def build_package(job: Job, cfg: Config) -> Path:
    """Create upload/<id>/ for one generated job. Returns the kit folder."""
    video_path = Path(job.video_file)
    meta_path = Path(job.meta_file)
    if not video_path.exists():
        raise FileNotFoundError(f"video file missing: {video_path}")
    if not meta_path.exists():
        raise FileNotFoundError(f"metadata file missing: {meta_path}")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    raw_title = str(meta.get("title", "") or "")
    title = clean_title(raw_title, job.topic)
    if title != raw_title.strip()[:YOUTUBE_TITLE_LIMIT]:
        print(f"  [package] ⚠️ title guard: {raw_title!r} -> {title!r}")
    meta["title"] = title
    tags = fit_tags(meta.get("tags") or [])

    kit = cfg.package_dir / job.id
    if kit.exists():
        shutil.rmtree(kit)
    kit.mkdir(parents=True, exist_ok=True)

    shutil.copy2(video_path, kit / "video.mp4")

    thumb_src = video_path.with_suffix(".jpg")
    if thumb_src.exists():
        shutil.copy2(thumb_src, kit / "thumbnail.jpg")

    srt_name = meta.get("subtitle_file")
    if srt_name:
        srt_src = meta_path.parent / srt_name
        if srt_src.exists():
            shutil.copy2(srt_src, kit / "captions.srt")
        else:
            meta["subtitle_file"] = None  # keep the checklist honest

    (kit / "title.txt").write_text(title, encoding="utf-8")
    (kit / "description.txt").write_text(build_description(meta, cfg), encoding="utf-8")
    (kit / "tags.txt").write_text(", ".join(tags), encoding="utf-8")
    (kit / "tiktok.txt").write_text(build_platform_caption(meta, "tiktok"), encoding="utf-8")
    (kit / "reels.txt").write_text(build_platform_caption(meta, "reels"), encoding="utf-8")
    (kit / "CHECKLIST.md").write_text(build_checklist(job, meta, cfg, kit), encoding="utf-8")

    return kit
