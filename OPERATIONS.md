# OPERATIONS — the daily workflow

Everything the pipeline can do, in the order you actually use it.
(Setup: see QUICKSTART.md. Uploading: see UPLOAD-GUIDE.md.)

---

## The loop (about 15 minutes a day)

```powershell
python main.py snap        # numbers + day-over-day deltas + retitle flags
python main.py generate    # today's videos (2-3 per channel)
python main.py package     # build upload kits -> upload/<id>/
```

Then upload by hand from each kit (CHECKLIST.md inside walks you through it,
~3 min per video). `snap` flags any video ≥2 days old under 50 views with
`RETITLE?` — exactly once per video. Retitle it in Studio from the kit's
`TITLE.txt` ideas, or leave it.

**First time on a new channel:** `python main.py snap --add <any video link
from that channel>` — once per channel, ever.

## A/B titles (two channels)

Every kit with a strong enough title contest contains `title-b.txt`:

1. Upload to **channel 1** with `title.txt` (A).
2. Upload the **same video** to **channel 2** with `title-b.txt` (B).
3. After 72h: `python main.py snap` — the deltas name the winner.
4. Keep the winner's title on both channels.

## Feeding the topic backlog (once or twice a week)

```powershell
python main.py scout             # LLM proposes, Wikipedia interest validates
python main.py scout --count 12  # more topics per run
python main.py topics            # see the backlog
```

A topic only enters the backlog if its Wikipedia article got ≥500 views in
the last week (bar: `topics.scout_min_weekly_views`). The old
`python main.py topics --topup` still works (no interest check, LLM only).

## Clips (from any long video)

```powershell
python main.py clip <youtube link>          # a VOD (shortcut form)
python main.py clip --url <youtube link>     # same thing, explicit
python main.py clip --file C:\path\to.mp4    # a local file
python main.py clip --url A --url B --file C # a batch: any mix, any count
```

Downloads → transcript (mishears auto-corrected, cached — re-runs are
free) → picks the strongest moments → cuts on sentence boundaries, opens
on the hook → **landscape VODs keep their whole frame** (sharp, centered,
blurred background fill; `clip.crop_mode` switches to the old crop) →
upload kits in `clips/` (with TikTok + Reels captions per clip). Batch
runs isolate failures: one dead link doesn't kill the others. Costs ~15-17 API requests per source,
less than one generated video.

Subtitles covering the main event? Move them per run with
`--sub-pos top|middle|bottom`, or `--sub-pos auto` (free frame analysis
picks the calmest third), or set it once in `subtitles.position` —
plus `font` / `font_scale` / `outline` in the same block.
See the style before rendering anything:
`python main.py subpreview <any .mp4>` — a picker window with the exact
YAML to paste (writes top/middle/bottom PNGs instead when headless).
Swear words are masked in captions by default (fuck -> f*ck,
`subtitles.mask_profanity`) — whole words only, timings untouched.

More from one source: `--max-clips 12` (default 10), and
`--top 5` builds ONE extra "Top 5 moments" countdown video from the
run's best clips — numbered cards, best moment revealed last, no
extra API calls. Its kit lands next to the clip kits.

## Decision rules (what the data says to do)

| Signal | Rule | Where |
|---|---|---|
| <50 views after 2 days | retitle once | automated (`snap`) |
| APV <70% twice | debug the script, not the upload | Studio → retention |
| APV <40% | topic goes on the kill-list | Studio → retention |
| A/B winner after 72h | keep winner on both channels | `snap` deltas |

Retention (APV) is only in Studio Analytics — the public API can't see it.

## Money & quota

Everything runs on free tiers. Check the tanks any time:

```powershell
python main.py keys           # per-key usage vs every free-tier limit
python main.py keys --probe   # live-check EVERY lane, per key (each check logged)
python main.py keys --month   # 30-day spend per provider, split by call tag
python main.py costs          # Azure spend (if ever configured)
```

The ledger counts everything with an origin tag — `script`, `clipfix`,
`clippick`, `vision`, `probe`, … — so `keys --month` answers "what were
the tokens FOR", including the agent's own diagnostic probes.

- Gemini's free tier saturates at US peak hours — EU mornings are fast and
  quiet. The fallback chain (groq → openrouter → template) carries you
  either way.
- ElevenLabs: 1 premium-voice video/day (`ai.premium_voices`), the rest use
  free edge-tts. Raise/lower the number in config.yaml.
- `snap` costs ~4 of 10,000 daily YouTube API units.

### LLM call budget (audited 2026-09-23 — every call site walked)

| Lane | LLM calls | Breakdown |
|---|---|---|
| `generate` | **5–6 / video** | script 1 · factcheck 1 · punch-up 1 · title polish 1 · decringe 1 · topic top-up 1 (only when the backlog dips). Hook fix, title fix, expand/tighten fire only on violations/word-budget misses — normally 0. |
| `clip` | **2 + 1/clip / source** | moment pick 1 · transcript fix 1 (`clip.transcript_fix`, on by default) · title polish 1 per clip written. |
| `scout` | **1 / run** | proposals in one call; interest evidence is free keyless Wikimedia pageviews. |
| `snap` · voice | **0** | YouTube API units only; edge-tts. |
| image QC | ~1 Gemini **vision** call per candidate photo | cached by photo+query, circuit breaker, fails OPEN on outage (storm-time renders ship un-QC'd images). Live catch 2026-09-24: rejected Tokyo Tower photos posing as Eiffel. |
| `snap` · images | **0** | YouTube API units; Pexels/Pixabay fetch only. |

Verdict: nothing left to cut — the diet already happened (premium voices
off by default, template fallback, single-call scout, heuristic QC). The
only waste during a Gemini 429-storm is time, not tokens: the chain burns
~2 min on retries before groq takes over. Generate in EU mornings, or
`--no-gemini` when a storm is known. A full backlog (8+ topics) also skips
the top-up call.

## When something goes wrong

| Symptom | Meaning | Fix |
|---|---|---|
| `final mux failed (exit 3221225477)` | Intel QSV driver crashed (now auto-retries on CPU) | update Intel graphics driver, or `encoder: cpu` |
| voice reads markup / odd pacing | old edge-tts (pre-7.2) | `pip install -r requirements.txt` |
| `Sign in to confirm you're not a bot` | YouTube distrusts the network | `clip.cookies_browser: "firefox"` in config.yaml |
| HTTP 503 storms from Gemini | free-tier saturation at US peak | just wait / EU morning; fallbacks engage automatically |
| `[queue] state.json was unreadable` | a crash interrupted a queue write | already auto-quarantined; nothing to do |
| job shows `reclaimed` | that render was killed mid-run (timeout, Ctrl-C, power cut) | nothing to do — the next generate put its topic back on the backlog |
| `elevenlabs key ... is dead` | a revoked key | remove it from `ai.elevenlabs_api_keys` |

## What is automated vs yours

Automated: topic interest checks, title scoring + A/B picking, sentence-cut
editing, subject-following crops, transcript caching, crash recovery, quota
tracking, retitle flags, dead-key dropping, encoder fallbacks.

Yours: the uploads (~3 min each), the retitle/kill-list calls that need
retention data, and judgment. The machine proposes; you decide.
