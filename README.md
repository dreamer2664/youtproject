# youtproject — free AI video generator (no audit needed, ever)

Makes documentary-style videos with AI and prepares everything for upload.
**Everything here is free, and no audit or verification can ever be required —
because this tool never talks to the YouTube API at all.** You upload the
finished files yourself in about 3 minutes per video.

```
Gemini script -> edge-tts voice -> Pollinations images -> FFmpeg -> YOU upload
   (free key)      (free, no key)      (free, no key)     (free)   (youtube.com)
                                              + karaoke subtitles burned in
```

Each video gets an upload kit with the file, thumbnail, title, description,
tags, captions file, and a step-by-step `CHECKLIST.md` for that exact video.

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

This project sidesteps the whole thing: **no API calls means no private lock,
no quota, no OAuth dance, no audit form, nothing to reject.** The price is
three minutes of manual uploading per video — which also happens to be where
you tick YouTube's AI-disclosure box, set scheduling, and do all the things the
API makes awkward anyway.

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
here as it goes. Voice notes work too — talk, and it transcribes ("jarvis, …"
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
| `ai.gemini_model` | `gemini-flash-latest` tracks the current model automatically. |

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
| `autopost.py` | Buffer autopost: video hosting + TikTok/YouTube/IG drafts or scheduled posts |
| `jarvis.py` | Channel manager brain: tasks → render + schedule + report |
| `openai_compat.py` | Shared base for OpenAI-style chat lanes (rotation + fallback) |
| `groq.py` / `openrouter.py` | Free LLM lanes (primary / backup) |
| `broll.py` | Pexels stock B-roll fetcher (fetch-only) |
| `editorial.py` | Punch-up (retention) + decringe (taste veto) passes |
| `analytics.py` | Buffer stats: posts + per-post metrics roll-up (read-only) |
| `voice.py` | Voice notes: Telegram download + Groq Whisper transcription |
| `youtube.py` | YouTube Data API: video/channel stats + Shorts niche search |
| `crew.py` | Autonomous team: Manager/Scout/Maker/Herald missions (`crew`, `/crew`) |
| `test_smoke.py` | Offline self-tests: `python test_smoke.py` (no keys/network needed) |
| `UPLOAD-GUIDE.md` | The manual-upload walkthrough |
| `hooks/pre-commit` | Blocks credentials from being committed |

> `jobqueue.py` is deliberately **not** named `queue.py` — that would shadow
> Python's stdlib `queue` module.

---

## Security

There is no OAuth here at all. The only secrets in the project are your Gemini
key and (if you enable phone control) your Telegram bot token, both living in
`config.yaml`, which is git-ignored, and `hooks/pre-commit` blocks
commits containing API keys or tokens. The setup scripts install that guard
automatically — this repo is public, so that guard is not optional.

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
