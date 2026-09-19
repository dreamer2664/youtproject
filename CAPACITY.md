# Daily capacity: what the free stack can actually produce

Researched 2026-09-16. All quotas below are free-tier, no credit card.
Bottom line first: **5 videos/day is sustainable; ~25/day is a safe burst
ceiling.** The binding constraints are premium voice minutes and the
OpenRouter daily cap — not script intelligence.

## What one ~65s video costs the stack

| Resource | Per video | Notes |
|---|---|---|
| LLM requests | ~11 | script + fact-check + 2 editorial + ~7 director queries |
| LLM tokens | ~20k | prompts are long, answers short |
| Images | 21 (7 scenes × 3) | or 7 Pexels searches when the stock lane is on |
| Voice | ~1,000 chars | ~65s of narration |
| YouTube API | ~0 | no YouTube API is used (manual upload) |

## Lane-by-lane ceilings (with your key counts)

| Lane | Free quota (2026-09) | Videos/day |
|---|---|---|
| Gemini (5 keys) | ~1,000 req/day/key on Flash models, 5–15 RPM ([cloudzero](https://www.cloudzero.com/blog/gemini-pricing/), [tokenmix](https://tokenmix.ai/blog/gemini-api-free-tier-limits)) | **~450** (5,000 req ÷ 11) |
| Groq (6 keys) | 30 RPM; 1,000–14,400 req/day + ~500k tokens/day on small models; **limits are org-level, extra keys on the same org do NOT multiply quota** ([tokenmix](https://tokenmix.ai/blog/groq-free-tier-limits-2026), [layer3labs](https://www.layer3labs.io/guides/groq-pricing), [eesel](https://www.eesel.ai/blog/groq-pricing)) | **~25–90** (token-bound on 8B, request-bound on 120B) |
| OpenRouter (6 keys) | 20 RPM + 50 req/day per unfunded account; 1,000/day after a one-time $10 credit purchase ([ask-coreai](https://ask-coreai.com/blog/openrouter-free-models-2026-limits-catches), [pinggy](https://pinggy.io/blog/free_ai_model_apis_unlimited_tokens_openrouter/), [datastudios](https://www.datastudios.org/post/openrouter-api-key-free-limits-free-routes-paid-access-and-byok)) | **~4–27** (depends whether the 6 keys are separate accounts) |
| Pollinations text | no published quota (soft/unknown) | estimated 100+; last resort before offline template |
| Pexels stock | 200 req/hr + 20,000 req/mo, commercial OK ([freeapihub](https://freeapihub.com/apis/pexels-api), [pexels docs](https://github.com/developer-ishan/mcp-pexels/blob/main/docs/official/pexels-api-docs.md)) | **~95/day** sustained (20k/mo ÷ 7 searches) |
| Pollinations images | anonymous ~1 req/15s (own pacer) | 105 imgs/day ≈ 30 min of paced fetching — fine |
| ElevenLabs voice | 6 × 10k chars/mo (your plan) | **~2/day** premium; overflow → edge-tts (soft limits) |

## How to read this

- Everyday 5/day runs almost entirely on **Gemini + Pexels + edge-tts**,
  none of which break a sweat at that volume.
- Groq + OpenRouter are the afternoon-slump insurance: when Gemini 503s,
  they cover the same 5 videos with room to spare.
- If OpenRouter ever starves (50/day on one account ≈ 4 videos), the fix
  is the one-time $10 credit purchase → 1,000/day on that account.
  Nothing else in the stack needs money.
- Re-verify quotas quarterly — free tiers move (e.g. Gemini Pro left the
  free tier 2026-04-01, which is why no Pro model is in any chain).

## Live usage

`python main.py keys` shows every key: requests/characters spent today or
this month, what's left of each free tier, and when it refills (self-counted
ledger in work/usage_ledger.json — the APIs don't expose remaining quota).
