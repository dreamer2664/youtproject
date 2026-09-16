"""Pollinations free text lane: anonymous LLM script backup, no key needed.

Same people as the image lane (images.py). OpenAI-compatible endpoint, free
tier, no account — so it is always available as the last free script lane
before the offline template. Quality is mid-tier (small open models) and the
concrete model behind an alias rotates without notice (probed 2026-09-16:
"openai" answered as gpt-oss-20b), so this lane buys QUOTA DEPTH for the
afternoon slump, not brilliance. JSON mode is deliberately NOT requested
(the endpoint's support is unreliable); the prompts already demand raw JSON
and scriptgen.extract_json forgives fences and prose.
"""

from __future__ import annotations

from openai_compat import OpenAICompatProvider


class PollinationsTextProvider(OpenAICompatProvider):
    name = "pollinations"
    api_url = "https://text.pollinations.ai/openai"
    debug_file = "pollinations_last.txt"
    DEFAULT_MODEL = "openai"
    # 2026-09-16: ?model=mistral 404s ("legacy API") and /models lists
    # only openai-fast — mistral removed; retries carry the lane now.
    FALLBACK_MODELS = ["openai"]
    no_key_hint = "no key needed — anonymous free tier"

    def __init__(self, model: str = "") -> None:
        # A single dummy key drives the shared keys loop; _headers sends no
        # auth (anonymous tier). 401/403 just pops it and moves on.
        self.api_keys = ["anonymous"]
        self.model = model or self.DEFAULT_MODEL

    def _headers(self, key: str) -> dict:
        return {"Referer": "https://github.com/dreamer2664/youtproject"}

    def _tweak_body(self, body: dict, model: str) -> None:
        body.pop("response_format", None)  # unreliable here; prompts suffice
        body["private"] = True  # keep generations out of the public feed
