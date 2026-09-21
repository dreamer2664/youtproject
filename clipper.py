"""Clip lane: turn a long video into short, subtitled, vertical clips.

python main.py clip --url <youtube link>     (or --file local.mp4)

Pipeline (free tiers only):
  download (yt-dlp, <=1080p) -> audio (opus 16k mono, chunked <=25MB)
  -> Groq Whisper word-timestamp transcript (28,800 audio-s/day free)
  -> LLM picks 6-10 candidate windows (20-45s) from the transcript text
  -> vision QC: 3 frames per candidate (person/scene visible, not a dead
     frame) — accept / reject, fail-open
  -> ffmpeg: cut, center-crop 1080x1920, burn karaoke subtitles
  -> scored title + upload kit with source credit (rights hygiene)

The heavy lanes (ElevenLabs, Pexels) are untouched — this lane reads ONE
source video and produces N clips for ~15 API requests total.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import requests

from assembler import ffprobe_duration, resolve_encoder_args
from config import Config
from subtitles import build_karaoke_events, filter_args, write_ass

MAX_CLIPS_DEFAULT = 6
MIN_CLIP_SECONDS = 20
MAX_CLIP_SECONDS = 45
CHUNK_SECONDS = 1500          # 25 min of opus@16k mono ~ 3-5MB, under 25MB
AUDIO_MIME = "audio/ogg"
CLIP_PROMPT = (
    "You are the quality filter for a video-clips channel. This is one "
    "frame from a candidate clip. Reply ONLY JSON: "
    '{"usable": true or false, "reason": "max 8 words"}. '
    "usable=false when: black frame, loading/menu screen, a wall of text, "
    "or nothing visually present. usable=true when a person, action, "
    "scene or subject is visible."
)


class ClipError(RuntimeError):
    pass


@dataclass
class Candidate:
    start: float
    end: float
    hook: str = ""
    title_idea: str = ""
    score: int = 0


# ---------------------------------------------------------------- ingest
def download_source(url: str, work_dir: Path) -> tuple[Path, dict]:
    """yt-dlp the video at <=1080p. Returns (mp4 path, source metadata)."""
    try:
        import yt_dlp
    except ImportError as exc:
        raise ClipError(
            "yt-dlp is not installed — run: pip install -r requirements.txt"
        ) from exc
    work_dir.mkdir(parents=True, exist_ok=True)
    meta: dict = {}

    class _Meta:
        def __init__(self):
            self.info = {}

        def __call__(self, d):
            if d.get("status") == "finished":
                self.info = d.get("info_dict") or {}

    hook = _Meta()
    opts = {
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "outtmpl": str(work_dir / "source.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "noprogress": True,
        "progress_hooks": [hook],
    }
    print(f"  [clip] downloading (<=1080p): {url}")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:  # noqa: BLE001 - surface one clean line
        raise ClipError(f"download failed: {str(exc)[:180]}") from exc
    meta = info or hook.info
    path = work_dir / "source.mp4"
    if not path.exists():
        for candidate in work_dir.glob("source.*"):
            path = candidate
            break
    if not path.exists():
        raise ClipError("download produced no file")
    return path, {
        "title": str(meta.get("title") or "unknown source"),
        "channel": str(meta.get("channel") or meta.get("uploader") or "unknown"),
        "url": str(meta.get("webpage_url") or url),
    }


def probe_dims(src: Path) -> tuple[int, int]:
    """(width, height) via ffmpeg's own stream line (ffprobe-free)."""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(src)],
                         capture_output=True, text=True)
    match = re.search(r", (\d{2,5})x(\d{2,5})[ ,]",
                      out.stderr or "")
    if not match:
        raise ClipError(f"could not read video dimensions from {src.name}")
    return int(match.group(1)), int(match.group(2))


def extract_audio(src: Path, work_dir: Path) -> Path:
    """Full audio as 16kHz mono opus (~3MB per 25 min, whisper-ready)."""
    dest = work_dir / "audio.opus"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-vn", "-c:a", "libopus", "-b:a", "24k", "-ar", "16000",
         "-ac", "1", str(dest)],
        check=True, capture_output=True)
    if not dest.exists():
        raise ClipError("audio extraction produced nothing")
    return dest


# ------------------------------------------------------------ transcript
def plan_chunks(total_seconds: float,
                chunk_seconds: float = CHUNK_SECONDS) -> list[float]:
    """Chunk lengths covering total_seconds (pure, tested)."""
    if total_seconds <= 0:
        return []
    chunks = []
    remaining = total_seconds
    while remaining > 0:
        take = min(chunk_seconds, remaining)
        chunks.append(take)
        remaining -= take
    return chunks


def transcribe_words(audio: Path, cfg: Config) -> list[dict]:
    """Word-level transcript via Groq Whisper, chunked + key rotation.

    Returns [{"word": str, "start": float, "end": float}, ...] with
    ABSOLUTE times across chunks. Falls back to per-chunk segments with
    words spread evenly when the endpoint returns no word timestamps.
    """
    from voice import WHISPER_MODEL, WHISPER_URL

    keys = [k for k in cfg.groq_api_keys if k]
    if not keys:
        raise ClipError("no Groq API keys — transcription is the one hard "
                        "dependency of the clip lane (free tier: 8h audio/day)")
    total = ffprobe_duration(audio)
    offset = 0.0
    words: list[dict] = []
    for index, length in enumerate(plan_chunks(total), start=1):
        chunk_path = audio if len(plan_chunks(total)) == 1 else \
            audio.with_name(f"chunk_{index:02d}.opus")
        if chunk_path != audio:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-ss", f"{offset:.3f}", "-t", f"{length:.3f}",
                 "-i", str(audio), "-c:a", "copy", str(chunk_path)],
                check=True, capture_output=True)
        print(f"  [clip] transcribing {int(offset//60)}:{int(offset%60):02d}"
              f"-{int((offset+length)//60)}:{int((offset+length)%60):02d} "
              f"({index}/{len(plan_chunks(total))})")
        data = _whisper_request(chunk_path, keys)
        got = data.get("words") or []
        if got:
            for item in got:
                words.append({"word": str(item.get("word") or "").strip(),
                              "start": offset + float(item.get("start") or 0),
                              "end": offset + float(item.get("end") or 0)})
        else:  # segment fallback: spread each segment's words evenly
            for seg in data.get("segments") or []:
                text_words = str(seg.get("text") or "").split()
                if not text_words:
                    continue
                s0 = offset + float(seg.get("start") or 0)
                s1 = offset + float(seg.get("end") or 0)
                step = max(0.05, (s1 - s0) / len(text_words))
                for i, word in enumerate(text_words):
                    words.append({"word": word,
                                  "start": s0 + i * step,
                                  "end": s0 + (i + 1) * step})
        offset += length
    return [w for w in words if w["word"]]


def _whisper_request(path: Path, keys: list[str]) -> dict:
    """One chunk -> verbose_json with word timestamps (rotates keys)."""
    last = "no keys tried"
    for key in keys:
        try:
            with open(path, "rb") as handle:
                response = requests.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {key}"},
                    files={"file": (path.name, handle, AUDIO_MIME)},
                    data={"model": "whisper-large-v3-turbo",
                          "response_format": "verbose_json",
                          "timestamp_granularities[]": "word"},
                    timeout=300)
        except requests.RequestException as exc:
            last = f"network: {str(exc)[:100]}"
            continue
        import keystats
        keystats.bump("groq", key, req=1)
        if response.status_code == 200:
            try:
                return response.json()
            except ValueError:
                last = "non-JSON reply"
                continue
        last = f"HTTP {response.status_code}: {response.text[:120]}"
        if response.status_code not in (429, 401, 403):
            break
    raise ClipError(f"transcription failed ({last})")


# ------------------------------------------------------ moment picking
def transcript_lines(words: list[dict], window: float = 10.0) -> list[str]:
    """Compact timestamped transcript for the LLM (pure, tested)."""
    lines: list[str] = []
    current: list[str] = []
    start = 0.0
    for word in words:
        if not current:
            start = word["start"]
        current.append(word["word"])
        if word["start"] - start >= window or (
                current and word["word"][-1:] in ".!?"):
            lines.append(f"[{_ts(start)}] {' '.join(current)}")
            current = []
    if current:
        lines.append(f"[{_ts(start)}] {' '.join(current)}")
    return lines


def _ts(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def build_picker_prompt(lines: list[str], cfg: Config, max_clips: int,
                        min_len: int, max_len: int) -> str:
    transcript = "\n".join(lines)
    return f"""You find viral clip moments in long video transcripts.

VIDEO SOURCE: {cfg.topic}
TRANSCRIPT (timestamps [m:ss] are video-absolute):

{transcript}

Pick the {max_clips} strongest STANDALONE clip moments. A great clip:
- opens on a hook (shocking claim, big laugh, sudden emotion) in the first
  2 seconds — never on mid-sentence context
- tells a complete mini-story or lands a full punchline by the end
- {min_len}-{max_len} seconds long
- understandable WITHOUT having seen the rest of the video
- visually alive (something happening on screen, not dead air or a menu)

Reject: "you had to be there" moments, half-finished stories, boring
exposition, inside jokes that need context.

Return ONLY JSON:
{{"clips": [{{"start": seconds, "end": seconds, "hook": "why this pops
(under 15 words)", "title": "a 4-7 word Shorts title idea"}}]}}
Use ABSOLUTE seconds matching the [m:ss] timestamps."""


def parse_candidates(raw: str, duration: float, max_clips: int,
                     min_len: int = MIN_CLIP_SECONDS,
                     max_len: int = MAX_CLIP_SECONDS) -> list[Candidate]:
    """Validate/clamp/dedupe the LLM's clip list (pure, tested)."""
    from scriptgen import extract_json

    try:
        data = extract_json(raw)
        clips = data.get("clips") if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - garbage in, empty list out
        return []
    out: list[Candidate] = []
    for item in clips or []:
        if not isinstance(item, dict):
            continue
        try:
            start = max(0.0, float(item.get("start") or 0))
            end = min(duration, float(item.get("end") or 0))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        if end - start > max_len:
            end = start + max_len
        if end - start < min_len:
            continue
        cand = Candidate(start=round(start, 2), end=round(end, 2),
                         hook=str(item.get("hook") or "")[:120],
                         title_idea=str(item.get("title") or "")[:80])
        # Overlap dedupe: keep the first (the LLM orders by strength).
        if any(cand.start < kept.end and kept.start < cand.end
               for kept in out):
            continue
        out.append(cand)
        if len(out) >= max_clips:
            break
    return out


# ----------------------------------------------------------- vision QC
def _frame_ok(jpeg: bytes, cfg: Config) -> bool:
    """One frame -> usable verdict (fail-open True on any error)."""
    if not cfg.vision_qc:
        return True
    keys = [k for k in cfg.gemini_api_keys if k]
    if not keys:
        return True
    from vision import MODELS, URL

    import base64

    payload = {
        "contents": [{"parts": [
            {"text": CLIP_PROMPT},
            {"inline_data": {"mime_type": "image/jpeg",
                             "data": base64.b64encode(jpeg).decode("ascii")}},
        ]}],
        "generationConfig": {"temperature": 0.0,
                             "response_mime_type": "application/json"},
    }
    for model in MODELS:
        for key in keys:
            try:
                resp = requests.post(URL.format(model=model),
                                     params={"key": key}, json=payload,
                                     timeout=20)
            except requests.RequestException:
                continue
            import keystats

            keystats.bump("gemini", key, req=1)
            if resp.status_code == 200:
                try:
                    text = (resp.json()["candidates"][0]["content"]
                            ["parts"][0].get("text") or "")
                    from scriptgen import extract_json

                    verdict = extract_json(text)
                    value = verdict.get("usable") if isinstance(verdict, dict) else None
                    if isinstance(value, str):
                        return value.strip().lower() not in ("false", "no", "0")
                    return bool(value)
                except Exception:  # noqa: BLE001 - unparseable: fail open
                    return True
            if resp.status_code == 404:
                break  # dead model id: next model
    return True  # every path failed: the show goes on


def frame_times(cand: Candidate) -> list[float]:
    """3 sample times inside the candidate (pure, tested)."""
    length = cand.end - cand.start
    if length < 6:
        return [cand.start + length / 2]
    return [cand.start + 1.0,
            cand.start + length / 2,
            cand.end - 1.0]


def extract_frame(src: Path, when: float, dest: Path) -> bytes:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-ss", f"{when:.3f}", "-i", str(src),
         "-frames:v", "1", "-q:v", "4", str(dest)],
        check=True, capture_output=True)
    return dest.read_bytes()


# ------------------------------------------------------------ rendering
def crop_filter(width: int, height: int) -> str:
    """Center-crop to vertical then scale to 1080x1920 (pure, tested)."""
    if height > 0 and width / height > 9 / 16:
        return "crop=ih*9/16:ih,scale=1080:1920"
    return "scale=1080:1920"


def clip_words(words: list[dict], start: float, end: float) -> list[dict]:
    """Words inside [start, end], times relative to start (pure, tested)."""
    return [{"word": w["word"], "start": w["start"] - start,
             "end": w["end"] - start}
            for w in words if start <= w["start"] < end]


def build_clip_ass(words_in_clip: list[dict], clip_len: float,
                   cfg: Config, work_dir: Path) -> Path:
    """Karaoke .ass for one clip (adapter over subtitles.build_karaoke_events)."""
    narration = " ".join(w["word"] for w in words_in_clip)
    timings = [(w["start"], w["end"]) for w in words_in_clip]
    events = build_karaoke_events(
        [narration], [timings], [0.0], [clip_len], head_tail=0.0)
    path = work_dir / "clip.ass"
    write_ass(events, path, cfg.format, cfg.width, cfg.height)
    return path


def render_clip(src: Path, cand: Candidate, words: list[dict],
                cfg: Config, out_path: Path, work_dir: Path) -> Path:
    """Cut + crop + burn subtitles -> one vertical clip."""
    length = cand.end - cand.start
    window = clip_words(words, cand.start, cand.end)
    ass_path = build_clip_ass(window, length, cfg, work_dir)
    width, height = probe_dims(src)
    vf = ",".join([crop_filter(width, height), filter_args(ass_path, cfg.format)])
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{cand.start:.3f}", "-t", f"{length:.3f}", "-i", str(src),
        "-vf", vf,
        *resolve_encoder_args(cfg.encoder),
        "-threads", "4", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out_path


def pick_title(cand: Candidate, words_in_clip: list[dict]) -> str:
    """Best-scoring title: the LLM idea vs rule-based variants (pure)."""
    from editorial import _title_keywords, score_title

    narration = " ".join(w["word"] for w in words_in_clip)
    keywords = _title_keywords([narration]) or [words_in_clip[0]["word"]
                                                if words_in_clip else "clip"]
    main = keywords[0].capitalize()
    ideas = [cand.title_idea,
             f"Why {main} Breaks The Rules",
             f"The Real Reason Behind {main}",
             f"How {main} Really Works"]
    scored = [(score_title(idea, keywords), idea) for idea in ideas if idea]
    scored = [(s, t) for s, t in scored if s > -10]
    if not scored:
        return cand.title_idea or f"{main}, Explained"
    return max(scored, key=lambda pair: pair[0])[1]


# ----------------------------------------------------------------- kit
def write_kit(clip: Path, title: str, cand: Candidate, source: dict,
              out_root: Path) -> Path:
    """upload-style kit: mp4 + title + credited description."""
    kit = out_root / clip.stem
    kit.mkdir(parents=True, exist_ok=True)
    shutil.copy2(clip, kit / clip.name)
    (kit / "TITLE.txt").write_text(title, encoding="utf-8")
    (kit / "DESCRIPTION.txt").write_text(
        f"{title}\n\n{cand.hook}\n\nClipped from: {source['title']} — "
        f"{source['channel']}\nSource: {source['url']}\nFull credit to the "
        f"original creator.", encoding="utf-8")
    (kit / "CREDIT.txt").write_text(
        f"source: {source['url']}\nchannel: {source['channel']}\n"
        f"window: {cand.start:.1f}s - {cand.end:.1f}s\n",
        encoding="utf-8")
    return kit


# ---------------------------------------------------------------- main
def run_clip(cfg: Config, url: str = "", file: str = "",
             max_clips: int = MAX_CLIPS_DEFAULT,
             min_len: int = MIN_CLIP_SECONDS,
             max_len: int = MAX_CLIP_SECONDS,
             use_vision: bool = True, keep_work: bool = False,
             out_dir: Path | None = None) -> int:
    """The whole lane. Returns process exit code."""
    if not url and not file:
        raise ClipError("give me --url <youtube link> or --file <local mp4>")
    out_root = Path(out_dir) if out_dir else cfg.root / "clips"
    work = cfg.work_dir / "clip_run"
    work.mkdir(parents=True, exist_ok=True)

    if url:
        src, source = download_source(url, work)
    else:
        src = Path(file)
        if not src.exists():
            raise ClipError(f"file not found: {src}")
        source = {"title": src.stem, "channel": "local file", "url": "local"}

    audio = extract_audio(src, work)
    duration = ffprobe_duration(src)
    print(f"  [clip] source: {src.name} ({duration/60:.0f} min, "
          f"{source['channel']})")
    words = transcribe_words(audio, cfg)
    if len(words) < 40:
        raise ClipError("transcript too thin to mine for moments")
    print(f"  [clip] transcript: {len(words)} words")

    from scriptgen import get_provider

    provider = get_provider(cfg)
    prompt = build_picker_prompt(transcript_lines(words), cfg,
                                 max_clips, min_len, max_len)
    print("  [clip] picking moments...")
    raw = provider.generate_text(prompt, temperature=0.4, tag="clippick",
                                 json_mode=True)
    candidates = parse_candidates(raw, duration, max_clips, min_len, max_len)
    if not candidates:
        raise ClipError("the model found no usable moments")
    print(f"  [clip] {len(candidates)} candidate moment(s)")

    accepted: list[Candidate] = []
    for index, cand in enumerate(candidates, start=1):
        if use_vision:
            verdicts = []
            for t in frame_times(cand):
                frame = extract_frame(src, t, work / f"frame_{t:.0f}.jpg")
                verdicts.append(_frame_ok(frame, cfg))
            if sum(verdicts) < 2:
                print(f"  [clip] rejected {int(cand.start//60)}:"
                      f"{int(cand.start%60):02d} (frames look dead)")
                continue
        accepted.append(cand)
        print(f"  [clip] accepted {int(cand.start//60)}:"
              f"{int(cand.start%60):02d}-{int(cand.end//60)}:"
              f"{int(cand.end%60):02d} — {cand.hook[:60]}")
    if not accepted:
        raise ClipError("vision QC rejected every candidate "
                        "(rerun with --no-vision to override)")

    out_root.mkdir(parents=True, exist_ok=True)
    kits = []
    for index, cand in enumerate(accepted, start=1):
        clip_path = out_root / f"clip_{index:02d}.mp4"
        render_clip(src, cand, words, cfg, clip_path, work)
        title = pick_title(cand, clip_words(words, cand.start, cand.end))
        kits.append(write_kit(clip_path, title, cand, source, out_root))
        print(f"  [clip] {clip_path.name}: {title!r}")
    print(f"\n  {len(kits)} clip(s) + kits -> {out_root}")
    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)
    return 0
