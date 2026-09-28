"""Virality scoring for pre-gen clips: which clip deserves the phone push.

Pure functions, no network, no API keys. The score comes from the clip's
own transcript + title + length, reusing the retention lane's helpers
(topic scorer, weak-hook guard) so the rater and the renderer agree on
what a good hook is.

Signals (weights sum to 100):
  hook     30  first ~3 seconds: concrete number / direct address /
               punchy-short — penalized for question hooks and weak
               openers, exactly like editorial.hook_violated (the rater
               must agree with the renderer: live data says "Why…?"
               hooks retain worse than punchy statements)
  topic    25  topics.score_topic_interest(), mapped onto 0-25
  length   20  Shorts sweet-spot curve (peak 20-40s)
  density  15  speech pace 2.0-3.5 words/sec
  title    10  editorial.title_punch_violated + digit + no shouty caps
"""

from __future__ import annotations

import re

HOOK_WEIGHT = 30
TOPIC_WEIGHT = 25
LENGTH_WEIGHT = 20
DENSITY_WEIGHT = 15
TITLE_WEIGHT = 10

_YOU_RE = re.compile(r"\b(you|your|yours|you're|youve|you'll)\b", re.I)
_DIGIT_RE = re.compile(r"\d")


def score_hook(text: str) -> tuple[int, list[str]]:
    """First-seconds hook text -> (points 0-30, reason lines). Pure."""
    from editorial import hook_violated

    words = (text or "").split()
    if not words:
        return 0, ["hook unscored — no transcript"]
    if len(words) < 4:
        return 3, ["hook too thin to judge"]
    head = (text or "").strip()
    violated = hook_violated(head)
    points = 12
    reasons = ["audible hook"]
    if _DIGIT_RE.search(head[:140]):
        points += 6
        reasons.append("hook has a number (+6)")
    if _YOU_RE.search(head[:140].replace("’", "'")):
        points += 4
        reasons.append("hook talks to you (+4)")
    first = re.split(r"[.?!…]+", head, maxsplit=1)[0].strip()
    if not violated and 0 < len(first.split()) <= 8:
        points += 8
        reasons.append("hook is punchy-short (+8)")
    if violated:
        points -= 12
        reasons.append("question/weak hook (−12)")
    return max(0, min(HOOK_WEIGHT, points)), reasons


def score_length(duration_s: float) -> tuple[int, list[str]]:
    """Clip length -> (points 0-20, reason lines). Pure."""
    try:
        seconds = float(duration_s)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds <= 0:
        return 4, ["length unknown"]
    if 20 <= seconds <= 40:
        return 20, ["20–40s sweet spot"]
    if 12 <= seconds < 20 or 40 < seconds <= 60:
        return 14, ["good length"]
    if 8 <= seconds < 12 or 60 < seconds <= 90:
        return 8, ["awkward length for Shorts"]
    return 4, ["awkward length for Shorts"]


def score_density(words_per_sec: float | None) -> tuple[int, list[str]]:
    """Speech pace -> (points 0-15, reason lines). Pure."""
    if words_per_sec is None:
        return 7, ["pace unscored — no transcript"]
    try:
        wps = float(words_per_sec)
    except (TypeError, ValueError):
        return 7, ["pace unscored — no transcript"]
    if wps <= 0:
        return 7, ["pace unscored — no transcript"]
    if 2.0 <= wps <= 3.5:
        return 15, [f"pace {wps:.1f} words/sec"]
    if 1.2 <= wps < 2.0 or 3.5 < wps <= 4.5:
        return 10, [f"pace {wps:.1f} words/sec"]
    slow = "dragging" if wps < 1.2 else "rushed"
    return 5, [f"pace {wps:.1f} words/sec ({slow})"]


def _is_shouty(title: str) -> bool:
    letters = [c for c in title if c.isalpha()]
    if len(letters) < 12:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters) > 0.6


def score_title(title: str) -> tuple[int, list[str]]:
    """Title packaging -> (points 0-10, reason lines). Pure."""
    from editorial import title_punch_violated

    text = (title or "").strip()
    if not text:
        return 0, ["no title"]
    if len(text) < 15:
        points, reasons = 3, ["title very short"]
    elif title_punch_violated(text):
        points, reasons = 2, ["title parses slowly"]
    else:
        points, reasons = 7, ["title punches"]
    if _DIGIT_RE.search(text):
        points += 3
        reasons.append("title has a number (+3)")
    if _is_shouty(text):
        points -= 3
        reasons.append("title shouts (−3)")
    return max(0, min(TITLE_WEIGHT, points)), reasons


def _topic_points(topic_score: int) -> int:
    if topic_score >= 5:
        return 25
    if topic_score >= 3:
        return 20
    if topic_score >= 1:
        return 14
    if topic_score == 0:
        return 8
    return 3


def score_clip(title: str, hook_text: str, word_count: int,
               duration_s: float) -> dict:
    """Whole-clip verdict (pure). Returns score/reasons/signals.

    word_count = transcript words inside the clip window (0 when the
    transcript cache is missing — pace then scores neutral, honestly).
    """
    from topics import score_topic_interest

    hook_pts, hook_why = score_hook(hook_text)
    length_pts, length_why = score_length(duration_s)
    try:
        seconds = float(duration_s)
    except (TypeError, ValueError):
        seconds = 0.0
    wps = (word_count / seconds
           if word_count > 0 and seconds > 0 else None)
    density_pts, density_why = score_density(wps)
    title_pts, title_why = score_title(title)
    topic_score, topic_flags = score_topic_interest(title or "")
    topic_pts = _topic_points(topic_score)
    topic_why = [f"topic pull {topic_score:+d}"]
    topic_why += list(topic_flags[:2])

    signals = {"hook": hook_pts, "topic": topic_pts, "length": length_pts,
               "density": density_pts, "title": title_pts}
    reasons = hook_why + topic_why + length_why + density_why + title_why
    return {"score": sum(signals.values()), "reasons": reasons,
            "signals": signals}


def pick_best(scored: list[dict]) -> dict | None:
    """Highest score wins; ties break toward the shorter clip (pure)."""
    if not scored:
        return None
    return min(scored, key=lambda e: (-int(e.get("score") or 0),
                                     float(e.get("duration_s") or 0.0)))
