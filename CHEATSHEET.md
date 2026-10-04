# CHEAT SHEET — every command, copy-paste ready

Windows PowerShell assumed (you're on Windows). On mac/Linux the commands
are identical; only venv activation differs (noted in §1). All commands
run from the project folder. `python` = inside your venv.

Want fewer words? `python main.py --help` lists everything; every command
also takes `--help` (e.g. `python main.py clip --help`).

---

## 1. One-time setup (new PC)

```powershell
git clone https://github.com/dreamer2664/youtproject.git
cd youtproject
.\setup.ps1                        # venv + all libraries (mac/linux: ./setup.sh)
python main.py preflight           # check FFmpeg, libraries, folders
python main.py preflight --live    # + validate the Gemini key (one tiny free call)
python main.py keys --probe        # every Gemini key + the reserved Groq text keys
python main.py keys --advice          # key lanes, the Groq account split, the
                                           # OpenRouter $10 move, ranked upgrades
```

Keys live in `config.yaml` (copy `config.example.yaml` to start — it's
commented line by line):

| what | where in config.yaml | get it from |
|---|---|---|
| Gemini (scripts/titles/agents) | `ai.gemini_api_keys` | aistudio.google.com (free) |
| Groq (first 90% Whisper; remainder late text fallback) | `ai.groq_api_keys` + `ai.groq_transcription_percent` | console.groq.com (free) |
| DeepSeek, OpenRouter (extra LLM lanes) | `ai.deepseek_api_keys`, `ai.openrouter_api_keys` | their consoles (free tiers) |
| **YouTube Data API (channel stats)** | `youtube.api_keys` | console.cloud.google.com — see §2 |
| Telegram (DMs, phone queue, boardroom summaries) | `telegram.bot_token`, `telegram.owner_id`, `telegram.channel_id` | @BotFather, @userinfobot |
| Pixabay (stock photos) | `pixabay.api_keys` | pixabay.com/api (free) |

---

## 2. Connect your YouTube channels (so the board can see them)

The boardroom reads what `snap` collected — public stats only, read-only,
~3 quota units per channel per day out of 10,000 free ones.

**Step 1 — the key (once):**
1. https://console.cloud.google.com → new project (any name).
2. Enable *YouTube Data API v3*: console.cloud.google.com/apis/library/youtube.googleapis.com
3. Credentials → *Create credentials* → *API key* → copy it.
4. Paste it in `config.yaml`:
   ```yaml
   youtube:
     api_keys:
       - "AIza..."
   ```
   (This is a DIFFERENT key from the Gemini one — different console, same Google account is fine.)

**Step 2 — register each channel (once per channel):**

```powershell
python main.py snap --add @yourhandle        # or a channel link, or any video link from it
python main.py snap --add @secondchannel     # repeat for all three
python main.py snap --list                   # what's tracked (offline, free)
```

**Step 3 — daily (the board reads the LATEST snapshot):**

```powershell
python main.py snap                # all channels: views, deltas, RETITLE? flags (~3 units/channel)
python main.py meeting stats       # the boardroom argues about those numbers
```

One `config.yaml` tracking all three channels is enough — `meeting stats`
puts every channel's numbers on the table. (If you keep one config per
channel, run `snap` + `meeting stats` per config; each sees its own store.)

More stats commands:

```powershell
python main.py snap --channel facts        # one channel (name fragment)
python main.py snap --recent 20 --sort views
python main.py snap --export               # + out/stats/stats-DATE.xlsx (open in Excel)
python main.py snap --remove "old name"    # stop tracking (history stays)
```

---

## 3. The daily loop (three clips a day — one per channel)

```powershell
python main.py snap                # 1. numbers + day-over-day deltas
python main.py meeting stats       # 2. optional: the board reads them with you
python main.py clip --sheet        # 3. clip the next queued source (run once per channel config)
python main.py pregen --push       # 4. park the day's clips on your phone
```

Then upload by hand (~3 min per video; each kit has a CHECKLIST.md).
Long-form is NOT part of the loop yet — it's new; see §6.

---

## 4. The source sheet (paste links, skip the searching)

The sheet is `sources/sheet.csv` — paste links in Excel or notepad
(`url,note` per row; a bare link on a line is fine). Statuses are the
tool's: queued → picked (board's choice) → clipped / failed. Rows are
never deleted — the sheet is also the history.

```powershell
python main.py sheet                              # where things stand
python main.py sheet --add "https://youtu.be/xyz | why I saved it"
python main.py sheet --add "https://youtu.be/abc" --add "https://youtu.be/def"
python main.py clip --sheet                       # clip the next queued source
python main.py clip --sheet 3                     # ...or the next three
```

---

## 5. The boardroom (AI management meetings)

```powershell
python main.py meeting pick                # the board argues over your queued sources
python main.py meeting pick --render       # ...and clips the winner right after
python main.py meeting pick --url "https://youtu.be/xyz"   # add a one-off candidate
python main.py meeting pick --rounds 3     # longer debate (default 2)
python main.py meeting pick --dry-run      # room + agenda, spend nothing
python main.py meeting stats               # review the channel numbers (needs `snap` first)
python main.py meeting act                 # the room decides today's move ITSELF and
                                           #   executes it (clip a queued source, or
                                           #   generate a video on a topic it writes)
python main.py meeting act --dry-run       # preview the act agenda (numbers, 3-day
                                           #   momentum, sources, memory) — $0
python main.py meeting act --rounds 3      # a longer debate before committing
python main.py meeting memory              # the board's full decision log
python main.py meeting last                # re-read the last meeting — full transcript
python main.py meeting pick --no-send      # skip the Telegram summary
```

Every turn prints live, the Telegram DM carries each seat's last word,
and the minutes (with the FULL transcript) land in
`out/meetings/YYYY-MM-DD-pick.md` (or `-stats.md` / `-act.md`).

`meeting act` agenda = channel numbers + 3-day view momentum per video
and channel + pending sources + the last 6 board decisions. Its action
is machine-checked (clip must name a listed source; generate must carry
a real topic) and executed on the spot — board-written generates ride
the stock-footage generate lane (Pexels/Pixabay stills + voiceover) and
are exempt from the meeting's ~10-call budget. Every decision lands in
`work/board_memory.json`; `meeting memory` replays the log.

---

## 6. Clip lanes (source video → vertical Shorts / 16:9 videos)

### clip — subtitled vertical clips (the daily lane)

```powershell
python main.py clip "https://youtube.com/watch?v=..."   # a VOD (shortcut form)
python main.py clip --sheet                              # next source from the sheet
python main.py clip --url A --url B --file C:\video.mp4  # a batch: any mix
python main.py clip <link> --max-clips 12                # keep up to 12 (default 10)
python main.py clip <link> --min-len 25 --max-len 60     # clip length window (seconds)
python main.py clip <link> --sub-pos top                 # bottom|middle|top|auto
python main.py clip <link> --no-vision                   # skip the frame quality check
python main.py clip <link> --top 5                       # + ONE "Top 5 moments" countdown (16:9)
python main.py clip <link> --whole                       # one piece, never cut (auto under 3:00)
python main.py clip <link> --half                        # two near-equal halves
python main.py clip <link> --out C:\somewhere            # output folder (default clips/)
python main.py clip <link> --keep-work                   # keep .ass/frames intermediates
```

### parts — split one long video into a numbered series

```powershell
python main.py parts <link>                  # "Title — Part 1..N" Shorts
python main.py parts <link> --part-len 90    # target seconds per part
python main.py parts <link> --max-parts 20   # episode cap
python main.py parts <link> --no-header      # skip the fading top title
python main.py parts <link> --half           # just two halves
python main.py parts <link> --dry-run        # the part windows only, no renders
```

### longform — 16:9 videos (optional, new — not in the daily loop)

```powershell
python main.py longform <link>                  # the whole source as ONE 1920x1080 video
python main.py longform <link> --minutes 8      # ONE ~8-minute episode
python main.py longform <link> --minutes 8 --start 600   # episode starting near 10:00
python main.py longform <link> --top 5          # "Top 5 chapters" countdown, best LAST
python main.py longform <link> --top 5 --no-vision      # skip frame QC
python main.py longform <link> --dry-run        # the plan only, no render
python main.py longform <link> --no-chapters    # skip description timestamps
python main.py longform <link> --no-subs        # skip the subtitle burn (kit keeps .srt)
python main.py longform --file C:\video.mp4     # a local file
```

Tip: run longform on a source you already clipped — the transcript is
cached, so the whole-source render costs 0 LLM calls.

---

## 7. Original AI videos (script → voice → images → video)

```powershell
python main.py generate                       # one video from the config topic
python main.py generate --topic "the Vasa warship"    # override the topic
python main.py generate --count 3             # three in a row
python main.py generate --seconds 45          # target length
python main.py generate --format portrait     # vertical Short (default) / landscape
python main.py generate --style cartoon       # photoreal (default) | cartoon | stickman
python main.py generate --images-per-scene 4  # denser cuts (1-6)
python main.py generate --no-subs             # skip subtitles
python main.py generate --no-gemini           # scripts straight to Groq/OpenRouter
python main.py generate --keep-going          # continue after a failure

python main.py batch --topics topics.txt      # render a whole list overnight
python main.py batch --count 7                # 7 fresh backlog topics
python main.py batch --dry-run --count 3      # preview: topics + time, $0

python main.py schedule --per-day 2           # unattended: 2/day until Ctrl+C
python main.py schedule --at "08:00,20:00"    # fixed clock times
python main.py schedule --once                # one backlog topic now, then exit
```

Topic backlog:

```powershell
python main.py scout                # LLM proposes, Wikipedia interest validates
python main.py scout --count 12     # more per run
python main.py topics               # see the backlog
python main.py topics --audit       # score every topic for scroll-potential
python main.py topics --add "my own topic"
python main.py topics --topup       # old refill (LLM only, no interest check)
```

After rendering:

```powershell
python main.py package              # upload-ready kits in upload/<id>/
python main.py package --id 3f9     # one job (id prefix ok)
python main.py reburn 3f9           # re-burn subtitles into a made video
python main.py published 3f9 "https://youtu.be/xyz"   # record the upload
python main.py autopost             # newest video -> Buffer draft (manual upload alternative)
python main.py autopost --publish --title "..." --desc "..."
```

---

## 7.5. The panel (no typing at all)

```powershell
# Windows: just double-click  Start Panel.bat   (no terminal, app-style window)
python main.py panel                 # same thing by hand; opens your browser
python main.py panel --status              # who owns each port + the URL to open
python main.py panel --port 9000     # pin the port (8765, else next free)
python main.py panel --no-browser    # start it, open the page yourself
```

What each button actually runs (nothing new — same commands as above):

| Button | Runs |
|---|---|
| Generate a video | `generate --count N --seconds S --style X --image-provider ROUTE` |
| Clip a source | `clip --url <picked link>` or `clip --sheet 1` (next queued) |
| Parts series | `parts --url <picked link>` |
| Run the board | `meeting act --events-json` (the page renders the room live) |
| Dry run | `meeting act --dry-run` (free, offline) |
| Stats meeting | `meeting stats --events-json` |
| Snapshot / Keys / Queue / Preflight / Kits / Errors | `snap`, `keys --month`, `queue`, `preflight`, `package --limit 3`, `errors` |
| YouTube links list | reads/writes `sources/sheet.csv` (`sheet`) |
| Chat with a seat | that seat's model lane, tagged `panel-chat` in the ledger |

One job at a time, output streams live, `■ stop` stops it. The panel binds
127.0.0.1 (this machine only) and **cannot post** — there is no button for
publishing anywhere in it.

Port 8765 already taken by something else (an older project)? The launcher
probes `/api/state` to recognise *this* panel, then opens the port it really
bound — it can never open a stranger's page, and a second double-click just
re-opens the existing window.

---

## 7.6. The night batch (unattended — for 4+ channels)

```powershell
python main.py nightbatch              # clip queue + generate + park + report
python main.py nightbatch --dry-run    # plan only, nothing spent
python main.py nightbatch --fresh      # redo every step (ignore the journal)
```

What one run does, in order: **clip** the next queued sheet sources (each
≈10 min → up to 10 clips), **generate** `--count` videos from the backlog,
**park** new clips on Telegram (`pregen --push`), **report** to Telegram.
Steps run as separate processes with logs in `work/nightbatch/logs/`; the
journal `work/nightbatch/<date>.json` makes re-runs resume (done steps are
skipped). One runner at a time — the lock refuses a second batch (`--force`
overrides). It cannot post anything.

Windows: double-click `Night Batch.bat`, or schedule nightly:

```powershell
schtasks /Create /TN "youtproject night batch" /TR "\"%~dp0Night Batch.bat\"" /SC DAILY /ST 01:30 /F
```

Tick *"Wake the computer to run this task"* in Task Scheduler if the PC
sleeps. From Telegram: `/night` starts it, `/night dry` shows the plan.

## 7.7. From bed: `/go` and the overnight wake task

```powershell
# In Telegram, from your phone:
/go                 # default night: 3 clips + stats meeting + review + top 5
/go 4 2             # 4 clips + 2 videos
/go later           # queue only - runs at the next wake/boot
/go dry             # show the plan, spend nothing
/go status          # queued? what did last night do?

# On the PC (once): the overnight worker
schtasks /Create /TN "youtproject overnight" /TR "\"%~dp0Wake and Run.bat\"" /SC DAILY /ST 01:00 /F
```

Then Task Scheduler → the task → tick **"Wake the computer to run this
task"** and **"Run task as soon as possible after a scheduled start is
missed"**. Hibernating PC = true overnight run; fully shutdown PC = it runs at
the next boot. `Start Bot.bat` keeps the bot listening while the PC is on.

Do it by hand (same thing the task does):

```powershell
python main.py wakeup --sleep-after     # inbox pass + pending /go + hibernate
python main.py wakeup --dry-run         # what would run
python main.py nightbatch --if-requested   # just the pending request, if any
```

Just the second meeting (rank finished clips, pick what to post):

```powershell
python main.py meeting review                 # everything new since the last review
python main.py meeting review --since 2026-10-04T21:00:00 --top 3
python main.py meeting review --clip c-ab12cd34-01
python main.py meeting review --dry-run       # agenda only, spend nothing
```

Nothing here can post. Uploads stay manual.

## 7.8. Orders & the desktop lane (the AI takes the mouse)

One sentence does the whole job — plan first, then work:

```powershell
python main.py order "get a link from the database, get 6 clips and post them in 6 channels, and generate 2 videos for 2 channels"
python main.py order "get 3 clips" --plan-only      # plan only; runs nothing
python main.py order "get 3 clips and 1 video" --dry-run   # clips/renders/stages; no browser post
# --dry-run may consume configured provider quota; "viral" means AI hook/story picks, not a separate top-score filter
```

The desktop lane itself (your own Chrome — the window you watch):

```powershell
python main.py desktop setup        # once: install the browser engine (~120 MB)
Desktop Chrome.bat                  # start your Chromium browser (Opera GX/Edge/Chrome/...) with the debug port; log in once
python main.py desktop status       # settings + lessons AND a live connection test (✅/❌)
python main.py desktop go "open youtube studio and tell me how the newest video is doing"
python main.py desktop go "…" --no-hands     # look + plan, click nothing
python main.py desktop shot                 # screenshot of the page already open (no URL needed)
python main.py desktop text                 # read the page already open (no URL needed)
python main.py order "get a link from the database, get 6 clips and post them in 5 channels" --plan-only
                                            # the same sentence in the panel's Publish box (Plan / Dry run / Run)
# status: ✅ connected | ⚠️ a tab froze (browser fine, click the tab) | ❌ browser not running
# status also prints 'code : <commit> <date>' — proof of which version you are running
python main.py desktop stop                 # kill switch for a running task
python main.py desktop channels             # channels the lane can post to
python main.py desktop channels add "MicroFeed-0" UCWKpOEGAYSgCUzJL-0fO4iQ
python main.py desktop channels remove "MicroFeed-0"    # runtime ones only

python main.py browser data @handle         # public subscriber/video numbers — no API key, no quota
python main.py browser shot <url>           # screenshot any allowed page
python main.py browser text <url>           # the page's text as a real browser sees it
```

Gates, not suggestions: only `desktop.allowed_domains` can be opened; the
publish/schedule click needs `desktop.uploads: on` (`/desk uploads on`); the
delete/buy/unsubscribe family is refused always. Every run: log in
`work/desktop/logs/`, screenshots in `work/desktop/shots/`, lessons in
`work/desktop/lessons.json`, winning paths in `work/desktop/playbooks.json`.

From the phone: `/order <sentence>` · `/desk status|shot|log|stop|uploads on`
· `/look <url>`.

## 8. Your phone (Telegram)

```powershell
python main.py bot                  # bot mode: text it a topic, it renders + sends back
python main.py bot --seconds 45 --format portrait --style cartoon

python main.py pregen --push        # score unpushed clips, send to your private channel
python main.py pregen --best        # today's winner + scorecard (no sends)
python main.py pregen --push --limit 3
python main.py pregen --dry-run     # score + predict, send nothing
python main.py pregen --baseline    # ignore the current backlog (fresh start)
```

While the bot runs you can text it: any message = a topic to render,
`/queue`, `/clips`, `/stats` (channel numbers, one message per channel).

---

## 9. Numbers, quotas, oversight

```powershell
python main.py snap                 # (see §2) channel stats + RETITLE? flags
python main.py yt "https://youtu.be/xyz"     # one video/channel's public stats (1 unit)
python main.py yt --search "roman shorts"    # niche Shorts research (~100 units)
python main.py stats                # local render-history report
python main.py stats --days 14
python main.py keys                 # API key usage vs free-tier limits
python main.py keys --probe         # live-check every key
python main.py keys --probe --sample 5   # ...or the first 5 keys per provider
python main.py keys --month         # 30-day spend rollup per provider
python main.py costs                # cost ledger of recent runs
python main.py errors               # recent failure log
python main.py errors --count 20
python main.py queue                # render-queue status
python main.py voices               # available narrator voices
python main.py voices --lang it-    # filter by language
python main.py subpreview C:\clip.mp4          # subtitle position/size on a real frame
python main.py subpreview C:\clip.mp4 --at 12 --text "sample line"
```

---

## 10. Agents (more AI muscle)

```powershell
python main.py crew --days 3 --per-day 4       # an agent team on a mission (drafts only)
python main.py crew --goal "grow channel 2" --live      # really publish — careful
python main.py crew --status                   # mission progress + chatter
python main.py crew --stop
python main.py jarvis "audit today's clips and retitle the weakest"   # one plain-language task
```

---

## 11. Dev & testing

```powershell
python test_smoke.py                # full offline test suite (no network, no keys, no FFmpeg)
python test_smoke.py longform       # one test group by name (clip, parts, meeting, sheet, ...)
```

---

## 12. Where everything lands

| output | folder | what's inside |
|---|---|---|
| clips | `clips/<key>/` | `_clip_01.mp4...` + kit (TITLE/DESCRIPTION/CHECKLIST/captions) |
| parts | `parts/` | numbered series shorts + kits |
| long-form | `longform/` | 16:9 video + kit with chapter timestamps |
| generated videos | `out/`, kits in `upload/<id>/` | |
| source sheet | `sources/sheet.csv` | your pasted links + statuses (gitignored) |
| boardroom minutes | `out/meetings/YYYY-MM-DD-*.md` | full transcript + decisions |
| channel stats | `out/stats/`, history in `work/snapshots.json` | |
| phone queue | your private Telegram channel | via `pregen --push` |

Every kit's CHECKLIST.md walks you through the manual upload (~3 min).
