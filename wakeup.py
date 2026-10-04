"""Wake-up runner: what the PC does the moment it is allowed to work again.

The owner shuts the PC down before sleeping, so nothing can *run* at night.
What CAN happen is this: he sends `/go` from the phone (Telegram holds the
message; the bot writes `work/nightrun/request.json`), and then —

* if the PC is on and the bot is running, the request starts immediately;
* if the PC is off, the message waits, and THIS module is what runs at the
  next boot or the next wake-from-hibernate:

      1. check Telegram once for commands sent while the PC was off
         (a /go sent at midnight is found even at 07:00),
      2. run the night batch if a request is pending,
      3. go back to hibernate — but only when nobody is at the keyboard.

One command for Task Scheduler: `python main.py wakeup --sleep-after`

Windows setup (Task Scheduler, once):
  * trigger: Daily 01:00, "Wake the computer to run this task";
  * action:  `Wake and Run.bat`
  * if you truly shut down (not hibernate) every night, schedule the same
    bat at logon instead — the run then happens the moment you boot.

Never posts anything; the batch it runs cannot post either.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

SLEEP_IDLE_MIN = 300   # hibernate only after this much keyboard silence


def idle_seconds() -> "float | None":
    """Seconds since the last keyboard/mouse input (Windows); None else.

    Best-effort: if anything about the call fails, None means "unknown"
    and the caller leaves the PC alone.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes

        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint),
                        ("dwTime", ctypes.c_uint)]

        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        millis = ctypes.windll.kernel32.GetTickCount() - info.dwTime
        return max(0.0, millis / 1000.0)
    except Exception:  # noqa: BLE001 - unknown is a valid answer
        return None


def hibernate() -> str:
    """Hibernate the machine (Windows). Returns a human sentence."""
    if os.name != "nt":
        return "not Windows — hibernate skipped"
    try:
        subprocess.run(["shutdown", "/h"], check=False, timeout=30)
        return "hibernating now — good night"
    except Exception as exc:  # noqa: BLE001
        return f"hibernate failed ({exc}); leaving the PC awake"


def run(cfg, sleep_after: bool = False, no_inbox: bool = False,
        dry_run: bool = False, min_idle: int | None = None, echo=print,
        inbox=None, batch_runner=None, idler=None, sleeper=None) -> int:
    """The wake/boot task. Returns a process exit code."""
    min_idle = SLEEP_IDLE_MIN if min_idle is None else int(min_idle)
    idler = idler or idle_seconds
    sleeper = sleeper or hibernate

    import nightreq

    def default_inbox(c):
        from bot import check_inbox_once

        return check_inbox_once(c)

    if inbox is None:
        inbox = default_inbox

    def default_batch(c):
        from nightbatch import run as nb_run

        return nb_run(c, if_requested=True)

    if batch_runner is None:
        batch_runner = default_batch

    echo("  [wakeup] good morning (or good night) — checking what the "
         "phone asked for while I was off")

    if not no_inbox:
        try:
            result = inbox(cfg) or {}
            if result.get("error"):
                echo(f"  [wakeup] inbox: {result['error']}")
            else:
                echo(f"  [wakeup] inbox: {result.get('seen', 0)} command(s) "
                     f"seen, {result.get('queued', 0)} queued")
        except Exception as exc:  # noqa: BLE001 - offline is not fatal
            echo(f"  [wakeup] inbox check failed ({exc}) — continuing")

    pending = nightreq.pending(cfg)
    if dry_run:
        echo(f"  [wakeup] dry run: request pending? {'yes' if pending else 'no'} "
             f"| sleep-after={'yes' if sleep_after else 'no'} — nothing "
             "executed")
        return 0

    rc = 0
    if pending:
        echo("  [wakeup] a /go request is pending — starting the night "
             "shift")
        try:
            rc = int(batch_runner(cfg) or 0)
        except Exception as exc:  # noqa: BLE001 - never die mid-wake
            echo(f"  [wakeup] the batch crashed ({exc}) — the request "
                 "stays pending for the next wake")
            rc = 1
    else:
        echo("  [wakeup] nothing requested — exiting fast so the wake task "
             "doesn't hold the PC awake")

    if sleep_after:
        idle = idler()
        if idle is None:
            echo("  [wakeup] idle time unknown — leaving the PC awake "
                 "(use hibernate + a wake timer for overnight runs)")
        elif idle >= min_idle:
            echo(f"  [wakeup] nobody at the keyboard ({idle / 60:.0f} min "
                 f"idle) — {sleeper()}")
        else:
            echo(f"  [wakeup] you're using the PC ({idle:.0f}s since the "
                 "last input) — staying awake")
    return rc
