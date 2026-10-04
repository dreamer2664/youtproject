"""The boardroom: a management group of AIs that meets, argues, decides.

    python main.py meeting stats     # agents review channel numbers
    python main.py meeting pick      # agents choose today's source video
    python main.py meeting pick --render   # ... and render the winner
    python main.py meeting act       # the room DECIDES today's move itself:
                                     #   clip a pending source, or generate
                                     #   a video on a topic it writes
    python main.py meeting memory    # what the board decided so far

Inspired by the agent-team videos (multi-AI group chats): each seat is a
different persona on a DIFFERENT provider when keys allow — Strategist on
Gemini, Analyst on DeepSeek, Producer on Groq, Skeptic on OpenRouter —
so the room genuinely disagrees instead of one model agreeing with
itself. Every provider rides the full fallback chain, so a missing key
only costs personality, never the meeting.

The room runs ON DEMAND (not all day): one meeting = a few seconds of
chatter, a chair-written minutes file in out/meetings/, a short Telegram
summary, and machine-checked decisions (a pick must name one of the
candidates; a stats meeting yields at most 5 concrete actions).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from config import Config

# seat, emoji, preferred provider lane, persona. The provider is a
# PREFERENCE: no keys for it -> the shared fallback chain answers instead.
ROLES: list[dict] = [
    {"name": "Strategist", "emoji": "🎯", "lane": "gemini",
     "persona": ("You own channel growth and positioning. You think in "
                 "audiences, packaging and what makes someone subscribe. "
                 "You bring concrete tactics, not vibes.")},
    {"name": "Analyst", "emoji": "🔎", "lane": "deepseek",
     "persona": ("You are the numbers person. You read the data given to "
                 "the meeting literally, spot trends and outliers, and you "
                 "quote the actual figures when you make a point. You "
                 "never invent numbers.")},
    {"name": "Producer", "emoji": "🎬", "lane": "groq",
     "persona": ("You run content operations. You care about what can be "
                 "rendered TODAY with the pipeline (clips, parts, "
                 "longform, generated videos), about workload, and about "
                 "what is realistic for one person uploading by hand.")},
    {"name": "Skeptic", "emoji": "🤨", "lane": "openrouter",
     "persona": ("You are the devil's advocate. Your job is to challenge "
                 "every proposal the room makes — is it really the data, "
                 "or a hunch? Is it worth the effort? What would make it "
                 "backfire? You are respected, not annoying: you also "
                 "concede when a point is solid.")},
]

MAX_DECISIONS = 5
DECISION_CHARS = 240
MEMORY_LIMIT = 100          # entries kept in the board memory file
MEMORY_DIGEST_N = 6         # entries injected into every agenda


class MeetingError(RuntimeError):
    pass


# ------------------------------------------------------------- watchlist
def parse_watchlist(text: str) -> list[tuple[str, str]]:
    """Lines of 'URL | optional note' -> [(url, note)] (pure, tested).

    '#' comments and blanks skipped; the note keeps whatever the author
    wrote after the first '|'.
    """
    out: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        url, _, note = line.partition("|")
        url = url.strip()
        if url.lower().startswith(("http://", "https://")):
            out.append((url, note.strip()))
    return out


def remove_watchlist_url(text: str, url: str) -> str:
    """Watchlist text without the chosen URL's line (pure, tested).

    Only that line goes; comments and other candidates survive, so a
    pick meeting consumes exactly one candidate per day.
    """
    target = (url or "").strip().rstrip("/")
    kept: list[str] = []
    for line in (text or "").splitlines():
        candidate = line.split("|", 1)[0].strip().rstrip("/")
        if target and candidate == target:
            continue
        kept.append(line)
    return "\n".join(kept) + ("\n" if kept else "")


def load_watchlist(path: Path) -> list[tuple[str, str]]:
    try:
        return parse_watchlist(path.read_text(encoding="utf-8"))
    except OSError:
        return []


# --------------------------------------------------------- board memory
def _memory_path(cfg: Config) -> Path:
    return cfg.work_dir / "board_memory.json"


def load_memory(cfg: Config) -> list[dict]:
    """The board's decision log: [{date, kind, text}, ...] oldest first."""
    import json

    try:
        data = json.loads(_memory_path(cfg).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - corrupt/missing = empty memory
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and e.get("text")]


def remember(cfg: Config, kind: str, text: str) -> None:
    """Append one decision to the log. Never raises; caps the file."""
    import json

    entry = {"date": f"{datetime.now().astimezone():%Y-%m-%d %H:%M}",
             "kind": kind, "text": " ".join(str(text).split())[:300]}
    try:
        entries = load_memory(cfg) + [entry]
        _memory_path(cfg).parent.mkdir(parents=True, exist_ok=True)
        _memory_path(cfg).write_text(
            json.dumps(entries[-MEMORY_LIMIT:], indent=1, ensure_ascii=False),
            encoding="utf-8")
    except Exception:  # noqa: BLE001 - memory is a bonus, never a blocker
        pass


def memory_digest(cfg: Config, n: int = MEMORY_DIGEST_N) -> str:
    """The last n decisions, compact (pure).

    Full re-reads of every minutes file would cost far too many tokens;
    one line per past meeting is the board's long-term memory.
    """
    entries = load_memory(cfg)
    if not entries:
        return ""
    return "\n".join(f"- {e.get('date', '?')[:16]} [{e.get('kind', '?')}] "
                     f"{e.get('text', '')}" for e in entries[-n:])


def memory_report(cfg: Config) -> str:
    """`meeting memory` - the whole decision log, newest first."""
    entries = load_memory(cfg)
    if not entries:
        return ("The board has no memory yet - decisions get logged here "
                "automatically after every meeting.\n"
                f"(will live in {_memory_path(cfg)})")
    lines = [f"Board memory - {len(entries)} decision(s), newest first "
             f"(file: {_memory_path(cfg)})", ""]
    for e in reversed(entries):
        lines.append(f"{e.get('date', '?')[:16]}  [{e.get('kind', '?')}] "
                     f"{e.get('text', '')}")
    lines += ["", "Meetings see the last "
              f"{MEMORY_DIGEST_N} of these automatically (keeps token "
              "cost flat); full transcripts live in out/meetings/."]
    return "\n".join(lines)


def _with_memory(cfg: Config, agenda: str) -> str:
    """Append the memory digest to an agenda (pure wrapper)."""
    digest = memory_digest(cfg)
    if not digest:
        return agenda
    return (agenda.rstrip() + "\n\nPREVIOUS BOARD DECISIONS "
            "(most recent last):\n" + digest)


# ------------------------------------------------------------- prompting
def clamp_words(text: str, limit: int) -> str:
    """Cut a turn at a sentence end near the word limit (pure, tested)."""
    words = (text or "").split()
    if len(words) <= limit:
        return text or ""
    cut = " ".join(words[:limit])
    for mark in (".", "!", "?", ";", ":"):
        pos = cut.rfind(mark)
        if pos > limit * 3:  # keep most of the budget, end on a sentence
            return cut[:pos + 1]
    return cut + "…"


def build_turn_prompt(role: dict, agenda: str, transcript: str,
                      question: str) -> str:
    """One agent's speaking turn (pure, tested)."""
    return f"""You are the {role['name']} in a small management team running
video channels. {role['persona']}

MEETING DATA:
{agenda}

MEETING SO FAR:
{transcript or "(the meeting has just started)"}

QUESTION ON THE TABLE: {question}

Speak as the {role['name']} in FIRST PERSON, to the room. Be specific and
brief (a few sentences, max ~80 words). Do not repeat what was already
said; either BUILD on it or CHALLENGE it. No headers, no lists, no
signature — just what you say out loud."""


def build_minutes_prompt(agenda: str, transcript: str, kind: str) -> str:
    """The chair closes the meeting (pure, tested)."""
    if kind == "pick":
        shape = ('Return ONLY JSON: {"choice": "<one candidate URL, '
                 'EXACTLY as written in the meeting data>", "reason": '
                 '"<one sentence>"}')
        instruction = ("Choose ONE candidate video for today's clip run. "
                       "Weigh the room's arguments; the choice must be one "
                       "of the candidate URLs above, exactly.")
    elif kind == "act":
        shape = ('Return ONLY JSON: {"action": "clip" or "generate" or '
                 '"none", "url": "<pending source URL, exactly as listed '
                 '- only when action is clip>", "topic": "<a specific '
                 'video topic, 6-15 words - only when action is '
                 'generate>", "reason": "<one sentence>"}')
        instruction = ("Commit the room to exactly ONE action. clip: the "
                       "url must be one of the pending sources above, "
                       "exactly. generate: invent a specific, self-"
                       "contained topic in the channel's niche that rides "
                       "whatever the momentum data says is working (your "
                       "own idea, not a listed source). none: when the "
                       "room agrees that doing nothing extra today is "
                       "the right call.")
    else:
        shape = ('Return ONLY JSON: {"summary": "<3 sentences: what the '
                 'room agreed on>", "decisions": ["<action 1>", ...]} '
                 f'with 1-{MAX_DECISIONS} decisions.')
        instruction = ("Turn the room's discussion into DECISIONS the "
                       "channel owner can act on today. Each decision is "
                       "one concrete, checkable action (max 20 words).")
    return f"""You chair a management meeting for video channels.

MEETING DATA:
{agenda}

MEETING TRANSCRIPT:
{transcript}

{instruction} {shape}"""


def _match_candidate(choice: str,
                     candidates: list[str] | None) -> str | None:
    """Exact (or contained) match of the chair's pick against candidates."""
    return next((c for c in (candidates or [])
                 if c.strip().rstrip("/") == choice.rstrip("/")
                 or choice in c or c in choice), None)


def parse_decisions(raw: str, kind: str,
                    candidates: list[str] | None = None) -> dict | None:
    """Validate the chair's closing JSON (pure, tested).

    pick: the choice must be one of the candidates (exact or contained
    match). stats: 1..MAX_DECISIONS non-empty strings. Anything else is
    None — the meeting still delivers its transcript; a bad chair just
    doesn't get to decide.
    """
    from scriptgen import extract_json

    try:
        data = extract_json(raw)
    except Exception:  # noqa: BLE001 - garbage in, None out
        return None
    if not isinstance(data, dict):
        return None
    if kind == "pick":
        choice = str(data.get("choice") or "").strip()
        if not choice:
            return None
        match = _match_candidate(choice, candidates)
        if match is None:
            return None
        return {"choice": match,
                "reason": str(data.get("reason") or "").strip()[:300]}
    if kind == "act":
        action = str(data.get("action") or "").strip().lower()
        if action in ("", "nothing", "do nothing", "skip"):
            action = "none"
        if action not in ("clip", "generate", "none"):
            return None
        out = {"action": action,
               "reason": str(data.get("reason") or "").strip()[:300]}
        if action == "clip":
            choice = str(data.get("url") or data.get("choice") or "").strip()
            if not choice:
                return None
            match = _match_candidate(choice, candidates)
            if match is None:
                return None
            out["url"] = match
        elif action == "generate":
            topic = " ".join(str(data.get("topic") or "").split())
            if len(topic) < 8:
                return None
            out["topic"] = topic[:160]
        return out
    decisions = data.get("decisions")
    if not isinstance(decisions, list):
        return None
    cleaned = [str(d).strip()[:DECISION_CHARS] for d in decisions
               if str(d).strip()]
    if not 1 <= len(cleaned) <= MAX_DECISIONS:
        return None
    return {"summary": str(data.get("summary") or "").strip()[:600],
            "decisions": cleaned}


def format_minutes(kind: str, agenda: str, turns: list[dict],
                   decision: dict | None) -> str:
    """The minutes file body (pure, tested)."""
    now = datetime.now().astimezone()
    title = {"pick": "source pick", "act": "board action"}.get(
        kind, "stats review")
    lines = [f"# Boardroom — {now:%Y-%m-%d %H:%M} — {title}", "",
             "## Present", ""]
    lines += [f"- {role['name']} {role['emoji']} ({role['lane']})"
              for role in ROLES]
    lines += ["- Chair (closing summary)", "", "## Data on the table", "",
              agenda.strip(), "", "## Transcript", ""]
    for turn in turns:
        lines.append(f"**{turn['name']} {turn['emoji']}:** {turn['text']}")
        lines.append("")
    lines.append("## Outcome")
    lines.append("")
    if kind == "pick":
        if decision:
            lines.append(f"**Today's source:** {decision['choice']}")
            lines.append("")
            lines.append(f"*Why:* {decision['reason']}")
        else:
            lines.append("*No valid decision — the chair's pick did not "
                         "name a candidate.*")
    elif kind == "act":
        if decision:
            if decision["action"] == "clip":
                lines.append(f"**Board action: clip** {decision['url']}")
            elif decision["action"] == "generate":
                lines.append(f"**Board action: generate** "
                             f"(board-written topic: {decision['topic']})")
            else:
                lines.append("**Board action: nothing extra today**")
            lines.append("")
            lines.append(f"*Why:* {decision['reason']}")
            if decision.get("result"):
                lines.append("")
                lines.append(f"*Follow-through:* {decision['result']}")
        else:
            lines.append("*No valid action could be parsed from the "
                         "chair's summary.*")
    else:
        if decision:
            lines.append(decision.get("summary") or "")
            lines.append("")
            lines += [f"{i}. {d}" for i, d
                      in enumerate(decision["decisions"], start=1)]
        else:
            lines.append("*No valid decisions could be parsed from the "
                         "chair's summary.*")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------- the room
def _role_provider(cfg: Config, lane: str):
    """A provider chain that PREFERS this seat's lane (never raises)."""
    from scriptgen import get_provider

    saved = cfg.data.get("ai", {}).get("provider")
    try:
        cfg.data["ai"]["provider"] = lane
        return get_provider(cfg)
    except Exception:  # noqa: BLE001 - a seat never kills the meeting
        return None
    finally:
        if saved is None:
            cfg.data["ai"].pop("provider", None)
        else:
            cfg.data["ai"]["provider"] = saved


def _stats_agenda(cfg: Config) -> str:
    """The numbers on the table: latest snap report, offline."""
    import channelstats as cs
    from youtube import load_snapshots

    history = load_snapshots(cs.store_path(cfg))
    days = history.get("days") or []
    if not days:
        raise MeetingError("no snapshots yet — run `python main.py snap` "
                           "first (it costs ~3 YouTube API units per channel), then "
                           "re-run the meeting.")
    today = days[-1]
    prev = days[-2] if len(days) >= 2 else None
    report, _ = cs.build_channel_report(history, today["date"], prev,
                                        recent=cfg.snap_settings["recent"])
    return report


def _pick_candidates(cfg: Config, urls: list[str]) -> list[tuple[str, str]]:
    """CLI --url flags, then the source sheet, then the legacy watchlist."""
    from sheet import load_sheet, pending_rows

    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(url: str, note: str = "") -> None:
        key = (url or "").strip().rstrip("/")
        if key and key not in seen:
            seen.add(key)
            out.append((url.strip(), note))

    for url in [u for u in (urls or []) if u and u.strip()]:
        add(url)
    for row in pending_rows(load_sheet(cfg.sources_sheet)):
        add(row["url"], row["note"])
    for url, note in load_watchlist(cfg.meeting_watchlist):
        add(url, note)
    return out


def _mark_sheet(cfg: Config, url: str, status: str, result: str) -> None:
    """Best-effort sheet bookkeeping — never risks the meeting."""
    try:
        from sheet import mark_sheet

        mark_sheet(cfg.sources_sheet, url, status, result)
    except Exception:  # noqa: BLE001 - bookkeeping only
        pass


def _pick_agenda(candidates: list[tuple[str, str]], cfg: Config) -> str:
    lines = [f"Channel niche: {cfg.topic}", "",
             "Candidate source videos for today's clip run:"]
    for index, (url, note) in enumerate(candidates, start=1):
        lines.append(f"{index}. {url}"
                     + (f" — {note}" if note else ""))
    lines += ["", "The winner gets rendered into clips/shorts today "
              "(one video per day per channel is the current cadence)."]
    return "\n".join(lines)


def trend_lines(cfg: Config, days: int = 3, top: int = 12) -> str:
    """Per-video view momentum across the last `days` snapshot days.

    This is the board's trend signal: which videos gained views recently
    (and which channels are stalling) — "ride what works, drop what
    doesn't" starts from this table.
    """
    import channelstats as cs
    from youtube import load_snapshots

    history = load_snapshots(cs.store_path(cfg))
    snaps = history.get("days") or []
    if len(snaps) < 2:
        return ("(momentum needs 2+ snapshot days - run "
                "`python main.py snap` daily)")
    latest = snaps[-1]
    back = snaps[-(days + 1)] if len(snaps) > days else snaps[0]
    now_v = latest.get("videos") or {}
    then_v = back.get("videos") or {}
    gains: list[tuple[int, str, str, bool]] = []
    for vid, row in now_v.items():
        views = row.get("views") or 0
        old = then_v.get(vid, {}).get("views")
        gains.append((views - (old if old is not None else 0),
                      str(row.get("title") or "?"),
                      str(row.get("channel") or "?"), old is None))
    gains.sort(key=lambda g: (-g[0], g[1].lower()))
    total = sum(g for g, *_ in gains)
    lines = [f"Across the last {days} days (snapshots "
             f"{back.get('date', '?')} -> {latest.get('date', '?')}): "
             f"{total:+d} views total."]
    by_channel: dict[str, int] = {}
    for gain, _title, channel, _new in gains:
        by_channel[channel] = by_channel.get(channel, 0) + gain
    if by_channel:
        mom = " · ".join(f"{c} {g:+d}" for c, g in
                         sorted(by_channel.items(), key=lambda kv: -kv[1]))
        lines.append(f"Channel momentum: {mom}")
    lines.append("Per video (biggest movers first):")
    for gain, title, channel, is_new in gains[:top]:
        tag = " (published since)" if is_new else ""
        lines.append(f"  {gain:+d} — {title[:60]} [{channel}]{tag}")
    if len(gains) > top:
        lines.append(f"  …and {len(gains) - top} more")
    return "\n".join(lines)


def _act_agenda(cfg: Config, candidates: list[tuple[str, str]]) -> str:
    """The act meeting's data: numbers, momentum, clip sources."""
    lines = [f"Channel niche: {cfg.topic}",
             "This is a BOARD ACTION meeting: the room itself commits to "
             "today's one extra move.", ""]
    try:
        lines += ["LATEST CHANNEL NUMBERS (last `snap`):",
                  _stats_agenda(cfg), ""]
    except MeetingError as exc:
        lines += [f"LATEST CHANNEL NUMBERS: unavailable — {str(exc)[:120]}",
                  ""]
    lines += ["VIEWS MOMENTUM:", trend_lines(cfg), ""]
    if candidates:
        lines.append("Sources available to clip today:")
        lines += [f"{i}. {url}" + (f" — {note}" if note else "")
                  for i, (url, note) in enumerate(candidates, 1)]
    else:
        lines.append("Sources available to clip: none queued — the clip "
                     "option is off the table today.")
    lines.append("")
    lines.append("Standing context: one clip per day per channel is the "
                 "cadence. The generate lane renders a stock-footage "
                 "video (Pexels/Pixabay stills + edge-tts voiceover + "
                 "karaoke subtitles) from a topic the board writes "
                 "itself — the move for when clips are underperforming "
                 "or the momentum data points at a topic worth riding. "
                 "It costs a full generate budget (~5-6 LLM calls, ~21 "
                 "images), not a clip's.")
    return "\n".join(lines)


def _transcript_text(turns: list[dict]) -> str:
    return "\n".join(f"{t['name']}: {t['text']}" for t in turns) or ""


def room_digest(turns: list[dict], per_turn_chars: int = 200) -> str:
    """Each seat's LAST word, for the Telegram summary (pure, tested).

    The full transcript lives in the minutes file; this is the taste of
    the argument you get on your phone.
    """
    last: dict[str, dict] = {}
    for turn in turns:
        last[turn["name"]] = turn
    out: list[str] = []
    for turn in last.values():          # speaking order
        text = " ".join((turn["text"] or "").split())
        if len(text) > per_turn_chars:
            text = text[:per_turn_chars - 1].rstrip() + "…"
        out.append(f"{turn['name']} {turn['emoji']}: {text}")
    return "\n".join(out)


def latest_minutes(cfg: Config) -> Path | None:
    """The most recent minutes file (for `meeting last`).

    Sorted by mtime with the FILE NAME as the tie-break. Two minutes files
    land on the same mtime more often than it looks: the board can hold a
    second meeting inside one clock tick, and some filesystems have coarse
    mtime granularity (a smoke-test run hit an exact tie, after which
    `max()` returned whichever file the directory happened to list first —
    so `meeting last` showed the wrong meeting at random). Names start with
    YYYY-MM-DD, so on a tie the later-dated meeting wins, deterministically.
    """
    try:
        files = list((cfg.out_dir / "meetings").glob("*.md"))
    except OSError:
        return None
    if not files:
        return None

    def recency(p: Path) -> tuple[float, str]:
        try:
            return (p.stat().st_mtime, p.name)
        except OSError:                    # deleted mid-glob: sort it last
            return (float("-inf"), "")

    return max(files, key=recency)


def run_meeting(cfg: Config, kind: str, urls: list[str] | None = None,
                rounds: int | None = None, render: bool = False,
                send: bool = True, say=None, executor=None,
                events=None) -> str:
    """Hold one meeting. Returns a summary string. Never raises.

    kind="act" is the autonomous lane: the room commits to ONE action
    (clip a listed source / generate a video on a board-written topic /
    nothing) and `executor(decision)` — wired by main.py — carries it
    out. Board-initiated renders are NOT part of the meeting's call
    budget: the generate pipeline has its own quota headroom.

    `events(event_dict)` is optional and additive: the panel/dashboard
    (`--events-json`) use it to render the room live. A listener that
    raises is ignored — watching must never break a meeting.
    """
    say = say or print

    def emit(etype: str, **payload) -> None:
        if events is None:
            return
        try:
            events({"type": etype, **payload})
        except Exception:  # noqa: BLE001 - a listener never kills a meeting
            pass
    if kind not in ("stats", "pick", "act"):
        return f"Unknown meeting kind {kind!r} (stats, pick or act)."
    rounds = rounds or cfg.meeting_rounds
    try:
        if kind == "stats":
            agenda = _with_memory(cfg, _stats_agenda(cfg))
            candidates = None
            question = ("What do these numbers say, and what should we do "
                        "about it today? One video per day per channel is "
                        "the current plan — challenge it if the data says "
                        "so.")
        elif kind == "act":
            candidates = _pick_candidates(cfg, urls or [])
            agenda = _with_memory(cfg, _act_agenda(cfg, candidates))
            question = ("What should the room DO today? Study the momentum "
                        "data, the latest numbers and what the board "
                        "previously decided, then commit to exactly ONE "
                        "action: (a) CLIP one of the listed sources (name "
                        "it), (b) GENERATE one video on a specific topic "
                        "you write that rides what's working, or (c) "
                        "NOTHING extra (say why). Argue it through first.")
        else:
            candidates = _pick_candidates(cfg, urls or [])
            if not candidates:
                return ("No candidates for a pick meeting. Add some:\n"
                        "  python main.py sheet --add <youtube link> "
                        "--add <another>\n"
                        "or paste links into sources/sheet.csv "
                        "('url,note' per row; a bare link on a line is "
                        "fine too).")
            agenda = _pick_agenda(candidates, cfg)
            question = ("Which ONE candidate should we clip today, and "
                        "why? Argue from the channel's data and niche.")
    except MeetingError as exc:
        return str(exc)

    emit("agenda", kind=kind, question=question, text=agenda,
         candidates=[c[0] for c in (candidates or [])])

    # -- the room speaks ---------------------------------------------------
    turns: list[dict] = []
    calls = 0
    est_tokens = 0
    providers: dict[str, object] = {}
    for role in ROLES:
        providers[role["name"]] = _role_provider(cfg, role["lane"])
    if all(p is None for p in providers.values()):
        return ("No LLM lane answered — add a Gemini/Groq/OpenRouter/"
                "DeepSeek key to config.yaml (ai section), then re-run.")
    for round_no in range(1, max(1, rounds) + 1):
        for role in ROLES:
            provider = providers[role["name"]]
            if provider is None:
                continue
            prompt = build_turn_prompt(role, agenda,
                                       _transcript_text(turns), question)
            try:
                reply = provider.generate_text(prompt, temperature=0.8,
                                               tag="meeting")
                calls += 1
            except Exception as exc:  # noqa: BLE001 - one seat going quiet
                say(f"  {role['name']} {role['emoji']} is silent "
                    f"({str(exc)[:80]})")
                emit("silent", role=role["name"], emoji=role["emoji"],
                     lane=role["lane"], reason=str(exc)[:160])
                continue
            est_tokens += (len(prompt) + len(reply)) // 4
            text = clamp_words(reply.strip(), cfg.meeting_max_words)
            turns.append({"name": role["name"], "emoji": role["emoji"],
                          "text": text})
            say(f"  {role['name']} {role['emoji']}: {text}")
            emit("turn", role=role["name"], emoji=role["emoji"],
                 lane=role["lane"], round=round_no, text=text)

    # -- the chair decides -------------------------------------------------
    decision = None
    chair = _role_provider(cfg, cfg.ai_provider)
    if chair is not None:
        cand_urls = ([c[0] for c in candidates]
                     if kind in ("pick", "act") else None)
        prompt = build_minutes_prompt(agenda, _transcript_text(turns), kind)
        for attempt in (1, 2):
            try:
                raw = chair.generate_text(prompt, temperature=0.2,
                                          tag="meeting-chair", json_mode=True)
                calls += 1
                est_tokens += (len(prompt) + len(raw)) // 4
            except Exception:  # noqa: BLE001
                break
            decision = parse_decisions(raw, kind, cand_urls)
            if decision is not None:
                break
            prompt += ("\n\nYour last reply was not valid (wrong shape or "
                       "not one of the candidates). Try again, JSON only.")

    emit("decision", kind=kind, decision=decision)

    # -- remember the decision (the board's long-term memory) --------------
    if decision:
        if kind == "pick":
            remember(cfg, "pick",
                     f"picked {decision['choice']} — {decision['reason']}")
        elif kind == "stats":
            joined = "; ".join(decision["decisions"])
            remember(cfg, "stats",
                     (decision.get("summary") or "")
                     + (f" Decisions: {joined}" if joined else ""))
        elif kind == "act":
            if decision["action"] == "clip":
                remember(cfg, "act", f"clip {decision['url']} — "
                                     f"{decision['reason']}")
            elif decision["action"] == "generate":
                remember(cfg, "act", f"generate '{decision['topic']}' — "
                                     f"{decision['reason']}")
            else:
                remember(cfg, "act", f"none — {decision['reason']}")

    # -- act: carry the decision out ----------------------------------------
    if kind == "act" and decision and decision["action"] != "none":
        if executor is None:
            decision["result"] = ("not executed — no executor was wired "
                                  "(re-run via `python main.py meeting "
                                  "act` to follow through)")
        else:
            say(f"  [meeting] executing the board's action: "
                f"{decision['action']}")
            emit("action", action=decision["action"], decision=decision)
            try:
                decision["result"] = str(executor(decision) or "done")
            except Exception as exc:  # noqa: BLE001 - report, don't crash
                decision["result"] = f"execution failed: {str(exc)[:200]}"
        remember(cfg, "act", f"outcome: {decision['result']}")

    # -- deliver -----------------------------------------------------------
    now = datetime.now().astimezone()
    minutes = format_minutes(kind, agenda, turns, decision)
    out_dir = cfg.out_dir / "meetings"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{now:%Y-%m-%d}-{kind}.md"
        path.write_text(minutes, encoding="utf-8")
    except OSError as exc:
        path = None
        say(f"  [meeting] could not write minutes ({exc})")

    header = (f"📋 Boardroom {now:%Y-%m-%d}: "
              + {"pick": "source pick", "act": "board action"}.get(
                  kind, "stats review"))
    if kind == "pick" and decision:
        summary = (f"{header}\n\nToday's source: {decision['choice']}\n"
                   f"Why: {decision['reason']}")
    elif kind == "act" and decision:
        if decision["action"] == "clip":
            body = f"Board action: clip {decision['url']}"
        elif decision["action"] == "generate":
            body = (f"Board action: generate — '{decision['topic']}' "
                    "(board-written script, stock-footage generate lane)")
        else:
            body = "Board action: nothing extra today"
        summary = f"{header}\n\n{body}\nWhy: {decision['reason']}"
        if decision.get("result"):
            summary += f"\n{decision['result']}"
    elif kind == "stats" and decision:
        summary = (f"{header}\n\n{decision.get('summary', '')}\n\n"
                   "Decisions:\n"
                   + "\n".join(f"{i}. {d}" for i, d
                               in enumerate(decision["decisions"], 1)))
    else:
        summary = f"{header}\n\n(The chair produced no valid decision.)"
    digest = room_digest(turns)
    if digest:
        summary += f"\n\nThe room:\n{digest}"
    if path is not None:
        summary += f"\n\nFull minutes: {path}"
    summary += f"\n\nCost: {calls} LLM calls, ~{est_tokens:,} tokens."

    if send:
        from crew import _tg_ping

        _tg_ping(cfg, summary)
    say("")

    # -- optional follow-through -------------------------------------------
    if kind == "pick" and decision and render:
        say(f"  [meeting] rendering the winner: {decision['choice']}")
        emit("render", url=decision["choice"])
        from clipper import run_clip

        try:
            run_clip(cfg, url=decision["choice"])
            summary += "\n\nRendered: clips/ (pregen --push parks it)."
            _mark_sheet(cfg, decision["choice"], "clipped",
                        f"clips/ — boardroom pick --render ({cfg.topic})")
        except Exception as exc:  # noqa: BLE001 - render failure is reported
            summary += f"\n\nRender failed: {str(exc)[:200]}"
            _mark_sheet(cfg, decision["choice"], "failed",
                        f"render failed: {str(exc)[:120]}")
    if kind == "act" and decision and decision["action"] == "clip":
        _mark_sheet(cfg, decision["url"], "picked",
                    f"boardroom: {decision['reason']}")
        try:
            wl = cfg.meeting_watchlist
            if wl.exists():
                wl.write_text(remove_watchlist_url(
                    wl.read_text(encoding="utf-8"), decision["url"]),
                    encoding="utf-8")
        except OSError:
            pass
    if kind == "pick" and decision:
        _mark_sheet(cfg, decision["choice"], "picked",
                    f"boardroom: {decision['reason']}")
        try:
            wl = cfg.meeting_watchlist
            if wl.exists():
                wl.write_text(remove_watchlist_url(
                    wl.read_text(encoding="utf-8"), decision["choice"]),
                    encoding="utf-8")
        except OSError:
            pass
    return summary
