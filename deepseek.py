"""DeepSeek script provider: free-tier LLM lane, OpenAI-compatible.

Sits after OpenRouter in the chain — a separate upstream with its own
free quota (rate-limited, no card), so when Gemini 503s and Groq/
OpenRouter are dry, this lane is one more chance before the offline
template. Models verified live 2026-09-21: deepseek-flash (fast) and
deepseek-v4-pro (smart) both answer on the free tier.
"""

from __future__ import annotations

from openai_compat import OpenAICompatProvider


class DeepSeekProvider(OpenAICompatProvider):
    name = "deepseek"
    api_url = "https://api.deepseek.com/chat/completions"
    debug_file = "deepseek_last.txt"
    DEFAULT_MODEL = "deepseek-flash"
    FALLBACK_MODELS = ["deepseek-flash", "deepseek-v4-pro"]
    no_key_hint = ("Get a free key at https://platform.deepseek.com, then set "
                   "ai.deepseek_api_keys in config.yaml or export DEEPSEEK_KEYS.")

    def _headers(self, key: str) -> dict:
        return {"Authorization": f"Bearer {key}"}

    def _pool_dry(self, text: str) -> bool:
        # DeepSeek 429s are per-key rate limits, not shared-pool droughts:
        # rotating keys is enough — no need to jump models.
        return False
