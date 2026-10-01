# OPERATIONS — the daily workflow

Everything the pipeline can do, in the order you actually use it.
(Setup: see QUICKSTART.md. Uploading: see UPLOAD-GUIDE.md.)

---

## The loop (one video a day per channel)

The cadence that beat the floods: **one generation per channel per day**,
always around the same hour. More volume never fixed a bad day — it just
piles up more duds while the algorithm reads the channel as a content
farm.

```powershell
python main.py snap                  # numbers + day-over-day deltas + retitle flags
python main.py meeting stats         # optional: the boardroom reads them with you
python main.py clip <today's source> # one clip generation (any lane)
python main.py pregen --push         # park it on your phone
```

Then upload by hand (CHECKLIST.md inside each kit walks you through it,
~3 min per video). `snap` flags any video ≥2 days old under 50 views with
`RETITLE?` — exactly once per video. Retitle it in Studio from the kit's
`TITLE.txt` ideas, or leave it. The old `generate`/`package` flow still
works for the original-video lane.

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
python main.py topics --audit   # score every topic for scroll-potential
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

Downloads → transcript (YouTube captions first when they exist — 0 Groq
audio-minutes — else Whisper; mishears auto-corrected, cached — re-runs
are free) → picks the strongest moments → cuts on sentence boundaries, opens
on the hook → **landscape VODs keep their whole frame** (sharp, centered,
blurred background fill; `clip.crop_mode` switches to the old crop) →
upload kits in `clips/` (with TikTok + Reels captions per clip). Batch
runs isolate failures: one dead link doesn't kill the others. Costs ~15-17 API requests per source,
less than one generated video.

Captions default to the bottom (the old centered default sat on the
subject whenever the whole frame was kept). Still not right for a
source? Move them per run with
`--sub-pos top|middle|bottom`, or `--sub-pos auto` (free frame analysis
picks the calmest third), or set it once in `subtitles.position` —
plus `font` / `font_scale` / `outline` in the same block.
See the style before rendering anything:
`python main.py subpreview <any .mp4>` — a picker window with the exact
YAML to paste (writes top/middle/bottom PNGs instead when headless).
Swear words are masked in captions by default (fuck -> f*ck,
`subtitles.mask_profanity`) — whole words only, timings untouched.

More from one source: `--max-clips 12` (default 10). For a 16:9
"Top N moments" countdown of the best chapters, see the long-form
lane's `--top N` below — it lives there, not on clip.

### Short sources: kept whole (clip and parts)

Anything up to 3 minutes (`whole.under_seconds: 180`, YouTube's Shorts
cap) isn't cut: both `clip` and `parts` turn it into ONE vertical short:
subtitles at the bottom, the video's title on top for 4 seconds, then it
fades (`parts.header_seconds`). No "Part 1", plain kit title. The clip
lane also skips moment-picking (fewer API calls) and a thin transcript is
fine; the output is a normal `clips/…_clip_01.mp4`, so `pregen` parks it
like any clip.

```bash
python main.py clip <link>              # auto: whole if <= 3:00
python main.py clip <link> --no-whole   # mine moments anyway
python main.py clip <link> --whole      # one piece even if longer (warns past 3:00)
python main.py clip <link> --half       # two halves, split at a sentence end
```

`--half` (clip and parts) splits at the best sentence end near the
middle (within 5–15 s, more if needed), so the halves are near-equal and
nobody is cut off mid-word. The halves get "Title / Part 1" and "Part 2"
headers and "Title — Part X" kit titles. It warns when a half runs past 3:00.

## The boardroom (AI management meetings)

```powershell
python main.py meeting stats              # agents review the channel numbers
python main.py meeting pick               # agents argue over today's source
python main.py meeting pick --add <link>  # seed the watchlist (no meeting)
python main.py meeting pick --render      # ...and clip the winner after
python main.py meeting pick --dry-run     # room + agenda, nothing spent
```

Four seats — Strategist 🎯 (Gemini), Analyst 🔎 (DeepSeek), Producer 🎬
(Groq), Skeptic 🤨 (OpenRouter) — plus a chair that closes with
machine-checked decisions: a pick MUST name one of the candidates, a
stats review yields at most 5 concrete actions. Different providers per
seat means the room really disagrees; a seat without keys rides the
fallback chain (you lose personality, never the meeting). Minutes land
in `out/meetings/YYYY-MM-DD-<kind>.md`, a short summary in your Telegram
DM, and `--render` turns a pick into a clip run and pops the winner from
`topics/sources.txt` (the watchlist: one `URL | note` per line). A
meeting is ~10 short LLM calls — run it before the day's generation, not
all day.

## Parts (series splitter — mechanical, not editorial)

```powershell
python main.py parts <youtube link>            # ~60s episodes, header + subs
python main.py parts --file C:\path\to.mp4    # a local file
python main.py parts <link> --part-len 90      # 90-second episodes
python main.py parts <link> --dry-run          # windows only, $0
```

Same ingest as clips (download once, transcript once — cached and shared
with the clip lane), but the cuts are mechanical: every `--part-len`
seconds, moved to a sentence end. How the lane finds sentence ends:
YouTube auto-captions have no punctuation, so one small LLM pass marks
where sentences end (numbered words in, positions out, cached; Whisper
transcripts skip it), and FFmpeg `silencedetect` finds the real pauses
in the audio. Preference order: a sentence end inside a pause (cut in
the middle of the silence), then any sentence end (±10 s, then ±20 s),
then a plain pause, then a word boundary. Each run prints what it got,
e.g. `4 cut(s): 3 sentence end in a pause, 1 sentence end`; "fallback"
there means that stretch had no sentence end nearby.
Each part opens with a top header ("<title> / Part X", 4 s, then it
fades — free, it rides the subtitle burn; `parts.header_seconds: 0`
keeps it up the whole part), karaoke subs at the bottom, and its own kit
with `captions.srt` + `part.ass` included. Single-part videos show just
the title (no "Part 1"). Long titles word-wrap to 3 lines (`parts.header_max_lines`)
instead of truncating; past that the lane asks the LLM chain to shorten
the title once per video (existing keys, validation-gated, never fatal —
`parts.shorten_titles: false` keeps pure truncation). ~2 API calls per
source, then pure FFmpeg. Post parts in order, one per day — the numbering
only works as a sequence.

## Long-form (landscape videos — the clip lane's 16:9 sibling)

```powershell
python main.py longform <link>             # the whole source as ONE 1920x1080 video
python main.py longform <link> --minutes 8 # ONE ~8-min episode of a longer source
python main.py longform <link> --minutes 8 --start 600   # ...starting near 10:00
python main.py longform --file C:\path\to.mp4
python main.py longform <link> --dry-run   # window + chapters only, $0
```

Same ingest as clips and parts (download once, transcript once — the
cache is shared across lanes, so running `clip` then `longform` on the
same source costs one transcript total). What's different:

- **16:9 always.** Landscape sources scale+pad to 1920x1080 — nothing is
  cropped. Portrait sources get the whole frame over a blurred landscape
  fill (the mirror of the clip lane's vertical fit).
- **Karaoke subs at the bottom**, landscape sizing. `--sub-pos` moves
  them, `--no-subs` skips the burn (the kit still ships `captions.srt`).
- **Chapters** in DESCRIPTION.txt: marks at real sentence ends/pauses,
  labelled with the first words actually spoken there — no API call.
  YouTube engages chapters at 3+ marks starting 0:00; fewer is written
  as none. `--no-chapters` or `longform.chapters: false` skips.
- **Episode cuts** (`--minutes`) close on a sentence end in a pause
  (same tier order as parts); `--start` snaps to a boundary too. A
  whole source over `longform.max_minutes` (20) warns but renders.
- **Kit**: TITLE/DESCRIPTION/CREDIT/captions.srt/longform.ass +
  THUMB_1-3.jpg (subtitle-free frames for the thumbnail). No
  tiktok/reels captions — it isn't vertical.

### `--top N` — the countdown compilation

```powershell
python main.py longform <link> --top 5          # best 5 chapters, best LAST
python main.py longform <link> --top 5 --no-vision   # skip the frame QC
python main.py longform <link> --top 5 --dry-run     # the plan only, $0
```

The editorial sibling of the whole/episode mode: the picker reads the
cached transcript in windows and looks for **chapters** (40-90 s complete
stories, not 20-45 s highlight flashes), a vision pass drops low-quality
frames, and the survivors render 1920x1080 with bottom subtitles and get
assembled into ONE countdown — numbered card → chapter, worst first,
the best moment revealed as #1. The kit carries YouTube **chapter
timestamps** (first line exactly `0:00`) and the source credit; past
8:00 of chapter time the checklist reminds you about mid-roll placement.
A landscape source keeps its whole frame, a portrait one rides over a
blurred fill. `longform.moments / min_len / max_len / target_seconds`
tune it. Output: `longform/<key8>_top{N}.mp4`. Costs ~2-4 LLM calls on
a cached transcript (one per picker window, ~2.3k tokens in each) —
still cheaper than a clip run. One edited, credited digest per source
is the reused-content-safe way to make long-form — do not also
re-upload the raw source.

Output: `longform/<key8>_longform.mp4` (whole) or
`..._longform_5m00s.mp4` (an episode cut at 5:00). Costs: **0 LLM
calls** when the transcript is cached or harvested from captions and
you render whole; one cached eos pass when cutting an unpunctuated
transcript; the transcript-fix pass only on a fresh Whisper source.
Not in the Telegram phone queue by design — long files are past
Telegram's 50 MB bot cap. See API-REPORT.md for the full accounting.

## Phone queue (pregen — clips on your phone, PC on or off)

```powershell
python main.py pregen --push      # score new clips, park them on Telegram
python main.py pregen --best      # today's winner + scorecard (no sends)
python main.py pregen              # list parked + unpushed (no sends)
python main.py pregen --push --dry-run   # predict the push, send nothing
python main.py pregen --baseline        # ignore the backlog, start fresh (no sends)
```

One-time setup: create a PRIVATE Telegram channel, add your bot as admin,
forward any channel post to @userinfobot to learn the channel id, and set
`telegram.channel_id` in config.yaml. Then run `--push` after your renders
(or schedule it): each finished clip/parts file is scored 0–100 (hook 30,
topic 25, length 20, pace 15, title 10 — same hook/title rules the
retention lane renders by, so question-hooks score LOW on purpose), sent to
the channel with its scorecard, and the day's winner is announced with a
copy-paste caption. `pregen.json` remembers what's parked — re-runs only
send new clips, and a crash mid-push never double-sends. First day with a
big backlog? `--baseline` marks everything on disk ignored (no sends,
invisible to `/clips`/`/today`) so the queue starts from your next render;
re-rendering an old source changes its file fingerprint, which
automatically makes it pending again.

Phone side: with the PC **off**, open the channel and download — the files
live on Telegram's servers. With the PC on and `python main.py bot`
running, text the bot: `/today` (best pick + video), `/clips` (parked
list), `/clip <id>` (pull one here). Delivery uses Telegram's own copy
(`file_id`) first, so re-sends work even after the PC file is deleted.
Files over 48 MB are skipped with a reason (Telegram caps bot files at
50 MB).

## Channel stats (snap — all your channels, one command)

```bash
python main.py snap --add @yourhandle     # once per channel (channel link or any video link works too)
python main.py snap                       # stats for every tracked channel
python main.py snap --channel facts       # just one channel (name fragment)
python main.py snap --recent 20 --sort views
python main.py snap --export              # + out/stats/stats-DATE.xlsx and CSVs
python main.py snap --list                # tracked channels (offline)
python main.py snap --remove "old name"   # stop tracking (history stays)
```

Per channel: subs and total views (with change since the previous
snapshot), then the newest videos with AGE, LEN, VIEWS, Δ (views gained
since the last snap), LIKES, COMMENTS, ENG ((likes+comments)/views) and
PACE (views/hour for the first 48h, then views/day). Videos 2+ days old
with under 50 views are flagged `RETITLE?` once. On the phone (bot
running): `/stats` or `/stats <name>`: one message per channel.

Run it once a day, roughly at the same hour, so the Δ numbers read as
"per day". Cost is ~3 of your 10,000 daily quota units per channel.
Public data can't show impressions, CTR or retention (that's Studio);
YouTube rounds public subscriber counts (3 digits), so small sub gains
can show as no change. Open the `.xlsx`, not the CSV, in Excel: with
Italian regional settings a comma-CSV opens as one column.

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
| `longform` | **0–2 / source** | whole render of a captioned/cached source: 0. Cutting an unpunctuated transcript: one cached eos pass (8 calls on a 20-min source). Fresh Whisper source: + transcript fix (4 calls on 20 min). Chapters, thumbnails, kit: 0. |
| `scout` | **1 / run** | proposals in one call; interest evidence is free keyless Wikimedia pageviews. |
| `longform` | **2-4 / source** | chapter pick 1/window + transcript fix 1 (cached transcript: pick only). Vision QC ~3 frames/chapter. |
| `meeting` | **~10 / meeting** | 4 seats × 2 rounds + chair; short turns, one agenda in every prompt. Minutes + Telegram delivery are free. |
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
| mid-download `HTTP 403` | cookies never took effect (wrong path?) | read the `cookies:` line each download prints; `Test-Path` your `clip.cookies_file` |
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
