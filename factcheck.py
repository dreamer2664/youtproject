"""Optional second-pass model review for generated scripts.

This is a plausibility/consistency review, NOT source-backed fact-checking.
It never claims independent verification; every script still needs human source
review before a manual YouTube upload. A model-marked [CUT] removes unsafe
material, and a script with no remaining scenes blocks the render.
"""

from __future__ import annotations

import re

from config import Config
from safe_errors import configured_secrets, redact_error
from scriptgen import extract_json


def _base_report(**updates) -> dict:
    report = {
        "checked": False,
        "review_type": "model_sanity_review",
        "source_verified": False,
        "review_required": True,
        "blocking": False,
        "changed": 0,
        "removed_scenes": 0,
        "notes": [],
    }
    report.update(updates)
    return report


def _reviewer_key_available(cfg: Config) -> bool:
    """True only when a configured credential-backed reviewer can run."""
    return bool(
        cfg.gemini_api_keys or cfg.groq_llm_api_keys or cfg.openrouter_api_keys
        or cfg.deepseek_api_keys or (cfg.azure_api_key and cfg.azure_endpoint)
    )


def check_script(script, cfg: Config) -> dict:
    """Review scene narration in place; return a JSON-safe, honest report."""
    if not cfg.factcheck_enabled:
        return _base_report(reason="disabled")
    if not _reviewer_key_available(cfg):
        print("      script review : skipped (no configured reviewer credentials)")
        return _base_report(reason="no reviewer credentials")

    print("      script review : checking for obvious errors/uncertainty...")
    try:
        return _check(script, cfg)
    except Exception as exc:  # noqa: BLE001 - review failure never loses the script
        clean = redact_error(exc, configured_secrets(cfg))
        print(f"      script review : unavailable ({clean[:100]}) — human review still required")
        return _base_report(reason=clean[:200])


def _check(script, cfg: Config) -> dict:
    from scriptgen import get_provider

    scenes = list(script.scenes or [])
    if not scenes:
        return _base_report(blocking=True, reason="script contains no scenes")
    numbered = "\n".join(
        f"[{i}] {scene.narration}" for i, scene in enumerate(scenes, 1))
    prompt = f"""You are an AI consistency and plausibility reviewer, NOT an independent fact-checker.
You have no web access and no source documents. Never claim that a statement is verified.
LANGUAGE: {cfg.language}
Review the {len(scenes)} scenes of narration for obvious contradictions, implausible
claims, unsupported specificity, and uncertain names, dates, numbers, records, or causes.
For each scene:
- Keep sound, ordinary narration unchanged.
- Make only a minimal correction when you are genuinely confident from general knowledge.
- If a factual sentence is uncertain or cannot be responsibly corrected, replace that
  sentence with the exact marker [CUT]. Do not replace the whole scene unless nothing
  else in it can safely remain.
- Never add a new factual claim, source, quotation, or certainty. Do not treat your
  internal knowledge as evidence. Preserve the remaining wording and speakability.

NARRATION:
{numbered}

Return ONLY a JSON object in exactly this shape:
{{"scenes": [{{"narration": "...", "changed": false, "note": "brief uncertainty/correction note"}}]}}"""
    provider = get_provider(cfg)
    raw = provider.generate_text(
        prompt, temperature=0.2, tag="script-review", json_mode=True)
    data = extract_json(raw)
    fixed = data.get("scenes") if isinstance(data, dict) else None
    if not isinstance(fixed, list) or len(fixed) != len(scenes):
        got = len(fixed) if isinstance(fixed, list) else "?"
        raise ValueError(f"expected {len(scenes)} scenes back, got {got}")

    kept = []
    changed = 0
    removed = 0
    notes: list[str] = []
    for original, item in zip(scenes, fixed):
        if not isinstance(item, dict):
            notes.append("malformed review item ignored")
            kept.append(original)
            continue
        proposed = str(item.get("narration") or "").strip()
        if not proposed:
            notes.append("empty correction ignored")
            kept.append(original)
            continue

        # [CUT] is a removal instruction, never words for the TTS voice.
        cleaned = re.sub(r"\[\s*CUT\s*\]", " ", proposed, flags=re.I)
        cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
        cleaned = " ".join(cleaned.split()).strip(" ,;:")
        note = str(item.get("note") or "").strip()
        if note:
            notes.append(note[:160])
        if not cleaned:
            removed += 1
            continue

        if cleaned != str(original.narration or "").strip():
            original.narration = cleaned
            changed += 1
        kept.append(original)

    if not kept:
        print("      script review : blocked — every scene was marked [CUT]")
        return _base_report(
            checked=True, blocking=True, changed=changed,
            removed_scenes=removed, reviewed_scenes=len(scenes),
            notes=notes, reason="model review removed every scene; human correction required",
        )

    script.scenes = kept
    if changed or removed:
        print(f"      script review : {changed} scene(s) revised, "
              f"{removed} removed; source review still required")
    else:
        print(f"      script review : {len(kept)} scene(s) reviewed; "
              "no source verification performed")
    return _base_report(
        checked=True, blocking=False, changed=changed,
        removed_scenes=removed, reviewed_scenes=len(scenes),
        remaining_scenes=len(kept), notes=notes,
    )
