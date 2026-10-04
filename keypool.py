"""One shared dead-key memory for every lane (live log 2026-09-21).

A dead ElevenLabs key was retried on every scene — three wasted calls per
render — until voiceover.py grew a process-scoped dead set. That was the
right fix in the wrong place: every other lane (script, vision, Whisper,
stock photos, YouTube stats) still re-tried keys the provider had already
rejected, on every call, all run long. This module is that fix,
generalized — one memory, keyed by provider, consulted by everyone.

The rules each lane already applied, now remembered process-wide:

  * 401/403 (revoked key, wrong account, keyInvalid) -> dead(): the key
    is dropped for the rest of the process.
  * 429 -> NOT death: quota refills; per-key rotation handles it.
  * 404 / 5xx / request-shaped 400s -> not the key's fault; lanes decide.

Deadness is deliberately process-scoped: CLI runs are short, and a fresh
process is a fresh chance for a rotated key to work again. `keys --probe`
tests every configured key, dead-set or not — probing exists to find out
whether a key recovered.
"""

from __future__ import annotations

from typing import Iterable

_DEAD: dict[str, set[str]] = {}


def dead_set(provider: str) -> set[str]:
    """The mutable dead set for one provider (voiceover's public contract)."""
    return _DEAD.setdefault(provider, set())


def dead(provider: str, key: str) -> None:
    """Remember one rejected key for the rest of the process."""
    text = str(key).strip()
    if text:
        _DEAD.setdefault(provider, set()).add(text)


def is_dead(provider: str, key: str) -> bool:
    """True when this key was already rejected this process."""
    return str(key).strip() in _DEAD.get(provider, ())


def live(provider: str, keys: Iterable[str]) -> list[str]:
    """Keys minus dead ones: stripped, de-duped, order kept.

    De-dupe matters too — duplicate entries in a key list used to be
    tried twice per call in most lanes (a silent tax on every request).
    """
    deads = _DEAD.get(provider, ())
    seen: set[str] = set()
    out: list[str] = []
    for raw in keys or []:
        text = str(raw).strip() if raw is not None else ""
        if text and text not in seen and text not in deads:
            seen.add(text)
            out.append(text)
    return out


def key_dead_like(status: int, text: str) -> bool:
    """True when a status+body clearly condemns the KEY, not the request.

    401/403 always; a 400 only when the body says the key itself is bad
    (Gemini's "API key not valid" / API_KEY_INVALID). Everything else —
    including JSON-mode 400s — leaves the key alone for other payloads.
    """
    if status in (401, 403):
        return True
    if status == 400:
        low = str(text or "").lower()
        return ("api key not valid" in low or "api_key_invalid" in low
                or "api key expired" in low)
    return False


def reset() -> None:
    """Forget every dead key (tests; anything wanting a clean slate).

    Clears the sets IN PLACE: modules hold aliases (voiceover keeps
    `_DEAD_ELEVEN_KEYS = dead_set("elevenlabs")`), and dropping the dict
    entries would orphan every alias — writes through it would then go to
    a set nobody reads.
    """
    for deads in _DEAD.values():
        deads.clear()
