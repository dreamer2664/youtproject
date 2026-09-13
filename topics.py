"""Self-refilling topic backlog.

The scheduler (and bored humans) pop ideas from topics/backlog.txt; Gemini
tops the file back up to topics.backlog_target so the queue never runs dry.
One topic per line, # = comment — the same format batch --topics reads.
"""

from __future__ import annotations

from pathlib import Path

from config import Config

HEADER = "# Topic backlog — one per line, # = comment. Refilled by: python main.py topics --topup\n"


def load_backlog(path: Path) -> list[str]:
    if not Path(path).exists():
        return []
    return [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")]


def save_backlog(path: Path, topics: list[str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + "".join(t.strip() + "\n" for t in topics if t.strip()),
                    encoding="utf-8")


def pop_topic(path: Path) -> str | None:
    topics = load_backlog(path)
    if not topics:
        return None
    first, rest = topics[0], topics[1:]
    save_backlog(path, rest)
    return first


def propose_topics(cfg: Config, count: int, existing: list[str]) -> list[str]:
    """Ask Gemini for fresh, specific video topics. Raises RuntimeError."""
    from scriptgen import GeminiProvider

    sample = "; ".join(existing[:12]) if existing else "none yet"
    prompt = f"""You programme a faceless YouTube Shorts channel about: {cfg.topic}
Tone: {cfg.tone}. Audience: {cfg.audience}. Language: {cfg.language}.
Propose {count} SPECIFIC video topics. Each must be one concrete story, fact or
question that fills a 30-45 second Short — narrow and curiosity-driven.
"Facts about X" is banned; "why X does Y" / "the X that Y" is perfect.
Do not repeat or resemble these existing topics: {sample}
RULES: one topic per line; no numbering, no bullets, no quotes, no explanations;
each under 120 characters; nothing offensive, nothing needing visuals of real people."""
    provider = GeminiProvider(cfg.gemini_api_key, cfg.gemini_model)
    raw = provider.generate_text(prompt, temperature=1.0, tag="topics")
    return _clean(raw, existing)


def _clean(raw: str, existing: list[str]) -> list[str]:
    seen = {t.strip().lower() for t in existing}
    out: list[str] = []
    for line in raw.splitlines():
        text = line.strip().lstrip("0123456789.)-•* \t").strip().strip("\"'").strip()
        if not text or len(text) > 140 or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
    return out


def top_up_backlog(cfg: Config, path: Path | None = None,
                   target: int | None = None) -> tuple[list[str], int]:
    """Fill the backlog to `target` topics. Returns (added, total). Never raises."""
    backlog = Path(path or cfg.topics_backlog_file)
    want = target or cfg.topics_backlog_target
    topics = load_backlog(backlog)
    need = want - len(topics)
    if need <= 0:
        return [], len(topics)
    if not cfg.gemini_api_key:
        print("  [topics] no Gemini key — backlog left as is "
              "(add ideas by hand or run: python main.py topics --add \"...\")")
        return [], len(topics)
    try:
        fresh = propose_topics(cfg, min(40, need + 5), topics)[:need]
    except Exception as exc:  # noqa: BLE001 - top-up must never kill a run
        print(f"  [topics] top-up failed ({str(exc)[:120]}) — backlog left as is")
        return [], len(topics)
    if not fresh:
        print("  [topics] model returned no usable topics — backlog left as is")
        return [], len(topics)
    save_backlog(backlog, topics + fresh)
    return fresh, len(topics) + len(fresh)
