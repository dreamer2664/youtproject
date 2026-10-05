"""The Panel — a click-only control surface for youtproject.

    python main.py panel               # opens the page in your browser
    double-click "Start Panel.bat"     # Windows: same thing, no terminal

One local page, big buttons, no commands to remember. Every button runs
the exact CLI you would have typed, as a subprocess, and streams its
output into the page. The panel adds no pipeline logic of its own: if a
command works in the terminal, it works here — and if it breaks, the
page shows the same error the terminal would.

Deliberate constraints (do not relax these casually):

  * stdlib only (http.server) — no new dependencies, works offline;
  * binds 127.0.0.1 by default — a local tool, not a web service;
  * one job at a time, in order — the queue is visible, nothing stacks
    up invisibly and two renders never fight over the same files;
  * Generate/Clip/Parts never publish; the Publish section drives the same
    durable `order` lane as the CLI. It previews exact channels/action and
    requires a one-use typed phrase; desktop.uploads off = stage only.
  * no direct uploader/API in the panel — confirmed posts use only the
    existing desktop.py local Studio browser lane (not Buffer);
  * keys stay masked — the CLI already masks them; mask_line() is the
    second lock on the same door, because logs are shown on a page.

Routes (all JSON, all local):

    GET  /                      the page itself
    GET  /events                SSE: log lines, meeting events, job state
    GET  /api/state             header chips, jobs, action catalog
    POST /api/run               {"action": ..., "params": {...}}
    POST /api/stop              stop the running job + clear the queue
    GET  /api/links             the source sheet as rows
    POST /api/links             {"op": "add"|"drop"|"restore", ...}
    GET  /api/meetings          minutes files, newest first
    GET  /api/minutes?name=...  one minutes file's text
    POST /api/chat              {"role": ..., "text": ..., "history": [...]}
    POST /api/open              {"what": "out"} — open the folder
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
EVENT_PREFIX = "##PANEL## "
MAX_LOG_LINES = 3000
HISTORY_KEEP = 25
DEFAULT_PORT = 8765


CONFIRM_TTL_SECONDS = 300
_ORDER_CONFIRMATIONS: dict[str, dict] = {}
_ORDER_CONFIRM_LOCK = threading.Lock()


def _order_confirm_fingerprint(params: dict) -> str:
    """Bind the one-use panel approval to the exact displayed request."""
    fields = {
        "text": str(params.get("text") or "").strip(),
        "topic": str(params.get("topic") or "").strip(),
        "channel_names": [str(x).strip() for x in (params.get("channel_names") or [])],
        "visibility": str(params.get("visibility") or "").strip().lower(),
        "rights_confirmed": bool(params.get("rights_confirmed")),
        "altered_content": params.get("altered_content"),
        "made_for_kids": params.get("made_for_kids"),
        "source_file": str(params.get("source_file") or "").strip(),
        "dry_run": bool(params.get("dry_run")),
        "plan_only": bool(params.get("plan_only")),
    }
    encoded = json.dumps(fields, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _preview_order(cfg, params: dict) -> dict:
    """Resolve a panel order without executing it; mint one-use consent token."""
    import orders
    from desktop import dconf

    text = str(params.get("text") or "").strip()
    if not text:
        raise ValueError("write the order first")
    order = orders.parse_order(text)
    order.update({
        "channel_names": [str(x).strip() for x in (params.get("channel_names") or [])
                          if str(x).strip()],
        "topic": str(params.get("topic") or "").strip(),
        "channel_selection_explicit": True,
        "visibility": str(params.get("visibility") or dconf(cfg)["visibility"]),
        "rights_confirmed": bool(params.get("rights_confirmed")),
        "altered_content": (None if params.get("altered_content") not in ("yes", "no")
                            else params.get("altered_content") == "yes"),
        "made_for_kids": (None if params.get("made_for_kids") not in ("yes", "no")
                          else params.get("made_for_kids") == "yes"),
        "source_file": str(params.get("source_file") or ""),
        "dry": bool(params.get("dry_run")),
        "plan_only": bool(params.get("plan_only")),
        "confirm_publish": False,
    })
    try:
        selected = orders.resolve_selected_channels(cfg, order) if order.get("post") else []
        selection_error = ""
    except ValueError as exc:
        selected, selection_error = [], str(exc)
    plan = orders.plan_text(cfg, order)
    conf = dconf(cfg)
    blockers = []
    if not order.get("ok"):
        blockers.append("the request does not contain a recognized operation")
    if order.get("post"):
        if selection_error:
            blockers.append(selection_error)
        if not selected:
            blockers.append("select at least one exact YouTube channel")
        if not conf["uploads"]:
            blockers.append("desktop.uploads is off")
        if order.get("dry") or order.get("plan_only"):
            blockers.append("this is a dry/plan-only request")
        if bool(order.get("clips") or order.get("parts")) and not order.get("rights_confirmed"):
            blockers.append("source-use rights are not attested")
        if order.get("altered_content") not in (True, False):
            blockers.append("choose Yes or No for altered content")
        if order.get("made_for_kids") not in (True, False):
            blockers.append("choose Yes or No for the made-for-kids audience setting")
        if str(order.get("visibility") or "").lower() not in ("public", "unlisted", "private"):
            blockers.append("choose a valid visibility")
    will_publish = bool(order.get("post") and not blockers)
    token = ""
    phrase = ""
    channel_rows = [{"name": c.get("name", ""), "id": c.get("id", "")}
                    for c in selected]
    if will_publish:
        token = uuid.uuid4().hex
        phrase = ("CONFIRM PUBLISH to " + ", ".join(
            f"{c['name']} ({c['id']})" if c.get("id") else c["name"]
            for c in selected) + " as " + str(order["visibility"]).lower())
        with _ORDER_CONFIRM_LOCK:
            now = time.time()
            for old in [key for key, value in _ORDER_CONFIRMATIONS.items()
                        if value.get("expires", 0) < now]:
                _ORDER_CONFIRMATIONS.pop(old, None)
            _ORDER_CONFIRMATIONS[token] = {
                "fingerprint": _order_confirm_fingerprint(params),
                "expires": now + CONFIRM_TTL_SECONDS,
                "phrase": phrase,
            }
    return {
        "plan": plan, "valid": bool(order.get("ok")),
        "post_requested": bool(order.get("post")),
        "will_publish": will_publish, "uploads": conf["uploads"],
        "channels": channel_rows,
        "visibility": str(order.get("visibility") or "").lower(),
        "blockers": blockers, "confirmation_token": token,
        "confirmation_phrase": phrase,
    }


def _consume_order_confirmation(params: dict) -> tuple[bool, str]:
    token = str(params.pop("confirmation_token", "") or "")
    if not token:
        return False, "publish confirmation is missing; preview and confirm the exact plan first"
    with _ORDER_CONFIRM_LOCK:
        confirmation = _ORDER_CONFIRMATIONS.pop(token, None)
    if not confirmation or confirmation.get("expires", 0) < time.time():
        return False, "publish confirmation expired or was already used; preview the plan again"
    if confirmation.get("fingerprint") != _order_confirm_fingerprint(params):
        return False, "publish request changed after confirmation; preview the exact plan again"
    params["confirm_publish"] = True
    params["panel_confirmed"] = True
    return True, ""


# --------------------------------------------------------------- actions
def build_argv(action: str, params: dict | None = None) -> list[str]:
    """Turn a click into exact CLI args (pure, tested).

    This is the ONLY place button -> command mapping lives, so the page
    can never invent a flag the CLI does not have.
    """
    p = dict(params or {})
    if action == "generate":
        count = max(1, min(10, int(p.get("count") or 1)))
        argv = ["generate", "--count", str(count)]
        if p.get("topic"):
            argv += ["--topic", str(p["topic"])]
        if p.get("seconds"):
            argv += ["--seconds", str(int(p["seconds"]))]
        if p.get("style") in ("photoreal", "cartoon", "stickman"):
            argv += ["--style", str(p["style"])]
        if p.get("route"):
            argv += ["--image-provider", str(p["route"])]
        # Standalone panel generation follows the same no-paid provider policy
        # as the durable order pipeline; explicit UI routes cannot enable a
        # metered provider for this click.
        argv.append("--free-only")
        return argv
    if action == "clip":
        url = (p.get("url") or "").strip()
        argv = ["clip", "--url", url] if url else ["clip", "--sheet", "1"]
        argv.append("--free-only")
        return argv
    if action == "parts":
        url = (p.get("url") or "").strip()
        if not url:
            raise ValueError("parts needs a source link")
        return ["parts", "--url", url, "--free-only"]
    if action == "meeting":
        kind = p.get("kind") if p.get("kind") in ("act", "stats", "pick") else "act"
        argv = ["meeting", kind, "--no-send"]
        if p.get("dry_run"):
            argv.append("--dry-run")      # instant, spends nothing
        else:
            argv.append("--events-json")  # so the page can render the room
        return argv
    if action == "meeting_memory":
        return ["meeting", "memory"]
    if action == "meeting_last":
        return ["meeting", "last"]
    if action == "order":
        text = (p.get("text") or "").strip()
        if not text:
            raise ValueError("the order needs words — e.g. \"generate 1 video and stage it\"")
        argv = ["order", text, "--no-default-channels"]
        if p.get("topic"):
            argv += ["--topic", str(p["topic"])]
        if p.get("plan_only"):
            argv.append("--plan-only")
        elif p.get("dry_run"):
            argv.append("--dry-run")
        for channel in p.get("channel_names") or []:
            if str(channel).strip():
                argv += ["--channel", str(channel).strip()]
        if p.get("visibility"):
            argv += ["--visibility", str(p["visibility"])]
        if p.get("rights_confirmed"):
            argv.append("--rights-confirmed")
        if p.get("altered_content") in ("yes", "no"):
            argv += ["--altered-content", str(p["altered_content"])]
        if p.get("made_for_kids") in ("yes", "no"):
            argv += ["--made-for-kids", str(p["made_for_kids"])]
        if p.get("source_file"):
            argv += ["--source-file", str(p["source_file"])]
        if p.get("resume"):
            argv += ["--resume", str(p["resume"])]
        if p.get("confirm_publish"):
            argv += ["--confirm-publish", "--panel-confirmed"]
        return argv
    if action == "desktop_status":
        return ["desktop", "status"]
    if action == "desktop_channels":
        return ["desktop", "channels", "list"]
    if action == "snap":
        return ["snap"]
    if action == "keys":
        return ["keys", "--month"]
    if action == "queue":
        return ["queue"]
    if action == "preflight":
        return ["preflight"]
    if action == "package":
        limit = max(1, min(20, int(p.get("limit") or 3)))
        return ["package", "--limit", str(limit)]
    if action == "errors":
        return ["errors"]
    if action == "sheet":
        return ["sheet"]
    if action == "topics":
        return ["topics"]
    if action == "yt_video":
        url = (p.get("url") or "").strip()
        if not url:
            raise ValueError("yt_video needs a link")
        return ["yt", url]
    raise ValueError(f"unknown action {action!r}")


def describe(action: str, params: dict | None = None) -> str:
    """Human label for the job bar (pure, tested)."""
    p = dict(params or {})
    if action == "generate":
        bits = [f"{p.get('count', 1)} video(s)"]
        if p.get("seconds"):
            bits.append(f"{p['seconds']}s")
        if p.get("style"):
            bits.append(str(p["style"]))
        if p.get("route"):
            bits.append(f"{p['route']} route")
        return "generate: " + ", ".join(bits)
    if action == "clip":
        return "clip: " + ((p.get("url") or "").strip() or "next on the list")
    if action == "parts":
        return "parts: " + (p.get("url") or "")
    if action == "meeting":
        kind = p.get("kind", "act")
        return f"meeting {kind}" + (" (dry run)" if p.get("dry_run") else "")
    if action == "order":
        text = (p.get("text") or "").strip()
        short = text if len(text) <= 48 else text[:45] + "…"
        flag = (" (plan only)" if p.get("plan_only") else
                " (dry run)" if p.get("dry_run") else "")
        return f"order: {short}{flag}"
    if action == "desktop_status":
        return "desktop status (browser check)"
    if action == "desktop_channels":
        return "desktop channels"
    return action.replace("_", " ")


# ----------------------------------------------------------------- events
def encode_event(event: dict) -> str:
    """One meeting event -> the `##PANEL## {json}` stdout line."""
    return EVENT_PREFIX + json.dumps(event, ensure_ascii=False, default=str)


def decode_line(line: str) -> dict | None:
    """A stdout line -> event dict, or None when it's an ordinary log line."""
    if not line.startswith(EVENT_PREFIX):
        return None
    try:
        event = json.loads(line[len(EVENT_PREFIX):])
    except (ValueError, TypeError):
        return None
    return event if isinstance(event, dict) else None


def sse_frame(payload: dict) -> str:
    """One SSE frame: JSON on a single `data:` line (never splits a frame)."""
    return "data: " + json.dumps(payload, ensure_ascii=False, default=str) + "\n\n"


# ----------------------------------------------------------------- masking
_KEY_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxoxb-[0-9A-Za-z\-]{10,}\b"),
    re.compile(r"\bgAAAAA[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_\-]{30,}\b"),   # telegram bot token
    re.compile(r"(?i)\b(api[_-]?key|key|token)=([A-Za-z0-9_\-]{16,})"),
)
_LOOSE_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{28,}\b")


def mask_line(line: str) -> str:
    """Second lock on the key door: strips anything key-shaped from a line.

    The CLI masks keys itself; this is defense in depth because panel logs
    are rendered on a page. URLs, paths and hashes are deliberately left
    alone — they are not secrets and mangling them makes logs useless.
    """
    text = (line or "").rstrip("\n")
    for pattern in _KEY_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(
                lambda m: f"{m.group(1)}=…{m.group(2)[-4:]}", text)
        else:
            text = pattern.sub(lambda m: "…" + m.group(0)[-4:], text)

    def _loose(m: re.Match) -> str:
        token = m.group(0)
        if "://" in token or "/" in token or "." in token:
            return token
        if sum(c.isdigit() for c in token) < 3:
            return token
        return "…" + token[-4:]

    return _LOOSE_TOKEN.sub(_loose, text)


# ------------------------------------------------------------ sheet links
def pending(status: str) -> bool:
    """A sheet row is queued when its status is '', new or picked (sheet.py)."""
    return (status or "new") in ("new", "picked")


def links_payload(cfg) -> list[dict]:
    """Every sheet row, newest last, with a pending flag for the page."""
    from sheet import load_sheet

    rows = []
    for row in load_sheet(cfg.sources_sheet):
        rows.append({"url": row["url"], "note": row["note"],
                     "added": row["added"],
                     "status": row["status"] or "new",
                     "result": row["result"],
                     "pending": pending(row["status"])})
    return rows


def add_link(cfg, url: str, note: str = "") -> str:
    from sheet import SheetError, append_sheet

    try:
        added = append_sheet(cfg.sources_sheet, (url or "").strip(), note)
    except SheetError as exc:
        return f"skipped — {exc}"
    return "added to the list" if added else "already on the list"


def drop_link(cfg, url: str) -> str:
    """Queue-remove without deleting history: status -> dropped."""
    from sheet import mark_sheet

    return "removed from the queue" if mark_sheet(
        cfg.sources_sheet, url, "dropped") else "row not found"


def restore_link(cfg, url: str) -> str:
    from sheet import mark_sheet

    return "back in the queue" if mark_sheet(
        cfg.sources_sheet, url, "new") else "row not found"


# --------------------------------------------------------------- minutes
def meetings_payload(cfg) -> list[dict]:
    """Minutes files, newest first (name + kind enough for the list)."""
    folder = cfg.out_dir / "meetings"
    out = []
    try:
        for path in sorted(folder.glob("*.md"), reverse=True)[:50]:
            stat = path.stat()
            out.append({"name": path.name,
                        "kind": path.stem.rsplit("-", 1)[-1],
                        "when": stat.st_mtime, "bytes": stat.st_size})
    except OSError:
        pass
    return out


def minutes_text(cfg, name: str) -> str:
    """Read one minutes file by name (regex-guarded: no path traversal)."""
    if not re.fullmatch(r"[A-Za-z0-9._\-]+\.md", name or ""):
        return "bad file name"
    try:
        return (cfg.out_dir / "meetings" / name).read_text(encoding="utf-8")
    except OSError as exc:
        return f"cannot read {name}: {exc}"


# ------------------------------------------------------------------ chat
CHAT_MAX_WORDS = 120
LLM_LANES = ("gemini", "groq", "openrouter", "deepseek")


def has_llm_lane(cfg) -> bool:
    """True when at least one chat-capable lane is configured.

    Pollinations and the template writer are deliberately NOT counted:
    they exist so a *render* never dies without keys, but answering a
    chat through a keyless endpoint would hide a missing key behind
    plausible-sounding text. Better to say what is missing.
    """
    for name in LLM_LANES:
        try:
            attr = ("groq_llm_api_keys" if name == "groq"
                    else f"{name}_api_keys")
            # Groq is special: its ordered pool is split by
            # ai.groq_transcription_percent, and Whisper-only keys cannot
            # serve a chat turn. Gate on the text share.
            if [k for k in getattr(cfg, attr) if k]:
                return True
        except Exception:  # noqa: BLE001 - a missing lane is not an error
            continue
    try:
        return bool(cfg.azure_api_key and cfg.azure_endpoint)
    except Exception:  # noqa: BLE001
        return False


def chat_reply(cfg, role: str, question: str, history=None,
               provider=None) -> str:
    """One chat turn with a board seat. `provider` is injectable for tests."""
    import meeting as mt

    role_meta = next((r for r in mt.ROLES
                      if r["name"].lower() == (role or "").strip().lower()),
                     None)
    if role_meta is None:
        return ("Unknown seat — I only know: "
                + ", ".join(r["name"] for r in mt.ROLES))
    question = (question or "").strip()
    if not question:
        return "(empty message)"
    lines = []
    for turn in (history or [])[-8:]:
        who = str(turn.get("who", "owner"))[:40]
        text = str(turn.get("text", ""))[:600]
        if text:
            lines.append(f"{who}: {text}")
    prompt = (
        f"You are the {role_meta['name']} {role_meta['emoji']} on the board "
        "of a YouTube channel run by one person who uploads by hand. "
        f"Your standing brief: {role_meta['persona']}\n\n"
        "You are now in a private chat with the owner. Be direct and "
        "practical, at most "
        f"{CHAT_MAX_WORDS} words, no roleplay asterisks. If you need data "
        "you don't have, say exactly which page or command has it.\n\n"
        + ("Conversation so far:\n" + "\n".join(lines) + "\n\n" if lines else "")
        + f"Owner: {question}\n{role_meta['name']}:"
    )
    if provider is None:
        if not has_llm_lane(cfg):
            return ("No LLM lane answered — no Gemini/Groq/OpenRouter/"
                    "DeepSeek/Azure key is configured yet. Add one (the "
                    "Keys button shows what exists), then try again.")
        provider = mt._role_provider(cfg, role_meta["lane"])
    if provider is None:
        return ("No LLM lane answered — check the keys "
                "(Stats & tools → Keys), then try again.")
    try:
        text = provider.generate_text(prompt, temperature=0.7,
                                      tag="panel-chat")
    except Exception as exc:  # noqa: BLE001 - a chat reply never crashes
        return f"{role_meta['name']} could not answer ({str(exc)[:120]})."
    return str(text).strip() or "(silence)"


# --------------------------------------------------------------- snapshot
KEY_PROVIDERS = ("gemini", "groq", "openrouter", "deepseek",
                 "elevenlabs", "youtube", "pexels", "pixabay")


def configured_keys(cfg) -> dict[str, int]:
    """How many keys each lane has (never raises — a chip is not worth it)."""
    out: dict[str, int] = {}
    for name in KEY_PROVIDERS:
        try:
            keys = getattr(cfg, f"{name}_api_keys")
            out[name] = len([k for k in keys if k])
        except Exception:  # noqa: BLE001
            out[name] = 0
    return out


def snapshot(cfg) -> dict:
    """Header-strip numbers: keys, disk, queue, meetings. Offline only."""
    out: dict = {"keys": configured_keys(cfg),
                 "disk_free_mb": None, "disk_total_mb": None,
                 "queue": {}, "meetings": 0, "work_dir": str(cfg.work_dir)}
    try:
        usage = shutil.disk_usage(str(ROOT))
        out["disk_free_mb"] = int(usage.free / 2**20)
        out["disk_total_mb"] = int(usage.total / 2**20)
    except OSError:
        pass
    try:
        from jobqueue import Queue

        out["queue"] = Queue(cfg.state_file).counts()
    except Exception:  # noqa: BLE001
        pass
    try:
        out["meetings"] = len(list((cfg.out_dir / "meetings").glob("*.md")))
    except OSError:
        pass
    try:  # the publishing gate: the page must never hide this
        import desktop

        out["uploads"] = bool(desktop.dconf(cfg)["uploads"])
        out["channels"] = len(desktop.all_channels(cfg))
    except Exception:  # noqa: BLE001
        out["uploads"] = None
        out["channels"] = 0
    return out


# ------------------------------------------------------------------- hub
class Hub:
    """Fan-out to every open page (normally one)."""

    def __init__(self) -> None:
        self._clients: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        client: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._clients.add(client)
        return client

    def unsubscribe(self, client: queue.Queue) -> None:
        with self._lock:
            self._clients.discard(client)

    def broadcast(self, payload: dict) -> None:
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.put_nowait(payload)
            except queue.Full:
                pass


# ------------------------------------------------------------------- jobs
class Job:
    def __init__(self, action: str, params: dict, argv: list[str],
                 label: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.action = action
        self.params = params
        self.argv = argv
        self.label = label
        self.status = "queued"        # queued running done failed stopped
        self.started: float | None = None
        self.ended: float | None = None
        self.returncode: int | None = None
        self.stopped = False
        self.lines: deque[str] = deque(maxlen=MAX_LOG_LINES)
        self.events: list[dict] = []
        self.proc: subprocess.Popen | None = None

    def to_dict(self, lines: int = 0) -> dict:
        out = {"id": self.id, "action": self.action, "label": self.label,
               "argv": self.argv, "status": self.status,
               "started": self.started, "ended": self.ended,
               "returncode": self.returncode,
               "events": self.events[-40:]}
        if lines:
            out["lines"] = list(self.lines)[-lines:]
        return out


class Runner:
    """One subprocess at a time, FIFO. This is the whole scheduler."""

    def __init__(self, hub: Hub, history_path: Path) -> None:
        self.hub = hub
        self.history_path = history_path
        self.jobs: deque[Job] = deque(maxlen=HISTORY_KEEP)
        self.pending: deque[Job] = deque()
        self.current: Job | None = None
        self.history: list[dict] = self._load_history()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False
        threading.Thread(target=self._work, daemon=True,
                         name="panel-runner").start()

    # -- queue ----------------------------------------------------------
    def submit(self, action: str, params: dict | None = None) -> Job:
        argv = build_argv(action, params)      # raises ValueError -> 400
        job = Job(action, dict(params or {}), argv, describe(action, params))
        with self._lock:
            self.pending.append(job)
            self.jobs.append(job)
        self._broadcast_jobs()
        self._wake.set()
        return job

    def stop(self) -> str:
        with self._lock:
            self._stop = True
            dropped = len(self.pending)
            self.pending.clear()
            proc = self.current.proc if self.current else None
            if self.current is not None and self.current.status == "running":
                self.current.stopped = True
        if proc is not None and proc.poll() is None:
            _terminate(proc)
            note = "stopping the running job"
        elif self.current is not None:
            note = "job already finished"
        else:
            note = "nothing was running"
        self._broadcast_jobs()
        return f"{note}; {dropped} queued job(s) dropped"

    # -- worker ---------------------------------------------------------
    def _work(self) -> None:
        while True:
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            with self._lock:
                if self._stop:
                    self._stop = False
                    continue
                if not self.pending:
                    continue
                job = self.pending.popleft()
                self.current = job
            try:
                self._run(job)
            finally:
                with self._lock:
                    self.current = None

    def _run(self, job: Job) -> None:
        job.status = "running"
        job.started = time.time()
        self._broadcast_jobs()
        self._log(job, f"$ python main.py {' '.join(job.argv)}")
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            job.proc = subprocess.Popen(
                [sys.executable, "-u", str(ROOT / "main.py"), *job.argv],
                cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, creationflags=flags,
                start_new_session=(os.name != "nt"))
        except OSError as exc:
            job.status = "failed"
            job.ended = time.time()
            self._log(job, f"[panel] could not start the command: {exc}")
            self._finish(job)
            return
        assert job.proc.stdout is not None
        try:
            for raw in job.proc.stdout:
                line = mask_line(raw)
                event = decode_line(line)
                if event is not None:      # meeting event: parsed, not shown
                    job.events.append(event)
                    self.hub.broadcast({"kind": "event", "job": job.id,
                                        "event": event})
                    continue
                self._log(job, line)
        except (OSError, ValueError):
            pass                            # process died mid-read: fine
        rc = job.proc.wait()
        job.returncode = rc
        job.ended = time.time()
        if job.stopped:
            job.status = "stopped"
        else:
            job.status = "done" if rc == 0 else "failed"
        self._finish(job)

    def _finish(self, job: Job) -> None:
        self._log(job, f"[panel] {job.status} (exit {job.returncode})")
        entry = {k: v for k, v in job.to_dict().items() if k != "argv"}
        entry["tail"] = list(job.lines)[-12:]
        self.history = [h for h in self.history if h.get("id") != job.id]
        self.history.append(entry)
        self.history = self.history[-HISTORY_KEEP:]
        self._save_history()
        self.hub.broadcast({"kind": "job", "job": job.to_dict()})

    def _log(self, job: Job, line: str) -> None:
        job.lines.append(line)
        self.hub.broadcast({"kind": "log", "job": job.id, "line": line})

    def _broadcast_jobs(self) -> None:
        self.hub.broadcast({"kind": "jobs", "state": self.state()})

    def state(self) -> dict:
        with self._lock:
            return {
                "current": self.current.to_dict() if self.current else None,
                "pending": [j.to_dict() for j in self.pending],
                "recent": [j.to_dict() for j in self.jobs][-8:],
            }

    # -- small persistence: 'last finished' survives a restart ----------
    def _load_history(self) -> list[dict]:
        try:
            raw = json.loads(self.history_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, list) else []
        except (OSError, ValueError):
            return []

    def _save_history(self) -> None:
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            self.history_path.write_text(
                json.dumps(self.history[-HISTORY_KEEP:], indent=1),
                encoding="utf-8")
        except OSError:
            pass


def _terminate(proc: subprocess.Popen) -> None:
    """Stop a job: SIGTERM to its whole group, then SIGKILL after 8s."""
    try:
        if os.name != "nt":
            os.killpg(os.getpgid(proc.pid), 15)
        else:
            proc.terminate()
    except (OSError, ProcessLookupError):
        return
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(proc.pid), 9)
            else:
                proc.kill()
        except (OSError, ProcessLookupError):
            pass


# ------------------------------------------------------------------ http
def _open_folder(path: Path) -> str:
    """Open a folder in the OS file manager (no shell-out the user typed)."""
    try:
        if os.name == "nt":
            os.startfile(str(path))            # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return f"opened {path}"
    except OSError as exc:
        return f"could not open {path}: {exc}"


def make_handler(cfg, hub: Hub, runner: Runner):
    page = ROOT / "panel.html"

    class Handler(BaseHTTPRequestHandler):
        server_version = "youtpanel/1.0"

        # -- helpers ----------------------------------------------------
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, payload: dict, code: int = 200) -> None:
            self._send(code, json.dumps(payload, ensure_ascii=False,
                                        default=str).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                return {}
            try:
                raw = self.rfile.read(length).decode("utf-8", "replace")
                data = json.loads(raw)
            except (ValueError, OSError):
                return {}
            return data if isinstance(data, dict) else {}

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        # -- GET ---------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            route = urlparse(self.path)
            if route.path in ("/", "/index.html"):
                try:
                    html = page.read_bytes()
                except OSError:
                    self._send(500, b"panel.html is missing next to panel.py",
                               "text/plain; charset=utf-8")
                    return
                self._send(200, html, "text/html; charset=utf-8")
                return
            if route.path == "/events":
                self._events()
                return
            if route.path == "/api/state":
                self._json({"app": APP_MARKER, "token": RUN_TOKEN,
                            "snapshot": snapshot(cfg),
                            "runner": runner.state(),
                            "history": runner.history[-12:],
                            "roles": _roles_payload(),
                            "work_dir": str(cfg.work_dir),
                            "out_dir": str(cfg.out_dir),
                            "sheet_path": str(cfg.sources_sheet)})
                return
            if route.path == "/api/order/options":
                import orders
                from desktop import dconf

                self._json({"channels": orders.channel_list(cfg),
                            "visibility": dconf(cfg)["visibility"],
                            "uploads": dconf(cfg)["uploads"]})
                return
            if route.path == "/api/links":
                self._json({"rows": links_payload(cfg)})
                return
            if route.path == "/api/meetings":
                self._json({"rows": meetings_payload(cfg)})
                return
            if route.path == "/api/minutes":
                name = (parse_qs(route.query).get("name") or [""])[0]
                self._json({"name": name, "text": minutes_text(cfg, name)})
                return
            self._json({"error": "not found"}, 404)

        # -- POST --------------------------------------------------------
        def do_POST(self) -> None:  # noqa: N802
            route = urlparse(self.path).path
            data = self._body()
            if route == "/api/order/plan":
                params = data.get("params") if isinstance(data.get("params"), dict) else {}
                try:
                    self._json(_preview_order(cfg, params))
                except ValueError as exc:
                    self._json({"error": str(exc)}, 400)
                return
            if route == "/api/run":
                action = str(data.get("action") or "")
                params = dict(data.get("params") if isinstance(
                    data.get("params"), dict) else {})
                if action == "order" and params.get("confirm_publish"):
                    okay, why = _consume_order_confirmation(params)
                    if not okay:
                        self._json({"error": why}, 400)
                        return
                try:
                    job = runner.submit(action, params)
                except ValueError as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json({"job": job.to_dict()})
                return
            if route == "/api/stop":
                self._json({"note": runner.stop()})
                return
            if route == "/api/links":
                op = str(data.get("op") or "")
                url = str(data.get("url") or "")
                if op == "add":
                    note = add_link(cfg, url, str(data.get("note") or ""))
                elif op == "drop":
                    note = drop_link(cfg, url)
                elif op == "restore":
                    note = restore_link(cfg, url)
                else:
                    self._json({"error": f"unknown op {op!r}"}, 400)
                    return
                self._json({"note": note, "rows": links_payload(cfg)})
                return
            if route == "/api/chat":
                text = chat_reply(cfg, str(data.get("role") or ""),
                                  str(data.get("text") or ""),
                                  history=data.get("history")
                                  if isinstance(data.get("history"), list)
                                  else [])
                self._json({"text": text})
                return
            if route == "/api/open":
                what = str(data.get("what") or "out")
                folder = cfg.out_dir if what == "out" else cfg.work_dir
                if what == "sheet":
                    note = _open_folder(Path(cfg.sources_sheet).parent)
                elif what == "minutes":
                    note = _open_folder(cfg.out_dir / "meetings")
                else:
                    note = _open_folder(folder)
                self._json({"note": note})
                return
            self._json({"error": "not found"}, 404)

        # -- SSE ---------------------------------------------------------
        def _events(self) -> None:
            client = hub.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                hello = {"kind": "hello", "runner": runner.state()}
                self.wfile.write(sse_frame(hello).encode("utf-8"))
                self.wfile.flush()
                while True:
                    try:
                        payload = client.get(timeout=15)
                    except queue.Empty:
                        payload = {"kind": "ping"}
                    self.wfile.write(sse_frame(payload).encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unsubscribe(client)

    return Handler


def _roles_payload() -> list[dict]:
    try:
        import meeting as mt

        return [{"name": r["name"], "emoji": r["emoji"], "lane": r["lane"]}
                for r in mt.ROLES]
    except Exception:  # noqa: BLE001
        return []


# ------------------------------------------------------------------ launch
# Why this block exists (bug found on the owner's machine, 2026-10-04): he
# also runs an OLDER board project whose window answers on the same port
# range. The old Start Panel.bat trusted a bare "something answers on
# 8765" check, so it skipped starting this panel and opened the OTHER
# project's window. The port file + /api/state marker below make the
# launcher identify THIS process only, and always open the port we
# actually bound (8765, else the next free one).
PORT_FILE = "panel_port.txt"

# Every run of THIS panel gets its own token, written into the port file and
# answered by /api/state. The owner also runs sibling projects on the same
# port range; a lookalike can copy our JSON shape, but it cannot know a
# token that was generated one second ago. Identity, not resemblance.
RUN_TOKEN = ""
APP_MARKER = "youtproject-panel"


def _open_host(host: str) -> str:
    """The host a local browser window should use for a bind address."""
    return "127.0.0.1" if host in ("0.0.0.0", "::", "") else host


def port_file_path(cfg) -> Path:
    return Path(cfg.work_dir) / PORT_FILE


def write_port_file(cfg, port: int) -> None:
    """Record the live port so the next double-click finds THIS panel."""
    try:
        path = port_file_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"port": int(port), "pid": os.getpid(),
                                    "token": RUN_TOKEN}),
                        encoding="utf-8")
    except OSError:
        pass  # a missing port file only costs a cold start, never a crash


def read_port_file(cfg) -> "int | None":
    """The port of our last run — tolerates a bare number and garbage."""
    return read_port_file_full(cfg).get("port")


def read_port_file_full(cfg) -> dict:
    """The port file as a dict: {port, pid, token}. {} when unreadable."""
    try:
        raw = port_file_path(cfg).read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    try:
        data = json.loads(raw)
        port = int(data["port"])
    except Exception:  # noqa: BLE001 - fall through to the bare-int form
        def _int_only() -> dict:
            try:
                return {"port": int(raw)}
            except ValueError:
                return {}
        data = _int_only()
        port = data.get("port")
    if port is None or not 1 <= port <= 65535:
        return {}
    return {"port": port, "pid": data.get("pid"),
            "token": str(data.get("token") or "")}


def clear_port_file(cfg, port: int) -> None:
    """Remove the port file, but only if it still points at `port`."""
    if read_port_file(cfg) != int(port):
        return
    try:
        port_file_path(cfg).unlink()
    except OSError:
        pass


def probe_ours(host: str = "127.0.0.1", port: int = DEFAULT_PORT,
               timeout: float = 0.8, token: str = "") -> bool:
    """True only when what answers on `port` is THIS panel.

    Two locks: the `app` marker (a stranger that only answers JSON fails),
    and — when a token is known from the port file — an exact token match
    (a stranger that copies our shape still fails). A foreign server is
    ignored, never opened as if it were us.
    """
    import urllib.request

    try:
        with urllib.request.urlopen(
                f"http://{_open_host(host)}:{int(port)}/api/state",
                timeout=timeout) as resp:
            body = resp.read(8192)
    except Exception:  # noqa: BLE001 - unreachable/garbage/foreign = not ours
        return False
    if f'"{APP_MARKER}"'.encode() not in body:
        return False
    if token:
        return f'"{token}"'.encode() in body
    return b'"snapshot"' in body and b'"runner"' in body


def _port_live(host: str, port: int, timeout: float = 0.25) -> bool:
    """Something is listening there? A cheap TCP connect — no HTTP."""
    import socket

    try:
        with socket.socket() as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((_open_host(host), int(port))) == 0
    except OSError:
        return False


def find_our_panel(cfg, host: str = "127.0.0.1",
                   start: "int | None" = None, span: int = 11) -> "int | None":
    """The port of a RUNNING panel of ours, or None.

    The port file first (with its token — proof), then the usual range with
    a cheap TCP check before the HTTP probe. This is what lets a double
    click find the panel even when a sibling project holds 8765, and even
    when the port file was lost.
    """
    saved = read_port_file_full(cfg)
    if saved.get("port") and probe_ours(host, saved["port"],
                                        token=saved.get("token") or ""):
        return int(saved["port"])
    begin = int(start or default_port(cfg))
    for candidate in range(begin, begin + span):
        if candidate == saved.get("port"):
            continue
        if _port_live(host, candidate) and probe_ours(
                host, candidate, timeout=0.35):
            return candidate
    return None


def existing_panel_url(cfg, host: str = "127.0.0.1",
                       start: "int | None" = None) -> "str | None":
    """URL of a panel of OURS that is already running, else None."""
    port = find_our_panel(cfg, host, start=start)
    if not port:
        return None
    return f"http://{_open_host(host)}:{port}/"


def default_port(cfg) -> int:
    """The port to try first: `panel.port` in config.yaml, else 8765."""
    try:
        block = (cfg.data or {}).get("panel") or {}
        port = int(block.get("port") or DEFAULT_PORT)
        return port if 1 <= port <= 65535 else DEFAULT_PORT
    except Exception:  # noqa: BLE001 - bad config is not a crash
        return DEFAULT_PORT


def scan_ports(cfg, host: str = "127.0.0.1", span: int = 11) -> list:
    """Every port of the usual range: ours / other / free. Cheap + offline."""
    start = default_port(cfg)
    out = []
    for candidate in range(start, start + span):
        if not _port_live(host, candidate):
            state = "free"
        elif probe_ours(host, candidate, timeout=0.35):
            state = "ours"
        else:
            state = "other"
        out.append({"port": candidate, "state": state})
    return out


def panel_status(cfg, host: str = "127.0.0.1") -> int:
    """`panel --status`: who owns which port and how to open the panel."""
    rows = scan_ports(cfg, host)
    start = default_port(cfg)
    mine = [r["port"] for r in rows if r["state"] == "ours"]
    others = [r["port"] for r in rows if r["state"] == "other"]
    print(f"  [panel] ports {start}..{start + len(rows) - 1} on {_open_host(host)}:")
    for row in rows:
        note = {"ours": "← THIS panel (yours)",
                "other": "held by another program (not this panel)",
                "free": "free"}[row["state"]]
        print(f"    {row['port']}  {note}")
    print("")
    if mine:
        print(f"  [panel] your panel is running — open: "
              f"http://{_open_host(host)}:{mine[0]}/")
        if len(mine) > 1:
            print(f"          (more than one is running: {mine} — that is "
                  "harmless but you can close the extras)")
    else:
        print("  [panel] no panel of ours is running right now. Start it with")
        print("          Desktop: double-click Start Panel.bat   (it picks a "
              "free port itself)")
        print("          Terminal: python main.py panel")
    if others:
        print("")
        print(f"  [panel] port(s) {others} belong to something else (another "
              "project, a dev server).")
        print("          That is exactly why Start Panel.bat may open on a "
              "different port — use the URL above.")
    print("")
    print("  [panel] to always use one port: add to config.yaml")
    print(f"          panel:\n            port: {start}")
    print("          or: python main.py panel --port 9000")
    print("  [panel] to stop a panel you cannot see: Task Manager → end "
          "pythonw.exe (or Ctrl+C in its terminal)")
    return 0


def browser_candidates() -> list:
    """Chromium-family browsers (they support --app=), Edge first on Win."""
    found = []
    if os.name == "nt":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LocalAppData", "")
        for cand in (rf"{pf}\Microsoft\Edge\Application\msedge.exe",
                     rf"{pf86}\Microsoft\Edge\Application\msedge.exe",
                     rf"{pf}\Google\Chrome\Application\chrome.exe",
                     rf"{local}\Google\Chrome\Application\chrome.exe"):
            if cand and Path(cand).is_file():
                found.append(cand)
    for name in ("msedge", "google-chrome", "chromium", "chromium-browser",
                 "chrome"):
        path = shutil.which(name)
        if path and path not in found:
            found.append(path)
    return found


def open_app_window(url: str) -> bool:
    """Open `url` chromeless (Edge/Chrome --app=). False = tab fallback."""
    for exe in browser_candidates():
        try:
            subprocess.Popen([exe, f"--app={url}"],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except OSError:
            continue
    webbrowser.open(url)
    return False


def launch(cfg, host: str = "127.0.0.1", port: "int | None" = None) -> int:
    """`--app-window`: reuse a running panel of ours, else start one and
    open it in a chromeless window. What Start Panel.bat runs."""
    port = int(port or default_port(cfg))
    url = existing_panel_url(cfg, host, start=port)
    if url:
        open_app_window(url)
        return 0
    return serve(cfg, host=host, port=port, open_browser=False,
                 app_window=True)


def _alert(title: str, text: str) -> None:
    """A dialog on Windows (pythonw shows no console), else stderr."""
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
            return
        except Exception:  # noqa: BLE001
            pass
    print(f"{title}: {text}", file=sys.stderr)


# ------------------------------------------------------------------ serve
def serve(cfg, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          open_browser: bool = True, app_window: bool = False) -> int:
    """Start the panel. Returns a process exit code (Ctrl+C -> 130)."""
    global RUN_TOKEN

    RUN_TOKEN = uuid.uuid4().hex[:12]     # this run's identity (see probe_ours)
    hub = Hub()
    history_path = Path(cfg.work_dir) / "panel_history.json"
    runner = Runner(hub, history_path)
    handler = make_handler(cfg, hub, runner)

    httpd = None
    chosen = port
    for candidate in range(port, port + 11):
        try:
            httpd = ThreadingHTTPServer((host, candidate), handler)
            chosen = candidate
            break
        except OSError as exc:
            if candidate == port + 10:
                print(f"  [panel] could not bind {host}:{port}..{candidate} "
                      f"({exc}) — run: python main.py panel --status")
                return 1
    assert httpd is not None
    url = f"http://{_open_host(host)}:{chosen}/"
    write_port_file(cfg, chosen)  # the launcher reads this, not a bare scan
    if chosen != port:
        print(f"  [panel] port {port} was taken (another program?) — using "
              f"{chosen}")
    print(f"  [panel] {url}  (Ctrl+C stops it)")
    print("  [panel] every button runs the real CLI — same behavior as "
          "the terminal")
    print("  [panel] Publish uses the existing local Studio browser lane only "
          "after exact per-job confirmation; desktop.uploads off = stage only.")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("  [panel] WARNING: bound beyond this machine — anyone who "
              "can reach this port can click these buttons")
    if app_window:
        threading.Timer(0.4, lambda: open_app_window(url)).start()
    elif open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  [panel] stopped")
        return 130
    finally:
        httpd.server_close()
        clear_port_file(cfg, chosen)
    return 0


def _main(argv: list[str] | None = None) -> int:
    """Entry point for `python panel.py …` (also what Start Panel.bat runs).

    `python main.py panel` goes through main.py's parser instead; both end
    at serve(). Flags MUST be parsed here — Start Panel.bat runs
    `panel.py --app-window`, which must (a) reuse a running panel of ours,
    (b) tell it apart from any other program on those ports, and (c) show
    a dialog instead of dying silently under pythonw.
    """
    import argparse

    from config import load_config

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", help="path to config.yaml")
    parser.add_argument("--port", type=int, default=None,
                        help=f"port (default: panel.port in config.yaml, "
                             f"else {DEFAULT_PORT})")
    parser.add_argument("--status", action="store_true",
                        help="show who owns each port of the usual range, "
                             "and the URL to open")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (127.0.0.1 = this machine only)")
    parser.add_argument("--no-browser", action="store_true", dest="no_browser",
                        help="don't open the browser window")
    parser.add_argument("--app-window", action="store_true", dest="app_window",
                        help="reuse a running panel or start one, then open "
                             "it as a chromeless Edge/Chrome window (what "
                             "Start Panel.bat runs)")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if args.status:
        return panel_status(cfg, host=args.host)
    if args.app_window:
        try:
            rc = launch(cfg, host=args.host, port=args.port)
        except Exception as exc:  # noqa: BLE001 - pythonw has no console
            _alert("Start Panel",
                   "The panel could not start:\n\n"
                   f"{exc!r}\n\nRun this in a terminal to see the details:"
                   "\n\n    venv\\Scripts\\python.exe panel.py --app-window")
            return 1
        if rc not in (0, 130):
            # pythonw has no console: a silent exit looks like "nothing
            # happened" and the owner falls back to typing URLs by hand.
            _alert("Start Panel",
                   "The panel could not start (exit " + str(rc) + ").\n\n"
                   "Run this in a terminal to see the details:\n\n"
                   "    venv\\Scripts\\python.exe panel.py --app-window\n\n"
                   "or: python main.py panel --status")
        return rc
    return serve(cfg, host=args.host, port=args.port or default_port(cfg),
                 open_browser=not args.no_browser)


if __name__ == "__main__":  # pragma: no cover - thin convenience shim
    raise SystemExit(_main())
