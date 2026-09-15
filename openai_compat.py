"""Shared base for OpenAI-compatible chat providers (Groq, OpenRouter, ...).

Subclasses set only data (endpoint URL, model list, headers, key hint); all
behavior lives here: key rotation, model fallback, quota-aware pool skips.
The logic was built and live-verified against Groq (see groq.py notes) and
generalized when OpenRouter joined as the second lane.
"""

from __future__ import annotations

import requests

from config import Config
from scriptgen import GeminiProvider


class OpenAICompatProvider(GeminiProvider):
    """Gemini-shaped generate()/generate_text() over an OpenAI chat endpoint."""

    name = "openai-compat"
    api_url = ""
    debug_file = "llm_last.txt"
    DEFAULT_MODEL = ""
    FALLBACK_MODELS: list[str] = []
    no_key_hint = "set API keys in config.yaml"

    def __init__(self, api_keys: list[str] | str, model: str = "") -> None:
        if isinstance(api_keys, str):
            api_keys = [api_keys]
        keys = [key.strip() for key in api_keys if key and key.strip()]
        if not keys:
            raise RuntimeError(f"No {self.name} API keys. {self.no_key_hint}")
        self.api_keys = keys
        self.model = model or self.DEFAULT_MODEL

    @classmethod
    def _save_debug(cls, cfg: Config, raw: str) -> None:
        """Keep the raw model output so a parse failure is diagnosable."""
        try:
            cfg.work_dir.mkdir(parents=True, exist_ok=True)
            (cfg.work_dir / cls.debug_file).write_text(raw, encoding="utf-8")
        except OSError:
            pass

    def _headers(self, key: str) -> dict:
        return {"Authorization": f"Bearer {key}"}

    def _tweak_body(self, body: dict, model: str) -> None:
        """Per-model body adjustments (reasoning flags etc.). No-op here."""

    def _pool_dry(self, text: str) -> bool:
        """True when a 429 condemns every key (jump model pools at once)."""
        return "organization" in text

    @staticmethod
    def _read_response(response) -> tuple[int, str, dict]:
        """(status, text, payload): folds 200-with-error into the status flow.

        Some gateways answer HTTP 200 with an {"error": {...}} body instead
        of a real status (seen on OpenRouter free pools); without this the
        reply-shape parser would raise instead of rotating.
        """
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict) and "error" in payload and "choices" not in payload:
            err = payload.get("error") or {}
            try:
                status = int(err.get("code", 0) or 0)
            except (TypeError, ValueError):
                status = 0
            if status < 400:
                status = response.status_code if response.status_code >= 400 else 502
            text = str(err.get("message", "")) or response.text
            return status, text, {}
        if not isinstance(payload, dict):
            payload = {}
        return response.status_code, response.text, payload

    def _post(self, payload: dict, tag: str = "script") -> dict:
        """POST with key rotation + model fallback; reply in Gemini shape."""
        # generate() speaks Gemini (payload carries contents[]; the reply is
        # read from candidates[]). Rebuilt here as OpenAI chat, translated
        # back on return so the inherited flow works untouched.
        try:
            prompt = payload["contents"][0]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"unexpected internal payload shape: {str(payload)[:200]}")
        gen = payload.get("generationConfig", {}) or {}
        text = self._complete(
            prompt,
            temperature=gen.get("temperature", 0.7),
            json_mode=gen.get("responseMimeType") == "application/json",
            tag=tag,
        )
        return {"candidates": [{"content": {"parts": [{"text": text}]}}]}

    def _complete(self, prompt: str, temperature: float,
                  json_mode: bool, tag: str) -> str:
        models = [self.model] + [m for m in self.FALLBACK_MODELS if m != self.model]
        keys = list(self.api_keys)
        last_error = "no keys tried"
        for model in models:
            body: dict = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
            }
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            self._tweak_body(body, model)
            tried = down = 0
            index = 0
            while index < len(keys):
                key = keys[index]
                try:
                    response = requests.post(
                        self.api_url,
                        headers=self._headers(key),
                        json=body,
                        timeout=60,
                    )
                except Exception as exc:
                    # Hung route / DNS: one spare key, then next model —
                    # waiting out every key at 60 s each is never worth it.
                    last_error = f"{model} unreachable ({str(exc)[:120]})"
                    down += 1
                    if down >= 2:
                        print(f"  [{tag}] {self.name} {model} unreachable twice; "
                              f"trying next model.")
                        break
                    index += 1
                    continue
                down = 0
                status, text, payload = self._read_response(response)
                if status == 200:
                    try:
                        content = payload["choices"][0]["message"]["content"]
                    except (KeyError, IndexError, TypeError):
                        raise RuntimeError(
                            f"unexpected {self.name} reply shape: {response.text[:200]}")
                    # A dry backing pool can come back 200-with-empty (seen
                    # live): a pool-wide symptom, so jump pools at once.
                    if not content or not str(content).strip():
                        last_error = f"{model} returned empty content"
                        print(f"  [{tag}] {self.name} {model} returned empty content; "
                              f"trying next model.")
                        break
                    return content
                last_error = f"HTTP {status}: {text[:160]}"
                if status in (401, 403):
                    print(f"  [{tag}] {self.name} key ...{key[-4:]} rejected — "
                          f"dropping it for this run")
                    keys.pop(index)
                    continue
                if status == 404:
                    print(f"  [{tag}] {self.name} {model} unavailable (404); trying next model.")
                    break
                if status == 429:
                    # Pool-dry 429 (marks every key): jump pools at once.
                    # Any other 429 is per-key: rotate immediately.
                    if self._pool_dry(text):
                        print(f"  [{tag}] {self.name} {model} pool dry; "
                              f"trying next model.")
                        break
                    tried += 1
                    index += 1
                    continue
                # 5xx/odd: server-side, no key will fix it — next model.
                print(f"  [{tag}] {self.name} {model} HTTP {status}; "
                      f"trying next model.")
                break
            else:
                if tried:
                    print(f"  [{tag}] all {tried} {self.name} key(s) rate-limited on "
                          f"{model}; trying next model.")
        raise RuntimeError(
            f"{self.name.capitalize()} unavailable after {len(models)} model(s) "
            f"x keys ({last_error}). Free-tier quotas reset quickly — re-run in a bit.")

    def generate_text(self, prompt: str, temperature: float = 0.7,
                      tag: str = "llm", json_mode: bool = False) -> str:
        """Raw text completion using key rotation + model fallback."""
        return self._complete(prompt, temperature=temperature,
                              json_mode=json_mode, tag=tag)
