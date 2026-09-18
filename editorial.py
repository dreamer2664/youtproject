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
