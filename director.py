"""The director brain v1: connects script beats to visuals.

plan_query(prompt, cfg) turns one art prompt ("cinematic still of a Japanese
vending machine glowing at night, wide establishing shot") into a short
stock-photo search query ("japanese vending machine night"). It tries a tiny
LLM call first (first free lane that answers), cached per scene-base, and any
failure — or no LLM keys at all — falls back to heuristic keyword extraction.

Never raises: worst case returns generic-but-sane words, and the stock lane
retries with even less before yielding to the next image provider. v1 only
plans queries; pacing notes and hook scores join later.
"""

from __future__ import annotations

import re

# Words that never help a stock search: filler + camera jargon the art
# prompts are full of (shot framings would poison every query).
_STOP = frozenset("""
a an the and or of to in on at for with from by as is are was were be been
this that these those it its into out over after before during
photorealistic cinematic photorealistic ultra detailed render 8k
shot view angle angles perspective composition lighting establishing
wide medium closeup close-up close extreme dramatic low high aerial
side profile symmetrical centered symmetric shallow depth field moody
new another other full scene
cartoon vector anime illustration illustrations clipart painting paintings
drawing drawings sketch sketches watercolor cgi 3d artwork digital
""".split())

_CACHE: dict[str, str] = {}


def _normalize(prompt: str) -> str:
    """Strip framing suffixes so all slots of a scene share one query."""
    try:
        from images import SHOT_STYLES  # lazy: images imports stock, not vice versa
    except ImportError:
        shot_styles: tuple[str, ...] = ()
    else:
        shot_styles = tuple(SHOT_STYLES)
    base = prompt.lower()
    for style in shot_styles:
        base = base.replace(style.lower(), " ")
    base = re.sub(r"[^a-z0-9\s]", " ", base)
    return re.sub(r"\s+", " ", base).strip()[:300]


def heuristic_keywords(text: str, limit: int = 4) -> str:
    """Best-effort query from word shape alone (no LLM, pure, tested)."""
    words = [word for word in _normalize(text).split()
             if len(word) > 2 and word not in _STOP]
    seen: list[str] = []
    for word in words:
        if word not in seen:
            seen.append(word)
        if len(seen) >= limit:
            break
    return " ".join(seen) if seen else "cinematic b-roll"


def _llm_query(provider, base: str) -> str:
    raw = provider.generate_text(
        "Reply with ONLY 3-5 lowercase words: a stock-photo search query "
        f"capturing this scene (objects + setting, no camera terms): {base[:250]}",
        temperature=0.2, tag="director")
    words = re.sub(r"[^a-z0-9\s]", "", raw.lower()).split()[:5]
    return " ".join(words)


def plan_query(prompt: str, cfg) -> str:
    """Short stock query for one art prompt (LLM first, heuristic fallback)."""
    base = _normalize(prompt)
    if not base:
        return "cinematic b-roll"
    if base in _CACHE:
        return _CACHE[base]
    query = ""
    try:
        from scriptgen import get_provider

        query = _llm_query(get_provider(cfg), base)
    except Exception:  # noqa: BLE001 - brain failure falls back, never fatal
        query = ""
    if not query:
        query = heuristic_keywords(base)
    _CACHE[base] = query
    return query
