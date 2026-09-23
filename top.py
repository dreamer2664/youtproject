"""Top-X countdown compilations from the clip lane's own output.

    python main.py clip --url <link> --top 5

Same pipeline as a normal clip run (download, transcript, moment pick,
render) — then the best N clips get assembled into ONE "Top 5 moments"
video: a numbered title card before each clip, worst first, the best
moment revealed last (the countdown is the retention trick — viewers
wait for #1). Zero extra LLM calls; titles, credit and windows come
from the clips that already exist.
"""

from __future__ import annotations

from pathlib import Path

CARD_SECONDS = 1.4


def plan_top(entries: list[dict], count: int) -> list[dict]:
    """Pick the top `count` clips and order them for a countdown.

    Play order: lowest score first, best moment LAST (as #1). Pure,
    tested. entries: [{"path", "title", "score", "len"}, ...]
    """
    best = sorted(entries, key=lambda e: e.get("score") or 0,
                  reverse=True)[:max(1, count)]
    best.sort(key=lambda e: e.get("score") or 0)      # worst first
    n = len(best)
    return [{"rank": n - i, "path": e["path"], "title": e["title"],
             "len": e.get("len") or 0.0} for i, e in enumerate(best)]


def _fmt(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"


def build_top_description(plan: list[dict], source: dict) -> str:
    """Description with per-moment timestamps + source credit (tested)."""
    lines = [f"Top {len(plan)} moments from "
             f"\"{source.get('title', 'this video')}\" "
             f"({source.get('channel', 'unknown channel')}).", ""]
    t = 0.0
    for seg in plan:
        t += CARD_SECONDS
        lines.append(f"#{seg['rank']}  {_fmt(t)}  {seg['title']}")
        t += seg["len"]
    lines += ["", f"Source: {source.get('url', '')} — full credit to the "
                  "original creator."]
    return "\n".join(lines)


def make_card(rank: int, total: int, title: str, dest: Path,
              width: int = 1080, height: int = 1920) -> Path:
    """One title card PNG: '#N' huge, the moment title below (tested)."""
    from PIL import Image, ImageDraw

    from subpreview import _find_font

    img = Image.new("RGB", (width, height), (14, 14, 18))
    draw = ImageDraw.Draw(img)
    gold = (255, 193, 30)
    big = _find_font("Arial", int(height * 0.17))
    label = f"#{rank}"
    w = draw.textlength(label, font=big)
    draw.text(((width - w) / 2, height * 0.34), label, font=big, fill=gold)
    head = _find_font("Arial", int(height * 0.030))
    text = f"TOP {total} MOMENTS"
    w = draw.textlength(text, font=head)
    draw.text(((width - w) / 2, height * 0.27), text, font=head,
              fill=(200, 200, 205))
    body = _find_font("Arial", int(height * 0.036))
    # naive wrap into <=2 lines; long titles get clipped, not overflowed
    words, lines, cur = title.split(), [], ""
    for word in words:
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=body) > width * 0.8 and cur:
            lines.append(cur)
            cur = word
        else:
            cur = trial
        if len(lines) == 2:
            break
    if cur and len(lines) < 2:
        lines.append(cur)
    y = height * 0.56
    for line in lines:
        w = draw.textlength(line, font=body)
        draw.text(((width - w) / 2, y), line, font=body, fill=(240, 240, 240))
        y += height * 0.05
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG")
    return dest


def build_top_video(plan: list[dict], out_path: Path, cfg,
                    work_dir: Path) -> Path:
    """Concat cards + clips into the countdown video (one re-encode)."""
    import subprocess

    from clipper import resolve_encoder_args

    inputs: list[str] = []
    filters: list[str] = []
    maps: list[str] = []
    idx = 0
    for seg in plan:
        card = make_card(seg["rank"], len(plan), seg["title"],
                         work_dir / f"card_{seg['rank']}.png")
        inputs += ["-loop", "1", "-t", f"{CARD_SECONDS}", "-i", str(card),
                   "-f", "lavfi", "-t", f"{CARD_SECONDS}", "-i",
                   "anullsrc=r=44100:cl=stereo"]
        filters.append(
            f"[{idx}:v]scale=1080:1920,setsar=1,fps=30,format=yuv420p"
            f"[v{idx}]")
        filters.append(f"[{idx + 1}:a]atrim=duration={CARD_SECONDS},"
                       f"asetpts=N/SR/TB[a{idx}]")
        maps += [f"[v{idx}]", f"[a{idx}]"]
        idx += 2
        inputs += ["-i", str(seg["path"])]
        filters.append(
            f"[{idx}:v]scale=1080:1920,setsar=1,fps=30,format=yuv420p"
            f"[v{idx}]")
        filters.append(
            f"[{idx}:a]aresample=44100,"
            f"aformat=channel_layouts=stereo[a{idx}]")
        maps += [f"[v{idx}]", f"[a{idx}]"]
        idx += 1
    fc = (";" .join(filters)) + ";" + "".join(maps) + \
        f"concat=n={len(plan) * 2}:v=1:a=1[outv][outa]"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs,
           "-filter_complex", fc, "-map", "[outv]", "-map", "[outa]",
           *resolve_encoder_args(cfg.encoder), "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", str(out_path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out_path


def build_top(cfg, entries: list[dict], source: dict, source_key: str,
              out_root: Path, count: int, work_dir: Path) -> Path | None:
    """The whole Top-X step. Returns the kit dir, or None if not enough."""
    plan = plan_top(entries, count)
    if len(plan) < 2:
        print("  [top] fewer than 2 clips — skipping the compilation")
        return None
    key = (source_key or "top")[:8]
    out_path = out_root / f"{key}_top{len(plan)}.mp4"
    print(f"  [top] building Top {len(plan)} countdown "
          f"({sum(e['len'] for e in plan):.0f}s of clips)...")
    build_top_video(plan, out_path, cfg, work_dir / "top")
    from clipper import _clip_checklist, clip_platform_caption

    kit = out_root / f"{key}_top{len(plan)}"
    kit.mkdir(parents=True, exist_ok=True)
    shutil_copy(out_path, kit / "video.mp4")
    title = f"Top {len(plan)} Moments - {source.get('title', '')}".strip()
    if len(title) > 90:
        title = title[:87] + "..."
    (kit / "TITLE.txt").write_text(title, encoding="utf-8")
    (kit / "DESCRIPTION.txt").write_text(
        build_top_description(plan, source), encoding="utf-8")
    (kit / "tiktok.txt").write_text(
        clip_platform_caption(title, "tiktok"), encoding="utf-8")
    (kit / "reels.txt").write_text(
        clip_platform_caption(title, "reels"), encoding="utf-8")
    (kit / "CHECKLIST.md").write_text(_clip_checklist(), encoding="utf-8")
    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"  [top] ✅ {out_path.name}  {size_mb:.1f} MB  "
          f"-> kit {kit.name}")
    return kit


def shutil_copy(src: Path, dest: Path) -> None:
    import shutil

    shutil.copy2(src, dest)
