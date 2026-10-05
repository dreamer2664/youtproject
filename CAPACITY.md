# Daily capacity: what the free stack can actually produce

Researched 2026-09-16; most quotas re-verified 2026-10-02, with YouTube's
quota scope corrected 2026-10-05 (see the bottom section). All quotas below
are free-tier, no credit card.

**Bottom line at a planning cadence** (one clip per channel per day, five
channels currently, eight after the three planned additions, plus a boardroom
meeting): the stack still has ample free-tier headroom. **The constraint is
judgment and hand-uploading, not quota.** The pool most likely to bite is
**Groq Whisper audio-minutes** (~8 h/day, org-level): fresh/uncached sources
now use Whisper first even when YouTube captions exist, while the shared
transcript cache prevents repeat audio spend. Premium-voice minutes and the
OpenRouter daily cap used to be the binding constraints; premium voices are
off by default now and OpenRouter only burns during a multi-provider outage.

For volume runs the honest ceilings are below, but **do not plan from this
table** — run `python main.py keys` for self-counted usage and
`python main.py keys --advice` for the multi-key truths. The dashboard only
aggregates where independent pools are known; it deliberately does not
multiply YouTube keys because that quota is project-level. Quotas move; the
ledger shows local spend, not provider-side remaining balances.

## What one ~65s generated video costs the stack

| Resource | Per video | Notes |
|---|---|---|
| LLM requests | **5–6** | script 1 · factcheck 1 · punch-up 1 · title polish 1 · decringe 1 · topic top-up 1 (only when the backlog dips). Was ~11 before the 2026-09-23 call-site audit — see the budget table in OPERATIONS.md. Hook/title fixes and expand/tighten fire only on violations. |
| LLM tokens | ~20k | prompts are long, answers short |
| Images | **7 × images_per_scene, plus extra shots on long scenes** | the adaptive shot plan (`images.plan_shot_counts`) gives any scene longer than ~6.5 s per image more shots, capped at 3× `images_per_scene` (max 10). So 3/scene ≈ 21 for evenly-paced scenes, more when one scene runs long. With a Pexels/Pixabay key each is a stock search; without one each is a paced Pollinations fetch (~50 s). |
| Voice | ~1,000 chars | ~65 s of edge-tts narration (free, unmetered) |
| Groq Whisper | 0 | the generate lane writes its own script — nothing to transcribe |
| YouTube API | ~0 | no upload path; only the optional read-only public-stats commands (`yt` = 1 unit, `snap` ≈ 3/channel), which never gate a render |

## Lane-by-lane ceilings

Key counts below are the ones this project actually runs with as of
2026-10-03; `python main.py keys` prints the live census.

| Lane | Free quota (Oct 2026) | Generated videos/day |
|---|---|---|
| Gemini (**~35 keys**) | ~1,500 req/day **per project/key** on Flash (3 Flash 10 RPM · 250k TPM · 3.1 Flash-Lite 1,000/day · Pro 50/day). Separate Google accounts/Cloud projects genuinely multiply ([pecollective](https://www.pecollective.com/tools/gemini-free-tier-guide), [tokenmix](https://tokenmix.ai/blog/gemini-api-free-tier-limits)) | **effectively unbounded for this workload** — 35 pools × 1,500 ≈ 52,500 req/day ÷ 6 per video, IF the keys are on separate projects. Verify with `keys --probe`; anything on one shared project is ONE pool. |
| Groq (**5 keys**) | 30 RPM · ~1,000 req/day per model (14.4k/day org aggregate) · **~8 h Whisper audio/day** — **org-level: 5 keys on one account = 5 keys' worth of nothing extra** ([cloudzero](https://www.cloudzero.com/blog/groq-pricing), [layer3labs](https://www.layer3labs.io/guides/groq-pricing)) | ~165 chat-bound, but the real gate is Whisper: **~24 uncaptioned 20-min sources/day**, and captions-first ingest means most sources cost 0 |
| OpenRouter | 20 RPM + **50 req/day per unfunded ACCOUNT**; one-time $10 credit → 1,000/day forever ([costgoat](https://costgoat.com/deals/openrouter.ai)) | **~8** unfunded (50 ÷ 6), **~165** after the $10 credit. Last-resort lane: normally spends 0 |
| DeepSeek | balance-based free grant (platform.deepseek.com) | overflow lane, normally 0 |
| Pollinations text | no published quota (soft/unknown); measured ~45 s even on tiny prompts | last resort before the offline template |
| Pexels stock | 200 req/hr + 20,000 req/mo, commercial OK ([pexels docs](https://www.pexels.com/api/)) | **~95/day** sustained (20k/mo ÷ 7 searches/video) — the practical ceiling of the generate lane if you want stock photos in every video |
| Pixabay stock | ~100 req/min per key, no published monthly cap | the second stock lane; effectively unmetered at this volume |
| Pollinations images | anonymous ~1 req/15 s (own pacer) + ~50 s per image | ~105 imgs/day paced ≈ 5 videos. **Add a Pexels/Pixabay key or a Pollinations token** — this is the slowest link in the generate lane |
| ElevenLabs voice | ~10k chars/month **per key** | **0 today**: the 6 keys recorded in Sep 2026 all probed 401-dead (commit `ea37869`). Re-create keys or stay on edge-tts (free, unmetered) — `ai.premium_voices` caps it either way |
| YouTube Data API | 10,000 standard units/day/project + separate 100 `search.list` calls/day/project (1 unit/call) | n/a — `snap` costs ~3 units/channel/run; searches use the separate call bucket |


## How to read this

- Everyday 5/day runs almost entirely on **Gemini + Pexels + edge-tts**,
  none of which break a sweat at that volume.
- **Long-form (Sep 2026) stays cheapest per output-minute when
  transcripts are cached**: a whole render uses 0 LLM/audio calls;
  cutting an unpunctuated cached transcript adds one cached eos pass
  (~8 calls / ~11k tokens on 20 min). On an uncached source the policy
  (2026-10-04) uses Groq Whisper first even when YouTube captions exist,
  and harvests captions only if Whisper fails or returns no words. It
  touches no image, vision, voice or YouTube-API quota — see
  API-REPORT.md for the accounting.
- Groq + OpenRouter are the afternoon-slump insurance: when Gemini 503s,
  they cover the same 5 videos with room to spare.
- If OpenRouter ever starves (50/day on one account ≈ 8 generated videos,
  and it's a last-resort lane that normally spends 0), the fix is the
  one-time $10 credit purchase → 1,000/day on that account. Nothing else
  in the stack needs money. The other clean move is a Groq credit card:
  Developer tier is $0 minimum spend and multiplies rate limits ~10×,
  **including the Whisper audio pool** — the one quota that can gate a day.
- Re-verify quotas quarterly — free tiers move (e.g. Gemini Pro left the
  free tier 2026-04-01, which is why no Pro model is in any chain).

## Live usage

`python main.py keys` shows self-counted requests/characters/audio and
refill windows; providers do not expose remaining quota to this ledger.
With multiple keys, section headers explain shared pools. For known
per-key providers it can show an aggregate row. YouTube is different:
quota is per Cloud project, but the ledger cannot map keys to projects or
count other apps, so it shows per-key usage and search calls without
claiming project quota remaining or adding capacities. `keys --month`
splits spend by call tag, including which lane ate the Whisper pool.

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
| Pexels / Pixabay | unchanged (200/h + 20k/mo · ~100/min) | per-key limits; see provider docs |
| YouTube Data API | 10k standard units/day/project; separate 100 `search.list` calls/day/project (1 unit/call) | keys in one project share quota; do not shard one use case across projects |
| Pollinations | unchanged (1 req/15s) | paced; no published hard cap |

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

## Transcript routing + key split update (2026-10-04)

- **Transcript order on a cache miss:** Groq Whisper first, then keyless
  YouTube captions only if Whisper fails or returns no words, then the
  existing transcript-fix pass. Fresh/uncached sources therefore use
  Whisper audio even when captions are available. The shared cache is
  kept; old caption-derived entries remain in use until manually cleared.
- **Groq allocation:** `ai.groq_transcription_percent` defaults to 90.
  In the ordered Groq list the first share is transcription-only; the tail
  is reserved for the Groq text fallback after the complete Gemini
  primary/reserve sweep. Examples: 35 keys → 32 Whisper / 3 text;
  80 → 72 / 8. With at least two keys the default rounding preserves a
  text key; a single key is Whisper-only. Set the percent to 0 for
  captions-first or 100 for Whisper-only.
- **Per-account pools:** Groq limits are per organization/account. Keys on
  one account share its pool; separately owned accounts are separate. The
  `keys` dashboard therefore shows a Groq role split and per-key Whisper
  audio labelled per account, and does not print a multiplied daily
  aggregate (that row would invent capacity that does not exist).
- Whisper tries the configured keys in order and advances on
  auth/rate/network failures; it is failover, not round-robin. In normal
  operation the first healthy Whisper key takes the load until it is
  rate-limited. The usage ledger tracks attempted Groq audio requests by
  masked key; it is not a provider-side quota read.

## YouTube quota correction (2026-10-05)

The current [official quota calculator](https://developers.google.com/youtube/v3/determine_quota_cost)
(last updated 2026-09-15) defines **10,000 standard units/day per Cloud
project**. `search.list` has a separate **100 calls/day/project** bucket,
and each call costs **1 unit**; `videos.insert` likewise has a separate
100-calls/day bucket at 1 unit/call. `channels.list`, `videos.list`, and
`playlistItems.list` cost 1 unit/call. Keys in one project share the
project quota; the YouTube guide forbids spreading one API use case across
projects just to increase quota.

The code now counts search calls separately, corrects identifiable legacy
100-unit search rows at read time without rewriting the ledger, and stops
on quota/rate errors instead of trying another project. The ledger can only
see calls from this installation and cannot infer which configured keys
share a project or what other clients used; `keys` therefore shows
self-counted usage but does not add project capacity or claim a precise
remaining quota. `crew.max_searches: 3` is a local safety limit, not the
published API limit.
