"""Phone control: a Telegram bot that renders videos on demand.

Setup (free, ~3 minutes):
  1. Message @BotFather on Telegram -> /newbot -> copy the token.
  2. Message @userinfobot -> copy your numeric user id.
  3. Put both in config.yaml (telegram.bot_token / telegram.owner_id)
     and set telegram.enabled: true. (Or export TELEGRAM_BOT_TOKEN.)
  4. python main.py bot   (leave your PC on — the bot polls from here)

Then just text the bot a topic from your phone. It replies with the
finished video as a file (bit-exact, ready to upload) plus the caption
and hashtags. One video renders at a time; extra topics queue up.

Only the configured owner can use the bot — everyone else is ignored.
Uses raw HTTPS calls (requests is already a dependency), so there is no
new package to install and nothing to break on Windows.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import traceback
from pathlib import Path
from queue import Queue as TQueue

import requests

from config import Config
from jobqueue import Queue

API_TIMEOUT = 30          # seconds for normal Bot API calls
POLL_TIMEOUT = 60         # seconds for long-poll getUpdates (server holds 50s)
UPLOAD_TIMEOUT = 600      # seconds for sending the finished video
HEARTBEAT_SECONDS = 240   # "still rendering..." nudge while a video cooks
MAX_MESSAGE = 4000        # Telegram caps messages at 4096 chars
MAX_CAPTION = 1000        # ... and file captions at 1024

HELP_TEXT = """🎬 Send me any topic and I'll render a vertical video for it.

Commands:
/queue — what I'm working on right now
/help — this message
/send <id> — re-send a finished video

One video renders at a time; extra topics queue up behind it.
A video takes roughly 15–25 minutes. I'll send it here as a file
(bit-exact, ready to upload) plus the caption and hashtags.

/jarvis <task> — channel manager: render + schedule + report, e.g.
/jarvis make 3 videos and schedule them 4 hours apart tomorrow.

/crew <mission> — autonomous team for N days, e.g.
/crew manage yourself for 3 days, post 4 times a day (drafts unless "for real")
/stop — halt the mission after the current video."""


class TelegramError(RuntimeError):
    pass


def trim(text: str, limit: int = MAX_MESSAGE) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def slugify(title: str, fallback: str) -> str:
    slug = re.sub(r"-+", "-", "".join(
        c if c.isalnum() else "-" for c in title.lower())).strip("-")[:50]
    return slug or fallback


def parse_incoming(text: str) -> tuple[str, str]:
    """Pure command parser (unit-tested). Returns (action, argument).

    Actions: 'topic' (render this), 'queue', 'send', 'help', 'ignore',
    'jarvis' (channel-manager task).
    """
    text = (text or "").strip()
    if not text:
        return ("ignore", "")
    low = text.lower()
    if low in ("/start", "/help"):
        return ("help", "")
    if low in ("/queue", "/status"):
        return ("queue", "")
    if low.startswith("/new"):
        topic = text[4:].strip()
        return ("topic", topic) if topic else ("help", "")
    if low.startswith("/jarvis"):
        return ("jarvis", text[7:].strip())
    if low.startswith("/crew"):
        return ("crew", text[5:].strip())
    if low == "/stop" or low.startswith("/stop "):
        return ("stop", "")
    if low.startswith("/send"):
        return ("send", text[5:].strip())
    if text.startswith("/"):
        return ("help", "")
    return ("topic", text[:200])


def check_token(cfg: Config) -> str:
    """Validate the Telegram token. Returns the bot's @username."""
    return str(PhoneBot(cfg)._api("getMe").get("username") or "?")


class PhoneBot:
    def __init__(self, cfg: Config, seconds: int | None = None,
                 fmt: str | None = None) -> None:
        self.cfg = cfg
        self.seconds = seconds
        self.fmt = fmt
        self.token = cfg.telegram_token
        self.owner = cfg.telegram_owner
        self.jobs: TQueue[tuple[int, str]] = TQueue()
        self.session = requests.Session()

    # -- low-level API ----------------------------------------------------
    def _api(self, method: str, **kwargs):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        for attempt in (1, 2):
            try:
                resp = self.session.post(
                    url, timeout=kwargs.pop("timeout", API_TIMEOUT), **kwargs)
            except requests.RequestException as exc:
                raise TelegramError(f"network error calling {method}: {exc}") from exc
            if resp.status_code == 429 and attempt == 1:
                try:
                    wait = int(resp.json().get("parameters", {}).get(
                        "retry_after", 5))
                except ValueError:
                    wait = 5
                time.sleep(wait + 1)
                continue
            if resp.status_code in (401, 404):
                raise TelegramError(
                    f"token rejected by Telegram (HTTP {resp.status_code}) — "
                    f"check telegram.bot_token (from @BotFather).")
            try:
                data = resp.json()
            except ValueError as exc:
                raise TelegramError(
                    f"Telegram returned HTTP {resp.status_code} (not JSON).") from exc
            if not data.get("ok"):
                raise TelegramError(
                    f"Telegram error on {method}: {data.get('description')}")
            return data["result"]
        raise TelegramError(f"Telegram rate-limited {method} twice — try later.")

    def send_message(self, chat_id: int, text: str) -> None:
        self._api("sendMessage", data={"chat_id": chat_id,
                                       "text": trim(text)})

    def send_document(self, chat_id: int, path, filename: str,
                      caption: str = "") -> None:
        with open(path, "rb") as handle:
            self._api("sendDocument", timeout=UPLOAD_TIMEOUT,
                      data={"chat_id": chat_id,
                            "caption": trim(caption, MAX_CAPTION)},
                      files={"document": (filename, handle, "video/mp4")})

    # -- message handling -------------------------------------------------
    def handle_message(self, message: dict) -> None:
        chat = message.get("chat", {})
        sender = message.get("from", {})
        chat_id = chat.get("id")
        if chat_id != self.owner or sender.get("id") != self.owner:
            # Only the owner's private chat. Reply once so a wrong-account
            # setup is diagnosable instead of silently dead.
            try:
                self.send_message(chat_id, "⛔ This bot only answers its owner.")
            except TelegramError:
                pass
            print(f"  [bot] ignored message from user {sender.get('id')} "
                  f"(owner is {self.owner})")
            return
        voice = message.get("voice") or message.get("audio")
        if isinstance(voice, dict) and voice.get("file_id"):
            self._handle_voice(chat_id, voice["file_id"])
            return
        text = message.get("text", "")
        if not isinstance(text, str) or not text.strip():
            self.send_message(chat_id, "Send me a topic as text — "
                                      "I'll render a video for it. /help for more.")
            return
        action, arg = parse_incoming(text)
        if action == "help":
            self.send_message(chat_id, HELP_TEXT)
        elif action == "queue":
            self.send_message(chat_id, "📋 Queue:\n" +
                              Queue(self.cfg.state_file).format_table())
        elif action == "send":
            self._send_existing(chat_id, arg)
        elif action == "ignore":
            self.send_message(chat_id, HELP_TEXT)
        elif action == "jarvis":
            if not arg:
                self.send_message(chat_id, "Give me a task, e.g. /jarvis make 3 videos and schedule them 4 hours apart tomorrow.")
            else:
                position = self.jobs.qsize()
                self.jobs.put((chat_id, "jarvis", arg))
                print(f"  [bot] queued jarvis task: {arg}")
                self.send_message(chat_id, f"🧠 Task queued #{position + 1} — I'll report here as I go.")
        elif action == "crew":
            from crew import mission_active, parse_mission

            spec = parse_mission(arg)
            if mission_active(self.cfg):
                self.send_message(chat_id, "A mission is already running. /stop it first.")
            else:
                thread = threading.Thread(target=self._run_crew,
                                          args=(chat_id, spec), daemon=True)
                thread.start()
                mode = "LIVE posting" if spec["live"] else "DRAFT mode — nothing public"
                self.send_message(chat_id,
                                  f"🚀 Mission accepted: {spec['days']} days × "
                                  f"{spec['per_day']}/day, {mode}.\n"
                                  f"I'll ping every post + a digest nightly. "
                                  f"/stop halts.")
        elif action == "stop":
            (self.cfg.root / "crew_stop").write_text("stop", encoding="utf-8")
            print("  [bot] stop requested")
            self.send_message(chat_id, "🛑 Stop requested — halting after the current video.")
        else:
            position = self.jobs.qsize()
            self.jobs.put((chat_id, "topic", arg))
            print(f"  [bot] queued: {arg}")
            if position == 0:
                self.send_message(
                    chat_id, f"🎬 Making \"{arg}\" — I'll send the finished "
                            f"video here. (~15–25 min)")
            else:
                self.send_message(
                    chat_id, f"📥 Queued #{position + 1}: \"{arg}\"")

    def _run_crew(self, chat_id: int, spec: dict) -> None:
        """Run a /crew mission on a background thread (outside the queue)."""
        from crew import run_mission

        def say(message: str) -> None:
            try:
                self.send_message(chat_id, message)
            except TelegramError:
                pass

        try:
            run_mission(self.cfg, spec["days"], spec["per_day"],
                        spec["live"], spec["goal"], say=say)
        except Exception as exc:  # noqa: BLE001 - mission thread must not die silent
            traceback.print_exc()
            say(f"❌ Crew crashed: {exc}")

    def _handle_voice(self, chat_id: int, file_id: str) -> None:
        """Voice note -> transcribed topic (or 'jarvis, ...' task)."""
        from voice import download_telegram_voice, transcribe

        try:
            path = download_telegram_voice(self.token, file_id,
                                           self.cfg.work_dir)
            topic = transcribe(self.cfg, path)
        except Exception as exc:
            self.send_message(chat_id,
                              f"🎙️ Couldn't hear that ({exc}). Try text?")
            return
        if not topic:
            self.send_message(chat_id, "🎙️ Heard nothing — try again or type it?")
            return
        print(f"  [bot] voice -> {topic}")
        if topic.lower().lstrip().startswith("jarvis"):
            task = topic.lstrip()[6:].lstrip(" ,:—-")
            if task:
                self.jobs.put((chat_id, "jarvis", task))
                self.send_message(chat_id, f"🎙️ Heard: \"{topic}\"\n🧠 On it.")
                return
        position = self.jobs.qsize()
        self.jobs.put((chat_id, "topic", topic))
        self.send_message(chat_id, f"🎙️ Heard: \"{topic}\"")
        if position:
            self.send_message(chat_id, f"📥 Queued #{position + 1}")

    def _send_existing(self, chat_id: int, job_ref: str) -> None:
        """Deliver an already-made video (recovery + re-send)."""
        job = Queue(self.cfg.state_file).get(job_ref.strip()) if job_ref.strip() else None
        if job is None:
            self.send_message(chat_id, f"No job matching '{job_ref.strip()}'. "
                                      "/queue to list them.")
            return
        if job.status not in ("generated", "packaged"):
            self.send_message(chat_id, f"Job {job.id} is '{job.status}' — "
                                      "nothing to send yet.")
            return
        video = Path(job.video_file) if job.video_file else None
        if video is None or not video.exists():
            self.send_message(chat_id, f"Video file for {job.id} is missing on the PC.")
            return
        self.send_message(chat_id, f"\U0001F4E4 Sending \"{(job.title or job.topic)[:60]}\"…")
        try:
            self._deliver(chat_id, job.id, job.topic)
        except Exception as exc:  # noqa: BLE001 - tell the user, keep polling
            print(f"  [bot] re-send failed: {exc}")
            try:
                self.send_message(chat_id, f"\u274C couldn't send it: {exc}")
            except TelegramError:
                pass

    # -- rendering worker (one video at a time) ---------------------------
    def _heartbeat(self, chat_id: int, topic: str,
                   started: float, stop: threading.Event) -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            mins = int((time.time() - started) / 60)
            try:
                self.send_message(
                    chat_id, f"⏳ Still rendering \"{topic[:60]}\" "
                             f"({mins} min in). I'll send the video here "
                             f"when it's done.")
            except TelegramError:
                pass

    def _run_task(self, chat_id: int, task: str) -> None:
        """Run a /jarvis task, narrating progress back to the chat."""
        def say(message: str) -> None:
            try:
                self.send_message(chat_id, message)
            except TelegramError:
                pass

        say(f"🧠 Working on it: {task[:200]}")
        try:
            from jarvis import run_task

            summary = run_task(self.cfg, task, say=say)
        except Exception as exc:  # noqa: BLE001 - worker loop is immortal
            traceback.print_exc()
            say(f"❌ Task failed: {exc}")
            return
        say(f"✅ Done.\n\n{summary}")

    def _render_and_send(self, chat_id: int, topic: str) -> None:
        # Deferred import: main.py imports this module for cmd_bot.
        from main import cmd_generate

        before = {job.id for job in Queue(self.cfg.state_file).jobs}
        gen_args = argparse.Namespace(
            topic=topic, count=1, seconds=self.seconds, format=self.fmt,
            images_per_scene=None, no_subs=False, style=None, keep_work=False,
            keep_going=False, verbose=False,
        )
        stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(chat_id, topic, time.time(), stop), daemon=True)
        heartbeat.start()
        try:
            print(f"  [bot] rendering: {topic}")
            cmd_generate(self.cfg, gen_args)
        except SystemExit as exc:
            print(f"  [bot] render exited: {exc.code}")
        except Exception:  # noqa: BLE001 - one video must never kill the bot
            traceback.print_exc()
        finally:
            stop.set()
            heartbeat.join(timeout=5)

        new = [j for j in Queue(self.cfg.state_file).jobs if j.id not in before]
        job = new[-1] if new else None
        if job is None or job.status != "generated":
            detail = (job.error if job and job.error else "no video was produced")[:200]
            print(f"  [bot] render failed: {detail}")
            try:
                self.send_message(chat_id, f"❌ \"{topic[:60]}\" failed: {detail}\n"
                                           f"Send another topic whenever — nothing "
                                           f"else was affected.")
            except TelegramError:
                pass
            return

        # Auto-package so the full YouTube kit also exists on the PC.
        try:
            from package import build_package

            queue = Queue(self.cfg.state_file)
            kit = build_package(job, self.cfg)
            queue.update(job, status="packaged", package_dir=str(kit))
            print(f"  [bot] packaged: {kit}")
        except Exception as exc:  # noqa: BLE001 - video still sendable
            print(f"  [bot] packaging failed (video is fine): {exc}")

        try:
            self._deliver(chat_id, job.id, topic)
        except Exception as exc:  # noqa: BLE001 - tell the user, keep polling
            print(f"  [bot] delivery failed: {exc}")
            traceback.print_exc()
            try:
                self.send_message(chat_id, f"\u274C video rendered fine, but I couldn't send it: {exc}\n"
                                           f"It's packaged on your PC: upload/{job.id}/")
            except TelegramError:
                pass

    def _deliver(self, chat_id: int, job_id: str, topic: str) -> None:
        from package import build_platform_caption

        queue = Queue(self.cfg.state_file)
        job = queue.get(job_id)
        if job is None:
            self.send_message(chat_id, "❌ Finished, but I lost track of the "
                                      "job — check the PC queue.")
            return

        meta_path = Path(job.meta_file)
        meta = json.loads(meta_path.read_text(encoding="utf-8")) \
            if meta_path.exists() else {}
        title = meta.get("title") or job.title or topic
        video = Path(job.video_file)
        size_mb = video.stat().st_size / (1024 * 1024) if video.exists() else 0
        if size_mb > 48:
            self.send_message(
                chat_id, f"⚠️ \"{title[:60]}\" rendered ({size_mb:.0f} MB) but "
                         f"Telegram caps bot files at 50 MB — grab it from the PC: "
                         f"{video}")
            return
        caption = build_platform_caption(meta, "tiktok")
        try:
            self.send_document(chat_id, video, slugify(title, job.id) + ".mp4",
                               caption=f"🎬 {title}"[:MAX_CAPTION])
            self.send_message(
                chat_id, f"Caption + hashtags (copy-paste):\n\n{caption}\n"
                         f"After uploading, track it on your PC:\n"
                         f"`python main.py published {job.id} <url>`\n"
                         f"Full YouTube kit: upload/{job.id}/")
        except TelegramError as exc:
            print(f"  [bot] delivery failed: {exc}")
            try:
                self.send_message(chat_id, f"❌ Rendered fine but Telegram "
                                           f"wouldn't take the file: {exc}")
            except TelegramError:
                pass
        print(f"  [bot] delivered {job.id} ({size_mb:.1f} MB)")

    def _worker(self) -> None:
        while True:
            chat_id, kind, text = self.jobs.get()
            try:
                if kind == "jarvis":
                    self._run_task(chat_id, text)
                else:
                    self._render_and_send(chat_id, text)
            except Exception:  # noqa: BLE001 - worker loop is immortal
                traceback.print_exc()
            finally:
                self.jobs.task_done()

    # -- main loop --------------------------------------------------------
    def run_forever(self) -> None:
        me = self._api("getMe")
        username = me.get("username") or "?"
        print(f"  [bot] live as @{username} — "
              f"message it from the owner's Telegram account.")
        print(f"  [bot] test it now: send /start to @{username} from your phone.")
        print("  [bot] Ctrl+C stops the bot "
              "(a running render is abandoned; its files stay in out/).")
        worker = threading.Thread(target=self._worker, daemon=True)
        worker.start()

        # Skip anything sent while we were away — never auto-render stale mail.
        offset = 0
        try:
            pending = self._api("getUpdates", data={"timeout": 0})
            if pending:
                offset = pending[-1]["update_id"] + 1
                print(f"  [bot] skipped {len(pending)} stale update(s).")
        except TelegramError as exc:
            print(f"  [bot] couldn't drain pending updates ({exc}); continuing.")

        print("  [bot] polling… (silence is normal — I'm waiting for your message)")
        polling_since = time.time()
        last_log = polling_since
        while True:
            if time.time() - last_log > 300:
                total = int((time.time() - polling_since) / 60)
                state = "rendering" if self.jobs.unfinished_tasks > 0 else "idle"
                print(f"  [bot] still polling ({total} min, {state})…")
                last_log = time.time()
            try:
                updates = self._api(
                    "getUpdates", timeout=POLL_TIMEOUT,
                    data={"offset": offset, "timeout": 50,
                          "allowed_updates": ["message"]})
            except KeyboardInterrupt:
                raise
            except TelegramError as exc:
                print(f"  [bot] poll error ({exc}); retrying in 5s…")
                time.sleep(5)
                continue
            for update in updates:
                offset = update.get("update_id", offset) + 1
                message = update.get("message")
                if not message:
                    continue
                try:
                    self.handle_message(message)
                except KeyboardInterrupt:
                    raise
                except Exception:  # noqa: BLE001 - one message never kills the bot
                    traceback.print_exc()
