"""Self-refilling topic backlog.

The scheduler (and bored humans) pop ideas from topics/backlog.txt; the
script chain tops the file back up to topics.backlog_target so the queue
never runs dry. One topic per line, # = comment — the same format batch
--topics reads.

generate/schedule/batch pop through pop_fresh_topic(), which skips
anything the channel already covered (exact or near-duplicate), so the
same video concept never renders twice.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
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


def normalize(topic: str) -> str:
    """Comparable core: no parentheticals, lowercase, alnum + spaces."""
    text = re.sub(r"\([^)]*\)", " ", topic.lower())
    text = re.sub(r"[^a-z0-9 ]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


# Glue words with no topic identity. Negations (not/no/never) are CONTENT:
# "never expires" and "expires" are opposite videos.
_STOPWORDS = frozenset("""why what how when where who whom which whose
    that this these those the a an of to in on at by for with from into out
    up down over under again further then once and or but nor as than too
    very just even also still such there here you your yours we our us they
    them their it its is are was were be been being am do does did doing
    doesnt dont didnt isnt arent could would should may might must shall
    will can i me my mine he him his she her hers""".split())


def _content_words(topic: str) -> list[str]:
    """Rare-word core of a topic: normalized words minus glue/stopwords."""
    return [w for w in normalize(topic).split()
            if len(w) > 1 and w not in _STOPWORDS]


def _shared_concepts(a: list[str], b: list[str]) -> int:
    """Content words in common, tolerating inflections (expire/expires)."""
    unmatched = list(b)
    shared = 0
    for word in a:
        for i, other in enumerate(unmatched):
            if word == other or SequenceMatcher(None, word, other).ratio() >= 0.85:
                shared += 1
                del unmatched[i]
                break
    return shared


def is_same_topic(a: str, b: str, threshold: float = 0.8) -> bool:
    """Same video concept? Exact, >=threshold similar, or shared concept.

    The concept net catches paraphrases ("doesn't expire" vs "never
    expires"): 2+ shared content words covering half the smaller core.
    Single shared nouns ("vending machine" x2) still pass as fresh.
    """
    left, right = normalize(a), normalize(b)
    if left == right:
        return True
    if len(left) < 12 or len(right) < 12:
        return False  # short titles: fuzzy matching is too trigger-happy
    if SequenceMatcher(None, left, right).ratio() >= threshold:
        return True
    core_a, core_b = _content_words(a), _content_words(b)
    if len(core_a) < 2 or len(core_b) < 2:
        return False
    shared = _shared_concepts(core_a, core_b)
    return shared >= 2 and shared / min(len(core_a), len(core_b)) >= 0.5


def pop_fresh_topic(path: Path, used: list[str]) -> str | None:
    """Pop the first backlog topic the channel hasn't covered yet.

    Entries matching `used` (exact or near-duplicate) are dropped as
    stale, with a note — they can never become videos. Returns None
    when nothing fresh remains.
    """
    path = Path(path)
    topics = load_backlog(path)
    if not topics:
        return None
    fresh: str | None = None
    stale = 0
    rest: list[str] = []
    for topic in topics:
        if any(is_same_topic(topic, old) for old in used):
            stale += 1
            continue
        if fresh is None:
            fresh = topic
            continue
        rest.append(topic)
    if stale:
        print(f"  [topics] dropped {stale} stale backlog topic(s) already covered.")
    save_backlog(path, rest)
    return fresh


def propose_topics(cfg: Config, count: int, existing: list[str]) -> list[str]:
    """Ask the script chain for fresh, specific topics. Raises RuntimeError."""
    from scriptgen import get_provider

    sample = "; ".join(existing[:12]) if existing else "none yet"
    prompt = f"""You programme a faceless YouTube Shorts channel about: {cfg.topic}
Tone: {cfg.tone}. Audience: {cfg.audience}. Language: {cfg.language}.
Propose {count} SPECIFIC video topics. Each must be one concrete story, fact or
question that fills a {cfg.target_seconds}-second Short — narrow and curiosity-driven.
"Facts about X" is banned; proven shapes: "why X does Y", "the X that Y",
"what happens when X", "how X really works".
Mix TWO flavours evenly: (1) pure-curiosity candy (animal superpowers, bizarre
places, everyday mysteries) and (2) genuinely USEFUL explainers (how everyday
systems work, body and brain mechanics, food science, money).
VISUAL RULE: every topic must be filmable with stock footage — real animals,
places, objects, food, space. Nothing abstract, no real people needed.
Do not repeat or resemble these existing topics: {sample}
RULES: one topic per line; no numbering, no bullets, no quotes, no explanations;
each under 120 characters; nothing offensive, nothing needing visuals of real people."""
    provider = get_provider(cfg)  # script chain: primary -> gemini -> groq
    raw = provider.generate_text(prompt, temperature=1.0, tag="topics")
    return _clean(raw, existing)


def _clean(raw: str, existing: list[str]) -> list[str]:
    seen = list(existing)
    out: list[str] = []
    for line in raw.splitlines():
        text = line.strip().lstrip("0123456789.)-•* \t").strip().strip("\"'").strip()
        if not text or len(text) > 140:
            continue
        if any(is_same_topic(text, old) for old in seen):
            continue
        seen.append(text)
        out.append(text)
    return out


def top_up_backlog(cfg: Config, path: Path | None = None,
                   target: int | None = None,
                   used: list[str] | None = None) -> tuple[list[str], int]:
    """Fill the backlog to `target` topics. Returns (added, total). Never raises."""
    backlog = Path(path or cfg.topics_backlog_file)
    want = target or cfg.topics_backlog_target
    topics = load_backlog(backlog)
    need = want - len(topics)
    if need <= 0:
        return [], len(topics)
    if not (cfg.gemini_api_key or cfg.groq_api_keys):
        print("  [topics] no Gemini/Groq key — backlog left as is "
              "(add ideas by hand or run: python main.py topics --add \"...\")")
        return [], len(topics)
    # Past videos count as "existing" so top-ups never re-propose them.
    existing = topics + [u for u in (used or []) if u]
    try:
        fresh = propose_topics(cfg, min(40, need + 5), existing)[:need]
    except Exception as exc:  # noqa: BLE001 - top-up must never kill a run
        print(f"  [topics] top-up failed ({str(exc)[:120]}) — backlog left as is")
        return [], len(topics)
    if not fresh:
        print("  [topics] model returned no usable topics — backlog left as is")
        return [], len(topics)
    save_backlog(backlog, topics + fresh)
    return fresh, len(topics) + len(fresh)
