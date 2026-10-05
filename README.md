# youtproject — free video generation and local YouTube pipeline

Makes documentary-style videos and YouTube upload kits. The standalone
`generate` → `package` workflow still supports manual Studio upload. The
optional local `order` pipeline can also connect generation/clipping/parts,
staging, QA, and the existing browser-controlled Studio uploader. It uses no
YouTube Data API upload endpoint; `yt` / `snap` make read-only public-data calls.

The uploader is `desktop.py` / `orders.py` controlling the owner's local
browser—not Buffer and not a direct API publisher. The owner has affirmed
prior written YouTube permission covering this browser automation; that is an
owner attestation, not independently reviewed here. Keep use within its scope.
The browser commit remains off unless `desktop.uploads: on`, the specific job
is confirmed for an exact channel/visibility, and required per-video answers
are supplied. No live upload was performed during implementation or testing.
YouTube's [Terms of Service](https://www.youtube.com/t/terms) still apply.

```
Gemini script -> edge-tts voice -> Pollinations images -> FFmpeg -> YOU upload
   (free key)      (free, no key)      (free, no key)     (free)   (youtube.com)
                                              + karaoke subtitles burned in
```

Each video gets a YouTube-only upload kit with the video, available thumbnail,
title, description, tags, optional captions, and a per-video `CHECKLIST.md`.
The kit also reminds you to review facts, rights, and AI disclosure before
manual upload.

> **Just want the commands?** `CHEATSHEET.md` is every command, copy-paste
> ready — setup, connecting your channels, the daily loop, every lane.

---

## API audit vs. manual Studio upload

The YouTube Data API restriction applies to uploads through `videos.insert`
from unverified API projects created after 28 July 2020. The audit is at the
**API-project level**, not a separate audit for each channel. YouTube's Help
says creators of videos locked private by an unverified API service can
re-upload through the YouTube app/site; that is the legitimate path used here.

This project prepares local files and does not call `videos.insert`. Manual
YouTube Studio upload avoids this particular API-project audit restriction;
it is not a way to disguise an API upload or a blanket exemption for other
automation. Separate channel-level feature/identity checks may still apply to
some Studio features. The optional desktop lane is automated browser access
and must be used only where permitted; it is not part of the manual workflow.

Official references: [Videos: insert](https://developers.google.com/youtube/v3/docs/videos/insert),
[Videos locked as private](https://support.google.com/youtube/answer/7300965?hl=en),
and [API Services Developer Policies](https://developers.google.com/youtube/terms/developer-policies).

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

`config.yaml` is git-ignored, so it will not be committed. Gemini rate limits
are per project—not per API key—and keys in the same project share quota.
A non-empty `GEMINI_API_KEYS` environment variable replaces the YAML key list;
`python main.py keys` reports the effective source/count without revealing key
contents. No key? Set `provider: "template"` and it runs fully offline with
lower-quality scripts.

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
python main.py batch --topics topics.txt  # render a whole list while the PC is on
python main.py batch --count 7          # 7 fresh backlog topics, auto-picked
python main.py batch --dry-run --count 3  # preview a batch: topics + time, $0
python main.py topics --topup       # refill the topic backlog with AI ideas
python main.py schedule --per-day 2 # render while this PC/process stays on
python main.py schedule --dry-run   # preview the daily plan, $0
python main.py bot                         # render videos from your phone via Telegram
python main.py yt "@handle"                   # channel subs/views (1 quota unit)
python main.py yt --search "roman engineering"  # 1 unit; separate 100-calls/day project bucket
python main.py queue                    # what's in the queue
python main.py package                  # build upload kits for generated videos
python main.py published <id> <url>     # record a manual upload's URL
python main.py voices --lang it-        # list Italian voiceover voices
```

The default standalone workflow stops at a local YouTube kit for **your
manual upload**. The separate `order` command/Publish panel section now offer
a local-PC end-to-end route, using the existing gated Studio browser lane.
Buffer and other legacy publisher paths are not part of this pipeline. The
pipeline was tested offline only; no provider call, browser action, or live
upload was used in this review.

### Implemented local order pipeline

`python main.py order` and the Panel's **Publish** section share a durable
local-PC pipeline for generation, YouTube-source clipping, splitting into
parts, staging, QA, and the existing Studio browser upload lane. Details,
known limits, and rollout status are in
[`AUTOMATION-PIPELINE-PLAN.md`](AUTOMATION-PIPELINE-PLAN.md).

Each job is journaled under `work/pipeline/jobs/<job-id>/`; staged packets and
reports are under `work/post/<date>/<job-id>/`. Jobs are resumable with
`--resume`. A browser commit is blocked unless `desktop.uploads: on`, an exact
channel/visibility/action is confirmed, source-use rights are attested for
clips/parts, and altered-content plus audience answers are set. The Panel
previews the plan and requires typing its exact one-use confirmation phrase.
An uncertain post is never blindly retried: inspect that channel in Studio,
then use `pipeline-reconcile` to record the verified result.

The order pipeline follows configured providers by default: Groq Whisper is
preferred for transcription, with public YouTube captions as fallback; the
configured AI provider (Gemini by default) handles clip selection and script
work. Provider quotas and account billing rules still apply. Add `--free-only`
to `python main.py order` to explicitly force the keyless Pollinations/template
and captions route. Standalone Panel Generate/Clip/Parts actions remain
free-only; the Publish section uses the order pipeline's configured route.

This is **not** a live-service validation: the offline smoke suite passed, but
no real render, provider request, Studio browser session, or live upload was
performed. A fully shut-down PC still cannot run this local worker. Phone-led
or PC-off processing remains deferred, and hosted/free-runtime feasibility is
open for a later phase.

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

Choose a cadence that you can sustain and review. See `UPLOAD-GUIDE.md`
for the manual Studio workflow, source/rights checks, captions, audience
settings, thumbnail availability, and the per-video AI-use disclosure criteria.

### The panel (click instead of typing)

Double-click **`Start Panel.bat`** (Windows) — or run `python main.py panel`.
A local page opens (127.0.0.1 only, no terminal) with **Generate**, **Clip**,
and **Parts** actions, plus the boardroom, snapshots, keys, queue, kits, and
errors. Generate offers free stock photos or Pollinations images; these three
standalone actions always pass `--free-only`. The **Publish** section previews
and runs the durable local `order` pipeline. It shows exact channels and
visibility and requires typing the per-job confirmation phrase before a
publish request can proceed. Missing consent/answers or `desktop.uploads: off`
means stage-only. The panel itself has no uploader: it runs the CLI, which
uses the existing `desktop.py` Studio browser lane. Every button streams its
output live, and one job runs at a time. On Windows, FFmpeg/ffprobe runs
without opening a separate console for every probe/render; progress and
reports stay in the panel log. If something else already holds port
8765 (an older local project, a dev server) the panel takes the next free
port and opens *that* window, saying so in the console. Every panel run gets
its own identity token, so the launcher can tell one of your projects from
another that merely looks similar — it never shows you a stranger's window.
If a double-click ever seems to do nothing, the launcher now says why in a
dialog instead of dying silently, and this tells you the whole story:

```
python main.py panel --status     # who owns each port, and the URL to open
```

To always use one port (e.g. 9000), set `panel: {port: 9000}` in
config.yaml — then `Start Panel.bat` binds 9000 and opens
`http://127.0.0.1:9000/` for you.

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

### Phone requests and PC power

Telegram commands can queue work, but the local bot and render pipeline only
run while the PC is powered on and the bot process is active. A fully shut
down PC cannot poll Telegram or render in the background. `/go later` persists
a request for the next time the worker is started; it does not wake a fully
shut down PC or complete work overnight. A Windows wake task works only when
the machine is in a supported sleep/hibernate state and has been configured
for that purpose. This project does not assume the PC stays on while you
sleep. For this phase, run the pipeline on the local PC while it is on; the
existing gated Studio lane can upload after exact per-job confirmation.
Phone-triggered/PC-off processing remains deferred.

A genuinely hosted worker would be a separate deployment decision: it would
need an always-available free host, secrets management, and a fresh privacy /
reliability review. No remote runner or paid service is enabled here.

### Local order pipeline and Studio upload

The order pipeline is for an intentional local-PC job; it does not use Buffer
or a direct YouTube API uploader. For example, a stage-only job:

```
python main.py order "generate 1 video about Roman aqueducts and stage it" \
  --topic "How Roman aqueducts carried water" --no-default-channels
```

For an actual publish request, select the exact configured channel and supply
the required per-job answers. The disclosure/audience values below are only
illustrative: choose them after reviewing the finished video and intended
audience. The CLI prints the full plan and then requires an exact typed
phrase; do not run this example unless you intend that action:

```
python main.py order "generate 1 video about Roman aqueducts and post it" \
  --topic "How Roman aqueducts carried water" \
  --channel "Exact channel name" --visibility unlisted \
  --altered-content no --made-for-kids no --confirm-publish
```

For source-derived clips/parts, provide a YouTube URL or local file/source
sheet entry and add `--rights-confirmed` only after verifying your rights.
Direct source URLs are YouTube-only. The pipeline validates the completed
video and upload metadata, stages one packet per item/channel, and uses the
existing `desktop.py` Studio browser only after `desktop.uploads: on` and the
exact job confirmation. `--plan-only` runs nothing. `--dry-run` still renders
and stages but suppresses the browser action; free-service quotas may still
apply. Free-only runs use Pollinations/template text, free stock/Pollinations
images, edge-tts, and public YouTube captions—Groq Whisper is skipped.

Jobs and reports are journaled locally. Resume a saved job with
`python main.py order "resume" --resume JOB_ID`. If a browser click may have
reached Studio but success could not be verified, first inspect the exact
channel manually, then reconcile:

```
python main.py pipeline-reconcile --job-id JOB_ID \
  --outcome committed
```

When more than one upload is unresolved, add `--key KEY`. Reconciliation
requires an interactive exact confirmation and must reflect what you actually
verified in Studio; it is never an automatic retry. The scheduling time is not
part of this pipeline. No live upload was run in the offline implementation
tests. Clip counts are requested maxima: if output is short or media QA fails,
the order is reported incomplete and posting is blocked.

The driver itself:

```
python main.py desktop setup              # one-time: the browser engine
Desktop Chrome.bat                        # starts your Chromium browser with the debug port
python main.py desktop status             # ...and CONNECTS to the browser to prove it is ready
python main.py desktop go "open youtube studio and tell me how the newest video is doing"
python main.py desktop go "…" --no-hands  # plan and screenshot, click nothing
python main.py desktop shot               # screenshot of the page already open → Telegram (/desk shot)
python main.py desktop text               # read the page already open (no URL = wherever you are)
# status ends with ✅ connected / ⚠️ a tab froze (the browser is fine) / ❌ start the browser
# a sleeping or frozen tab is reported in seconds — never a hang
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

Channels the lane can post to (config.yaml `desktop.channels:` or the
runtime store — no YAML editing needed):

```
python main.py desktop channels                            # list them
python main.py desktop channels add "MicroFeed-0" UCWKpOEGAYSgCUzJL-0fO4iQ
python main.py desktop channels remove "MicroFeed-0"       # runtime ones
```

Phone-triggered/PC-off processing is deferred for this phase. The Telegram bot
and local worker still require the PC to be on; the phone command is not a
replacement for the Panel/CLI's exact per-job publish confirmation. Do not use
`/desk uploads on` as a substitute for that confirmation.

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
runs at a time; extras queue up. A rendered video arrives as a file plus YouTube title/description/tags for
manual upload; the YouTube kit is also built on the PC. The bot only responds
while its polling process is running, and only your account can use it.

---

## Configuration

Everything lives in `config.yaml`. The important keys:

| Key | What it does |
|---|---|
| `channel.topic` | Your niche. Drives every script and image. Be specific. |
| `channel.tone` | How the narration sounds. |
| `channel.voice` | Free neural voice. `python main.py voices` lists them. |
| `channel.speech_rate` | Narration speed; tune it by listening to the finished export. |
| `channel.target_seconds` | Default target length. 90–180 suits a new channel. |
| `channel.category_id` | Prefilled into each video's checklist. |
| `video.format` | `landscape` (1920x1080) or `portrait` (1080x1920 Shorts). |
| `video.images_per_scene` | Pictures per narrated scene, 1–6. Higher = denser cuts. |
| `video.style` | Art direction: `photoreal` (default), `cartoon`, or `stickman` whiteboard explainer. |
| `subtitles.enabled` | Karaoke captions (word highlight) burned in. Leave on. |
| `disclosure.append_to_description` | Adds an optional AI-tools transparency note; Studio's AI-use answer is conditional on the actual video. |
| `ai.provider` | `gemini` (good scripts) or `template` (keyless fallback). |
| `ai.gemini_model` | `gemini-3.8-flash` (re-surveyed 2026-09-16, newest-first fallbacks). |

Every run setting has a CLI override too (`--seconds`, `--format`,
`--images-per-scene`, `--style`, `--no-subs`, `--topic`), so you can mix
Shorts, long-form and art styles without touching the config.

---

## Files

| File | Purpose |
|---|---|
| `main.py` | CLI and local order-pipeline entry point |
| `config.py` | Config loading, env overrides |
| `scriptgen.py` | Gemini + offline template script writers |
| `images.py` | Image generation: Pollinations → Gemini fallback |
| `voiceover.py` | edge-tts voiceover + word timings |
| `subtitles.py` | Karaoke ASS + SRT writer, burn-in styling |
| `assembler.py` | FFmpeg: sub-segments, burn-in, mux, thumbnails |
| `package.py` | YouTube-only manual upload kits, checklists, and retry-safe replacement |
| `jobqueue.py` | `state.json` job tracking |
| `bot.py` | Telegram phone control (polls, renders, delivers) |
| `nightreq.py` | the `/go` request file (`work/nightrun/request.json`) — survives a shutdown |
| `wakeup.py` | wake/boot worker: check Telegram once, run a pending `/go`, hibernate again |
| `desktop.py` | local Studio browser control (snapshot, click, read, screenshot, gated upload, lessons, playbooks) |
| `pipeline.py` | durable local generation/clip/parts → QA → staging → confirmed Studio upload; resume/reconcile |
| `orders.py` | order parsing, exact channel resolution, staging manifests, and compatibility wrapper |
| `Desktop Chrome.bat` | starts a Chromium browser (Opera GX, Edge, Chrome, Brave, Vivaldi) with the debug port so the agent can drive the window you see |
| `panel.py` / `panel.html` | Click-only control panel (`Start Panel.bat`, or `main.py panel`) |
| `autopost.py` | Optional legacy Buffer adapter; YouTube is the only enabled destination, not used by manual upload |
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

There is no YouTube OAuth client or API upload scope in this code. The
optional desktop lane uses an existing signed-in browser profile instead; that
is still automated access, not a policy exemption. API keys for the Gemini,
other LLM lanes (Groq / OpenRouter / DeepSeek), ElevenLabs, Pexels / Pixabay,
read-only YouTube Data API (`yt` / `snap`), Telegram, Buffer, and Cloudinary
live in git-ignored `config.yaml`; `hooks/pre-commit` blocks commits containing
keys or tokens. The setup scripts install that guard automatically. YouTube
Data API quota, retention, and cross-channel metric policy boundaries are
documented in `API-REPORT.md` §11.

---

## Scope and cautions

- This project does not use the YouTube Data API upload endpoint. Unverified
  API projects can have `videos.insert` uploads restricted to private; the
  official Help describes re-uploading through the YouTube app/site as an
  option. The compliance audit is project-level, not per channel.
- Manual Studio upload is the supported workflow here. The separate desktop
  automation lane is off by default and is not enabled or exercised by this
  review; check current Terms/permissions before using it.
- The tool can render faster than a person can review. Set a cadence you can
  sustain while checking sources, rights, the finished export, and metadata.
