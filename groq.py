"""Groq script provider with API-key rotation.

Thin subclass: all behavior (rotation, fallback, pool skips) lives in
openai_compat.OpenAICompatProvider. The model notes below are
Groq-specific and were verified live Sep 2026 (the llama-3.x IDs are
gone from Groq).
"""

from __future__ import annotations

from openai_compat import OpenAICompatProvider


class GroqProvider(OpenAICompatProvider):
    name = "groq"
    api_url = "https://api.groq.com/openai/v1/chat/completions"
    debug_file = "groq_last.txt"
    DEFAULT_MODEL = "qwen/qwen3.8-27b"
    # qwen: own 8k-TPM pool, ~12 ms, clean JSON mode + tool calls.
    # 120b: fallback, needs reasoning hidden (see _tweak_body).
    # compound-mini excluded: 120b-backed, shares that pool, and goes
    # 200-with-empty when dry.
    FALLBACK_MODELS = [
        "qwen/qwen3.8-27b",
        "openai/gpt-oss-120b",
    ]
    no_key_hint = ("Get free ones at https://console.groq.com/keys, then set "
                   "ai.groq_api_keys in config.yaml or export GROQ_API_KEYS.")

    def _tweak_body(self, body: dict, model: str) -> None:
        if "gpt-oss" in model:
            # Reasoning model: hide the chain-of-thought or `content`
            # comes back empty (verified live Sep 2026).
            body["reasoning_format"] = "hidden"
