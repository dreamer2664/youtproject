# API report — the long-form lane + a full audit of the stack

Written 2026-09-30, alongside the lane itself (`python main.py longform`).
Numbers below are **measured from the real prompt builders** (a provider
fake captured the exact strings; no API calls were made), on a synthetic
3,000-word transcript ≈ 20 minutes at speaking pace. Token figures use
~4 characters/token for English. Quota limits are the free-tier numbers
already tracked in `CAPACITY.md` / `python main.py keys`.

---

## 1. TL;DR

- **Long-form is the cheapest lane in the project per minute of output.**
  A whole-source render of a captioned (or already-cached) video costs
  **0 LLM calls, 0 audio-minutes, 0 quota units** — pure FFmpeg.
- Cutting an episode (`--minutes`) on a captioned source adds **one eos
  pass, cached forever**: 8 calls / ~11k tokens in on a 20-minute source.
- A fresh source with no YouTube captions costs one **Groq Whisper
  transcription** (20 audio-minutes = 4% of the 8-hour daily pool) plus
  the transcript-fix pass (4 calls / ~11k tokens in).
- **Nothing else in the stack feels it**: no images, no vision, no
  voice, no YouTube Data API units, no ElevenLabs chars. Even
  3 long-forms/day on uncaptioned sources uses ~12% of the Whisper pool
  and under 2% of one Gemini key's request quota.
- **Audit verdict:** two working lanes were invisible (DeepSeek, Pixabay
  — both already in the code, undocumented in the example config; now
  documented), one API is dead by design (HuggingFace, removed Sep 2026),
  and the binding constraint remains **Groq Whisper audio-minutes**, not
  tokens. The Mistral and Vercel keys you hold are unused —
  recommendations in §5.

---

## 2. What the new lane does

```powershell
python main.py longform <link>                    # whole source -> ONE 1920x1080 video
python main.py longform <link> --minutes 8        # ONE ~8-min episode, cut on a sentence end
python main.py longform <link> --minutes 8 --start 600
python main.py longform <link> --dry-run          # plan only, $0
python main.py longform <link> --no-subs          # skip the burn (kit keeps captions.srt)
```

Landscape sources scale+pad (nothing cropped); portrait sources get the
blurred-fill treatment in a 16:9 frame. Karaoke subs at the bottom,
YouTube chapter timestamps in the description (labels = the first words
spoken in each chapter), attribution kit with 3 thumbnail candidates.
Output: `longform/`, deliberately outside the Telegram phone queue
(50 MB bot cap).

Full user docs: `OPERATIONS.md` → "Long-form".

---

## 3. Token & quota usage — measured

### 3.1 The only call types the lane can make

| Call | When it fires | Calls on a 20-min source | Tokens in | Tokens out | Cached? |
|---|---|---|---|---|---|
| Groq Whisper | source has no harvestable YouTube captions | 1-2 chunked requests (20 audio-min) | — | — | transcript cache, shared with clip/parts |
| transcript fix (`clipfix`) | fresh Whisper transcript (on by default) | 4 (800 words/batch) | ~10,700 | ~120 | rides the transcript cache |
| sentence-end pass (`clipeos`) | `--minutes` cutting an unpunctuated transcript | 8 (400 words/batch) | ~10,650 | ~320 | own `.eos.json`, shared with parts |
| title / chapters / kit / thumbnails | always | 0 | 0 | 0 | — |

For scale: a single `generate` video is ~11 LLM requests / ~20k tokens
(CAPACITY.md audit). **One long-form episode cut from a captioned source
costs about half a generated video's tokens, once — and $0 forever
after, because the eos cache is keyed to the transcript.**

### 3.2 Scenario table (one 20-minute source, 3,000 words)

| Scenario | LLM calls | Tokens in/out | Groq audio | Gemini req | OpenRouter |
|---|---|---|---|---|---|
| captions exist, render whole | **0** | 0 / 0 | 0 min | 0 | 0 |
| captions exist, `--minutes 8` | 8 (eos) | ~10.7k / ~320 | 0 min | ≤8 (first chain link) | 0 |
| no captions, render whole | 4 (fix) | ~10.7k / ~120 | 20 min | ≤4 | 0 |
| no captions, `--minutes 8` | 12 (fix+eos) | ~21.4k / ~440 | 20 min | ≤12 | 0 |
| same source through `clip` instead | 6-16 | ~25-35k / ~1k | 20 min | ≤6 + ~1 vision/frame | 0 |

The clip lane costs more on the same source because of moment-picking
(~7.3k tokens per transcript window) + per-clip title polish + vision QC
frames. Long-form skips all three by design: there is nothing to pick,
and the source's own title is already proven.

### 3.3 What 3 long-forms/day (one per channel) does to each budget

Assume the worst mix: all three sources uncaptioned, all cut.

| Provider | Free-tier limit | Long-form draw | Share |
|---|---|---|---|
| Groq Whisper audio | 28,800 s/day (8 h), **org-level** | 3 × 20 min = 60 min | **12.5%** |
| Groq chat | ~14,400 req/day (see §4 caveat) | ≤36 | <0.3% |
| Gemini | ~1,000 req/day/key | ≤36 (only if Groq is dry) | ≤3.6% of one key |
| OpenRouter | 50 req/day on `:free` | 0 (last resort, chain order) | 0% |
| Pexels / Pixabay | 200/h, 20k/mo · ~100/min | 0 | 0% |
| ElevenLabs | 10k chars/mo per key | 0 | 0% |
| YouTube Data API | 10,000 units/day/key | 0 | 0% |
| Pollinations | paced, no hard cap | 0 | 0% |

**Conclusion: you can run long-form daily on every channel without
moving a single quota dial that matters.** The only pool it touches is
Whisper audio, and only for sources without captions — which the
captions-first harvest already minimizes.

---

## 4. Full audit — every API in the stack

Legend: ✅ healthy · ⚠️ watch · 💤 dormant by design · 🪦 dead ·
🫥 hidden (working but undocumented) · ➕ unused capacity you hold.

| # | API | Where used | Verdict | Notes / action |
|---|---|---|---|---|
| 1 | **Gemini** (text) | script, factcheck, editorial, scout, clipfix/clippick/clipeos, title polish | ✅ | Primary chain link, ~1k req/day/key. Saturates at US peak (503) — chain + EU mornings already handle it. Long-form adds ≤1% load. |
| 2 | **Gemini** (vision) | image QC, clip smart-crop subject tracking | ✅ | Cached per photo+query, circuit breaker, fails OPEN. Long-form makes **0** vision calls (nothing to pick). |
| 3 | **Groq chat** | LLM fallback link | ⚠️ | Sources conflict on the daily cap: our ledger says ~14,400/day, a Sep-11-2026 check says 1,000/day on the main chat models — **both agree limits are per organization, not per key**. Verify once in console.groq.com → Limits. Not urgent: Gemini takes the load first. |
| 4 | **Groq Whisper** | transcript fallback when no captions | ⚠️ **binding constraint** | 8 h audio/day, org-level, 25 MB/chunk (hence 25-min chunking). Long-form is a *lighter* user per output-minute than clips. Captions-first + shared cache already minimize it. |
| 5 | **OpenRouter** | last LLM resort before DeepSeek | ✅ | 50 req/day on `:free` — tiny by design, only burns during a multi-provider outage. |
| 6 | **DeepSeek** | LLM lane after OpenRouter | 🫥→✅ | In code + chain since 2026-09-21, verified live, **but invisible**: not in config.example.yaml. **Fixed in this commit** — documented with keys/model. |
| 7 | **Pollinations** (text) | offline-ish last text resort | ✅ | `openai-fast` only (mistral model removed upstream 2026-09-16). No hard cap. |
| 8 | **Pollinations** (images) | image fallback | ✅ | flux free, paced ~1/15 s anonymous. Untouched by long-form. |
| 9 | **Pexels** | primary stock images | ✅ | 200/h + 20k/mo. Generate-lane only. |
| 10 | **Pixabay** | stock fallback | 🫥→✅ | In code + `config.py` defaults (first fallback!), **undocumented in the example config** — your config.yaml likely has the old fallback list. **Fixed in this commit**: documented, and the example's `image_fallbacks` now matches the real default (`pixabay` first fallback). Add a key if you want the second stock lane. |
| 11 | **ElevenLabs** | premium voice (generate lane) | 💤 | Off unless keys set; `crew.premium_voices` caps daily use. Untouched. |
| 12 | **YouTube Data API v3** | `snap`, `yt` | ✅ | ~3-4 units/channel/day of 10,000. Long-form: 0 units. |
| 13 | **youtube-transcript-api** | captions harvest | ✅ | Keyless. Cloud IPs are blocked; your residential PC is fine — this is why the lane runs on your machine, not a server. |
| 14 | **yt-dlp** | source downloads | ✅ | Unmetered; bot-wall handled via cookies config. |
| 15 | **Telegram** | bot, pregen phone queue | ✅ | Bot API is free at our volume. 50 MB/file cap — long-form is excluded from the phone queue on purpose. |
| 16 | **Buffer + Cloudinary** | autopost | 💤 | Drafts by default. Note for later: Cloudinary free storage/bandwidth (25 credits/mo) would strain under daily 100-300 MB long-form uploads — long-form kits are manual-upload, keep it that way. |
| 17 | **Azure OpenAI** | optional paid lane | 💤 | Needs endpoint+deployment; spend-guarded ($1/day, $5/mo caps). Off. |
| 18 | **Sentry / Honeybadger** | crash reporting / dead-man switch | 💤 | Optional, off. |
| 19 | **HuggingFace** | was: image inference | 🪦 | Serverless inference retired — removed from the code in Sep 2026. Nothing to do. |
| 20 | **edge-tts** | free voice | ✅ | Not an API-key service; unaffected. |

**Overstressed?** No single API is over its head at your volumes. The
*heaviest* user per video is the `generate` lane (script + factcheck +
2 editorial + vision QC on every image) — and that's the lane your data
says to de-emphasize anyway (clips outperform generations). The scarcest
shared resource is Groq Whisper minutes; long-form spends those more
efficiently than any other lane.

**Left out?** DeepSeek + Pixabay (fixed, §4/6 and 10). `openpyxl` was
missing from requirements.txt (snap --export needs it) — fixed.

---

## 5. The keys you hold but the stack doesn't use

- **Mistral (La Plateforme).** Genuinely free "Experiment" tier, no
  card — but rate-limited to ~1-10 RPM depending on source (checked
  2026-09-30; sources disagree, the console is authoritative). The
  chain's failure mode is a Gemini 503-storm, where dozens of retries
  queue up; a 1-10 RPM lane can't absorb that. Gemini (1k/day) → Groq
  (14.4k/day) → OpenRouter (50/day) → DeepSeek → Pollinations already
  cover the storm with five separate pools. **Recommendation: keep it
  unwired.** If you ever want it, it's a ~20-line
  `OpenAICompatProvider` subclass (`api.mistral.ai/v1/chat/completions`)
  — say the word and I'll add it as a chain link after DeepSeek.
- **Vercel.** A hosting platform; nothing in this pipeline is a web app.
  No sensible use today. If you ever want a public stats dashboard over
  `out/stats/`, that's the one place it would fit.

---

## 6. Strategy notes for the long-form experiment (non-API, briefly)

- **8+ minutes matters**: mid-roll ads only serve on videos ≥8:00
  (if/when the channels monetize) — `--minutes 8` is the sensible
  default episode length, and it's what the OPERATIONS examples use.
- **Re-upload risk is higher on long-form than on Shorts clips**: the
  longer the verbatim stretch, the more it looks like a re-upload under
  YouTube's reused-content policy. The lane ships every mitigation the
  clip lane has (attribution kit, subtitle transformation, sentence-cut
  windows, chapters) — but the safest long-term play is eventually
  mixing in original commentary. Watch the first videos' Reach tab in
  Studio before scaling up.
- **The 0-view pattern you're seeing on Shorts** (4 in a row on a
  channel that hit 14k) is the collapse pattern from the earlier views
  research: check impressions in Studio — collapsed impressions means
  suppression (pause/fix), stable impressions + low CTR means packaging.

---

## 7. What shipped with this report

- `longform.py` — the lane (geometry, window planning, chapters, kit,
  render). `clipper.ingest_transcript` — the shared transcript pipeline,
  factored out for reuse (clip/parts untouched).
- `main.py` — `longform` command (batch-capable like clip/parts).
- `config.py` / `config.example.yaml` — `longform:` block; DeepSeek and
  Pixabay documented; example fallbacks match real defaults.
- `requirements.txt` — openpyxl added.
- `OPERATIONS.md` — Long-form section + LLM-budget table row.
- `test_smoke.py` — 131 tests (2 new: `longform`, `longform_lane`;
  9/9 mutation checks caught; 4 real end-to-end renders verified:
  landscape cut, whole, chapters, portrait-fit).


## 8. Update — one lane, two modes (2026-10-01, the merge)

A second long-form implementation of the same brief landed on main a day
after this report. Instead of picking a winner, the two were merged into
one lane; this section is the delta to everything above.

**The merged shape.** The whole/episode mode from this report stays the
default (0 LLM calls on a cached transcript). The second design became
`--top N`: the picker mines **chapters** (40-90 s complete stories, not
highlight flashes) from the cached transcript, an optional vision pass
(reuses the clip lane's frame QC, `--no-vision` skips it) drops dead
frames, and the survivors render into ONE countdown video — numbered
cards, best moment revealed last. Output `longform/<key8>_topN.mp4`,
kit with 0:00-first chapter timestamps and the source credit.

**What `--top N` costs** (measured on the merged code):

| situation | LLM calls | tokens in | notes |
|---|---|---|---|
| cached transcript, `--top N` | 1 per picker window | ~2.3k per window | window = 1400 words ≈ 9 min of speech, 120-word overlap |
| + vision QC on | ~1 per candidate chapter | small | same API the clip lane's frame check uses |
| fresh uncaptioned source | + Whisper audio-min + transcript fix | ~11k | identical to every other lane — the transcript is the cost |

A 20-minute source is 2-3 picker windows: **2-3 calls, ~5-7k tokens in**
— about one clip run. The boardroom lane shipped in the same merge
(`meeting stats|pick`): a full meeting measured **9 calls, ~2.3k tokens**
total. Neither moves the binding constraint (Groq Whisper audio-minutes);
transcripts stay cached across lanes.

**Stack changes worth knowing:** `ingest_transcript` (cache → captions →
Whisper → fix) is now shared by clip/parts/longform; the single-source
silent-failure fix (❌ line instead of a quiet exit 1) covers all three
batch lanes; the compilation assembly encodes each card to a tiny mp4
first and then walks one concat-demuxer playlist instead of a single
mega-filtergraph — the mega-encode OOMs a 2 GB machine at three
chapters, the playlist shape does not; compilation geometry is pinned to
the lane's landscape constants (the channel's portrait/landscape setting
is restored before assembly runs). Tests: **133/133** green; `--top 3`
verified end-to-end with real FFmpeg (1920x1080, stereo, card-first
countdown, chapters at 0:00/1:01/2:08).

The bottom line survives the merge unchanged: whole long-form is a
0-LLM lane on cached sources, the compilation is a clip-run-priced
editorial lane, and the daily quota question is still "how many fresh
audio-minutes did we transcribe today".

---

## 9. Note on the test counts above (2026-10-03)

§7 (131) and §8 (133/133) are point-in-time records of what shipped with
each change and are left as written. The suite has grown since: run
`python test_smoke.py` for the live count (144 as of 2026-10-04, all
green on CPython 3.11 and 3.13). Since the 138 note: the dead broll.py
went away with its test, and these joined — Pollinations-402 fail-fast,
the Whisper probe, `--probe --sample N`, and the stock/vision ledger
tags. Nothing else in this report changes — the quota accounting is
still measured against the same prompt builders, and the daily question
is still "how many fresh audio-minutes did we transcribe today".
