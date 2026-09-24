"""Script generation: turns a channel topic into a narrated scene list.

Two providers:
  * GeminiProvider  - Google AI Studio, free tier, needs a free API key.
  * TemplateProvider - no key, no network, always works, lower quality.

Both return the same Script object, so the rest of the pipeline does not care.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Protocol

import requests

import keystats

from config import Config
from images import style_spec


@dataclass
class Scene:
    narration: str
    image_prompt: str


@dataclass
class Script:
    title: str
    description: str
    tags: list[str]
    scenes: list[Scene]
    provider: str = "unknown"
    title_alt: str = ""  # A/B lab: runner-up title for the 2nd channel

    def estimated_seconds(self, words_per_minute: float = 130.0) -> float:
        words = sum(len(s.narration.split()) for s in self.scenes)
        return round(words / words_per_minute * 60.0, 1)


# A terminal period is not proof of completeness: the writer sometimes
# ends a scene on a conjunction ("...leans because soft soil.") — the
# sentence hangs semantically and the voice stops mid-thought.
_HANGING_ENDERS = ("because", "but", "and", "so", "which", "that", "when",
                   "while", "since", "until", "unless")


def _ends_sentence(narration: str) -> bool:
    """True when the narration ends a complete sentence."""
    text = (narration or "").strip()
    while text and text[-1] in "\"')]}\u00bb\u201d\u2019":
        text = text[:-1].rstrip()
    if not text or text[-1] not in ".!?\u2026":
        return False
    last_word = text[:-1].strip().split()[-1].lower().strip(",.;!?\u2026") \
        if text[:-1].strip() else ""
    return last_word not in _HANGING_ENDERS


def merge_incomplete_scenes(script: Script, *, floor: int = 2,
                            cap: int = 3) -> int:
    """Join scenes that ACCIDENTALLY split one sentence (pure, tested).

    Two live postmortems (2026-09-20): (1) a scene ending mid-sentence
    sounds like the voice stopping mid-thought — merge it; (2) but the
    retention prompts DELIBERATELY end scenes mid-tension (teases,
    colons), and v2's punctuation-only check read those as splits and
    collapsed a 6-scene script into ONE scene — one image held for 27
    seconds, the "2 images in a 1:19 video" regression. v3 needs real
    evidence of an accident: the scene hangs without terminal punctuation,
    does NOT end in a colon (that is a tease by construction), and the
    next scene starts LOWERCASE (a true sentence continuation). A floor
    (never merge below N scenes) and a cap keep the structure intact no
    matter what the model writes.
    """
    merged = 0
    i = 0
    while i < len(script.scenes) and merged < cap:
        scene = script.scenes[i]
        text = (scene.narration or "").strip()
        next_text = ((script.scenes[i + 1].narration or "").lstrip()
                     if i + 1 < len(script.scenes) else "")
        if (len(script.scenes) > floor and text
                and not _ends_sentence(text)
                and not text.endswith(":")
                and next_text[:1].islower()):
            scene.narration = (f"{scene.narration.rstrip()} "
                               f"{next_text}").strip()
            del script.scenes[i + 1]
            merged += 1
            continue  # the joined text may still hang — re-check
        i += 1
    return merged


class ScriptProvider(Protocol):
    name: str

    def generate(self, cfg: Config, topic_override: str | None = None) -> Script: ...


# --------------------------------------------------------------------------
# JSON extraction — LLMs wrap JSON in fences or add prose. Be forgiving.
# --------------------------------------------------------------------------
def extract_json(text: str) -> dict:
    text = text.strip()

    # strip markdown fences
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # fall back to the outermost balanced braces
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON object found in model output: {text[:200]!r}")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : index + 1])
    raise ValueError("Unbalanced JSON in model output")


def normalise_script(raw: dict, provider: str) -> Script:
    scenes = []
    for item in raw.get("scenes") or []:
        narration = str(item.get("narration") or item.get("text") or "").strip()
        prompt = str(item.get("image_prompt") or item.get("image") or "").strip()
        if not narration:
            continue
        if not prompt:
            prompt = narration[:180]
        scenes.append(Scene(narration=narration, image_prompt=prompt))

    if not scenes:
        raise ValueError("Model returned no usable scenes")

    title = str(raw.get("title") or "Untitled").strip()[:100]
    description = str(raw.get("description") or "").strip()
    tags = [str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()]
    return Script(title=title, description=description, tags=tags, scenes=scenes, provider=provider)


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------
def end_rule(cta_enabled: bool) -> str:
    """Closing-line rule for the script prompt (pure, tested).

    Mirrors main.py: when the rotating CTA is on, the render appends that
    line to the narration — so the script must end on the payoff with NO
    follow/subscribe call of its own, or the ending repeats itself
    ("...follow for more facts. Follow for more."). CTA off keeps the old
    self-contained rule, exactly like TemplateProvider.
    """
    if cta_enabled:
        return ("- END with a punchy payoff line. Do NOT add any follow, subscribe, "
                "like, or call-to-action line — the outro CTA is added automatically.")
    return ("- END with a punchy payoff line plus a call to action of 5 words or less\n"
            "  (e.g. \"Follow for part two.\").")


def script_words(script: Script) -> int:
    """Total narration words across all scenes (pure, tested)."""
    return sum(len(scene.narration.split()) for scene in script.scenes)


# Below this fraction of the word budget, Gemini gets one expansion pass
# instead of shipping a short video. 0.85 with a single retry: a model that
# writes 84% gets fixed; a model that writes 20% gets one honest rescue
# attempt, then the run keeps the original and main.py warns as before.
MIN_WORDS_FRACTION = 0.85


def needs_expansion(words: int, budget: int,
                    fraction: float = MIN_WORDS_FRACTION) -> bool:
    """True when a script is too short to hit its target (pure, tested)."""
    if budget <= 0:
        return False
    return words < int(budget * fraction)


def build_expansion_prompt(previous_json: str, words: int, budget: int,
                           per_scene: int, cta_enabled: bool) -> str:
    """Second-chance prompt: same story, but long enough (pure, tested)."""
    return f"""The script below is {words} words but the video needs at least {budget} words
of narration (about {per_scene} words per scene). Rewrite it LONGER: keep the
same story, facts, title, and scene count, but expand every thin scene with
concrete detail. Keep sentences under 12 words and the same high-energy tone.
{end_rule(cta_enabled)}

Previous script JSON:
{previous_json}

Return ONLY the full rewritten JSON object in the same shape."""


# Above this fraction of the word budget, Gemini gets one tightening pass
# instead of shipping a rambling video (observed: 404 words against 169).
MAX_WORDS_FRACTION = 1.30


def needs_shortening(words: int, budget: int,
                     fraction: float = MAX_WORDS_FRACTION) -> bool:
    """True when a script overshoots its target badly (pure, tested)."""
    if budget <= 0:
        return False
    return words > int(budget * fraction)


def build_shorten_prompt(previous_json: str, words: int, budget: int,
                         per_scene: int, cta_enabled: bool) -> str:
    """Second-chance prompt: same story, but tight (pure, tested)."""
    return f"""The script below is {words} words but the video needs {budget} words at most
(about {per_scene} words per scene). Tighten it: keep the same story, facts,
title, and scene count, but cut filler, merge rambling sentences, and drop the
weakest detail in every scene. Keep sentences under 12 words and the same
high-energy tone.
{end_rule(cta_enabled)}

Previous script JSON:
{previous_json}

Return ONLY the full rewritten JSON object in the same shape."""


def build_script_prompt(cfg, topic: str, target: int, scene_count: int,
                        word_budget: int, ceiling: int, per_scene: int,
                        per_scene_max: int) -> str:
    """Full script-writer prompt with the retention architecture (pure, tested).

    v2 (2026-09-16): hook menu with questions demoted, first-five-words rule,
    per-scene micro-teases, escalation + loop-back ending, concrete-camera
    rule, and stock-searchable image briefs (the director brain feeds
    Pexels now, not just AI renderers).
    v2.1 (2026-09-18): grabber tightened — 9-word hook cap, banned
    throat-clearing openers, promise sentence right after the hook.
    v2.2 (2026-09-19): title formula from live analytics — 4-7 words,
    concrete subject in the first 3 words (short titles outperformed
    long vague ones by ~100x in views).
    """
    art_brief = style_spec(cfg.style)["brief"]
    return f"""You are a script writer for a high-retention vertical video channel (TikTok, YouTube Shorts, Instagram Reels).

CHANNEL TOPIC: {topic}
TONE: {cfg.tone}
AUDIENCE: {cfg.audience}
LANGUAGE: {cfg.language}
TARGET VIDEO LENGTH: about {target} seconds

Pick ONE specific, genuinely interesting story or fact within the topic.
Write {scene_count} scenes of narration that together take about {target} seconds
when read aloud fast (roughly {word_budget} words total).

LENGTH IS A HARD REQUIREMENT: the narration must total {word_budget}-{ceiling} words
(about {per_scene} words per scene, never more than {per_scene_max}). Scripts outside
this range are rejected — count the words in each scene before returning: expand
thin scenes with concrete detail, cut filler if running long. Keep scenes BALANCED: the longest scene
at most ~2x the shortest (a 50-second scene under three static images
reads as a slideshow).

RETENTION ARCHITECTURE — follow every rule:
- HOOK (first sentence, 9 WORDS MAX — a grabber, not a summary). Pick the
  strongest shape that fits, in this order of power. NEVER a bare question,
  NEVER "did you know":
  1. WRONG-BELIEF FLIP ("Everything you know about X is wrong.")
  2. PARADOX ("The animal that survives being boiled alive.")
  3. STAKES ("This kills more people than sharks every year.")
  4. COLD PAYOFF ("Sea otters hold hands to stay alive.")
  5. VIVID SCENE ("Picture a lake that turns birds to stone.")
- The hook's key noun must appear in the FIRST 5 WORDS. No greeting, no
  "in this video", no throat-clearing, no setup of any kind. Banned openers:
  "here's why", "let me tell you", "fun fact", "believe it or not". Prefer
  "you/your" when the topic allows — direct address grips scrollers.
- THE PROMISE (sentence 2, right after the hook): one short line that makes
  staying feel worth it — name the weirdness or the stakes in concrete
  words, never vaguely ("wait for it" is banned).
- SHORT sentences: 12 words max each, one idea per sentence. Staccato rhythm.
- RHYTHM: vary sentence length — mix punchy 4-7 word sentences with flowing
  9-12 word ones joined by commas. Never stack 4+ choppy sentences in a
  row. The voiceover must FLOW like speech, not machine-gun.
- NEVER write '...' or '\u2026' — the voice reads them as dead-air pauses.
  Write tension with words and commas, not dots.
- NO filler: cut every word that does not earn the next second of attention.
- MICRO-TEASES: every scene except the last ends mid-tension — an unfinished
  idea, a "but then", a tease of what comes next. Never resolve early.
- One PATTERN INTERRUPT around the middle: a twist ("but here's what nobody
  tells you"), a rhetorical question, or a contrarian turn.
- One OPEN LOOP before the payoff ("and the last one changes everything").
- ESCALATE, never repeat: each scene deepens the mystery or raises the
  stakes. "That's not even the wildest part" energy — once, in own words.
- LOOP-BACK ENDING: the final sentence echoes the hook WITH the payoff, so
  replays feel rewarding ("and that's why X will never look the same").
- CONCRETE CAMERA RULE: name physical things (animals, objects, places,
  food) the camera can show. Every abstract idea must be anchored to
  something visible within the same scene.
{end_rule(cfg.cta_enabled)}
- Every scene's narration is 1-3 COMPLETE sentences, ending with '.', '!' or '?' — NEVER split a sentence across two scenes (each scene is voiced as a separate clip; a split sentence audibly breaks mid-word).
- narration: plain spoken prose for a voiceover. No stage directions, no quotes
  inside the text, no markdown, no emoji.
- image_prompt: 2-4 CONCRETE visible subjects (exact animals/objects/places +
  setting + one action) that a stock-photo search could find, then style:
  {art_brief}
- title: a scroll-stopper, 4-7 words, under 45 characters. Formula: Why/How +
  the CONCRETE subject (animal, object, place) in the first 3 words + a
  surprising twist. Model on "Why Cats Break The Laws Of Physics" and
  "How Honey Never Expires". No vague tails ("they don't want you to know"),
  no clickbait lies, no hashtags.
- tags: 8 to 12 short search tags. Do not leave this empty.
- Be factually careful. If a detail is uncertain, leave it out rather than invent it.

Return ONLY a JSON object in exactly this shape:
{{
  "title": "scroll-stopper, 4-7 words, under 45 characters",
  "description": "2-4 sentence video description",
  "tags": ["up to 12 relevant tags"],
  "scenes": [
    {{"narration": "...", "image_prompt": "..."}}
  ]
}}"""


class GeminiProvider:
    name = "gemini"

    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    # 429 = rate limited, 500/502/503/504 = transient. The free tier returns 503
    # "high demand" often and unpredictably, so retries are mandatory, not optional.
    RETRYABLE = {429, 500, 502, 503, 504}
    MAX_RETRIES = 6
    MAX_DELAY = 45.0
    # Tried, in order, if the configured model keeps failing (5xx saturation
    # or 404 renames — Google retires model IDs regularly). ORDER IS
    # EMPIRICAL, re-surveyed live 2026-09-16: the 2.5 IDs 404 despite being
    # listed; 3.8-flash + flash-latest 503 under load; pro/omni IDs 429 on
    # tiny free quotas; 3-flash-preview answers in ~1s, 3.5-flash in ~16s,
    # lite always. Newest-first for the smartest script that answers; proven
    # workers catch every miss (failover is seconds, not minutes).
    FALLBACK_MODELS = [
        "gemini-3.8-flash", "gemini-3.5-flash", "gemini-3-flash-preview",
        "gemini-flash-latest", "gemini-flash-lite-latest",
        "gemini-3.1-flash-lite",
    ]
    # Split for the provider sandwich (see get_provider): the best three
    # run before the scarce Groq/OpenRouter brains, the rest only after.
    PRIMARY_MODELS = FALLBACK_MODELS[:3]
    RESERVE_MODELS = FALLBACK_MODELS[3:]

    def __init__(self, api_key: str | list[str],
                 model: str = "gemini-3.8-flash",
                 models: list[str] | None = None) -> None:
        if isinstance(api_key, str):
            api_key = [api_key]
        keys = [key.strip() for key in api_key if key and key.strip()]
        if not keys:
            raise RuntimeError(
                "No Gemini API key. Get a free one at https://aistudio.google.com/apikey, "
                "then put it in config.yaml or export GEMINI_API_KEY. "
                "Or set ai.provider: template in config.yaml to run keyless."
            )
        self.api_keys = keys
        self.api_key = keys[0]  # first key; kept for backward compat
        self.model = model
        self.models = list(models) if models else None  # sandwich slice

    def _try_model(self, model: str, payload: dict, tag: str = "script") -> tuple[dict | None, int, str]:
        """One model, with retries across keys. Returns (result, status, error)."""
        keys = list(self.api_keys)
        last_status = 0
        last_error = ""
        # 429 budget: every key gets one immediate shot (spent per-key quota
        # is the common case), minimum 3 attempts like the single-key days.
        failover_after = max(3, len(keys))
        tried_429 = 0
        net_errors = 0
        for attempt in range(1, self.MAX_RETRIES + 1):
            key = keys[0]
            try:
                response = requests.post(
                    self.URL.format(model=model),
                    params={"key": key},
                    json=payload,
                    timeout=60,
                )
            except requests.exceptions.ConnectionError as exc:
                # DNS/refused/reset: the host itself is down — no other key
                # or model on this host will answer. Abort Gemini at once
                # so the provider chain (groq/...) picks up immediately.
                return None, -1, f"Gemini unreachable: {exc}"
            except requests.exceptions.RequestException as exc:
                # Timeout (server stalled) or other transient: one spare key
                # in case of a blip, then fail over to the next model. Never
                # sleeps — a stalled host never recovers on a timescale
                # worth sitting silent for.
                net_errors += 1
                last_status, last_error = 0, f"network error: {exc}"
                print(f"  [{tag}] {model}: {last_error} — "
                      f"trying {'next key' if net_errors < 2 else 'next model'}")
                keys.append(keys.pop(0))
                if net_errors >= 2:
                    break
                continue
            if response.status_code == 200:
                try:
                    tok = (len(str(payload)) + len(response.text)) // 4
                except TypeError:  # mocked transport in tests
                    tok = 0
                keystats.bump("gemini", key, req=1, tok=tok, tag=tag)
                return response.json(), 200, ""

            last_status = response.status_code
            last_error = response.text[:300]

            # Dead model ID: retrying is pointless — fail over at once.
            if response.status_code == 404:
                return None, last_status, last_error

            # Rejected key: drop it and try the next (keys come from
            # different accounts, so one's revocation is no verdict on the
            # others). All rejected -> fatal; _post raises with a hint.
            if response.status_code in (400, 401, 403):
                keys.pop(0)
                if keys:
                    print(f"  [{tag}] Gemini key ...{key[-4:]} rejected — "
                          f"trying next key")
                    continue
                return None, last_status, last_error

            # 429: rotate immediately (a different key = a different quota
            # bucket; sleeping helps nothing). Every key 429'd -> the model
            # itself is saturated, fail over to the next model.
            if response.status_code == 429:
                tried_429 += 1
                keys.append(keys.pop(0))
                if tried_429 >= failover_after:
                    break
                continue

            if response.status_code in self.RETRYABLE and attempt < self.MAX_RETRIES:
                # A different key sometimes routes around a saturated shard,
                # and trying it costs nothing — so every key gets one INSTANT
                # shot before anyone sleeps. (The old code slept ~107s on the
                # first key and never tried the rest, then dropped a model.)
                keys.append(keys.pop(0))
                if attempt >= len(keys):
                    # Second sweep: all keys failed once, back off now.
                    # Single-key setups behave exactly as before.
                    wave = attempt - len(keys) + 1
                    delay = min(2 ** wave + random.uniform(0, 1), self.MAX_DELAY)
                    print(f"  [{tag}] {model}: HTTP {response.status_code}, retry in "
                          f"{delay:.0f}s ({attempt}/{self.MAX_RETRIES})")
                    time.sleep(delay)

        return None, last_status, last_error

    def _post(self, payload: dict, tag: str = "script") -> dict:
        if self.models:
            chain = list(self.models)
        else:
            chain = [self.model] + [m for m in self.FALLBACK_MODELS
                                    if m != self.model]
        last_status, last_error = 0, ""

        for index, model in enumerate(chain):
            result, status, error = self._try_model(model, payload, tag)
            if result is not None:
                if index > 0:
                    print(f"  [{tag}] using fallback model {model}")
                return result

            last_status, last_error = status, error

            if status == -1:
                raise RuntimeError(f"{error} — failing over to the next provider.")
            if status in (400, 401, 403):
                hint = ""
                if "API key not valid" in error:
                    hint = " -> the key was rejected. Check it at https://aistudio.google.com/apikey"
                raise RuntimeError(
                    f"Gemini rejected the request (HTTP {status}){hint}: {last_error}"
                )
            if status == 404:
                print(f"  [{tag}] {model} is not available (404); trying the next model.")
                continue
            if index + 1 < len(chain):
                detail = last_error if status == 0 else f"HTTP {status}"
                print(f"  [{tag}] {model} gave up ({detail}); trying a fallback model.")

        if last_status == 404:
            raise RuntimeError(
                "No Gemini model in the fallback chain exists (Google may have "
                "renamed them again). Set ai.gemini_model to a current free-tier "
                f"ID such as 'gemini-flash-lite-latest'. Last error: {last_error}"
            )
        detail = last_error if last_status == 0 else f"HTTP {last_status}"
        raise RuntimeError(
            f"Gemini unavailable after trying {len(chain)} model(s) with retries "
            f"(last: {detail}). This is free-tier saturation — wait a few "
            f"minutes and re-run; nothing was lost. {last_error}"
        )

    def generate_text(self, prompt: str, temperature: float = 0.7,
                      tag: str = "gemini", json_mode: bool = False) -> str:
        """Raw text completion using the retry + fallback chain. Raises RuntimeError."""
        config: dict = {"temperature": temperature}
        if json_mode:
            config["responseMimeType"] = "application/json"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": config,
        }
        data = self._post(payload, tag=tag)
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected Gemini response shape: {str(data)[:400]}") from exc

    @staticmethod
    def _save_debug(cfg: Config, raw: str) -> None:
        """Keep the raw model output so a parse failure is diagnosable."""
        try:
            cfg.work_dir.mkdir(parents=True, exist_ok=True)
            (cfg.work_dir / "gemini_last.txt").write_text(raw, encoding="utf-8")
        except OSError:
            pass

    def generate(self, cfg: Config, topic_override: str | None = None) -> Script:
        topic = topic_override or cfg.topic
        target = cfg.target_seconds
        # Narration runs at ~130 wpm times the speech-rate factor (measured on
        # edge-tts neural voices); portrait
        # cuts fast (~10s scenes) for TikTok/Shorts pacing, landscape
        # breathes (~20s scenes).
        wpm = 130.0 * cfg.speech_rate_factor
        scene_len = 10 if cfg.format == "portrait" else 20
        scene_count = max(3, min(12, round(target / scene_len)))
        word_budget = int(target * wpm / 60)
        per_scene = max(15, word_budget // scene_count)
        ceiling = int(word_budget * 1.25)
        per_scene_max = max(20, ceiling // scene_count)
        prompt = build_script_prompt(
            cfg, topic, target, scene_count, word_budget, ceiling,
            per_scene, per_scene_max)

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.9,
                "responseMimeType": "application/json",
            },
        }

        data = self._post(payload)
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            self._save_debug(cfg, json.dumps(data)[:4000])
            raise RuntimeError(f"Unexpected Gemini response shape: {str(data)[:400]}") from exc

        def _parse(raw_text: str) -> Script:
            return normalise_script(extract_json(raw_text), self.name)

        try:
            script = _parse(text)
        except ValueError:
            # Weak models sometimes fumble the shape once, then comply.
            print("  [script] model returned no usable scenes — asking once more...")
            data = self._post(payload)
            try:
                text = data["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError) as exc:
                self._save_debug(cfg, json.dumps(data)[:4000])
                raise RuntimeError(f"Unexpected {self.name} response shape: {str(data)[:400]}") from exc
            try:
                script = _parse(text)
            except ValueError as exc:
                self._save_debug(cfg, text)
                raise RuntimeError(
                    f"{self.name.capitalize()} returned no usable scenes twice. Raw response saved to "
                    f"{cfg.work_dir / (self.name + '_last.txt')}. Snippet: {text[:200]!r}"
                ) from exc
        words = script_words(script)
        if needs_expansion(words, word_budget):
            # Small/loaded models often undershoot the budget (the observed
            # case: 71 words against 169). One expansion pass on the same
            # model that just answered; failures keep the original and the
            # short-video warning in main.py still applies.
            print(f"  [script] too short ({words}/{word_budget} words) — "
                  f"asking the model to expand...")
            try:
                expanded_raw = self.generate_text(
                    build_expansion_prompt(
                        json.dumps({
                            "title": script.title,
                            "description": script.description,
                            "tags": script.tags,
                            "scenes": [
                                {"narration": s.narration,
                                 "image_prompt": s.image_prompt}
                                for s in script.scenes
                            ],
                        }),
                        words, word_budget, per_scene, cfg.cta_enabled),
                    temperature=0.7, tag="script", json_mode=True)
                expanded = normalise_script(extract_json(expanded_raw), self.name)
                if script_words(expanded) > words:
                    script = expanded
                    words = script_words(script)
                    print(f"  [script] expanded to {words} words")
                else:
                    print("  [script] expansion added no words — keeping original")
            except (ValueError, RuntimeError) as exc:
                print(f"  [script] expansion failed ({exc}) — keeping original")
        if needs_shortening(words, word_budget):
            # Mirror of the above: cap rambling scripts (observed: 404 words
            # against 169). The tightened script is kept only if it is both
            # shorter AND still above 70% of budget — a rewrite that
            # overcorrects into a short script is worse than the original.
            print(f"  [script] too long ({words}/{word_budget} words) — "
                  f"asking the model to tighten...")
            try:
                tightened_raw = self.generate_text(
                    build_shorten_prompt(
                        json.dumps({
                            "title": script.title,
                            "description": script.description,
                            "tags": script.tags,
                            "scenes": [
                                {"narration": s.narration,
                                 "image_prompt": s.image_prompt}
                                for s in script.scenes
                            ],
                        }),
                        words, word_budget, per_scene, cfg.cta_enabled),
                    temperature=0.7, tag="script", json_mode=True)
                tightened = normalise_script(extract_json(tightened_raw), self.name)
                tight_words = script_words(tightened)
                if tight_words < words and tight_words >= int(word_budget * 0.7):
                    script = tightened
                    words = tight_words
                    print(f"  [script] tightened to {words} words")
                else:
                    print("  [script] tightening missed — keeping original")
            except (ValueError, RuntimeError) as exc:
                print(f"  [script] tightening failed ({exc}) — keeping original")
        if not script.description:
            script.description = f"{script.title}\n\n{topic}"
        if not script.tags:
            script.tags = derive_tags(script.title + " " + topic)
        return script


def derive_tags(text: str, limit: int = 12) -> list[str]:
    """Fallback tags from the title/topic when the model omits them."""
    stop = {
        "the", "and", "for", "with", "that", "this", "from", "into", "your",
        "about", "which", "their", "there", "what", "when", "were", "have",
        "untold", "story", "stories", "true", "really", "happened", "strange",
        "truth", "nobody", "talks", "forgotten", "chapter",
    }
    words = [w.strip(".,!?\"'").lower() for w in re.findall(r"[A-Za-z']{3,}", text)]
    seen: list[str] = []
    for word in words:
        if word not in stop and word not in seen:
            seen.append(word)
        if len(seen) >= limit:
            break
    return seen


# --------------------------------------------------------------------------
# Template fallback — no key, no network.
# --------------------------------------------------------------------------
class TemplateProvider:
    name = "template"

    FRAMINGS = [
        "The untold story of",
        "What really happened during",
        "The strange truth about",
        "Nobody talks about",
        "A forgotten chapter of",
    ]

    def generate(self, cfg: Config, topic_override: str | None = None) -> Script:
        topic = topic_override or cfg.topic
        seed = random.choice(self.FRAMINGS)
        title = f"{seed} {topic}"[:100]
        scene_len = 10 if cfg.format == "portrait" else 20
        count = max(3, min(8, round(cfg.target_seconds / scene_len)))

        # Fast-paced even offline: cold open, short sentences, a twist, an
        # open loop, and a payoff. Beats run ~25 words each so the offline
        # path lands near the target length instead of a 25-second short.
        core = [
            f"Stop scrolling. Nobody knows this about {topic}. The real story "
            "is stranger than anything heard. Listen close, because this gets "
            "wild fast. Do not blink. Stay with me.",
            f"Here is the part they always skip. {topic} started with one "
            "strange decision. Nobody understood it then. Nobody predicted it. "
            "That single choice changed everything after.",
            "The details sound fake. Every one is documented and checked twice. "
            "Witnesses who were there confirmed it. Truth beats fiction every "
            "time. Believe it. Check the archives.",
            "But here is the twist nobody talks about. Everything flips right "
            "here. What looked like luck was something else entirely. Nobody "
            "saw it coming. Watch closely. Rewind that twice.",
            f"Think that is wild? The last fact about {topic} tops it all. "
            "Almost nobody has heard it. Stay until the very end for the payoff. "
            "You will see why.",
            "Records from the time back it up. Witnesses agreed on every detail. "
            "The papers printed it twice. This really happened, start to finish. "
            "History kept the receipts. Case closed.",
            f"So remember this. {topic} changed everything after. The world "
            "still feels it today. That is the untold story nobody taught you. "
            "Pass it on. Tell a friend.",
        ]
        # When the rotating CTA is on, main.py appends this video's line —
        # the template must not add its own or the ending repeats itself.
        cta = "" if cfg.cta_enabled else "Follow for part two."
        spec = style_spec(cfg.style)
        shots = spec["shots"]
        # No art-direction prefix here: images.stylize() prepends it at
        # render time. Prefixing here too would duplicate it ("flat 2D
        # vector cartoon, flat 2D vector cartoon, ...") and waste the
        # 900-char prompt budget. Photoreal gets its cinematic lead-in
        # here instead, since stylize() adds nothing for photoreal.
        prefix = "" if cfg.style != "photoreal" else "cinematic photorealistic, "
        # CTA on: main.py appends the line to the last scene, so all `count`
        # beats are used. CTA off: the last slot carries the self-contained CTA.
        slots = count if cfg.cta_enabled else max(2, count - 1)
        picked = [b for b in (core * 2)[:slots] + [cta] if b]
        scenes = [
            Scene(
                narration=beat,
                image_prompt=f"{prefix}{shots[i % len(shots)]} depicting {topic}",
            )
            for i, beat in enumerate(picked)
        ]

        return Script(
            title=title,
            description=(
                f"{title}\n\nA fast-paced short about {topic}.\n\n"
                "Generated with the offline template provider — add a free Gemini key "
                "to config.yaml for much better scripts."
            ),
            tags=[w for w in re.findall(r"[A-Za-z]{4,}", topic)][:12],
            scenes=scenes,
            provider=self.name,
        )


class ChainedProvider:
    """Try script providers in order; the first success wins (pure chain).

    Covers both generate() (scripts) and generate_text() (factcheck, topics
    top-ups). Providers lacking generate_text (template) are skipped for
    text calls. Exhaustion raises RuntimeError listing every failure.
    """

    name = "chain"

    def __init__(self, chain: list[tuple[str, object]]) -> None:
        self.chain = [(label, provider) for label, provider in chain
                      if provider is not None]
        if not self.chain:
            raise RuntimeError("no script provider available")

    def _run(self, method: str, *args, **kwargs):
        errors: list[str] = []
        usable = [(label, provider) for label, provider in self.chain
                  if hasattr(provider, method)]
        if not usable:
            raise RuntimeError(f"no script provider supports {method}()")
        for index, (label, provider) in enumerate(usable):
            try:
                result = getattr(provider, method)(*args, **kwargs)
                if index > 0:
                    print(f"  [script] {label} covered after "
                          f"{usable[index - 1][0]} failed")
                return result
            except Exception as exc:  # noqa: BLE001 - fall through, report all
                errors.append(f"{label}: {exc}")
                if index < len(usable) - 1:
                    print(f"  [script] {label} failed — trying "
                          f"{usable[index + 1][0]} ({str(exc)[:110]})")
        raise RuntimeError("all script providers failed: " + " | ".join(errors))

    def generate(self, cfg: Config, topic_override: str | None = None) -> Script:
        return self._run("generate", cfg, topic_override)

    def generate_text(self, prompt: str, temperature: float = 0.7,
                      tag: str = "chain", json_mode: bool = False) -> str:
        return self._run("generate_text", prompt, temperature=temperature,
                         tag=tag, json_mode=json_mode)


def get_provider(cfg: Config) -> ScriptProvider:
    """Primary + fallbacks as a chain; template is always last, never fatal."""
    primary = cfg.ai_provider
    if primary not in ("azure", "gemini", "groq", "openrouter", "deepseek",
                   "pollinations", "template"):
        primary = "gemini"
    order = [primary] + [name for name in ("azure", "gemini", "groq", "openrouter",
                                           "deepseek", "pollinations", "template")
                         if name != primary]
    chain: list[tuple[str, object]] = []
    for name in order:
        if name == "azure" and cfg.azure_api_key and cfg.azure_endpoint:
            from azure_openai import (AzureOpenAIProvider, is_supported_model,
                                      ledger_path_for)

            if not is_supported_model(cfg.azure_model):
                print(f"  [azure] model {cfg.azure_model!r} has no pinned price — "
                      f"lane off (fail-closed).")
            elif not cfg.azure_deployment.strip():
                print("  [azure] deployment name missing — lane off.")
            else:
                chain.append(("azure", AzureOpenAIProvider(
                    endpoint=cfg.azure_endpoint, api_key=cfg.azure_api_key,
                    deployment=cfg.azure_deployment, model=cfg.azure_model,
                    api_version=cfg.azure_api_version,
                    ledger_path=ledger_path_for(cfg),
                    max_usd_per_day=cfg.azure_max_usd_per_day,
                    max_usd_per_month=cfg.azure_max_usd_per_month)))
        elif name == "gemini" and cfg.gemini_api_key:
            chain.append(("gemini", GeminiProvider(
                cfg.gemini_api_keys, cfg.gemini_model,
                models=[cfg.gemini_model] + [
                    m for m in GeminiProvider.PRIMARY_MODELS
                    if m != cfg.gemini_model])))
        elif name == "groq" and cfg.groq_api_keys:
            from groq import GroqProvider

            chain.append(("groq", GroqProvider(cfg.groq_api_keys, cfg.groq_model)))
        elif name == "deepseek" and cfg.deepseek_api_keys:
            from deepseek import DeepSeekProvider
            chain.append(("deepseek", DeepSeekProvider(
                cfg.deepseek_api_keys, cfg.deepseek_model)))
        elif name == "openrouter" and cfg.openrouter_api_keys:
            from openrouter import OpenRouterProvider

            chain.append(("openrouter", OpenRouterProvider(cfg.openrouter_api_keys,
                                                           cfg.openrouter_model)))
        elif name == "pollinations":
            from pollinations_text import PollinationsTextProvider

            chain.append(("pollinations",
                          PollinationsTextProvider(cfg.pollinations_model)))
        elif name == "template":
            chain.append(("template", TemplateProvider()))
    # Sandwich: Gemini's best three run first (huge quota), then the best
    # scarce brains (Groq 120b, OpenRouter 550b), and only then Gemini's
    # weaker reserves — so a slump costs brains, not minutes crawling
    # through Lite models while a 550B brain sits idle. Inserted after the
    # last scarce lane so an explicit --primary always keeps its place.
    if any(label == "gemini" for label, _ in chain):
        reserve = [m for m in GeminiProvider.RESERVE_MODELS
                   if m != cfg.gemini_model]
        if reserve:
            link = ("gemini-reserve", GeminiProvider(
                cfg.gemini_api_keys, cfg.gemini_model, models=reserve))
            pos = max(i for i, (label, _) in enumerate(chain)
                      if label in ("gemini", "groq", "openrouter")) + 1
            chain.insert(pos, link)
    return ChainedProvider(chain)
