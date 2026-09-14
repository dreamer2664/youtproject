"""Groq script provider with API-key rotation.

Groq is OpenAI-compatible (POST /openai/v1/chat/completions, Bearer key),
so this subclasses GeminiProvider and overrides only the transport: the
script prompt, JSON parsing, and length-repair passes are inherited
unchanged, which keeps the two LLM paths from drifting apart. _post
translates Groq's OpenAI-shaped reply into the Gemini shape generate()
expects (documented hack, contained to one method).

Key rotation is the point, but it is quota-aware: Groq's TPM quota is
shared per organization, so a 429 naming the organization skips the
remaining keys and jumps to the next model pool at once (rotating keys
there only burns seconds). Per-key 429s still rotate; rejected keys
(401/403) are dropped for the run; retired model IDs (404), server
errors (5xx), empty replies, and two consecutive hangs all advance to
the next model. Exhaustion raises RuntimeError so the chain moves on.
"""

from __future__ import annotations

import requests

from config import Config
from scriptgen import GeminiProvider

URL = "https://api.groq.com/openai/v1/chat/completions"


class GroqProvider(GeminiProvider):
    name = "groq"

    # Verified live Sep 2026 (the llama-3.x IDs are gone from Groq). qwen
    # is the default: own 8k-TPM pool, ~12 ms, clean JSON mode. 120b is
    # the fallback: needs reasoning_format:hidden (see _complete) or its
    # reply lands in `reasoning` with empty content. compound-mini is NOT
    # chained: it is 120b-backed (shares that pool, adds nothing) and goes
    # 200-with-empty when the pool is dry. Configured model first.
    FALLBACK_MODELS = [
        "qwen/qwen3.8-27b",
        "openai/gpt-oss-120b",
    ]

    def __init__(self, api_keys: list[str] | str, model: str = "qwen/qwen3.8-27b") -> None:
        if isinstance(api_keys, str):
            api_keys = [api_keys]
        keys = [key.strip() for key in api_keys if key and key.strip()]
        if not keys:
            raise RuntimeError(
                "No Groq API keys. Get free ones at https://console.groq.com/keys, "
                "then set ai.groq_api_keys in config.yaml or export GROQ_API_KEYS."
            )
        self.api_keys = keys
        self.model = model

    @staticmethod
    def _save_debug(cfg: Config, raw: str) -> None:
        """Keep the raw model output so a parse failure is diagnosable."""
        try:
            cfg.work_dir.mkdir(parents=True, exist_ok=True)
            (cfg.work_dir / "groq_last.txt").write_text(raw, encoding="utf-8")
        except OSError:
            pass

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
            if "gpt-oss" in model:
                # Reasoning model: hide the chain-of-thought or `content`
                # comes back empty (verified live Sep 2026).
                body["reasoning_format"] = "hidden"
            tried = down = 0
            index = 0
            while index < len(keys):
                key = keys[index]
                try:
                    response = requests.post(
                        URL,
                        headers={"Authorization": f"Bearer {key}"},
                        json=body,
                        timeout=60,
                    )
                except Exception as exc:
                    # Hung route / DNS: one spare key, then next model —
                    # waiting out every key at 60 s each is never worth it.
                    last_error = f"{model} unreachable ({str(exc)[:120]})"
                    down += 1
                    if down >= 2:
                        print(f"  [{tag}] groq {model} unreachable twice; "
                              f"trying next model.")
                        break
                    index += 1
                    continue
                down = 0
                if response.status_code == 200:
                    try:
                        text = response.json()["choices"][0]["message"]["content"]
                    except (KeyError, IndexError, TypeError):
                        raise RuntimeError(
                            f"unexpected Groq reply shape: {response.text[:200]}")
                    # A dry backing pool can come back 200-with-empty (seen
                    # live): a pool-wide symptom, so jump pools at once.
                    if not text or not str(text).strip():
                        last_error = f"{model} returned empty content"
                        print(f"  [{tag}] groq {model} returned empty content; "
                              f"trying next model.")
                        break
                    return text
                last_error = f"HTTP {response.status_code}: {response.text[:160]}"
                if response.status_code in (401, 403):
                    print(f"  [{tag}] groq key ...{key[-4:]} rejected — "
                          f"dropping it for this run")
                    keys.pop(index)
                    continue
                if response.status_code == 404:
                    print(f"  [{tag}] groq {model} unavailable (404); trying next model.")
                    break
                if response.status_code == 429:
                    # Org-shared TPM quota (the "organization ..." reply):
                    # every key shares this fate, so jump pools at once.
                    # Any other 429 is per-key: rotate immediately.
                    if "organization" in response.text:
                        print(f"  [{tag}] groq {model} org quota dry; "
                              f"trying next model.")
                        break
                    tried += 1
                    index += 1
                    continue
                # 5xx/odd: server-side, no key will fix it — next model.
                print(f"  [{tag}] groq {model} HTTP {response.status_code}; "
                      f"trying next model.")
                break
            else:
                if tried:
                    print(f"  [{tag}] all {tried} groq key(s) rate-limited on "
                          f"{model}; trying next model.")
        raise RuntimeError(
            f"Groq unavailable after {len(models)} model(s) x keys ({last_error}). "
            f"Free-tier quotas reset quickly — re-run in a bit.")

    def generate_text(self, prompt: str, temperature: float = 0.7,
                      tag: str = "groq", json_mode: bool = False) -> str:
        """Raw text completion using key rotation + model fallback."""
        return self._complete(prompt, temperature=temperature,
                              json_mode=json_mode, tag=tag)
