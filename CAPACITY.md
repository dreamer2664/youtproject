# Daily capacity: what the free stack can actually produce

Researched 2026-09-16; Gemini and YouTube quota scopes corrected 2026-10-05
(see the update sections below). All quotas below are free-tier, no credit card.

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
multiply Gemini or YouTube keys because those quotas are project-scoped and
the ledger cannot map API keys to projects. Quotas move; the ledger shows
local spend, not provider-side remaining balances.

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
| Gemini (configured API-key pool) | Limits vary by model and usage tier; Google applies them **per project, not per API key**. Check the active values in [Google AI Studio](https://aistudio.google.com/) and the official [rate-limit page](https://ai.google.dev/gemini-api/docs/rate-limits). | **Unknown from key count alone.** Keys in the same project share limits; this app stores no project IDs and cannot know whether the pool represents one project or many. `keys --probe` tests a request, not remaining quota. |
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
per-key providers it can show an aggregate row. Gemini and YouTube are
project-scoped; the ledger cannot map keys to projects or count other apps,
so it shows per-key local use without claiming project quota remaining or
adding capacities. Gemini's active limits are model/tier-specific and live
in AI Studio; `keys --probe` only checks whether one request succeeds.
`keys --month` splits spend by call tag, including which lane ate the
Whisper pool.

## Re-verification 2026-10-01 (what moved, what to check)

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

## Re-verification 2026-10-05 (Gemini project-scope correction)

Free-tier limits move; the official [Gemini rate-limit page](https://ai.google.dev/gemini-api/docs/rate-limits)
now explicitly says limits are per project, vary by model and usage tier, and
are visible in AI Studio. `python main.py keys` remains a local spend ledger;
`keys --probe` tests a request, not your remaining quota.

| Provider | Free tier (Oct 2026) | Extra keys on the SAME account |
|---|---|---|
| Gemini | Active request/token/image limits vary by model and usage tier; view them in Google AI Studio. Official scope: **per project, not per API key** ([rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)). | Keys in one project share quota; the app cannot map API keys to project IDs. Another key helps only if its project has an available applicable limit; account/key count alone proves nothing. |
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
3. Gemini: quotas are project-scoped, not key-scoped. Keys in the same
   project share limits; verify each key's project in AI Studio/Cloud
   Console before treating the pool as independent. Google account count
   alone is not enough to infer project count.
4. The exact active Gemini RPM/TPM/RPD and image limits vary by model and
   usage tier. AI Studio is the current source of truth; the local probe
   only shows that one request succeeded and does not measure quota.

Sources (checked 2026-10-05): Google [Gemini API rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)
— project scope, model/tier variation, and AI Studio as the active-limit
view; Google [troubleshooting guide](https://ai.google.dev/gemini-api/docs/troubleshooting)
— recommends backoff for 429/503 errors; neither status alone proves a bad
key. Other provider estimates below were previously checked 2026-10-02:
cloudzero.com/blog/groq-pricing (Groq org-level limits),
layer3labs.io/guides/groq-pricing (Whisper pool), and
costgoat.com/deals/openrouter.ai + buldrr.com (OpenRouter free tier).

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
