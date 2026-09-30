"""Where to cut: sentence ends, confirmed by real pauses in the audio.

Parts / halves used to snap only to words ending in . ! ? — but the
lanes harvest YouTube auto-captions first, and those carry NO
punctuation, so cuts fell back to "any word" or the exact second and
landed mid-sentence. Three layers fix that:

1. Sentence ends. Punctuated transcripts (Whisper) already have them.
   Unpunctuated ones get ONE cheap LLM pass (numbered words in, the
   indices of sentence-final words out — a few dozen tokens back, can't
   garble the text), cached next to the transcript so re-runs are free.
   Stored as an `eos` flag on the word: subtitles never change.
2. Pauses. FFmpeg silencedetect over the source audio (local, seconds,
   free). Caption timings are spread evenly per line, so a sentence end
   is moved into the real silence right after it — no clipped syllable.
3. Choice, best first: sentence end inside a pause > sentence end (also
   searched in a doubled window) > plain pause > word start > exact.

Everything here degrades instead of failing: no LLM, no ffmpeg, no
words — you still get a plan.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

SILENCE_DB = -30          # quieter than this counts as a pause
SILENCE_MIN = 0.2         # shortest silence silencedetect reports (s)
PAUSE_ONLY_MIN = 0.45     # a pause with no sentence end must be this long
SNAP_TO_SILENCE = 1.5     # how far a sentence end may move to its pause
PUNCT_PER_100 = 2.0       # below this density the transcript is "unpunctuated"
EOS_BATCH = 400           # words per sentence-end LLM call
TIER_BOTH, TIER_SENTENCE, TIER_PAUSE = 3, 2, 1
TIER_NAMES = {TIER_BOTH: "sentence end in a pause",
              TIER_SENTENCE: "sentence end", TIER_PAUSE: "pause"}


# ------------------------------------------------------------ sentence ends
def is_eos(word: dict) -> bool:
    text = str(word.get("word") or "")
    return bool(word.get("eos")) or text[-1:] in (".", "!", "?")


def has_punctuation(words: list[dict]) -> bool:
    """Enough sentence ends already? (pure, tested)"""
    if len(words) < 30:
        return any(is_eos(w) for w in words)
    ends = sum(1 for w in words if is_eos(w))
    return ends * 100.0 / len(words) >= PUNCT_PER_100


def _words_hash(words: list[dict]) -> str:
    text = " ".join(str(w.get("word") or "") for w in words)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def restore_sentence_ends(words: list[dict], title: str,
                          provider) -> set[int]:
    """LLM pass: indices of sentence-final words. Never raises.

    Guards: ints only, in-batch range, and a batch claiming a sentence
    end more often than every 3 words is discarded as garbage.
    """
    from scriptgen import extract_json

    ends: set[int] = set()
    for start in range(0, len(words), EOS_BATCH):
        stop = min(start + EOS_BATCH, len(words))
        numbered = "\n".join(f"{j}: {words[start + j].get('word', '')}"
                             for j in range(stop - start))
        prompt = (
            "This is an unpunctuated automatic transcript of a video"
            + (f" titled {title!r}" if (title or "").strip() else "")
            + ", numbered word by word.\n"
            "Mark the words that END a sentence — where a period, question "
            "mark or exclamation mark belongs. Spoken sentences are usually "
            "8-25 words; do not mark every small pause or comma.\n"
            'Return ONLY JSON: {"ends": [<index>, ...]}\n'
            "NUMBERED WORDS:\n" + numbered)
        try:
            raw = provider.generate_text(prompt, temperature=0.0,
                                         tag="clipeos", json_mode=True)
            data = extract_json(raw)
        except Exception:  # noqa: BLE001 - boundaries are an upgrade
            continue
        found = data.get("ends") if isinstance(data, dict) else None
        if not isinstance(found, list):
            continue
        batch: set[int] = set()
        for item in found:
            if isinstance(item, bool):
                continue
            try:
                j = int(item)
            except (TypeError, ValueError):
                continue
            if 0 <= j < stop - start and float(item) == j:
                batch.add(start + j)
        if batch and len(batch) * 3 > (stop - start):
            continue  # "every word ends a sentence" = the model misread us
        ends |= batch
    return ends


def eos_cache_path(transcript_cache: Path) -> Path:
    return transcript_cache.with_name(transcript_cache.stem + ".eos.json")


def ensure_sentence_ends(words: list[dict], title: str, provider,
                         cache: Path | None = None,
                         log=print) -> list[dict]:
    """Words with `eos` flags where sentences end (copies; never raises).

    Punctuated transcripts pass through untouched. Otherwise the cached
    result is reused when it matches these exact words, else one LLM
    pass runs and is cached.
    """
    if not words or has_punctuation(words):
        return words
    digest = _words_hash(words)
    ends: set[int] | None = None
    side = eos_cache_path(cache) if cache else None
    if side and side.exists():
        try:
            data = json.loads(side.read_text(encoding="utf-8"))
            if data.get("hash") == digest and data.get("n") == len(words):
                ends = {int(i) for i in data.get("ends") or []
                        if 0 <= int(i) < len(words)}
                log(f"  [cuts] sentence ends: cached ({len(ends)})")
        except Exception:  # noqa: BLE001 - corrupt cache = recompute
            ends = None
    if ends is None:
        log("  [cuts] transcript has no punctuation — finding sentence "
            "ends (one small LLM pass, cached)...")
        ends = restore_sentence_ends(words, title, provider)
        if ends and side:
            try:
                side.parent.mkdir(parents=True, exist_ok=True)
                side.write_text(json.dumps({"hash": digest, "n": len(words),
                                            "ends": sorted(ends)}),
                                encoding="utf-8")
            except OSError:
                pass
        log(f"  [cuts] sentence ends: {len(ends)} found"
            + ("" if ends else " — falling back to pauses"))
    return [dict(w, eos=True) if i in ends else w
            for i, w in enumerate(words)]


# ------------------------------------------------------------------ pauses
_SIL_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SIL_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def parse_silences(stderr: str, duration: float = 0.0
                   ) -> list[tuple[float, float]]:
    """silencedetect log -> [(start, end)] (pure, tested).

    An unterminated final silence (runs to the end) closes at duration.
    """
    out: list[tuple[float, float]] = []
    start: float | None = None
    for line in (stderr or "").splitlines():
        m = _SIL_START.search(line)
        if m:
            start = max(0.0, float(m.group(1)))
            continue
        m = _SIL_END.search(line)
        if m and start is not None:
            end = float(m.group(1))
            if end > start:
                out.append((start, end))
            start = None
    if start is not None and duration > start:
        out.append((start, float(duration)))
    return out


def detect_silences(src: Path, duration: float = 0.0,
                    noise_db: int = SILENCE_DB,
                    min_len: float = SILENCE_MIN) -> list[tuple[float, float]]:
    """Real pauses in the source audio via FFmpeg. [] on any failure."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(src), "-vn",
           "-af", f"silencedetect=noise={int(noise_db)}dB:d={min_len}",
           "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=600, errors="replace")
    except Exception:  # noqa: BLE001 - pauses are an upgrade, not a need
        return []
    return parse_silences(proc.stderr or "", duration)


# -------------------------------------------------------------- boundaries
def build_boundaries(words: list[dict], silences: list[tuple[float, float]],
                     duration: float) -> list[tuple[float, int]]:
    """Scored cut points [(time, tier)] sorted by time (pure, tested).

    Sentence end + a pause nearby -> cut in the middle of that pause
    (TIER_BOTH). Sentence end alone -> cut just after the last word,
    inside any gap before the next one (TIER_SENTENCE). Long pauses with
    no sentence end -> TIER_PAUSE. The video's final word is never a cut.
    """
    out: dict[float, int] = {}
    used: set[int] = set()
    n = len(words)
    for i, word in enumerate(words):
        if not is_eos(word) or i + 1 >= n:
            continue
        w_start = float(word.get("start") or 0.0)
        last_end = float(word.get("end") or w_start)
        nxt = words[i + 1]
        next_start = float(nxt.get("start") or last_end)
        next_end = float(nxt.get("end") or next_start)
        best, best_gap = None, None
        for k, (s, e) in enumerate(silences):
            mid = (s + e) / 2
            # The pause must sit between this word and the end of the next
            # one (caption timings are estimates), near the sentence end.
            if not (w_start <= mid <= max(next_end, last_end + 0.2)):
                continue
            gap = 0.0 if s <= last_end <= e else min(abs(s - last_end),
                                                     abs(e - last_end))
            if gap <= SNAP_TO_SILENCE and (best_gap is None or gap < best_gap):
                best, best_gap = k, gap
        if best is not None:
            s, e = silences[best]
            used.add(best)
            t = round((s + e) / 2, 3)
            out[t] = max(out.get(t, 0), TIER_BOTH)
        else:
            gap = max(0.0, next_start - last_end)
            t = round(last_end + min(0.25, gap / 2), 3)
            out[t] = max(out.get(t, 0), TIER_SENTENCE)
    for k, (s, e) in enumerate(silences):
        if k in used or (e - s) < PAUSE_ONLY_MIN:
            continue
        t = round((s + e) / 2, 3)
        if 0 < t < duration:
            out.setdefault(t, TIER_PAUSE)
    return sorted((t, tier) for t, tier in out.items()
                  if 0 < t < (duration or float("inf")))


def pick_cut(ideal: float, lo: float, hi: float, window: float,
             bounds: list[tuple[float, int]],
             word_starts: list[float] | None = None,
             word_radius: float = 2.0) -> tuple[float, int]:
    """Best cut near ideal, strictly inside (lo, hi) -> (time, tier).

    tier 0 = word/exact fallback. Order: sentence ends (a pause-backed
    one wins unless 2s+ farther) within window, then within 2x window,
    then plain pauses within window, then the nearest word start, then
    the exact ideal (clamped into range). lo/hi keep a wide search from
    ever producing a sliver of a part.
    """
    legal = [(t, tier) for t, tier in bounds if lo < t < hi]

    def nearest(pool, radius):
        near = [(t, tier) for t, tier in pool if abs(t - ideal) <= radius]
        if not near:
            return None
        return min(near, key=lambda b: abs(b[0] - ideal)
                   - (2.0 if b[1] == TIER_BOTH else 0.0))

    sentences = [b for b in legal if b[1] >= TIER_SENTENCE]
    for pool, radius in ((sentences, window), (sentences, 2 * window),
                         ([b for b in legal if b[1] == TIER_PAUSE], window)):
        hit = nearest(pool, radius)
        if hit:
            return hit
    near = [s for s in (word_starts or [])
            if lo < s < hi and abs(s - ideal) <= word_radius]
    if near:
        return (min(near, key=lambda s: abs(s - ideal)), 0)
    return (min(max(ideal, lo), hi) if lo < hi else ideal, 0)


def plan_halves(duration: float, bounds: list[tuple[float, int]],
                word_starts: list[float] | None = None
                ) -> list[tuple[float, float]]:
    """Two near-equal halves split at the best cut near the middle.

    Window = 5% of the length, clamped to 5-15 s (doubled for sentence
    ends before settling for less), so halves stay close to equal.
    """
    duration = float(duration or 0.0)
    if duration <= 0:
        return []
    window = max(5.0, min(15.0, duration * 0.05))
    cut, _ = pick_cut(duration / 2, duration * 0.25, duration * 0.75,
                      window, bounds, word_starts)
    return [(0.0, cut), (cut, duration)]


def cut_summary(plan: list[tuple[float, float]],
                bounds: list[tuple[float, int]]) -> str:
    """'4 cut(s): 3 sentence end in a pause, 1 sentence end' (pure)."""
    cuts = [end for _, end in plan[:-1]]
    if not cuts:
        return ""
    tiers = dict(bounds)
    counts: dict[str, int] = {}
    for cut in cuts:
        tier = tiers.get(round(cut, 3), 0)
        name = TIER_NAMES.get(tier, "fallback (no sentence end nearby)")
        counts[name] = counts.get(name, 0) + 1
    order = list(TIER_NAMES.values()) + ["fallback (no sentence end nearby)"]
    parts = [f"{counts[k]} {k}" for k in order if k in counts]
    return f"{len(cuts)} cut(s): " + ", ".join(parts)
