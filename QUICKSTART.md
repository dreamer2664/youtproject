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

## Bonus: hand it a sentence (optional)

Later, when you want it: `python main.py order "get a link from the database,
get 6 clips and post them in 6 channels"` — the AI drives your own browser
(see README "Orders" and OPERATIONS "the desktop lane"). Nothing to set up
today; uploads stay manual until you allow them.


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

Then open `upload\<id>\CHECKLIST.md` and follow it — drag the video into
<https://youtube.com/upload>, paste the title/description/tags, upload the
captions file, tick the AI-disclosure box. About 3 minutes.

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
python main.py batch --topics topics.txt     # render a whole list overnight
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

This project has no `videos.insert` upload endpoint. YouTube can restrict API
uploads from unverified API projects to private visibility; check the current
YouTube developer requirements before building an API uploader. Manual Studio
uploads and automated browser interaction are different paths and should not
be conflated.

Manual upload through YouTube Studio does not use this project's Data API
upload client, so the API upload audit/quota restriction described above does
not apply to that manual step. This does not create an exemption for browser
automation. The `CHECKLIST.md` in each kit walks you through Studio step by
step, including the captions upload and the AI-disclosure box you're required
to tick.
