"""Editorial passes: punch-up (retention) + decringe (taste veto).

These run after factcheck, before voiceover. Both are narrow LLM rewrites
over the finished scenes; both are validation-gated and never fatal — any
failure keeps the previous version with a printed note, never a crash.
"""

from __future__ import annotations

import json
import re

from config import Config


def polish_script(script, cfg: Config, provider) -> None:
    """Punch-up then decringe, in place. Never raises, never breaks a render."""
    if not cfg.editorial_enabled:
        return
    for stage, func in (("punch-up", _punch_up), ("hook fix", _fix_hook),
                        ("title fix", _fix_title),
                        ("title polish", _optimize_title),
                        ("decringe", _decringe)):
        try:
            func(script, cfg, provider)
        except Exception as exc:  # noqa: BLE001 - polish never kills a render
            print(f"      editorial : {stage} skipped ({str(exc)[:100]})")


def _scenes_payload(script) -> str:
    return json.dumps(
        [{"n": i, "narration": s.narration} for i, s in enumerate(script.scenes, 1)],
        ensure_ascii=False)


def _apply(script, raw: str, stage: str, max_growth: float) -> bool:
    """Validate + apply a stage's JSON. False = keep the previous version."""
    from scriptgen import extract_json

    try:
        data = extract_json(raw)
    except ValueError:
        print(f"      editorial : {stage} returned no JSON — keeping previous.")
        return False
    scenes = data.get("scenes") if isinstance(data, dict) else None
    if not isinstance(scenes, list) or len(scenes) != len(script.scenes):
        print(f"      editorial : {stage} changed scene count — keeping previous.")
        return False
    narrations = []
    for item in scenes:
        text = (item.get("narration") or "").strip() if isinstance(item, dict) else ""
        if not text:
            print(f"      editorial : {stage} emptied a scene — keeping previous.")
            return False
        narrations.append(text)
    old_words = sum(len(s.narration.split()) for s in script.scenes)
    new_words = sum(len(t.split()) for t in narrations)
    if old_words and new_words > old_words * max_growth:
        print(f"      editorial : {stage} bloated {old_words}->{new_words} words — "
              f"keeping previous.")
        return False
    for scene, text in zip(script.scenes, narrations):
        scene.narration = text
    print(f"      editorial : {stage} applied ({old_words}->{new_words} words).")
    return True


_QUESTION_OPENERS = ("why ", "what ", "how ", "who ", "when ", "where ",
                     "which ", "is ", "are ", "was ", "were ", "do ", "does ",
                     "did ", "can ", "could ", "have ", "has ", "will ",
                     "would ")

# Throat-clearing that wastes the 3 seconds where scrollers decide.
_BANNED_HOOK_OPENERS = ("here's why", "here is why", "let me tell", "fun fact",
                        "in this video", "today we", "today i", "imagine if",
                        "believe it or not")


# Vague tails that parse slowly — the titles that earned 3-8 views while
# short concrete ones earned 800-1,100 (channel analytics, 2026-09-19).
_BANNED_TITLE_PHRASES = ("want you to", "don't want you", "they still want",
                         "nobody tells you", "not what you think")


def title_punch_violated(title: str) -> bool:
    """True if the title parses too slowly to stop a scroller (pure, tested).

    Live-data rule: 4-8 words with the concrete subject up front earned
    800-1,100 views; 9-10 word titles with vague tails earned 3-8.
    """
    text = re.sub(r"\s*#\S+", "", title or "").strip()
    if not text:
        return False
    if len(text.split()) > 8 or len(text) > 52:
        return True
    lowered = text.lower()
    return any(phrase in lowered for phrase in _BANNED_TITLE_PHRASES)


def _fix_title(script, cfg: Config, provider) -> None:
    """One targeted retitle when the title parses too slowly.

    Highest-leverage line in the pipeline: the same video can 6x its views
    from a title change alone (honey, 2026-09-19). Same narrow-single-line
    pattern as the hook fix; validation-gated, failures keep the original.
    """
    from scriptgen import extract_json

    title = getattr(script, "title", "") or ""
    if not title or not title_punch_violated(title):
        return
    prompt = (
        "Rewrite this YouTube Shorts title as a scroll-stopper.\n"
        "Hard rules: 4-8 words; under 50 characters; Why/How when it fits; "
        "the topic's concrete subject (animal, object, place) in the first "
        "3 words; same fact; no hashtags; no vague tails like 'they don't "
        "want you to know'.\n"
        f"TITLE: {title}\n"
        'Return ONLY JSON: {"title": "..."}'
    )
    print("      editorial : title fix pass...")
    try:
        raw = provider.generate_text(prompt, temperature=0.5, tag="titlefix",
                                     json_mode=True)
        data = extract_json(raw)
    except Exception as exc:  # noqa: BLE001 - polish never kills a render
        print(f"      editorial : title fix failed ({str(exc)[:100]}) — keeping previous.")
        return
    new = data.get("title") if isinstance(data, dict) else None
    new = re.sub(r"\s*#\S+", "", new).strip() if isinstance(new, str) else ""
    if not new or new.lower() == title.lower() or title_punch_violated(new):
        print("      editorial : title fix returned no usable title — keeping previous.")
        return
    script.title = new
    print(f"      editorial : title fix applied ({title[:40]!r} -> {new[:40]!r}).")


# Real channel analytics, 2026-09-20 — the closest thing to training data
# a free tier gets: few-shot examples drawn from what actually earned views.
_WINNING_TITLES = (
    "Why We Close Our Eyes When We Sneeze",     # 1,118 views
    "Why Cats Break The Laws Of Physics",       # 989
    "How Honey Never Expires",                  # 885 (retitle: 146 -> 885)
    "The Strange Reason Clocks Go Clockwise",   # 817
    "Why Zebras Have Stripes",                  # 492
    "How The QWERTY Keyboard Was Born",         # 462 (retitle: 8 -> 462)
)
_FLOPPED_TITLES = (
    "The Great Diamond Lie They Still Want You to Believe",  # 3 views
    "Why Your Keyboard Was Built to Slow You Down",          # 9 words, 8 views
)


def _title_keywords(narrations: list[str]) -> list[str]:
    """The script's subject words, most frequent first (pure, tested)."""
    from collections import Counter

    from topics import _content_words

    counter: Counter = Counter()
    for narration in narrations:
        counter.update(word for word in _content_words(narration or "")
                       if len(word) > 3)
    return [word for word, _ in counter.most_common(4)]


def score_title(title: str, keywords: list[str]) -> int:
    """View-potential score for a title (pure, tested; higher = better).

    Encodes the channel's live results: 4-7 words, <=45 chars, Why/How/The
    opener, the subject word present (searchability — Shorts titles feed
    YouTube search), no hype mechanics. Violating shapes score -10.
    """
    text = re.sub(r"\s*#\S+", "", title or "").strip()
    if not text or title_punch_violated(text):
        return -10
    score = 0
    words = text.split()
    if 4 <= len(words) <= 7:
        score += 2
    elif len(words) >= 9:
        score -= 3
    if len(text) <= 45:
        score += 1
    elif len(text) > 55:
        score -= 1
    if text.startswith(("Why ", "How ", "The ", "What ")):
        score += 2
    lowered = text.lower()
    for index, keyword in enumerate(keywords):
        if keyword in lowered or keyword.rstrip("s") in lowered:
            score += 2 if index == 0 else 1
    if any(phrase in lowered for phrase in _BANNED_TITLE_PHRASES):
        score -= 3
    if any(word.isupper() and len(word) >= 3 for word in words):
        score -= 2
    if "!" in text:
        score -= 1
    return score


def _optimize_title(script, cfg: Config, provider) -> None:
    """Few-shot title rewrite: 5 candidates, best scorer wins (tested).

    The free-tier equivalent of "train an AI on our titles": the model sees
    this channel's real winners and flops, then a deterministic scorer
    (score_title) picks the best candidate — swaps only on a clear win,
    keeps the current title otherwise. Never raises; no title -> no call.
    """
    from scriptgen import extract_json

    title = getattr(script, "title", "") or ""
    if not title:
        return
    narrations = [scene.narration for scene in script.scenes] \
        if getattr(script, "scenes", None) else []
    keywords = _title_keywords(narrations)
    if not keywords:
        return
    current = score_title(title, keywords)
    prompt = (
        "Rewrite this YouTube Shorts title to maximize views.\n"
        f"CURRENT TITLE: {title}\n"
        f"SUBJECT WORDS (the title must contain the main one): "
        f"{', '.join(keywords)}\n"
        f"OPENING LINES: {' '.join(narrations)[:280]}\n\n"
        "PROVEN WINNERS on this channel (copy the style, never the words):\n"
        + "\n".join(f"  - {won}" for won in _WINNING_TITLES)
        + "\nFLOPPED (never write like this):\n"
        + "\n".join(f"  - {lost}" for lost in _FLOPPED_TITLES)
        + "\nRULES: 4-7 words; under 45 characters; Why/How/The opener; the "
        "main subject word present (searchability); a curiosity gap with no "
        "lies; no hashtags, no ALL CAPS, no quotes.\n"
        'Return ONLY JSON: {"titles": ["candidate 1", "candidate 2", '
        '"candidate 3", "candidate 4", "candidate 5"]}'
    )
    print("      editorial : title polish pass...")
    try:
        raw = provider.generate_text(prompt, temperature=0.7,
                                     tag="titlepolish", json_mode=True)
        data = extract_json(raw)
    except Exception as exc:  # noqa: BLE001 - polish never kills a render
        print(f"      editorial : title polish failed ({str(exc)[:100]}) — keeping previous.")
        return
    candidates = data.get("titles") if isinstance(data, dict) else None
    if not isinstance(candidates, list):
        print("      editorial : title polish returned no candidates — keeping previous.")
        return
    scored = []
    for candidate in candidates:
        if isinstance(candidate, str):
            candidate = re.sub(r"\s*#\S+", "", candidate).strip()
            score = score_title(candidate, keywords)
            if score > -10:
                scored.append((score, candidate))
    if not scored:
        print("      editorial : title polish found no usable candidate — keeping previous.")
        return
    best_score, best = max(scored, key=lambda pair: pair[0])
    if best_score >= current + 2:
        print(f"      editorial : title polish applied (score {current}->"
              f"{best_score}: {title[:38]!r} -> {best[:38]!r}).")
        script.title = best
    else:
        print(f"      editorial : title polish kept current "
              f"(best candidate scored {best_score} vs {current}).")


def polish_clip_title(draft: str, narration: str, provider) -> str | None:
    """Title-polish for CLIP titles — the generate lane's system, clip-sized.

    Clip titles were the lane's weakest link (live 2026-09-22: 'Why
    Jellyfish Breaks The Rules' — pure template-think). Same few-shot
    winners/flops + score gate as _optimize_title; returns the better
    title or None to keep the draft. Never raises.
    """
    from scriptgen import extract_json

    if not draft:
        return None
    keywords = _title_keywords([narration or ""])
    if not keywords:
        return None
    current = score_title(draft, keywords)
    prompt = (
        "Rewrite this YouTube Shorts title to maximize views.\n"
        f"CURRENT TITLE: {draft}\n"
        f"SUBJECT WORDS (the title must contain the main one): "
        f"{', '.join(keywords)}\n"
        f"WHAT THE CLIP SAYS: {(narration or '')[:280]}\n\n"
        "PROVEN WINNERS on this channel (copy the style, never the words):\n"
        + "\n".join(f"  - {won}" for won in _WINNING_TITLES)
        + "\nFLOPPED (never write like this):\n"
        + "\n".join(f"  - {lost}" for lost in _FLOPPED_TITLES)
        + "\nRULES: 4-7 words; under 45 characters; Why/How/The opener; the "
        "main subject word present (searchability); a curiosity gap with no "
        "lies; no hashtags, no ALL CAPS, no quotes.\n"
        'Return ONLY JSON: {"titles": ["candidate 1", "candidate 2", '
        '"candidate 3", "candidate 4", "candidate 5"]}'
    )
    try:
        raw = provider.generate_text(prompt, temperature=0.7,
                                     tag="cliptitle", json_mode=True)
        data = extract_json(raw)
    except Exception as exc:  # noqa: BLE001 - polish never kills a clip
        print(f"  [clip] title polish failed ({str(exc)[:80]}) — keeping draft.")
        return None
    candidates = data.get("titles") if isinstance(data, dict) else None
    scored = []
    for candidate in candidates if isinstance(candidates, list) else []:
        if isinstance(candidate, str):
            candidate = re.sub(r"\s*#\S+", "", candidate).strip()
            score = score_title(candidate, keywords)
            if score > -10:
                scored.append((score, candidate))
    if not scored:
        return None
    best_score, best = max(scored, key=lambda pair: pair[0])
    if best_score >= current + 2:
        print(f"  [clip] title polish: {draft[:38]!r} -> {best[:38]!r} "
              f"(score {current}->{best_score})")
        return best
    return None


def scrub_narration(text: str) -> str:
    """Spoken-word cleanup: no dot-dot-dot pauses, no stage directions.

    ONE function used by TTS and captioning alike (same words in, so word
    timings stay aligned): "..." reads as dead air in every voice engine and
    prints literally in captions, and "[pause]"-style asides are unspoken.
    """
    cleaned = re.sub(r"\[.*?\]", " ", text or "")
    cleaned = cleaned.replace("\u2026", "...")
    cleaned = re.sub(r"([?!])\.{2,}", r"\1", cleaned)
    cleaned = re.sub(r"\.{2,}", ",", cleaned)
    cleaned = re.sub(r",(\s*,)+", ",", cleaned)
    cleaned = re.sub(r"(^|[.!?]\s*),\s*", r"\1", cleaned)
    cleaned = re.sub(r",([A-Za-z])", r", \1", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" ,")


def hook_violated(narration: str) -> bool:
    """True if scene 1's opening line breaks a hook rule (pure, tested).

    Catches question hooks ("Why ...?"), throat-clearing openers ("Here's
    why..."), and first sentences too bloated to grab a scroller (over 10
    words) — the difference between a 42%-stayed video and a 28% one.
    """
    text = (narration or "").strip()
    first = re.split(r"[.?!…]+", text, maxsplit=1)[0].strip()
    if not first:
        return False
    lowered = first.lower()
    if "did you know" in lowered:
        return True
    if any(lowered.startswith(banned) for banned in _BANNED_HOOK_OPENERS):
        return True
    if len(first.split()) > 10:
        return True
    asked = len(first) < len(text) and text[len(first)] == "?"
    return bool(asked and lowered.startswith(_QUESTION_OPENERS))


def _fix_hook(script, cfg, provider) -> None:
    """One targeted rewrite when scene 1's opening line breaks a hook rule.

    The writer + punch-up prompts demand a grabber, but models disobey often
    enough that the rule needs teeth: this narrow single-line task (boldest
    grabber, 10 words, same fact) complies where broad rewrites don't.
    Validation-gated like every stage — failures keep the original.
    """
    from scriptgen import extract_json

    if not script.scenes or not hook_violated(script.scenes[0].narration):
        return
    text = script.scenes[0].narration.strip()
    first = re.split(r"[.?!…]+", text, maxsplit=1)[0].strip()
    rest = text[len(first):].lstrip(".?!… ").strip()
    prompt = (
        "Rewrite ONLY this Shorts opening line as the strongest possible "
        "attention grabber.\n"
        "Hard rules: NOT a question (no question mark anywhere); 10 words "
        "maximum; the topic's key noun in the first 5 words; same fact; no "
        "throat-clearing ('here's why', 'let me tell you', 'fun fact'); "
        "speakable aloud; no hashtags.\n"
        f"OPENING: {first}\n"
        'Return ONLY JSON: {"line": "..."}'
    )
    print("      editorial : hook fix pass...")
    try:
        raw = provider.generate_text(prompt, temperature=0.5, tag="hookfix",
                                     json_mode=True)
        data = extract_json(raw)
    except Exception as exc:  # noqa: BLE001 - polish never kills a render
        print(f"      editorial : hook fix failed ({str(exc)[:100]}) — keeping previous.")
        return
    line = data.get("line") if isinstance(data, dict) else None
    line = line.strip() if isinstance(line, str) else ""
    if not line or "?" in line or len(line.split()) > 14:
        print("      editorial : hook fix returned no usable line — keeping previous.")
        return
    if not line.endswith((".", "!", "…")):
        line += "."
    script.scenes[0].narration = line + (" " + rest if rest else "")
    print(f"      editorial : hook fix applied ({first[:50]!r} -> {line[:50]!r}).")


def _punch_up(script, cfg: Config, provider) -> None:
    """Retention edit: stronger hook, cliffhangers, payoff. Facts frozen."""
    prompt = (
        f"You are a short-form retention editor. Rewrite these {len(script.scenes)} "
        f"voiceover scenes for maximum watch-through.\n"
        f"CHANNEL: {cfg.topic}. Tone: {cfg.tone}. Language: {cfg.language}.\n"
        "RULES (hard):\n"
        "- Keep the SAME facts, SAME scene count, SAME order. Never add new claims.\n"
        "- Scene 1: grabber in under 3 seconds — a claim, paradox, or "
        "wrong-belief flip in 10 words or fewer.\n"
        "  Any question-hook ('did you know?', 'what if?') MUST be rewritten as\n"
        "  a bold claim. The hook noun lands in the first 5 words — delete ALL\n"
        "  throat-clearing before it.\n"
        "- End scenes 1..N-1 mid-tension (unfinished idea, 'but then', tease of\n"
        "  what comes next). Last scene: full payoff + one loop-back line echoing\n"
        "  the hook, so replays feel rewarding.\n"
        "- Concrete camera rule: swap abstract nouns for visible ones (animals,\n"
        "  objects, places) wherever the meaning survives.\n"
        "- Speakable aloud, present tense where possible, no hashtags/emoji.\n"
        "- Keep varied sentence rhythm — mix punchy and flowing sentences,\n"
        "  never 4+ choppy in a row. The voiceover must FLOW like speech.\n"
        "- No '...' or '\u2026' anywhere — the voice reads them as dead-air pauses.\n"
        "  Write tension with words and commas, not dots.\n"
        "- Each scene within 20% of its current word count.\n"
        f"SCENES: {_scenes_payload(script)}\n"
        'Return ONLY JSON: {"scenes": [{"narration": "..."}]}'
    )
    print("      editorial : punch-up pass...")
    raw = provider.generate_text(prompt, temperature=0.7, tag="punchup", json_mode=True)
    _apply(script, raw, "punch-up", 1.3)


def _decringe(script, cfg: Config, provider) -> None:
    """Taste veto: smallest possible fixes to cringe lines. Nothing else."""
    prompt = (
        "You are a taste editor with veto power ONLY over cringe. "
        "Read these voiceover scenes.\n"
        "FIX (smallest edit that removes it): begging for likes, 'smash that "
        "button' energy, fake hype ('insane!!', 'mind-blowing!!'), talking down "
        "to the viewer, trying too hard to be funny, dated slang.\n"
        "FORBIDDEN: restructuring, adding jokes, changing facts, rewording clean "
        "lines, touching the hook's claim. If nothing is cringe, return every "
        "narration UNCHANGED.\n"
        f"SCENES: {_scenes_payload(script)}\n"
        'Return ONLY JSON: {"scenes": [{"narration": "..."}]}'
    )
    print("      editorial : decringe pass...")
    raw = provider.generate_text(prompt, temperature=0.2, tag="decringe", json_mode=True)
    _apply(script, raw, "decringe", 1.1)
