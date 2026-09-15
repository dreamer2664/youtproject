"""OpenRouter script provider: free-model overflow lane, key rotation.

Last LLM resort before the offline template: free pools are congested most
afternoons, but they sit on different upstreams than Gemini/Groq, so when
those are dry this lane is often still wet. Same transport base as Groq
(openai_compat), different endpoint + headers + pool signals.
"""

from __future__ import annotations

from openai_compat import OpenAICompatProvider


class OpenRouterProvider(OpenAICompatProvider):
    name = "openrouter"
    api_url = "https://openrouter.ai/api/v1/chat/completions"
    debug_file = "openrouter_last.txt"
    DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
    # Verified Sep 2026: nemotron + lfm returned exact JSON; glm + gemma
    # are valid IDs on congested pools (429, not 404) at probe time.
    # inkling:free excluded: 403 unless called from an agentic harness.
    FALLBACK_MODELS = [
        "nvidia/nemotron-3-super-120b-a12b:free",
        "z-ai/glm-5.2:free",
        "google/gemma-4-31b-it:free",
        "liquid/lfm-2.5-2.6b:free",
    ]
    no_key_hint = ("Get free ones at https://openrouter.ai/keys, then set "
                   "ai.openrouter_api_keys in config.yaml or export OPENROUTER_KEYS.")

    def _headers(self, key: str) -> dict:
        return {"Authorization": f"Bearer {key}",
                "HTTP-Referer": "https://github.com/dreamer2664/youtproject",
                "X-Title": "youtproject"}

    def _pool_dry(self, text: str) -> bool:
        # OpenRouter wraps upstream congestion ("temporarily rate-limited
        # upstream", "provider ... overloaded"): every key shares that
        # pool, so jump models. Its own per-key RPM has neither marker.
        lowered = text.lower()
        return ("upstream" in lowered or "provider" in lowered
                or "organization" in lowered)
