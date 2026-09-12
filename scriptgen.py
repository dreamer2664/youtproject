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

    def estimated_seconds(self, words_per_minute: float = 155.0) -> float:
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
class GeminiProvider:
    name = "gemini"

    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    # 429 = rate limited, 500/502/503/504 = transient. The free tier returns 503
    # "high demand" often and unpredictably, so retries are mandatory, not optional.
    RETRYABLE = {429, 500, 502, 503, 504}
    MAX_RETRIES = 6
    MAX_DELAY = 45.0
    # Tried, in order, if the configured model keeps failing with 5xx.
    # These are the lighter models, which stay available when flash saturates.
    FALLBACK_MODELS = ["gemini-flash-lite-latest", "gemini-3.1-flash-lite"]

    def __init__(self, api_key: str, model: str = "gemini-flash-latest") -> None:
        if not api_key:
            raise RuntimeError(
                "No Gemini API key. Get a free one at https://aistudio.google.com/apikey, "
                "then put it in config.yaml or export GEMINI_API_KEY. "
                "Or set ai.provider: template in config.yaml to run keyless."
            )
        self.api_key = api_key
        self.model = model

    def _try_model(self, model: str, payload: dict) -> tuple[dict | None, int, str]:
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
                print(f"  [script] {model}: HTTP {response.status_code}, retry in "
                      f"{delay:.0f}s ({attempt}/{self.MAX_RETRIES})")
                time.sleep(delay)

        return None, last_status, last_error

    def _post(self, payload: dict) -> dict:
        chain = [self.model] + [m for m in self.FALLBACK_MODELS if m != self.model]
        last_status, last_error = 0, ""

        for index, model in enumerate(chain):
            result, status, error = self._try_model(model, payload)
            if result is not None:
                if index > 0:
                    print(f"  [script] using fallback model {model}")
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
                print(f"  [script] {model} is not available (404); trying the next model.")
                continue
            if index + 1 < len(chain):
                print(f"  [script] {model} gave up (HTTP {status}); trying a fallback model.")

        if last_status == 404:
            raise RuntimeError(
                "No Gemini model in the fallback chain exists. Set ai.gemini_model to "
                f"'gemini-flash-latest'. Last error: {last_error}"
            )
        raise RuntimeError(
            f"Gemini unavailable after trying {len(chain)} model(s) with retries "
            f"(last HTTP {last_status}). This is free-tier saturation — wait a few "
            f"minutes and re-run; nothing was lost. {last_error}"
        )

    def generate(self, cfg: Config, topic_override: str | None = None) -> Script:
        topic = topic_override or cfg.topic
        target = cfg.target_seconds
        # roughly 155 words/minute of narration
        scene_count = max(3, min(10, round(target / 22)))

        prompt = f"""You are a script writer for a YouTube channel.

CHANNEL TOPIC: {topic}
TONE: {cfg.tone}
AUDIENCE: {cfg.audience}
LANGUAGE: {cfg.language}
TARGET VIDEO LENGTH: about {target} seconds

Pick ONE specific, genuinely interesting story or fact within the topic.
Write {scene_count} scenes of narration that together take about {target} seconds
when read aloud (roughly {int(target * 2.6)} words total).

Requirements:
- narration: plain spoken prose for a voiceover. No stage directions, no quotes
  inside the text, no markdown, no emoji.
- image_prompt: a detailed visual description for an AI image generator matching
  that scene. Photorealistic, cinematic, specific. No text or watermarks in image.
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
            raise RuntimeError(f"Unexpected Gemini response shape: {str(data)[:400]}") from exc

        script = normalise_script(extract_json(text), self.name)
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

        beats = [
            f"Today we look at {topic}, a subject that is far stranger than most people realise.",
            f"To understand it, we have to start with the context, because {topic} did not "
            f"happen in isolation.",
            "The details that survive are specific, and they are the reason this story still "
            "holds up today.",
            "There is a moment in this account that changes how you read everything before it.",
            f"So what does {topic} actually tell us? More than you would expect.",
        ]

        scenes = [
            Scene(narration=beat, image_prompt=f"cinematic photorealistic scene depicting {topic}")
            for beat in beats
        ]

        return Script(
            title=title,
            description=(
                f"{title}\n\nA short documentary-style look at {topic}.\n\n"
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
