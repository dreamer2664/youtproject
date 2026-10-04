# OPERATIONS — the daily workflow

Everything the pipeline can do, in the order you actually use it.
(Setup: see QUICKSTART.md. Uploading: see UPLOAD-GUIDE.md.)

---

## The loop (three clips a day — one per channel)

The cadence that beat the floods: **one clip per channel per day** (three
channels = three `clip` runs), always around the same hour. More volume
never fixed a bad day — it just piles up more duds while the algorithm
reads the channel as a content farm.

```powershell
python main.py snap                  # numbers + day-over-day deltas + retitle flags
python main.py meeting stats         # optional: the boardroom reads them with you
python main.py clip --sheet          # per channel: clip the next source from the sheet
python main.py pregen --push         # park the day's clips on your phone
```

Sources come from the **source sheet** (below) — paste links once a week
and the lanes never search YouTube again. `meeting pick` is the
deliberate alternative: the boardroom argues over which queued source
deserves today's slot. The long-form lane is NOT part of the daily loop
yet — it's new and untried; see its section below.

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

## The source sheet (paste links, skip the searching)

The sheet is a tiny CSV — `sources/sheet.csv` — that opens in Excel and
stays hand-editable in notepad. You paste YouTube links; the lanes eat
them. One row per source, and you only fill the first column(s):

    url,note,added,status,result
    https://youtube.com/watch?v=...,deep dive on aqueducts
    https://youtu.be/...

(a bare link on its own line is fine — `added`/`status`/`result` are the
tool's columns).

```powershell
python main.py sheet                          # where things stand
python main.py sheet --add <link> "| my note" # queue from the terminal
python main.py clip --sheet                   # clip the next queued source
python main.py clip --sheet 3                 # ...or the next three
python main.py meeting pick                   # let the boardroom choose instead
```

Statuses, managed entirely by the tool: **queued** (new) → **picked**
(the boardroom chose it — its reason lands in `result`) → **clipped**
(the row remembers which channel did it) or **failed** (why it refused:
no captions and no Whisper key, dead link...). Rows are never deleted —
the sheet is also the history. `clip --sheet` takes the boardroom's
`picked` row first, then the oldest queued one, and marks the row after
the run — so channels run one after another never clip the same source.
The sheet is shared across all your channel configs; the boardroom sees
every queued row and argues about fit for the channel it's running on.

The old watchlist (`topics/sources.txt`, `URL | note` lines) still counts
as candidates — nothing you had there is lost; it just doesn't grow
anymore (`--add` writes to the sheet now).

## Clips (from any long video)

```powershell
python main.py clip <youtube link>          # a VOD (shortcut form)
python main.py clip --url <youtube link>     # same thing, explicit
python main.py clip --file C:\path\to.mp4    # a local file
python main.py clip --url A --url B --file C # a batch: any mix, any count
python main.py clip --sheet                   # the next queued source from the sheet
```

Downloads → transcript (shared cache → Groq Whisper first → keyless
YouTube captions if Whisper fails or returns nothing; mishears
auto-corrected and cached, so re-runs are free) → picks the strongest
moments → cuts on sentence boundaries, opens
on the hook → **landscape VODs keep their whole frame** (sharp, centered,
blurred background fill; `clip.crop_mode` switches to the old crop) →
upload kits in `clips/` (with TikTok + Reels captions per clip). Batch
runs isolate failures: one dead link doesn't kill the others. Costs ~15-17 API requests per source,
less than one generated video.

The ordered `ai.groq_api_keys` list is split by `ai.groq_transcription_percent`
(default 90%): the first share is Whisper-only, and the remaining keys are
reserved for the Groq text fallback after Gemini's primary/reserve models
and key pool. With two or more Groq keys the default split preserves at
least one text key; a single key cannot serve both roles and defaults to
Whisper. Existing cached transcripts are retained — clear the transcript
cache only if you want old sources re-transcribed under the new policy.

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
python main.py meeting pick --render      # ...and clip the winner after
python main.py meeting act                # the room DECIDES today's move itself
python main.py meeting act --dry-run      # preview its agenda, spend nothing
python main.py meeting memory             # every decision the board ever made
python main.py meeting last               # re-read the whole debate
python main.py meeting pick --dry-run     # room + agenda, nothing spent
```

Four seats — Strategist 🎯 (Gemini), Analyst 🔎 (DeepSeek), Producer 🎬
(Groq), Skeptic 🤨 (OpenRouter) — plus a chair that closes with
machine-checked decisions: a pick MUST name one of the candidates, a
stats review yields at most 5 concrete actions. Different providers per
seat means the room really disagrees; a seat without keys rides the
fallback chain (you lose personality, never the meeting).

**You see everything the room says.** Every turn is printed live while
the meeting runs, the Telegram summary carries each seat's last word
("The room:"), and the minutes file keeps the **full transcript** —
`meeting last` reprints the most recent one in the terminal. Nothing the
AIs tell each other is hidden.

Candidates come from the source sheet (every queued row) plus any `--url`
flags. `--render` turns a pick into a clip run: the winner's sheet row
moves to `clipped` (or `failed` if the render refuses). A meeting is ~10
short LLM calls — run it before the day's generation, not all day.

### `meeting act` — the board acts on its own

The autonomous lane. The agenda carries the channel numbers, a **3-day
momentum table** (which videos and channels actually gained views —
"ride what works"), the pending sources, and the board's previous
decisions. The room argues, then commits to exactly ONE action:

- **clip** one of the listed sources — rendered immediately, sheet row
  marked (`clipped`/`failed`);
- **generate** one video on a topic the board writes itself — the
  stock-footage generate lane (Pexels/Pixabay stills + edge-tts
  voiceover + karaoke subtitles), chosen from the trend data (e.g.
  psychological explainers outperforming animal clips → another
  psychological topic). The chair's topic is validated to be a real,
  specific topic, then rendered via the normal `generate` pipeline
  (its own ~5-6 LLM calls + ~21 images, NOT a clip's budget);
- **none** — a no-action day, with the reason on record.

Board-initiated renders are **exempt from the meeting's ~10-call
budget**: the generate pipeline has its own quota headroom (CAPACITY.md)
and runs after the meeting closes.

### Board memory

Every decision — picks, stats actions, act moves and their outcomes —
is logged to `work/board_memory.json` (capped at the last 100). Each
meeting's agenda automatically includes the last 6 entries as one-line
digest items, so the room builds on what it previously decided without
re-reading old transcripts (which would cost far too many tokens).
`meeting memory` shows the whole log; the full debates stay in
`out/meetings/*.md`.

## Parts (series splitter — mechanical, not editorial)

```powershell
python main.py parts <youtube link>            # ~60s episodes, header + subs
python main.py parts --file C:\path\to.mp4    # a local file
python main.py parts <link> --part-len 90      # 90-second episodes
python main.py parts <link> --dry-run          # windows only, no renders
```

Same ingest as clips (download once, transcript once — cached and shared
with the clip lane), but the cuts are mechanical: every `--part-len`
seconds, moved to a sentence end. `--dry-run` prints the windows without
rendering — but it still ingests the transcript, so on a source with no
YouTube captions it spends the Whisper audio-minutes once (cached; the
real run is then free). See the note under "Long-form".
How the lane finds sentence ends:
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

## Long-form (landscape videos — optional, not in the daily loop yet)

New and not part of the 3-clips-a-day cadence — try it on a source you
already clipped (the transcript is cached, so a whole re-render costs
**0 LLM calls**) and judge the retention yourself before it earns a
daily slot.

```powershell
python main.py longform <link>             # the whole source as ONE 1920x1080 video
python main.py longform <link> --minutes 8 # ONE ~8-min episode of a longer source
python main.py longform <link> --minutes 8 --start 600   # ...starting near 10:00
python main.py longform --file C:\path\to.mp4
python main.py longform <link> --dry-run   # window + chapters only, no render
```

Same ingest as clips and parts (download once, transcript once — the
cache is shared across lanes, so running `clip` then `longform` on the
same source costs one transcript total).

> **What `--dry-run` really costs.** It skips the render and every
> editorial LLM call, but it plans windows and chapters from the
> transcript, so it still ingests one. On a source with harvestable
> YouTube captions (or an already-cached transcript) that is genuinely
> free. On a **fresh uncaptioned source it spends the Groq Whisper
> audio-minutes** — the stack's binding quota — exactly once: the
> transcript lands in `work/clip_cache/`, so the real run afterwards
> costs 0 audio-minutes and 0 LLM calls. Dry runs are never billed in
> dollars, which is what the old "$0" wording meant.

What's different:

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
python main.py longform <link> --top 5 --dry-run     # the plan only, no render
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
`..._longform_5m00s.mp4` (an episode cut at 5:00). Costs: **0 LLM/audio
calls** when the transcript is already cached and you render whole; one
cached eos pass when cutting an unpunctuated transcript. An uncached
source uses Groq Whisper first (even when YouTube captions exist), with
YouTube captions as fallback, then the transcript-fix pass. Existing
cache entries are kept to avoid surprise re-transcription. Not in the
Telegram phone queue by design — long files are past Telegram's 50 MB
bot cap. See API-REPORT.md for the full accounting.

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

## The night batch (unattended runs)

When you run several channels, the per-video minutes stop being the problem —
sitting at the PC is. `nightbatch` is one command that does the day's work on
its own:

    python main.py nightbatch [--clips N] [--count M] [--seconds S]
                              [--style STYLE] [--image-provider ROUTE]
                              [--dry-run] [--fresh] [--force] [--no-push]

Order of work: clip the next queued sources from the sheet → generate M videos
from the backlog → park the new clips on Telegram (`pregen --push`) → send you
a Telegram report (steps, durations, new file counts, failures with the last
log line). Uploads stay manual; nothing here can post.

Robustness, because it is meant to run at 01:30 while you sleep:

- every step is its own subprocess with its own log under
  `work/nightbatch/logs/` — one crash can't kill the rest;
- the journal `work/nightbatch/<date>.json` is updated after EVERY step, so
  re-running the same day resumes: done steps are skipped, failed ones retried
  (`--fresh` redoes everything);
- a lock file refuses a second batch at the same time (a crashed run's lock
  goes stale after 6 h, or take it with `--force`);
- the report names failures with the failing log's last line, so you know
  whether it was quota, network or a dud source.

Schedule it (Windows, once):

    schtasks /Create /TN "youtproject night batch" /TR "\"%~dp0Night Batch.bat\"" /SC DAILY /ST 01:30 /F

Then open Task Scheduler → the task → tick **"Wake the computer to run this
task"** and **"Run task as soon as possible after a scheduled start is
missed"**. A laptop that sleeps plugged-in will wake, batch, and go back to
sleep. Check `work/nightbatch/console.log` for the raw output.

From the phone: `/night` starts it, `/night dry` previews the plan.

A sane starting cadence for ~8 channels (clips carry the views, per your own
numbers): `--clips 3 --count 1` ≈ 45–60 min of unattended work producing
~20–30 clips + 1 generated video a night.

## The phone night shift (`/go` — the whole night from bed)

The night batch assumes you are at the PC or that it is already running. This
one assumes the opposite: you text the bot from your phone right before
sleeping, and **the PC may already be off**.

What actually happens, in order:

1. you send **`/go`** (defaults: 3 clips, 0 videos, stats meeting, clip
   review, top 5). Modifiers: `/go 4 2`, `/go later`, `/go dry`,
   `/go status`, `fresh`, `nomeeting`, `noreview`;
2. if the bot is running, the job is written to
   `work/nightrun/request.json` FIRST and then executed — a shutdown mid-reply
   cannot lose it. If the PC is off, Telegram holds the message and the bot
   reads it at the next start (`/go later` is the same thing deliberately);
3. the overnight task runs the request: **meeting stats** (the board reads
   your channel numbers) → clip the queued sources → **meeting review** (the
   four seats rank every new clip on hook and overall quality and pick what
   to post, written to `work/nightbatch/review.json`) → generate videos →
   park new clips on Telegram;
4. the report arrives with a **"POST THESE TODAY"** block at the top, the
   stats summary, and what failed;
5. `wakeup` puts the PC back to hibernate, but only if nobody has touched
   the keyboard for 5 minutes (`--min-idle` to change, `--no-inbox` to skip
   the Telegram pass).

Scheduling it (Windows, once):

    schtasks /Create /TN "youtproject overnight" /TR "\"%~dp0Wake and Run.bat\"" /SC DAILY /ST 01:00 /F

Then Task Scheduler → the task → tick **"Wake the computer to run this
task"** and **"Run task as soon as possible after a scheduled start is
missed"**. What those two ticks buy you:

- *PC hibernating* → it wakes itself at 01:00, runs the shift, reports,
  hibernates again. This is the true overnight case.
- *PC fully shut down* → nothing can run, but nothing is lost: the `/go` sits
  in Telegram and in `request.json`, and the missed-start tick runs it at the
  next boot (turn the PC on and walk away — the shift starts by itself).

Honest limits, so nothing is a surprise in the morning: a machine that is
off does not execute code, and a machine without wake-from-sleep support (or
a laptop closed with "do nothing" lid settings) behaves like the shutdown
case. Both cases still produce a full night's work — just at the next
power-on instead of 01:00.

Manual run, same code path:

    python main.py wakeup [--sleep-after] [--min-idle 300] [--no-inbox] [--dry-run]
    python main.py nightbatch --if-requested     # no request = exits 0, does nothing
    python main.py meeting review [--since ISO] [--clip ID] [--top N] [--json-out PATH]

Request lifecycle: `pending` → `done` (with `failed_steps` and a note even
when steps failed, so the morning report is truthful), or left `pending` if
the machine dies mid-run — the next wake resumes from the journal instead of
redoing the day. `work/nightrun/wake.log` has the raw output of the task.
Uploads stay manual; no step in this pipeline can post.

## Orders & the desktop lane (the AI drives your browser)

This is the "type it like a person" layer. One sentence:

    python main.py order "get a link from the database, get 6 clips and post
                          them in 6 channels, and generate 2 videos for 2 channels"

What happens, in order: the parser reads the sentence and **prints the plan
first** (what it understood, the channel mapping, whether posting is allowed);
then it takes the next queued source from the sheet, clips it, generates the
videos, stages one upload packet per item under `work/post/<date>/<channel>/`,
and — only when `desktop.uploads: on` — drives your Chrome to upload each one
in Studio. `--plan-only` prints the plan and runs nothing. `--dry-run`
suppresses the browser post but still runs clip/generate/stage steps; the
configured providers may consume quota. For a no-work preview use
`--plan-only`. If the sentence asks for “viral” clips, the moment-picker
already requests the strongest standalone hooks/stories; `order` does not
create extra candidates and use the separate heuristic virality score to
filter them. From the phone it's `/order <sentence>`.

### The desktop agent (`desktop …`, `/desk …`)

    python main.py desktop setup        # once: install the browser engine
    Desktop Chrome.bat                  # Chromium browser with the debug port + its own profile
    python main.py desktop status      # connects too: ✅ connected / ❌ with the fix
    python main.py desktop go "open youtube studio and tell me how the newest video is doing"
    python main.py desktop go "…" --no-hands     # plan/screenshot only
    python main.py desktop shot        # no URL = the page already open
    python main.py desktop text | stop
    python main.py desktop channels [add "Name" <UC…|studio link> | remove X]

How the loop works: every step it takes a **snapshot** (visible elements with
indexes + the page text + a screenshot), asks the model for ONE next action
(`click 7`, `fill 3 "…"`, `scroll`, `wait`, `done`), executes it, screenshots,
repeats — then closes with a summary you get in Telegram.

**It learns.** A failed run costs one extra model call: "write one sentence:
what to do differently" → stored in `work/desktop/lessons.json` and injected
into every future attempt on that site. A successful run is recorded as a
**playbook** (the action path with element names, not indexes) and replayed
first next time — with **zero model calls** when it still works. Playbooks
that fail twice in a row are dropped.

**The rules that make it safe to leave alone:**

- `desktop.allowed_domains` is an allowlist (YouTube + Google by default);
  "goto anything else" aborts the run and is logged;
- clicking Publish / Schedule / Save is a **commit**: refused unless
  `desktop.uploads: on` (set it in `config.yaml` or `/desk uploads on`);
- delete / buy / cancel / unsubscribe are refused **always**, config or not;
- `--no-hands` (or `desktop go` with dry-run) never clicks, only looks;
- kill switch: `/desk stop` or Ctrl-C — checked between steps;
- everything is in `work/desktop/logs/*.jsonl` + `work/desktop/shots/<run>/`
  (kept 14 days), so any surprise can be reconstructed screenshot by step.

**Your real browser, not a robot one:** `Desktop Chrome.bat` starts **any
Chromium-based browser** — Opera GX (checked first), Edge, Chrome, Brave,
Vivaldi — with `--remote-debugging-port=9222` and its own profile
(`%USERPROFILE%\youtproject-desktop`). Log into your channels once in that
window; after that the agent clicks in the window you can watch. Close it any
time — nothing is lost. `desktop.backend: browser` switches to a private
headless Chromium for read-only work (no logins).

**Opera and the "new tab" landmine (playwright#21812):** Playwright asking a
CDP-connected Opera for a *new tab* can crash it. The lane therefore never
does: it drives the tab that is already open. Two consequences you will
notice: keep **at least one tab open** in that window (a browser with zero
tabs gets a clear "open one and try again" instead of being killed), and a
follow-up search **reuses the current tab** rather than spawning a new one.
`desktop status` ends with a live connection test, in three honest flavours:

| you see | it means | what to do |
|---|---|---|
| `✅ connected — <page>` | the agent is looking at your browser right now | nothing |
| `⚠️ your browser IS running … but a tab did not answer` | the browser is fine, one TAB is frozen/asleep/mid-reload (Studio is heavy — this is the page, not the tool) | click/refresh that tab, run it again; the message names the tab |
| `❌ could not reach your browser at …` | nothing is listening on the debug port | double-click `Desktop Chrome.bat` first |

`status` also prints `code : <commit> <date>` — the exact code your clone is
running. If a fix was announced with a newer commit than the one shown, you
have not pulled it yet.

Every page-touching call is time-boxed (attach 6s, read 8s, screenshot 15s),
so a sleeping tab gives you a sentence in seconds instead of a command that
hangs forever. Opera itself snoozes background tabs — if the Studio tab is in
the background for a long while, click it once before asking for work.

**Studio speaks your language:** the lane matches controls in English AND
Italian (`Avanti`, `Titolo`, `Pubblica`, `Non elencato`, plus the
language-independent `PUBLIC/PRIVATE/UNLISTED` attributes), so an Italian
Studio is driven exactly like an English one — including the publish gate.

If Opera ever misbehaves, `Desktop Chrome.bat msedge` runs the same lane on
Edge (already on every Windows machine).

**Panel ports, and why a double-click can land elsewhere:** `Start Panel.bat`
tries 8765 first (or `panel.port` from config.yaml), then the next free port
in the 8765..8775 range, and opens the window on the port it actually bound.
A panel of yours that is already running is *reused* instead of duplicated;
identification is by token (`/api/state` answers with a per-run token, and the
port file records it), so an older sibling project answering on the same port
is never mistaken for this panel. `python main.py panel --status` prints a
line per port — `THIS panel (yours)` / `held by another program` / `free` —
plus the URL to open. If the start fails under `pythonw` (no console) a
dialog says so instead of nothing happening.

**From the panel:** the Publish section (and the phone bot) drive the same
`order` lane. One sentence, three buttons: **Plan** (free), **Dry run**
(everything up to the publish click), **Run** (respects the uploads gate;
asks for confirmation when the gate is ON). The header chip shows
`uploads off / uploads ON` and the channel count at all times, so the page
can never quietly be one click away from publishing without saying so.

**Channels:** the lane reads two places — `desktop.channels:` in
config.yaml (durable) and `work/desktop/channels.json`, written by
`desktop channels add` (no YAML surgery; `/desk channels add` from the
phone). They merge, deduped by the UC id (config wins), and an order spreads
items across them round-robin. `desktop channels` lists what it can see and
where each entry came from.

**Honest limits:** Studio's markup changes and a step will fail sometimes —
that is exactly what the lesson/playbook loop is for (the next attempt
usually succeeds). And a publish click is irreversible once YouTube accepts
it; that is why it sits behind its own gate and never happens unless you said
`post` and allowed uploads.

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
python main.py keys --probe   # every Gemini key + the reserved Groq text keys
python main.py keys --month   # 30-day spend per provider, split by call tag
python main.py costs          # Azure spend (if ever configured)
```

The ledger counts everything with an origin tag — `script`, `clipfix`,
`clippick`, `vision`, `probe`, … — so `keys --month` answers "what were
the tokens FOR", including the agent's own diagnostic probes. Whisper-only
Groq keys are not chat-probed; they are validated during transcription.

- Gemini's free tier saturates at US peak hours — EU mornings are fast and
  quiet. The full Gemini primary/reserve pool runs before the reserved
  Groq text keys; the rest of the configured fallback chain stays active.
- ElevenLabs: 1 premium-voice video/day (`ai.premium_voices`), the rest use
  free edge-tts. Raise/lower the number in config.yaml.
- `snap` costs ~4 of 10,000 daily YouTube API units.

### LLM call budget (audited 2026-09-23 — every call site walked)

| Lane | LLM calls | Breakdown |
|---|---|---|
| `generate` | **5–6 / video** | script 1 · factcheck 1 · punch-up 1 · title polish 1 · decringe 1 · topic top-up 1 (only when the backlog dips). Hook fix, title fix, expand/tighten fire only on violations/word-budget misses — normally 0. |
| `clip` | **2 + 1/clip / source** | moment pick 1 · transcript fix 1 (`clip.transcript_fix`, on by default) · title polish 1 per clip written. |
| `longform` (whole / `--minutes`) | **0–12 / source** | whole render of a captioned or cached source: **0**. Cutting an unpunctuated transcript: one cached eos pass (8 calls on a 20-min source). A fresh uncaptioned source adds the transcript-fix pass (4 calls on 20 min). Chapters, thumbnails and the kit: 0. |
| `longform --top N` | **2–4 / source** | chapter pick 1 per transcript window (~1400 words ≈ 9 min of speech) + transcript fix 1 (a cached transcript pays the picker only). Vision QC adds ~1 call per candidate chapter (~3 frames each) unless `--no-vision`. |
| `scout` | **1 / run** | proposals in one call; interest evidence is free keyless Wikimedia pageviews. |
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
| `[image] attempt N/6 failed: HTTP 402` | Pollinations' anonymous image lane refusing (free tier exhausted / gated upstream). It retries, but each attempt also pays the 16 s pacer — ~45 s wasted per image | set a free `ai.pollinations_token` (auth.pollinations.ai), or better: a free `ai.pexels_api_key` / `pixabay.api_keys` so stock photos are primary and Pollinations is only the last fallback |
| `[image] pacing: next request in Ns` on every image, render takes 20-40 min | no Pexels/Pixabay key, so all ~21 images ride anonymous Pollinations (~1 req/15 s + ~50 s each) | add a Pexels key (200/h, 20k/mo, free, no card) — the single biggest speed win in the generate lane |
| `keys` shows a wall of Gemini rows and no total | expected with many keys; each is its own ~1,500/day pool | the `all keys  N / M requests today` row under the section is the aggregate; the header says how many pools you hold |
| `keys` shows less Groq audio than you actually spent | the voice-note lane used to be off the books | fixed — `voice.transcribe` now ledgeres with `tag=voicenote`; `keys --month` splits `whisper` (clip/parts/longform) from `voicenote` (bot) |

## What is automated vs yours

Automated: topic interest checks, title scoring + A/B picking, sentence-cut
editing, subject-following crops, transcript caching, crash recovery, quota
tracking, retitle flags, dead-key dropping, encoder fallbacks.

Yours: the uploads (~3 min each), the retitle/kill-list calls that need
retention data, and judgment. The machine proposes; you decide.
