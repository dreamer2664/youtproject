"""Editorial passes: punch-up (retention) + decringe (taste veto).

These run after factcheck, before voiceover. Both are narrow LLM rewrites
over the finished scenes; both are validation-gated and never fatal — any
failure keeps the previous version with a printed note, never a crash.
"""

from __future__ import annotations

import json

from config import Config


def polish_script(script, cfg: Config, provider) -> None:
    """Punch-up then decringe, in place. Never raises, never breaks a render."""
    if not cfg.editorial_enabled:
        return
    for stage, func in (("punch-up", _punch_up), ("decringe", _decringe)):
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


def _punch_up(script, cfg: Config, provider) -> None:
    """Retention edit: stronger hook, cliffhangers, payoff. Facts frozen."""
    prompt = (
        f"You are a short-form retention editor. Rewrite these {len(script.scenes)} "
        f"voiceover scenes for maximum watch-through.\n"
        f"CHANNEL: {cfg.topic}. Tone: {cfg.tone}. Language: {cfg.language}.\n"
        "RULES (hard):\n"
        "- Keep the SAME facts, SAME scene count, SAME order. Never add new claims.\n"
        "- Scene 1: hook in 3 seconds with a claim, paradox, or wrong-belief flip.\n"
        "  Any question-hook ('did you know?', 'what if?') MUST be rewritten as\n"
        "  a bold claim. The hook noun lands in the first 5 words — delete ALL\n"
        "  throat-clearing before it.\n"
        "- End scenes 1..N-1 mid-tension (unfinished idea, 'but...', tease of\n"
        "  what comes next). Last scene: full payoff + one loop-back line echoing\n"
        "  the hook, so replays feel rewarding.\n"
        "- Concrete camera rule: swap abstract nouns for visible ones (animals,\n"
        "  objects, places) wherever the meaning survives.\n"
        "- Speakable aloud, present tense where possible, no hashtags/emoji.\n"
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
