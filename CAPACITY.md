# Daily capacity: what the free stack can actually produce

Researched 2026-09-16, quotas re-verified 2026-10-01 (see the bottom
section). All quotas below are free-tier, no credit card. Bottom line
first: **5 videos/day is sustainable; ~25/day is a safe burst ceiling.**
The binding constraints are premium voice minutes and the OpenRouter
daily cap — not script intelligence. At the NEW cadence (one clip
generation per channel per day + a boardroom meeting) the stack runs at
~5% of its free capacity — the constraint is judgment, not quota.

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
- **The long-form lane (Sep 2026) is the cheapest per output-minute**:
  0 LLM calls for a whole render of a captioned/cached source, one
  cached eos pass (~8 calls / ~11k tokens on 20 min) when cutting, and
  Groq Whisper minutes only when captions don't exist. It touches no
  image, vision, voice or YouTube-API quota at all — see API-REPORT.md
  for the measured table.
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

## Re-verification 2026-10-01 (what moved, what to check)

- **Gemini**: Google removed the public free-tier RPM/RPD tables (now
  only inside AI Studio, per project). Third-party reports CONFLICT on
  the newest models: gemini-3.8/3.7/3.6/3.5 Flash ~20 RPD/key vs older
  Flash/Flash-Lite 500–1,500 RPD. Treat the tight number as real for the
  newest Flash and **run `python main.py keys --probe`** to measure your
  own keys; the model sandwich already falls back to the bigger-quota
  older models. If probes confirm ~20 RPD on 3.8-flash, set
  `ai.gemini_model` to a Flash-Lite id for the bulk lanes and keep the
  newest Flash for scripts only.
- **Groq** (gpt-oss-120b/20b, Qwen): 30 RPM, **1,000 req/day but only
  ~200k tokens/day**, org-level — extra keys on one account do NOT add
  quota. The clip lane's Whisper minutes are a separate pool.
- **OpenRouter**: 50 free-model requests/day unfunded, 1,000/day after a
  one-time $10 credit purchase. Still the afternoon-slump insurance.
- **DeepSeek**: the official platform free tier is promo-credit based;
  aggregators (LLM7, TeamoRouter/OpenCode Zen) serve deepseek-flash class
  models permanently free at ~200 req/day if you ever want a dedicated
  Analyst seat.
- **Pollinations**: anonymous ~1 request/15s (text AND image); a free
  auth.pollinations.ai token raises it and removes the image watermark.
  Measured from this sandbox: even tiny prompts can take ~45s — it is a
  last-resort lane, never a primary.
- **New lanes' budgets**: `longform` ≈ a clip run or less (chapter pick
  1/window + fix 1; transcript cache shared). `meeting` ≈ 10 short calls,
  ~3–25k tokens depending on the agenda size. Two meetings/day is noise
  next to any lane.

## Re-verification 2026-10-02 (fresh quotas + the multi-key truth)

Current free-tier consensus (sources below; `python main.py keys` stays
the live ledger, `keys --probe` measures your own keys):

| Provider | Free tier (Oct 2026) | Extra keys on the SAME account |
|---|---|---|
| Gemini | Gemini 3 Flash 10 RPM / 250k TPM / **1,500 req/day per project**; 3.1 Flash-Lite 15 RPM / 1,000/day; Pro 50/day | nothing — the quota is per PROJECT. New keys from *other projects/accounts* = new pools (the legit multiplier) |
| Groq | 30 RPM / 6k TPM / ~1,000 req/day per model (14.4k/day org aggregate), ~8h Whisper audio | **NOTHING — the pool is org-level.** All keys on one account share it |
| OpenRouter | 25+ `:free` models, 20 RPM, **50 req/day per account**; one-time $10 credit → **1,000/day** (credit never expires) | nothing — the cap is per ACCOUNT, keys share it |
| DeepSeek | balance-based free grant (platform.deepseek.com) | new account = new grant |
| ElevenLabs | ~10k chars/month per key | each key its own ~10k |
| Pexels / Pixabay / YouTube / Pollinations | unchanged (200/h + 20k/mo · ~100/min · 10k units/day · 1 req/15s) | each key its own pool |

**What this means for the key re-creation plan** (decided 2026-10-02):

1. Ten OpenRouter keys on ONE account = still 50 requests/day total.
   Either spread across separate accounts (ToS gray zone — it works,
   know what it is), or put ONE $10 credit on one account → 1,000
   requests/day forever. The $10 beats ~19 extra accounts and is fully
   legit.
2. Groq: same-account keys add literally nothing. Separate accounts
   each get the org pool. The clean move: **add a credit card** — the
   Developer tier has zero minimum spend and multiplies rate limits
   ~10x (that's 10x Whisper minutes too, our binding constraint).
3. Gemini: keys from separate Google accounts (or separate Cloud
   projects) genuinely multiply — each carries its own ~1,500/day.
   This is the provider where multiple keys are unambiguously worth it.
4. The 2026-10-01 caveat stands for the very newest Flash ids
   (3.5–3.8): third-party tables disagree (some report ~20 RPD early in
   a model's life); our model sandwich falls back to older Flash ids
   automatically. `keys --probe` settles it on your own keys.

Sources (fetched 2026-10-02): pecollective.com/tools/gemini-free-tier-guide
(Gemini 3 Flash 10 RPM / 250k TPM / 1,500 RPD; 3.1 Flash-Lite 1,000 RPD),
tokenmix.ai/blog/gemini-api-free-tier-limits (2.5 Flash 1,500 RPD; Pro 50
RPD), cloudzero.com/blog/groq-pricing (Groq org-level: 30 RPM / 6k TPM /
14.4k RPD, "multiple API keys don't help"; Developer tier 10x),
layer3labs.io/guides/groq-pricing (Whisper ~20 RPM / 2,000 req/day
separate pool), costgoat.com/deals/openrouter.ai + buldrr.com (50/day →
$10 credit → 1,000/day, credits never expire).
