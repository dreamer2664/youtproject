"""The desktop agent — AI takes the mouse, in the user's own browser.

This is the lane that does what the Data API cannot: it opens real pages,
click, types, reads, and screenshots what it sees. Two backends:

  * "chrome"  — connects to the user's REAL browser over the DevTools port
                (`Desktop Chrome.bat` starts it — any Chromium browser: Opera
                GX, Edge, Chrome, Brave, Vivaldi). The window is visible: the
                user watches the AI work. Logins live in that profile, so
                Studio is reachable when the user says so.
  * "browser" — a private headless Chromium (no logins, read-only work).

Design rules, kept deliberately strict:

  * playwright is imported lazily — the offline suite and CI never need it;
  * every navigation is checked against `desktop.allowed_domains`;
  * clicking something that publishes/schedules/deletes/buys is a COMMIT and
    is refused unless `desktop.uploads: on` (publish family) or is in the
    banned family (always refused; no config opens it);
  * dry-run does everything except the commit click;
  * every step is logged (JSONL) and screenshotted, so nothing is invisible;
  * mistakes become lessons (one sentence, stored, injected into the next
    prompt) and successes become playbooks (rehearsal cache, replayed first).

Nothing in this module can upload by itself — `post_one()` is the only
commit path and it is gated by `desktop.uploads: on` plus an explicit order.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

# --------------------------------------------------------------------------
# defaults & shared state

DEFAULT_ALLOWED = ["youtube.com", "google.com", "googleusercontent.com"]

# publish family — needs desktop.uploads: on. Studio speaks the ACCOUNT's
# language: the owner's UI is Italian, where the publish button says
# "Pubblica". Missing that spelling would have let a publish click through
# with uploads off — English alone is not a safety net.
COMMIT_WORDS = (
    "publish", "schedule", "go live", "post", "upload", "save", "submit",
    "confirm", "send", "make public", "set public",
    # Italian (and close cousins): publish / schedule / save / upload
    "pubblica", "programma", "pianifica", "salva", "carica", "invia",
)
# never allowed, whatever the config says
BANNED_WORDS = (
    "delete", "remove", "trash", "buy", "purchase", "pay", "checkout",
    "subscribe", "unsubscribe", "cancel", "close account", "sign out",
    # Italian: delete / remove / buy / subscribe / cancel
    "elimina", "rimuovi", "cancella", "acquista", "abbonati", "disdici",
    "annulla",
)

MAX_ELEMENTS = 120
TEXT_DIGEST_CHARS = 3000
LESSON_CAP = 200
LESSONS_IN_PROMPT = 6
PLAYBOOK_CAP = 40
PLAYBOOK_FAIL_LIMIT = 3
KEEP_RUN_DAYS = 14

_STOP = threading.Event()


def stop_requested() -> bool:
    return _STOP.is_set()


def request_stop() -> None:
    _STOP.set()


def clear_stop() -> None:
    _STOP.clear()


# --------------------------------------------------------------------------
# config & paths


def settings_path(cfg) -> Path:
    return cfg.root / "work" / "desktop" / "settings.json"


def set_setting(cfg, key: str, value) -> None:
    """Runtime override (the bot's /desk uploads on) — kept OUT of config.yaml."""
    data = _load_json(settings_path(cfg), {})
    if not isinstance(data, dict):
        data = {}
    data[str(key)] = value
    _save_json(settings_path(cfg), data)


def channels_path(cfg) -> Path:
    return work_dir(cfg) / "channels.json"


_UC_RE = re.compile(r"(UC[A-Za-z0-9_\-]{20,24})")


def normalize_studio_url(ref: str) -> str:
    """Turn any acceptable channel reference into a Studio upload base URL.

    Accepts https://studio.youtube.com/channel/UC… ,
    https://www.youtube.com/channel/UC… and a bare UC… id. Returns "" for
    anything else (handles need a lookup the CLI should not fake).
    """
    text = (ref or "").strip()
    if not text:
        return ""
    match = _UC_RE.search(text)
    if match:
        return f"https://studio.youtube.com/channel/{match.group(1)}"
    return ""


def load_extra_channels(cfg) -> list[dict]:
    """Channels added at runtime (`desktop channels add`) — config untouched."""
    data = _load_json(channels_path(cfg), [])
    if not isinstance(data, list):
        return []
    out = []
    for entry in data:
        if isinstance(entry, dict) and entry.get("name") and entry.get("id"):
            out.append({"name": str(entry["name"]), "id": str(entry["id"]),
                        "studio_url": str(entry.get("studio_url") or ""),
                        "added": str(entry.get("added") or "")})
    return out


def _save_extra_channels(cfg, entries: list[dict]) -> None:
    _save_json(channels_path(cfg), entries)


def add_extra_channel(cfg, name: str, ref: str) -> tuple[bool, str]:
    """Add one channel by name + URL/id. (ok, message)."""
    name = re.sub(r"\s+", " ", (name or "").strip())[:60]
    if not name:
        return False, "give the channel a name, e.g. desktop channels add \"MicroFeed-0\" <url>"
    match = _UC_RE.search(ref or "")
    if not match:
        return False, ("no channel id found — paste the Studio link "
                       "(https://studio.youtube.com/channel/UC…), the public "
                       "channel link, or the bare UC… id")
    cid = match.group(1)
    studio = normalize_studio_url(cid)
    entries = load_extra_channels(cfg)
    for entry in entries:
        if entry["id"] == cid:
            entry["name"], entry["studio_url"] = name, studio
            _save_extra_channels(cfg, entries)
            return True, f"updated {name} ({cid})"
    entries.append({"name": name, "id": cid, "studio_url": studio,
                    "added": time.strftime("%Y-%m-%dT%H:%M")})
    _save_extra_channels(cfg, entries)
    return True, f"added {name} ({cid})"


def remove_extra_channel(cfg, needle: str) -> tuple[bool, str]:
    needle = (needle or "").strip().lower()
    if not needle:
        return False, "usage: desktop channels remove <name or UC…>"
    entries = load_extra_channels(cfg)
    kept = [e for e in entries
            if needle not in e["name"].lower() and needle not in e["id"].lower()]
    if len(kept) == len(entries):
        return False, f"nothing in the runtime list matches {needle!r}"
    _save_extra_channels(cfg, kept)
    return True, f"removed {len(entries) - len(kept)} channel(s)"


def all_channels(cfg) -> list[dict]:
    """Config channels first, then runtime ones — deduped by channel id.

    Config entries win on a clash (they are the durable home), and the
    runtime store makes `desktop channels add` work without touching
    config.yaml at all.
    """
    merged: list[dict] = []
    seen: set[str] = set()
    raw = (cfg.data.get("desktop") or {}) if getattr(cfg, "data", None) else {}
    for entry in (raw.get("channels") or []):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("handle") or "").strip()
        url = str(entry.get("studio_url") or "").strip()
        match = _UC_RE.search(url)
        cid = match.group(1) if match else ""
        if not name and not cid:
            continue
        # identity: the UC id when there is one, else the NAME. Never the URL
        # — placeholder/base studio URLs repeat across channels and deduping
        # on them silently ate every channel after the first.
        key = cid or ("name:" + name.lower())
        if key in seen:
            continue
        seen.add(key)
        merged.append({"name": name or cid, "id": cid, "studio_url": url,
                       "source": "config"})
    for entry in load_extra_channels(cfg):
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        merged.append({**entry, "source": "runtime"})
    return merged


def dconf(cfg) -> dict:
    """The `desktop:` config block with safe defaults (never crashes).

    Precedence: work/desktop/settings.json (runtime override, e.g.
    `/desk uploads on`) wins over config.yaml, which wins over the defaults.
    """
    raw = (cfg.data.get("desktop") or {}) if getattr(cfg, "data", None) else {}
    if isinstance(raw, dict) and getattr(cfg, "root", None):
        override = _load_json(settings_path(cfg), {})
        if isinstance(override, dict):
            raw = {**raw, **{k: v for k, v in override.items()
                             if v is not None}}
    return {
        "backend": str(raw.get("backend") or "chrome").strip().lower(),
        "cdp_url": str(raw.get("cdp_url") or "http://127.0.0.1:9222").strip(),
        "allowed_domains": [str(d).strip().lower()
                            for d in (raw.get("allowed_domains")
                                      or DEFAULT_ALLOWED) if str(d).strip()],
        "uploads": str(raw.get("uploads") or "off").strip().lower() in
                   ("on", "true", "1", "yes"),
        "visibility": str(raw.get("visibility") or "unlisted").strip().lower(),
        "vision": str(raw.get("vision") or "off").strip().lower() in
                  ("on", "true", "1", "yes"),
        "max_steps": int(raw.get("max_steps") or 25),
        "channels": [c for c in (raw.get("channels") or [])
                     if isinstance(c, dict)],
    }


def work_dir(cfg) -> Path:
    return cfg.root / "work" / "desktop"


def lessons_path(cfg) -> Path:
    return work_dir(cfg) / "lessons.json"


def playbooks_path(cfg) -> Path:
    return work_dir(cfg) / "playbooks.json"


def _load_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - missing/corrupt = fresh start
        return fallback


def _save_json(path: Path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False),
                        encoding="utf-8")
    except OSError:
        pass


def new_run(cfg) -> tuple[str, Path, Path]:
    """(run_id, log_path, shots_dir) for one task run."""
    run_id = time.strftime("%Y%m%d-%H%M%S")
    base = work_dir(cfg)
    log = base / "logs" / f"{run_id}.jsonl"
    shots = base / "shots" / run_id
    log.parent.mkdir(parents=True, exist_ok=True)
    shots.mkdir(parents=True, exist_ok=True)
    prune_runs(cfg)
    return run_id, log, shots


def prune_runs(cfg, keep_days: int = KEEP_RUN_DAYS) -> None:
    """Drop logs/screenshots older than keep_days (best effort)."""
    cutoff = time.time() - keep_days * 86400
    for folder in (work_dir(cfg) / "logs", work_dir(cfg) / "shots"):
        try:
            for child in folder.iterdir():
                try:
                    if child.stat().st_mtime < cutoff:
                        shutil.rmtree(child) if child.is_dir() else child.unlink()
                except OSError:
                    continue
        except OSError:
            continue


class RunLog:
    """Append-only JSONL of everything the agent did (one line per step)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def add(self, **fields) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"t": time.strftime("%H:%M:%S"),
                                         **fields},
                                        ensure_ascii=False) + "\n")
        except OSError:
            pass


# --------------------------------------------------------------------------
# safety


def host_of(url: str) -> str:
    match = re.match(r"https?://([^/]+)", (url or "").strip(), re.I)
    host = (match.group(1) if match else "").lower()
    return host.split(":")[0].removeprefix("www.")


def domain_allowed(cfg, url: str) -> tuple[bool, str]:
    """Allowlist check for a URL. (ok, reason)."""
    text = (url or "").strip()
    if not text:
        return False, "empty url"
    if not text.lower().startswith(("http://", "https://")):
        return False, "only http(s) urls are allowed"
    host = host_of(text)
    if not host:
        return False, "no host in url"
    allowed = dconf(cfg)["allowed_domains"]
    for entry in allowed:
        if host == entry or host.endswith("." + entry):
            return True, ""
    return False, (f"{host} is not in desktop.allowed_domains "
                   f"({', '.join(allowed)})")


def classify_click(cfg, element_name: str, action: dict) -> tuple[str, str]:
    """'safe' | 'commit' | 'banned' for one click. (kind, reason)."""
    name = (element_name or "").lower()
    if any(word in name for word in BANNED_WORDS):
        return "banned", f"'{element_name}' looks destructive (delete/buy/...)"
    commitish = bool(action.get("commit")) or any(
        word in name for word in COMMIT_WORDS)
    if commitish:
        if not dconf(cfg)["uploads"]:
            return "commit", ("commit click needs desktop.uploads: on "
                              "(publish/schedule/save)")
        return "commit-ok", "publish enabled (desktop.uploads: on)"
    return "safe", ""


# --------------------------------------------------------------------------
# drivers


class BaseDriver:
    """Tiny duck-typed interface; the offline suite implements this too."""

    def start(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def goto(self, url: str) -> None:
        raise NotImplementedError

    def url(self) -> str:
        raise NotImplementedError

    def title(self) -> str:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def click(self, target) -> None:
        raise NotImplementedError

    def fill(self, target, text: str) -> None:
        raise NotImplementedError

    def press(self, key: str) -> None:
        raise NotImplementedError

    def scroll(self, dy: int) -> None:
        raise NotImplementedError

    def read(self, target=None) -> str:
        raise NotImplementedError

    def open_tab(self, url: str) -> None:
        raise NotImplementedError

    def switch_tab(self, index: int) -> None:
        raise NotImplementedError

    def close_tab(self, index: int) -> None:
        raise NotImplementedError

    def set_input_files(self, target, path: str) -> None:
        raise NotImplementedError

    def element_name(self, target) -> str:
        """Human name of the element an action would touch ('' if none)."""
        return ""

    def screenshot(self, path: Path) -> str:
        raise NotImplementedError


_SNAPSHOT_JS = """
() => {
 try {
  const out = [];
  const els = document.querySelectorAll(
    'a, button, input, textarea, select, [role="button"], [role="tab"],' +
    ' [contenteditable="true"], label');
  let i = 0;
  for (const el of els) {
    let r; try { r = el.getBoundingClientRect(); } catch (e) { continue; }
    const cs = window.getComputedStyle(el);
    if (!r || r.width < 2 || r.height < 2) continue;
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    let name = (el.getAttribute('aria-label') || el.innerText || el.value ||
                el.placeholder || el.title || '').trim()
               .replace(/\\s+/g, ' ').slice(0, 90);
    if (!name && el.tagName === 'INPUT' && el.type === 'file') {
      name = '(file upload)';   // Studio's file input has no label at all
    }
    if (!name) continue;
    try { el.setAttribute('data-desk-idx', String(i)); } catch (e) {}
    out.push({i: i, tag: el.tagName.toLowerCase(),
              role: el.getAttribute('role') || el.tagName.toLowerCase(),
              name: name, disabled: !!el.disabled,
              id: (el.id || '').slice(0, 40),
              attr: (el.getAttribute('name') || '').slice(0, 40)});
    i += 1;
    if (i >= %d) break;
  }
  let text = '';
  try { text = (document.body.innerText || '').replace(/\\s+/g, ' ').trim(); }
  catch (e) {}
  return {elements: out, text: text.slice(0, %d)};
 } catch (err) {
  return {elements: [], text: '', why: String(err).slice(0, 200)};
 }
}
""" % (MAX_ELEMENTS, TEXT_DIGEST_CHARS)


class PlayDriver(BaseDriver):
    """Real browser via Playwright (lazy import — CI never sees it)."""

    def __init__(self, cfg, backend: str | None = None,
                 timeout_ms: int | None = None) -> None:
        self.cfg = cfg
        self.backend = (backend or dconf(cfg)["backend"] or "chrome").lower()
        self._timeout_ms = int(timeout_ms) if timeout_ms else None
        # Playwright's page.evaluate() takes NO timeout - that is what hung
        # `desktop shot` on a sleeping tab. Everything page-shaped goes
        # through bounded primitives instead (see probe/snapshot below).
        self._probe_ms = 4000      # "is the page awake?"
        self._snap_ms = 8000       # reading a big page
        self._shot_ms = 15000      # a real screenshot, PNG bytes over CDP
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._snap: dict = {"elements": [], "text": ""}

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "playwright is not installed — run: "
                "python -m pip install -r requirements.txt   then: "
                "python main.py desktop setup"
            ) from exc
        self._pw = sync_playwright().start()
        if self.backend == "chrome":
            url = dconf(self.cfg)["cdp_url"]
            try:
                kwargs = {"timeout": self._timeout_ms or 6000}
                self._browser = self._pw.chromium.connect_over_cdp(url, **kwargs)
            except Exception as exc:  # noqa: BLE001 - say the RIGHT fix
                message, busy = _reach_error(url, exc)
                raise (BrowserBusy if busy else RuntimeError)(message) from exc
            contexts = self._browser.contexts
            if not contexts or not contexts[0].pages:
                # Playwright issue #21812: Opera (and some Chromium builds)
                # CRASH when Playwright asks a CDP-connected browser for a
                # new page/context. Never do that to the user's browser —
                # ask them for a tab instead.
                raise RuntimeError(
                    "your browser is running but has no open tab — open one "
                    "(studio.youtube.com is a good start) and try again")
            self._context = contexts[0]
            self._page = self._context.pages[0]
        else:
            self._browser = self._pw.chromium.launch(headless=True)
            self._context = self._browser.new_context(
                locale="en-US", viewport={"width": 1280, "height": 900},
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})
            self._page = self._context.new_page()
        try:  # clicks/fills/navigation get a real ceiling too
            self._page.set_default_timeout(15000)
            self._page.set_default_navigation_timeout(30000)
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            if self.backend == "browser" and self._browser is not None:
                self._browser.close()
            # backend "chrome": never kill the user's browser, just disconnect
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._browser = self._context = self._page = self._pw = None

    # -- navigation & snapshot ------------------------------------------
    def goto(self, url: str) -> None:
        self._page.goto(url, timeout=45000, wait_until="domcontentloaded")
        self._page.wait_for_timeout(1200)

    def url(self) -> str:
        return self._page.url if self._page else ""

    def title(self) -> str:
        try:
            return self._page.title()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _one_line(exc) -> str:
        text = str(exc).strip()
        line = text.splitlines()[0] if text else exc.__class__.__name__
        return line[:200]

    @staticmethod
    def title_if_fast(info: dict) -> str:
        return str(info.get("title") or "")

    def probe(self) -> dict:
        """Liveness + title of the current page, both BOUNDED. Never raises.

        `page.title()` and `page.evaluate()` have no timeout in Playwright:
        on a tab that is asleep, crashed, or mid-reload they can block
        forever with no output. `wait_for_function` is an action and DOES
        take a timeout, so it is the primitive everything here builds on.
        """
        out = {"url": "", "title": "", "alive": False, "reason": ""}
        try:
            out["url"] = self._page.url or ""
        except Exception:  # noqa: BLE001
            pass
        try:
            handle = self._page.wait_for_function(
                "() => (document.title || 'about:blank')",
                timeout=self._probe_ms, polling=250)
            try:
                out["title"] = str(handle.json_value() or "")
            finally:
                try:
                    handle.dispose()
                except Exception:  # noqa: BLE001
                    pass
            out["alive"] = True
        except Exception as exc:  # noqa: BLE001 - a dead tab is a fact
            out["reason"] = self._one_line(exc)
        return out

    def snapshot(self) -> dict:
        """Indexed visible elements + text + title, BOUNDED.

        Ran through `wait_for_function` because `page.evaluate` cannot time
        out; a page that never answers returns a clear sentence instead of
        hanging the command.
        """
        info = self.probe()
        url, title = info["url"], self.title_if_fast(info)
        if not info["alive"]:
            self._snap = {
                "url": url, "title": "", "elements": [], "text": "",
                "degraded": True,
                "reason": (f"the page did not answer in "
                           f"{self._probe_ms // 1000}s "
                           f"({info['reason']}) - it is probably asleep, "
                           f"busy, or mid-reload; click the tab once and "
                           f"retry")}
            return self._snap
        data = None
        last = ""
        for attempt in range(2):
            try:
                handle = self._page.wait_for_function(
                    _SNAPSHOT_JS, timeout=self._snap_ms, polling=250)
                try:
                    data = handle.json_value()
                finally:
                    try:
                        handle.dispose()
                    except Exception:  # noqa: BLE001
                        pass
                break
            except Exception as exc:  # noqa: BLE001
                last = self._one_line(exc)
                if attempt == 0:
                    time.sleep(0.6)   # it was probably just navigating
        if not isinstance(data, dict):
            self._snap = {
                "url": url, "title": title, "elements": [], "text": "",
                "degraded": True,
                "reason": (f"the page kept navigating or its JS never "
                           f"answered within {self._snap_ms // 1000}s "
                           f"({last}) - bring the tab to the front and "
                           f"retry")}
            return self._snap
        if data.get("why"):
            self._snap = {
                "url": url, "title": title, "elements": [], "text": "",
                "degraded": True,
                "reason": f"the page refused to be read: {data['why']}"}
            return self._snap
        self._snap = {"url": url, "title": title,
                      "elements": data.get("elements") or [],
                      "text": data.get("text") or ""}
        return self._snap

    def _locator(self, target):
        """Element by snapshot index (tagged) or a CSS/text= selector.

        The snapshot tags every VISIBLE element with data-desk-idx = its
        index, so `target: 5` always means the same element the model saw —
        the old handle-list approach drifted whenever hidden elements
        matched the selector.
        """
        if isinstance(target, int):
            return self._page.locator(f'[data-desk-idx="{target}"]').first
        if isinstance(target, str):
            if target.startswith("text="):
                return self._page.get_by_text(target[5:], exact=False).first
            return self._page.locator(target).first
        raise RuntimeError(f"bad target: {target!r}")

    def click(self, target) -> None:
        self._locator(target).click(timeout=15000)

    def fill(self, target, text: str) -> None:
        locator = self._locator(target)
        try:
            locator.fill("", timeout=15000)
        except Exception:  # noqa: BLE001 - some editors refuse empty fill
            pass
        locator.fill(text, timeout=15000)

    def press(self, key: str) -> None:
        self._page.keyboard.press(key)

    def scroll(self, dy: int) -> None:
        self._page.mouse.wheel(0, int(dy))
        self._page.wait_for_timeout(600)

    def read(self, target=None) -> str:
        if target is None:
            return self.snapshot()["text"]
        try:
            return self._locator(target).inner_text(timeout=10000)
        except Exception:  # noqa: BLE001
            return ""

    def open_tab(self, url: str) -> None:
        """Open a tab — or reuse the current one when CDP-connected.

        `new_page()` against the user's own browser is the exact call that
        crashes Opera (playwright#21812). Crashing their browser to honor a
        "new tab" nicety is a bad trade, so the real-browser backend
        navigates the open tab instead; the headless backend still opens a
        real tab.
        """
        if self.backend == "chrome":
            self.goto(url)
            return
        self._page = self._context.new_page()
        self.goto(url)

    def switch_tab(self, index: int) -> None:
        pages = self._context.pages
        if 0 <= index < len(pages):
            self._page = pages[index]
            self._page.bring_to_front()

    def close_tab(self, index: int) -> None:
        pages = self._context.pages
        if 0 <= index < len(pages) and len(pages) > 1:
            try:
                pages[index].close()
            except Exception:  # noqa: BLE001 - never crash the user's browser
                return
            self._page = self._context.pages[0]

    def tabs(self) -> int:
        return len(self._context.pages)

    def set_input_files(self, target, path: str) -> None:
        self._locator(target).set_input_files(path, timeout=30000)

    def element_name(self, target) -> str:
        if isinstance(target, int):
            for el in (self._snap.get("elements") or []):
                if el.get("i") == target:
                    return str(el.get("name") or "")
            return ""
        return str(target)

    def screenshot(self, path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._page.screenshot(path=str(path), full_page=False,
                              timeout=self._shot_ms, animations="disabled")
        return str(path)


def make_driver(cfg, backend: str | None = None) -> BaseDriver:
    return PlayDriver(cfg, backend=backend)


def find_browsers() -> list[dict]:
    """Chromium-based browsers this machine has, best-effort (never raises).

    Any of them can drive the desktop lane — Chrome, Opera GX, Edge, Brave,
    Vivaldi, plain Chromium. Edge is the reliable fallback on Windows
    because it ships with the OS.
    """
    import os
    import shutil

    found: list[dict] = []
    seen: set[str] = set()

    def add(name: str, path: str) -> None:
        try:
            real = os.path.expandvars(os.path.expanduser(path))
        except Exception:  # noqa: BLE001
            return
        key = real.lower()
        if key in seen or not os.path.exists(real):
            return
        seen.add(key)
        found.append({"name": name, "path": real})

    local = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("PROGRAMFILES", "")
    pf86 = os.environ.get("PROGRAMFILES(X86)", "")
    windows_candidates = [
        ("Opera GX", os.path.join(local, "Programs", "Opera GX", "opera.exe")),
        ("Opera GX", os.path.join(local, "Programs", "Opera GX", "launcher.exe")),
        ("Opera GX", os.path.join(pf, "Opera GX", "opera.exe")),
        ("Opera", os.path.join(local, "Programs", "Opera", "opera.exe")),
        ("Opera", os.path.join(local, "Programs", "Opera", "launcher.exe")),
        ("Opera", os.path.join(pf, "Opera", "launcher.exe")),
        ("Edge", os.path.join(pf86, "Microsoft", "Edge", "Application",
                              "msedge.exe")),
        ("Edge", os.path.join(pf, "Microsoft", "Edge", "Application",
                              "msedge.exe")),
        ("Chrome", os.path.join(pf, "Google", "Chrome", "Application",
                                "chrome.exe")),
        ("Chrome", os.path.join(pf86, "Google", "Chrome", "Application",
                                "chrome.exe")),
        ("Chrome", os.path.join(local, "Google", "Chrome", "Application",
                                "chrome.exe")),
        ("Brave", os.path.join(pf, "BraveSoftware", "Brave-Browser",
                               "Application", "brave.exe")),
        ("Brave", os.path.join(local, "Programs", "BraveSoftware",
                               "Brave-Browser", "Application", "brave.exe")),
        ("Vivaldi", os.path.join(local, "Vivaldi", "Application",
                                 "vivaldi.exe")),
        ("Vivaldi", os.path.join(pf, "Vivaldi", "Application",
                                 "vivaldi.exe")),
    ]
    for name, path in windows_candidates:
        add(name, path)
    for name, binary in (("Chromium", "chromium"), ("Chrome", "google-chrome"),
                         ("Opera", "opera"), ("Edge", "microsoft-edge"),
                         ("Brave", "brave-browser"), ("Vivaldi", "vivaldi")):
        where = shutil.which(binary)
        if where:
            add(name, where)
    for name, path in (
            ("Chrome", "/Applications/Google Chrome.app/Contents/MacOS/"
                       "Google Chrome"),
            ("Edge", "/Applications/Microsoft Edge.app/Contents/MacOS/"
                     "Microsoft Edge"),
            ("Opera GX", "/Applications/Opera GX.app/Contents/MacOS/"
                         "Opera GX"),
            ("Brave", "/Applications/Brave Browser.app/Contents/MacOS/"
                      "Brave Browser")):
        add(name, path)
    return found


# --------------------------------------------------------------------------
# lessons & playbooks


def _goal_key(goal: str) -> str:
    words = re.findall(r"[a-z0-9]+", (goal or "").lower())
    stop = {"the", "a", "an", "to", "of", "and", "in", "on", "for", "my"}
    return " ".join(w for w in words if w not in stop)[:80]


def load_lessons(cfg) -> list[dict]:
    data = _load_json(lessons_path(cfg), [])
    return data if isinstance(data, list) else []


def add_lesson(cfg, goal: str, url: str, text: str) -> None:
    """Record one hard-won sentence; dedupe, cap, timestamp."""
    text = re.sub(r"\s+", " ", (text or "").strip())[:300]
    if not text:
        return
    host = host_of(url)
    entries = load_lessons(cfg)
    for entry in entries:
        if entry.get("text") == text:
            entry["when"] = time.strftime("%Y-%m-%dT%H:%M")
            _save_json(lessons_path(cfg), entries)
            return
    entries.append({"goal": _goal_key(goal), "host": host, "text": text,
                    "when": time.strftime("%Y-%m-%dT%H:%M")})
    _save_json(lessons_path(cfg), entries[-LESSON_CAP:])


def lessons_for(cfg, goal: str, url: str) -> list[str]:
    """Most relevant lessons: same host first, then same goal, then recent."""
    host = host_of(url)
    key = _goal_key(goal)
    same_host, same_goal, other = [], [], []
    for entry in reversed(load_lessons(cfg)):
        text = str(entry.get("text") or "")
        if not text:
            continue
        if entry.get("host") and entry["host"] == host:
            same_host.append(text)
        elif entry.get("goal") and entry["goal"] == key:
            same_goal.append(text)
        else:
            other.append(text)
    picked = (same_host + same_goal + other)[:LESSONS_IN_PROMPT]
    return picked


def load_playbooks(cfg) -> list[dict]:
    data = _load_json(playbooks_path(cfg), [])
    return data if isinstance(data, list) else []


def find_playbook(cfg, goal: str, url: str) -> dict | None:
    key, host = _goal_key(goal), host_of(url)
    best = None
    for entry in load_playbooks(cfg):
        if entry.get("goal") == key and entry.get("host") == host:
            if best is None or entry.get("when", "") > best.get("when", ""):
                best = entry
    if best and int(best.get("failures") or 0) < PLAYBOOK_FAIL_LIMIT:
        return best
    return None


def record_playbook(cfg, goal: str, url: str, steps: list[dict]) -> None:
    """Save a winning action path (targets as role+name, not indexes)."""
    steps = [{"action": s.get("action"), "target_name": s.get("target_name"),
              "target_role": s.get("target_role"), "url": s.get("url"),
              "text": s.get("text"), "key": s.get("key"),
              "why": s.get("why")} for s in steps if s.get("action")]
    if not steps:
        return
    key, host = _goal_key(goal), host_of(url)
    entries = [e for e in load_playbooks(cfg)
               if not (e.get("goal") == key and e.get("host") == host)]
    entries.append({"goal": key, "host": host, "steps": steps,
                    "failures": 0, "when": time.strftime("%Y-%m-%dT%H:%M")})
    _save_json(playbooks_path(cfg), entries[-PLAYBOOK_CAP:])


def _bump_playbook_failure(cfg, playbook: dict) -> None:
    entries = load_playbooks(cfg)
    for entry in entries:
        if (entry.get("goal") == playbook.get("goal")
                and entry.get("host") == playbook.get("host")
                and entry.get("when") == playbook.get("when")):
            entry["failures"] = int(entry.get("failures") or 0) + 1
    _save_json(playbooks_path(cfg), entries)


# Studio runs in the ACCOUNT's language (the owner's is Italian), so every
# deterministic control matches several spellings. Matching tries exact names
# first, then substrings, so English keeps working exactly as before.
LABELS = {
    "next": ("Next", "Avanti"),
    "title": ("Add a title", "title", "Titolo", "Aggiungi un titolo"),
    "description": ("Add a description", "description", "Descrizione",
                    "Aggiungi una descrizione"),
    "public": ("Public", "Pubblica", "Pubblico", "PUBLIC"),
    "private": ("Private", "Privato", "PRIVATE"),
    "unlisted": ("Unlisted", "Non elencato", "Non in elenco", "UNLISTED"),
    "publish": ("Publish", "Pubblica"),
    "schedule": ("Schedule", "Programma", "Pianifica"),
    "save": ("Save", "Salva", "Salva come bozza"),
}


def _seen_controls(snapshot: dict, limit: int = 10) -> str:
    """What the page actually showed — so a failure can be pasted back."""
    names: list[str] = []
    for el in (snapshot.get("elements") or []):
        if el.get("tag") not in ("button", "a", "input", "label"):
            continue
        name = str(el.get("name") or "").strip()[:40]
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return ", ".join(names) or "(nothing readable)"


def _resolve_named(snapshot: dict, name, role: str = "", last: bool = False,
                   attr: bool = False) -> int | None:
    """Find the index of an element by name (exact first, then substring).

    `name` may be one spelling or a tuple of them (LABELS: English +
    Italian). `last=True` prefers the BOTTOM-most match — in Italian the
    visibility radio and the publish button are both "Pubblica", and the
    button is always further down the dialog. `attr=True` also matches the
    id/name attributes, which are language-independent (Studio's radios
    carry name="PUBLIC" whatever the label says).
    """
    elements = snapshot.get("elements") or []
    wanted = [str(w).strip().lower()
              for w in ((name,) if isinstance(name, str) else name)]
    wanted = [w for w in wanted if w]
    if not wanted:
        return None

    def hit(el: dict, exact: bool) -> bool:
        keys = [(el.get("name") or "").lower()]
        if attr:
            keys += [(el.get("id") or "").lower(),
                     (el.get("attr") or "").lower()]
        for want in wanted:
            for key in keys:
                if not key:
                    continue
                if (key == want) if exact else (want in key):
                    return True
        return False

    found = [el for el in elements if hit(el, True)]
    if not found:
        found = [el for el in elements if hit(el, False)
                 and (not role or el.get("role") == role
                      or el.get("tag") == role)]
    if not found:
        return None
    return int(found[-1 if last else 0]["i"])


# --------------------------------------------------------------------------
# the agent loop


def _brain(cfg, model: Callable[[str], str] | None):
    if model is not None:
        return model

    def ask(prompt: str) -> str:
        from scriptgen import get_provider

        return get_provider(cfg).generate_text(
            prompt, temperature=0.2, tag="desktop", json_mode=True)

    return ask


def _step_prompt(cfg, goal: str, snap: dict, lessons: list[str],
                 history: list[str], remaining: int) -> str:
    lines = [f"0: {el['role']} \"{el['name']}\"" +
             (" (disabled)" if el.get("disabled") else "")
             for el in (snap.get("elements") or [])[:MAX_ELEMENTS]]
    hist = "\n".join(f"- {h}" for h in history[-10:]) or "(none yet)"
    lesson_block = ("\n".join(f"- {l}" for l in lessons)
                    or "(none — this is the first attempt)")
    return f"""You control a real web browser on the user's own PC.
GOAL: {goal}

You get ONE action per reply. Reply with a single JSON object, nothing else:
  {{"action": "goto|click|fill|press|scroll|wait|read|screenshot|open_tab|switch_tab|close_tab|set_file|done",
    "url": "...", "target": 3, "text": "...", "key": "Enter", "seconds": 2,
    "path": "C:/...", "why": "short reason", "ok": true, "summary": "..."}}

Rules:
- `done` ends the task: include "ok": true/false and a one-line "summary".
- prefer clicking by element index (number) from the list below;
- do not invent elements — if what you need is missing, scroll, wait, or
  report done ok=false with what is missing;
- clicking anything that publishes/schedules/deletes/buys is refused by the
  safety layer unless the user enabled it; do not try to work around that.

Current page: {snap.get('url') or '(none)'} — {snap.get('title') or ''}
Interactive elements (index: role "name"):
{chr(10).join(lines) or '(none)'}

Visible text (trimmed):
{(snap.get('text') or '')[:1800]}

Recent actions:
{hist}

Lessons learned from past attempts (trust these):
{lesson_block}

Steps left: {remaining}. Next action (JSON only):"""


def parse_action(raw: str) -> dict | None:
    """Pull one action object out of a model reply (never crashes)."""
    text = (raw or "").strip()
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    action = str(data.get("action") or "").strip().lower()
    if action not in ("goto", "click", "fill", "press", "scroll", "wait",
                      "read", "screenshot", "open_tab", "switch_tab",
                      "close_tab", "set_file", "done"):
        return None
    return data


def execute_action(cfg, driver: BaseDriver, action: dict, dry_run: bool,
                   log: RunLog, shots: Path, step: int,
                   echo=print) -> tuple[bool, str, dict]:
    """Run one action with the gates. Returns (ok, note, resolved_info)."""
    kind = str(action.get("action") or "").lower()
    target = action.get("target")
    info: dict[str, Any] = {}
    try:
        if kind in ("goto", "open_tab"):
            url = str(action.get("url") or "").strip()
            ok, reason = domain_allowed(cfg, url)
            if not ok:
                return False, f"blocked: {reason}", info
            if kind == "goto":
                driver.goto(url)
            else:
                driver.open_tab(url)
            return True, f"{kind} {url}", info

        if kind == "click":
            name = driver.element_name(target)
            verdict, reason = classify_click(cfg, name, action)
            info.update({"target_name": name, "verdict": verdict})
            if verdict == "banned":
                return False, f"refused: {reason}", info
            if verdict == "commit":
                if dry_run:
                    return True, f"dry-run: would click '{name}' ({reason})", info
                return False, f"refused: {reason}", info
            driver.click(target)
            return True, f"clicked '{name or target}'", info

        if kind == "fill":
            driver.fill(target, str(action.get("text") or ""))
            return True, f"typed into #{target}", info

        if kind == "press":
            driver.press(str(action.get("key") or "Enter"))
            return True, f"pressed {action.get('key') or 'Enter'}", info

        if kind == "scroll":
            driver.scroll(int(action.get("dy") or 800))
            return True, "scrolled", info

        if kind == "wait":
            time.sleep(max(0.0, min(30.0, float(action.get("seconds") or 2))))
            return True, "waited", info

        if kind == "read":
            return True, "read page", {"read": driver.read(target)}

        if kind == "screenshot":
            path = driver.screenshot(shots / f"step-{step:02d}-manual.png")
            return True, "screenshot taken", {"shot": path}

        if kind in ("switch_tab", "close_tab"):
            index = int(target if target is not None else 0)
            (driver.switch_tab if kind == "switch_tab" else driver.close_tab)(index)
            return True, f"{kind} {index}", info

        if kind == "set_file":
            path = str(action.get("path") or "")
            if not path or not Path(path).exists():
                return False, f"file not found: {path}", info
            driver.set_input_files(target, path)
            return True, f"set file {Path(path).name}", info

        return False, f"unknown action {kind}", info
    except Exception as exc:  # noqa: BLE001 - one bad step must not kill the run
        return False, f"{kind} failed: {str(exc)[:200]}", info


def run_task(cfg, goal: str, driver: BaseDriver | None = None,
             model: Callable[[str], str] | None = None,
             max_steps: int | None = None, dry_run: bool = False,
             backend: str | None = None, echo=print,
             stop_check: Callable[[], bool] | None = None,
             start_url: str | None = None) -> dict:
    """Run one free-language goal in the browser. Returns a result dict."""
    conf = dconf(cfg)
    max_steps = int(max_steps or conf["max_steps"])
    run_id, log_path, shots = new_run(cfg)
    log = RunLog(log_path)
    owns_driver = driver is None
    if owns_driver:
        driver = make_driver(cfg, backend=backend)
        driver.start()
    ask = _brain(cfg, model)
    stop_check = stop_check or stop_requested
    result = {"run": run_id, "goal": goal, "ok": False, "steps": 0,
              "summary": "", "log": str(log_path), "shots": str(shots),
              "playbook": False, "error": ""}
    taken: list[dict] = []
    try:
        if start_url:
            ok, reason = domain_allowed(cfg, start_url)
            if not ok:
                result["error"] = reason
                log.add(action="goto", ok=False, note=reason)
                return result
            driver.goto(start_url)
        snap = driver.snapshot()
        echo(f"  [desktop] run {run_id}: {goal}")
        echo(f"  [desktop] starting at {snap.get('url') or '(blank)'}")

        # -- try a recorded playbook first (cheap replay, no model calls)
        playbook = find_playbook(cfg, goal, snap.get("url") or "")
        if playbook:
            echo("  [desktop] replaying a recorded playbook…")
            replayed, note = _replay(cfg, driver, playbook, dry_run, log,
                                     shots, echo, stop_check)
            if replayed:
                result.update(ok=True, steps=len(playbook["steps"]),
                              summary="playbook replayed",
                              playbook=True)
                log.add(action="done", ok=True, via="playbook")
                echo("  [desktop] playbook worked.")
                return result
            _bump_playbook_failure(cfg, playbook)
            echo(f"  [desktop] playbook failed ({note}) — thinking instead…")
            snap = driver.snapshot()

        lessons: list[str] = []
        history: list[str] = []
        for step in range(1, max_steps + 1):
            if stop_check():
                result["summary"] = "stopped by user"
                log.add(action="stop", ok=False)
                break
            lessons = lessons_for(cfg, goal, snap.get("url") or "")
            prompt = _step_prompt(cfg, goal, snap, lessons, history,
                                  max_steps - step + 1)
            try:
                raw = ask(prompt)
            except Exception as exc:  # noqa: BLE001 - provider down
                result["error"] = f"model failed: {str(exc)[:200]}"
                log.add(action="model", ok=False, error=result["error"])
                break
            action = parse_action(raw)
            if action is None:
                history.append("model sent an unparseable reply; retry")
                log.add(action="parse", ok=False, raw=str(raw)[:200])
                continue

            if str(action.get("action")).lower() == "done":
                ok = bool(action.get("ok"))
                result.update(ok=ok,
                              summary=str(action.get("summary") or "")[:300])
                log.add(action="done", ok=ok, summary=result["summary"])
                break

            ok, note, info = execute_action(cfg, driver, action, dry_run, log,
                                            shots, step, echo=echo)
            entry = {"action": action.get("action"), "target": action.get("target"),
                     "target_name": info.get("target_name"),
                     "target_role": None, "url": action.get("url"),
                     "text": action.get("text"), "key": action.get("key"),
                     "why": action.get("why"), "ok": ok, "note": note}
            taken.append(entry)
            history.append(f"step {step}: {note} — {str(action.get('why') or '')[:60]}")
            log.add(step=step, ok=ok, note=note, why=action.get("why"),
                    action=action.get("action"), target=action.get("target"))
            echo(f"  [desktop] {step:02d} {note}")

            shot = None
            try:
                shot = driver.screenshot(shots / f"step-{step:02d}.png")
            except Exception:  # noqa: BLE001
                pass
            if shot:
                log.add(step=step, shot=shot)
            snap = driver.snapshot()
            snap["shot"] = shot
            if not ok and str(action.get("action")).lower() in (
                    "goto", "open_tab", "click", "fill"):
                # a failed core action: allow the model one recovery attempt,
                # but note it so the error becomes a lesson below
                history.append(f"step {step} FAILED: {note}")
        result["steps"] = len(history)
    finally:
        if owns_driver and driver is not None:
            try:
                driver.close()
            except Exception:  # noqa: BLE001
                pass
        clear_stop()

    # -- learn from this run -------------------------------------------------
    try:
        if result.get("ok") and taken:
            record_playbook(cfg, goal, (driver.url() if not owns_driver
                                        else "") or "", taken)
        elif not result.get("ok"):
            _record_lesson(cfg, goal, log_path, taken, result, ask, echo)
    except Exception:  # noqa: BLE001 - learning is best-effort
        pass
    return result


def _replay(cfg, driver, playbook: dict, dry_run: bool, log: RunLog,
            shots: Path, echo, stop_check) -> tuple[bool, str]:
    """Try a recorded playbook against the live page. (ok, note)."""
    for index, step in enumerate(playbook.get("steps") or [], 1):
        if stop_check():
            return False, "stopped"
        snap = driver.snapshot()
        action = dict(step)
        action["action"] = step.get("action")
        name = step.get("target_name")
        if name and action.get("action") in ("click", "fill", "set_file"):
            found = _resolve_named(snap, name, step.get("target_role") or "")
            if found is None:
                return False, f"step {index}: '{name}' not on the page"
            action["target"] = found
            action["target_name"] = name
        ok, note, _info = execute_action(cfg, driver, action, dry_run, log,
                                         shots, index, echo=echo)
        log.add(step=index, ok=ok, note=note, action="replay")
        echo(f"  [desktop] replay {index:02d} {note}")
        if not ok:
            return False, f"step {index}: {note}"
        try:
            driver.screenshot(shots / f"replay-{index:02d}.png")
        except Exception:  # noqa: BLE001
            pass
    return True, ""


def _record_lesson(cfg, goal: str, log_path: Path, taken: list[dict],
                   result: dict, ask, echo) -> None:
    """One extra model call: what to do differently next time."""
    failures = [t for t in taken if not t.get("ok")][-4:]
    outcome = result.get("error") or result.get("summary") or "did not finish"
    last_actions = "\n".join(
        f"- {t.get('action')} on '{t.get('target_name') or t.get('target')}': "
        f"{t.get('note')}" for t in (failures or taken[-4:])) or "(nothing ran)"
    prompt = (f"A browser agent tried this goal and failed.\n"
              f"GOAL: {goal}\nOUTCOME: {outcome}\n"
              f"LAST ACTIONS:\n{last_actions}\n\n"
              "Write ONE short sentence (max 25 words) as advice for the next "
              "attempt on this site — the concrete thing that will work "
              "instead. No preamble, just the sentence.")
    try:
        text = ask(prompt)
        add_lesson(cfg, goal, result.get("url") or "", text)
        echo(f"  [desktop] lesson recorded: {text[:120]}")
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# quick lanes (stage 1, no model calls)


def _open(cfg, url: str, backend: str | None = None,
          driver: BaseDriver | None = None):
    owns = driver is None
    if owns:
        driver = make_driver(cfg, backend=backend)
        driver.start()
    return driver, owns


def page_look(cfg, url: str, backend: str | None = None,
              driver: BaseDriver | None = None,
              shot_path: Path | None = None) -> dict:
    """Open a page, screenshot it, return its text (the stage-1 lane)."""
    ok, reason = domain_allowed(cfg, url)
    if not ok:
        return {"ok": False, "error": reason, "url": url}
    drv = None
    owns = False
    try:
        drv, owns = _open(cfg, url, backend, driver)
        drv.goto(url)
        snap = drv.snapshot()
        shot = None
        if shot_path is not None:
            shot = drv.screenshot(Path(shot_path))
        return {"ok": True, "url": snap.get("url"), "title": snap.get("title"),
                "text": snap.get("text") or "", "shot": shot,
                "elements": len(snap.get("elements") or [])}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300], "url": url,
                "alive": isinstance(exc, BrowserBusy)}
    finally:
        if owns and drv is not None:
            try:
                drv.close()
            except Exception:  # noqa: BLE001
                pass


class BrowserBusy(RuntimeError):
    """The browser is running but a tab would not answer (frozen/asleep).

    Kept as its own type so callers can say the honest thing: this is not
    "no browser", it is "one tab is stuck" — different fix, different mark.
    """


def cdp_http_info(url: str, timeout: float = 2.0) -> dict:
    """Ask the browser's OWN http endpoint what it is. Bounded, never raises.

    This answers in milliseconds even when a tab is frozen solid, which is
    exactly what Playwright's attach cannot do. That difference is the whole
    diagnosis: browser up + attach fails = a stuck tab, not a missing
    browser. Returns {} when nothing answers.
    """
    import json as _json
    import urllib.request

    base = (url or "").strip().rstrip("/")
    if not base:
        return {}
    out: dict = {}
    try:
        with urllib.request.urlopen(base + "/json/version", timeout=timeout) as resp:
            data = _json.loads(resp.read().decode("utf-8", "replace"))
        if isinstance(data, dict):
            out["browser"] = str(data.get("Browser") or "")
    except Exception:  # noqa: BLE001 - nothing listening is a normal answer
        return {}
    try:
        with urllib.request.urlopen(base + "/json/list", timeout=timeout) as resp:
            pages = _json.loads(resp.read().decode("utf-8", "replace"))
        if isinstance(pages, list):
            tabs = [p for p in pages if isinstance(p, dict)]
            out["tabs"] = len(tabs)
            out["pages"] = [str(p.get("title") or p.get("url") or "")[:60]
                            for p in tabs if p.get("type") == "page"][:5]
    except Exception:  # noqa: BLE001 - version already answered
        pass
    return out


def _reach_error(cdp_url: str, exc: Exception) -> tuple[str, bool]:
    """The right sentence for a failed attach: missing browser vs stuck tab."""
    info = cdp_http_info(cdp_url)
    if info:
        who = info.get("browser") or "Chromium"
        tabs = info.get("tabs")
        pages = info.get("pages") or []
        where = f" ({tabs} tab(s)" + (f": {', '.join(pages)}" if pages else "") + ")" \
            if tabs is not None else ""
        return ((f"your browser IS running ({who}{where}) but a tab did not "
                 "answer, so the agent could not attach — a page that is "
                 "stuck, asleep, or still loading does that. Close or refresh "
                 "that tab, then try again."), True)
    return ((f"could not reach your browser at {cdp_url} — start it with "
             "Desktop Chrome.bat first (the window with the debug port). "
             "Opera GX, Edge, Chrome, Brave, Vivaldi all work; Edge is "
             "already on every Windows machine."), False)


def connection_check(cfg, backend: str | None = None,
                     driver: BaseDriver | None = None,
                     timeout_ms: int = 5000) -> dict:
    """Can we reach the configured browser RIGHT NOW? Never raises.

    `desktop status` calls this: printing settings is not an answer to "is
    it ready?" — connecting is. The short timeout keeps a half-open port
    from hanging the command.
    """
    owns = driver is None
    info: dict = {}
    if owns:
        info = cdp_http_info(dconf(cfg)["cdp_url"])
    # retry only when NOTHING answered: the browser may still be starting up.
    # A browser that answers http but refuses the attach has a stuck tab, and
    # retrying that just wastes six more seconds.
    attempts = 2 if (owns and not info) else 1
    last_error = "could not reach the browser"
    for attempt in range(attempts):
        if owns or driver is None:
            drv = PlayDriver(cfg, backend=backend, timeout_ms=timeout_ms)
        else:
            drv = driver
        try:
            drv.start()
            probe = getattr(drv, "probe", None)
            if callable(probe):
                info = probe() or {}
                return {"ok": True, "url": info.get("url") or "",
                        "title": info.get("title") or "",
                        "page_answers": bool(info.get("alive")),
                        "reason": info.get("reason") or ""}
            snap = drv.snapshot()
            return {"ok": True, "url": snap.get("url") or "",
                    "title": snap.get("title") or "",
                    "page_answers": not snap.get("degraded"),
                    "reason": snap.get("reason") or ""}
        except Exception as exc:  # noqa: BLE001 - status must always answer
            last_error = str(exc)[:240]
            try:
                drv.close()
            except Exception:  # noqa: BLE001
                pass
            if attempt + 1 < attempts:
                time.sleep(1.5)
    return {"ok": False, "error": last_error, "alive": bool(info),
            "browser": info.get("browser") or "", "tabs": info.get("tabs"),
            "pages": info.get("pages") or []}


def peek(cfg, backend: str | None = None, driver: BaseDriver | None = None,
         note: str = "peek", progress=None, with_shot: bool = True) -> dict:
    """Screenshot + read the CURRENT page (no navigation) — /desk shot.

    Every step is bounded: a tab that is asleep, busy, or mid-reload comes
    back as a sentence (degraded) instead of hanging the command. The
    screenshot is attempted even then — it rides CDP, not the page's JS.
    """
    def say(message: str) -> None:
        if progress:
            try:
                progress(message)
            except Exception:  # noqa: BLE001
                pass

    drv = None
    owns = False
    try:
        drv, owns = _open(cfg, "", backend, driver)
        say("connected — reading the page")
        snap: dict = {}
        why = ""
        busy = False
        try:
            snap = drv.snapshot()
        except Exception as exc:  # noqa: BLE001
            busy = isinstance(exc, BrowserBusy)
            why = str(exc).strip().splitlines()[0][:200] if str(exc).strip() \
                else exc.__class__.__name__
        shot = None
        if with_shot:      # `text` does not pay for a PNG it will not use
            path = work_dir(cfg) / "shots" / f"{note}-{time.strftime('%H%M%S')}.png"
            say("taking the screenshot")
            try:
                shot = drv.screenshot(path)
            except Exception as exc:  # noqa: BLE001
                busy = busy or isinstance(exc, BrowserBusy)
                if not why:
                    msg = str(exc).strip()
                    why = msg.splitlines()[0][:200] if msg else exc.__class__.__name__
        text = snap.get("text") or ""
        # degraded = the snapshot itself said so, or the read FAILED (why).
        # An answered-but-empty page (about:blank) is not degraded.
        degraded = bool(snap.get("degraded")) or (not snap and bool(why))
        busy = busy or bool(snap.get("degraded"))
        out = {"ok": True, "url": snap.get("url") or "",
               "title": snap.get("title") or "", "text": text, "shot": shot,
               "degraded": degraded, "alive": busy,
               "reason": snap.get("reason") or why or ""}
        if degraded and not shot and not text:
            out["ok"] = False
            out["error"] = out["reason"] or "the page did not answer"
        return out
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).strip()
        return {"ok": False, "alive": isinstance(exc, BrowserBusy),
                "error": msg.splitlines()[0][:300] if msg
                else exc.__class__.__name__}
    finally:
        if owns and drv is not None:
            try:
                drv.close()
            except Exception:  # noqa: BLE001
                pass


_KMB = r"([\d.,]+)\s*([KMB]?)"


def parse_channel_text(text: str) -> dict:
    """Pull public numbers out of a channel page's visible text."""
    out: dict[str, Any] = {}
    match = re.search(_KMB + r"\s+subscribers", text or "", re.I)
    if match:
        out["subscribers"] = f"{match.group(1)}{match.group(2)}"
    match = re.search(_KMB + r"\s+videos", text or "", re.I)
    if match:
        out["videos"] = f"{match.group(1)}{match.group(2)}"
    match = re.search(_KMB + r"\s+views", text or "", re.I)
    if match:
        out["views_seen"] = f"{match.group(1)}{match.group(2)}"
    return out


def channel_data(cfg, ref: str, backend: str | None = None,
                 driver: BaseDriver | None = None) -> dict:
    """Public numbers for a channel, no API key, no quota (`browser data`)."""
    ref = (ref or "").strip()
    if ref.startswith("http"):
        url = ref if ref.rstrip("/").endswith("/about") else ref.rstrip("/") + "/about"
    elif ref.startswith("@"):
        url = f"https://www.youtube.com/{ref}/about"
    else:
        url = f"https://www.youtube.com/@{ref}/about"
    got = page_look(cfg, url, backend=backend, driver=driver)
    if not got.get("ok"):
        return {"ok": False, "ref": ref, "error": got.get("error")}
    data = parse_channel_text(got.get("text") or "")
    return {"ok": True, "ref": ref, "url": got.get("url"),
            "title": got.get("title"), **data}


# --------------------------------------------------------------------------
# posting (the commit path — gated, deterministic, one item at a time)


def studio_upload_url(channel: dict) -> str:
    base = str(channel.get("studio_url") or
               "https://studio.youtube.com").strip().rstrip("/")
    if base.endswith("/videos/upload"):
        return base
    return base + "/videos/upload"


def post_one(cfg, driver: BaseDriver, item: dict, channel: dict,
             dry_run: bool = False, run_log: RunLog | None = None,
             shots: Path | None = None, echo=print,
             pause: float = 2.0) -> dict:
    """Upload one item to one channel via Studio's own upload page.

    Deterministic steps (works even when the model is down), each resolved by
    element name so small Studio renames do not break it. The final commit
    click is gated by `desktop.uploads: on` — with it off the whole sequence
    runs up to the publish button and reports "staged (commit blocked)".
    """
    conf = dconf(cfg)
    name = str(channel.get("name") or "channel")
    url = studio_upload_url(channel)
    ok, reason = domain_allowed(cfg, url)
    if not ok:
        return {"ok": False, "channel": name, "error": reason}
    title = str(item.get("title") or Path(str(item.get("file") or "")).stem)
    description = str(item.get("description") or "")
    result = {"ok": False, "channel": name, "item": item.get("id"),
              "title": title, "committed": False, "error": ""}

    last_shot = {"path": ""}

    def shoot(label: str):
        if shots is not None:
            try:
                path = Path(shots) / f"post-{name}-{label}.png"
                driver.screenshot(path)
                last_shot["path"] = str(path)
            except Exception:  # noqa: BLE001
                pass


    try:
        driver.goto(url)
        snap = driver.snapshot()
        if "upload" not in (driver.url() or "").lower():
            # sometimes Studio lands on the channel picker; try once to click
            index = _resolve_named(snap, name)
            if index is not None:
                driver.click(index)
                driver.goto(url)
                snap = driver.snapshot()
        shoot("0-open")

        # 1. file input
        file_target = None
        for el in snap.get("elements") or []:      # the tagged one first
            if el.get("name") == "(file upload)":
                file_target = int(el["i"])
                break
        if file_target is None:
            for el in snap.get("elements") or []:
                if el.get("tag") == "input":
                    file_target = int(el["i"])
                    break
        if file_target is None:
            result["error"] = ("no file input found on the upload page - "
                               "the page showed: " + _seen_controls(snap))
            return result
        driver.set_input_files(file_target, str(item.get("file")))
        time.sleep(pause)
        snap = driver.snapshot()
        shoot("1-file")

        # 2. title / description — Studio speaks the account's language,
        #    so both spellings are tried (LABELS)
        for key, value in (("title", title), ("description", description)):
            index = _resolve_named(driver.snapshot(), LABELS[key])
            if index is not None and value:
                driver.fill(index, value[:4900])
        shoot("2-meta")

        # 3. Next ×3 (Details -> Video elements -> Checks -> Visibility)
        for attempt in range(3):
            index = _resolve_named(driver.snapshot(), LABELS["next"])
            if index is None:
                break
            driver.click(index)
            time.sleep(pause * 0.6)
        shoot("3-next")

        # 4. visibility
        key = {"public": "public", "private": "private",
               "unlisted": "unlisted"}.get(conf["visibility"], "unlisted")
        index = _resolve_named(driver.snapshot(), LABELS[key], attr=True)
        if index is not None:
            driver.click(index)
        shoot("4-visibility")

        # 5. the commit
        snap = driver.snapshot()
        # role="button" so the identically-named visibility radio can never
        # be clicked instead; last=True = the one in the dialog footer
        commit_index = None
        for key in ("publish", "schedule", "save"):
            commit_index = _resolve_named(snap, LABELS[key], role="button",
                                          last=True)
            if commit_index is not None:
                break
        if commit_index is None:
            result["error"] = ("no publish/save button found - the page "
                               "showed: " + _seen_controls(snap))
            return result
        commit_name = driver.element_name(commit_index)
        verdict, why = classify_click(cfg, commit_name, {})
        if verdict == "banned":
            result["error"] = f"refused: {why}"
            return result
        if verdict == "commit":
            if dry_run:
                result.update(ok=True, committed=False,
                              error="dry-run: stopped at the commit button")
                if run_log:
                    run_log.add(action="post", ok=True, item=item.get("id"),
                                channel=name, note="dry-run: would " + commit_name)
                return result
            result["error"] = (f"refused: {why} — set desktop.uploads: on "
                               "to allow posting")
            if run_log:
                run_log.add(action="post", ok=False, item=item.get("id"),
                            channel=name, note=result["error"])
            return result
        driver.click(commit_index)
        time.sleep(pause * 1.5)
        shoot("5-done")
        result.update(ok=True, committed=True)
        if run_log:
            run_log.add(action="post", ok=True, item=item.get("id"),
                        channel=name, note=f"clicked '{commit_name}'")
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)[:300]
        if run_log:
            run_log.add(action="post", ok=False, item=item.get("id"),
                        channel=name, note=result["error"])
    if last_shot["path"]:
        result["shot"] = last_shot["path"]
    return result
