"""Small platform-specific flags for child processes.

The web panel is launched with pythonw and starts CLI jobs without a console.
A console-mode child such as FFmpeg can otherwise open a new Windows console
for every subprocess (frame probe, audio extraction, render, and so on). These
kwargs suppress that extra window without changing the caller's stdout/stderr
handles (FFmpeg call sites capture or pipe their output). On Linux/macOS the
dict is empty, so normal subprocess behavior is unchanged.
"""

from __future__ import annotations

import os
import subprocess


def no_console_kwargs() -> dict[str, int]:
    """Return subprocess kwargs preventing a child console on Windows."""
    if os.name != "nt":
        return {}
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": flag} if flag else {}
