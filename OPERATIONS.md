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
```

Downloads → transcript (mishears auto-corrected, cached — re-runs are
free) → picks the strongest moments → cuts on sentence boundaries, opens
on the hook → **landscape VODs keep their whole frame** (sharp, centered,
blurred background fill; `clip.crop_mode` switches to the old crop) →
upload kits in `clips/`. Costs ~15-17 API requests per source,
less than one generated video.

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
python main.py keys     # per-key usage vs every free-tier limit
python main.py costs    # Azure spend (if ever configured)
```

- Gemini's free tier saturates at US peak hours — EU mornings are fast and
  quiet. The fallback chain (groq → openrouter → template) carries you
  either way.
- ElevenLabs: 1 premium-voice video/day (`ai.premium_voices`), the rest use
  free edge-tts. Raise/lower the number in config.yaml.
- `snap` costs ~4 of 10,000 daily YouTube API units.

## When something goes wrong

| Symptom | Meaning | Fix |
|---|---|---|
| `final mux failed (exit 3221225477)` | Intel QSV driver crashed (now auto-retries on CPU) | update Intel graphics driver, or `encoder: cpu` |
| voice reads markup / odd pacing | old edge-tts (pre-7.2) | `pip install -r requirements.txt` |
| `Sign in to confirm you're not a bot` | YouTube distrusts the network | `clip.cookies_browser: "firefox"` in config.yaml |
| HTTP 503 storms from Gemini | free-tier saturation at US peak | just wait / EU morning; fallbacks engage automatically |
| `[queue] state.json was unreadable` | a crash interrupted a queue write | already auto-quarantined; nothing to do |
| `elevenlabs key ... is dead` | a revoked key | remove it from `ai.elevenlabs_api_keys` |

## What is automated vs yours

Automated: topic interest checks, title scoring + A/B picking, sentence-cut
editing, subject-following crops, transcript caching, crash recovery, quota
tracking, retitle flags, dead-key dropping, encoder fallbacks.

Yours: the uploads (~3 min each), the retitle/kill-list calls that need
retention data, and judgment. The machine proposes; you decide.
