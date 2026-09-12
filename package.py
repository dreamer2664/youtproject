"""Build upload-ready kits for manual YouTube upload.

`python main.py package` turns each generated video into a folder:

    upload/<job-id>/
        video.mp4          drag this into youtube.com/upload
        thumbnail.jpg      upload as the custom thumbnail
        title.txt          copy-paste (100 chars max, enforced)
        description.txt    copy-paste (AI-disclosure footer appended)
        tags.txt           copy-paste (comma-separated, <= 500 chars)
        CHECKLIST.md       step-by-step Studio walkthrough for THIS video

Nothing here touches the YouTube API, so there is no quota, no OAuth and no
audit involved. The 3 minutes of manual uploading is what buys you freedom
from all of that.
"""

from __future__ import annotations

import json
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


def build_checklist(job: Job, meta: dict, cfg: Config, kit: Path) -> str:
    """Per-video Studio walkthrough with this video's values prefilled."""
    title = meta.get("title", "")
    tags = fit_tags(meta.get("tags") or [])
    category = CATEGORY_NAMES.get(str(meta.get("categoryId", "")), "see dropdown in Studio")
    description = build_description(meta, cfg)

    return f"""# Upload checklist — {job.id}

Video: **{title}**

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

## 4. Audience + visibility

- [ ] **Audience** — *No, it's not made for kids* (unless it genuinely is —
      see UPLOAD-GUIDE.md before answering Yes).
- [ ] **Visibility** — Public + *Publish now*, or Schedule it. Scheduling one
      video per day at the same hour beats dumping five at once.

## 5. After publishing

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


def build_package(job: Job, cfg: Config) -> Path:
    """Create upload/<id>/ for one generated job. Returns the kit folder."""
    video_path = Path(job.video_file)
    meta_path = Path(job.meta_file)
    if not video_path.exists():
        raise FileNotFoundError(f"video file missing: {video_path}")
    if not meta_path.exists():
        raise FileNotFoundError(f"metadata file missing: {meta_path}")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    title = str(meta.get("title", "") or "Untitled")[:YOUTUBE_TITLE_LIMIT]
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

    (kit / "title.txt").write_text(title, encoding="utf-8")
    (kit / "description.txt").write_text(build_description(meta, cfg), encoding="utf-8")
    (kit / "tags.txt").write_text(", ".join(tags), encoding="utf-8")
    (kit / "CHECKLIST.md").write_text(build_checklist(job, meta, cfg, kit), encoding="utf-8")

    return kit
