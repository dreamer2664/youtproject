"""Second-pass fact-check on generated scripts (Gemini, optional).

Catches invented dates, wrong names and impossible numbers before they reach
the voiceover. Never kills a render: any failure (no key, 429s, bad JSON)
keeps the original script and just reports what happened.
"""

from __future__ import annotations

from config import Config
from scriptgen import extract_json


def check_script(script, cfg: Config) -> dict:
    """Verify/fix scene narrations in place. Returns a JSON-safe report."""
    if not cfg.factcheck_enabled:
        return {"checked": False, "reason": "disabled"}
    if not cfg.gemini_api_key:
        print("      factcheck : skipped (no Gemini key)")
        return {"checked": False, "reason": "no key"}
    try:
        return _check(script, cfg)
    except Exception as exc:  # noqa: BLE001 - fact-check must never kill a render
        print(f"      factcheck : failed ({str(exc)[:100]}) — keeping original script")
        return {"checked": False, "reason": str(exc)[:200]}


def _check(script, cfg: Config) -> dict:
    from scriptgen import GeminiProvider

    numbered = "\n".join(f"[{i}] {s.narration}" for i, s in enumerate(script.scenes, 1))
    prompt = f"""You are a meticulous fact-checker for a short-form video channel.
LANGUAGE: {cfg.language}
Below are {len(script.scenes)} scenes of voiceover narration. Verify every factual
claim (names, dates, numbers, causes, records). For each scene:
- If every claim checks out (or is clearly opinion/CTA), return the narration UNCHANGED.
- If a claim is wrong, fix it with the SMALLEST possible edit that stays speakable.
- If a claim is false and unfixable in one sentence, replace just that sentence with [CUT].
- Never add new claims, never reword for style, keep each scene speakable aloud.

NARRATION:
{numbered}

Return ONLY a JSON object in exactly this shape:
{{"scenes": [{{"narration": "...", "changed": false, "note": "..."}}]}}"""
    provider = GeminiProvider(cfg.gemini_api_key, cfg.gemini_model)
    raw = provider.generate_text(prompt, temperature=0.2, tag="factcheck", json_mode=True)
    data = extract_json(raw)
    fixed = data.get("scenes")
    if not isinstance(fixed, list) or len(fixed) != len(script.scenes):
        got = len(fixed) if isinstance(fixed, list) else "?"
        raise ValueError(f"expected {len(script.scenes)} scenes back, got {got}")
    changed, notes = 0, []
    for scene, item in zip(script.scenes, fixed):
        text = str((item or {}).get("narration") or "").strip()
        if not text:
            notes.append("empty correction ignored")
            continue
        text = " ".join(text.replace("[CUT]", " ").split())
        if not text or text == scene.narration.strip():
            continue
        scene.narration = text
        changed += 1
        note = str((item or {}).get("note") or "").strip()
        if note:
            notes.append(note[:160])
    if changed:
        print(f"      factcheck : {changed}/{len(script.scenes)} scenes corrected")
    else:
        print(f"      factcheck : clean ({len(script.scenes)} scenes verified)")
    return {"checked": True, "changed": changed, "notes": notes}
