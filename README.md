# youtproject — free AI video generator (no audit needed, ever)

Makes documentary-style videos with AI and prepares everything for upload.
**Everything here is free, and no audit or verification can ever be required —
because there are no upload API calls anywhere in this code: it cannot call the
YouTube upload API, ever.** (The optional `yt` / `snap` commands make
read-only public-stats calls — no OAuth, no upload scope.) You upload the
finished files yourself in about 3 minutes per video.

There is also an optional desktop lane: with `desktop.uploads: on` the AI can
drive *your own browser* to Studio and click through the upload — that is UI
automation on your machine, not an API call, so the audit story above still
holds. It is off by default and nothing is ever staged as a public post without
the word `post` in your order.

```
Gemini script -> edge-tts voice -> Pollinations images -> FFmpeg -> YOU upload
   (free key)      (free, no key)      (free, no key)     (free)   (youtube.com)
                                              + karaoke subtitles burned in
```

Each video gets an upload kit with the file, thumbnail, title, description,
tags, captions file, and a step-by-step `CHECKLIST.md` for that exact video.

> **Just want the commands?** `CHEATSHEET.md` is every command, copy-paste
> ready — setup, connecting your channels, the daily loop, every lane.

---

## Why this project exists (the audit problem, in plain words)

YouTube's rule for API uploads:

> All videos uploaded via `videos.insert` from **unverified API projects created
> after 28 July 2020** are restricted to **private viewing mode**.

Worse, that lock is **permanent**: a video uploaded through an unaudited API
project cannot be made public afterwards — not via the API, not in YouTube
Studio — and there is no appeal. You would have to re-upload every video.

Lifting the lock requires passing YouTube's Compliance Audit: a free form, read
by a human, with no guaranteed approval and no published timeline. Personal and
hobby projects get rejected often.

This project sidesteps the whole thing: **no upload API calls means no
private lock, no OAuth dance, no audit form, nothing to reject.** The price is
three minutes of manual uploading per video — which also happens to be where
you tick YouTube's AI-disclosure box, set scheduling, and do all the things the
API makes awkward anyway. (If you turn on the optional desktop lane below, the *clicking* is automatic;
the API audit question still never applies, because it never calls the API.)

---

## Setup

### Windows (PowerShell)

```powershell
cd $HOME
git clone https://github.com/dreamer2664/youtproject.git
cd youtproject
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

Re-run the last line once in a fresh window after it installs FFmpeg, since
PATH changes need a new window. Then:

```powershell
.\venv\Scripts\Activate.ps1
notepad config.yaml
```

> **Python version note.** If `pip install` fails on very new Python, install
> Python 3.12 from python.org — `setup.ps1` prefers it automatically.

> If `Activate.ps1` is blocked by execution policy:
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`

### Linux / macOS

```bash
sudo apt install ffmpeg          # or: brew install ffmpeg
git clone https://github.com/dreamer2664/youtproject.git
cd youtproject
bash setup.sh
source venv/bin/activate
```

### Gemini API key (free, no card)

<https://aistudio.google.com/apikey>

Open `config.yaml` and paste it in:

```yaml
ai:
  provider: "gemini"
  gemini_api_key: "AQ.your-key-here"
```

`config.yaml` is git-ignored, so it will not be committed. No key? Set
`provider: "template"` and it runs fully offline with lower-quality scripts.

### Check it

```bash
python main.py preflight          # checks FFmpeg, libraries, key, folders
python main.py preflight --live   # also validates the Gemini key (one tiny free call)
```

---

## Use

```bash
python main.py generate                 # make one video (config defaults)
python main.py generate --count 3       # make three
python main.py generate --topic "..."   # override the channel topic for one run
python main.py generate --seconds 45    # target ~45 seconds
python main.py generate --format portrait --seconds 45   # vertical Short
python main.py generate --images-per-scene 3             # denser cuts
python main.py generate --style stickman  # whiteboard stickman explainer look
python main.py longform <link>     # ONE 16:9 video: whole source, episode cut, or --top N countdown
python main.py sheet --add <link>  # queue a source (or paste rows in sources/sheet.csv)
python main.py clip --sheet        # clip the next queued source from the sheet
python main.py meeting stats       # the AI boardroom reviews your numbers
python main.py meeting pick --render   # ...or picks + clips today's source
python main.py meeting act         # the board decides today's move ITSELF:
                                   #   clip a queued source, or generate a
                                   #   video on a topic it writes
python main.py meeting memory      # every decision the board ever made
python main.py meeting last        # re-read the full boardroom transcript
python main.py generate --style cartoon   # flat 2D vector toon look
python main.py generate --no-subs       # skip subtitles for one run
python main.py batch --topics topics.txt  # render a whole list overnight
python main.py batch --count 7          # 7 fresh backlog topics, auto-picked
python main.py batch --dry-run --count 3  # preview a batch: topics + time, $0
python main.py topics --topup       # refill the topic backlog with AI ideas
python main.py schedule --per-day 2 # render 2 videos/day unattended (Ctrl+C stops)
python main.py schedule --dry-run   # preview the daily plan, $0
python main.py bot                         # render videos from your phone via Telegram
python main.py jarvis "make 3 videos and schedule them 4h apart tomorrow"  # channel manager
python main.py autopost                     # post newest video via Buffer (draft)
python main.py stats --days 30                # views/reach/eng per channel (Buffer)
python main.py yt "@handle"                   # channel subs/views (1 quota unit)
python main.py yt --search "roman engineering"  # top niche Shorts (~100 units)
python main.py crew --days 3 --per-day 4      # autonomous mission (drafts; add --live)
python main.py queue                    # what's in the queue
python main.py package                  # build upload kits for generated videos
python main.py published <id> <url>     # record a manual upload's URL
python main.py voices --lang it-        # list Italian voiceover voices
```

### Picking topics (nothing repeats)

`channel.topic` in `config.yaml` is your niche (e.g. `"forgotten medieval
engineering"`). Every auto run pops one **fresh story from the backlog**
(`topics/backlog.txt`, auto-refilled by AI), skipping anything the channel
already covered — exact or near-duplicate. Same video twice is structurally
impossible. For a one-off: `python main.py generate --topic "..."` (explicit
always wins, with a warning if it's a repeat). The output tells you the
source (`from backlog` vs `--topic override` vs channel fallback).

### The daily flow

1. `python main.py generate` → finished video lands in `out/`.
2. `python main.py package` → upload kit lands in `upload/<id>/`.
3. Open `upload/<id>/CHECKLIST.md`, drag `video.mp4` into
   <https://youtube.com/upload>, follow the checklist (~3 min).
4. `python main.py published <id> https://youtu.be/...` → queue stays accurate.

One video a day, at the same hour, beats five at once. See `UPLOAD-GUIDE.md`
for the full walkthrough: the AI-disclosure box, captions, audience settings,
thumbnails, Shorts, and what not to do on a new channel.

### The panel (click instead of typing)

Double-click **`Start Panel.bat`** (Windows) — or run `python main.py panel`.
A local page opens (127.0.0.1 only, no terminal) with buttons for the moves
you actually make: **Generate** (image route `stock`/`ai`/`free`, style and
length as clicks), **Clip** and **Parts** (pick a link off your list or take
the next queued one), the **boardroom** (run it, dry-run it, read minutes,
or chat with any seat), plus snapshots, keys, queue, kits, errors.
Every button runs exactly the command you would have typed, streams its
output live, and one job runs at a time. No button here can post anything —
uploading stays manual, by design. If something else already holds port
8765 (an older local project, a dev server), the panel quietly takes the
next free port and opens *that* window — the launcher recognises its own
server by probing `/api/state`, so it can never show you another program.

```
python main.py panel                  # the same thing from a terminal
python main.py panel --port 9000      # if 8765 is taken
python main.py panel --host 0.0.0.0   # reachable from your phone on the LAN (read the warning)
```

`sources/sheet.csv` is the links list the panel manages; the board and the
clip lanes already read it, so nothing is duplicated anywhere.

### The night batch (unattended)

With several channels, the day's generating does not need *you* — it needs
your PC to be on. One command runs the whole shift and reports to Telegram:

```
python main.py nightbatch                    # 2 clips + 2 videos + park + report
python main.py nightbatch --clips 3 --count 1 --seconds 45 --image-provider stock
python main.py nightbatch --dry-run          # show the plan, spend nothing
```

It clips the next queued sources from the sheet (~10 min each, up to 10 clips
per source), generates videos from the topic backlog, parks new clips on
Telegram (`pregen --push`), and messages you the report. Every step runs in
its own process with its own log (`work/nightbatch/logs/`), the journal
(`work/nightbatch/<date>.json`) is written after each step, so re-running the
same day resumes instead of redoing — a crash costs one step, not the night.
**Nothing in it can post**: no Buffer, no autopost, no publish path.

Windows: double-click **`Night Batch.bat`**, or schedule it once and let it
run while you sleep:

```
schtasks /Create /TN "youtproject night batch" /TR "\"%~dp0Night Batch.bat\"" /SC DAILY /ST 01:30 /F
```

(then in Task Scheduler tick *"Wake the computer to run this task"* if the PC
sleeps). From your phone: **`/night`** starts it, **`/night dry`** previews
the plan.

### The whole night from your phone (`/go`)

Send one message from bed and sleep. `/go` runs the full shift: a boardroom
**stats meeting first**, then the clip + generate work, then a second meeting
that **ranks the finished clips by hook and overall quality and picks what to
post today**:

```
/go              # the default night: 3 clips, both meetings, top 5 picks
/go 4 2          # 4 clips + 2 generated videos
/go later        # queue it and stop — it runs at the next wake/boot
/go dry          # print the plan, spend nothing
/go status       # what is queued / what happened last night
```

Add `fresh` (redo everything), `nomeeting` or `noreview` to any of them.

**The honest part about a PC that is off.** A powered-off PC executes nothing,
so `/go` works by never needing it to be on *at the moment you send it*:
Telegram holds the message, and the bot writes the job to
`work/nightrun/request.json` before anything else — a shutdown cannot lose it.
The **Wake and Run** task then picks it up when the machine is available:

- PC hibernating → the task's *"wake the computer"* tick wakes it at 01:00,
  does the whole shift, sends you the report, and hibernates again;
- PC fully shut down → the task's *"run as soon as possible after a missed
  start"* tick runs the shift at the next boot instead (turn the PC on in the
  morning, the night's work is done and waiting).

Setup (Windows, once each):

```
schtasks /Create /TN "youtproject overnight" /TR "\"%~dp0Wake and Run.bat\"" /SC DAILY /ST 01:00 /F
```

then in Task Scheduler → that task → tick **"Wake the computer to run this
task"** and **"Run task as soon as possible after a scheduled start is
missed"**. `Start Bot.bat` runs the bot while the PC is on; `Wake and Run.bat`
is the overnight worker (its output goes to `work/nightrun/wake.log`). A
hibernated PC is what makes this truly overnight — a full shutdown can only
run it at the next power-on, which is exactly what the second tick covers.

### Orders — hand the AI your browser

One sentence, spoken like a person:

```
python main.py order "get a link from the database, get 6 clips and post
                      them in 6 channels, and generate 2 videos for 2 channels"
```

The plan prints first (what it read, which channel gets what, whether posting
is allowed), then it works: clip the queued source → generate → map items onto
your channels → stage an upload packet per item in `work/post/<date>/` → and,
only if `desktop.uploads: on`, drive your Chrome to upload them in Studio.
`--plan-only` shows the plan and stops; `--dry-run` does everything except the
final publish click.

The driver itself:

```
python main.py desktop setup              # one-time: the browser engine
Desktop Chrome.bat                        # starts your Chromium browser with the debug port
python main.py desktop status             # backend, uploads gate, channels, what it learned
python main.py desktop go "open youtube studio and tell me how the newest video is doing"
python main.py desktop go "…" --no-hands  # plan and screenshot, click nothing
python main.py desktop shot               # screenshot of the current page → Telegram (/desk shot)
python main.py browser data @handle       # public subscriber/video numbers, no API key, no quota
python main.py browser shot <url>         # screenshot of any allowed page
```

`/desk stop` (or Ctrl-C) is the kill switch. Every step is logged in
`work/desktop/logs/` and screenshotted in `work/desktop/shots/`.

Three rules are built in, not optional:

- **Domain allowlist** — the agent can only open sites you listed in
  `desktop.allowed_domains` (YouTube and Google by default). Anything else
  aborts the run.
- **The commit gate** — clicking Publish/Schedule/Save needs
  `desktop.uploads: on`. Delete/buy/unsubscribe are refused always, whatever
  the config says.
- **It learns** — a failed run writes one sentence ("lesson") into
  `work/desktop/lessons.json` and injects it into the next attempt; a
  successful run becomes a playbook that is replayed first next time, with no
  model calls at all.

From the phone: `/order <sentence>`, `/desk status|shot|log|stop|uploads on`,
`/look <url>`.

### Phone control (Telegram, free)

Text the bot a topic from your phone, get back the finished video:

1. Message `@BotFather` → `/newbot` → copy the token.
2. Message `@userinfobot` → copy your numeric user id.
3. Put both in `config.yaml` (`telegram.bot_token` / `telegram.owner_id`),
   set `telegram.enabled: true`.
4. `python main.py bot` — leave your PC on; the bot polls from here, so no
   firewall or port setup is needed.

Plain text = a topic to render. `/queue` shows progress, `/send <id>`
re-sends a finished video. `/jarvis <task>` hands the channel manager a job
("make 3 videos and schedule them 4 hours apart tomorrow") — it reports back
here as it goes. `/crew <mission>` launches the autonomous team for days
(`/log` replays their chatter, `/stop` halts). Voice notes work too — talk, and it transcribes ("jarvis, …"
routes to the channel manager, anything else becomes a render topic). One thing
runs at a time; extras queue up. A rendered video
arrives as a file (bit-exact, ready to upload) plus the caption and hashtags,
and the full YouTube kit is also built on your PC. Only your account can use
the bot.

---

## Configuration

Everything lives in `config.yaml`. The important keys:

| Key | What it does |
|---|---|
| `channel.topic` | Your niche. Drives every script and image. Be specific. |
| `channel.tone` | How the narration sounds. |
| `channel.voice` | Free neural voice. `python main.py voices` lists them. |
| `channel.speech_rate` | Narration speed. `+40%` is brisk TikTok pacing (~180 wpm). |
| `channel.target_seconds` | Default target length. 90–180 suits a new channel. |
| `channel.category_id` | Prefilled into each video's checklist. |
| `video.format` | `landscape` (1920x1080) or `portrait` (1080x1920 Shorts). |
| `video.images_per_scene` | Pictures per narrated scene, 1–6. Higher = denser cuts. |
| `video.style` | Art direction: `photoreal` (default), `cartoon`, or `stickman` whiteboard explainer. |
| `subtitles.enabled` | Karaoke captions (word highlight) burned in. Leave on. |
| `disclosure.append_to_description` | Adds the AI-disclosure footer to descriptions. Leave on. |
| `ai.provider` | `gemini` (good scripts) or `template` (keyless fallback). |
| `ai.gemini_model` | `gemini-3.8-flash` (re-surveyed 2026-09-16, newest-first fallbacks). |

Every run setting has a CLI override too (`--seconds`, `--format`,
`--images-per-scene`, `--style`, `--no-subs`, `--topic`), so you can mix
Shorts, long-form and art styles without touching the config.

---

## Files

| File | Purpose |
|---|---|
| `main.py` | CLI |
| `config.py` | Config loading, env overrides |
| `scriptgen.py` | Gemini + offline template script writers |
| `images.py` | Image generation: Pollinations → Gemini fallback |
| `voiceover.py` | edge-tts voiceover + word timings |
| `subtitles.py` | Karaoke ASS + SRT writer, burn-in styling |
| `assembler.py` | FFmpeg: sub-segments, burn-in, mux, thumbnails |
| `package.py` | Upload kits + checklists + TikTok/Reels captions |
| `jobqueue.py` | `state.json` job tracking |
| `bot.py` | Telegram phone control (polls, renders, delivers) |
| `nightreq.py` | the `/go` request file (`work/nightrun/request.json`) — survives a shutdown |
| `wakeup.py` | wake/boot worker: check Telegram once, run a pending `/go`, hibernate again |
| `desktop.py` | the desktop agent: drives a real browser (snapshot, click, read, screenshot, gates, lessons, playbooks) |
| `orders.py` | plain-language orders → clips + videos + channel mapping + staged packets |
| `Desktop Chrome.bat` | starts a Chromium browser (Opera GX, Edge, Chrome, Brave, Vivaldi) with the debug port so the agent can drive the window you see |
| `panel.py` / `panel.html` | Click-only control panel (`Start Panel.bat`, or `main.py panel`) |
| `autopost.py` | Buffer autopost: video hosting + TikTok/YouTube/IG drafts or scheduled posts |
| `jarvis.py` | Channel manager brain: tasks → render + schedule + report |
| `openai_compat.py` | Shared base for OpenAI-style chat lanes (rotation + fallback) |
| `groq.py` / `openrouter.py` | Free LLM lanes (primary / backup) |
| `editorial.py` | Punch-up (retention) + decringe (taste veto) passes |
| `analytics.py` | Buffer stats: posts + per-post metrics roll-up (read-only) |
| `voice.py` | Voice notes: Telegram download + Groq Whisper transcription |
| `youtube.py` | YouTube Data API: video/channel stats + Shorts niche search |
| `crew.py` | Autonomous team: Manager/Scout/Maker/Herald missions (`crew`, `/crew`) |
| `longform.py` | Long-form lane: whole source / episode cut / `--top N` countdown, all 16:9 |
| `meeting.py` | The boardroom: 4 AI seats + chair, machine-checked decisions |
| `sheet.py` | The source sheet: paste links, lanes clip them, rows keep history |
| `test_smoke.py` | Offline self-tests: `python test_smoke.py` (no keys/network needed) |
| `UPLOAD-GUIDE.md` | The manual-upload walkthrough |
| `hooks/pre-commit` | Blocks credentials from being committed |

> `jobqueue.py` is deliberately **not** named `queue.py` — that would shadow
> Python's stdlib `queue` module.

---

## Security

There is no OAuth here at all, and no upload scope exists to leak. Every
secret is an optional free-tier key you may never need: the Gemini key, the
other LLM lanes (Groq / OpenRouter / DeepSeek), ElevenLabs, Pexels / Pixabay,
the read-only YouTube Data API keys (`yt` / `snap` stats only), Telegram,
Buffer, Cloudinary. They all live in `config.yaml`, which is git-ignored, and
`hooks/pre-commit` blocks commits containing API keys or tokens. The setup
scripts install that guard automatically — this repo is public, so that guard
is not optional.

---

## Not recommended

- **Uploading via the YouTube API from an unaudited project.** Videos get
  permanently locked to private. That is the trap this project exists to avoid.
- **Selenium / browser automation for uploading.** Violates the YouTube ToS,
  breaks whenever Studio's HTML changes, and risks your channel. Three minutes
  of manual uploading is not worth automating at that price.
- **Dumping dozens of videos on day one.** New channels that publish one solid
  video a day do better than ones that flood. The tool can generate faster than
  you should publish.
