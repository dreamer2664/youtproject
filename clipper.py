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

import itertools
import json
import keypool
import re
import shlex
import shutil
import subprocess
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

import requests

from assembler import ffprobe_duration, resolve_encoder_args
from config import Config
from subtitles import build_karaoke_events, filter_args, sub_style_from_cfg, write_ass

MAX_CLIPS_DEFAULT = 10
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


# YouTube player clients to try, in order. None = yt-dlp's default (deno
# PO tokens when a JS runtime exists); the others dodge PO-token 403s.
CLIENT_FALLBACKS: list[str | None] = [None, "tv", "web_safari"]


def bot_wall_error(message: str) -> bool:
    """True for YouTube's bot-check wall (pure, tested).

    Live string (2026-09-22 field test, datacenter IP): 'Sign in to
    confirm you\u2019re not a bot. Use --cookies-from-browser ...'.
    Client rotation alone does not pass this; cookies do.
    """
    low = (message or "").lower()
    # Careful: the AGE gate also says "Sign in to confirm your age" —
    # that one stays fatal (no client retry helps). Match bot evidence
    # only, never the shared prefix.
    return ("not a bot" in low or "not a robot" in low
            or "cookies-from-browser" in low)


def cookie_opts(cookies_browser: str, cookies_file: str) -> dict:
    """yt-dlp opts for browser/file cookies (pure, tested). File wins."""
    browser = (cookies_browser or "").strip().lower()
    file = (cookies_file or "").strip()
    if file:
        return {"cookiefile": file}
    if browser:
        return {"cookiesfrombrowser": (browser,)}
    return {}


# Browsers yt-dlp can read cookies from (--cookies-from-browser).
COOKIE_BROWSERS = ("brave", "chrome", "chromium", "edge", "firefox",
                   "opera", "safari", "vivaldi")


def cookie_status(cookies_browser: str, cookies_file: str) -> str:
    """One log line describing the cookie setup (tested).

    Loud by design: silent cookie misconfig used to surface as a bare
    mid-download 403 with no hint cookies were never in effect. A missing
    file or unknown browser is reported, never fatal — anonymous
    downloads sometimes still work.
    """
    file = (cookies_file or "").strip()
    if file:
        path = Path(file)
        if not path.exists():
            return (f"cookies: WARNING — cookies_file not found: {file} "
                    f"(downloading anonymously)")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return (f"cookies: WARNING — cookies_file unreadable: {file} "
                    f"(downloading anonymously)")
        count = sum(1 for line in text.splitlines()
                    if line.strip() and not line.startswith("#"))
        if count == 0:
            return (f"cookies: WARNING — {file} holds no cookies "
                    f"(downloading anonymously)")
        return f"cookies: file {file} ({count} cookies)"
    browser = (cookies_browser or "").strip().lower()
    if browser:
        if browser not in COOKIE_BROWSERS:
            return (f"cookies: WARNING — unknown browser {browser!r} "
                    f"(yt-dlp knows: {', '.join(COOKIE_BROWSERS)})")
        return f"cookies: browser '{browser}'"
    return ("cookies: none — anonymous download "
            "(expect throttling/403s on some videos)")


def retryable_download_error(message: str) -> bool:
    """True when another player client might still succeed (pure, tested)."""
    low = (message or "").lower()
    return ("403" in low or "forbidden" in low
            or "unavailable" in low or "not available" in low
            or "no video formats" in low
            or bot_wall_error(message))


@dataclass
class Candidate:
    start: float
    end: float
    hook: str = ""
    title_idea: str = ""
    score: int = 0
    hook_start: float | None = None  # where the hook line begins, if later


# ---------------------------------------------------------------- ingest
def download_source(url: str, work_dir: Path, cookies_browser: str = "",
                    cookies_file: str = "") -> tuple[Path, dict]:
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

    class _Logger:
        """Surface yt-dlp warnings/errors with actionable advice.

        Live case 2026-09-21: a VOD download ran silent for minutes (the
        no-JS-runtime warning was invisible under quiet mode) and looked
        exactly like a hang.
        """

        def __init__(self):
            self.notes: set[str] = set()

        def debug(self, msg):  # noqa: D102 - chatter stays hidden
            pass

        def info(self, msg):  # noqa: D102
            pass

        def warning(self, msg):
            if "JavaScript runtime" in str(msg) and "js" not in self.notes:
                self.notes.add("js")
                print("  [clip] NOTE: no JS runtime found — YouTube "
                      "extraction is degraded (formats may be missing). "
                      "One-time fix: winget install DenoLand.Deno "
                      "(then restart the shell).")

        def error(self, msg):
            print(f"  [clip] yt-dlp error: {str(msg)[:160]}")

    class _Progress:
        """A progress line every ~10s: silence reads as a hang."""

        def __init__(self):
            self.last = 0.0

        def __call__(self, d):
            if d.get("status") != "downloading":
                return
            import time

            now = time.time()
            if now - self.last < 10:
                return
            self.last = now
            print("  [clip] "
                  + fmt_progress(d.get("downloaded_bytes") or 0,
                                 d.get("total_bytes")
                                 or d.get("total_bytes_estimate") or 0,
                                 d.get("speed") or 0, d.get("eta")))

    hook = _Meta()
    opts = {
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "outtmpl": str(work_dir / "source.%(ext)s"),
        "merge_output_format": "mp4",
        "logger": _Logger(),
        "progress_hooks": [hook, _Progress()],
        **cookie_opts(cookies_browser, cookies_file),
    }
    saw_bot_wall = False
    # YouTube refuses media downloads without PO tokens (no JS runtime)
    # and A/B-tests clients per region: what 403s on one client often
    # downloads fine on another (live case 2026-09-21).
    errors: list[str] = []
    print(f"  [clip] {cookie_status(cookies_browser, cookies_file)}")
    for client in CLIENT_FALLBACKS:
        attempt = dict(opts)
        if client:
            attempt["extractor_args"] = {"youtube": {"player_client": [client]}}
        label = client or "default"
        print(f"  [clip] downloading (<=1080p): {url} [{label}]")
        try:
            with yt_dlp.YoutubeDL(attempt) as ydl:
                info = ydl.extract_info(url, download=True)
            break
        except Exception as exc:  # noqa: BLE001 - collect, try next client
            message = str(exc)
            errors.append(f"[{label}] {message[:160]}")
            if bot_wall_error(message):
                saw_bot_wall = True
            if "JavaScript runtime" in message:
                print("  [clip] yt-dlp needs a JS runtime for this video — "
                      "one-time fix: winget install DenoLand.Deno, then "
                      "restart the shell and re-run.")
            if not retryable_download_error(message):
                raise ClipError(f"download failed: {message[:180]}") from exc
            print("  [clip] YouTube refused that attempt — "
                  "retrying with a different player client...")
            info = None
    else:
        hint = (" Try: pip install -U yt-dlp  (YouTube changes weekly), "
                "and make sure Deno is installed (winget install "
                "DenoLand.Deno).")
        if saw_bot_wall:
            hint = (" YouTube bot-checked this network. One-time fix: set "
                    "clip.cookies_browser: 'firefox' in config.yaml "
                    "(Firefox is safest; Chrome must be fully closed), or "
                    "export a cookies.txt and set clip.cookies_file.")
        raise ClipError(
            "download failed on every player client ("
            + " | ".join(errors[-2:]) + ")." + hint)
    if not info and not hook.info:
        raise ClipError("download produced no metadata")
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


def fmt_progress(done: int, total: int, speed: float,
                 eta) -> str:
    """One download progress line (pure, tested): '25% at 4.0 MB/s, ETA 0:30'."""
    pct = f"{done / total * 100:.0f}%" if total else "?%"
    speed_txt = f"{speed / 1e6:.1f} MB/s" if speed else "? MB/s"
    try:
        eta_s = int(eta)
        eta_txt = f"{eta_s // 60}:{eta_s % 60:02d}"
    except (TypeError, ValueError):
        eta_txt = "?"
    return f"downloading: {pct} at {speed_txt}, ETA {eta_txt}"


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


def whisper_params(title: str) -> dict:
    """Extra Groq Whisper params that cut mishears (pure, tested).

    language pins English decoding (auto-detect wanders); prompt feeds
    the video's title so topic words spell themselves (Whisper biases
    toward the prompt's vocabulary).
    """
    params = {"language": "en"}
    title = (title or "").strip()
    if title:
        params["prompt"] = f"English narration. Video title: {title}"
    return params


TRANSCRIPT_FIX_BATCH = 800


def fix_transcript_words(words: list[dict], title: str,
                         provider) -> list[dict]:
    """One LLM pass fixing misheard words; timings untouched (tested).

    Live complaint 2026-09-23: Whisper mishears homophones (know/no).
    DIFFS ONLY (live lesson 2026-09-23, groq probe): the old contract
    made the model echo all ~800 words back — gpt-oss burns its 3,072-
    token completion cap on hidden reasoning and comes back EMPTY or
    truncated mid-JSON (finish=length), so on groq the fix silently
    never applied. The model now numbers the words and returns only the
    ones that change — a typical reply is a few dozen tokens. HARD
    GUARDS: each fix must name one in-batch index and ONE replacement
    word; anything else is ignored. Word timings are never touched, so
    captions cannot desync. Never raises.
    """
    from scriptgen import extract_json

    if not words:
        return words
    out = [dict(w) for w in words]
    for start in range(0, len(out), TRANSCRIPT_FIX_BATCH):
        stop = min(start + TRANSCRIPT_FIX_BATCH, len(out))
        batch_words = [str(out[i].get("word") or "") for i in range(start, stop)]
        numbered = "\n".join(f"{j}: {w}" for j, w in enumerate(batch_words))
        prompt = (
            "This is an automatic transcript of a video"
            + (f" titled {title!r}" if (title or "").strip() else "") + ".\n"
            "Fix ONLY obvious misheard words — homophones such as know/no, "
            "their/there, your/you're, heel/heal — and clear spelling "
            "slips.\n"
            "The words are numbered below. Reply with ONLY the words that "
            "need fixing:\n"
            'Return ONLY JSON: {"fixes": [{"i": <index>, "w": '
            '"<corrected word>"}]} — one entry per word that changes.\n'
            "Never merge, add or drop words; keep punctuation as-is; when "
            "unsure, leave the word out.\n"
            'No changes needed -> {"fixes": []}.\n'
            "NUMBERED WORDS:\n" + numbered)
        try:
            raw = provider.generate_text(prompt, temperature=0.0,
                                         tag="clipfix", json_mode=True)
            data = extract_json(raw)
        except Exception:  # noqa: BLE001 - a fix pass never kills clips
            continue
        fixes = data.get("fixes") if isinstance(data, dict) else None
        if not isinstance(fixes, list):
            continue
        for fix in fixes:
            if not isinstance(fix, dict):
                continue
            try:
                j = int(fix.get("i"))
                new = str(fix.get("w") or "").strip()
            except (TypeError, ValueError):
                continue
            if not (0 <= j < stop - start):
                continue
            gi = start + j
            old = batch_words[j]
            if new and len(new.split()) == 1 and new != old:
                patched = dict(out[gi])
                patched["word"] = new
                out[gi] = patched
    return out


def video_id_for_captions(source_url: str) -> str:
    """YouTube video id for caption harvesting, "" when none (never raises).

    Local files, channels, and non-YouTube links all yield "" — there are
    no captions to fall back on, so the caller relies on Groq Whisper.
    """
    try:
        from youtube import extract_id

        kind, ref = extract_id(source_url)
    except (ValueError, AttributeError, TypeError):
        return ""
    return ref if kind == "video" else ""


def _is_english_track(track) -> bool:
    code = str(getattr(track, "language_code", "") or "").lower()
    name = str(getattr(track, "language", "") or "").lower()
    return code.startswith("en") or name == "english"


def _pick_caption_track(tracks: list) -> tuple[object | None, bool]:
    """(track, needs_translation): manual EN > generated EN > EN-capable."""
    tracks = [t for t in (tracks or []) if t is not None]
    for track in tracks:
        if _is_english_track(track) and not getattr(track, "is_generated",
                                                    False):
            return track, False
    for track in tracks:
        if _is_english_track(track):
            return track, False
    for track in tracks:
        if getattr(track, "is_translatable", False):
            return track, True
    return None, False


def fetch_youtube_captions(video_id: str) -> tuple[list[dict], str] | None:
    """(segments, origin) for a video, None when unavailable (never raises).

    Keyless caption harvest (youtube-transcript-api: v1 instance API with a
    0.x static fallback); every failure mode returns None so the caller
    falls back after Groq Whisper fails. Origin is manual/generated/translated.
    """
    if not (video_id or "").strip():
        return None
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None
    try:
        api = YouTubeTranscriptApi()
    except Exception:
        return None
    if not hasattr(api, "list"):
        try:
            get_transcript = YouTubeTranscriptApi.get_transcript
        except AttributeError:
            return None
        try:
            raw = get_transcript(video_id)
        except Exception:
            return None
        segments = [{"text": d.get("text", ""), "start": d.get("start", 0.0),
                     "duration": d.get("duration", 0.0)} for d in raw]
        return (segments, "captions") if segments else None
    try:
        track, translate = _pick_caption_track(list(api.list(video_id)))
        if track is None:
            return None
        if translate:
            track = track.translate("en")
            origin = "translated"
        elif getattr(track, "is_generated", False):
            origin = "generated"
        else:
            origin = "manual"
        fetched = track.fetch()
    except Exception:
        return None
    segments = []
    for item in fetched:
        if isinstance(item, dict):
            text = item.get("text", "")
            start = item.get("start", 0.0)
            dur = item.get("duration", item.get("dur", 0.0))
        else:
            text = getattr(item, "text", "")
            start = getattr(item, "start", 0.0)
            dur = getattr(item, "duration", 0.0)
        segments.append({"text": text, "start": start, "duration": dur})
    if not segments:
        return None
    return segments, origin


def captions_to_words(segments: list[dict]) -> list[dict]:
    """Caption segments -> word dicts with spread timings (pure, tested).

    Captions are line-timed, not word-timed: each segment's words spread
    evenly (same math as the Whisper segment fallback in transcribe_words).

    YouTube auto-captions OVERLAP: each line's duration runs until the
    line after next starts (the rolling two-line display). Spreading words
    over that raw duration interleaves neighbouring lines in time, the
    karaoke builder then shows the same subtitle line twice at once —
    live bug 2026-09-30 ("two simultaneous subtitles, same exact text").
    So each segment ends where the next one begins.
    """
    parsed: list[tuple[float, float, list[str]]] = []
    for seg in segments or []:
        try:
            text_words = str(seg.get("text") or "").split()
            s0 = float(seg.get("start") or 0.0)
            dur = float(seg.get("duration") or 0.0)
        except (AttributeError, TypeError, ValueError):
            continue
        if not text_words or dur <= 0:
            continue
        parsed.append((s0, dur, text_words))
    parsed.sort(key=lambda item: item[0])  # stable: equal starts keep order
    words: list[dict] = []
    for index, (s0, dur, text_words) in enumerate(parsed):
        end = s0 + dur
        if index + 1 < len(parsed):
            next_start = parsed[index + 1][0]
            if s0 < next_start < end:
                end = next_start
        step = max(0.05, (end - s0) / len(text_words))
        for i, word in enumerate(text_words):
            words.append({"word": word, "start": s0 + i * step,
                          "end": s0 + (i + 1) * step})
    return normalize_word_timings(words)


def normalize_word_timings(words: list[dict],
                           eps: float = 0.01) -> list[dict]:
    """Monotonic, non-overlapping word timings; order kept (pure, tested).

    Repairs transcripts cached before the caption fix (and any other
    overlapping source): the list is split into runs wherever a word
    starts before the previous one ended; a run that spills into the
    next is compressed to end where the next begins (so word ORDER — the
    spoken order — never changes, only times). A final pass guarantees
    each word starts at/after the previous one's end. Well-formed input
    (Whisper) passes through unchanged. Extra keys (eos, …) survive.
    """
    clean: list[dict] = []
    for word in words or []:
        if not isinstance(word, dict):
            continue
        try:
            start = float(word.get("start") or 0.0)
            end = float(word.get("end") or 0.0)
        except (TypeError, ValueError):
            continue
        clean.append(dict(word, start=start, end=max(end, start)))
    if not clean:
        return clean
    runs, cur = [], [clean[0]]
    for word in clean[1:]:
        if word["start"] < cur[-1]["end"] - eps:
            runs.append(cur)
            cur = [word]
        else:
            cur.append(word)
    runs.append(cur)
    for run, nxt in zip(runs, runs[1:]):
        r0 = run[0]["start"]
        r_end = max(w["end"] for w in run)
        limit = nxt[0]["start"]
        if r_end > limit and limit - r0 >= 0.02 * len(run):
            scale = (limit - r0) / (r_end - r0)
            for w in run:
                w["start"] = r0 + (w["start"] - r0) * scale
                w["end"] = r0 + (w["end"] - r0) * scale
    prev_end = float("-inf")
    for w in clean:
        if w["start"] < prev_end:
            w["start"] = prev_end
        if w["end"] < w["start"] + 0.02:
            w["end"] = w["start"] + 0.02
        w["start"], w["end"] = round(w["start"], 3), round(w["end"], 3)
        prev_end = w["end"]
    return clean


def transcribe_words(audio: Path, cfg: Config,
                     title: str = "", video_id: str = "") -> list[dict]:
    """Word-level transcript: Groq Whisper first, YouTube captions fallback.

    Returns [{"word": str, "start": float, "end": float}, ...] with
    ABSOLUTE times. Whisper uses the configured transcription share of the
    Groq keys (`ai.groq_transcription_percent`, default 90%) and runs
    chunked with ordered key failover; if it is unavailable or returns no
    words, the keyless YouTube captions are tried as a fallback. Whisper
    timestamps are word-level; caption line timings are spread evenly
    across words.
    """
    keys = keypool.live("groq", cfg.groq_transcription_api_keys)
    whisper_error: Exception | None = None
    whisper_empty = False

    if keys:
        try:
            total = ffprobe_duration(audio)
            chunks = plan_chunks(total)
            offset = 0.0
            words: list[dict] = []
            for index, length in enumerate(chunks, start=1):
                chunk_path = audio if len(chunks) == 1 else \
                    audio.with_name(f"chunk_{index:02d}.opus")
                if chunk_path != audio:
                    subprocess.run(
                        ["ffmpeg", "-y", "-loglevel", "error",
                         "-ss", f"{offset:.3f}", "-t", f"{length:.3f}",
                         "-i", str(audio), "-c:a", "copy", str(chunk_path)],
                        check=True, capture_output=True)
                print(f"  [clip] transcribing {int(offset//60)}:"
                      f"{int(offset%60):02d}-"
                      f"{int((offset+length)//60)}:"
                      f"{int((offset+length)%60):02d} "
                      f"({index}/{len(chunks)})")
                data = _whisper_request(chunk_path, keys, title=title,
                                        seconds=length)
                got = data.get("words") or []
                if got:
                    for item in got:
                        words.append({
                            "word": str(item.get("word") or "").strip(),
                            "start": offset + float(item.get("start") or 0),
                            "end": offset + float(item.get("end") or 0),
                        })
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
            words = normalize_word_timings(
                [word for word in words if word["word"]])
            if words:
                print(f"  [clip] transcript: Groq Whisper ({len(words)} words)")
                return words
            whisper_empty = True
            print("  [clip] Groq Whisper returned no words; "
                  "trying YouTube captions")
        except Exception as exc:  # noqa: BLE001 - captions can salvage source
            whisper_error = exc
            print(f"  [clip] Groq Whisper unavailable "
                  f"({str(exc)[:120]}); trying YouTube captions")
    else:
        whisper_error = ClipError("no Groq Whisper transcription keys")
        print("  [clip] no Groq Whisper transcription keys; "
              "trying YouTube captions")

    if (video_id or "").strip():
        try:
            harvested = fetch_youtube_captions(video_id)
            if harvested:
                segments, origin = harvested
                words = captions_to_words(segments)
                if words:
                    print(f"  [clip] transcript: YouTube captions fallback "
                          f"({origin}, {len(words)} words)")
                    return words
        except Exception as exc:  # noqa: BLE001 - fallback must not mask cause
            print(f"  [clip] YouTube captions fallback failed "
                  f"({str(exc)[:100]})")

    if whisper_empty:
        return []  # preserve the silent-source result when captions are absent
    if whisper_error is not None:
        if keys:
            raise ClipError(f"Groq Whisper failed and YouTube captions "
                            f"were unavailable ({str(whisper_error)[:180]})") \
                from whisper_error
        raise ClipError("no Groq Whisper transcription keys and no usable "
                        "YouTube captions") from whisper_error
    return []


def _whisper_request(path: Path, keys: list[str],
                     title: str = "", seconds: float = 0.0) -> dict:
    """One chunk -> verbose_json with word timestamps (rotates keys).

    seconds = chunk audio length, ledgered so `keys` can show the ~8h/day
    audio pool (the binding Groq quota for clip/parts lanes).
    """
    from voice import WHISPER_MODEL, WHISPER_URL

    keys = keypool.live("groq", keys)  # dead keys stay dead (keypool)
    last = "no keys tried"
    for key in keys:
        try:
            with open(path, "rb") as handle:
                response = requests.post(
                    WHISPER_URL,
                    headers={"Authorization": f"Bearer {key}"},
                    files={"file": (path.name, handle, AUDIO_MIME)},
                    data={"model": WHISPER_MODEL,
                          "response_format": "verbose_json",
                          "timestamp_granularities[]": "word",
                          **whisper_params(title)},
                    timeout=300)
        except requests.RequestException as exc:
            last = f"network: {str(exc)[:100]}"
            continue
        import keystats
        # tag="whisper": the clip/parts/longform transcription lane. The
        # bot's voice notes use tag="voicenote" (voice.py), so
        # `keys --month` can show WHICH lane ate the ~8h/day audio pool.
        keystats.bump("groq", key, req=1, audio=max(0, int(seconds)),
                      tag="whisper")
        if response.status_code == 200:
            try:
                return response.json()
            except ValueError:
                last = "non-JSON reply"
                continue
        last = f"HTTP {response.status_code}: {response.text[:120]}"
        if response.status_code in (401, 403):
            keypool.dead("groq", key)  # revoked: skip it in every lane
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
{{"clips": [{{"start": seconds, "end": seconds, "hook_start": seconds
where the hook line begins (use start if the clip already opens on it),
"hook": "why this pops (under 15 words)", "title": "a 4-7 word Shorts
title idea"}}]}}
Use ABSOLUTE seconds matching the [m:ss] timestamps."""


def sentence_spans(words: list[dict]) -> list[tuple[float, float]]:
    """(start, end) of every sentence in a word list (pure, tested).

    Boundary = word whose text ends in . ! or ? (same rule the transcript
    formatter uses), or one flagged eos by cutpoints.ensure_sentence_ends.
    A trailing unpunctuated fragment becomes one span.
    """
    spans: list[tuple[float, float]] = []
    cur_start: float | None = None
    for word in words:
        text = str(word.get("word") or "")
        if cur_start is None:
            cur_start = float(word.get("start") or 0.0)
        if (text and text[-1:] in ".!?") or word.get("eos"):
            spans.append((cur_start, float(word.get("end") or 0.0)))
            cur_start = None
    if cur_start is not None and words:
        spans.append((cur_start, float(words[-1].get("end") or 0.0)))
    return spans


def snap_candidate(cand: "Candidate", words: list[dict],
                   min_len: int, max_len: int) -> "Candidate":
    """Move clip edges onto sentence boundaries (pure, tested).

    A start mid-sentence retreats to that sentence's beginning; a
    mid-sentence end completes the sentence. Both directions respect
    max_len, falling back to the other direction; never shrinks a clip
    below min_len. Cuts landing in pauses (between sentences) are left
    alone — silence is a fine place to cut.
    """
    spans = sentence_spans(words)
    if not spans:
        return cand
    start, end = cand.start, cand.end
    lo, hi = float(min_len), float(max_len)
    containing = next((s for s in spans if s[0] <= start < s[1]), None)
    if containing and containing[0] < start:
        if end - containing[0] <= hi:
            start = containing[0]
        else:
            nxt = next((s[0] for s in spans if s[0] > start), None)
            if nxt is not None and end - nxt >= lo:
                start = nxt
    containing = next((s for s in spans if s[0] < end <= s[1]), None)
    if containing and end < containing[1]:
        if containing[1] - start <= hi:
            end = containing[1]
        else:
            prev = [s for s in spans if s[1] <= end]
            if prev and prev[-1][1] - start >= lo:
                end = prev[-1][1]
    if end <= start:
        return cand
    return Candidate(start=round(start, 2), end=round(end, 2),
                     hook=cand.hook, title_idea=cand.title_idea,
                     score=cand.score, hook_start=cand.hook_start)


def apply_hook_start(cand: "Candidate", min_len: int) -> "Candidate":
    """Open the clip ON the hook when the picker says it starts later.

    The picker marks where the hook line begins; if that is usefully
    later than the clip start (>= 3s of context to skip) and enough clip
    remains (>= min_len), the context is dropped and the clip opens on
    the hook. snap_candidate then tidies the new start to the hook
    sentence's first word.
    """
    hs = cand.hook_start
    if hs is None:
        return cand
    if hs > cand.start + 3.0 and cand.end - hs >= min_len:
        return Candidate(start=round(hs, 2), end=cand.end,
                         hook=cand.hook, title_idea=cand.title_idea,
                         score=cand.score)
    return cand


def parse_candidates(raw: str, duration: float, max_clips: int,
                     min_len: int = MIN_CLIP_SECONDS,
                     max_len: int = MAX_CLIP_SECONDS, lo: float = 0.0,
                     hi: float | None = None) -> list[Candidate]:
    """Validate/clamp/dedupe the LLM's clip list (pure, tested).

    lo/hi bound a picker window: candidates outside are dropped (used
    when a long VOD is picked window-by-window).
    """
    from scriptgen import extract_json

    hi = duration if hi is None else min(hi, duration)

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
            start = max(lo, float(item.get("start") or 0))
            end = min(hi, float(item.get("end") or 0))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        if end - start > max_len:
            end = start + max_len
        if end - start < min_len:
            continue
        hook_start = None
        try:
            hs = float(item.get("hook_start"))
            if start < hs <= end:
                hook_start = hs
        except (TypeError, ValueError):
            pass
        cand = Candidate(start=round(start, 2), end=round(end, 2),
                         hook=str(item.get("hook") or "")[:120],
                         title_idea=str(item.get("title") or "")[:80],
                         hook_start=hook_start)
        # Overlap dedupe: keep the first (the LLM orders by strength).
        if any(cand.start < kept.end and kept.start < cand.end
               for kept in out):
            continue
        out.append(cand)
        if len(out) >= max_clips:
            break
    return out


# Long VODs: one picker prompt per ~10 minutes of speech (a 30-min
# transcript is ~4,500 words — a single prompt buries the good moments).
# Windows overlap so a moment spanning a seam is still catchable; the
# duplicate pick collapses in dedupe_overlaps.
PICK_WINDOW_WORDS = 1400
PICK_OVERLAP_WORDS = 120


def split_windows(words: list[dict],
                  window_words: int = PICK_WINDOW_WORDS,
                  overlap_words: int = PICK_OVERLAP_WORDS) -> list[list[dict]]:
    """Split a long transcript into overlapping picker windows (tested)."""
    if not words:
        return []
    if len(words) <= window_words:
        return [words]
    if overlap_words >= window_words:
        overlap_words = window_words // 2
    windows: list[list[dict]] = []
    step = window_words - overlap_words
    start = 0
    while True:
        windows.append(words[start:start + window_words])
        if start + window_words >= len(words):
            return windows
        start += step


def dedupe_overlaps(cands: list["Candidate"]) -> list["Candidate"]:
    """Drop candidates overlapping an earlier one; keep-first (tested).

    Same rule parse_candidates applies inside one LLM reply, applied to
    the merged multi-window list.
    """
    kept: list["Candidate"] = []
    for cand in cands:
        if any(cand.start < k.end and k.start < cand.end for k in kept):
            continue
        kept.append(cand)
    return kept


# ----------------------------------------------------------- vision QC
def _frame_ok(jpeg: bytes, cfg: Config) -> bool:
    """One frame -> usable verdict (fail-open True on any error)."""
    if not cfg.vision_qc:
        return True
    keys = keypool.live("gemini", cfg.gemini_api_keys)
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

            # tag="vision": clip frame QC shares the vision bucket with
            # the image-pick checks (vision.py), so `keys --month` can
            # answer "how much did QC burn" vs "how much did scripting".
            keystats.bump("gemini", key, req=1, tag="vision")
            if keypool.key_dead_like(resp.status_code, resp.text):
                keypool.dead("gemini", key)
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


def fit_filter() -> str:
    """Whole landscape frame inside 1080x1920, blurred fill (pure, tested).

    Live complaint 2026-09-23: the middle-slice crop threw away ~65% of
    a landscape VOD's picture. This is the standard clipping-tool look:
    the full frame, sharp and centered, over a blurred, darkened copy of
    itself that fills the vertical canvas.
    """
    return ("split=2[bg][fg];"
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=20:2,eq=brightness=-0.15[bgf];"
            "[fg]scale=1080:-2[fgf];"
            "[bgf][fgf]overlay=(W-w)/2:(H-h)/2")


def vertical_treatment(width: int, height: int,
                       mode: str = "auto") -> str:
    """How a source becomes vertical: fit | crop | scale (pure, tested).

    auto: landscape (>= 0.9) keeps its whole frame (blurred fill);
    near-vertical sources (9/16 .. 0.9) crop — the sliver lost is small
    and a full frame would be mostly bars; vertical sources just scale.
    """
    if height <= 0:
        return "scale"
    ratio = width / height
    if mode == "fit":
        return "scale" if ratio <= 9 / 16 else "fit"
    if mode == "crop":
        return "scale" if ratio <= 9 / 16 else "crop"
    return "scale" if ratio <= 9 / 16 else ("fit" if ratio >= 0.9 else "crop")


def decide_subject_x(positions: list[float | None],
                     spread_limit: float = 0.25) -> float | None:
    """Consensus subject position from sampled frames (pure, tested).

    No valid samples, or samples that disagree wildly, means uncertainty
    — and the safest crop under uncertainty is the center.
    """
    valid = [p for p in positions if p is not None]
    if not valid:
        return None
    if len(valid) > 1 and max(valid) - min(valid) > spread_limit:
        return None
    mean = sum(valid) / len(valid)
    return round(min(0.85, max(0.15, mean)), 3)


def smart_crop_filter(width: int, height: int,
                      subject_x: float | None) -> str:
    """Vertical crop that follows the subject (pure, tested).

    Center crop when the subject is centered or unknown (jumping the crop
    for near-center subjects just adds motion); off-center subjects shift
    the window, clamped to the frame edges.
    """
    if height <= 0 or width / height <= 9 / 16:
        return "scale=1080:1920"
    if subject_x is None or abs(subject_x - 0.5) < 0.08:
        return "crop=ih*9/16:ih,scale=1080:1920"
    crop_w = int(height * 9 / 16)
    offset = int(round(subject_x * width - crop_w / 2))
    offset = max(0, min(offset, width - crop_w))
    return f"crop={crop_w}:{height}:{offset}:0,scale=1080:1920"


def clip_words(words: list[dict], start: float, end: float) -> list[dict]:
    """Words inside [start, end], times relative to start (pure, tested)."""
    return [{"word": w["word"], "start": w["start"] - start,
             "end": w["end"] - start}
            for w in words if start <= w["start"] < end]


def build_clip_ass(words_in_clip: list[dict], clip_len: float,
                   cfg: Config, work_dir: Path, style: dict | None = None,
                   extra: str | None = None, caps: bool = False) -> Path:
    """Karaoke .ass for one clip (adapter over subtitles.build_karaoke_events).

    extra: one full Dialogue line appended to the file (the progress-bar
    mechanism reused — the parts lane's header rides here).
    """
    # Last line of defence: overlapping word timings make the karaoke
    # builder show one subtitle line twice at once, whatever their source.
    words_in_clip = normalize_word_timings(words_in_clip)
    narration = " ".join(w["word"] for w in words_in_clip)
    timings = [(w["start"], w["end"]) for w in words_in_clip]
    events = build_karaoke_events(
        [narration], [timings], [0.0], [clip_len], head_tail=0.0, caps=caps)
    path = work_dir / "clip.ass"
    write_ass(events, path, cfg.format, cfg.width, cfg.height,
              progress=extra, style=style)
    return path


def _auto_sub_position(src: Path, cand: Candidate, work_dir: Path) -> str | None:
    """Calmest vertical third across 3 sampled frames (no API, no LLM).

    Rule-based like smart-crop's subject_x: free, offline, deterministic.
    None when no frame can be read — the caller keeps the configured
    position instead of guessing.
    """
    from subtitles import frame_band_energies, pick_sub_band

    length = cand.end - cand.start
    totals = [0.0, 0.0, 0.0]
    used = 0
    for frac in (0.2, 0.5, 0.8):
        try:
            data = extract_frame(src, cand.start + length * frac,
                                 work_dir / f"subpos_{int(frac * 100)}.jpg")
        except Exception:  # noqa: BLE001 - a bad frame is not fatal
            continue
        bands = frame_band_energies(data)
        if bands:
            totals = [t + b for t, b in zip(totals, bands)]
            used += 1
    if not used:
        return None
    return pick_sub_band(totals)


def render_clip(src: Path, cand: Candidate, words: list[dict],
                cfg: Config, out_path: Path, work_dir: Path,
                sub_pos: str = "default",
                extra_ass: str | None = None, caps: bool = False) -> Path:
    """Cut + crop + burn subtitles -> one vertical clip.

    extra_ass: one full Dialogue line appended to the clip's .ass
    (the parts lane's header rides here, zero extra cost).
    """
    length = cand.end - cand.start
    window = clip_words(words, cand.start, cand.end)
    style = sub_style_from_cfg(cfg, sub_pos)
    if style.get("position") == "auto":
        resolved = _auto_sub_position(src, cand, work_dir)
        if resolved:
            print(f"  [clip] subs auto: the {resolved} third is the calmest")
            style = {**style, "position": resolved}
        else:
            print("  [clip] subs auto: no frame readable — using the "
                  "configured position")
            style = {**style, "position": "default"}
    ass_path = build_clip_ass(window, length, cfg, work_dir, style,
                                extra=extra_ass, caps=caps)
    width, height = probe_dims(src)
    treatment = vertical_treatment(width, height, cfg.clip_crop_mode)
    if treatment == "fit":
        print("  [clip] fit: whole frame kept, blurred background fill")
        vf = fit_filter() + "," + filter_args(ass_path, cfg.format)
    else:
        subject = None
        if treatment == "crop" and cfg.clip_smart_crop:
            from vision import subject_x as ask_subject

            positions = []
            for frac in (0.25, 0.6):
                frame = extract_frame(
                    src, cand.start + length * frac,
                    work_dir / f"subject_{cand.start:.0f}_{int(frac * 100)}.jpg")
                positions.append(ask_subject(frame, cfg))
            subject = decide_subject_x(positions)
            if subject is not None and abs(subject - 0.5) >= 0.08:
                print(f"  [clip] smart crop: subject at {subject:.0%} of width")
            else:
                print("  [clip] crop: centered (subject centered or uncertain)")
        vf = ",".join([smart_crop_filter(width, height, subject)
                       if treatment == "crop" else crop_filter(width, height),
                       filter_args(ass_path, cfg.format)])
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
def clip_hashtags(title: str, platform: str) -> list[str]:
    """Caption hashtags for a clip: platform staples + title words (tested)."""
    base = {"tiktok": ["fyp", "foryou", "learnontiktok"],
            "reels": ["reels", "explore", "learn"]}[platform]
    words = []
    for word in "".join(c if c.isalnum() else " " for c in title.lower()
                        ).split():
        if len(word) > 3 and word not in words and word not in base:
            words.append(word)
    return (words[:3] + base)[:6]


def clip_platform_caption(title: str, platform: str) -> str:
    """Ready-to-paste TikTok/Reels caption for a clip (pure, tested).

    No source credit here — TikTok/Reels bios carry it; the YouTube kit
    (DESCRIPTION.txt / CREDIT.txt) keeps the full attribution.
    """
    tags = clip_hashtags(title, platform)
    return (title.strip() + "\n\n" + " ".join(f"#{t}" for t in tags)
            + "\n") if tags else title.strip() + "\n"


def _clip_checklist() -> str:
    """Compact Studio walkthrough for a clip upload (tested).

    First live clip posting (2026-09-23) went out with an EMPTY
    description — the attribution line never reached YouTube. The
    checklist exists so that never happens again.
    """
    return (
        "# Upload checklist — clip\n\n"
        "- [ ] **Title** — paste from `TITLE.txt` (hashtags included)\n"
        "- [ ] **Description** — paste from `DESCRIPTION.txt` **before "
        "publishing**.\n"
        "      It credits the source video and its channel — that "
        "attribution is what\n      separates a clip from a reupload if "
        "YouTube ever reviews the channel.\n"
        "- [ ] **Category** — Entertainment, not Education: a stream clip "
        "should be\n      compared against other clips, not courseware.\n"
        "- [ ] **Upload** — the .mp4 in this folder, exactly as rendered "
        "(subtitles burned in)\n"
        "- [ ] **Cross-post** — `tiktok.txt` / `reels.txt` captions are "
        "ready (no credit\n      line on those platforms by design)\n"
        "- [ ] `CREDIT.txt` keeps the source link and the exact time "
        "window for your records\n")


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
    (kit / "tiktok.txt").write_text(
        clip_platform_caption(title, "tiktok"), encoding="utf-8")
    (kit / "reels.txt").write_text(
        clip_platform_caption(title, "reels"), encoding="utf-8")
    (kit / "CHECKLIST.md").write_text(_clip_checklist(), encoding="utf-8")
    return kit


# ---------------------------------------------------------------- cache
TRANSCRIPT_CACHE_VERSION = 1


def transcript_cache_key(url: str, src: Path) -> str:
    """Stable identity for a clip source (pure, tested).

    URL: the link itself. File: resolved path + size + mtime, so an
    edited or replaced file never reuses a stale transcript.
    """
    import hashlib

    if url:
        raw = "url:" + url.strip()
    else:
        try:
            stat = src.stat()
            raw = f"file:{src.resolve()}:{stat.st_size}:{int(stat.st_mtime)}"
        except OSError:
            raw = f"file:{src.name}:unknown"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def transcript_cache_path(cfg: Config, key: str) -> Path:
    return cfg.work_dir / "clip_cache" / f"{key}.json"


def load_transcript_cache(path: Path) -> list[dict] | None:
    """Cached words for this source, or None (corrupt-safe, tested)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or \
            data.get("version") != TRANSCRIPT_CACHE_VERSION:
        return None
    words = data.get("words")
    # Transcripts cached before the 2026-09-30 caption fix still hold
    # overlapping timings (duplicate subtitle lines) — repaired on load.
    return normalize_word_timings(words) if isinstance(words, list) else None


def save_transcript_cache(path: Path, words: list[dict]) -> None:
    """Persist the transcript (best-effort: caching never fails a run)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"version": TRANSCRIPT_CACHE_VERSION, "words": words}),
            encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------- work dirs
def ingest_transcript(cfg: Config, src: Path, source: dict, url: str,
                      work: Path, lane: str = "clip",
                      log=print) -> list[dict]:
    """Transcript for a source: cache -> Groq Whisper -> YouTube captions.

    The exact pipeline run_clip/run_parts run, factored out for the
    longform lane (they keep their own copies — working code isn't
    refactored for style). Prints with the calling lane's tag, masks
    nothing (the caller masks after, clip-lane rule), returns [] only
    when the source has no speech at all.
    """
    from scriptgen import get_provider

    provider = get_provider(cfg)
    source_key = transcript_cache_key(url, src)
    cache = transcript_cache_path(cfg, source_key)
    words = load_transcript_cache(cache)
    if words is not None:
        log(f"  [{lane}] transcript: cached ({len(words)} words)")
        return words
    audio = extract_audio(src, work)
    words = transcribe_words(audio, cfg, title=source["title"],
                             video_id=video_id_for_captions(source["url"]))
    if cfg.clip_transcript_fix:
        fixed = fix_transcript_words(words, source["title"], provider)
        changed = sum(1 for a, b in zip(words, fixed)
                      if a.get("word") != b.get("word"))
        if changed:
            log(f"  [{lane}] transcript fix: {changed} misheard "
                f"word(s) corrected")
            words = fixed
    save_transcript_cache(cache, words)
    log(f"  [{lane}] transcript: {len(words)} words (cached for re-runs)")
    return words


_RUN_SEQ = itertools.count(1)

def new_clip_work_dir(work_root: Path) -> Path:
    """A fresh, never-colliding run dir (tested).

    Old behaviour — a fixed 'clip_run' — meant two concurrent runs (or a
    run started while another's files were still warm) overwrote each
    other's downloads and frames.
    """
    import os

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return work_root / f"clip_run_{stamp}_{os.getpid()}_{next(_RUN_SEQ)}"


def prune_stale_runs(work_root: Path, keep: Path,
                     max_age_hours: float = 48.0) -> None:
    """Delete clip_run_* dirs older than the cutoff (best-effort, tested).

    Unique dirs would otherwise pile up forever after crashes; `keep` is
    never touched regardless of age.
    """
    import shutil as _shutil

    cutoff = time.time() - max_age_hours * 3600
    try:
        entries = list(work_root.glob("clip_run_*"))
    except OSError:
        return
    for entry in entries:
        if entry == keep:
            continue
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                _shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            pass


# ---------------------------------------------------------------- main
def plan_clip_sources(target: str, urls: list[str],
                      files: list[str]) -> list[tuple[str, str]]:
    """All sources to clip, in order (pure, tested).

    Positional shortcut first (if given), then every --url / --file in
    the order typed. One source is [(url, "")] or [("", file)].
    """
    sources: list[tuple[str, str]] = []
    pair = resolve_clip_target(target, "", "")
    if pair != ("", ""):
        sources.append(pair)
    for url in urls or []:
        if str(url).strip():
            sources.append((str(url).strip(), ""))
    for file in files or []:
        if str(file).strip():
            sources.append(("", str(file).strip()))
    return sources


def resolve_clip_target(target: str, url: str,
                        file: str) -> tuple[str, str]:
    """Positional shortcut -> (url, file) (pure, tested).

    `main.py clip <link-or-path>` is the natural way people call this
    (live 2026-09-23: a bare link died as 'unrecognized arguments').
    http(s) -> url, anything else -> file path; explicit flags always win.
    """
    target = (target or "").strip()
    if target and not (url or "").strip() and not (file or "").strip():
        if target.lower().startswith(("http://", "https://")):
            return target, ""
        return "", target
    return (url or "").strip(), (file or "").strip()


def resolve_clip_sub_pos(cfg: Config, sub_pos: str = "default") -> str:
    """Effective subtitle position for the clip lane (pure, tested).

    Double-default (no --sub-pos flag, no subtitles.position in config)
    resolves to bottom: centered captions sit on the fit-centered frame
    (STUDY F2), while bottom is shorts-safe and never covers the subject.
    Any explicit choice — flag or config — wins unchanged.
    """
    if (sub_pos or "default") == "default" \
            and cfg.subtitles_position == "default":
        return "bottom"
    return sub_pos or "default"


def run_clip(cfg: Config, url: str = "", file: str = "",
             max_clips: int = MAX_CLIPS_DEFAULT,
             min_len: int = MIN_CLIP_SECONDS,
             max_len: int = MAX_CLIP_SECONDS,
             use_vision: bool = True, keep_work: bool = False,
             out_dir: Path | None = None,
             sub_pos: str = "default",
             top_count: int = 0,
             whole: bool | None = None,
             half: bool = False) -> int:
    """The whole lane. Returns process exit code.

    whole: None = auto (source <= whole.under_seconds becomes ONE clip of
    the whole video — subs + fading title header, no moment picking),
    True/False = --whole / --no-whole. half: two near-equal clips split
    at the best sentence end near the middle ("Part 1/2" headers).
    """
    if not url and not file:
        raise ClipError("give me --url <youtube link> or --file <local mp4>")
    sub_pos = resolve_clip_sub_pos(cfg, sub_pos)
    out_root = Path(out_dir) if out_dir else cfg.root / "clips"
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
    print(f"  [clip] source: {src.name} ({duration/60:.0f} min, "
          f"{source['channel']})")
    from scriptgen import get_provider

    provider = get_provider(cfg)
    source_key = transcript_cache_key(url, src)
    cache = transcript_cache_path(cfg, source_key)
    words = load_transcript_cache(cache)
    if words is not None:
        print(f"  [clip] transcript: cached ({len(words)} words)")
    else:
        audio = extract_audio(src, work)
        words = transcribe_words(audio, cfg, title=source["title"],
                                 video_id=video_id_for_captions(source["url"]))
        if cfg.clip_transcript_fix:
            fixed = fix_transcript_words(words, source["title"], provider)
            changed = sum(1 for a, b in zip(words, fixed)
                          if a.get("word") != b.get("word"))
            if changed:
                print(f"  [clip] transcript fix: {changed} misheard "
                      f"word(s) corrected")
                words = fixed
        save_transcript_cache(cache, words)
        print(f"  [clip] transcript: {len(words)} words (cached for re-runs)")
    # Mask AFTER the cache: caches keep true words (the fix pass stays
    # effective on re-runs), every consumer below sees masked words.
    if cfg.subtitles_mask_profanity:
        from subtitles import mask_profanity

        words = [dict(w, word=mask_profanity(str(w.get("word") or "")))
                 for w in words]
    from parts import (PART_LEN_MIN, render_window_clips, render_whole_clip,
                       shorts_cap_warning, use_whole)

    if half:
        from cutpoints import (build_boundaries, cut_summary, detect_silences,
                               ensure_sentence_ends, plan_halves)

        if duration < 2 * PART_LEN_MIN:
            raise ClipError(f"{duration:.0f}s is too short to halve "
                            f"(need {2 * PART_LEN_MIN:.0f}s+)")
        words = ensure_sentence_ends(words or [], str(source.get("title")
                                                      or ""), provider,
                                     cache=cache)
        bounds = build_boundaries(words, detect_silences(src, duration),
                                  duration)
        plan = plan_halves(duration, bounds,
                           [float(w.get("start") or 0.0) for w in words])
        print(f"  [clip] halves: {plan[0][1]:.1f}s + "
              f"{duration - plan[0][1]:.1f}s — {cut_summary(plan, bounds)}")
        for start, end in plan:
            warning = shorts_cap_warning(end - start)
            if warning:
                print(f"  [clip] ⚠️ half {start:.0f}-{end:.0f}s: {warning}")
        kits = render_window_clips(cfg, src, source, words, plan, source_key,
                                   out_root, work, sub_pos, provider)
        for kit in kits:
            print(f"  [clip] {kit.name}.mp4: "
                  f"{(kit / 'TITLE.txt').read_text(encoding='utf-8')!r}")
        print(f"\n  2 clips + kits -> {out_root}")
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)
        return 0
    if duration > 0 and use_whole(duration, cfg.whole_under_seconds, whole):
        # Short source: nothing to mine — the whole thing is the clip.
        # Checked before the thin-transcript guard: a 40-second video
        # with 30 words is a fine short, not a failure.
        print(f"  [clip] {duration:.0f}s source — kept whole: one short, "
              f"title header, no cutting (--no-whole to mine moments)")
        warning = shorts_cap_warning(duration)
        if warning:
            print(f"  [clip] ⚠️ {warning}")
        kit = render_whole_clip(cfg, src, source, words or [], duration,
                                source_key, out_root, work, sub_pos,
                                provider)
        print(f"  [clip] {kit.name}.mp4: "
              f"{(kit / 'TITLE.txt').read_text(encoding='utf-8')!r}")
        print(f"\n  1 clip + kit -> {out_root}")
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)
        return 0
    if len(words) < 40:
        raise ClipError("transcript too thin to mine for moments")
    windows = split_windows(words)
    candidates = []
    for index, win in enumerate(windows, start=1):
        if len(windows) == 1:
            print("  [clip] picking moments...")
        else:
            print(f"  [clip] picking moments (window {index}/{len(windows)}: "
                  f"{_ts(win[0]['start'])}-{_ts(win[-1]['end'])})...")
        prompt = build_picker_prompt(transcript_lines(win), cfg,
                                     max_clips, min_len, max_len)
        raw = provider.generate_text(prompt, temperature=0.4, tag="clippick",
                                     json_mode=True)
        candidates += parse_candidates(raw, duration, max_clips,
                                       min_len, max_len,
                                       lo=win[0]["start"], hi=win[-1]["end"])
    candidates = [apply_hook_start(c, min_len) for c in candidates]
    candidates = [snap_candidate(c, words, min_len, max_len)
                  for c in candidates]
    candidates = dedupe_overlaps(candidates)[:max_clips]
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
    entries: list[dict] = []
    for index, cand in enumerate(accepted, start=1):
        clip_path = out_root / f"{source_key[:8]}_clip_{index:02d}.mp4"
        render_clip(src, cand, words, cfg, clip_path, work, sub_pos,
                    caps=cfg.subtitles_caps)
        clip_words_list = clip_words(words, cand.start, cand.end)
        title = pick_title(cand, clip_words_list)
        if cfg.clip_polish_titles:
            from editorial import polish_clip_title

            narration = " ".join(w["word"] for w in clip_words_list)
            polished = polish_clip_title(title, narration, provider)
            if polished:
                title = polished
        kits.append(write_kit(clip_path, title, cand, source, out_root))
        entries.append({"path": clip_path, "title": title,
                        "score": cand.score, "len": cand.end - cand.start})
        print(f"  [clip] {clip_path.name}: {title!r}")
    print(f"\n  {len(kits)} clip(s) + kits -> {out_root}")
    if top_count and entries:
        try:
            from top import build_top

            build_top(cfg, entries, source, source_key, out_root,
                      top_count, work)
        except Exception as exc:  # noqa: BLE001 - the compilation is a bonus
            print(f"  [top] compilation failed ({str(exc)[:120]}) — "
                  "clips are unaffected")
    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)
    return 0
