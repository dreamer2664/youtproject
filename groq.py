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
    DEFAULT_MODEL = "openai/gpt-oss-120b"
    # Probed live 2026-09-16: 120b answers instantly (biggest brain on
    # the free tier, reasoning hidden — see _tweak_body); qwen 8B next
    # (fast, but its TPM pool is shared org-wide, not per-key); 20b is
    # last-resort ballast. compound-mini excluded: 120b-backed, shares
    # that pool, and goes 200-with-empty when dry.
    FALLBACK_MODELS = [
        "openai/gpt-oss-120b", "qwen/qwen3.8-27b",
        "openai/gpt-oss-20b",
    ]
    no_key_hint = ("Get free ones at https://console.groq.com/keys, then set "
                   "ai.groq_api_keys in config.yaml or export GROQ_API_KEYS.")

    def _tweak_body(self, body: dict, model: str) -> None:
        if "gpt-oss" in model:
            # Reasoning model: hide the chain-of-thought or `content`
            # comes back empty (verified live Sep 2026).
            body["reasoning_format"] = "hidden"
