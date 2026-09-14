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

    def estimated_seconds(self, words_per_minute: float = 130.0) -> float:
        words = sum(len(s.narration.split()) for s in self.scenes)
        return round(words / words_per_minute * 60.0, 1)


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
    # EMPIRICAL: on 2026-09-14 the rolling alias saturated (503) and the
    # 2.5/3.0 pinned IDs 404'd on v1beta, while 3.1-flash-lite answered —
    # so it leads. The user's configured model is always tried first.
    FALLBACK_MODELS = [
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash",
        "gemini-3-flash",
        "gemini-2.5-flash-lite",
        "gemini-flash-lite-latest",
    ]

    def __init__(self, api_key: str, model: str = "gemini-flash-latest") -> None:
        if not api_key:
            raise RuntimeError(
                "No Gemini API key. Get a free one at https://aistudio.google.com/apikey, "
                "then put it in config.yaml or export GEMINI_API_KEY. "
                "Or set ai.provider: template in config.yaml to run keyless."
            )
        self.api_key = api_key
        self.model = model

    def _try_model(self, model: str, payload: dict, tag: str = "script") -> tuple[dict | None, int, str]:
        """One model, with retries. Returns (result, last_status, last_error)."""
        last_status = 0
        last_error = ""
        for attempt in range(1, self.MAX_RETRIES + 1):
            response = requests.post(
                self.URL.format(model=model),
                params={"key": self.api_key},
                json=payload,
                timeout=180,
            )
            if response.status_code == 200:
                return response.json(), 200, ""

            last_status = response.status_code
            last_error = response.text[:300]

            # A bad key will never work. Stop immediately, on every model.
            if response.status_code in (400, 401, 403):
                return None, last_status, last_error

            if response.status_code in self.RETRYABLE and attempt < self.MAX_RETRIES:
                delay = min(2 ** attempt + random.uniform(0, 1), self.MAX_DELAY)
                print(f"  [{tag}] {model}: HTTP {response.status_code}, retry in "
                      f"{delay:.0f}s ({attempt}/{self.MAX_RETRIES})")
                time.sleep(delay)

        return None, last_status, last_error

    def _post(self, payload: dict, tag: str = "script") -> dict:
        chain = [self.model] + [m for m in self.FALLBACK_MODELS if m != self.model]
        last_status, last_error = 0, ""

        for index, model in enumerate(chain):
            result, status, error = self._try_model(model, payload, tag)
            if result is not None:
                if index > 0:
                    print(f"  [{tag}] using fallback model {model}")
                return result

            last_status, last_error = status, error

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
                print(f"  [{tag}] {model} gave up (HTTP {status}); trying a fallback model.")

        if last_status == 404:
            raise RuntimeError(
                "No Gemini model in the fallback chain exists (Google may have "
                "renamed them again). Set ai.gemini_model to a current free-tier "
                f"ID such as 'gemini-3.1-flash-lite'. Last error: {last_error}"
            )
        raise RuntimeError(
            f"Gemini unavailable after trying {len(chain)} model(s) with retries "
            f"(last HTTP {last_status}). This is free-tier saturation — wait a few "
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
        art_brief = style_spec(cfg.style)["brief"]

        prompt = f"""You are a script writer for a high-retention vertical video channel (TikTok, YouTube Shorts, Instagram Reels).

CHANNEL TOPIC: {topic}
TONE: {cfg.tone}
AUDIENCE: {cfg.audience}
LANGUAGE: {cfg.language}
TARGET VIDEO LENGTH: about {target} seconds

Pick ONE specific, genuinely interesting story or fact within the topic.
Write {scene_count} scenes of narration that together take about {target} seconds
when read aloud fast (roughly {word_budget} words total).

LENGTH IS A HARD REQUIREMENT: the narration must total at least {word_budget} words
(about {per_scene} words per scene). Short scripts are rejected — count the words
in each scene before returning, and expand thin scenes with concrete detail
instead of wrapping up early.

RETENTION RULES — follow all of them:
- COLD OPEN: the first sentence must hook in under 3 seconds. A shocking payoff,
  a bold claim, or a question. Never a greeting, never "in this video", never setup.
- SHORT sentences: 12 words max each, one idea per sentence. Staccato rhythm.
- NO filler: cut every word that does not earn the next second of attention.
- One PATTERN INTERRUPT around the middle: a twist ("but here's what nobody
  tells you..."), a rhetorical question, or a contrarian turn.
- One OPEN LOOP before the payoff ("...and the last one is the wildest").
{end_rule(cfg.cta_enabled)}
- narration: plain spoken prose for a voiceover. No stage directions, no quotes
  inside the text, no markdown, no emoji.
- image_prompt: a detailed visual description for an AI image generator matching
  that scene. {art_brief}
- title: curiosity-gap style, under 70 characters. No clickbait lies.
- tags: 8 to 12 short search tags. Do not leave this empty.
- Be factually careful. If a detail is uncertain, leave it out rather than invent it.

Return ONLY a JSON object in exactly this shape:
{{
  "title": "engaging video title, under 70 characters",
  "description": "2-4 sentence video description",
  "tags": ["up to 12 relevant tags"],
  "scenes": [
    {{"narration": "...", "image_prompt": "..."}}
  ]
}}"""

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
                raise RuntimeError(f"Unexpected Gemini response shape: {str(data)[:400]}") from exc
            try:
                script = _parse(text)
            except ValueError as exc:
                self._save_debug(cfg, text)
                raise RuntimeError(
                    "Gemini returned no usable scenes twice. Raw response saved to "
                    f"{cfg.work_dir / 'gemini_last.txt'}. Snippet: {text[:200]!r}"
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


def get_provider(cfg: Config) -> ScriptProvider:
    """Return the configured provider, falling back to templates if unusable."""
    if cfg.ai_provider == "template":
        return TemplateProvider()

    try:
        return GeminiProvider(cfg.gemini_api_key, cfg.gemini_model)
    except RuntimeError as exc:
        print(f"[script] {exc}")
        print("[script] falling back to the offline template provider.")
        return TemplateProvider()
