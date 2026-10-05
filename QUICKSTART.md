# QUICKSTART — Windows

Run these **four lines** in PowerShell, one at a time:

```powershell
cd $HOME
git clone https://github.com/dreamer2664/youtproject.git
cd youtproject
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

That installs FFmpeg, creates a Python environment, installs the libraries,
creates `config.yaml`, and installs the secret-blocking git hook.

**Then close PowerShell, open a new one, and re-run the last line once** — FFmpeg needs a
fresh window to be found.

---

## Scope for now

Use the local generator and prepare YouTube kits for **manual upload**. The
separate browser/Buffer automation commands are outside this workflow and are
not enabled by this quickstart.


## After that

```powershell
cd $HOME\youtproject
.\venv\Scripts\Activate.ps1
notepad config.yaml
```

In `config.yaml`, set two things:

```yaml
channel:
  topic: "your channel topic here, be specific"

ai:
  provider: "gemini"
  gemini_api_key: "AQ.your-key-here"
```

Free key, no card: <https://aistudio.google.com/apikey>

Save, then:

```powershell
python main.py preflight     # tells you exactly what is still missing
python main.py generate      # makes a video into out\
python main.py package       # builds the upload kit into upload\<id>\
```

Then open `upload\<id>\CHECKLIST.md` and follow it — upload through
YouTube Studio, review the facts/rights and metadata, add `captions.srt` if
present, and answer Studio's AI-use question based on this video's actual
content. The kit's AI-tools description note does not decide that answer.

```powershell
python main.py published <id> https://youtu.be/...   # record the URL
```

---

## Useful variations

```powershell
python main.py generate --format portrait --seconds 45   # vertical Short
python main.py generate --seconds 120                    # 2-minute landscape video
python main.py generate --topic "one-off idea here"      # single video outside your niche
python main.py generate --images-per-scene 3             # more pictures, denser cuts
python main.py generate --style stickman                 # whiteboard stickman look
python main.py generate --no-subs                        # no subtitles for this run
python main.py batch --topics topics.txt     # render a whole list (PC must be on)
```

These override `config.yaml` for one run only — the file stays untouched, so
you can mix Shorts and long-form freely.

---

## Why your earlier commands failed

They were Linux commands and you are on Windows:

| You ran | Why it failed | Windows equivalent |
|---|---|---|
| `sudo apt install ffmpeg` | `sudo`/`apt` do not exist on Windows | `winget install Gyan.FFmpeg` (setup.ps1 does this) |
| `cp config.example.yaml config.yaml` | wrong folder or wrong shell | `git clone` first, then `cd youtproject`; setup.ps1 copies it |
| `export GEMINI_API_KEY=...` | `export` is bash, not PowerShell | Put the key in `config.yaml` instead |
| `python main.py ...` | `main.py` was not in that folder | `cd $HOME\youtproject` first |

Also: **very new Python versions break some libraries.** If `pip install` fails,
install Python 3.12 from python.org — setup.ps1 will prefer it automatically.

---

## Why there is no YouTube Data API upload command

This project prepares local files and does not call `videos.insert`. YouTube's
restriction applies to videos uploaded through that endpoint from unverified
API projects created after 28 July 2020. The compliance audit is per API
project, not per channel. YouTube Help says creators may re-upload through the
YouTube app/site when a video was locked private by an unverified API service;
manual Studio upload is the supported route used here, not a disguised API
upload or an exemption for unrelated automation.

Official references: [Videos: insert](https://developers.google.com/youtube/v3/docs/videos/insert)
and [YouTube Help: Videos locked as private](https://support.google.com/youtube/answer/7300965?hl=en).
The `CHECKLIST.md` walks through the manual upload, including fact/rights
review and the conditional AI-use answer.
