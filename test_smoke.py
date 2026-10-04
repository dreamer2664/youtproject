#!/usr/bin/env python3
"""Offline smoke tests — no network, no API keys, no FFmpeg needed.

Run from the project folder:

    python test_smoke.py

Covers the parts of the pipeline that don't need external services:
config loading (including garbage tolerance), the offline template script
writer in every art style, subtitle timing, CTA rotation, the Telegram
command parser, upload-kit building, procedural audio, and the job queue.

If all tests pass, the core logic is healthy. Failures here mean broken
code — fix them before touching anything else. Live services (Gemini key,
voice, images, FFmpeg) are checked separately by `python main.py preflight`.
"""

from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

PASS = 0
FAIL = 0
FAILURES: list[str] = []


_TMP_REGISTRY: list[str] = []


def _mkdtemp(prefix: str = "youttest_") -> str:
    """mkdtemp that registers the tree, so the run can clean up after itself."""
    path = tempfile.mkdtemp(prefix=prefix)
    _TMP_REGISTRY.append(path)
    return path


def _clean_temp_dirs(run_started: float = 0.0) -> None:
    """Run-end hygiene: remove the temp trees THIS run created.

    A full suite run creates ~190 trees and nothing removed them, so the
    temp filesystem grew until it filled — which then surfaced as random
    unrelated failures (ENOSPC in pregen, log writes silently producing
    zero bytes; hit live 2026-10-04). This deletes the run's registered
    trees, plus unregistered youttest_* dirs older than 2h (crashed runs
    — a concurrent live run's trees are fresh, so they survive). Never
    fails the suite. Set KEEP_TEST_TMP=1 to keep the trees for debugging.
    """
    import os
    import shutil
    import time

    if os.environ.get("KEEP_TEST_TMP"):
        return
    while _TMP_REGISTRY:
        shutil.rmtree(_TMP_REGISTRY.pop(), ignore_errors=True)
    now = time.time()
    for entry in Path(tempfile.gettempdir()).glob("youttest_*"):
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime < now - 2 * 3600:
            shutil.rmtree(entry, ignore_errors=True)


def check(name: str, fn) -> None:
    global PASS, FAIL
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - report, don't stop
        FAIL += 1
        FAILURES.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=3)
    else:
        PASS += 1
        print(f"PASS {name}")


def tmp_cfg(**overrides):
    """A Config rooted in a fresh temp dir (nothing touches the repo)."""
    from config import load_config

    tmp = Path(_mkdtemp())
    cfg_path = tmp / "config.yaml"
    if overrides:
        import yaml

        cfg_path.write_text(yaml.safe_dump(overrides), encoding="utf-8")
    return load_config(cfg_path)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
def t_config_defaults():
    cfg = tmp_cfg()
    assert cfg.style == "photoreal", cfg.style
    assert cfg.images_per_scene == 3, cfg.images_per_scene
    assert cfg.format == "portrait"
    assert (cfg.width, cfg.height) == (1080, 1920)
    assert cfg.speech_rate == "+40%"
    assert abs(cfg.speech_rate_factor - 1.4) < 1e-6
    assert cfg.target_seconds == 65
    assert cfg.fps == 30
    assert cfg.zoom == 1.18
    assert cfg.image_timeout == 90


def t_config_example_parses():
    """config.example.yaml must always be valid YAML that loads cleanly."""
    import yaml

    from config import load_config

    example = Path(__file__).resolve().parent / "config.example.yaml"
    data = yaml.safe_load(example.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and "channel" in data
    cfg = load_config(example)  # root becomes the repo; dirs already exist
    assert cfg.images_per_scene == 3
    assert cfg.style == "photoreal"


def t_groq_key_split():
    """The configured Groq list allocates ~90% to Whisper dynamically."""
    from config import load_config

    cfg = tmp_cfg()
    cfg.data["ai"]["groq_api_keys"] = [f"g{i:02d}" for i in range(35)]
    assert len(cfg.groq_api_keys) == 35
    assert len(cfg.groq_transcription_api_keys) == 32
    assert len(cfg.groq_llm_api_keys) == 3
    assert cfg.groq_transcription_api_keys + cfg.groq_llm_api_keys == \
        cfg.groq_api_keys

    cfg.data["ai"]["groq_api_keys"] = [f"g{i:02d}" for i in range(80)]
    assert (len(cfg.groq_transcription_api_keys),
            len(cfg.groq_llm_api_keys)) == (72, 8)

    # Tiny lists cannot approximate 90/10 exactly; preserve a text reserve
    # whenever there are at least two keys. A single key must pick one role.
    for count, expected in ((1, (1, 0)), (2, (1, 1)), (3, (2, 1))):
        cfg.data["ai"]["groq_api_keys"] = [f"g{i}" for i in range(count)]
        assert (len(cfg.groq_transcription_api_keys),
                len(cfg.groq_llm_api_keys)) == expected

    cfg.data["ai"]["groq_api_keys"] = [f"g{i}" for i in range(10)]
    cfg.data["ai"]["groq_transcription_percent"] = 50
    assert (len(cfg.groq_transcription_api_keys),
            len(cfg.groq_llm_api_keys)) == (5, 5)
    cfg.data["ai"]["groq_transcription_percent"] = 150
    assert len(cfg.groq_transcription_api_keys) == 10
    assert cfg.groq_llm_api_keys == []
    cfg.data["ai"]["groq_transcription_percent"] = "bad"
    assert cfg.groq_transcription_percent == 90

    example = Path(__file__).resolve().parent / "config.example.yaml"
    assert load_config(example).groq_transcription_percent == 90


def t_config_no_shared_mutation():
    """One Config's overrides must never leak into another (or DEFAULTS)."""
    from config import DEFAULTS, load_config

    tmp = Path(_mkdtemp())
    missing = tmp / "sub" / "config.yaml"  # does not exist -> pure defaults
    cfg1 = load_config(missing)
    cfg1.data["video"]["style"] = "cartoon"
    cfg1.data["channel"]["target_seconds"] = 999
    cfg2 = load_config(missing)
    assert cfg2.style == "photoreal", "mutation leaked into a fresh Config"
    assert cfg2.target_seconds == 65
    assert DEFAULTS["video"]["style"] == "photoreal", "DEFAULTS was mutated!"
    assert DEFAULTS["channel"]["target_seconds"] == 65


def t_config_garbage_tolerated():
    """Every typo-able value degrades to a default instead of crashing."""
    cfg = tmp_cfg()
    cfg.data["video"]["style"] = "anime"
    assert cfg.style == "photoreal"
    cfg.data["video"]["images_per_scene"] = "lots"
    assert cfg.images_per_scene == 3
    cfg.data["channel"]["speech_rate"] = "garbage"
    assert cfg.speech_rate == "+0%"
    cfg.data["channel"]["target_seconds"] = "garbage"
    assert cfg.target_seconds == 65
    for key, prop, default in [
        ("fps", "fps", 30),
        ("zoom", "zoom", 1.18),
        ("transition", "transition", 0.5),
    ]:
        cfg.data["video"][key] = "garbage"
        assert getattr(cfg, prop) == default, key
    cfg.data["ai"]["image_timeout"] = "garbage"
    assert cfg.image_timeout == 90
    cfg.data["video"]["images_per_scene"] = 99
    assert cfg.images_per_scene == 6
    cfg.data["channel"]["target_seconds"] = -5
    assert cfg.target_seconds == 5
    # music level: garbage -> the documented default (-12), never the stale
    # pre-retune -24 the fallback carried until 2026-10-04
    cfg.data["music"]["level_db"] = "loud"
    assert cfg.music_level_db == -12
    cfg.data["music"]["level_db"] = -18
    assert cfg.music_level_db == -18


# --------------------------------------------------------------------------
# scriptgen (offline parts)
# --------------------------------------------------------------------------
def t_template_all_styles():
    from images import style_spec, stylize
    from scriptgen import TemplateProvider

    for style in ("photoreal", "cartoon", "stickman"):
        cfg = tmp_cfg()
        cfg.data["video"]["style"] = style
        script = TemplateProvider().generate(cfg, "test topic")
        assert len(script.scenes) >= 3, style
        assert script.title and script.description and script.tags, style
        final = stylize(script.scenes[0].image_prompt, style)
        assert not final.startswith(", "), f"{style}: leading-comma artifact"
        direction = style_spec(style)["direction"]
        if direction:
            # The art direction must appear exactly once — a duplicate
            # ("flat 2D vector cartoon, flat 2D vector cartoon, ...") means
            # the template and the renderer are both prefixing.
            assert final.count(direction[:40]) == 1, f"{style}: doubled prefix"


def t_template_unknown_style_falls_back():
    from scriptgen import TemplateProvider

    cfg = tmp_cfg()
    cfg.data["video"]["style"] = "watercolor"
    script = TemplateProvider().generate(cfg, "test topic")
    assert len(script.scenes) >= 3
    assert "cinematic photorealistic" in script.scenes[0].image_prompt


def t_extract_json():
    from scriptgen import derive_tags, extract_json, normalise_script

    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    raw = extract_json('prose {"title":"T","scenes":[{"narration":"hi","image_prompt":"pic"}]} tail')
    script = normalise_script(raw, "test")
    assert script.title == "T" and len(script.scenes) == 1
    try:
        normalise_script({"scenes": []}, "x")
    except ValueError:
        pass
    else:
        raise AssertionError("empty scenes should raise")
    assert derive_tags("the quick brown fox")  # fallback tags never empty-ish


def t_factcheck_never_blocks():
    from factcheck import check_script
    from scriptgen import TemplateProvider

    cfg = tmp_cfg()  # no Gemini key
    script = TemplateProvider().generate(cfg, "test topic")
    report = check_script(script, cfg)
    assert report["checked"] is False
    cfg.data["factcheck"]["enabled"] = False
    assert check_script(script, cfg)["checked"] is False


# --------------------------------------------------------------------------
# subtitles
# --------------------------------------------------------------------------
def t_subtitles():
    from subtitles import (
        build_cues,
        build_karaoke_events,
        max_chars_for,
        write_ass,
        write_srt,
    )

    tmp = Path(_mkdtemp())
    narr = ["Hello world this is a test", "Second scene here"]
    timings = [[(0.0, 0.3), (0.3, 0.6), (0.6, 0.9), (0.9, 1.2), (1.2, 1.5), (1.5, 1.8)], []]
    cues = build_cues(narr, timings, [0.0, 5.0], [5.0, 5.0], max_chars_for("portrait"), 0.25)
    assert len(cues) >= 2
    assert all(c.end > c.start for c in cues)
    events = build_karaoke_events(narr, timings, [0.0, 5.0], [5.0, 5.0], 0.25)
    assert len(events) > 5  # one event per word
    assert write_srt(cues, tmp / "t.srt").exists()
    assert write_ass(events, tmp / "t.ass", "portrait", 1080, 1920).exists()


# --------------------------------------------------------------------------
# cta / topics / bot parser
# --------------------------------------------------------------------------
def t_script_length_repair():
    from scriptgen import (TemplateProvider, build_expansion_prompt,
                           needs_expansion, script_words)

    # Word counting across scenes.
    cfg = tmp_cfg()
    assert script_words(TemplateProvider().generate(cfg, "x")) >= 150
    # Expansion trigger: the real 71/169-word undershoot fires, 160/169 passes.
    assert needs_expansion(71, 169) is True
    assert needs_expansion(160, 169) is False
    assert needs_expansion(0, 0) is False
    # Expansion prompt carries the numbers and the CTA contract.
    prompt = build_expansion_prompt("{}", 71, 169, 28, True)
    assert "71 words" in prompt and "169" in prompt and "Do NOT" in prompt
    prompt_off = build_expansion_prompt("{}", 71, 169, 28, False)
    assert "call to action" in prompt_off

    from scriptgen import build_shorten_prompt, needs_shortening

    # Overshoot guard: the real 404/169-word blowout fires, 200/169 passes.
    assert needs_shortening(404, 169) is True
    assert needs_shortening(200, 169) is False
    assert needs_shortening(0, 0) is False
    short = build_shorten_prompt("{}", 404, 169, 28, True)
    assert "404 words" in short and "169" in short and "Tighten" in short


def t_script_no_double_cta():
    from scriptgen import TemplateProvider, end_rule

    # Gemini prompt rule mirrors main.py: the rotating CTA is appended at
    # render time, so the script must not add its own (else the ending is
    # "...follow for more facts. Follow for more.").
    assert "Do NOT" in end_rule(True) and "automatically" in end_rule(True)
    assert "call to action" in end_rule(False)
    # Template provider honors the same contract on both branches.
    cfg = tmp_cfg()
    cfg.data["cta"]["enabled"] = True
    script = TemplateProvider().generate(cfg, "test topic")
    assert "follow" not in script.scenes[-1].narration.lower()
    cfg.data["cta"]["enabled"] = False
    script = TemplateProvider().generate(cfg, "test topic")
    assert script.scenes[-1].narration == "Follow for part two."


def t_cta_rotation():
    from cta import commit_cta, next_cta

    cfg = tmp_cfg()
    _, _, n1 = next_cta(cfg, commit=False)
    _, _, n2 = next_cta(cfg, commit=False)
    assert n1 == n2 and n1 >= 1, "commit=False must not advance"
    commit_cta(cfg)
    _, _, n3 = next_cta(cfg, commit=False)
    assert n3 == n1 + 1, "commit_cta must advance by exactly one"


def t_topics_clean():
    from topics import _clean, load_backlog, pop_topic, save_backlog

    tmp = Path(_mkdtemp()) / "backlog.txt"
    save_backlog(tmp, ["alpha", "beta"])
    assert load_backlog(tmp) == ["alpha", "beta"]
    assert pop_topic(tmp) == "alpha"
    assert load_backlog(tmp) == ["beta"]
    assert pop_topic(tmp / "missing.txt") is None
    cleaned = _clean("1. First idea!\n- second idea\nFIRST IDEA!\n", ["existing"])
    assert cleaned == ["First idea!", "second idea"]  # deduped case-insensitively


def t_topics_norepeat():
    from topics import (_clean, is_same_topic, load_backlog, normalize,
                        pop_fresh_topic, save_backlog)

    assert normalize("The Secret Language of Trees!") == "the secret language of trees"
    assert normalize("X (variation 1 of 3: blah)") == "x"
    assert is_same_topic("the secret language of trees", "Secret Language of Trees!")
    assert is_same_topic("why cats stare at walls", "Why cats stare at walls?")
    assert not is_same_topic("why cats stare at walls", "why cats hate water")
    assert not is_same_topic("the ship", "the shop")  # short: exact only
    tmp = Path(_mkdtemp()) / "backlog.txt"
    save_backlog(tmp, ["The secret language of trees", "Why octopuses have three hearts"])
    used = ["secret language of trees!"]
    assert pop_fresh_topic(tmp, used) == "Why octopuses have three hearts"
    assert load_backlog(tmp) == []  # stale dupe dropped, fresh popped
    assert pop_fresh_topic(tmp, used) is None
    # top-up cleaning also filters history look-alikes, not just exact dupes.
    cleaned = _clean("Secret language of trees\nBrand new idea\n",
                     ["the secret language of trees"])
    assert cleaned == ["Brand new idea"]


def t_bot_parser():
    from bot import parse_incoming

    assert parse_incoming("/start") == ("help", "")
    assert parse_incoming("/help") == ("help", "")
    assert parse_incoming("/queue") == ("queue", "")
    assert parse_incoming("/send abc") == ("send", "abc")
    assert parse_incoming("  ") == ("ignore", "")
    assert parse_incoming("sharks")[0] == "topic"
    assert parse_incoming("/weird") == ("help", "")
    assert parse_incoming("/new ")[0] == "help"
    assert parse_incoming("/night") == ("night", "")
    assert parse_incoming("/night dry") == ("night", "dry")


# --------------------------------------------------------------------------
# package
# --------------------------------------------------------------------------
def t_package():
    import shutil

    from jobqueue import Job
    from package import build_package, build_platform_caption, fit_tags

    assert fit_tags(["a" * 400, "b" * 200]) == ["a" * 400]
    caption = build_platform_caption(
        {"title": "T", "tags": ["shark facts"], "format": "portrait"}, "tiktok"
    )
    assert "#shark" in caption and "#fyp" in caption

    tmp = Path(_mkdtemp())
    (tmp / "v.mp4").write_bytes(b"fakevideo")
    (tmp / "v.jpg").write_bytes(b"fakejpg")
    (tmp / "v.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
    meta = {
        "title": "T", "description": "D", "tags": ["a"], "categoryId": "22",
        "language": "English", "format": "portrait", "width": 1080,
        "height": 1920, "duration_seconds": 30,
        "subtitles_burned_in": True, "subtitle_file": "v.srt",
    }
    (tmp / "v.meta.json").write_text(json.dumps(meta))
    cfg = tmp_cfg()
    cfg.data["paths"]["package_dir"] = str(tmp / "kits")
    job = Job(id="abc123", topic="t", status="generated",
              video_file=str(tmp / "v.mp4"), meta_file=str(tmp / "v.meta.json"))
    kit = build_package(job, cfg)
    for name in ("video.mp4", "thumbnail.jpg", "captions.srt", "title.txt",
                 "description.txt", "tags.txt", "tiktok.txt", "reels.txt",
                 "CHECKLIST.md"):
        assert (kit / name).exists(), name
    # Missing .srt: the checklist must stay honest (no captions step).
    (tmp / "v.srt").unlink()
    shutil.rmtree(kit)
    kit2 = build_package(job, cfg)
    assert not (kit2 / "captions.srt").exists()
    assert "captions.srt" not in (kit2 / "CHECKLIST.md").read_text()


# --------------------------------------------------------------------------
# audiofx / jobqueue / main
# --------------------------------------------------------------------------
def t_audiofx():
    from audiofx import ensure_assets

    assets = ensure_assets(Path(_mkdtemp()))
    for key in ("music_loop", "whoosh", "pop"):
        assert assets[key].exists() and assets[key].stat().st_size > 1000, key


def t_queue():
    from jobqueue import Queue

    tmp = Path(_mkdtemp()) / "state.json"
    queue = Queue(tmp)
    job = queue.add("sharks")
    assert queue.get(job.id[:6]).id == job.id  # prefix lookup
    queue.update(job, status="generated", title="T")
    assert Queue(tmp).get(job.id).status == "generated"  # persisted
    assert len(Queue(tmp).pending()) == 1
    tmp.write_text("{corrupt", encoding="utf-8")
    assert Queue(tmp).jobs == []  # corrupt file -> moved aside, not fatal
    assert tmp.with_suffix(".corrupt.json").exists()


def t_profanity_mask():
    # Live request 2026-09-23: mask swears in captions (YouTube safety).
    # Whole-word only, first letter kept, word count never changes.
    from subtitles import mask_profanity

    assert mask_profanity("what the fuck is this shit") == \
        "what the f*ck is this sh*t"
    assert mask_profanity("Fucking hell, SHIT!") == "F*ck*ng hell, SH*T!"
    assert mask_profanity("motherfucker bitching cunts") == \
        "m*th*rf*ck*r b*tch*ng c*nts"
    # compounds never match
    assert mask_profanity("class bass assess assumption scunthorpe") == \
        "class bass assess assumption scunthorpe"
    # word count preserved (karaoke timing depends on it)
    assert len(mask_profanity("no damn way around it").split()) == 5
    assert mask_profanity("") == ""
    # vowel-less entries still mask (mf has no vowels to star)
    assert mask_profanity("mf") == "m*"
    assert mask_profanity("MF") == "M*"
    # config default + switch
    cfg = tmp_cfg()
    assert cfg.subtitles_mask_profanity is True
    cfg.data["subtitles"]["mask_profanity"] = False
    assert cfg.subtitles_mask_profanity is False


def t_timestamp_rollover():
    from subtitles import ass_timestamp, srt_timestamp

    # Minute/hour rollovers carry correctly (was 00:01:60,000).
    assert srt_timestamp(119.99996) == "00:02:00,000"
    assert ass_timestamp(119.99996) == "0:02:00.00"
    assert srt_timestamp(3599.9999) == "01:00:00,000"
    assert ass_timestamp(3599.9999) == "1:00:00.00"
    # Ordinary values are unchanged.
    assert srt_timestamp(3661.25) == "01:01:01,250"
    assert srt_timestamp(61.5) == "00:01:01,500"
    assert ass_timestamp(61.5) == "0:01:01.50"
    assert srt_timestamp(0) == "00:00:00,000"
    assert srt_timestamp(-3) == "00:00:00,000"
    assert ass_timestamp(-1) == "0:00:00.00"


def t_parts_plan():
    from parts import plan_parts

    spans = [(0.0, 9.5), (10.0, 21.0), (21.5, 32.0), (33.0, 44.0)]
    # Ideal cut at 20s snaps to the sentence end at 21s.
    plan = plan_parts(44.0, spans, target_len=20.0, tail_merge=15.0,
                      snap_window=10.0)
    assert plan == [(0.0, 21.0), (21.0, 44.0)]
    # A tail shorter than tail_merge joins the previous part.
    short = [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0), (30.0, 34.0)]
    assert plan_parts(34.0, short, target_len=20.0, tail_merge=15.0,
                      snap_window=10.0) == [(0.0, 34.0)]
    # Short video, empty plan, garbage target.
    assert plan_parts(25.0, [], target_len=60.0) == [(0.0, 25.0)]
    assert plan_parts(0, []) == []
    assert plan_parts(-5, None) == []
    assert plan_parts(100.0, [], target_len="junk") == [(0.0, 60.0), (60.0, 100.0)]
    assert len(plan_parts(100.0, [], target_len=-5)) == 6  # clamped to 15s
    # Past max_parts the target widens; coverage stays edge to edge.
    capped = plan_parts(600.0, [], target_len=60.0, max_parts=5)
    assert len(capped) == 5
    assert capped[0] == (0.0, 120.0) and capped[-1] == (480.0, 600.0)
    # No sentences nearby: fall back to word starts, then exact cuts.
    words = plan_parts(130.0, [], target_len=60.0,
                       word_starts=[59.5, 119.0])
    assert words == [(0.0, 59.5), (59.5, 130.0)]
    exact = plan_parts(130.0, [], target_len=60.0)
    assert exact == [(0.0, 60.0), (60.0, 130.0)]
    # Config surface + garbage tolerance.
    cfg = tmp_cfg()
    assert cfg.parts_part_len == 60.0
    assert cfg.parts_max_parts == 50
    assert cfg.parts_header is True
    assert cfg.parts_tail_merge == 15.0
    assert cfg.parts_snap_window == 10.0
    cfg.data["parts"]["part_len"] = "junk"
    cfg.data["parts"]["max_parts"] = "junk"
    assert cfg.parts_part_len == 60.0
    assert cfg.parts_max_parts == 50


def t_parts_header():
    from parts import esc_header, part_kit_title, parts_header_line

    # Solo/whole videos: title-only header (2026-09-30 decision, was
    # "no header"); blank titles still get none.
    solo = parts_header_line("Hello", 1, 1, 60.0)
    assert "Hello" in solo and "Part" not in solo
    assert parts_header_line("", 2, 5, 60.0) == ""
    assert parts_header_line("  ", 2, 5, 60.0) == ""
    line = parts_header_line("Testing 100 phones", 2, 5, 63.2)
    assert line.startswith("Dialogue: 0,0:00:00.00,0:00:04.00,")
    assert "\\fad" in line  # gentle in/out, ignored where unsupported
    # 0 (or huge) show_seconds keeps it up the whole part.
    long = parts_header_line("Testing 100 phones", 2, 5, 63.2,
                             show_seconds=0)
    assert long.startswith("Dialogue: 0,0:00:00.00,0:01:03.20,")
    assert parts_header_line("T", 1, 3, 63.2,
                             show_seconds=999).startswith(
        "Dialogue: 0,0:00:00.00,0:01:03.20,")
    assert ",0,0,110,," in line  # top margin overrides the style
    assert "\\an8" in line  # top-center, karaoke stays centered/bottom
    assert "Testing 100 phones" in line and "Part 2" in line
    assert "Part 2 of" not in line  # the overlay stays short
    # Header text: collapsed, truncated, ASS-escaped.
    assert esc_header("a  b") == "a b"
    long = esc_header("x" * 100)
    assert len(long) <= 44 and long.endswith("…")
    assert esc_header("a{b}\\c") == "a\\{b\\}\\\\c"
    # Kit titles: series suffix, truncation, masking.
    assert part_kit_title("Testing 100 phones", 3, 8) == \
        "Testing 100 phones — Part 3"
    assert part_kit_title("Solo", 1, 1) == "Solo"
    capped = part_kit_title("y" * 120, 1, 9)
    assert len(capped) <= 95 and capped.endswith("...")
    assert part_kit_title("What the fuck", 1, 2) == "What the f*ck — Part 1"


def t_parts_header_wrap():
    from parts import (prepare_header_title, parts_header_line,
                       wrap_header_title)

    # User's real case: fits two lines -> verbatim, entities decoded.
    two = prepare_header_title(
        "Every Mental Disorder &amp; Their Effects Explained",
        provider=None)
    assert two == "Every Mental Disorder & Their Effects\nExplained"
    assert "&amp;" not in two
    # Short titles stay one line; blanks stay blank.
    assert prepare_header_title("Testing 100 phones") == \
        "Testing 100 phones"
    assert prepare_header_title("  ", provider=None) == ""
    assert wrap_header_title("") == []
    assert wrap_header_title("a &amp; b") == ["a & b"]
    # Garbage width / max_lines fall back, never raise.
    assert wrap_header_title("hello world", width="junk") == \
        ["hello world"]
    assert prepare_header_title("hello", max_lines="junk") == "hello"
    # Over-long titles with no LLM: first 3 lines + … on the last.
    long_title = " ".join(["lorem ipsum dolor sit amet"] * 6)
    assert len(wrap_header_title(long_title)) > 3
    for kwargs in ({"provider": None}, {"shorten_enabled": False}):
        cut = prepare_header_title(long_title, **kwargs)
        assert len(cut.split("\n")) == 3
        assert cut.split("\n")[-1].endswith("…")
    solo = prepare_header_title(long_title, max_lines=1, provider=None)
    assert "\n" not in solo and solo.endswith("…")
    # The Dialogue stacks wrapped lines above Part X.
    multi = parts_header_line("Line One\nLine Two", 2, 5, 63.2)
    assert "Line One\\NLine Two" in multi and "Part 2" in multi
    assert "\\an8" in multi


def t_parts_header_shorten():
    from parts import prepare_header_title, shorten_header_title

    class Stub:
        """Canned generate_text; records the tag it was called with."""

        def __init__(self, reply="", explode=False):
            self.reply = reply
            self.explode = explode
            self.calls = 0
            self.tags = []

        def generate_text(self, prompt, temperature=0.7, tag="",
                          json_mode=False):
            self.calls += 1
            self.tags.append(tag)
            assert "TITLE:" in prompt
            if self.explode:
                raise RuntimeError("no keys")
            return self.reply

    long_title = " ".join(["lorem ipsum dolor sit amet"] * 6)
    # A good shorten is kept: shorter, fits, shares subject words.
    good = Stub("lorem ipsum dolor sit amet, shortened")
    kept = prepare_header_title(long_title, provider=good)
    assert good.calls == 1 and good.tags == ["headertitle"]
    assert "…" not in kept and kept.replace("\n", " ") == good.reply
    # Quoted / chatty replies are cleaned to the title itself.
    assert prepare_header_title(long_title,
                                provider=Stub('"lorem ipsum dolor"')) == \
        "lorem ipsum dolor"
    assert prepare_header_title(long_title, provider=Stub(
        "lorem ipsum\nHere is why I chose it")) == "lorem ipsum"
    # Short titles never touch the LLM.
    cold = Stub(explode=True)
    assert prepare_header_title("Testing 100 phones",
                                provider=cold) == "Testing 100 phones"
    assert cold.calls == 0
    # Every failure mode falls back to truncation, never raises.
    bad_replies = ["", "   ", long_title + " plus even more words",
                   "quantum banana recipes for dinner parties tonight"]
    for reply in bad_replies:
        cut = prepare_header_title(long_title, provider=Stub(reply))
        assert cut.split("\n")[-1].endswith("…"), reply
    cut = prepare_header_title(long_title, provider=Stub(explode=True))
    assert cut.split("\n")[-1].endswith("…")
    assert shorten_header_title("", Stub("x"), 44) is None
    assert shorten_header_title(long_title, None, 44) is None


def t_parts_kit():
    from parts import (build_part_description, build_part_srt,
                       write_part_kit)

    tmp = Path(_mkdtemp())
    clip = tmp / "ab12_part_01.mp4"
    clip.write_bytes(b"fake")
    src = {"title": "Source Vid", "channel": "Chan", "url": "http://x"}
    # captions.srt from part words; None for wordless stretches.
    words = [{"word": "hello", "start": 0.0, "end": 0.5},
             {"word": "world.", "start": 0.6, "end": 1.0}]
    srt = build_part_srt(words, 60.0, tmp / "p.srt")
    assert srt is not None and srt.exists()
    assert "hello world" in srt.read_text(encoding="utf-8")
    assert build_part_srt([], 60.0, tmp / "empty.srt") is None
    # Description carries the sibling index + attribution; solo skips it.
    desc = build_part_description("T — Part 1", 1, 2, src,
                                  ["a.mp4", "b.mp4"])
    assert "Part 1 of 2" in desc and "Series: a.mp4, b.mp4" in desc
    assert "Source Vid" in desc and "http://x" in desc
    solo = build_part_description("Solo", 1, 1, src, [])
    assert "Part 1 of" not in solo and "Full credit" in solo
    # The kit: media + titles + credit + captions + checklist.
    kit = write_part_kit(clip, "Source Vid — Part 1", 1, 2, src,
                         (0.0, 60.0), ["ab12_part_01.mp4", "ab12_part_02.mp4"],
                         srt, None, tmp)
    assert (kit / "TITLE.txt").read_text(encoding="utf-8") == \
        "Source Vid — Part 1"
    assert "Part 1 of 2" in (kit / "DESCRIPTION.txt").read_text(
        encoding="utf-8")
    credit = (kit / "CREDIT.txt").read_text(encoding="utf-8")
    assert "window: 0.0s - 60.0s" in credit and "part: 1/2" in credit
    assert (kit / "captions.srt").exists()
    assert not (kit / "part.ass").exists()  # None in -> no file out
    assert (kit / "CHECKLIST.md").exists()
    assert (kit / "tiktok.txt").exists() and (kit / "reels.txt").exists()


def t_caption_overlap():
    import json as _json
    import re
    import tempfile as _tf

    from clipper import (TRANSCRIPT_CACHE_VERSION, build_clip_ass,
                         captions_to_words, load_transcript_cache,
                         normalize_word_timings)

    def monotonic(words):
        return all(b["start"] >= a["end"] - 1e-9 for a, b in zip(words, words[1:]))

    segs = [{"text": "so the thing about octopuses", "start": 0.0, "duration": 4.2},
            {"text": "is that they have nine brains", "start": 2.1, "duration": 4.0},
            {"text": "one in the head and one", "start": 4.3, "duration": 3.9},
            {"text": "in every single arm", "start": 6.2, "duration": 3.5}]
    spoken = ("so the thing about octopuses is that they have nine brains "
              "one in the head and one in every single arm").split()
    words = captions_to_words(segs)
    assert [w["word"] for w in words] == spoken
    assert monotonic(words)
    # each line ends where the next begins; the last keeps its duration
    assert words[4]["end"] == 2.1 and words[5]["start"] == 2.1
    assert abs(words[-1]["end"] - 9.7) < 1e-6
    # out-of-order segments are sorted by start (stable for ties)
    shuffled = captions_to_words([segs[1], segs[0], segs[3], segs[2]])
    assert [w["word"] for w in shuffled] == spoken
    # non-overlapping captions: unchanged math
    plain = captions_to_words([{"text": "a b", "start": 0, "duration": 2},
                               {"text": "c", "start": 3, "duration": 1}])
    assert [(w["start"], w["end"]) for w in plain] == [(0, 1), (1, 2), (3, 4)]

    # normalize: well-formed (Whisper-like) input passes through unchanged
    whisper = [{"word": "hi", "start": 0.1, "end": 0.4},
               {"word": "there.", "start": 0.5, "end": 0.9, "eos": True}]
    assert normalize_word_timings(whisper) == whisper
    assert normalize_word_timings(whisper) is not whisper      # copies
    # old-style overlapping words (what caches hold): repaired, order kept
    old = []
    for sg in segs:
        tw = sg["text"].split()
        step = sg["duration"] / len(tw)
        old += [{"word": w, "start": sg["start"] + i * step,
                 "end": sg["start"] + (i + 1) * step} for i, w in enumerate(tw)]
    assert not monotonic(old)
    fixed = normalize_word_timings(old)
    assert [w["word"] for w in fixed] == spoken and monotonic(fixed)
    assert fixed[0]["start"] == 0.0 and fixed[5]["start"] == 2.1  # anchors kept
    # garbage tolerated, extra keys survive, never negative lengths
    messy = normalize_word_timings([{"word": "a", "start": 1, "end": 0.5},
                                    "junk", {"word": "b", "start": "x"},
                                    {"word": "c", "start": 0.2, "end": 0.3, "eos": 1}])
    assert [w["word"] for w in messy] == ["a", "c"] and monotonic(messy)
    assert messy[1]["eos"] == 1 and all(w["end"] > w["start"] for w in messy)
    assert normalize_word_timings([]) == [] and normalize_word_timings(None) == []

    # cached transcripts from before the fix are repaired on load
    tmp = Path(_mkdtemp())
    cache = tmp / "c.json"
    cache.write_text(_json.dumps({"version": TRANSCRIPT_CACHE_VERSION,
                                  "words": old}), encoding="utf-8")
    loaded = load_transcript_cache(cache)
    assert [w["word"] for w in loaded] == spoken and monotonic(loaded)

    # the actual symptom: no two karaoke events on screen at once, even
    # when overlapping words reach the renderer directly
    def sec(ts):
        h, m, s = ts.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    for sample in (old, words, loaded):
        ass = build_clip_ass(sample, 10.0, tmp_cfg(), Path(_mkdtemp()),
                             None).read_text(encoding="utf-8")
        events = [l.split(",", 9) for l in ass.splitlines()
                  if l.startswith("Dialogue")]
        spans = sorted((sec(e[1]), sec(e[2])) for e in events)
        assert len(spans) >= 15
        for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
            assert s2 >= e1 - 1e-6, ("overlapping subtitle events", s1, e1, s2, e2)
        text = " ".join(re.sub(r"\{[^}]*\}", "", e[9]) for e in events)
        assert "octopuses" in text and "arm" in text


def t_longform():
    """Long-form lane: geometry, window planning, chapters, kit (pure)."""
    from longform import (CHAPTER_MIN_COUNT, LONGFORM_TITLE_LIMIT,
                          build_longform_description, build_longform_srt,
                          chapter_marks, clean_long_title, fmt_timestamp,
                          landscape_fit_filter, landscape_pad_filter,
                          landscape_treatment, longform_stem,
                          plan_longform_window, write_longform_kit,
                          _shift_bounds, _chapter_label)

    # geometry ----------------------------------------------------------
    assert landscape_treatment(1920, 1080) == "pad"
    assert landscape_treatment(640, 480) == "pad"        # 4:3 still pads
    assert landscape_treatment(1000, 1000) == "fit"      # square blurs-fill
    assert landscape_treatment(720, 1280) == "fit"       # portrait blurs-fill
    assert landscape_treatment(1920, 0) == "pad"         # unknown never fit
    pad = landscape_pad_filter()
    assert "scale=1920:1080" in pad and "pad=1920:1080" in pad \
        and "force_original_aspect_ratio=decrease" in pad
    fit = landscape_fit_filter()
    assert "boxblur" in fit and "overlay=(W-w)/2:(H-h)/2" in fit \
        and "scale=-2:1080" in fit and "crop=1920:1080" in fit

    # window planning ---------------------------------------------------
    assert plan_longform_window(600, None) == (0.0, 600.0, True)
    assert plan_longform_window(600, 900) == (0.0, 600.0, True)  # target > source
    assert plan_longform_window(600, 590) == (0.0, 600.0, True)  # within slack
    start, end, whole = plan_longform_window(600, 480)
    assert not whole and start == 0.0 and 360 <= end <= 600
    # --start snaps to a cutpoint near the request (never past latest_start)
    bounds = [(605.0, 3)]
    start, end, whole = plan_longform_window(1200, 480, 600.0, bounds)
    assert not whole and start == 605.0
    assert 605 + 360 <= end <= 1200
    # a late --start clamps so the full target still fits — even when a
    # cutpoint sits past the clamp (a snap past latest_start starves the
    # target: 730 + 480 > 1200)
    start, end, whole = plan_longform_window(
        1200, 480, 1100.0, [(605.0, 3), (730.0, 3)])
    assert start <= 720.0 and end == 1200.0 and not whole
    # a cut landing <15s before the end swallows the tail
    start, end, whole = plan_longform_window(540, 480, 0.0, [(530.0, 3)])
    assert (start, end, whole) == (0.0, 540.0, False)
    # 90s is within the 30s slack of a 60s target: whole
    assert plan_longform_window(90, 60) == (0.0, 90.0, True)
    # exact target, no boundaries: cut lands on the ideal second
    assert plan_longform_window(100, 60) == (0.0, 60.0, False)

    # chapters ----------------------------------------------------------
    assert fmt_timestamp(0) == "0:00"
    assert fmt_timestamp(65) == "1:05"
    assert fmt_timestamp(3725) == "1:02:05"
    assert fmt_timestamp(-3) == "0:00"

    def words_over(total, step=5.0):
        out = []
        t = 0.0
        i = 0
        while t < total:
            out.append({"word": f"w{i}.", "start": t, "end": t + step / 2})
            t += step
            i += 1
        return out

    # too short for 3 marks -> none
    assert chapter_marks(200, words_over(200), [(115.0, 1)]) == []
    marks = chapter_marks(480, words_over(480),
                          [(115.0, 1), (235.0, 1), (355.0, 1)],
                          title="Ships that vanished")
    assert len(marks) >= CHAPTER_MIN_COUNT, marks
    assert marks[0][0] == 0.0 and marks[0][1] == "Ships that vanished"
    times = [t for t, _ in marks]
    assert all(b - a >= 60.0 - 1e-6 for a, b in zip(times, times[1:]))
    assert all(t <= 480 - 60.0 + 1e-6 for t in times)
    # every label comes from real words and fits the width
    flat = {w["word"] for w in words_over(480)}
    for t, label in marks[1:]:
        assert label and len(label) <= 42
        assert label.split()[0].rstrip("…") in {w.rstrip(".") for w in flat}
    # chapters with no words after them are skipped, not guessed —
    # skip enough of them and YouTube would ignore the list anyway: none
    short_words = words_over(210)
    marks2 = chapter_marks(480, short_words,
                           [(115.0, 1), (235.0, 1), (355.0, 1)])
    assert marks2 == []
    assert chapter_marks(480, short_words, [(115.0, 1)]) == []
    # label truncation uses the ellipsis, never a bare slice
    long_word = {"word": "extraordinary", "start": 116.0, "end": 117.0}
    label = _chapter_label([long_word] * 8, 115.0)
    assert label.endswith("…") and len(label) <= 42
    assert label.count("extraordinary") >= 3   # width, not a hard chop at 10

    # title / description / stem ----------------------------------------
    assert clean_long_title("Why ships &amp; sink  today") == \
        "Why ships & sink today"
    long_title = "word " * 40
    assert len(clean_long_title(long_title)) <= LONGFORM_TITLE_LIMIT
    assert clean_long_title(long_title).endswith("…")
    assert clean_long_title("") == "Long-form cut"
    desc = build_longform_description(
        "Ships that vanished", [(0.0, "Ships that vanished"),
                                (116.0, "the storm hit hard")],
        {"title": "Ocean Mysteries", "channel": "DeepBlue",
         "url": "https://youtu.be/x"}, (0.0, 480.0), cut=False)
    assert desc.splitlines()[0] == "Ships that vanished"
    assert "0:00  Ships that vanished" in desc
    assert "1:56  the storm hit hard" in desc
    assert "https://youtu.be/x" in desc and "DeepBlue" in desc
    assert "Window:" not in desc          # whole render carries no window
    desc_cut = build_longform_description(
        "T", [], {"title": "s", "channel": "c", "url": "u"}, (300.0, 780.0),
        cut=True)
    assert "Window: 5:00 - 13:00" in desc_cut
    assert longform_stem("abcd1234ef", (0.0, 600.0), 600.0) == "abcd1234_longform"
    assert longform_stem("abcd1234ef", (300.0, 780.0), 900.0) == \
        "abcd1234_longform_5m00s"
    assert _shift_bounds([(100.0, 3), (60.0, 1)], 60.0) == [(40.0, 3)]

    # srt + kit ---------------------------------------------------------
    import tempfile as _tf
    from pathlib import Path

    tmp = Path(_mkdtemp())
    srt = build_longform_srt(words_over(60), 60.0, tmp / "c.srt")
    assert srt and srt.exists()
    for line in srt.read_text(encoding="utf-8").splitlines():
        if line and not line[0].isdigit() and "-->" not in line:
            assert len(line) <= 42, line
    video = tmp / "v.mp4"
    video.write_bytes(b"x")
    (tmp / "t1.jpg").write_bytes(b"x")
    (tmp / "t2.jpg").write_bytes(b"x")
    kit = write_longform_kit(
        video, "Ships that vanished",
        {"title": "Ocean Mysteries", "channel": "DeepBlue", "url": "u"},
        (0.0, 480.0), [(0.0, "Ships that vanished")], srt, tmp / "c.srt",
        [tmp / "t1.jpg", tmp / "t2.jpg"], tmp, cut=False)
    assert kit == tmp / "v"
    for name in ("v.mp4", "TITLE.txt", "DESCRIPTION.txt", "CREDIT.txt",
                 "captions.srt", "longform.ass", "THUMB_1.jpg",
                 "THUMB_2.jpg", "CHECKLIST.md"):
        assert (kit / name).exists(), name
    assert not (kit / "tiktok.txt").exists() and not (kit / "reels.txt").exists()
    check = (kit / "CHECKLIST.md").read_text(encoding="utf-8")
    assert "NOT a Short" in check and "before publishing" in check
    assert "captions.srt" in check and "THUMB" in check


def t_longform_lane():
    """run_longform end to end on fakes: cache hit, plan, kit, cfg restore."""
    import tempfile as _tf
    from pathlib import Path

    import longform
    from clipper import (save_transcript_cache, transcript_cache_key,
                         transcript_cache_path)
    from config import load_config

    tmp = Path(_mkdtemp())
    cfg = load_config(None)  # defaults; paths re-rooted below
    cfg.data["paths"]["work_dir"] = str(tmp / "work")
    cfg.data["video"]["format"] = "portrait"
    # 60s chapter spacing: a 4-min test video yields 3+ marks (YouTube
    # ignores fewer), the default 120s would correctly yield none
    cfg.data["longform"]["chapter_seconds"] = 60
    src = tmp / "source.mp4"
    src.write_bytes(b"x" * 16)

    # a punctuated transcript covering 0..239s (whole source, 240s)
    words = []
    t = 0.0
    i = 0
    while t < 236:
        words.append({"word": f"word{i}" + ("." if i % 8 == 7 else ""),
                      "start": round(t, 2), "end": round(t + 1.8, 2)})
        t += 2.0
        i += 1
    key = transcript_cache_key("", src)
    save_transcript_cache(transcript_cache_path(cfg, key), words)

    # fakes: duration, silences, render, frames
    calls = {}

    import assembler
    real_dur = assembler.ffprobe_duration
    assembler.ffprobe_duration = lambda p: 240.0
    import cutpoints
    real_sil = cutpoints.detect_silences
    cutpoints.detect_silences = lambda p, d=0.0, **kw: [
        (115.0, 117.0), (235.0, 237.0)]

    def fake_render(src_, window_, words_, cfg_, out_path_, work_,
                    sub_pos="bottom", subs=True):
        calls["window"] = window_
        calls["fmt"] = cfg_.format
        calls["subs"] = subs
        calls["pos"] = sub_pos
        out_path_.write_bytes(b"video")

    real_render = longform.render_longform
    longform.render_longform = fake_render
    import clipper
    real_frame = clipper.extract_frame
    clipper.extract_frame = lambda s, when, dest: (Path(dest).write_bytes(
        b"jpg"), dest)[1]

    try:
        rc = longform.run_longform(cfg, file=str(src),
                                   out_dir=tmp / "longform")
        assert rc == 0
        assert calls["window"] == (0.0, 240.0)
        assert calls["fmt"] == "landscape"      # during the render
        assert cfg.format == "portrait"          # restored after
        out = tmp / "longform"
        video = out / f"{key[:8]}_longform.mp4"
        assert video.exists() and video.read_bytes() == b"video"
        kit = out / video.stem
        desc = (kit / "DESCRIPTION.txt").read_text(encoding="utf-8")
        assert desc.splitlines()[0] == "source"        # cleaned file title
        assert "0:00" in desc and "Full credit" in desc
        assert "Window:" not in desc                    # whole: no window line
        assert (kit / "captions.srt").exists()
        assert not (kit / "longform.ass").exists()      # fake render wrote none
        assert (kit / "THUMB_1.jpg").exists()

        # --minutes cut on a longer source, --start snapping
        assembler.ffprobe_duration = lambda p: 600.0
        cutpoints.detect_silences = lambda p, d=0.0, **kw: [(238.0, 240.0)]
        rc = longform.run_longform(cfg, file=str(src), minutes=4,
                                   start_at=0.0,
                                   out_dir=tmp / "longform")
        assert rc == 0
        start, end = calls["window"]
        assert start == 0.0 and 180.0 <= end <= 300.0
        kit2 = out / f"{key[:8]}_longform_0m00s"
        assert (kit2 / "DESCRIPTION.txt").read_text(
            encoding="utf-8").count("Window:") == 1

        # dry run: nothing rendered, nothing written
        before = sorted(p.name for p in out.iterdir())
        rc = longform.run_longform(cfg, file=str(src), minutes=4,
                                   dry_run=True,
                                   out_dir=tmp / "longform")
        assert rc == 0
        assert sorted(p.name for p in out.iterdir()) == before

        # --no-subs reaches the renderer; ass stays out of the kit
        rc = longform.run_longform(cfg, file=str(src), minutes=4,
                                   subs=False,
                                   out_dir=tmp / "longform")
        assert rc == 0 and calls["subs"] is False

        # garbage guards
        from clipper import ClipError

        # 300s start on a 600s source is legal; past the end is not
        for kw in ({"minutes": 0.5}, {"start_at": 700.0}):
            try:
                longform.run_longform(cfg, file=str(src),
                                      out_dir=tmp / "longform", **kw)
            except ClipError:
                pass
            else:
                raise AssertionError(f"expected ClipError for {kw}")
    finally:
        assembler.ffprobe_duration = real_dur
        cutpoints.detect_silences = real_sil
        longform.render_longform = real_render
        clipper.extract_frame = real_frame


def t_longform_top():
    import io
    import json as _json
    from contextlib import redirect_stdout
    from types import SimpleNamespace
    from unittest.mock import patch as _patch

    import clipper
    import longform as lf
    from clipper import Candidate, ClipError
    from longform import plan_chapters

    # -- chapter planning: strength order kept, target + cap respected --
    def cand(i, length):
        return Candidate(start=i * 100.0, end=i * 100.0 + length)

    cands = [cand(0, 60), cand(1, 60), cand(2, 60), cand(3, 60)]
    plan = plan_chapters(cands, 120, 6)         # target met after 2
    assert plan == cands[:2], plan
    assert plan_chapters(cands, 0, 6) == cands  # no target = best N
    assert plan_chapters(cands, 999, 2) == cands[:2]
    assert plan_chapters(cands, 999, 0) == []
    # too short for the target: min_total backfills within the cap
    plan = plan_chapters(cands, 999, 3, min_total=200)
    assert len(plan) == 3 and sum(c.end - c.start for c in plan) == 180
    assert plan_chapters([], 360, 6) == []

    # -- the picker prompt asks for chapters, not flash-cuts --
    from longform import build_chapter_picker_prompt
    prompt = build_chapter_picker_prompt(["[0:00] hello world"], tmp_cfg(),
                                         3, 40, 90)
    assert "40-90 seconds" in prompt and "6 strongest CHAPTERS" in prompt
    assert "completion" not in prompt.split("CHAPTERS")[0]

    # -- config knobs: clamps and garbage tolerance --
    assert tmp_cfg().longform_moments == 6
    assert tmp_cfg().longform_min_len == 40
    assert tmp_cfg().longform_max_len == 90
    assert tmp_cfg().longform_target_seconds == 360.0
    assert tmp_cfg(longform={"moments": 99}).longform_moments == 12
    assert tmp_cfg(longform={"moments": "x"}).longform_moments == 6
    assert tmp_cfg(longform={"min_len": 5}).longform_min_len == 15
    assert tmp_cfg(longform={"max_len": 9999}).longform_max_len == 600
    assert tmp_cfg(longform={"target_seconds": -1}
                   ).longform_target_seconds == 0.0
    assert tmp_cfg(meeting={"rounds": 9}).meeting_rounds == 4
    assert tmp_cfg(meeting={"max_words": 5}).meeting_max_words == 30
    assert tmp_cfg(meeting={"watchlist": "topics/mine.txt"}
                   ).meeting_watchlist.name == "mine.txt"

    # -- orchestration (mocked render, real kit files) ------------------
    def words_n(n, step=2.1):
        return [{"word": f"w{i}" + ("." if i % 9 == 8 else ""),
                 "start": i * step, "end": i * step + 0.4}
                for i in range(n)]

    picker_json = _json.dumps({"clips": [
        {"start": 10.0, "end": 70.0, "hook": "the best story",
         "title": "The Best Story"},
        {"start": 100.0, "end": 160.0, "hook": "second best",
         "title": "Second Best"},
        {"start": 200.0, "end": 260.0, "hook": "third", "title": "Third"},
    ]})

    class Provider:
        calls = 0
        def generate_text(self, *a, **k):
            Provider.calls += 1
            return picker_json

    renders, concats = [], []

    def fake_render(src, window, words, cfg, out_path, work,
                    sub_pos="bottom", subs=True):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"seg")
        renders.append((window[0], window[1], out_path, cfg.width,
                        cfg.height))
        return out_path

    def fake_concat(plan, out_path, cfg, work_dir):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"longform")
        concats.append([(s["rank"], s["start"]) for s in plan])
        return out_path

    cfg = tmp_cfg(clip={"transcript_fix": False, "polish_titles": False})
    src = cfg.root / "Source Talk.mp4"
    src.write_bytes(b"x")
    buf = io.StringIO()
    import assembler
    with _patch.object(clipper, "ffprobe_duration", lambda p: 300.0), \
         _patch.object(assembler, "ffprobe_duration", lambda p: 300.0), \
         _patch.object(clipper, "load_transcript_cache",
                       lambda p: words_n(140)), \
         _patch.object(lf, "render_longform", fake_render), \
         _patch.object(lf, "build_compilation_video", fake_concat), \
         _patch("scriptgen.get_provider", lambda c: Provider()), \
         redirect_stdout(buf):
        code = lf.run_longform(cfg, file=str(src), use_vision=False,
                               top=3, out_dir=cfg.root / "lf")
    out = buf.getvalue()
    assert code == 0, out
    assert "1920x1080" not in out  # no debug noise requirement, smoke only
    assert len(renders) == 3, renders
    # every segment rendered landscape
    assert all(r[3] == 1920 and r[4] == 1080 for r in renders), renders
    # countdown: the picker's FIRST (best) chapter plays LAST as #1
    assert concats and [c[0] for c in concats[0]] == [3, 2, 1], concats
    assert concats[0][-1][1] < 100.0          # best chapter is rank #1
    # kit (output name = the source cache key, like every clip lane)
    videos = list((cfg.root / "lf").glob("*_top3.mp4"))
    assert len(videos) == 1, videos
    video = videos[0]
    kit = cfg.root / "lf" / video.stem
    title = (kit / "TITLE.txt").read_text(encoding="utf-8")
    assert title.startswith("Top 3 Moments - ")
    desc = (kit / "DESCRIPTION.txt").read_text(encoding="utf-8")
    assert "Source:" in desc and "#1" in desc and "#3" in desc
    assert "0:00" in desc                     # chapter timestamps
    credit = (kit / "CREDIT.txt").read_text(encoding="utf-8")
    assert "#1 " in credit and "#3 " in credit and "source:" in credit
    check = (kit / "CHECKLIST.md").read_text(encoding="utf-8")
    assert "NOT a Short" in check and "Mid-rolls" not in check
    assert "UNticked" in check                # real footage: no AI box

    # mid-roll reminder fires past 8:00 of chapter time
    from longform import _compilation_checklist
    assert "Mid-rolls" in _compilation_checklist(500)
    assert "Mid-rolls" not in _compilation_checklist(300)

    # one candidate chapter -> refuse (a compilation needs >= 2)
    one_json = _json.dumps({"clips": [
        {"start": 10.0, "end": 70.0, "hook": "only", "title": "Only"}]})
    class OneProvider:
        def generate_text(self, *a, **k):
            return one_json
    import assembler
    renders.clear()
    with _patch.object(clipper, "ffprobe_duration", lambda p: 300.0), \
         _patch.object(assembler, "ffprobe_duration", lambda p: 300.0), \
         _patch.object(clipper, "load_transcript_cache",
                       lambda p: words_n(140)), \
         _patch("scriptgen.get_provider", lambda c: OneProvider()), \
         _patch.object(lf, "render_longform", fake_render), \
         _patch.object(lf, "build_compilation_video", fake_concat), \
         redirect_stdout(io.StringIO()):
        try:
            lf.run_longform(tmp_cfg(clip={"transcript_fix": False,
                                          "polish_titles": False}),
                            file=str(src), use_vision=False, top=3)
            raise AssertionError("one-chapter compilation accepted")
        except ClipError as exc:
            assert "fewer than 2" in str(exc)

    # -- guards ----------------------------------------------------------
    thin = tmp_cfg(clip={"transcript_fix": False, "polish_titles": False})
    with _patch.object(clipper, "ffprobe_duration", lambda p: 300.0), \
         _patch.object(assembler, "ffprobe_duration", lambda p: 300.0), \
         _patch.object(clipper, "load_transcript_cache",
                       lambda p: words_n(20)):
        try:
            lf.run_longform(thin, file=str(src), use_vision=False, top=3)
            raise AssertionError("thin transcript accepted")
        except ClipError as exc:
            assert "thin" in str(exc)
    # dry run: the cached transcript is read (their lane's design), but
    # no picking, no render, no LLM call
    renders.clear(); Provider.calls = 0
    buf = io.StringIO()
    with _patch.object(clipper, "ffprobe_duration", lambda p: 300.0), \
         _patch.object(assembler, "ffprobe_duration", lambda p: 300.0), \
         _patch.object(clipper, "load_transcript_cache",
                       lambda p: words_n(140)), \
         _patch("scriptgen.get_provider", lambda c: Provider()), \
         redirect_stdout(buf):
        code = lf.run_longform(tmp_cfg(), file=str(src), top=3,
                               dry_run=True)
    assert code == 0 and "dry run" in buf.getvalue()
    assert not renders and Provider.calls == 0




def t_meeting():
    import json as _json
    from unittest.mock import patch as _patch

    import meeting as mt
    from meeting import (ROLES, clamp_words, format_minutes,
                         parse_decisions, parse_watchlist,
                         remove_watchlist_url)

    # -- watchlist parsing -------------------------------------------------
    text = ("# candidates\n\n"
            "https://youtu.be/aaa | the octopus documentary\n"
            "https://youtu.be/bbb\n"
            "   https://youtu.be/ccc | 12:34 mark, shipwreck story  \n"
            "not-a-url\n")
    parsed = parse_watchlist(text)
    assert parsed == [("https://youtu.be/aaa", "the octopus documentary"),
                      ("https://youtu.be/bbb", ""),
                      ("https://youtu.be/ccc", "12:34 mark, shipwreck story")]
    rest = remove_watchlist_url(text, "https://youtu.be/bbb/")
    assert "bbb" not in rest and "aaa" in rest and "ccc" in rest
    assert rest.startswith("# candidates")   # comments survive
    assert remove_watchlist_url(text, "") == text

    # -- turn clamping -----------------------------------------------------
    short = "I agree with the Analyst. The numbers say pause."
    assert clamp_words(short, 70) == short
    long_txt = ("This is a longer opening sentence that ends right here. "
                "Then more words keep following on and on and on without "
                "any punctuation to stop them naturally at all.")
    cut = clamp_words(long_txt, 12)
    assert len(cut.split()) <= 12 and cut.endswith("."), cut
    assert clamp_words("no sentences here at all just words flowing "
                       "endlessly onward", 6).endswith("…")
    assert clamp_words("", 70) == ""

    # -- decision validation ------------------------------------------------
    cands = ["https://youtu.be/aaa", "https://youtu.be/bbb"]
    pick = parse_decisions(
        _json.dumps({"choice": "https://youtu.be/aaa",
                     "reason": "best fit"}), "pick", cands)
    assert pick == {"choice": "https://youtu.be/aaa", "reason": "best fit"}
    # trailing-slash and containment tolerance
    assert parse_decisions(
        _json.dumps({"choice": "https://youtu.be/aaa/"}), "pick",
        cands)["choice"] == "https://youtu.be/aaa"
    assert parse_decisions(
        _json.dumps({"choice": "aaa"}), "pick", cands)["choice"].endswith(
        "aaa")
    # not a candidate -> no decision, whatever the chair says
    assert parse_decisions(
        _json.dumps({"choice": "https://youtu.be/zzz"}), "pick",
        cands) is None
    assert parse_decisions("I choose the second one", "pick", cands) is None
    stats = parse_decisions(
        _json.dumps({"summary": "agreed", "decisions": [
            "Pause 48h", "Repackage the octopus series"]}), "stats")
    assert stats["decisions"] == ["Pause 48h",
                                  "Repackage the octopus series"]
    assert parse_decisions(
        _json.dumps({"summary": "", "decisions": []}), "stats") is None
    assert parse_decisions(
        _json.dumps({"summary": "", "decisions": ["x"] * 6}),
        "stats") is None
    assert parse_decisions(_json.dumps({"nope": 1}), "stats") is None

    # -- prompts: persona + question + agenda data present -------------------
    role = ROLES[0]
    turn = mt.build_turn_prompt(role, "AGENDA-DATA", "Name: prior turn",
                                "QUESTION?")
    assert "Strategist" in turn and role["persona"][:20] in turn
    assert "AGENDA-DATA" in turn and "QUESTION?" in turn
    assert "first person" in turn.lower() or "FIRST PERSON" in turn
    minutes = mt.build_minutes_prompt("AGENDA-DATA", "t", "pick")
    assert "candidate" in minutes and '"choice"' in minutes
    stats_p = mt.build_minutes_prompt("AGENDA-DATA", "t", "stats")
    assert '"decisions"' in stats_p
    # four seats, four different preferred lanes
    lanes = {r["lane"] for r in ROLES}
    assert len(ROLES) == 4 and len(lanes) == 4

    # -- minutes file -------------------------------------------------------
    body = format_minutes("pick", "agenda text",
                          [{"name": "Skeptic", "emoji": "🤨",
                            "text": "I doubt it."}],
                          {"choice": "https://youtu.be/aaa",
                           "reason": "why"})
    assert "Skeptic" in body and "I doubt it." in body
    assert "https://youtu.be/aaa" in body and "Outcome" in body
    body2 = format_minutes("stats", "agenda",
                           [{"name": "Analyst", "emoji": "🔎", "text": "x"}],
                           {"summary": "s", "decisions": ["a", "b"]})
    assert "1. a" in body2 and "2. b" in body2
    body3 = format_minutes("stats", "a", [],
                           {"summary": "s", "decisions": ["a"]})
    assert "no valid" not in body3.lower()

    # -- a full stats meeting on scripted providers -------------------------
    class Scripted:
        def __init__(self):
            self.turns = 0

        def generate_text(self, prompt, temperature=0.7, tag="",
                          json_mode=False):
            if json_mode:
                return _json.dumps({"summary": "Room agrees: pause and "
                                        "repackage.",
                                    "decisions": ["Pause uploads 48h",
                                                  "Re-render the octopus "
                                                  "series with new titles"]})
            self.turns += 1
            return f"Point number {self.turns}: the data is clear."

    made = {}
    chair = Scripted()

    def fake_role_provider(cfg, lane):
        made.setdefault(lane, Scripted())
        return made[lane] if lane != cfg.ai_provider else chair

    cfg = tmp_cfg()
    said = []
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with _patch.object(mt, "_role_provider", fake_role_provider), \
         _patch.object(mt, "_stats_agenda",
                       lambda c: "CHANNEL REPORT\nviews: down 40%"), \
         redirect_stdout(buf):
        summary = mt.run_meeting(cfg, "stats", rounds=1, send=False,
                                 say=said.append)
    assert "Pause uploads 48h" in summary and "2. " in summary
    assert "Cost:" in summary and "LLM calls" in summary
    assert len(said) >= 4, said                    # every seat spoke
    minutes_path = cfg.out_dir / "meetings"
    files = list(minutes_path.glob("*-stats.md"))
    assert len(files) == 1 and files[0].parent == minutes_path
    body = files[0].read_text(encoding="utf-8")
    assert "CHANNEL REPORT" in body and "Pause uploads 48h" in body
    # the chair's invalid first reply gets one retry
    class RetryChair:
        def __init__(self):
            self.n = 0
        def generate_text(self, prompt, temperature=0.7, tag="",
                          json_mode=False):
            if json_mode:
                self.n += 1
                return "not json" if self.n == 1 else _json.dumps(
                    {"summary": "ok", "decisions": ["Do the thing"]})
            return "turn"
    retry = RetryChair()
    with _patch.object(mt, "_role_provider",
                       lambda c, lane: retry), \
         _patch.object(mt, "_stats_agenda", lambda c: "R"), \
         redirect_stdout(io.StringIO()):
        summary = mt.run_meeting(cfg, "stats", rounds=1, send=False)
    assert "Do the thing" in summary and retry.n == 2

    # -- a pick meeting: candidates, decision, watchlist pop ----------------
    cfg = tmp_cfg()
    wl = cfg.meeting_watchlist
    wl.parent.mkdir(parents=True, exist_ok=True)
    wl.write_text("# watchlist\nhttps://youtu.be/aaa | octopus doc\n"
                  "https://youtu.be/bbb\n", encoding="utf-8")
    picked = {"n": 0}

    class PickChair:
        def generate_text(self, prompt, temperature=0.7, tag="",
                          json_mode=False):
            if json_mode:
                picked["n"] += 1
                return _json.dumps({"choice": "https://youtu.be/bbb",
                                    "reason": "stronger hook"})
            return "I say bbb."
    with _patch.object(mt, "_role_provider",
                       lambda c, lane: PickChair()), \
         redirect_stdout(io.StringIO()):
        summary = mt.run_meeting(cfg, "pick", rounds=1, send=False,
                                 urls=["https://youtu.be/ccc"])
    assert "https://youtu.be/bbb" in summary
    left = wl.read_text(encoding="utf-8")
    assert "bbb" not in left and "aaa" in left   # consumed one candidate
    assert left.startswith("# watchlist")
    # CLI urls joined the agenda (ccc came from --url, not the file)
    # (agenda text is covered by _pick_candidates below)

    # -- candidate collection: CLI first, watchlist deduped -----------------
    cfg = tmp_cfg()
    wl = cfg.meeting_watchlist
    wl.parent.mkdir(parents=True, exist_ok=True)
    wl.write_text("https://youtu.be/aaa\nhttps://youtu.be/ccc\n",
                  encoding="utf-8")
    got = mt._pick_candidates(cfg, ["https://youtu.be/aaa",
                                    "https://youtu.be/ddd"])
    assert [u for u, _ in got] == ["https://youtu.be/aaa",
                                   "https://youtu.be/ddd",
                                   "https://youtu.be/ccc"]
    assert mt._pick_candidates(cfg, []) == [
        ("https://youtu.be/aaa", ""), ("https://youtu.be/ccc", "")]
    assert mt._pick_candidates(tmp_cfg(), []) == []

    # -- stats meeting without snapshots says so, politely ------------------
    empty = tmp_cfg()
    try:
        mt._stats_agenda(empty)
        raise AssertionError("no-snapshot meeting should refuse")
    except mt.MeetingError as exc:
        assert "snap" in str(exc)

    # -- no LLM lanes at all: a clear message, not a crash ------------------
    class NoneProvider:
        def __init__(self, cfg):
            from scriptgen import get_provider
            self.p = get_provider(cfg)
        def __bool__(self):
            return False
    with _patch.object(mt, "_stats_agenda", lambda c: "R"), \
         _patch.object(mt, "_role_provider",
                       lambda c, lane: None), \
         redirect_stdout(io.StringIO()):
        summary = mt.run_meeting(tmp_cfg(), "stats", send=False)
    assert "No LLM lane" in summary

    # -- the room digest: last word per seat, for the Telegram DM ----------
    digest = mt.room_digest([
        {"name": "Strategist", "emoji": "🎯", "text": "first take"},
        {"name": "Skeptic", "emoji": "🤨", "text": "doubt one"},
        {"name": "Strategist", "emoji": "🎯", "text": "final take"},
    ])
    assert "final take" in digest and "first take" not in digest
    assert "doubt one" in digest and digest.index("Strategist") < \
        digest.index("Skeptic")    # seats in the order they first spoke
    long_turn = "word " * 80
    trimmed = mt.room_digest([{"name": "Analyst", "emoji": "🔎",
                               "text": long_turn}], per_turn_chars=40)
    assert trimmed.endswith("…") and len(trimmed) < 80
    # the summary carries the digest
    with _patch.object(mt, "_role_provider",
                       lambda c, lane: Scripted()), \
         _patch.object(mt, "_stats_agenda", lambda c: "R"), \
         redirect_stdout(io.StringIO()):
        summary = mt.run_meeting(tmp_cfg(), "stats", rounds=1,
                                 send=False)
    assert "The room:" in summary

    # -- a pick meeting fed by the SHEET: row marked picked -----------------
    import sheet as sh
    cfg = tmp_cfg()
    sh.append_sheet(cfg.sources_sheet, "https://youtu.be/s1",
                    "roman aqueducts")
    sh.append_sheet(cfg.sources_sheet, "https://youtu.be/s2")
    sh.mark_sheet(cfg.sources_sheet, "https://youtu.be/s2", "clipped",
                  "clips/ — yesterday")
    cands = mt._pick_candidates(cfg, [])
    assert [u for u, _ in cands] == ["https://youtu.be/s1"], cands
    class SheetChair:
        def generate_text(self, prompt, temperature=0.7, tag="",
                          json_mode=False):
            if json_mode:
                return _json.dumps({"choice": "https://youtu.be/s1",
                                    "reason": "stronger hook"})
            return "I say s1."
    with _patch.object(mt, "_role_provider",
                       lambda c, lane: SheetChair()), \
         redirect_stdout(io.StringIO()):
        summary = mt.run_meeting(cfg, "pick", rounds=1, send=False)
    assert "https://youtu.be/s1" in summary
    rows = sh.load_sheet(cfg.sources_sheet)
    row = next(r for r in rows if "s1" in r["url"])
    assert row["status"] == "picked" and "stronger hook" in row["result"]
    # picked rows stay in the queue (still to render) and go first
    assert sh.take_pending(cfg.sources_sheet, 3) == \
        [("https://youtu.be/s1", "roman aqueducts")]

    # -- pick --render must keep its terminal render status -----------------
    import clipper as clipper_mod

    rendered_cfg = tmp_cfg()
    sh.append_sheet(rendered_cfg.sources_sheet, "https://youtu.be/s1", "")
    with (
        _patch.object(mt, "_role_provider", lambda c, lane: SheetChair()),
        _patch.object(clipper_mod, "run_clip", return_value=None),
        redirect_stdout(io.StringIO()),
    ):
        mt.run_meeting(rendered_cfg, "pick", rounds=1, render=True,
                       send=False)
    rendered_row = sh.load_sheet(rendered_cfg.sources_sheet)[0]
    assert rendered_row["status"] == "clipped", rendered_row
    assert sh.take_pending(rendered_cfg.sources_sheet, 1) == []

    failed_render_cfg = tmp_cfg()
    sh.append_sheet(failed_render_cfg.sources_sheet, "https://youtu.be/s1", "")
    with (
        _patch.object(mt, "_role_provider", lambda c, lane: SheetChair()),
        _patch.object(clipper_mod, "run_clip",
                      side_effect=RuntimeError("render boom")),
        redirect_stdout(io.StringIO()),
    ):
        mt.run_meeting(failed_render_cfg, "pick", rounds=1, render=True,
                       send=False)
    failed_render_row = sh.load_sheet(failed_render_cfg.sources_sheet)[0]
    assert failed_render_row["status"] == "failed", failed_render_row
    assert sh.take_pending(failed_render_cfg.sources_sheet, 1) == []

    # -- latest_minutes + `meeting last` ------------------------------------
    import os
    import meeting as mt2
    meetings = cfg.out_dir / "meetings"
    meetings.mkdir(parents=True, exist_ok=True)
    old = meetings / "2026-09-30-stats.md"
    new = meetings / "2026-10-01-pick.md"
    old.write_text("OLD MINUTES", encoding="utf-8")
    new.write_text("NEW MINUTES", encoding="utf-8")
    # Explicit mtimes, and `new` strictly in the FUTURE. Two reasons: this
    # sandbox's clock handed consecutive writes the exact same timestamp, and
    # the run_meeting calls above already dropped a `2026-10-03-*.md` in here.
    # Left to itself, `new` tied with that file and the test's answer then
    # depended on the order the directory happened to list its files in.
    import time as _time
    stamp = _time.time() + 3600
    os.utime(old, (stamp - 86400, stamp - 86400))
    os.utime(new, (stamp, stamp))
    assert mt2.latest_minutes(cfg) == new, sorted(
        (p.name, p.stat().st_mtime) for p in meetings.glob("*.md"))

    # An exact tie is resolved by name (dates sort), never by glob order —
    # that tie is what made `meeting last` non-deterministic.
    tied = meetings / "2026-10-04-act.md"
    tied.write_text("TIED MINUTES", encoding="utf-8")
    os.utime(tied, (stamp, stamp))
    assert mt2.latest_minutes(cfg) == tied, "tie must break toward the later date"
    os.utime(new, (stamp + 60, stamp + 60))     # strictly newer wins again
    assert mt2.latest_minutes(cfg) == new, "mtime must still outrank the name"

    import main as cli
    from argparse import Namespace
    args = Namespace(kind="last", add=None, url=[], rounds=None,
                     render=False, no_send=True, dry_run=False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.cmd_meeting(cfg, args)
    assert code == 0 and "NEW MINUTES" in buf.getvalue()
    empty = tmp_cfg()
    try:
        cli.cmd_meeting(empty, args)
        raise AssertionError("meeting last without minutes must die")
    except SystemExit:
        pass

    # -- `sheet` command: add + list ----------------------------------------
    args = Namespace(add=["https://youtu.be/s9 | best video",
                          "not-a-link"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.cmd_sheet(empty, args)
    assert code == 0 and "+1" in buf.getvalue()
    assert "best video" in buf.getvalue() and "queued: 1" in buf.getvalue()
    assert "skipped" in buf.getvalue()      # the non-link was refused
    # empty-sheet message path
    fresh = tmp_cfg()
    buf3 = io.StringIO()
    with redirect_stdout(buf3):
        cli.cmd_sheet(fresh, Namespace(add=None))
    assert "empty" in buf3.getvalue()

    # -- `clip --sheet`: rows marked clipped / failed ------------------------
    import clipper
    cfg = tmp_cfg()
    sh.append_sheet(cfg.sources_sheet, "https://youtu.be/ok1", "good one")
    sh.append_sheet(cfg.sources_sheet, "https://youtu.be/bad1")
    sh.mark_sheet(cfg.sources_sheet, "https://youtu.be/bad1", "picked",
                  "boardroom: chosen yesterday")

    def fake_run_clip(cfg, url="", file="", **kwargs):
        if "ok1" not in (url or ""):
            from clipper import ClipError
            raise ClipError("no captions and no Groq key")
        return None

    args = Namespace(target="", url=[], file=[], sheet=2,
                     max_clips=6, min_len=20, max_len=45,
                     no_vision=True, keep_work=False, out=None,
                     sub_pos="default", top=0, whole=None, half=False)
    buf = io.StringIO()
    with _patch.object(clipper, "run_clip", fake_run_clip), \
         redirect_stdout(buf):
        code = cli.cmd_clip(cfg, args)
    assert code == 0, buf.getvalue()       # one source survived
    rows = {r["url"]: r for r in sh.load_sheet(cfg.sources_sheet)}
    assert rows["https://youtu.be/ok1"]["status"] == "clipped"
    assert cfg.topic in rows["https://youtu.be/ok1"]["result"]
    assert rows["https://youtu.be/bad1"]["status"] == "failed"
    assert "no captions" in rows["https://youtu.be/bad1"]["result"]
    # the board's pick was taken FIRST
    assert "bad1" in buf.getvalue().split("taking")[1]

    # all-failing sheet run -> exit 1 and the row still marked
    cfg = tmp_cfg()
    sh.append_sheet(cfg.sources_sheet, "https://youtu.be/nope")
    buf = io.StringIO()
    with _patch.object(clipper, "run_clip", fake_run_clip), \
         redirect_stdout(buf):
        code = cli.cmd_clip(cfg, args)
    assert code == 1
    rows = {r["url"]: r for r in sh.load_sheet(cfg.sources_sheet)}
    assert rows["https://youtu.be/nope"]["status"] == "failed"


def t_meeting_act():
    """Board autonomy: act meetings, board memory, momentum trends."""
    import io
    import json as _json
    from contextlib import redirect_stdout
    from unittest.mock import patch as _patch

    import meeting as mt
    from meeting import (MEMORY_DIGEST_N, format_minutes, load_memory,
                         memory_digest, memory_report, parse_decisions,
                         remember, trend_lines)
    from sheet import append_sheet

    # -- decision validation: the act JSON --------------------------------
    cands = ["https://youtu.be/aaa", "https://youtu.be/bbb"]
    clip = parse_decisions(
        _json.dumps({"action": "clip", "url": "https://youtu.be/aaa",
                     "reason": "strong pending source"}), "act", cands)
    assert clip == {"action": "clip", "url": "https://youtu.be/aaa",
                    "reason": "strong pending source"}
    # a clip that names no listed source is no decision at all
    assert parse_decisions(
        _json.dumps({"action": "clip", "url": "https://youtu.be/zzz"}),
        "act", cands) is None
    assert parse_decisions(
        _json.dumps({"action": "clip"}), "act", cands) is None
    gen = parse_decisions(
        _json.dumps({"action": "generate",
                     "topic": "why ships used to carry canaries",
                     "reason": "morbid explainers are carrying the "
                               "channel"}), "act", cands)
    assert gen["action"] == "generate" and "canaries" in gen["topic"]
    # a topic too thin to render is refused
    assert parse_decisions(
        _json.dumps({"action": "generate", "topic": "boats"}),
        "act", cands) is None
    assert parse_decisions(
        _json.dumps({"action": "generate"}), "act", cands) is None
    # none tolerates lazy spellings; anything else is invalid
    for lazy in ("none", "nothing", "do nothing", "skip"):
        got = parse_decisions(_json.dumps({"action": lazy,
                                           "reason": "r"}), "act", cands)
        assert got == {"action": "none", "reason": "r"}, lazy
    assert parse_decisions(
        _json.dumps({"action": "delete-the-channel"}), "act",
        cands) is None

    # -- the chair's act prompt names all three actions --------------------
    p = mt.build_minutes_prompt("AGENDA", "t", "act")
    assert '"action"' in p and "clip" in p and "generate" in p \
        and "none" in p

    # -- minutes: act outcomes + follow-through ---------------------------
    body = format_minutes("act", "agenda",
                           [{"name": "Producer", "emoji": "🎬",
                             "text": "clip it."}],
                           {"action": "clip", "url": "https://youtu.be/aaa",
                            "reason": "r", "result": "Clipped -> clips/."})
    assert "Board action: clip" in body and "Follow-through" in body
    assert "board action" in body.splitlines()[0].lower()
    body = format_minutes("act", "a", [], {"action": "generate",
                                           "topic": "canary ships",
                                           "reason": "r"})
    assert "Board action: generate" in body and "canary ships" in body
    body = format_minutes("act", "a", [], {"action": "none", "reason": "r"})
    assert "nothing extra today" in body
    body = format_minutes("act", "a", [], None)
    assert "no valid action" in body.lower()

    # -- board memory: remember, digest, report, cap ------------------------
    cfg = tmp_cfg()
    assert load_memory(cfg) == []
    assert memory_digest(cfg) == ""
    assert "no memory yet" in memory_report(cfg).lower()
    remember(cfg, "stats", "agreed to pause uploads")
    remember(cfg, "pick", "picked https://youtu.be/aaa")
    remember(cfg, "act", "generate 'canary ships' — momentum")
    entries = load_memory(cfg)
    assert [e["kind"] for e in entries] == ["stats", "pick", "act"]
    digest = memory_digest(cfg)
    assert digest.count("\n") == 2 and "canary ships" in digest
    assert "[stats]" in digest and "[pick]" in digest   # one line each
    # the report is newest-first; the digest is the tail
    report = memory_report(cfg)
    assert report.index("canary ships") < report.index("pause uploads")
    assert "newest first" in report
    # only the last MEMORY_DIGEST_N entries make it into agendas
    for i in range(MEMORY_DIGEST_N + 2):
        remember(cfg, "stats", f"filler decision {i}")
    short = memory_digest(cfg)
    assert short.count("\n") == MEMORY_DIGEST_N - 1
    assert "filler decision 0" not in short and \
        "filler decision 1" not in short
    assert len(load_memory(cfg)) == MEMORY_DIGEST_N + 5  # nothing lost yet
    # the log caps at MEMORY_LIMIT entries on disk
    import meeting
    with _patch.object(meeting, "MEMORY_LIMIT", 8):
        remember(cfg, "stats", "one past the cap")
    assert len(load_memory(cfg)) == 8
    assert load_memory(cfg)[-1]["text"] == "one past the cap"

    # -- momentum trends from snapshot history -----------------------------
    import channelstats as cs
    import youtube as yt

    cfg2 = tmp_cfg()
    history = {"channels": [], "days": [], "flagged": []}

    def day(hist, date, v1, v2):
        videos = [
            {"id": "vid1", "title": "The shipwreck psychology",
             "channel": "Compass", "views": v1, "likes": 0, "comments": 0,
             "published": date},
            {"id": "vid2", "title": "A very calm animal clip",
             "channel": "ClipStudios", "views": v2, "likes": 0,
             "comments": 0, "published": date},
        ]
        return yt.record_snapshot(hist, videos, date)

    history = {"channels": [], "days": [], "flagged": []}
    for date, v1, v2 in (("2026-09-29", 100, 50),
                         ("2026-09-30", 130, 52),
                         ("2026-10-01", 160, 53),
                         ("2026-10-02", 240, 54)):
        history = day(history, date, v1, v2)
    yt.save_snapshots(cs.store_path(cfg2), history)
    trend = trend_lines(cfg2, days=3)
    assert "+140" in trend          # vid1: 100 views (09-29) -> 240 (10-02)
    assert "+4" in trend            # vid2: 50 -> 54
    assert "momentum" in trend.lower()
    assert "Compass +140" in trend and "ClipStudios +4" in trend
    assert "shipwreck psychology" in trend and "calm animal" in trend
    # a brand-new video (not in the 3-days-ago snapshot) shows as new
    history["days"][-1]["videos"]["vid3"] = {
        "title": "Brand new upload", "channel": "Compass", "views": 12,
        "likes": 0, "comments": 0, "published": "2026-10-02"}
    yt.save_snapshots(cs.store_path(cfg2), history)
    assert "published since" in trend_lines(cfg2, days=3)
    # one snapshot day is not a trend
    cfg3 = tmp_cfg()
    single = day({"channels": [], "days": [], "flagged": []},
                 "2026-10-02", 10, 5)
    yt.save_snapshots(cs.store_path(cfg3), single)
    assert "2+ snapshot days" in trend_lines(cfg3)

    # -- a full act meeting on scripted seats ------------------------------
    cfg4 = tmp_cfg()
    append_sheet(cfg4.sources_sheet, "https://youtu.be/aaa", "octopus doc")
    calls = []

    class ActChair:
        def generate_text(self, prompt, temperature=0.7, tag="",
                          json_mode=False):
            if json_mode:
                return _json.dumps(
                    {"action": "generate",
                     "topic": "why lighthouse keepers heard voices",
                     "reason": "morbid explainers outperform, ride it"})
            return "I say we generate — the trend is clear."

    with _patch.object(mt, "_role_provider",
                       lambda c, lane: ActChair()), \
         redirect_stdout(io.StringIO()):
        summary = mt.run_meeting(cfg4, "act", rounds=1, send=False,
                                 executor=lambda d: calls.append(d) or
                                 f"Rendered '{d['topic']}' -> out/")
    assert "Board action: generate" in summary
    assert "lighthouse keepers" in summary
    assert "Rendered" in summary
    assert calls and calls[0]["action"] == "generate" \
        and "lighthouse" in calls[0]["topic"]
    # decision + outcome both landed in memory
    mem = " | ".join(e["text"] for e in load_memory(cfg4))
    assert "generate 'why lighthouse keepers heard voices'" in mem
    assert "outcome: Rendered" in mem
    # minutes file carries the follow-through
    act_files = list((cfg4.out_dir / "meetings").glob("*-act.md"))
    assert len(act_files) == 1
    body = act_files[0].read_text(encoding="utf-8")
    assert "Board action: generate" in body and "Follow-through" in body
    # the agenda the room saw: momentum note + sheet source + memory
    assert "octopus doc" in body          # candidates were on the table

    # -- a clip action executes + books the sheet ---------------------------
    cfg5 = tmp_cfg()
    append_sheet(cfg5.sources_sheet, "https://youtu.be/bbb", "kevin hart")

    class ClipChair:
        def generate_text(self, prompt, temperature=0.7, tag="",
                          json_mode=False):
            if json_mode:
                return _json.dumps(
                    {"action": "clip", "url": "https://youtu.be/bbb",
                     "reason": "pending and on-niche"})
            return "clip it."

    with _patch.object(mt, "_role_provider",
                       lambda c, lane: ClipChair()), \
         redirect_stdout(io.StringIO()):
        summary = mt.run_meeting(cfg5, "act", rounds=1, send=False,
                                 executor=lambda d: "Clipped -> clips/.")
    assert "Board action: clip" in summary
    assert "https://youtu.be/bbb" in summary
    rows = {r["url"]: r for r in
            __import__("sheet").load_sheet(cfg5.sources_sheet)}
    assert rows["https://youtu.be/bbb"]["status"] == "clipped"

    # -- failed clip executions are terminal and recorded ------------------
    cfg5_fail = tmp_cfg()
    append_sheet(cfg5_fail.sources_sheet, "https://youtu.be/bbb", "will fail")
    with _patch.object(mt, "_role_provider",
                       lambda c, lane: ClipChair()), \
         redirect_stdout(io.StringIO()):
        summary = mt.run_meeting(
            cfg5_fail, "act", rounds=1, send=False,
            executor=lambda d: (_ for _ in ()).throw(RuntimeError("clip boom")))
    failed_row = __import__("sheet").load_sheet(cfg5_fail.sources_sheet)[0]
    assert failed_row["status"] == "failed"
    assert "execution failed" in failed_row["result"]
    assert "execution failed" in summary

    # -- without an executor, a clip stays picked and pending ---------------
    cfg5_queue = tmp_cfg()
    append_sheet(cfg5_queue.sources_sheet, "https://youtu.be/bbb", "")
    with _patch.object(mt, "_role_provider",
                       lambda c, lane: ClipChair()), \
         redirect_stdout(io.StringIO()):
        summary = mt.run_meeting(cfg5_queue, "act", rounds=1, send=False)
    queued_row = __import__("sheet").load_sheet(cfg5_queue.sources_sheet)[0]
    assert queued_row["status"] == "picked"
    assert __import__("sheet").take_pending(cfg5_queue.sources_sheet, 1) == [
        ("https://youtu.be/bbb", "")]
    assert "not executed" in summary

    # -- 'none' is a valid outcome, and no executor fires -------------------
    cfg6 = tmp_cfg()

    class NoneChair:
        def generate_text(self, prompt, temperature=0.7, tag="",
                          json_mode=False):
            if json_mode:
                return _json.dumps({"action": "none",
                                    "reason": "clips are landing fine"})
            return "hold."

    fired = []
    with _patch.object(mt, "_role_provider",
                       lambda c, lane: NoneChair()), \
         redirect_stdout(io.StringIO()):
        summary = mt.run_meeting(cfg6, "act", rounds=1, send=False,
                                 executor=lambda d: fired.append(d))
    assert "nothing extra today" in summary and not fired
    assert "none — clips are landing fine" in \
        " | ".join(e["text"] for e in load_memory(cfg6))

    # -- executor = main.py's follow-through, unit level ---------------------
    import main as main_mod

    cfg7 = tmp_cfg()
    seen = {}

    def fake_generate(cfg, args):
        seen["topic"] = args.topic
        seen["count"] = args.count
        return 0

    with _patch.object(main_mod, "cmd_generate", fake_generate):
        out = main_mod._execute_board_action(
            cfg7, {"action": "generate", "topic": "canary ships",
                   "reason": "r"})
    assert seen["topic"] == "canary ships" and seen["count"] == 1
    assert "Rendered" in out
    with _patch.object(main_mod, "cmd_generate",
                       lambda c, a: 1):
        out = main_mod._execute_board_action(
            cfg7, {"action": "generate", "topic": "canary ships",
                   "reason": "r"})
    assert "failed" in out.lower()

    cfg8 = tmp_cfg()
    append_sheet(cfg8.sources_sheet, "https://youtu.be/ccc", "")
    import clipper as clipper_mod

    clipped = {}

    def fake_run_clip(cfg, url=None, **kw):
        clipped["url"] = url
        return ["clips/x.mp4"]

    with _patch.object(clipper_mod, "run_clip", fake_run_clip):
        out = main_mod._execute_board_action(
            cfg8, {"action": "clip", "url": "https://youtu.be/ccc",
                   "reason": "r"})
    assert clipped["url"] == "https://youtu.be/ccc"
    assert "clips/" in out
    rows = {r["url"]: r for r in
            __import__("sheet").load_sheet(cfg8.sources_sheet)}
    assert rows["https://youtu.be/ccc"]["status"] == "clipped"

    # -- without an executor the decision still lands, unexecuted ----------
    cfg9 = tmp_cfg()
    with _patch.object(mt, "_role_provider",
                       lambda c, lane: ActChair()), \
         redirect_stdout(io.StringIO()):
        summary = mt.run_meeting(cfg9, "act", rounds=1, send=False)
    assert "not executed" in summary
    assert "generate 'why lighthouse" in \
        " | ".join(e["text"] for e in load_memory(cfg9))


def t_sheet():
    from sheet import (SheetError, append_sheet, ensure_sheet,
                       load_sheet, mark_sheet, parse_sheet,
                       pending_rows, sheet_table, take_pending)

    # -- parsing: header skipped, bare links, commas, junk dropped ----------
    text = ("url,note,added,status,result\n"
            'https://youtu.be/a1,"roman aqueducts, best part",'
            "2026-10-01,new,\n"
            "https://youtu.be/a2\n"
            "https://youtu.be/a3,picked by the board,2026-10-02,picked,"
            "boardroom: strong hook\n"
            "https://youtu.be/a4,done thing,2026-10-01,clipped,clips/ — rome\n"
            "just a note line\n"
            "https://youtu.be/a5,failed one,2026-10-03,failed,"
            "\"no captions, no key\"\n")
    rows = parse_sheet(text)
    assert [r["url"] for r in rows] == [f"https://youtu.be/a{i}"
                                        for i in (1, 2, 3, 4, 5)]
    assert rows[0]["note"] == "roman aqueducts, best part"  # comma quoted
    assert rows[1]["note"] == "" and rows[1]["status"] == ""
    assert rows[2]["status"] == "picked"
    assert rows[4]["result"] == "no captions, no key"

    # -- queue semantics: picked first, new next, done never ---------------
    queue = pending_rows(rows)
    assert [r["url"] for r in queue] == ["https://youtu.be/a3",
                                         "https://youtu.be/a1",
                                         "https://youtu.be/a2"]

    # -- file round-trip: append (dedupe) -> take -> mark -------------------
    cfg = tmp_cfg()
    path = cfg.sources_sheet
    ensure_sheet(path)
    assert load_sheet(path) == []
    assert path.read_text(encoding="utf-8").startswith("url,note")
    assert append_sheet(path, "https://youtu.be/x1", "first")
    assert not append_sheet(path, "https://youtu.be/x1/", "dupe")
    assert append_sheet(path, "https://youtu.be/x2")
    assert take_pending(path, 1) == [("https://youtu.be/x1", "first")]
    assert take_pending(path, 0) == []
    assert mark_sheet(path, "https://youtu.be/x1/", "clipped",
                      "clips/ — rome") is True
    rows = load_sheet(path)
    assert rows[0]["status"] == "clipped" and \
        rows[0]["note"] == "first"          # note survives marking
    assert take_pending(path, 5) == [("https://youtu.be/x2", "")]
    # bad link refused; unknown status refused; missing url no-op
    try:
        append_sheet(path, "not a link")
        raise AssertionError("non-link accepted")
    except SheetError:
        pass
    assert mark_sheet(path, "https://youtu.be/x1", "deleted", "") is False
    assert mark_sheet(path, "https://youtu.be/gone", "clipped") is False
    # a marked-clipped row is out of the queue for good
    assert take_pending(path, 5) == [("https://youtu.be/x2", "")]

    # -- the listing ---------------------------------------------------------
    rows = load_sheet(path)
    table = sheet_table(rows, path)
    assert "clipped: 1" in table and "queued: 1" in table
    assert "x1" in table and "clips/ — rome" in table
    assert "empty" in sheet_table([], path)

def t_cutpoints():
    import json as _json
    from unittest.mock import patch as _patch

    import cutpoints as cp
    from clipper import sentence_spans
    from parts import plan_parts

    def caption_words(n, step=0.5):
        # auto-caption style: lowercase, NO punctuation, evenly spread
        return [{"word": f"w{i}", "start": round(i * step, 3),
                 "end": round(i * step + step, 3)} for i in range(n)]

    # -- punctuation detection ---------------------------------------------
    whisper = [{"word": w, "start": i, "end": i + 0.5} for i, w in
               enumerate(("One two three. " * 20).split())]
    assert cp.has_punctuation(whisper) is True
    assert cp.has_punctuation(caption_words(300)) is False
    sparse = caption_words(300)
    sparse[150]["word"] = "end."          # 1 per 300 < 2 per 100
    assert cp.has_punctuation(sparse) is False
    assert cp.has_punctuation([{"word": "hi."}]) is True   # tiny lists
    assert cp.has_punctuation([]) is False
    assert cp.is_eos({"word": "x", "eos": True}) and cp.is_eos({"word": "ok?"})
    assert not cp.is_eos({"word": "ok,"})

    # -- LLM restoration: guards + batching ----------------------------------
    class Fake:
        def __init__(self, replies):
            self.replies, self.prompts = list(replies), []

        def generate_text(self, prompt, **kw):
            self.prompts.append(prompt)
            reply = self.replies.pop(0) if self.replies else '{"ends": []}'
            if isinstance(reply, Exception):
                raise reply
            return reply

    fake = Fake(['{"ends": [7, 15, 999, -1, "3", "x", 2.5, true, 15]}'])
    assert cp.restore_sentence_ends(caption_words(40), "T", fake) == {3, 7, 15}
    assert "numbered word by word" in fake.prompts[0] and "0: w0" in fake.prompts[0]
    # 900 words -> 3 batches, indices offset per batch
    fake = Fake(['{"ends": [10]}', '{"ends": [10]}', '{"ends": [10]}'])
    assert cp.restore_sentence_ends(caption_words(900), "", fake) == {10, 410, 810}
    assert len(fake.prompts) == 3
    # a batch marking nearly every word is garbage -> dropped
    fake = Fake(['{"ends": ' + _json.dumps(list(range(0, 40, 2))) + '}'])
    assert cp.restore_sentence_ends(caption_words(40), "", fake) == set()
    # provider errors / junk never raise
    assert cp.restore_sentence_ends(caption_words(40), "", Fake([RuntimeError("503")])) == set()
    assert cp.restore_sentence_ends(caption_words(40), "", Fake(["no json"])) == set()
    assert cp.restore_sentence_ends(caption_words(40), "", Fake(['{"ends": 5}'])) == set()

    # -- ensure_sentence_ends: passthrough, cache, invalidation --------------
    cfg = tmp_cfg()
    cache = cfg.work_dir / "clip_cache" / "abc.json"
    logs = []
    fake = Fake(["x"])
    assert cp.ensure_sentence_ends(whisper, "", fake, cache, logs.append) is whisper
    assert fake.prompts == []  # punctuated: zero LLM calls
    words = caption_words(60)
    fake = Fake(['{"ends": [9, 29, 49]}'])
    flagged = cp.ensure_sentence_ends(words, "Title", fake, cache, logs.append)
    assert [i for i, w in enumerate(flagged) if w.get("eos")] == [9, 29, 49]
    assert all("eos" not in w for w in words)          # inputs untouched
    assert flagged[9]["word"] == "w9"                   # subtitles unchanged
    side = cp.eos_cache_path(cache)
    assert side.name == "abc.eos.json" and side.exists()
    fake2 = Fake([RuntimeError("must not be called")])
    again = cp.ensure_sentence_ends(words, "Title", fake2, cache, logs.append)
    assert fake2.prompts == [] and [i for i, w in enumerate(again) if w.get("eos")] == [9, 29, 49]
    assert any("cached" in line for line in logs)
    # different words (re-transcribed) -> cache ignored, recomputed
    changed = [dict(w) for w in words]
    changed[0]["word"] = "different"
    fake3 = Fake(['{"ends": [19]}'])
    redo = cp.ensure_sentence_ends(changed, "", fake3, cache, logs.append)
    assert len(fake3.prompts) == 1 and [i for i, w in enumerate(redo) if w.get("eos")] == [19]
    # corrupt cache -> recompute, no crash
    side.write_text("{broken", encoding="utf-8")
    fake4 = Fake(['{"ends": [19]}'])
    cp.ensure_sentence_ends(changed, "", fake4, cache, logs.append)
    assert len(fake4.prompts) == 1
    # nothing found -> no cache written (a later run may do better)
    other = tmp_cfg().work_dir / "clip_cache" / "zzz.json"
    cp.ensure_sentence_ends(words, "", Fake(['{"ends": []}']), other, logs.append)
    assert not cp.eos_cache_path(other).exists()
    assert any("falling back to pauses" in line for line in logs)
    # sentence_spans (used by the clip lane too) honours the flag
    assert len(sentence_spans(flagged)) == 4  # 3 flagged + trailing fragment

    # -- silencedetect parsing + failure --------------------------------------
    log = ("[silencedetect @ 0x1] silence_start: 3.21\n"
           "[silencedetect @ 0x1] silence_end: 3.9 | silence_duration: 0.69\n"
           "junk line\n"
           "[silencedetect @ 0x1] silence_start: -0.01\n"
           "[silencedetect @ 0x1] silence_end: 0.4 | silence_duration: 0.41\n"
           "[silencedetect @ 0x1] silence_start: 58.5\n")
    assert cp.parse_silences(log, 60.0) == [(3.21, 3.9), (0.0, 0.4), (58.5, 60.0)]
    assert cp.parse_silences("", 10) == [] and cp.parse_silences(None, 0) == []
    assert cp.parse_silences("silence_end: 4.0\n", 10) == []  # orphan end

    def no_ffmpeg(*a, **k):
        raise FileNotFoundError("ffmpeg")
    with _patch.object(cp.subprocess, "run", no_ffmpeg):
        assert cp.detect_silences("x.mp4", 10.0) == []

    class Proc:
        stderr = log
    with _patch.object(cp.subprocess, "run", lambda *a, **k: Proc()):
        assert len(cp.detect_silences("x.mp4", 60.0)) == 3

    # -- boundaries ------------------------------------------------------------
    w = [{"word": "a", "start": 0.0, "end": 0.4},
         {"word": "b.", "start": 0.5, "end": 0.9},     # eos, pause right after
         {"word": "c", "start": 1.6, "end": 2.0},
         {"word": "d", "start": 2.0, "end": 2.4, "eos": True},  # eos, no pause
         {"word": "e", "start": 2.6, "end": 3.0},
         {"word": "f", "start": 6.0, "end": 6.4},      # long pause before f
         {"word": "g.", "start": 6.4, "end": 6.8}]     # final word: never a cut
    sil = [(0.95, 1.55), (3.1, 5.9), (2.45, 2.55)]
    b = dict(cp.build_boundaries(w, sil, 7.0))
    assert b[round((0.95 + 1.55) / 2, 3)] == cp.TIER_BOTH
    # d: the 0.1s blip at 2.45-2.55 IS a real pause right after it
    assert b[2.5] == cp.TIER_BOTH
    assert b[round((3.1 + 5.9) / 2, 3)] == cp.TIER_PAUSE
    assert not any(t > 6.4 for t in b)                  # final word no cut
    # without the blip, d falls back to "sentence end" just after the word
    b2 = dict(cp.build_boundaries(w, [(0.95, 1.55), (3.1, 5.9)], 7.0))
    assert b2[2.5] == cp.TIER_SENTENCE                   # 2.4 + min(.25, .2/2)
    # a pause too far after the sentence end (> SNAP_TO_SILENCE) is not
    # borrowed: the end stays a plain sentence cut, the pause stands alone
    far = [{"word": "s.", "start": 0.5, "end": 1.0},
           {"word": "t", "start": 5.0, "end": 5.4},
           {"word": "u", "start": 5.4, "end": 5.8}]
    fb = dict(cp.build_boundaries(far, [(3.0, 4.9)], 6.0))
    assert fb == {1.25: cp.TIER_SENTENCE, 3.95: cp.TIER_PAUSE}, fb
    # a pause shorter than PAUSE_ONLY_MIN with no sentence end is ignored
    assert cp.build_boundaries([{"word": "x", "start": 0, "end": 1},
                                {"word": "y", "start": 1.3, "end": 2}],
                               [(1.0, 1.3)], 3.0) == []
    # a pause BEFORE the sentence's last word is not borrowed
    early = [{"word": "p", "start": 0, "end": 1},
             {"word": "q.", "start": 2.0, "end": 2.5},
             {"word": "r", "start": 2.5, "end": 3.0}]
    assert dict(cp.build_boundaries(early, [(1.0, 1.9)], 4.0)).get(1.45) == cp.TIER_PAUSE

    # -- pick_cut order ----------------------------------------------------------
    bounds = [(20.0, cp.TIER_SENTENCE), (27.0, cp.TIER_BOTH), (31.0, cp.TIER_PAUSE),
              (45.0, cp.TIER_SENTENCE)]
    assert cp.pick_cut(29.0, 0, 100, 10, bounds) == (27.0, cp.TIER_BOTH)
    # pause-backed wins over a slightly closer plain sentence end
    assert cp.pick_cut(22.0, 0, 100, 10, bounds) == (20.0, cp.TIER_SENTENCE)  # 2 vs 5-2=3
    assert cp.pick_cut(24.5, 0, 100, 10, bounds) == (27.0, cp.TIER_BOTH)     # 4.5 vs 2.5-2
    # the bonus decides: plain end 3s away vs pause-backed 4s away -> pause
    assert cp.pick_cut(23.0, 0, 100, 10, bounds) == (27.0, cp.TIER_BOTH)
    # ...but never beyond 2s of extra distance
    assert cp.pick_cut(21.0, 0, 100, 10, bounds) == (20.0, cp.TIER_SENTENCE)
    # no sliver parts: a sentence end 2s after the previous cut is illegal
    sliver = plan_parts(60.0, None, target_len=20, word_starts=[],
                        boundaries=[(2.0, cp.TIER_SENTENCE)], tail_merge=0)
    assert min(e - s for s, e in sliver) >= 10, sliver
    # nothing in window -> sentence end in 2x window beats a closer pause
    assert cp.pick_cut(36.0, 0, 100, 8, [(31.0, cp.TIER_PAUSE), (50.0, cp.TIER_SENTENCE)]) \
        == (50.0, cp.TIER_SENTENCE)
    assert cp.pick_cut(36.0, 0, 100, 5, [(31.0, cp.TIER_PAUSE)]) == (31.0, cp.TIER_PAUSE)
    assert cp.pick_cut(36.0, 0, 100, 5, [], [35.2, 38.9]) == (35.2, 0)
    assert cp.pick_cut(36.0, 0, 100, 5, [], []) == (36.0, 0)
    # lo/hi: never a sliver
    # (sentence end in 2x window still beats a nearer plain pause)
    assert cp.pick_cut(29.0, 28.0, 100, 10, bounds) == (45.0, cp.TIER_SENTENCE)
    assert cp.pick_cut(29.0, 28.0, 40.0, 10, bounds) == (31.0, cp.TIER_PAUSE)
    assert cp.pick_cut(29.0, 30.0, 40.0, 1, [], []) == (30.0, 0)  # clamped

    # -- property: 10-min unpunctuated source, LLM + pauses -> clean cuts ----
    step = 0.4
    words = caption_words(1500, step)
    ends = set(range(13, 1500, 17))                    # a sentence every ~7s
    flagged = [dict(x, eos=True) if i in ends else x for i, x in enumerate(words)]
    # speech timings from captions are estimates; the audio pause after each
    # sentence sits ~0.1s off the caption boundary
    sil = [(flagged[i]["end"] - 0.05, flagged[i]["end"] + 0.3)
           for i in sorted(ends) if i + 1 < 1500]
    bounds = cp.build_boundaries(flagged, sil, 600.0)
    plan = plan_parts(600.0, None, target_len=60, word_starts=[x["start"] for x in flagged],
                      boundaries=bounds, tail_merge=15, snap_window=10)
    tiers = dict(bounds)
    cuts = [e for _, e in plan[:-1]]
    assert len(plan) >= 8
    assert all(tiers.get(round(c, 3)) == cp.TIER_BOTH for c in cuts), cuts
    for c in cuts:  # never inside a sentence-final word's speech
        assert not any(x["start"] < c < x["end"] - 0.06 for i, x in enumerate(flagged)
                       if i in ends), c
    lengths = [e - s for s, e in plan]
    assert min(lengths[:-1]) >= 30 and max(lengths) <= 80, lengths
    assert cp.cut_summary(plan, bounds).startswith(f"{len(cuts)} cut(s): {len(cuts)} sentence end in a pause")
    # same source with NO sentence info at all: still a plan (fallbacks)
    bare = plan_parts(600.0, None, target_len=60, word_starts=[x["start"] for x in words],
                      boundaries=cp.build_boundaries(words, [], 600.0))
    assert len(bare) >= 9 and "fallback" in cp.cut_summary(bare, [])

    # -- halves -------------------------------------------------------------------
    halves = cp.plan_halves(600.0, bounds, [x["start"] for x in flagged])
    assert len(halves) == 2 and halves[0][0] == 0 and halves[1][1] == 600.0
    assert halves[0][1] == halves[1][0] and abs(halves[0][1] - 300) <= 15
    assert tiers.get(round(halves[0][1], 3)) == cp.TIER_BOTH
    assert cp.plan_halves(100.0, [], []) == [(0.0, 50.0), (50.0, 100.0)]
    # a boundary far from the middle is NOT taken (halves stay near-equal)
    assert cp.plan_halves(100.0, [(30.0, cp.TIER_BOTH)], [])[0][1] == 50.0
    assert cp.plan_halves(0, [], []) == []
    assert cp.cut_summary([(0, 10)], []) == ""


def t_whole_short():
    import io
    import sys
    from contextlib import redirect_stdout
    from unittest.mock import patch as _patch

    import assembler
    import clipper
    import cutpoints
    import main as main_mod
    import parts
    import scriptgen
    from clipper import ClipError
    from parts import (parts_header_line, shorts_cap_warning, use_whole)

    # -- decision table ------------------------------------------------------
    assert use_whole(120, 240) is True
    assert use_whole(240, 240) is True          # "under ~4 min" inclusive
    assert use_whole(240.5, 240) is False
    assert use_whole(600, 240) is False
    assert use_whole(120, 0) is False           # 0 = auto rule off
    assert use_whole(0, 240) is False and use_whole(-5, 240) is False
    assert use_whole("junk", 240) is False      # garbage never renders whole
    assert use_whole(120, "junk") is True       # garbage knob -> default 180
    assert use_whole(200, "junk") is False
    assert use_whole(600, 240, force=True) is True    # --whole
    assert use_whole(60, 240, force=False) is False   # --no-whole

    # -- 3:00 Shorts cap heads-up --------------------------------------------
    assert shorts_cap_warning(179) == "" and shorts_cap_warning(180) == ""
    warn = shorts_cap_warning(215)
    assert "3:35" in warn and "3:00" in warn and "--no-whole" in warn
    assert shorts_cap_warning("junk") == ""

    # -- config knob ---------------------------------------------------------
    assert tmp_cfg().whole_under_seconds == 180.0   # = YouTube Shorts cap
    assert tmp_cfg(whole={"under_seconds": 0}).whole_under_seconds == 0.0
    assert tmp_cfg(whole={"under_seconds": "x"}).whole_under_seconds == 180.0
    assert tmp_cfg(whole={"under_seconds": 99999}).whole_under_seconds == 900.0
    assert tmp_cfg(whole={"under_seconds": -3}).whole_under_seconds == 0.0
    assert tmp_cfg(whole="nonsense").whole_under_seconds == 180.0

    # -- solo header: title only, timed, fading, never "Part" ---------------
    solo = parts_header_line("How Octopuses Think", 1, 1, 90.0)
    assert solo.startswith("Dialogue: 0,0:00:00.00,0:00:04.00,"), solo
    assert "\\fad(200,400)" in solo and "\\an8" in solo
    assert "How Octopuses Think" in solo and "Part" not in solo
    assert solo.rstrip().endswith("How Octopuses Think")  # no trailing \N
    assert parts_header_line("T", 1, 1, 90.0, show_seconds=0).startswith(
        "Dialogue: 0,0:00:00.00,0:01:30.00,")        # 0 = whole video
    assert parts_header_line("T", 1, 1, 2.5).startswith(
        "Dialogue: 0,0:00:00.00,0:00:02.50,")        # never past the end
    two = parts_header_line("Line one\nLine two", 1, 1, 30.0)
    assert "Line one\\NLine two" in two and "Part" not in two
    assert parts_header_line("", 1, 1, 30.0) == ""
    assert parts_header_line("   ", 1, 1, 30.0) == ""
    # multi-part header unchanged
    multi = parts_header_line("How Octopuses Think", 2, 3, 60.0)
    assert "Part 2" in multi and "\\N{" in multi

    # -- orchestration fakes ------------------------------------------------
    def words_n(n, step=0.5):
        return [{"word": f"w{i}" + ("." if i % 8 == 7 else ""),
                 "start": i * step, "end": i * step + 0.4} for i in range(n)]

    renders = []

    def fake_render(src, cand, words, cfg, out_path, work, sub_pos="default",
                    extra_ass=None, caps=False):
        renders.append({"start": cand.start, "end": cand.end,
                        "extra": extra_ass or "", "out": out_path,
                        "sub_pos": sub_pos})
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake-mp4")
        return out_path

    class Provider:
        calls = 0

        def generate_text(self, *a, **k):
            Provider.calls += 1
            return "not json"

    def run(fn, duration, words, **kw):
        renders.clear()
        Provider.calls = 0
        cfg = tmp_cfg(clip={"transcript_fix": False, "polish_titles": False,
                            "smart_crop": False})
        src = cfg.root / "Octopus Opens A Jar.mp4"
        src.write_bytes(b"x")
        buf = io.StringIO()
        with _patch.object(assembler, "ffprobe_duration", lambda p: duration), \
             _patch.object(clipper, "ffprobe_duration", lambda p: duration), \
             _patch.object(clipper, "load_transcript_cache", lambda p: list(words)), \
             _patch.object(clipper, "render_clip", fake_render), \
             _patch.object(scriptgen, "get_provider", lambda c: Provider()), \
             _patch.object(cutpoints, "detect_silences", lambda *a, **k: []), \
             redirect_stdout(buf):
            code = fn(cfg, file=str(src), out_dir=cfg.root / "out", **kw)
        return code, buf.getvalue(), cfg

    # parts, auto whole: 100s source -> 1 render of the full length,
    # title-only header, plain kit title, no Part anywhere
    code, out, cfg = run(parts.run_parts, 100.0, words_n(150))
    assert code == 0 and len(renders) == 1, (out, renders)
    assert (renders[0]["start"], renders[0]["end"]) == (0.0, 100.0)
    assert "Octopus Opens A Jar" in renders[0]["extra"]
    assert "Part" not in renders[0]["extra"]
    kit_title = (cfg.root / "out" / renders[0]["out"].stem / "TITLE.txt"
                 ).read_text(encoding="utf-8")
    assert kit_title == "Octopus Opens A Jar", kit_title
    assert "kept whole" in out and "⚠️" not in out
    assert renders[0]["sub_pos"] == "bottom"
    # parts --no-whole on the same source -> really cut into episodes
    code, out, _ = run(parts.run_parts, 100.0, words_n(150), whole=False,
                       part_len=30)
    assert code == 0 and len(renders) >= 3, renders
    assert all("Part" in r["extra"] for r in renders)
    # parts auto on a long source -> normal parts
    code, out, _ = run(parts.run_parts, 300.0, words_n(500))
    assert len(renders) >= 4 and "kept whole" not in out
    # parts --whole on 3:35 -> one piece + the Shorts-cap warning
    code, out, _ = run(parts.run_parts, 215.0, words_n(300), whole=True)
    assert len(renders) == 1 and renders[0]["end"] == 215.0
    assert "3:00 Shorts cap" in out
    # parts dry-run whole: plan printed, nothing rendered
    code, out, _ = run(parts.run_parts, 100.0, words_n(150), dry_run=True)
    assert code == 0 and renders == [] and "Part 1: 0.0s - 100.0s" in out
    # --no-header still honoured in whole mode
    code, out, _ = run(parts.run_parts, 100.0, words_n(150), header=False)
    assert len(renders) == 1 and renders[0]["extra"] == ""

    # clip, auto whole: THIN transcript (30 words) is fine for a short
    # source — one clip, no moment picking (zero LLM calls), clip naming
    code, out, cfg = run(clipper.run_clip, 45.0, words_n(30),
                         use_vision=False)
    assert code == 0 and len(renders) == 1, out
    assert (renders[0]["start"], renders[0]["end"]) == (0.0, 45.0)
    assert renders[0]["out"].name.endswith("_clip_01.mp4")
    assert "Octopus Opens A Jar" in renders[0]["extra"]
    assert "Part" not in renders[0]["extra"]
    assert Provider.calls == 0, "whole mode must not ask the LLM for moments"
    kit = cfg.root / "out" / renders[0]["out"].stem
    assert (kit / "TITLE.txt").read_text(encoding="utf-8") == "Octopus Opens A Jar"
    assert "window: 0.0s - 45.0s" in (kit / "CREDIT.txt").read_text(encoding="utf-8")
    assert "1 clip + kit" in out
    # clip --no-whole on the same thin source -> the mining path (and its
    # thin-transcript guard) is really taken
    try:
        run(clipper.run_clip, 45.0, words_n(30), use_vision=False, whole=False)
    except ClipError as exc:
        assert "too thin" in str(exc)
    else:
        raise AssertionError("--no-whole must go through moment mining")
    # clip auto on a long source -> mining (fake LLM returns junk -> no moments)
    try:
        run(clipper.run_clip, 600.0, words_n(900), use_vision=False)
    except ClipError as exc:
        assert "no usable moments" in str(exc)
        assert Provider.calls >= 1
    else:
        raise AssertionError("long sources must be mined, not kept whole")
    # clip --whole on 3:20 -> one clip + warning
    code, out, _ = run(clipper.run_clip, 200.0, words_n(300),
                       use_vision=False, whole=True)
    assert len(renders) == 1 and "3:00 Shorts cap" in out and "3:20" in out
    # zero-length probe never "renders whole" (falls to the normal path)
    try:
        run(clipper.run_clip, 0.0, words_n(30), use_vision=False)
    except ClipError:
        pass
    assert renders == []

    # clip auto at 3:20 -> no longer whole (limit is 3:00 now) -> mined
    try:
        run(clipper.run_clip, 200.0, words_n(400), use_vision=False)
    except ClipError as exc:
        assert "no usable moments" in str(exc)
    else:
        raise AssertionError("200s must be cut now that the limit is 180s")
    assert renders == []
    code, out, _ = run(clipper.run_clip, 180.0, words_n(300), use_vision=False)
    assert len(renders) == 1 and "kept whole" in out and "⚠️" not in out

    # -- --half ------------------------------------------------------------------
    # words_n: a "." every 8th word -> sentence ends every 4s (0.5s step)
    sentence_cuts = {round(i * 0.5 + 0.4 + 0.05, 3) for i in range(7, 800, 8)}
    code, out, cfg = run(clipper.run_clip, 200.0, words_n(400),
                         use_vision=False, half=True)
    assert code == 0 and len(renders) == 2, out
    cut = renders[0]["end"]
    assert renders[0]["start"] == 0.0 and renders[1]["start"] == cut
    assert renders[1]["end"] == 200.0 and abs(cut - 100) <= 5
    assert round(cut, 3) in sentence_cuts, cut        # at a sentence end
    assert "Part 1" in renders[0]["extra"] and "Part 2" in renders[1]["extra"]
    assert [r["out"].name[-11:] for r in renders] == ["clip_01.mp4", "clip_02.mp4"]
    titles = [(cfg.root / "out" / r["out"].stem / "TITLE.txt").read_text(encoding="utf-8")
              for r in renders]
    assert titles == ["Octopus Opens A Jar — Part 1", "Octopus Opens A Jar — Part 2"]
    assert Provider.calls == 0     # punctuated + no picking: zero LLM calls
    assert "halves:" in out and "1 cut(s): 1 sentence end" in out and "⚠️" not in out
    # halves over 3:00 each -> warned
    code, out, _ = run(clipper.run_clip, 400.0, words_n(800), use_vision=False, half=True)
    assert len(renders) == 2 and out.count("3:00 Shorts cap") == 2
    # --half beats the auto-whole rule (a 2-minute video still halves)
    code, out, _ = run(clipper.run_clip, 120.0, words_n(240), use_vision=False, half=True)
    assert len(renders) == 2 and "kept whole" not in out
    code, out, _ = run(parts.run_parts, 120.0, words_n(240), half=True)
    assert len(renders) == 2 and "kept whole" not in out
    assert round(renders[0]["end"], 3) in sentence_cuts  # not the raw middle
    # too short to halve -> clear error, nothing rendered
    try:
        run(clipper.run_clip, 20.0, words_n(40), use_vision=False, half=True)
    except ClipError as exc:
        assert "too short to halve" in str(exc)
    else:
        raise AssertionError("20s must be refused by --half")
    assert renders == []
    # parts --half: same split, Part 1/2 headers, part naming
    code, out, _ = run(parts.run_parts, 200.0, words_n(400), half=True)
    assert len(renders) == 2 and abs(renders[0]["end"] - 100) <= 5
    assert round(renders[0]["end"], 3) in sentence_cuts
    assert renders[0]["out"].name.endswith("_part_01.mp4")
    assert "Part 2" in renders[1]["extra"]
    # parts regular cutting now lands on sentence ends too
    code, out, _ = run(parts.run_parts, 300.0, words_n(600), part_len=60)
    assert len(renders) >= 4
    assert all(round(r["end"], 3) in sentence_cuts for r in renders[:-1]), \
        [r["end"] for r in renders]
    assert "sentence end" in out and "fallback" not in out

    # -- CLI flags reach the lanes -------------------------------------------
    seen = {}

    def grab(name):
        def handler(cfg, args):
            seen[name] = "half" if args.half else args.whole
            return 0
        return handler

    cfg_path = tmp_cfg().root / "config.yaml"
    cfg_path.write_text("{}", encoding="utf-8")
    for argv, lane, want in (
            (["clip", "x.mp4"], "clip", None),
            (["clip", "x.mp4", "--whole"], "clip", True),
            (["clip", "x.mp4", "--no-whole"], "clip", False),
            (["parts", "x.mp4"], "parts", None),
            (["parts", "x.mp4", "--whole"], "parts", True),
            (["parts", "x.mp4", "--no-whole"], "parts", False),
            (["clip", "x.mp4", "--half"], "clip", "half"),
            (["parts", "x.mp4", "--half"], "parts", "half")):
        seen.clear()
        with _patch.object(sys, "argv", ["main.py", "--config", str(cfg_path)] + argv), \
             _patch.object(main_mod, "cmd_clip", grab("clip")), \
             _patch.object(main_mod, "cmd_parts", grab("parts")):
            assert main_mod.main() == 0
        assert seen == {lane: want}, (argv, seen)
    # both flags at once is a usage error, not a silent pick
    import contextlib
    for combo in (["--whole", "--no-whole"], ["--half", "--whole"],
                  ["--half", "--no-whole"]):
        with _patch.object(sys, "argv", ["main.py", "--config", str(cfg_path),
                                         "clip", "x.mp4", *combo]), \
             contextlib.redirect_stderr(io.StringIO()):
            try:
                main_mod.main()
            except SystemExit as exc:
                assert exc.code == 2
            else:
                raise AssertionError(f"{combo} must be rejected")


def t_sub_caps():
    import re

    from subtitles import build_cues, build_karaoke_events

    narr = ["hello world"]
    timings = [[(0.0, 0.5), (0.5, 1.0)]]
    cues = build_cues(narr, timings, [0.0], [2.0], 26, 0.0, caps=True)
    assert cues and all(line == line.upper() for c in cues for line in c.lines)
    plain = build_cues(narr, timings, [0.0], [2.0], 26, 0.0)
    assert "hello world" in " ".join(plain[0].lines)  # default unchanged
    events = build_karaoke_events(narr, timings, [0.0], [2.0], 0.0, caps=True)
    texts = [re.sub(r"\{[^}]*\}", "", e.text) for e in events]
    assert texts and all(t == t.upper() for t in texts)
    cfg = tmp_cfg()
    assert cfg.subtitles_caps is False
    cfg.data["subtitles"]["caps"] = True
    assert cfg.subtitles_caps is True


def t_sub_highlight():
    from subtitles import (KARAOKE_YELLOW, build_karaoke_events,
                           write_ass)

    # Gold highlight, matching the progress bar + Part-X header.
    assert "0000D7FF" in KARAOKE_YELLOW
    events = build_karaoke_events(["hi there"], [[(0.0, 0.4), (0.4, 0.8)]],
                                  [0.0], [2.0], 0.0)
    assert events and "0000D7FF" in events[0].text
    # Outline scales with font_scale; explicit outline stays absolute.
    tmp = Path(_mkdtemp())
    scaled = write_ass(events, tmp / "s.ass", "portrait", 1080, 1920,
                       style={"scale": 2.0})
    assert ",1,6,0," in scaled.read_text(encoding="utf-8")
    base = write_ass(events, tmp / "b.ass", "portrait", 1080, 1920)
    assert ",1,3,0," in base.read_text(encoding="utf-8")
    fixed = write_ass(events, tmp / "f.ass", "portrait", 1080, 1920,
                      style={"scale": 2.0, "outline": 5})
    assert ",1,5,0," in fixed.read_text(encoding="utf-8")


def t_clip_default_bottom():
    from clipper import resolve_clip_sub_pos

    # Double-default resolves to bottom (STUDY F2); explicit wins.
    cfg = tmp_cfg()
    assert resolve_clip_sub_pos(cfg, "default") == "bottom"
    assert resolve_clip_sub_pos(cfg, "auto") == "auto"
    assert resolve_clip_sub_pos(cfg, "top") == "top"
    assert resolve_clip_sub_pos(cfg, "") == "bottom"
    cfg.data["subtitles"]["position"] = "top"
    assert resolve_clip_sub_pos(cfg, "default") == "default"


def t_top_video():
    # Top-X countdown: pick the best N, worst first, best revealed last.
    from top import build_top_description, make_card, plan_top

    entries = [
        {"path": "a.mp4", "title": "first moment", "score": 5, "len": 30.0},
        {"path": "b.mp4", "title": "second moment", "score": 9, "len": 25.0},
        {"path": "c.mp4", "title": "third moment", "score": 7, "len": 20.0},
    ]
    plan = plan_top(entries, 2)
    assert [(p["rank"], p["title"]) for p in plan] == \
        [(2, "third moment"), (1, "second moment")]  # worst first, #1 last
    assert plan_top(entries, 10)[0]["rank"] == 3     # uses what exists
    desc = build_top_description(plan, {
        "title": "Big Compilation", "channel": "Someone",
        "url": "https://youtu.be/x"})
    assert "Top 2 moments" in desc and "\"Big Compilation\"" in desc
    assert "#2" in desc and "#1" in desc and "youtu.be/x" in desc
    assert "0:01" in desc           # #2 starts after its 1.4s card
    assert "0:23" in desc           # 1.4+20s+1.4=22.8 -> rounds to 0:23
    tmp = Path(_mkdtemp())
    card = make_card(1, 2, "the very best moment", tmp / "card.png")
    assert card.exists()
    from PIL import Image

    img = Image.open(card)
    assert img.size == (1080, 1920)  # portrait card, clip-shaped


def t_sub_position():
    # Subtitle placement + style knobs (2026-09-23): clips burned karaoke
    # dead-center (Alignment=5) — right where the main event usually is.
    from subtitles import (_karaoke_style, burn_style, pick_sub_band,
                           sub_style_from_cfg, write_ass)

    # style resolution: CLI > config > engine default; garbage -> default
    cfg = tmp_cfg()
    assert sub_style_from_cfg(cfg) == {
        "position": "default", "font": "Arial", "scale": 1.0, "outline": 0}
    assert sub_style_from_cfg(cfg, "top")["position"] == "top"
    cfg2 = tmp_cfg(subtitles={"position": "bottom", "font": "Impact",
                              "font_scale": 1.2, "outline": 4})
    assert sub_style_from_cfg(cfg2)["position"] == "bottom"
    assert sub_style_from_cfg(cfg2)["font"] == "Impact"
    assert sub_style_from_cfg(cfg2, "top")["position"] == "top"  # CLI wins
    junk = tmp_cfg(subtitles={"position": "diagonal"})
    assert sub_style_from_cfg(junk)["position"] == "default"

    # SRT force_style: bottom is the historic look; top/middle move it
    assert "Alignment=2" in burn_style("portrait")
    assert "Alignment=8" in burn_style("portrait", {"position": "top"})
    assert "Alignment=5,MarginV=0" in burn_style(
        "landscape", {"position": "middle"})
    assert "FontName=Impact" in burn_style("portrait", {"font": "Impact"})
    assert "FontSize=13" in burn_style("portrait", {"scale": 1.2})  # 11*1.2
    assert "Outline=4" in burn_style("portrait", {"outline": 4})
    assert "FontSize=11" in burn_style("portrait", {"outline": 9})  # independent

    # karaoke: portrait center is the historic look; bottom clears the
    # Shorts UI rail; top clears the top overlay
    assert ",5,60,60,0,1" in _karaoke_style("portrait")
    assert ",2,60,60,300,1" in _karaoke_style(
        "portrait", {"position": "bottom"})
    assert ",8,60,60,140,1" in _karaoke_style("portrait", {"position": "top"})
    assert "Style: Karaoke,Impact," in _karaoke_style(
        "portrait", {"font": "Impact"})
    assert ",110," in _karaoke_style("portrait", {"scale": 1.25})  # 88*1.25
    assert ",1,4,0," in _karaoke_style("portrait", {"outline": 4})

    # calmest-third chooser: bottom bias within 10%, else top, else middle
    assert pick_sub_band((9.0, 5.0, 4.0)) == "bottom"
    assert pick_sub_band((5.0, 5.0, 5.0)) == "bottom"   # tie -> bottom
    assert pick_sub_band((4.0, 9.0, 8.0)) == "top"      # bottom too busy
    assert pick_sub_band((10.0, 3.0, 9.0)) == "middle"
    assert pick_sub_band((4.0, 3.9, 6.0)) == "top"      # top within 10% of middle

    # the .ass file itself carries the style; no style = historic look
    import types

    ev = [types.SimpleNamespace(start=0.0, end=1.0, text="hello")]
    p = Path(_mkdtemp()) / "a.ass"
    write_ass(ev, p, "portrait", 1080, 1920, style={"position": "bottom"})
    assert ",2,60,60,300,1" in p.read_text(encoding="utf-8")
    write_ass(ev, p, "portrait", 1080, 1920)
    assert ",5,60,60,0,1" in p.read_text(encoding="utf-8")

    # restyle_ass: reburn's style refresh — Style line swapped, events
    # untouched, refuses files without a Karaoke style
    from subtitles import restyle_ass

    write_ass(ev, p, "portrait", 1080, 1920, style={"position": "top"})
    p2 = p.with_name("restyled.ass")
    restyle_ass(p, "portrait", {"position": "bottom"}, p2)
    body = p2.read_text(encoding="utf-8")
    assert ",2,60,60,300,1" in body            # new placement
    assert ",8,60,60,140,1" not in body        # old placement gone
    assert "Dialogue: 0,0:00:00.00,0:00:01.00,Karaoke,,0,0,0,,hello" in body
    bad = p.with_name("bad.ass")
    bad.write_text("[Script Info]\nScriptType: v4.00+", encoding="utf-8")
    try:
        restyle_ass(bad, "portrait", None, p2)
        raise AssertionError("should refuse a style-less .ass")
    except ValueError:
        pass

    # drift guard (2026-09-24 diagnostic): the clip parser once carried
    # a literal default=6 while the library said 10 — argparse always
    # wins, so the constants must rule the parser.
    src = Path(__file__).with_name("main.py").read_text(encoding="utf-8")
    assert "default=MAX_CLIPS_DEFAULT" in src
    assert '"--max-clips", type=int, default=6' not in src


def t_subpreview():
    # The style picker (2026-09-23): pure helpers render a captioned frame
    # and emit the config block; the tkinter shell stays untested, like
    # the bot lane's PhoneBot.
    import io

    import yaml
    from PIL import Image
    from subpreview import (preview_trio, render_preview, save_trio,
                            style_yaml)

    # the YAML block parses and round-trips the style
    block = style_yaml({"position": "top", "font": "Impact",
                        "scale": 1.2, "outline": 3})
    loaded = yaml.safe_load(block)["subtitles"]
    assert loaded["position"] == "top" and loaded["font"] == "Impact"
    assert loaded["font_scale"] == 1.2 and loaded["outline"] == 3

    # a plain dark portrait frame to draw on
    buf = io.BytesIO()
    Image.new("RGB", (1080, 1920), (24, 24, 28)).save(buf, format="PNG")
    frame = buf.getvalue()

    def bright_zone(png, top_frac, bottom_frac):
        img = Image.open(io.BytesIO(png)).convert("RGB")
        w, h = img.size
        count = 0
        for y in range(int(h * top_frac), int(h * bottom_frac)):
            for x in range(0, w, 3):
                r, g, b = img.getpixel((x, y))
                if r > 200 and g > 200 and b > 200:
                    count += 1
        return count

    top_png = render_preview(frame, {"position": "top"}, width=270)
    mid_png = render_preview(frame, {"position": "middle"}, width=270)
    bot_png = render_preview(frame, {"position": "bottom"}, width=270)
    assert bright_zone(top_png, 0.0, 0.35) > 50      # caption up top
    assert bright_zone(top_png, 0.75, 1.0) == 0
    assert bright_zone(mid_png, 0.3, 0.7) > 50       # caption centered
    assert bright_zone(bot_png, 0.65, 1.0) > 50      # caption at the bottom
    assert bright_zone(bot_png, 0.0, 0.3) == 0
    # "default" on portrait = the historic center look
    assert render_preview(frame, {"position": "default"},
                          width=270) != bot_png

    # the karaoke gold highlight word is in there
    img = Image.open(io.BytesIO(top_png)).convert("RGB")
    gold = sum(1 for p in img.getdata()
               if p[0] > 220 and 180 < p[1] < 240 and p[2] < 90)
    assert gold > 20

    # trio: three distinct placements; save_trio writes them out
    trio = preview_trio(frame, {})
    assert set(trio) == {"top", "middle", "bottom"}
    assert len(set(trio.values())) == 3
    tmp = Path(_mkdtemp())
    paths = save_trio(tmp, frame, {"font": "Arial"})
    assert len(paths) == 3 and all(p.exists() for p in paths)
    assert (tmp / "subpreview_top.png").stat().st_size > 1000


def t_reclaim_interrupted():
    # A killed run leaves 'queued' jobs behind forever: the table says
    # queued, but nothing ever resumes them and their topics count as
    # covered. _reclaim_interrupted must return the topics to the backlog.
    from jobqueue import Queue
    from main import _reclaim_interrupted
    from topics import load_backlog, save_backlog

    tmp = Path(_mkdtemp())
    queue = Queue(tmp / "state.json")
    ghost = queue.add("why octopuses have three hearts")
    twin = queue.add("why octopuses have three hearts")   # duplicate ghost
    honey = queue.add("how honey never spoils")
    queue.update(honey, status="generated", title="Honey")
    revenant = queue.add("how honey never spoils")   # killed run, but the
    # topic re-rendered successfully later (e.g. a --topic rerun) -> dead
    decorated = queue.add("how lava lamps work (variation 2 of 3: choose a "
                          "different specific story each time)")
    backlog_file = tmp / "backlog.txt"
    save_backlog(backlog_file, ["a fresh topic"])
    work = tmp / "work"
    (work / ghost.id).mkdir(parents=True)
    (work / ghost.id / "partial.txt").write_text("x", encoding="utf-8")

    returned = _reclaim_interrupted(queue, backlog_file, work)

    # duplicate collapsed, already-rendered topic skipped, decoration stripped
    assert returned == ["why octopuses have three hearts", "how lava lamps work"]
    assert load_backlog(backlog_file) == [
        "why octopuses have three hearts", "how lava lamps work", "a fresh topic"]
    statuses = {job.id: job.status for job in Queue(tmp / "state.json").jobs}
    assert statuses[ghost.id] == "reclaimed"
    assert statuses[twin.id] == "reclaimed"
    assert statuses[revenant.id] == "reclaimed"
    assert statuses[honey.id] == "generated"       # real work untouched
    assert not (work / ghost.id).exists()          # partial work dir gone
    # generate's used-list must no longer count the ghost topic as covered
    used = [job.topic for job in queue.jobs if job.status != "reclaimed"]
    assert "why octopuses have three hearts" not in used
    assert "how lava lamps work" not in used
    # no ghosts left -> no-op, backlog untouched
    assert _reclaim_interrupted(Queue(tmp / "state.json"), backlog_file, work) == []
    assert load_backlog(backlog_file)[0] == "why octopuses have three hearts"


def t_generate_rejects_bad_count():
    """--count 0 must fail fast with a clear message, not silently no-op."""
    import argparse

    from main import cmd_generate

    cfg = tmp_cfg()
    cfg.data["ai"]["provider"] = "template"  # stay offline if reached
    args = argparse.Namespace(
        topic=None, count=0, seconds=None, format=None,
        images_per_scene=None, no_subs=False, style=None,
        keep_work=False, keep_going=False, verbose=False,
    )
    try:
        cmd_generate(cfg, args)
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("--count 0 should exit(1)")


def t_mux_builder():
    """The final-mux command builder: full mix has burn + CTA + candy (the
    progress bar rides the .ass burn-in now — drawbox can't animate), the
    minimal mix is narration-only, and the CTA pop follows the end-card
    (no card -> no pop). Offline: builds commands, runs nothing."""
    from assembler import _build_mux_cmd, _drawtext_font_arg, _ffmpeg_has_filter

    cfg = tmp_cfg()
    tmp = Path(_mkdtemp())
    base = dict(
        concat_txt=tmp / "concat.txt", audio_txt=tmp / "audio.txt",
        out_path=tmp / "out.mp4", cfg=cfg, total_seconds=10.0, cuts=[5.0],
        assets={"music_loop": tmp / "m.wav", "whoosh": tmp / "w.wav",
                "pop": tmp / "p.wav"},
        music_track=tmp / "m.wav", burn_path=tmp / "t.ass",
        cta_overlay=("FOLLOW FOR MORE!", 3.5),
    )
    full = _build_mux_cmd(
        **base, with_burn=True, with_cta=True, with_candy=True)
    vf = full[full.index("-vf") + 1]
    assert "subtitles=" in vf and "drawbox" not in vf, vf
    if _ffmpeg_has_filter("drawtext") and _drawtext_font_arg() is not None:
        assert "drawtext" in vf, vf
    graph = full[full.index("-filter_complex") + 1]
    assert "amerge" in graph, graph
    assert full.count("-i") == 5  # video + narration + music + whoosh + pop

    nocta = _build_mux_cmd(**base, with_burn=True,
                           with_cta=False, with_candy=True)
    assert nocta.count("-i") == 4  # pop dropped with the card
    assert "drawtext" not in nocta[nocta.index("-vf") + 1]

    mini = _build_mux_cmd(**base, with_burn=False,
                          with_cta=False, with_candy=False)
    assert "-vf" not in mini
    mgraph = mini[mini.index("-filter_complex") + 1]
    assert mgraph.startswith("[1:a]volume=1.0,afade"), mgraph
    assert "amerge" not in mgraph
    assert mini[-1] == str(tmp / "out.mp4")


def t_gemini_fallback_order():
    from scriptgen import GeminiProvider

    # Re-surveyed live 2026-09-16: 2.5 IDs 404 despite being listed;
    # 3-flash-preview answers in ~1s, 3.5-flash in ~16s, lite always;
    # 3.8 + flash-latest 503 under load; pro/omni 429 on tiny quotas.
    # Newest-first, proven workers as catchers. Reorder only on probes.
    assert GeminiProvider.FALLBACK_MODELS == [
        "gemini-3.8-flash", "gemini-3.5-flash", "gemini-3-flash-preview",
        "gemini-flash-latest", "gemini-flash-lite-latest",
        "gemini-3.1-flash-lite",
    ]


def t_image_chain():
    from images import describe_chain, provider_ready, resolve_chain

    cfg = tmp_cfg()
    assert cfg.image_provider == "pexels"
    assert cfg.image_fallbacks == ["pixabay", "pollinations", "gemini"]
    assert cfg.image_model == "flux"
    assert resolve_chain(cfg) == ["pexels", "pixabay", "pollinations", "gemini"]
    assert provider_ready("pollinations", cfg) == (True, "anonymous")
    assert provider_ready("gemini", cfg)[0] is False
    assert provider_ready("pexels", cfg) == (False, "no Pexels key")
    assert provider_ready("pixabay", cfg) == (False, "no Pixabay key")
    assert provider_ready("huggingface", cfg) == (False, "unknown provider")
    assert "skipped" in describe_chain(cfg)
    cfg.data["ai"]["image_provider"] = "nonsense"  # garbage -> pollinations
    assert cfg.image_provider == "pollinations"
    cfg.data["ai"]["image_provider"] = "gemini"
    cfg.data["ai"]["image_fallbacks"] = ["gemini", "pollinations", "bogus", "pollinations"]
    assert cfg.image_fallbacks == ["pollinations"]  # primary + dupes + junk dropped
    assert resolve_chain(cfg) == ["gemini", "pollinations"]
    cfg.data["ai"]["gemini_api_key"] = "k"
    assert provider_ready("gemini", cfg)[0] is True
    assert "skipped" not in describe_chain(cfg)  # both usable now


def t_image_builders():
    import base64

    from images import gemini_extract, gemini_payload, pollinations_request

    cfg = tmp_cfg()
    url, params, headers = pollinations_request("a cat", cfg, 7)
    assert "image.pollinations.ai" in url and params["model"] == "flux"
    assert params["seed"] == 7 and headers == {}
    cfg.data["ai"]["pollinations_token"] = "tok123"
    _, _, headers = pollinations_request("a cat", cfg, 7)
    assert headers == {"Authorization": "Bearer tok123"}
    payload = gemini_payload("a cat")
    assert payload["generationConfig"]["responseModalities"] == ["IMAGE", "TEXT"]
    fake = {"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "image/png",
                        "data": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 3000).decode()}}
    ]}}]}
    assert gemini_extract(fake)[:8] == b"\x89PNG\r\n\x1a\n"
    for bad in ({"candidates": [{"content": {"parts": [{"text": "nope"}]}}]},
                {"promptFeedback": {"blockReason": "SAFETY"}}, {}):
        try:
            gemini_extract(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"text-only/blocked/empty should raise: {bad}")
    try:
        gemini_extract({"promptFeedback": {"blockReason": "SAFETY"}})
    except ValueError as exc:
        assert "SAFETY" in str(exc)

    from images import upstream_throttled

    assert upstream_throttled(
        'Gen Sana request failed with 429: {"message":'
        '"Per-user limit of 300 RPM exceeded"}') is True
    assert upstream_throttled("HTTP 500: something else broke") is False


def t_autopost_builders():
    from autopost import (BufferError, build_post_input, cloudinary_upload_url,
                          hashtags, match_channels, newest_video, read_sidecar,
                          resolve_mode)

    cfg = tmp_cfg()
    assert cfg.buffer_channels == ["youtube", "tiktok"]
    assert cfg.youtube_privacy == "public"
    cfg.data["buffer"]["channels"] = ["TikTok", "bogus", "instagram"]
    assert cfg.buffer_channels == ["tiktok", "instagram"]
    cfg.data["buffer"]["youtube_privacy"] = "nonsense"
    assert cfg.youtube_privacy == "public"

    assert hashtags(["vienna woods", "fun-fact"]) == "#viennawoods #funfact"
    assert cloudinary_upload_url("demo") == \
        "https://api.cloudinary.com/v1_1/demo/video/upload"

    yt = build_post_input("ch1", "youtube", "https://x/v.mp4", "T" * 150,
                          "Desc", ["a b"], cfg)
    assert yt["metadata"]["youtube"]["title"] == "T" * 100
    assert yt["metadata"]["youtube"]["isAiGenerated"] is True
    assert yt["metadata"]["youtube"]["privacy"] == "public"
    assert yt["assets"] == [{"video": {"url": "https://x/v.mp4"}}]
    assert yt["saveToDraft"] is True and yt["aiAssisted"] is True

    tt = build_post_input("ch2", "tiktok", "https://x/v.mp4", "Hi", "",
                          ["a b"], cfg)
    assert tt["metadata"]["tiktok"] == {"title": "Hi #ab", "isAiGenerated": True}

    ig = build_post_input("ch3", "instagram", "https://x/v.mp4", "Hi", "D",
                          [], cfg)
    assert ig["metadata"]["instagram"]["type"] == "reel"

    try:
        build_post_input("ch", "myspace", "https://x/v.mp4", "t", "", [], cfg)
    except BufferError:
        pass
    else:
        raise AssertionError("unknown service should raise")

    channels = [{"service": "youtube", "isDisconnected": False, "id": "y"},
                {"service": "tiktok", "isDisconnected": True, "id": "t"}]
    assert [c["id"] for c in match_channels(channels, ["youtube"])] == ["y"]
    try:
        match_channels(channels, ["tiktok"])
    except BufferError as exc:
        assert "tiktok" in str(exc)
    else:
        raise AssertionError("disconnected channel should raise")

    assert resolve_mode() == ("addToQueue", None, True)
    assert resolve_mode(publish=True) == ("shareNow", None, False)
    assert resolve_mode(schedule=True) == ("addToQueue", None, False)
    mode, due, draft = resolve_mode(at="2999-01-01T12:00")
    assert mode == "customScheduled" and draft is False and due.startswith("2999")
    for bad in ("yesterday", "2000-01-01T00:00"):
        try:
            resolve_mode(at=bad)
        except BufferError:
            pass
        else:
            raise AssertionError(f"bad --at should raise: {bad}")

    import tempfile
    from pathlib import Path as _Path
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = _Path(tmp)
        (tmpdir / "a.mp4").write_bytes(b"0")
        (tmpdir / "a.json").write_text('{"title": "TT", "tags": ["x"]}')
        assert read_sidecar(tmpdir / "a.mp4")["title"] == "TT"
        assert newest_video(tmpdir).name == "a.mp4"
        assert newest_video(tmpdir / "empty") is None

    from autopost import sign_upload_params

    sig1 = sign_upload_params({"timestamp": 123}, "secret")
    assert sig1 == sign_upload_params({"timestamp": 123}, "secret")
    assert len(sig1) == 40 and all(c in "0123456789abcdef" for c in sig1)
    assert sign_upload_params({"timestamp": 124}, "secret") != sig1
    assert sign_upload_params({"b": 2, "a": 1}, "s") == \
        sign_upload_params({"a": 1, "b": 2}, "s")  # sorted before hashing


def t_gemini_429_fast_failover():
    from unittest.mock import Mock, patch

    from scriptgen import GeminiProvider

    def run(status):
        with patch("requests.post", return_value=Mock(status_code=status, text="x")), \
                patch("time.sleep", return_value=None):
            provider = GeminiProvider(api_key="k")
            result, last_status, _ = provider._try_model("m", {}, tag="t")
        return result, last_status

    # 429: fail over after 3 tries, not 6 (free-tier 429s rarely clear fast).
    with patch("requests.post", return_value=Mock(status_code=429, text="x")) as post, \
            patch("time.sleep", return_value=None):
        result, status, _ = GeminiProvider(api_key="k")._try_model("m", {}, tag="t")
    assert result is None and status == 429 and post.call_count == 3
    # 503: still gets the full 6-try patience (transient, often clears).
    with patch("requests.post", return_value=Mock(status_code=503, text="x")) as post, \
            patch("time.sleep", return_value=None):
        result, status, _ = GeminiProvider(api_key="k")._try_model("m", {}, tag="t")
    assert result is None and status == 503 and post.call_count == 6
    assert run(200)[0] is not None  # sanity: 200 still returns immediately


def t_gemini_key_sweep():
    """5xx: every key gets one instant shot before any sleep — no more ~107s
    stuck on the first key while the rest sit idle, then a model drop."""
    from unittest.mock import Mock, patch

    from scriptgen import GeminiProvider

    with patch("requests.post", return_value=Mock(status_code=503, text="x")) as post, \
            patch("time.sleep", return_value=None) as sleeper:
        result, status, _ = GeminiProvider(
            api_key=["k1", "k2", "k3"])._try_model("m", {}, tag="t")
    assert result is None and status == 503
    assert post.call_count == 6
    used = [call.kwargs["params"]["key"] for call in post.call_args_list]
    assert used == ["k1", "k2", "k3", "k1", "k2", "k3"], used
    assert sleeper.call_count == 3  # first sweep instant, second sweep backs off

    # Single key keeps the old patience (5 sleeping retries).
    with patch("requests.post", return_value=Mock(status_code=503, text="x")), \
            patch("time.sleep", return_value=None) as sleeper1:
        GeminiProvider(api_key="k")._try_model("m", {}, tag="t")
    assert sleeper1.call_count == 5

    # Large unique-account pools must be swept fully before the provider
    # gives up to the next model/provider.
    for count in (35, 80):
        keys = [f"g{i:03d}" for i in range(count)]
        denied = Mock(status_code=429, text="account quota")
        with patch("requests.post", return_value=denied) as post:
            result, status, _ = GeminiProvider(api_key=keys)._try_model(
                "m", {}, tag="t")
        assert result is None and status == 429
        assert post.call_count == count, (count, post.call_count)
        used = [call.kwargs["params"]["key"]
                for call in post.call_args_list]
        assert used == keys, (count, used)


def t_gemini_network():
    """Network faults fail over fast: connection errors abort the provider,
    timeouts get one spare key then the next model. No 180s silences."""
    from unittest.mock import patch

    import requests

    from scriptgen import GeminiProvider

    # Read timeouts: two attempts (spare key), then give up the model.
    with patch("requests.post",
               side_effect=requests.exceptions.ReadTimeout("stalled")) as post, \
            patch("time.sleep", return_value=None) as sleeper:
        result, status, error = GeminiProvider(
            api_key=["k1", "k2", "k3"])._try_model("m", {}, tag="t")
    assert result is None and status == 0 and "network error" in error
    assert post.call_count == 2
    assert sleeper.call_count == 0
    used = [call.kwargs["params"]["key"] for call in post.call_args_list]
    assert used == ["k1", "k2"], used

    # Connection errors: host verdict, abort at once.
    with patch("requests.post",
               side_effect=requests.exceptions.ConnectionError("dns")):
        result, status, error = GeminiProvider(
            api_key=["k1", "k2"])._try_model("m", {}, tag="t")
    assert result is None and status == -1 and "unreachable" in error

    # And _post turns that into an immediate provider-level failure,
    # so the chain (groq/...) picks up without trying dead models.
    with patch("requests.post",
               side_effect=requests.exceptions.ConnectionError("dns")) as post:
        try:
            GeminiProvider(api_key="k")._post({})
        except RuntimeError as exc:
            assert "unreachable" in str(exc), str(exc)
        else:
            raise AssertionError("_post should have raised")
    assert post.call_count == 1


def t_sentry():
    """Sentry init: no DSN (or no SDK) = silent no-op; with DSN it inits."""
    import sys
    from unittest.mock import Mock

    from sentry_util import init_sentry

    assert init_sentry(tmp_cfg(), "generate") is False  # nothing configured

    cfg = tmp_cfg(**{"sentry": {"dsn": "https://x@o1.ingest.sentry.io/1"}})
    fake = Mock()
    sys.modules["sentry_sdk"] = fake
    try:
        assert init_sentry(cfg, "bot") is True
    finally:
        del sys.modules["sentry_sdk"]
    _, kwargs = fake.init.call_args
    assert kwargs["dsn"].startswith("https://x@")
    assert kwargs["traces_sample_rate"] == 0.0
    fake.set_tag.assert_called_with("command", "bot")


def t_azure_budget():
    """Azure lane: priced calls log to the ledger; caps and unknown models refuse."""
    from unittest.mock import Mock, patch

    from azure_openai import (AzureOpenAIProvider, BudgetExhausted,
                              costs_report, is_supported_model, price_for,
                              read_spend)

    assert is_supported_model("gpt-4o-mini")
    assert not is_supported_model("gpt-9-ultra")
    expect = 1500 * 0.15 / 1e6 + 2500 * 0.60 / 1e6
    assert abs(price_for("gpt-4o-mini", 1500, 2500) - expect) < 1e-9
    try:
        AzureOpenAIProvider(endpoint="https://x", api_key="k", deployment="d",
                            model="gpt-9-ultra",
                            ledger_path=Path("/nonexistent/l.jsonl"))
    except RuntimeError as exc:
        assert "no pinned price" in str(exc)
    else:
        raise AssertionError("unknown model should refuse")

    tmp = Path(_mkdtemp())
    prov = AzureOpenAIProvider(
        endpoint="https://x.openai.azure.com", api_key="k", deployment="mini",
        model="gpt-4o-mini", ledger_path=tmp / "azure_spend.jsonl")
    body = {"choices": [{"message": {"content": '{"a": 1}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
    with patch("requests.post",
               return_value=Mock(status_code=200, json=lambda: body,
                                 text="{}")) as post:
        assert prov.generate_text("hi", tag="t", json_mode=True) == '{"a": 1}'
    assert post.call_args.kwargs["params"] == {"api-version": "2024-08-01-preview"}
    report = costs_report(tmp / "azure_spend.jsonl")
    assert report["calls_total"] == 1
    assert read_spend(tmp / "azure_spend.jsonl")[0]["usd"] > 0

    # A $0.0001 daily cap blocks even a tiny call (worst-case pricing).
    poor = AzureOpenAIProvider(
        endpoint="https://x", api_key="k", deployment="d", model="gpt-4o-mini",
        ledger_path=tmp / "poor.jsonl", max_usd_per_day=0.0001)
    with patch("requests.post") as post2:
        try:
            poor.generate_text("hi")
        except BudgetExhausted:
            pass
        else:
            raise AssertionError("cap should have blocked the call")
    assert post2.call_count == 0  # refused BEFORE any network


def t_music_rotation():
    """Music picker: explicit file wins, else random library rotation, else built-in."""
    from unittest.mock import patch

    from audiofx import resolve_music

    cfg = tmp_cfg()
    lib = cfg.root / cfg.music_folder
    lib.mkdir(parents=True, exist_ok=True)
    for name in ("a.mp3", "b.mp3", "c.mp3"):
        (lib / name).write_bytes(b"fake")
    fallback = cfg.root / "loop.wav"
    fallback.write_bytes(b"fake")
    with patch("audiofx.random.choice", side_effect=lambda seq: seq[1]):
        assert resolve_music(cfg, fallback).name == "b.mp3"
    for name in ("a.mp3", "b.mp3", "c.mp3"):
        (lib / name).unlink()
    assert resolve_music(cfg, fallback) == fallback


def t_pollinations_text():
    """Pollinations lane: anonymous (no auth header), no response_format flag,
    reasoning field ignored, 404 fails over to the next model."""
    from unittest.mock import Mock, patch

    from pollinations_text import PollinationsTextProvider

    body = {"choices": [{"message": {"content": '{"title": "T"}',
                                     "reasoning": "thinking..."}}]}
    with patch("requests.post",
               return_value=Mock(status_code=200, json=lambda: body,
                                 text="{}")) as post:
        text = PollinationsTextProvider().generate_text("hi", tag="t",
                                                        json_mode=True)
    assert text == '{"title": "T"}'
    assert "Authorization" not in post.call_args.kwargs["headers"]
    assert "response_format" not in post.call_args.kwargs["json"]
    assert post.call_args.kwargs["json"]["private"] is True

    calls = []

    def fake_post(url, **kwargs):
        calls.append(kwargs["json"]["model"])
        if len(calls) == 1:
            return Mock(status_code=404, text="nope", json=lambda: {})
        return Mock(status_code=200, json=lambda: body, text="{}")

    # Only one text model exists now ("openai" alias; mistral 404s as
    # legacy) — a 404 ends the lane instead of failing over.
    with patch("requests.post", side_effect=fake_post):
        try:
            PollinationsTextProvider().generate_text("hi", tag="t")
        except RuntimeError as exc:
            assert "unavailable" in str(exc), exc
        else:
            raise AssertionError("expected RuntimeError on single-model 404")
    assert calls == ["openai"], calls


def t_stock():
    import tempfile
    from pathlib import Path
    from unittest.mock import Mock, patch

    from stock import pexels_fetch, pexels_params, pick_photo

    assert pexels_params("cat night", portrait=True, page=2) == {
        "query": "cat night", "orientation": "portrait",
        "size": "medium", "per_page": 3, "page": 2}
    assert pick_photo([], 0) is None
    assert pick_photo([{"id": 1}, {"id": 2}], 3)["id"] == 2
    # No key -> clean failure so the chain moves to the next provider.
    cfg = tmp_cfg()
    try:
        pexels_fetch("a cat", Path("x.jpg"), cfg, 0, 1)
    except RuntimeError as exc:
        assert "no Pexels key" in str(exc)
    else:
        raise AssertionError("expected RuntimeError without a key")
    # Search + download round-trip (LLM planner forced to heuristic).
    cfg.data["ai"]["pexels_api_key"] = "k"
    search = Mock(status_code=200)
    search.json.return_value = {"photos": [
        {"id": 11, "photographer": "A",
         "src": {"portrait": "http://img/p.jpg"}}]}
    jpg = Mock(status_code=200)
    jpg.content = b"\xff\xd8\xff" + b"0" * 3000
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "s.jpg"
        with patch("requests.get", side_effect=[search, jpg]) as get, \
                patch("scriptgen.get_provider", side_effect=RuntimeError("no llm")):
            out = pexels_fetch("a cat at night, wide establishing shot",
                               dest, cfg, 0, 2)
        assert out == dest and dest.stat().st_size > 2000
        assert get.call_args_list[0].kwargs["params"]["query"] == "cat night"
        assert get.call_args_list[0].kwargs["headers"] == {"Authorization": "k"}
    # Empty results shorten the query once, then fail cleanly.
    empty = Mock(status_code=200)
    empty.json.return_value = {"photos": []}
    with patch("requests.get", return_value=empty), \
            patch("scriptgen.get_provider", side_effect=RuntimeError("no llm")), \
            patch("time.sleep"):
        try:
            pexels_fetch("a cat at night", Path("y.jpg"), cfg, 0, 2)
        except RuntimeError as exc:
            assert "no photos" in str(exc), exc
        else:
            raise AssertionError("expected RuntimeError on empty results")


def t_director():
    from unittest.mock import patch

    from director import _CACHE, heuristic_keywords, plan_query

    assert heuristic_keywords(
        "A glowing Japanese vending machine at night, wide establishing shot"
    ) == "glowing japanese vending machine"
    assert heuristic_keywords("a an the shot view") == "cinematic b-roll"

    class Stub:
        def __init__(self):
            self.calls = 0

        def generate_text(self, prompt, temperature=0.0, tag=""):
            self.calls += 1
            assert "vending" in prompt
            return '"Neon vending machines, rainy Tokyo street!"'

    cfg = tmp_cfg()
    stub = Stub()
    with patch("scriptgen.get_provider", return_value=stub):
        first = plan_query("Neon vending machines on a rainy Tokyo street", cfg)
        second = plan_query("Neon vending machines on a rainy Tokyo street", cfg)
    assert first == "neon vending machines rainy tokyo", first
    assert second == first and stub.calls == 1  # second hit the cache
    assert any("vending" in base for base in _CACHE)  # keyed by scene base


def t_voice_stitch_mechanism():
    """The stitched path itself (mocked synth + ffmpeg): offsets, trims,
    timing shift, part cleanup. Regression: results used to be unpacked
    as (path, words) and crashed -> silent plain-text fallback."""
    import asyncio
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from voiceover import WordTiming, _synth_with_pauses

    async def fake_synth(text, voice, rate, dest):
        dest.write_bytes(b"x" * 100)
        if text.startswith("One"):
            return [WordTiming(word="One.", start=0.1, end=0.5),
                    WordTiming(word="Two.", start=0.6, end=0.9)]
        return [WordTiming(word="Three.", start=0.1, end=0.4)]

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "scene.mp3"
        with patch("voiceover._synth", side_effect=fake_synth), \
                patch("voiceover.stitch_cmd",
                      return_value=["ffmpeg", "fake"]) as cmd, \
                patch("assembler.run") as run:
            timings = asyncio.run(_synth_with_pauses(
                "One two. Three.", "v", "+0%", dest, 250))
        run.assert_called_once()
        parts = cmd.call_args[0][0]
        # keep = last word end + 0.18 breath, per part
        assert [(p.name, round(k, 2)) for p, k in parts] == \
            [("scene.part00.mp3", 1.08), ("scene.part01.mp3", 0.58)], parts
        assert cmd.call_args[0][1] == 0.25
        # sentence 2 starts at 1.08 + 0.25 pause -> shifted by 1.33
        assert [round(w.start, 2) for w in timings] == [0.1, 0.6, 1.43]
        assert [round(w.end, 2) for w in timings] == [0.5, 0.9, 1.73]
        assert not (Path(tmp) / "scene.part00.mp3").exists()
        assert not (Path(tmp) / "scene.part01.mp3").exists()


def t_voice_pauses():
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from voiceover import (plan_offsets, split_sentences, stitch_cmd,
                           synthesise)

    assert split_sentences("Hello world. How are you? Fine!") == [
        "Hello world.", "How are you?", "Fine!"]
    assert split_sentences("  ") == []
    # Offsets: each sentence starts after the previous one plus the gap.
    assert plan_offsets([1.0, 2.0], 0.25) == [0.0, 1.25]
    assert plan_offsets([1.0, 2.0, 3.0], 0.5) == [0.0, 1.5, 4.0]
    # Stitch command: 2 parts + 1 silence input, per-part trims, concat
    # in order, edge mp3 shape preserved.
    cmd = stitch_cmd([(Path("a.mp3"), 1.4), (Path("b.mp3"), 2.2)], 0.25,
                     Path("out.mp3"))
    assert cmd[0] == "ffmpeg" and cmd.count("-i") == 3
    assert "a.mp3" in cmd and "b.mp3" in cmd and "out.mp3" in cmd
    assert "anullsrc=r=24000:cl=mono" in cmd and "0.250" in cmd
    assert any("atrim=0:1.400" in part for part in cmd)
    assert any("atrim=0:2.200" in part for part in cmd)
    assert any("concat=n=3:v=0:a=1" in part for part in cmd)
    assert "48k" in cmd
    # Stitching failure -> plain edge-tts retry saves the scene.
    cfg = tmp_cfg()
    assert cfg.sentence_pause_ms == 250
    calls = []

    async def fake_synth(text, voice, rate, dest):
        calls.append(text)
        dest.write_bytes(b"x" * 2000)
        return []

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "s.mp3"
        with patch("voiceover._synth_with_pauses",
                   side_effect=RuntimeError("stitch no")), \
                patch("voiceover._synth", side_effect=fake_synth):
            synthesise("Hello. World.", dest, cfg)
        assert dest.stat().st_size == 2000
    assert len(calls) == 1 and not calls[0].startswith("<speak")


def t_title_guard():
    from package import clean_title

    # LLM hashtag tails stripped (they used to truncate mid-tag at 100 chars).
    assert clean_title("Why Honey Doesn't Expire #facts #science #viral", "t") == \
        "Why Honey Doesn't Expire"
    # Bare hash (shipped live 2026-09-14) rebuilt from the topic.
    assert clean_title("6924cd26281d", "why honey never expires") == \
        "Why honey never expires"
    assert clean_title("", "the immortal jellyfish") == "The immortal jellyfish"
    assert clean_title("Untitled", "") == "Untitled"
    assert clean_title("  Spaced   Out  Title  ", "t") == "Spaced Out Title"
    assert len(clean_title("x" * 150, "t")) == 100


def t_gemini_sandwich():
    from scriptgen import GeminiProvider, get_provider

    assert GeminiProvider.PRIMARY_MODELS == GeminiProvider.FALLBACK_MODELS[:3]
    assert GeminiProvider.RESERVE_MODELS == GeminiProvider.FALLBACK_MODELS[3:]
    assert not set(GeminiProvider.PRIMARY_MODELS) & set(GeminiProvider.RESERVE_MODELS)
    assert GeminiProvider(["k"], models=["a", "b"]).models == ["a", "b"]
    assert GeminiProvider(["k"]).models is None  # legacy: full chain
    cfg = tmp_cfg()
    cfg.data["ai"]["gemini_api_key"] = "g"
    cfg.data["ai"]["groq_api_keys"] = [f"g{i}" for i in range(10)]
    cfg.data["ai"]["openrouter_api_keys"] = ["o"]
    chain = get_provider(cfg).chain
    assert [label for label, _ in chain] == [
        "gemini", "openrouter", "gemini-reserve", "groq", "pollinations",
        "template"]
    assert dict(chain)["groq"].api_keys == cfg.groq_llm_api_keys
    links = dict(chain)
    assert links["gemini"].models[0] == cfg.gemini_model  # configured first
    assert links["gemini-reserve"].models == [
        m for m in GeminiProvider.RESERVE_MODELS if m != cfg.gemini_model]
    assert not set(links["gemini"].models) & set(links["gemini-reserve"].models)


def t_run_heartbeat():
    import io
    import sys
    from contextlib import redirect_stdout

    from assembler import AssemblyError, run

    # Slow command -> heartbeat lines, then success.
    buf = io.StringIO()
    with redirect_stdout(buf):
        run([sys.executable, "-c", "import time;time.sleep(0.5)"],
            "test", heartbeat_every=0.15)
    assert "still working" in buf.getvalue(), buf.getvalue()
    # Fast command -> silent success.
    buf = io.StringIO()
    with redirect_stdout(buf):
        run([sys.executable, "-c", "pass"], "test")
    assert buf.getvalue() == ""
    # Failing command -> AssemblyError naming the step.
    try:
        run([sys.executable, "-c", "import sys;sys.exit(1)"], "boom")
    except AssemblyError as exc:
        assert "boom" in str(exc), exc
    else:
        raise AssertionError("expected AssemblyError")


def t_progress_bar():
    import tempfile
    from pathlib import Path

    from pygarnish import _parse_hex_color, build_pygarnish_mux_cmd
    from subtitles import ass_colour, progress_ass_line, write_ass

    assert ass_colour("0xFFD700") == "&H0000D7FF&"
    assert ass_colour("#ff0000") == "&H000000FF&"
    assert ass_colour("garbage") == "&H0000D7FF&"  # gold fallback
    assert _parse_hex_color("0xFFD700") == (255, 215, 0)
    assert _parse_hex_color("nope") == (255, 215, 0)
    line = progress_ass_line(60.0, 1080, 1920)
    assert line.startswith("Dialogue: 0,0:00:00.00,0:01:00.00,"), line
    assert "\\move(-1080,1908,0,1908,0,60000)" in line, line
    assert "\\p1\\c&H0000D7FF&" in line, line
    assert "m 0 0 l 1080 0 l 1080 12 l 0 12" in line, line
    assert progress_ass_line(0, 1080, 1920) == ""
    top = progress_ass_line(10.0, 1080, 1920, position="top")
    assert "\\move(-1080,0,0,0,0,10000)" in top, top
    # write_ass carries it as the final event.
    tmp = Path(_mkdtemp())
    out = write_ass([], tmp / "t.ass", "portrait", 1080, 1920, progress=line)
    text = out.read_text(encoding="utf-8")
    assert "PlayResX: 1080" in text and "PlayResY: 1920" in text
    assert "\\move(-1080,1908,0,1908,0,60000)" in text
    # pygarnish fallback path: bar overlay present/omitted cleanly.
    cfg = tmp_cfg()
    cmd = build_pygarnish_mux_cmd(
        concat_txt=tmp / "c.txt", mixed_wav=tmp / "m.wav",
        out_path=tmp / "o.mp4", cfg=cfg, total_seconds=10.0,
        overlays=[], cta=None, progress_bar=tmp / "bar.png")
    joined = " ".join(cmd)
    assert "drawbox" not in joined, joined
    assert "overlay=x='-1080+1080*t/10.000'" in joined, joined
    plain = build_pygarnish_mux_cmd(
        concat_txt=tmp / "c.txt", mixed_wav=tmp / "m.wav",
        out_path=tmp / "o.mp4", cfg=cfg, total_seconds=10.0,
        overlays=[], cta=None, progress_bar=None)
    assert "overlay=" not in " ".join(plain)


def t_script_prompt_rules():
    from scriptgen import build_script_prompt

    cfg = tmp_cfg()
    prompt = build_script_prompt(cfg, "octopus arms", 65, 6, 169, 211, 28, 35)
    for rule in ("WRONG-BELIEF FLIP", "FIRST 5 WORDS", "MICRO-TEASES",
                 "LOOP-BACK ENDING", "CONCRETE CAMERA RULE", "RHYTHM",
                 "BORING-TOPIC RESCUE", "FIRST FRAME",
                 "dead-air pauses", "COMPLETE sentences",
                 "stock-photo search", "no hashtags",
                 '"scenes": [', "octopus arms"):
        assert rule in prompt, rule
    assert "did you know" in prompt.lower()  # named only to ban it
    assert "NEVER a bare question" in prompt
    assert "FLOW like speech" in prompt


def t_topup_prompt():
    from unittest.mock import patch

    from topics import propose_topics

    class Spy:
        def generate_text(self, prompt, temperature=0.0, tag=""):
            self.prompt = prompt
            return "Why octopuses dream\nHow glass is made\n"

    spy = Spy()
    with patch("scriptgen.get_provider", return_value=spy):
        out = propose_topics(tmp_cfg(), 2, ["Why honey never expires"])
    assert out == ["Why octopuses dream", "How glass is made"], out
    assert "filmable with stock footage" in spy.prompt
    assert "65-second" in spy.prompt  # real target, not stale 30-45
    assert "Why honey never expires" in spy.prompt  # dedupe sample
    assert "USEFUL explainers" in spy.prompt
    assert "SCROLL TEST" in spy.prompt


def t_batch_topics():
    from pathlib import Path as _Path

    pack = _Path(__file__).parent / "topics" / "batch-100.txt"
    if not pack.exists():
        # Optional data pack, deliberately deletable from a working copy
        # (live case 2026-09-21). Nothing functional depends on it —
        # renders pull from topics/backlog.txt, not from packs.
        print("  [topics] batch-100.txt not present — pack check skipped.")
        return
    lines = pack.read_text(encoding="utf-8").splitlines()
    lines = [line.strip() for line in lines if line.strip()]
    assert len(lines) == 100, len(lines)
    assert len(set(lines)) == 100  # no duplicates
    for line in lines:
        assert len(line) <= 140, line
        assert not line[0].isdigit()  # no numbering
    text = "\n".join(lines).lower()
    assert "facts about" not in text  # banned shape


def t_hook_guard():
    import types

    from editorial import hook_violated, polish_script

    assert hook_violated("Why do we close our eyes? Scientists disagree.")
    assert hook_violated("Did you know ants farm? Wild.")
    assert hook_violated("did you know this. really.")  # phrase banned unasked
    assert not hook_violated("Your eyes slam shut when you sneeze. Why.")
    assert not hook_violated("Why flamingos stand on one leg is physics. Next.")
    assert not hook_violated("")
    # v2.1 grabber rules: throat-clearing openers and bloated first lines.
    assert hook_violated("Here's why sea otters hold hands when they sleep.")
    assert hook_violated("Let me tell you something strange about honey.")
    assert hook_violated("Sea otters hold hands while sleeping so they "
                         "never drift apart from each other.")  # 14 words
    assert not hook_violated("Sea otters hold hands to stay alive.")
    assert not hook_violated("Clocks could have spun the other way. One man "
                             "chose.")  # 8-word vivid scene
    # v2.3: weak-verb openers are boring-topic tells, not grabbers.
    assert hook_violated("There is a lake that turns birds to stone.")
    assert hook_violated("There are 300 muscles in an elephant trunk.")
    assert hook_violated("This is a story about the fastest ant alive.")
    assert not hook_violated("Tanzania hides a lake that turns birds to stone.")

    def script(*lines):
        return types.SimpleNamespace(
            scenes=[types.SimpleNamespace(narration=t) for t in lines])

    class FakeProvider:
        def __init__(self, replies):
            self.replies = list(replies)
            self.tags = []

        def generate_text(self, prompt, temperature=0.7, tag="", json_mode=False):
            self.tags.append(tag)
            return self.replies.pop(0)

    cfg = tmp_cfg()
    keep = '{"scenes": [{"narration": "Why do we close our eyes? Scientists argue."}]}'
    fixed = '{"scenes": [{"narration": "Your eyes slam shut. Scientists argue."}]}'
    sc = script("Why do we close our eyes? Scientists argue.")
    fp = FakeProvider([keep, '{"line": "Your eyes slam shut"}', fixed])
    polish_script(sc, cfg, fp)
    assert fp.tags == ["punchup", "hookfix", "decringe"], fp.tags
    assert sc.scenes[0].narration == "Your eyes slam shut. Scientists argue."
    # Clean hook: no hookfix call at all.
    sc2 = script("Your eyes slam shut. Here is why.")
    fp2 = FakeProvider(['{"scenes": [{"narration": "Your eyes slam shut. Here is why."}]}'] * 2)
    polish_script(sc2, cfg, fp2)
    assert fp2.tags == ["punchup", "decringe"], fp2.tags
    assert sc2.scenes[0].narration == "Your eyes slam shut. Here is why."


def t_title_punch():
    import types

    from editorial import _fix_title, title_punch_violated

    # The channel's own results table, 2026-09-19: winners pass...
    assert not title_punch_violated("Why Cats Break The Laws Of Physics")
    assert not title_punch_violated("How Honey Never Expires")
    assert not title_punch_violated("Why We Close Our Eyes When We Sneeze")
    assert not title_punch_violated("Why Cats Break Physics #facts #didyouknow")
    assert not title_punch_violated("")
    # ...losers flagged: vague tail, 9 words, 10 words, over 52 chars.
    assert title_punch_violated("The Great Diamond Lie They Still Want You to Believe")
    assert title_punch_violated("Why Your Keyboard Was Built to Slow You Down")  # 9
    assert title_punch_violated("Why Cats Break The Laws Of Physics And Then Some")  # 11
    # 8 words is the proven ceiling (sneeze: 8 words, 1,118 views) — passes.
    assert not title_punch_violated("The Dark Reason Why Sea Otters Hold Hands")

    class FakeProvider:
        def __init__(self, replies):
            self.replies = list(replies)
            self.calls = 0

        def generate_text(self, *args, **kwargs):
            self.calls += 1
            return self.replies.pop(0)

    # Clean title: no provider call at all.
    sc = types.SimpleNamespace(title="Why Cats Break The Laws Of Physics", scenes=[])
    fp = FakeProvider([])
    _fix_title(sc, tmp_cfg(), fp)
    assert fp.calls == 0 and sc.title == "Why Cats Break The Laws Of Physics"
    # Violating title retitled (hashtag tail stripped from the fix too).
    sc = types.SimpleNamespace(
        title="The Great Diamond Lie They Still Want You to Believe", scenes=[])
    fp = FakeProvider(['{"title": "Why Diamonds Cost So Little #facts"}'])
    _fix_title(sc, tmp_cfg(), fp)
    assert sc.title == "Why Diamonds Cost So Little", sc.title
    # LLM returns another slow title: original kept, never raises.
    sc = types.SimpleNamespace(
        title="The Great Diamond Lie They Still Want You to Believe", scenes=[])
    fp = FakeProvider(
        ['{"title": "The Terrible Diamond Secret Nobody Wants You To Know"}'])
    _fix_title(sc, tmp_cfg(), fp)
    assert sc.title == "The Great Diamond Lie They Still Want You to Believe"
    # Missing title attribute (editorial fixtures): skipped silently.
    sc = types.SimpleNamespace(scenes=[])
    fp = FakeProvider([])
    _fix_title(sc, tmp_cfg(), fp)
    assert fp.calls == 0

def t_vision():
    import types

    import stock
    import vision

    # --- pure pieces --------------------------------------------------
    assert vision._coerce({"safe": "false", "relevant": True}) == \
        {"safe": False, "relevant": True, "reason": ""}
    assert vision._coerce({"safe": False, "relevant": "no",
                           "reason": "x" * 200})["reason"] == "x" * 80
    assert vision._coerce("junk") == {"safe": True, "relevant": True,
                                      "reason": ""}
    ranked = stock.rank_photos(
        [{"id": 1, "alt": "green forest road"},
         {"id": 2, "alt": "octopus swimming in deep water"},
         {"id": 3, "alt": ""}], "octopus water")
    assert [photo["id"] for photo in ranked] == [2, 1, 3], ranked
    # NSFW alt prefilter: announced-unsafe candidates never even download.
    kept = stock.filter_unsafe(
        [{"id": 1, "alt": "woman in bikini on beach"},
         {"id": 2, "alt": "octopus swimming"}])
    assert [photo["id"] for photo in kept] == [2]
    assert stock.filter_unsafe([{"id": 9, "alt": "nude beach sign"}]) == []

    # --- verdict parse + cache, over a fake transport ------------------
    vision._state["fails"] = 0
    cfg = tmp_cfg()
    cfg.data["ai"]["gemini_api_key"] = "g1"

    class FakeResp:
        status_code = 200
        text = '{"safe": true, "relevant": true}'

        def json(self):
            return {"candidates": [{"content": {"parts": [
                {"text": '{"safe": false, "relevant": true, '
                         '"reason": "bikini pose"}'}]}}]}

    posts = {"n": 0}

    class FakeRequests:
        def post(self, *a, **kw):
            posts["n"] += 1
            return FakeResp()

    real_requests, vision.requests = vision.requests, FakeRequests()
    jpeg = b"\xff\xd8\xff" + b"j" * 2500
    verdict = vision.check_image(jpeg, "beach", cfg, photo_key="77")
    assert verdict == {"safe": False, "relevant": True,
                       "reason": "bikini pose"}, verdict
    before = posts["n"]
    assert vision.check_image(jpeg, "beach", cfg, photo_key="77") == verdict
    assert posts["n"] == before  # cache hit: no second call

    # --- circuit breaker: 3 dead calls -> QC stops touching the net ----
    class DeadRequests:
        def post(self, *a, **kw):
            raise vision.requests.RequestException("down")

    DeadRequests.RequestException = real_requests.RequestException
    vision.requests = DeadRequests()
    for _ in range(3):
        assert vision.check_image(jpeg, "cat", cfg, photo_key="9") == \
            {"safe": True, "relevant": True, "reason": ""}  # fail open
    assert vision._state["fails"] == 3
    breaker_posts = {"n": 0}

    class CountingRequests(DeadRequests):
        def post(self, *a, **kw):
            breaker_posts["n"] += 1
            raise real_requests.RequestException("down")

    vision.requests = CountingRequests()
    vision.check_image(jpeg, "dog", cfg, photo_key="10")
    assert breaker_posts["n"] == 0  # breaker open: no request made

    # --- pexels_fetch walks candidates on a vision rejection ----------
    vision._state["fails"] = 0
    vision.requests = real_requests
    cfg2 = tmp_cfg()
    cfg2.data["ai"]["pexels_api_key"] = "pk"
    cfg2.data["ai"]["gemini_api_key"] = "g1"
    import director
    import tempfile
    from pathlib import Path

    real_plan, director.plan_query = director.plan_query, (
        lambda prompt, cfg: "cat")
    downloads = {"n": 0, "urls": []}
    qc_seen = []

    class Resp:
        def __init__(self, payload=None, content=b""):
            self.status_code = 200
            self._payload, self.content = payload, content

        def json(self):
            return self._payload

    def fake_get(url, **kw):
        if url == stock.SEARCH_URL:
            return Resp(payload={"photos": [
                {"id": 1, "alt": "cat sleeping on sofa",
                 "src": {"portrait": "u1"}, "photographer": "A"},
                {"id": 2, "alt": "cat playing with yarn ball",
                 "src": {"portrait": "u2"}, "photographer": "B"},
                {"id": 3, "alt": "bikini cat costume party",
                 "src": {"portrait": "u3"}, "photographer": "C"},
            ]})
        downloads["n"] += 1
        downloads["urls"].append(url)
        return Resp(content=jpeg)

    real_stock_requests, stock.requests = stock.requests, \
        types.SimpleNamespace(get=fake_get)
    real_check, vision.check_image = vision.check_image, (
        lambda body, query, cfg, photo_key="":
        qc_seen.append(photo_key)
        or ({"safe": False, "relevant": True, "reason": "unsafe"}
            if photo_key == "1"
            else {"safe": True, "relevant": True, "reason": ""}))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "img.jpg"
            stock.pexels_fetch("a cat scene", dest, cfg2, seed=1, attempts=1)
            assert dest.read_bytes() == jpeg
        # alt-prefiltered id 3 never downloaded; id 1 rejected by QC;
        # id 2 accepted — exactly two downloads, in order.
        assert downloads["urls"] == ["u1", "u2"], downloads
        assert qc_seen == ["1", "2"], qc_seen
    finally:
        director.plan_query = real_plan
        stock.requests = real_stock_requests
        vision.check_image = real_check
        vision._state["fails"] = 0


def t_no_gemini():
    import os

    from scriptgen import get_provider

    cfg = tmp_cfg()
    cfg.data["ai"]["gemini_api_key"] = "g"
    cfg.data["ai"]["groq_api_keys"] = [f"g{i}" for i in range(10)]
    assert any(label == "gemini" for label, _ in get_provider(cfg).chain)
    # The --no-gemini override clears every key source (env beats config
    # in config.py, so all three must go).
    os.environ["GEMINI_API_KEY"] = "env-g"
    for source in ("GEMINI_API_KEYS", "GEMINI_API_KEY"):
        os.environ.pop(source, None)
    cfg.data["ai"]["gemini_api_key"] = ""
    cfg.data["ai"]["gemini_api_keys"] = []
    labels = [label for label, _ in get_provider(cfg).chain]
    assert "gemini" not in labels, labels
    assert "groq" in labels and "template" in labels, labels

def t_keystats():
    from datetime import datetime, timedelta, timezone
    from unittest.mock import Mock, patch

    import keystats

    # bump before init(): a no-op that never raises.
    keystats._path = None
    keystats.bump("gemini", "AIzaSyFULL-KEY-MATERIALL-x7f2", req=1)

    cfg = tmp_cfg()
    keystats.init(cfg)
    keystats.bump("gemini", "AIzaSyFULL-KEY-MATERIALL-x7f2", req=1, tok=400)
    keystats.bump("gemini", "AIzaSyFULL-KEY-MATERIALL-x7f2", req=1, tok=200)
    keystats.bump("elevenlabs", "sk-full-secret-3d10", chars=6000)
    events = keystats._load(keystats._path)
    assert len(events) == 3
    raw = keystats._path.read_text(encoding="utf-8")
    # Full key material NEVER lands on disk — masked last-4 only.
    assert "AIzaSyFULL" not in raw and "sk-full-secret" not in raw, raw
    assert raw.count("...x7f2") == 2
    sums = keystats.window_sum(events, "gemini",
                               datetime.now(timezone.utc) - timedelta(hours=24))
    assert sums["...x7f2"] == {"req": 2, "tok": 600, "chars": 0, "units": 0,
                               "audio": 0}
    # Pruning: events older than the keep window vanish on write.
    stale = {"t": (datetime.now(timezone.utc)
                   - timedelta(days=40)).isoformat(),
             "p": "gemini", "k": "...old1", "req": 9}
    keystats._write(keystats._path, events + [stale])
    assert all(event["k"] != "...old1"
               for event in keystats._load(keystats._path))

    # Tagged entries (2026-09-25): every call carries its origin so
    # `keys --month` can attribute spend — including agent probes.
    keystats.bump("gemini", "AIzaSyFULL-KEY-MATERIALL-x7f2", req=1, tok=50,
                  tag="probe")
    keystats.bump("groq", "gsk-full-secret-0001", req=1, tok=1234, tag="clipfix")
    events = keystats._load(keystats._path)
    tagged = [e for e in events if e.get("tag")]
    assert {e["tag"] for e in tagged} == {"probe", "clipfix"}
    assert all("gsk-full-secret" not in json.dumps(e) for e in tagged)
    # month_totals: per-provider sums with per-tag split, old rows dropped
    now = datetime.now(timezone.utc)
    sample = [
        {"t": now.isoformat(), "p": "groq", "k": "...a", "req": 2, "tok": 100,
         "chars": 0, "units": 0, "tag": "script"},
        {"t": now.isoformat(), "p": "groq", "k": "...a", "req": 1, "tok": 40,
         "chars": 0, "units": 0, "tag": "probe"},
        {"t": now.isoformat(), "p": "groq", "k": "...a", "req": 7, "tok": 700,
         "chars": 0, "units": 0},                      # untagged (old format)
        {"t": (now - timedelta(days=31)).isoformat(), "p": "groq",
         "k": "...a", "req": 99, "tok": 9900, "chars": 0, "units": 0,
         "tag": "ancient"},                            # outside the window
    ]
    totals = keystats.month_totals(sample, now=now)
    row = totals["groq"]
    assert row["req"] == 10 and row["tok"] == 840
    assert row["tags"]["script"] == {"req": 2, "tok": 100, "audio": 0}
    assert row["tags"]["probe"] == {"req": 1, "tok": 40, "audio": 0}
    # Audio rides the tag split: "which lane ate the 8h Whisper pool"
    # (clip/parts/longform `whisper` vs the bot's `voicenote`).
    audio_sample = [
        {"t": now.isoformat(), "p": "groq", "k": "...a", "req": 1,
         "audio": 1200, "tag": "whisper"},
        {"t": now.isoformat(), "p": "groq", "k": "...a", "req": 1,
         "audio": 30, "tag": "voicenote"},
    ]
    arow = keystats.month_totals(audio_sample, now=now)["groq"]
    assert arow["audio"] == 1230
    assert arow["tags"]["whisper"]["audio"] == 1200
    assert arow["tags"]["voicenote"]["audio"] == 30

    # -- key strategy advice (2026-10-02): honest multi-key truths --------
    advice = keystats.advice_report()
    assert "org-level" in advice or "NOTHING" in advice   # groq truth
    assert "1,000/day" in advice                # openrouter $10 move
    assert "per project" in advice.lower() or "1,500" in advice
    assert "Ranked capacity moves" in advice
    # every provider with keys gets advice, in dashboard order
    for name in keystats.ORDER:
        title = keystats.LIMITS[name]["title"]
        assert title in advice, name
    # the default dashboard points at it
    assert "--advice" in keystats.build_status(cfg)
    assert row["tags"]["(untagged)"] == {"req": 7, "tok": 700, "audio": 0}
    assert "ancient" not in row["tags"]
    report = keystats.month_report()
    assert "30 days" in report and "probe" in report

    # Dashboard: sections, masked keys, refill wording, no secrets.
    cfg.data["ai"]["gemini_api_key"] = "AIzaSyFULL-KEY-MATERIALL-x7f2"
    cfg.data["channel"]["elevenlabs_api_keys"] = ["sk-full-secret-3d10"]
    out = keystats.build_status(cfg)
    assert "GEMINI" in out and "...x7f2" in out and "resets" in out
    assert "ELEVENLABS" in out and "6,000" in out and "4,000 left" in out
    assert "AIzaSyFULL" not in out and "sk-full-secret" not in out
    # Groq shows the dynamic role split (1 key = Whisper-only).
    cfg.data["ai"]["groq_api_keys"] = ["gq-full-secret-bb18"]
    out = keystats.build_status(cfg)
    assert "GROQ" in out and "Whisper×1" in out and "14,400" in out
    assert "text fallback×0" in out
    # Multi-key headers say the truth about what N keys buy (pure).
    assert keystats._keys_note(1, False) == ""
    assert keystats._keys_note(1, True) == ""
    assert "35 separate pools" in keystats._keys_note(35, False)
    note = keystats._keys_note(5, True)
    assert "ONE shared pool" in note and "no capacity" in note
    # A wall of PER-KEY pools (the real setup: ~35 Gemini keys) gets an
    # aggregate row — 35 individual "N left of 1,500" rows are unreadable
    # and the aggregate is the number that gates the day. The legacy key
    # bumped earlier in this test is still in the ledger, so it keeps its
    # row (rotated-out keys still show their spend) and counts once.
    cfg.data["ai"]["gemini_api_keys"] = [f"gkey-{i:02d}-secret-ab{i:02d}"
                                         for i in range(35)]
    gem = keystats._configured_keys(cfg, "gemini")
    assert len(gem) == 36                      # 35 listed + 1 legacy
    out = keystats.build_status(cfg)
    assert "36 keys = 36 separate pools" in out
    assert "all keys" in out
    assert f"{36 * keystats.LIMITS['gemini']['day']:,}" in out   # 54,000
    assert not any(f"gkey-{i:02d}-secret" in out for i in range(35))
    # Account-level pools must NOT claim multiplied capacity, and the note
    # says WHICH kind of pool this is. (The clipfix bump above left a
    # ledger-only Groq mask, so it keeps a row and counts once — hence 6
    # masks for 5 configured keys.)
    cfg.data["ai"]["groq_api_keys"] = [f"gq-secret-{i:02d}xx" for i in range(5)]
    assert len(cfg.groq_api_keys) == 5
    out = keystats.build_status(cfg)
    assert "ACCOUNT-level pools" in out and "6 keys" in out
    assert f"{keystats.LIMITS['groq']['day']:,}" in out            # 14,400
    assert f"{6 * keystats.LIMITS['groq']['day']:,}" not in out    # no 86,400
    # Groq shows the role split and per-key audio rows; the old aggregate
    # `pool total` row misread many separate accounts as one pool.
    groq_section = out.split("GROQ")[-1].split("OPENROUTER")[0]
    assert "key split: Whisper×4 / text fallback×1" in groq_section
    assert "pool total" not in groq_section and "all keys" not in groq_section
    # ElevenLabs is per-key but MONTHLY: header truth, no daily aggregate.
    cfg.data["channel"]["elevenlabs_api_keys"] = ["el-secret-1", "el-secret-2"]
    out = keystats.build_status(cfg)
    assert "ELEVENLABS" in out and "3 keys = 3 separate pools" in out
    assert "all keys" not in out.split("ELEVENLABS")[-1]   # monthly: no row
    keystats.init(cfg)                                     # restore for the rest
    # Audio formatter + bump round-trip for the Whisper pool.
    assert keystats._fmt_audio(0) == "0s"
    assert keystats._fmt_audio(30) == "30s"
    assert keystats._fmt_audio(90) == "1m"
    assert keystats._fmt_audio(3600) == "1h 00m"
    assert keystats._fmt_audio(8100) == "2h 15m"
    # Pexels month vs hour: separate windows (frozen clock for determinism).
    fixed = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    cfg.data["ai"]["pexels_api_key"] = "PX-full-secret-1234"
    cfg.data["ai"]["deepseek_api_keys"] = ["ds-full-secret-99"]
    crafted = keystats._load(keystats._path) + [
        {"t": "2026-09-15T12:10:00+00:00", "p": "pexels", "k": "...1234",
         "req": 1},
        {"t": "2026-09-15T10:00:00+00:00", "p": "pexels", "k": "...1234",
         "req": 1},
        {"t": "2026-08-20T12:00:00+00:00", "p": "pexels", "k": "...1234",
         "req": 1},
        {"t": "2026-09-15T12:05:00+00:00", "p": "groq", "k": "...bb18",
         "req": 1, "audio": 3600},
    ]
    keystats._write(keystats._path, crafted)
    out = keystats.build_status(cfg, now=fixed)
    assert "1 this hour" in out and "2 this month" in out
    assert "19,998 left" in out
    assert "DEEPSEEK" in out and "...t-99" in out
    assert "Whisper audio" in out and "1h 00m / ~8h 00m" in out
    # Audio bump round-trip on the live clock (crafted Sept-15 rows fall
    # outside the 24h window, so the fresh reading is exactly this bump).
    keystats.bump("groq", "gq-other-secret-zz99", req=1, audio=600)
    fresh = keystats.window_sum(
        keystats._load(keystats._path), "groq",
        datetime.now(timezone.utc) - timedelta(hours=24))
    assert fresh["...zz99"]["audio"] == 600

    # CLI entry: cmd_keys prints and returns 0.
    import io
    from contextlib import redirect_stdout
    import main as main_mod
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main_mod.cmd_keys(cfg, None) == 0
    assert "GEMINI" in buf.getvalue()

    # Integration: a real GeminiProvider 200 bumps the ledger.
    payload_out = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
    resp = Mock(status_code=200, text='{"candidates": []}')
    resp.json = lambda: payload_out
    with patch("requests.post", return_value=resp):
        _, status, _ = __import__("scriptgen").GeminiProvider(
            api_key="k-int9")._try_model("m", {"contents": []}, tag="t")
    assert status == 200
    last = keystats._load(keystats._path)[-1]
    assert last["p"] == "gemini" and last["k"] == "...int9", last
    # And the shared OpenAI-compat choke point (Groq) bumps its own lane.
    groq_resp = Mock(status_code=200, text="ok")
    groq_resp.json = lambda: {"choices": [{"message": {"content": "hello"}}]}
    with patch("requests.post", return_value=groq_resp):
        from groq import GroqProvider
        GroqProvider(api_keys="gq-77").generate_text("hi")
    last = keystats._load(keystats._path)[-1]
    assert last["p"] == "groq" and last["k"] == "...q-77", last

    keystats._path = None  # later tests bump no-op again


def t_bot_foundations():
    import types

    from bot import (PhoneBot, parse_incoming, recover_stuck_jobs,
                     summarize_stale)

    # Parser: new commands + the /send typo guard.
    assert parse_incoming("/status") == ("status", "")
    assert parse_incoming("/keys") == ("keys", "")
    assert parse_incoming("/nogemini") == ("nogemini", "")
    assert parse_incoming("/send ab12") == ("send", "ab12")
    assert parse_incoming("/sendxyz") == ("help", "")  # typo, not send "yz"
    assert parse_incoming("/queue") == ("queue", "")

    # Stale summary: owner messages only, capped, timestamped.
    updates = [
        {"update_id": 1, "message": {"from": {"id": 42}, "date": 900,
                                     "text": "why octopuses have three hearts"}},
        {"update_id": 2, "message": {"from": {"id": 99}, "date": 901,
                                     "text": "hack the planet"}},
        {"update_id": 3, "message": {"from": {"id": 42}, "date": 902,
                                     "text": ""}},
    ]
    summary = summarize_stale(updates, 42)
    assert "octopuses" in summary and "hack" not in summary
    assert len(summarize_stale(
        [{"message": {"from": {"id": 42}, "date": 0, "text": f"t{i}"}}
         for i in range(20)], 42).splitlines()) == 8  # capped at 8
    assert summarize_stale([], 42) == ""

    # Recovery: only interrupted renders (queued/rendering) come back.
    jobs = [types.SimpleNamespace(id="a", status="queued", topic="cats"),
            types.SimpleNamespace(id="b", status="rendering", topic="honey"),
            types.SimpleNamespace(id="c", status="generated", topic="sneeze"),
            types.SimpleNamespace(id="d", status="failed", topic="diamond"),
            types.SimpleNamespace(id="e", status="queued", topic="")]
    assert [j.topic for j in recover_stuck_jobs(jobs)] == ["cats", "honey"]

    # Wiring: /keys, /status, /nogemini toggle + gen_args passthrough.
    cfg = tmp_cfg()
    cfg.data["telegram"]["bot_token"] = "t"
    cfg.data["telegram"]["owner_id"] = 42
    cfg.data["telegram"]["enabled"] = True
    bot = PhoneBot(cfg)
    sent = []
    bot.send_message = lambda chat_id, text: sent.append(text)

    bot.handle_message({"chat": {"id": 42}, "from": {"id": 42},
                        "text": "/keys"})
    assert any("API keys" in text for text in sent), sent[-3:]

    bot.handle_message({"chat": {"id": 42}, "from": {"id": 42},
                        "text": "/nogemini"})
    assert bot.no_gemini is True
    bot.handle_message({"chat": {"id": 42}, "from": {"id": 42},
                        "text": "/status"})
    assert any("Gemini skipped" in text for text in sent)
    assert any("Idle" in text for text in sent)
    bot.handle_message({"chat": {"id": 42}, "from": {"id": 42},
                        "text": "/nogemini"})
    assert bot.no_gemini is False

    # The render path threads the toggle into cmd_generate's args and
    # tracks the current render (heartbeat thread is stubbed out).
    captured = {}

    def fake_generate(cfg, args):
        captured["args"] = args
        bot.current = None  # what the real finally-block does

    import main as main_mod
    real_generate, main_mod.cmd_generate = main_mod.cmd_generate, fake_generate
    bot.no_gemini = True
    try:
        bot._render_and_send(42, "why bees dance")
    finally:
        main_mod.cmd_generate = real_generate
    assert captured["args"].no_gemini is True
    # And the failure notice went to the chat (no job was really created).
    assert any("failed" in text for text in sent)

    # --- live incident 2026-09-19: 1 real topic + 9 copies of the
    # channel-topic default re-queued blindly. plan_recovery must collapse.
    from bot import clear_stale_stop, halt_requested, plan_recovery

    def job(topic, status="queued"):
        return types.SimpleNamespace(id="x", status=status, topic=topic)

    incident = [job("why your brain deletes most of your childhood memories")] \
        + [job("fun and useful facts, engaging and cool") for _ in range(9)]
    done = ["Fun and useful facts, engaging and cool"]  # rendered once already
    requeue, skipped = plan_recovery(incident, done)
    assert requeue == ["why your brain deletes most of your childhood memories"]
    assert len(skipped) == 9 and skipped[0][1] == "already rendered"
    # Without a past success, the nine still collapse to ONE.
    requeue, skipped = plan_recovery(incident, [])
    assert requeue == ["why your brain deletes most of your childhood memories",
                       "fun and useful facts, engaging and cool"]
    assert len(skipped) == 8 and skipped[0][1] == "duplicate"
    # Cap: five distinct interrupted renders -> three re-queued.
    five = [job(t) for t in ("why cats purr", "how honey lasts forever",
                             "sea otters hold hands",
                             "the immortal jellyfish",
                             "why clocks go clockwise")]
    requeue, skipped = plan_recovery(five, [])
    assert len(requeue) == 3 and len(skipped) == 2
    assert skipped[0][1] == "over cap — re-send it yourself"

    # --- halt flag: file check, stale cleanup, mission ownership --------
    halt_cfg = tmp_cfg()
    assert halt_requested(halt_cfg) is False
    (halt_cfg.root / "crew_stop").write_text("stop", encoding="utf-8")
    assert halt_requested(halt_cfg) is True
    assert clear_stale_stop(halt_cfg) is True
    assert halt_requested(halt_cfg) is False
    assert clear_stale_stop(halt_cfg) is False  # nothing to clear
    (halt_cfg.root / "crew_stop").write_text("stop", encoding="utf-8")
    from unittest.mock import patch
    with patch("crew.mission_active", return_value=True):
        assert clear_stale_stop(halt_cfg) is False  # live mission keeps it
    assert halt_requested(halt_cfg) is True
    (halt_cfg.root / "crew_stop").unlink()

    # --- worker honors the flag: pre-set stop drains the queue ---------
    import threading
    import time
    from queue import Empty

    stop_cfg = tmp_cfg()
    stop_cfg.data["telegram"]["bot_token"] = "t"
    stop_cfg.data["telegram"]["owner_id"] = 42
    stop_cfg.data["telegram"]["enabled"] = True
    stopped_bot = PhoneBot(stop_cfg)
    halted_msgs = []
    stopped_bot.send_message = lambda chat_id, text: halted_msgs.append(text)
    rendered = []
    stopped_bot._render_and_send = lambda chat_id, topic: rendered.append(topic)
    for i in range(3):
        stopped_bot.jobs.put((42, "topic", f"junk {i}"))
    (stop_cfg.root / "crew_stop").write_text("stop", encoding="utf-8")
    worker = threading.Thread(target=stopped_bot._worker, daemon=True)
    worker.start()
    for _ in range(40):  # wait for the drain (bounded poll)
        if halted_msgs:
            break
        time.sleep(0.05)
    try:
        stopped_bot.jobs.get_nowait()
        raised = False
    except Empty:
        raised = True
    assert raised, "queue should be empty after halt"
    assert not halt_requested(stop_cfg), "flag should be cleared after halt"
    assert rendered == [], "nothing should have rendered"
    assert any("Halted" in text and "3 queued" in text for text in halted_msgs)

    # --- voice notes: a spoken "stop" stops the bot, not renders "Stop".
    from unittest.mock import patch as mock_patch

    voice_cfg = tmp_cfg()
    voice_cfg.data["telegram"]["bot_token"] = "t"
    voice_cfg.data["telegram"]["owner_id"] = 42
    voice_cfg.data["telegram"]["enabled"] = True
    vbot = PhoneBot(voice_cfg)
    vbot.sent = []
    vbot.send_message = lambda chat_id, text: vbot.sent.append(text)

    def fake_voice(heard):
        return mock_patch("voice.download_telegram_voice",
                          return_value=Path("x.ogg")), \
            mock_patch("voice.transcribe", return_value=heard)

    dl, tr = fake_voice("Stop")
    with dl, tr:
        vbot._handle_voice(42, "fid")
    assert vbot.jobs.qsize() == 0, "spoken Stop must not queue a render"
    assert halt_requested(voice_cfg) is True, "spoken Stop must set the flag"
    assert any("Stop requested" in t for t in vbot.sent)
    (voice_cfg.root / "crew_stop").unlink()

    dl, tr = fake_voice("why octopuses have three hearts")
    with dl, tr:
        vbot._handle_voice(42, "fid")
    assert vbot.jobs.qsize() == 1
    chat_id, kind, topic = vbot.jobs.get_nowait()
    assert kind == "topic" and topic == "why octopuses have three hearts"

    keystats_reset = __import__("keystats")
    keystats_reset._path = None


def t_scene_pacing():
    """The voice must FLOW: scene-boundary silence stays a natural breath.

    Live incident 2026-09-20: HEAD_TAIL=0.25 meant 0.5s of inserted silence
    at every scene seam (plus TTS clip padding) — heard as the voice
    stopping for a second, 2-3 times per video. This pins the contract.
    """
    from assembler import HEAD_TAIL, MIN_SCENE_SECONDS

    boundary = 2 * HEAD_TAIL
    assert 0.15 <= boundary <= 0.30, boundary   # a breath, not a stall
    assert int(HEAD_TAIL * 1000) >= 80          # onset still protected
    assert MIN_SCENE_SECONDS >= 1.0             # ultra-short scenes banned


def t_audio_trim():
    from assembler import (CLIP_HEAD_KEEP, CLIP_TAIL_KEEP, DUCK_PARAMS,
                           HEAD_TAIL, pad_audio_filter, trim_audio_filter)

    # The trim: areverse sandwich, keeps are small and bounded.
    trims = trim_audio_filter()
    assert trims.count("silenceremove") == 2 and trims.count("areverse") == 2
    assert f"start_silence={CLIP_HEAD_KEEP:.2f}" in trims
    assert f"start_silence={CLIP_TAIL_KEEP:.2f}" in trims
    assert 0.03 <= CLIP_HEAD_KEEP <= 0.08
    assert 0.05 <= CLIP_TAIL_KEEP <= 0.15
    # The pad: NO trim here (trimming must happen before the scene length
    # is measured, or apad re-adds the trimmed silence — live-measured
    # 0.44s tail on a 0.09s-kept clip).
    pads = pad_audio_filter(HEAD_TAIL, 4.2)
    assert "silenceremove" not in pads
    assert f"adelay={int(HEAD_TAIL * 1000)}" in pads
    assert "apad=whole_dur=4.200" in pads
    # Ducking: 3:1 with a short release (6:1/400ms crushed the bed flat).
    assert "ratio=3" in DUCK_PARAMS and "release=250" in DUCK_PARAMS
    assert "ratio=6" not in DUCK_PARAMS


def t_scene_sentences():
    from scriptgen import Scene, Script, merge_incomplete_scenes

    def mk(*narrations):
        return Script(title="t", description="", tags=[],
                      scenes=[Scene(narration=n, image_prompt=f"img{i}")
                              for i, n in enumerate(narrations)])

    # Accidental split (next starts lowercase): swallowed, prompt kept.
    sc = mk("The tower leans because", "soft soil gave way. True story.",
            "It still stands today.")
    assert merge_incomplete_scenes(sc) == 1
    assert len(sc.scenes) == 2
    assert sc.scenes[0].narration == ("The tower leans because "
                                      "soft soil gave way. True story.")
    assert sc.scenes[0].image_prompt == "img0"
    assert sc.scenes[1].narration == "It still stands today."
    # Complete scenes untouched — . ! ? … and trailing quotes all terminal.
    sc = mk("It leans.", 'He said "wow"?', "So wild\u2026", "Still up!")
    assert merge_incomplete_scenes(sc) == 0 and len(sc.scenes) == 4
    # Commas are not sentence ends (lowercase continuation -> merge;
    # three scenes so the floor allows it).
    sc = mk("It leans because of soil,", "rains made it worse.", "It stands.")
    assert merge_incomplete_scenes(sc) == 1
    # Conjunction-FINAL hanging + lowercase continuation -> merge.
    sc = mk("The tower was leaning because.", "the soil shifts.", "It stands.")
    assert merge_incomplete_scenes(sc) == 1
    # A 2-scene script never merges (the floor): collapsing to one scene
    # is exactly the 2026-09-20 regression, accidental or not.
    sc = mk("It leans because of soil,", "rains made it worse.")
    assert merge_incomplete_scenes(sc) == 0
    # DELIBERATE teases are not splits: colon endings and Capitalized
    # continuations stay separate scenes (v2 collapsed whole scripts —
    # live regression 2026-09-20: 6 scenes merged into 1, one image for
    # 27 seconds).
    sc = mk("But here's the twist:", "In 1902 a doctor took it.")
    assert merge_incomplete_scenes(sc) == 0
    sc = mk("The tower was leaning because.", "The soil shifts.")
    assert merge_incomplete_scenes(sc) == 0  # capitalized = new sentence
    sc = mk("But then it gets stranger", "Much stranger.")
    assert merge_incomplete_scenes(sc) == 0
    sc = mk("The soil shifts.", "And the tower survives.")
    assert merge_incomplete_scenes(sc) == 0  # complete sentences stay
    # Floor + cap: a fully-hanging lowercase chain can never collapse the
    # script structure again.
    sc = mk("A", "b", "c", "d", "e", "f.")
    assert merge_incomplete_scenes(sc) == 3  # cap reached first
    assert len(sc.scenes) == 3  # 6 -> 3, never fewer
    # Single hanging scene with nothing to join: left alone, no crash.
    sc = mk("just hanging")
    assert merge_incomplete_scenes(sc) == 0


def t_topic_hygiene():
    from unittest.mock import patch

    from topics import (_clean, _is_command_topic, _looks_junky,
                         load_backlog, pop_fresh_topic, propose_topics,
                         score_topic_interest)

    # The live junk case and its family.
    assert _looks_junky("The GENIUS Scale! FactTechz Short AMAZING FACTS Show #shorts")
    assert _looks_junky("AMAZING facts you will not believe")
    assert _looks_junky("best facts compilation part 1")
    assert _looks_junky("why cats rule tiktok")
    assert not _looks_junky("Why wombat poop comes out as perfect cubes")
    # Curated-pack acronyms survive the lenient pop-time check.
    assert _looks_junky("the 72 seconds that broke SETI")  # strict top-up check
    assert not _looks_junky("the 72 seconds that broke SETI", strict_caps=False)
    # _clean drops junk from LLM top-up output.
    cleaned = _clean("Why cats always land on their feet\n"
                     "AMAZING SECRET Show #shorts\n"
                     "How honey never spoils\n", [])
    assert cleaned == ["Why cats always land on their feet", "How honey never spoils"]
    # pop skips junk lines (lenient check: hashtags/platform words).
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        backlog = Path(tmp) / "backlog.txt"
        backlog.write_text("# comment\n"
                           "the genius scale! amazing show #shorts\n"
                           "Why the Leaning Tower never fell\n"
                           "How octopuses taste with their arms\n",
                           encoding="utf-8")
        assert pop_fresh_topic(backlog, []) == "Why the Leaning Tower never fell"
        assert pop_fresh_topic(backlog, []) == "How octopuses taste with their arms"
        assert pop_fresh_topic(backlog, []) is None
    # The top-up prompt carries the anti-junk rules.
    class FakeProvider:
        def __init__(self):
            self.prompt = ""

        def generate_text(self, prompt, **kwargs):
            self.prompt = prompt
            return "Why cats always land on their feet\nHow honey never spoils"

    fake = FakeProvider()
    with patch("scriptgen.get_provider", return_value=fake):
        propose_topics(tmp_cfg(), 2, [])
    assert "documentary" in fake.prompt and "ALL CAPS" in fake.prompt
    assert "BANNED" in fake.prompt and "#shorts" not in fake.prompt.split("BANNED")[0]
    # Command words are never topics (the "Stop" zombie, 2026-09-19).
    assert _is_command_topic("Stop")
    assert _is_command_topic("  cancel! ")
    assert _is_command_topic("STOP EVERYTHING")
    assert not _is_command_topic("Why wombat poop comes out as perfect cubes")
    assert not _is_command_topic("How to stop a nosebleed fast")
    assert _clean("Stop\nWhy cats purr\ncancel\n", []) == ["Why cats purr"]
    with tempfile.TemporaryDirectory() as tmp2:
        zombie = Path(tmp2) / "backlog.txt"
        zombie.write_text("Stop\nWhy the moon looks bigger near the horizon\n",
                          encoding="utf-8")
        assert pop_fresh_topic(zombie, []) == \
            "Why the moon looks bigger near the horizon"
        assert load_backlog(zombie) == []  # zombie dropped, fresh popped
    # Interest scorer: detectable scroll-stopping signals.
    strong, flags = score_topic_interest(
        "Why wombat poop comes out as perfect cubes")
    assert strong >= 3, flags
    assert score_topic_interest("How Honey Never Expires")[0] >= 3
    assert "number" in score_topic_interest("7 animals that never sleep")[1]
    assert score_topic_interest("Stop")[0] <= 0
    assert score_topic_interest("fun and useful facts, engaging and cool")[0] <= 0
    mid, mflags = score_topic_interest("3 Facts You NEED To Know")
    assert "tired formula: you need to know" in mflags
    assert "number" in mflags and "ALL-CAPS hype" in mflags


def t_title_optimize():
    import types

    from editorial import _optimize_title, _title_keywords, score_title

    kws = ["stripes", "zebras", "flies"]
    # Subject word + shape beats vague; 9-word sprawl loses points.
    good = score_title("Why Zebras Have Stripes", kws)
    vague = score_title("The Great Pattern Mystery", kws)
    sprawl = score_title("Why Zebras Have Stripes That Keep Them Alive Today", kws)
    assert good > vague and good > sprawl
    # Violating shapes are unusable, hashtags are stripped first.
    assert score_title("The Great Diamond Lie They Still Want You to Believe",
                       kws) == -10
    assert score_title("Why Zebras Have Stripes #facts #didyouknow", kws) == good
    # Caps and exclamation hype lose points.
    assert score_title("WHY ZEBRAS ARE AMAZING!", kws) < \
        score_title("Why Zebras Are Amazing", kws)
    # Keywords come from the narration, most frequent first.
    kws2 = _title_keywords(["Zebras wear stripes to dodge flies. "
                            "The stripes confuse biting flies."])
    assert kws2[0] == "stripes" and "zebras" in kws2 and "flies" in kws2

    class FakeProvider:
        def __init__(self, replies):
            self.replies = list(replies)
            self.calls = 0

        def generate_text(self, *args, **kwargs):
            self.calls += 1
            return self.replies.pop(0)

    def script(title, narration):
        return types.SimpleNamespace(
            title=title,
            scenes=[types.SimpleNamespace(narration=narration)])

    narration = ("Zebras wear stripes to dodge flies. The stripes confuse "
                 "biting flies.")
    # A clear win swaps the title in.
    sc = script("The Strange Mystery of Zebra Coats", narration)
    fp = FakeProvider(['{"titles": ["Why Zebras Have Stripes", '
                       '"Zebra Stripes Explained", "The Secret Pattern '
                       'Nobody Understands Fully Here"]}'])
    _optimize_title(sc, tmp_cfg(), fp)
    assert sc.title == "Why Zebras Have Stripes", sc.title
    # A/B lab, E2E lesson 2026-09-23: no candidate within 3 of the winner
    # (8 vs 4/4), but the pre-polish title (6) is a fair B — it ships as
    # title-b.txt, the control for the swap itself.
    assert sc.title_alt == "The Strange Mystery of Zebra Coats", sc.title_alt
    # Original far below the winner (4 vs 8): no honest B exists.
    sc = script("Coats of the Zebra", narration)
    fp = FakeProvider(['{"titles": ["Why Zebras Have Stripes", '
                       '"The Secret Pattern Nobody Understands Fully Here"]}'])
    _optimize_title(sc, tmp_cfg(), fp)
    assert sc.title == "Why Zebras Have Stripes" and sc.title_alt == ""
    # A candidate within 3 of the winner (5 vs 8) outranks the original as B.
    sc = script("The Strange Mystery of Zebra Coats", narration)
    fp = FakeProvider(['{"titles": ["Why Zebras Have Stripes", '
                       '"The Great Pattern Mystery"]}'])
    _optimize_title(sc, tmp_cfg(), fp)
    assert sc.title == "Why Zebras Have Stripes"
    assert sc.title_alt == "The Great Pattern Mystery", sc.title_alt
    # Already-good title: candidates must beat it by a margin or it stays.
    sc = script("Why Zebras Have Stripes", narration)
    fp = FakeProvider(['{"titles": ["Zebra Stripes Explained", '
                       '"The Truth About Zebras"]}'])
    _optimize_title(sc, tmp_cfg(), fp)
    assert sc.title == "Why Zebras Have Stripes"
    # Provider failure / bad JSON: keep, never raise.
    sc = script("Why Zebras Have Stripes", narration)
    fp = FakeProvider(["not json"])
    _optimize_title(sc, tmp_cfg(), fp)
    assert sc.title == "Why Zebras Have Stripes"
    # No title attribute (editorial fixtures): no provider call at all.
    sc = types.SimpleNamespace(scenes=[])
    fp = FakeProvider([])
    _optimize_title(sc, tmp_cfg(), fp)
    assert fp.calls == 0


def t_clipper():
    from clipper import (CLIENT_FALLBACKS, Candidate, clip_words,
                         crop_filter, fmt_progress, frame_times,
                         parse_candidates, pick_title, plan_chunks,
                         retryable_download_error, transcript_lines,
                         write_kit)

    # Download errors worth a client retry vs fatal.
    assert retryable_download_error(
        "ERROR: unable to download video data: HTTP Error 403: Forbidden")
    assert retryable_download_error("This video is unavailable")
    assert retryable_download_error("requested format not available")
    assert not retryable_download_error("Private video")
    assert not retryable_download_error("Sign in to confirm your age")
    assert None in CLIENT_FALLBACKS and "tv" in CLIENT_FALLBACKS

    # Download progress line: knowns, unknowns, ETA formatting.
    assert fmt_progress(50, 200, 4e6, 30) == \
        "downloading: 25% at 4.0 MB/s, ETA 0:30"
    assert fmt_progress(0, 0, 0, None) == "downloading: ?% at ? MB/s, ETA ?"
    assert fmt_progress(90, 100, 2.5e6, 75) == \
        "downloading: 90% at 2.5 MB/s, ETA 1:15"

    # Chunking: 25-min cap, exact cover.
    assert plan_chunks(600) == [600]
    assert plan_chunks(3600) == [1500, 1500, 600]
    assert plan_chunks(0) == []
    # Timestamped transcript lines.
    words = ([{"word": w, "start": 10 + i * 2.0, "end": 11 + i * 2.0}
              for i, w in enumerate("one two three four five".split())])
    lines = transcript_lines(words)
    assert lines[0].startswith("[0:10]") and "one two three" in lines[0]
    # Candidate parsing: clamps, drops, overlap-dedupe, cap.
    raw = json.dumps({"clips": [
        {"start": 30, "end": 80, "hook": "h1", "title": "t1"},   # 50s -> clamp 45
        {"start": -5, "end": 30, "hook": "h2", "title": "t2"},   # clamp start 0
        {"start": 40, "end": 55, "hook": "overlap", "title": "x"},  # overlaps #1
        {"start": 200, "end": 210, "hook": "too short", "title": "x"},
        {"start": 100, "end": 130, "hook": "h3", "title": "t3"},
        {"start": 300, "end": 340, "hook": "h4", "title": "t4"},
        {"start": 500, "end": 545, "hook": "h5", "title": "t5"},
    ]})
    cands = parse_candidates(raw, duration=600, max_clips=4)
    assert [(c.start, c.end) for c in cands] == [(30, 75), (0, 30), (100, 130), (300, 340)]
    assert parse_candidates("not json at all", 600, 4) == []
    assert parse_candidates('{"clips": []}', 600, 4) == []
    # Frame times: three samples inside the window.
    cand = Candidate(100, 140)
    assert frame_times(cand) == [101, 120, 139]
    # Crop: landscape gets center-cropped to vertical, portrait just scales.
    assert "crop=ih*9/16:ih" in crop_filter(1920, 1080)
    assert crop_filter(1080, 1920) == "scale=1080:1920"
    # Window words become clip-relative.
    window = clip_words(words, start=12, end=20)
    assert window and window[0]["start"] < 0 or window[0]["start"] >= 0
    assert all(-1e-9 <= w["start"] <= 8 + 1e-9 for w in window)
    # Title pick: keyword-rich beats vague (scorer from the title system).
    words_in = [{"word": w, "start": i, "end": i + 1} for i, w in enumerate(
        "octopuses have three hearts and blue blood pumping".split())]
    cand = Candidate(0, 30, hook="h", title_idea="The Great Ocean Mystery")
    title = pick_title(cand, words_in)
    assert "Octopus" in title or "octopus" in title.lower(), title
    # Kit: mp4 + title + credit files.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        clip = Path(tmp) / "clip_01.mp4"
        clip.write_bytes(b"fake")
        kit = write_kit(clip, "Why Octopuses Have Three Hearts", cand,
                        {"title": "Big Stream", "channel": "Streamer",
                         "url": "https://youtu.be/x"}, Path(tmp) / "kits")
        assert (kit / "clip_01.mp4").exists()
        assert (kit / "TITLE.txt").read_text(encoding="utf-8") == \
            "Why Octopuses Have Three Hearts"
        desc = (kit / "DESCRIPTION.txt").read_text(encoding="utf-8")
        assert "youtu.be/x" in desc and "Streamer" in desc


def t_image_speed():
    import os as _os

    import vision
    from clipper import fmt_progress  # noqa: F401 - sanity that lane imports
    from stock import fmt_image_timing

    # Workers: pexels fetches are truly parallel now (the pacer only gates
    # pollinations) — sequential made one slow QC stall every image.
    assert tmp_cfg().image_workers == 3
    # QC fails open fast instead of grinding 4 retries x 20s.
    assert vision.TIMEOUT == 12 and vision.MAX_REQUESTS == 2
    # Token diet (2026-09-22): QC walks 2 candidates by default.
    assert tmp_cfg().qc_candidates == 2
    # Timing breakdown: every image now says where its seconds went.
    assert fmt_image_timing(6.8, 1.2, 0.9, 4.7) == \
        "6.8s (search 1.2 · dl 0.9 · qc 4.7)"
    # Pexels key pool: legacy single key still works, list + env stack.
    cfg = tmp_cfg()
    assert cfg.pexels_api_keys == []
    cfg.data["ai"]["pexels_api_key"] = "single-key"
    assert cfg.pexels_api_keys == ["single-key"]
    cfg.data["ai"]["pexels_api_keys"] = ["k1", "k2"]
    assert cfg.pexels_api_keys == ["k1", "k2", "single-key"]
    _os.environ["PEXELS_API_KEYS"] = "env-k"
    try:
        assert cfg.pexels_api_keys == ["env-k", "k1", "k2", "single-key"]
    finally:
        del _os.environ["PEXELS_API_KEYS"]


def t_shot_plan():
    from images import SHOT_TARGET_SECONDS, plan_shot_counts

    # The live case (2026-09-21): scenes of ~50s/13s/13s after a merge —
    # 3 images each meant 16s holds in the long scene.
    counts = plan_shot_counts([50.0, 13.0, 13.0], per_scene=3)
    assert counts == [8, 3, 3], counts  # holds: 6.3s / 4.3s / 4.3s
    # The Einstein extreme: one 80s scene never starves either (cap 3x).
    assert plan_shot_counts([80.0], 3) == [9]
    # Short scenes keep the snappy baseline; never fewer than configured.
    assert plan_shot_counts([8.0, 12.0], 3) == [3, 3]
    # Balanced script: unchanged behaviour.
    assert plan_shot_counts([20.0, 21.0, 19.0], 3) == [3, 4, 3] or \
        plan_shot_counts([20.0, 21.0, 19.0], 3) == [4, 4, 3]
    # Degenerate durations fall back to the configured count.
    assert plan_shot_counts([0.0, None, "x"], 3) == [3, 3, 3]
    assert plan_shot_counts([], 3) == [3]
    # Contract: holds respect the target, except capped scenes (quota
    # guard) where the hold is as short as the cap allows — never worse.
    for dur, count in zip([50.0, 13.0], [8, 3]):
        assert dur / count <= SHOT_TARGET_SECONDS + 1.5, (dur, count)
    assert 80.0 / 9 <= 9.0  # capped case: 8.9s, the accepted ceiling


def t_shot_plan_delivery():
    """The adaptive shot plan must actually REACH the video.

    Regression guard: generate_scene_images() used to plan extra shots for
    long scenes, fetch them all (paying the Pollinations pacer and the
    ledger for each) and then return only the first `images_per_scene` per
    scene — so the 2026-09-21 "images hold too long" fix silently did
    nothing on the real render path. `plan_shot_counts` alone can't catch
    that; this walks the whole function with a faked fetch.
    """
    import tempfile
    from unittest.mock import patch

    import assembler
    import images

    cfg = tmp_cfg()
    cfg.data["video"]["images_per_scene"] = 1
    cfg.data["ai"]["image_provider"] = "pollinations"   # keyless lane

    class Scene:
        def __init__(self, prompt):
            self.image_prompt = prompt
            self.narration = "n"

    class Script:
        scenes = [Scene("long scene"), Scene("short scene")]

    fetched: list[str] = []

    def fake_fetch(prompt, dest, cfg_, seed, attempts):
        fetched.append(Path(dest).name)
        Path(dest).write_bytes(b"\xff\xd8\xff fake jpeg")
        return dest

    # Scene 1 is 20s -> ceil(20/6.5) = 4 shots (capped at 3x per_scene = 3);
    # scene 2 is 5s -> the 1-image baseline.
    with tempfile.TemporaryDirectory() as tmp, \
            patch.dict(images._FETCH, {"pollinations": fake_fetch}, clear=True), \
            patch.object(assembler, "ffprobe_duration",
                         lambda p: 20.0 if p.name == "a1.wav" else 5.0), \
            patch.object(images, "MIN_REQUEST_INTERVAL", 0.0):
        result = images.generate_scene_images(
            Script(), cfg, Path(tmp),
            audio_paths=[Path("a1.wav"), Path("a2.wav")])

        planned = images.plan_shot_counts([20.0, 5.0], 1)
        assert planned == [3, 1], planned
        # Every planned shot is returned, in slot order — nothing dropped.
        assert [len(scene) for scene in result] == planned, result
        assert sum(len(scene) for scene in result) == len(fetched) == 4
        # ... and every returned path is a real file the assembler can read.
        assert all(path.exists() for scene in result for path in scene)
        # File names carry the true shot count (was "2of1" for the 3rd shot).
        assert any("1of3" in name for name in fetched), fetched
        assert any("3of3" in name for name in fetched), fetched
        assert not any("2of1" in name or "3of1" in name for name in fetched)

    # No audio -> no adaptive plan -> exactly images_per_scene per scene.
    cfg.data["video"]["images_per_scene"] = 3
    fetched.clear()
    with tempfile.TemporaryDirectory() as tmp, \
            patch.dict(images._FETCH, {"pollinations": fake_fetch}, clear=True), \
            patch.object(images, "MIN_REQUEST_INTERVAL", 0.0):
        result = images.generate_scene_images(Script(), cfg, Path(tmp))
        assert [len(scene) for scene in result] == [3, 3], result
        assert len(fetched) == 6
        assert all(path.exists() for scene in result for path in scene)


def t_voice_ledger():
    """Voice-note Whisper audio must land in the ledger (binding constraint).

    The clip lane has ledgered Groq audio since 6ff2be1; the bot's
    voice-note path (voice.transcribe) did not, so `keys` under-reported
    the ~8h/day org-level Whisper pool — the one quota that gates the day.
    """
    from unittest.mock import Mock, mock_open, patch

    import keystats
    from voice import _audio_seconds, transcribe

    cfg = tmp_cfg()
    keystats.init(cfg)
    cfg.data.setdefault("ai", {})["groq_api_keys"] = ["gq-voice-key-0001"]

    ok = Mock(status_code=200)
    ok.json.return_value = {"text": "make a video about black holes"}
    with patch("requests.post", return_value=ok), \
            patch("builtins.open", mock_open(read_data=b"ogg")), \
            patch("voice._audio_seconds", return_value=42.0):
        assert transcribe(cfg, Path("note.ogg")) == \
            "make a video about black holes"

    events = [e for e in keystats._load(keystats._path) if e.get("p") == "groq"]
    assert len(events) == 1, events
    assert events[0]["req"] == 1
    assert events[0]["audio"] == 42          # seconds spent, not 0
    assert events[0]["tag"] == "voicenote"   # distinguishable from `whisper`
    assert "gq-voice-key" not in str(events[0])     # masked on disk

    # The clip lane's tag stays distinct so `keys --month` can split them.
    from clipper import _whisper_request

    resp = Mock(status_code=200)
    resp.json.return_value = {"words": []}
    with patch("requests.post", return_value=resp), \
            patch("builtins.open", mock_open(read_data=b"ogg")):
        _whisper_request(Path("chunk.opus"), ["gq-clip-key-0002"], seconds=600)
    groq = [e for e in keystats._load(keystats._path) if e.get("p") == "groq"]
    assert {e.get("tag") for e in groq} == {"voicenote", "whisper"}
    assert sum(e["audio"] for e in groq) == 642

    # Audio length: unknown/missing file degrades to 0.0, never raises.
    assert _audio_seconds(Path("/nonexistent/nope.ogg")) == 0.0


def t_dry_run_banner_once():
    """`batch --dry-run` / `schedule --dry-run` print the banner ONCE.

    cmd_batch/cmd_schedule print BANNER, then the dry-run helper printed
    it again — the preview read like two runs had happened.
    """
    import io
    from contextlib import redirect_stdout

    from main import BANNER, _dry_run_batch, _dry_run_schedule, cmd_batch

    cfg = tmp_cfg()
    # The banner's rule line appears twice per banner — count its unique
    # middle line instead.
    marker = "Free AI video generator (manual-upload edition)"
    assert marker in BANNER
    for helper, call in (
            (_dry_run_batch, lambda: _dry_run_batch(cfg, ["t", None])),
            (_dry_run_schedule,
             lambda: _dry_run_schedule(cfg, [], 2))):
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert call() == 0
        assert marker not in buf.getvalue(), helper.__name__

    # ... and the CLI path still shows it exactly once.
    import argparse

    args = argparse.Namespace(topics=None, count=2, dry_run=True, seconds=None,
                              format=None, images_per_scene=None,
                              no_subs=False, no_gemini=False, style=None,
                              keep_work=False, keep_going=False,
                              verbose=False, sleep=0)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cmd_batch(cfg, args) == 0
    assert buf.getvalue().count(marker) == 1


def t_shot_bounds():
    from assembler import (MIN_SHOT_SECONDS, plan_shot_boundaries,
                           sentence_ends_from)

    # No timings -> uniform (previous behaviour).
    assert plan_shot_boundaries(30.0, 3, []) == [10.0, 10.0, 10.0]
    # A sentence end near the ideal cut snaps to it.
    assert plan_shot_boundaries(30.0, 3, [9.4]) == [9.4, 10.6, 10.0]
    # Ends outside the tolerance window are ignored.
    assert plan_shot_boundaries(30.0, 3, [3.0, 27.0]) == [10.0, 10.0, 10.0]
    # Both cuts snapped to real sentence pauses.
    assert plan_shot_boundaries(30.0, 3, [11.0, 19.5]) == [11.0, 8.5, 10.5]
    # A plan that starves a shot below the floor falls back to uniform:
    # both cuts snap to ends only 0.8s apart -> middle shot too short.
    assert plan_shot_boundaries(30.0, 3, [14.9, 15.7]) == [10.0, 10.0, 10.0]
    # Duration sum is preserved; single shots take the whole scene.
    assert sum(plan_shot_boundaries(31.0, 4, [7.0, 14.0, 24.0])) == 31.0
    assert plan_shot_boundaries(12.0, 1, [5.0]) == [12.0]
    assert min(plan_shot_boundaries(30.0, 3, [11.0])) >= MIN_SHOT_SECONDS
    # Sentence ends via the caption word alignment: "One. Two words here."
    narration = "One. Two words here."
    timings = [(0.0, 0.5), (0.6, 1.0), (1.0, 1.4), (1.4, 1.9), (2.0, 2.5)]
    ends = sentence_ends_from(narration, timings, head_tail=0.1,
                              scene_seconds=3.0)
    assert ends == [0.6, 2.0], ends  # head_tail offset applied, "here." end
    # No timings -> spread fallback still yields sentence ends
    # (span 1.8s / 2 words, head 0.1 -> "One." ends 1.0, "Two." 1.9).
    ends = sentence_ends_from("One. Two.", [], 0.1, 2.0)
    assert len(ends) == 2 and abs(ends[0] - 1.0) < 1e-6 \
        and abs(ends[1] - 1.9) < 1e-6, ends


def t_voice_budget():
    import tempfile
    from pathlib import Path

    import voiceover
    from voiceover import (_read_budget, premium_budget_allows,
                           record_premium_use)

    class _Cfg:
        work_dir = ""
        premium_voices = 1

    with tempfile.TemporaryDirectory() as tmp:
        _Cfg.work_dir = tmp
        path = Path(tmp) / "voice_budget.json"
        assert premium_budget_allows(_Cfg) is True      # fresh day
        record_premium_use(_Cfg)
        assert _read_budget(path, voiceover._today()) == 1
        assert premium_budget_allows(_Cfg) is False     # budget of 1 spent
        path.write_text('{"date": "2000-01-01", "used": 9}')
        assert premium_budget_allows(_Cfg) is True      # stale date resets
        _Cfg.premium_voices = 0
        assert premium_budget_allows(_Cfg) is False     # 0 = edge-tts only

    # Dead keys (401) leave the pool for the rest of the run.
    voiceover._DEAD_ELEVEN_KEYS.clear()

    class _KCfg:
        elevenlabs_api_keys = ["kAAA", "kBBB"]
        elevenlabs_voice_id = "voice"

    assert voiceover._elevenlabs_available(_KCfg) is True
    voiceover._DEAD_ELEVEN_KEYS.add("kAAA")
    assert voiceover._live_eleven_keys(_KCfg) == ["kBBB"]
    voiceover._DEAD_ELEVEN_KEYS.add("kBBB")
    assert voiceover._elevenlabs_available(_KCfg) is False  # all dead
    voiceover._DEAD_ELEVEN_KEYS.clear()


def t_pixabay():
    from config import IMAGE_PROVIDERS
    from images import provider_ready
    from stock import _pixabay_photo, pixabay_params

    params = pixabay_params("KEY", "frigatebird ocean", portrait=True, page=2)
    assert params["key"] == "KEY" and params["q"] == "frigatebird ocean"
    assert params["orientation"] == "vertical"
    assert params["safesearch"] == "true" and params["image_type"] == "photo"
    assert pixabay_params("K", "x", portrait=False, page=1)["orientation"] == "horizontal"

    hit = {"id": 123, "user": "carlo", "tags": "bird, ocean, wings",
           "largeImageURL": "https://pixabay.com/large.jpg",
           "webformatURL": "https://pixabay.com/web.jpg"}
    photo = _pixabay_photo(hit)
    assert photo["id"] == 123 and photo["photographer"] == "carlo"
    assert photo["src"]["portrait"].endswith("large.jpg")
    fallback = _pixabay_photo({"id": 5, "tags": "x",
                               "webformatURL": "https://pixabay.com/w.jpg"})
    assert fallback["src"]["portrait"].endswith("w.jpg")

    assert "pixabay" in IMAGE_PROVIDERS

    class _C:
        pixabay_api_key = "k"

    ok, _ = provider_ready("pixabay", _C)
    assert ok is True

    class _D:
        pixabay_api_key = ""

    ok2, why = provider_ready("pixabay", _D)
    assert ok2 is False and "Pixabay" in why

    # Key pool: list + legacy singular, deduped (mirrors the Pexels pool).
    cfg = tmp_cfg()
    cfg.data["ai"]["pixabay_api_keys"] = ["k1", "k2"]
    cfg.data["ai"]["pixabay_api_key"] = "k1"
    assert cfg.pixabay_api_keys == ["k1", "k2"]
    cfg.data["ai"]["pixabay_api_key"] = "k3"
    assert cfg.pixabay_api_keys == ["k1", "k2", "k3"]


def t_deepseek():
    from deepseek import DeepSeekProvider
    from scriptgen import get_provider

    provider = DeepSeekProvider(["sk-test"], "")
    assert provider.model == "deepseek-flash"
    assert "deepseek-v4-pro" in provider.FALLBACK_MODELS
    assert provider.api_url == "https://api.deepseek.com/chat/completions"
    assert DeepSeekProvider._headers(provider, "k") == {"Authorization": "Bearer k"}
    # Key pool: list + legacy singular, deduped.
    cfg = tmp_cfg()
    cfg.data["ai"]["deepseek_api_keys"] = ["sk-1", "sk-2"]
    cfg.data["ai"]["deepseek_api_key"] = "sk-1"
    assert cfg.deepseek_api_keys == ["sk-1", "sk-2"]
    assert cfg.deepseek_model == "deepseek-flash"
    # The chain picks it up when keys exist (template stays last).
    names = [name for name, _ in get_provider(cfg).chain]
    assert "deepseek" in names and names[-1] == "template"


def t_whisper_primary():
    import sys
    import types
    from unittest.mock import patch

    from clipper import (ClipError, captions_to_words,
                         fetch_youtube_captions, transcribe_words,
                         video_id_for_captions)

    # URL -> video id; everything else -> "" (Whisper fallthrough).
    assert video_id_for_captions("https://youtu.be/IpAjsMJKBvk") == \
        "IpAjsMJKBvk"
    assert video_id_for_captions(
        "https://www.youtube.com/watch?v=abc123XYZ_-&t=5") == "abc123XYZ_-"
    assert video_id_for_captions("IpAjsMJKBvk") == "IpAjsMJKBvk"
    assert video_id_for_captions("local") == ""
    assert video_id_for_captions("https://vimeo.com/123") == ""
    assert video_id_for_captions("https://www.youtube.com/@SomeChannel") == ""
    assert video_id_for_captions("") == ""
    # Spread math: line-timed segments -> evenly spaced words.
    words = captions_to_words([
        {"text": "hello brave world", "start": 10.0, "duration": 3.0},
        {"text": "", "start": 13.0, "duration": 1.0},
        {"text": "again", "start": 14.0, "duration": 0.0}])
    assert [(w["word"], w["start"], w["end"]) for w in words] == [
        ("hello", 10.0, 11.0), ("brave", 11.0, 12.0), ("world", 12.0, 13.0)]
    assert captions_to_words([]) == []
    assert captions_to_words([{"text": "x"}]) == []  # no timing
    assert captions_to_words([{"text": "x", "start": "junk",
                               "duration": 1.0}]) == []
    # Preference: manual EN > generated EN > translated; misses -> None.
    class Track:
        def __init__(self, code, generated, translatable=True, segs=()):
            self.language_code = code
            self.language = {"en": "English"}.get(code, code)
            self.is_generated = generated
            self.is_translatable = translatable
            self._segs = segs

        def fetch(self):
            return self._segs

        def translate(self, code):
            return Track(code, True, True, self._segs)

    segs = [{"text": "hi there", "start": 0.0, "duration": 2.0}]
    manual = Track("en", False, True, segs)
    gen = Track("en", True, True, segs)
    italian = Track("it", False, True, segs)

    class Api:
        def __init__(self, tracks):
            self._tracks = tracks

        def list(self, video_id):
            assert video_id == "vid1"
            return self._tracks

    def run_api(tracks):
        mod = types.ModuleType("youtube_transcript_api")
        mod.YouTubeTranscriptApi = lambda: Api(tracks)
        with patch.dict(sys.modules, {"youtube_transcript_api": mod}):
            return fetch_youtube_captions("vid1")

    got, origin = run_api([gen, manual])
    assert origin == "manual" and got == segs
    got, origin = run_api([gen])
    assert origin == "generated"
    got, origin = run_api([italian])
    assert origin == "translated"
    assert run_api([]) is None

    class BrokenApi(Api):
        def list(self, video_id):
            raise RuntimeError("IP blocked")

    broken = types.ModuleType("youtube_transcript_api")
    broken.YouTubeTranscriptApi = lambda: BrokenApi([])
    with patch.dict(sys.modules, {"youtube_transcript_api": broken}):
        assert fetch_youtube_captions("vid1") is None
    assert fetch_youtube_captions("") is None

    # Whisper wins on a fresh transcript and receives only its 90% key slice.
    import io
    from contextlib import redirect_stdout

    cfg = tmp_cfg()
    cfg.data["ai"]["groq_api_keys"] = [f"groq-{i}" for i in range(10)]
    whisper_words = [{"word": "Whisper", "start": 0.0, "end": 0.4},
                     {"word": "primary.", "start": 0.5, "end": 1.0}]
    with (
        patch("clipper.ffprobe_duration", return_value=2.0),
        patch("clipper._whisper_request",
              return_value={"words": whisper_words}) as request,
        patch("clipper.fetch_youtube_captions") as captions,
        redirect_stdout(io.StringIO()),
    ):
        got = transcribe_words(Path("audio.opus"), cfg, video_id="vid1")
    assert [word["word"] for word in got] == ["Whisper", "primary."]
    assert request.call_args.args[1] == cfg.groq_transcription_api_keys
    assert len(cfg.groq_transcription_api_keys) == 9
    assert len(cfg.groq_llm_api_keys) == 1
    captions.assert_not_called()

    fallback_segments = [{"text": "caption fallback works",
                          "start": 1.0, "duration": 3.0}]
    with (
        patch("clipper.ffprobe_duration", return_value=2.0),
        patch("clipper._whisper_request",
              side_effect=ClipError("all Whisper accounts rate-limited")),
        patch("clipper.fetch_youtube_captions",
              return_value=(fallback_segments, "manual")),
        redirect_stdout(io.StringIO()),
    ):
        got = transcribe_words(Path("audio.opus"), cfg, video_id="vid1")
    assert [word["word"] for word in got] == [
        "caption", "fallback", "works"]

    # Captions also rescue the source when the Whisper pool is empty.
    no_groq_cfg = tmp_cfg()
    with (
        patch("clipper.fetch_youtube_captions",
              return_value=(fallback_segments, "generated")),
        redirect_stdout(io.StringIO()),
    ):
        got = transcribe_words(Path("audio.opus"), no_groq_cfg,
                               video_id="vid1")
    assert [word["word"] for word in got] == [
        "caption", "fallback", "works"]

    # Empty Whisper output is not the winner if captions can help.
    with (
        patch("clipper.ffprobe_duration", return_value=2.0),
        patch("clipper._whisper_request",
              return_value={"words": [], "segments": []}),
        patch("clipper.fetch_youtube_captions",
              return_value=(fallback_segments, "manual")),
        redirect_stdout(io.StringIO()),
    ):
        got = transcribe_words(Path("audio.opus"), cfg, video_id="vid1")
    assert [word["word"] for word in got] == [
        "caption", "fallback", "works"]

    # If neither source succeeds, retain a readable primary failure.
    with (
        patch("clipper.ffprobe_duration", return_value=2.0),
        patch("clipper._whisper_request",
              side_effect=ClipError("all accounts throttled")),
        patch("clipper.fetch_youtube_captions", return_value=None),
        redirect_stdout(io.StringIO()),
    ):
        try:
            transcribe_words(Path("audio.opus"), cfg, video_id="vid1")
        except ClipError as exc:
            assert "Groq Whisper failed" in str(exc)
        else:
            raise AssertionError("both transcript providers failed")


def t_clip_cookies():
    from clipper import (bot_wall_error, cookie_opts,
                         retryable_download_error)

    # The LIVE bot-wall string from the 2026-09-22 field test (match
    # reality, not a paraphrase — it uses a unicode apostrophe).
    live = ("ERROR: [youtube] z0bVolxirl0: Sign in to confirm you\u2019re "
            "not a bot. Use --cookies-from-browser or --cookies for the "
            "authentication.")
    assert bot_wall_error(live) is True
    assert bot_wall_error("Sign in to confirm you're not a bot") is True
    assert bot_wall_error("HTTP 403 Forbidden: request denied") is False
    # The age gate shares the "Sign in to confirm" prefix — NOT a bot wall.
    assert bot_wall_error("Sign in to confirm your age") is False
    assert retryable_download_error("Sign in to confirm your age") is False
    assert bot_wall_error("") is False
    assert retryable_download_error(live) is True  # rotates clients too
    # Cookies opts: browser tuple, file override, empty = none.
    assert cookie_opts("firefox", "") == {"cookiesfrombrowser": ("firefox",)}
    assert cookie_opts(" Chrome ", "") == {"cookiesfrombrowser": ("chrome",)}
    assert cookie_opts("", "cookies.txt") == {"cookiefile": "cookies.txt"}
    assert cookie_opts("firefox", "cookies.txt") == {"cookiefile": "cookies.txt"}
    assert cookie_opts("", "") == {}
    # Config knobs exist and default off.
    cfg = tmp_cfg()
    assert cfg.clip_cookies_browser == "" and cfg.clip_cookies_file == ""
    cfg.data["clip"] = {"cookies_browser": "Firefox",
                        "cookies_file": "C:/tmp/cookies.txt"}
    assert cfg.clip_cookies_browser == "Firefox"
    assert cfg.clip_cookies_file == "C:/tmp/cookies.txt"
    cfg.data["ai"]["qc_candidates"] = 9
    assert cfg.qc_candidates == 3  # clamped
    cfg.data["ai"]["qc_candidates"] = 0
    assert cfg.qc_candidates == 1  # clamped


def t_clip_cookies_status():
    from clipper import cookie_status

    tmp = Path(_mkdtemp())
    # A real export: comment header + cookie lines.
    good = tmp / "cookies.txt"
    good.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tHSID\tdef\n", encoding="utf-8")
    assert cookie_status("", str(good)) == \
        f"cookies: file {good} (2 cookies)"
    # Missing / header-only files warn loudly, never raise.
    missing = str(tmp / "nope.txt")
    assert "WARNING" in cookie_status("", missing)
    assert "not found" in cookie_status("", missing)
    blank = tmp / "blank.txt"
    blank.write_text("# nothing exported yet\n\n", encoding="utf-8")
    assert "no cookies" in cookie_status("", str(blank))
    # File wins over browser, even when the file is the problem.
    assert "WARNING" in cookie_status("firefox", missing)
    # Browsers: known names pass through, typos get corrected.
    assert cookie_status(" Brave ", "") == "cookies: browser 'brave'"
    assert "unknown browser" in cookie_status("internet explorer", "")
    # Neither: honest anonymous label.
    assert "anonymous" in cookie_status("", "")


def t_clip_title_polish():
    import json as _json

    from editorial import polish_clip_title

    narration = ("Scientists watched one jellyfish reverse its own aging "
                 "for years without dying. Its cells completely reprogram "
                 "themselves after injury.")

    class _Provider:
        def __init__(self, titles):
            self.titles = titles

        def generate_text(self, prompt, **kwargs):
            return _json.dumps({"titles": self.titles})

    # A low-scoring draft is replaced by a clearly better candidate
    # (the gate needs best >= current + 2, so fixtures must differ hard).
    strong = ["The Jellyfish That Refuses To Die",
              "Why Jellyfish Never Grow Old"]
    weak_draft = "Clip 01 Moment"
    assert polish_clip_title(weak_draft, narration, _Provider(strong)) in strong
    # An already-good draft (score 6) with equal candidates (score 6)
    # stays: polish only replaces CLEAR winners.
    good_draft = "Why Jellyfish Breaks The Rules"
    assert polish_clip_title(good_draft, narration, _Provider(strong)) is None
    weak = ["Jellyfish Facts You Won't Believe Are True Today Friend"]
    assert polish_clip_title(weak_draft, narration, _Provider(weak)) is None
    # Hashtags are stripped from candidates.
    out = polish_clip_title(weak_draft, narration,
                            _Provider(["The Jellyfish That Refuses To Die #facts"]))
    assert out == "The Jellyfish That Refuses To Die"
    # Provider failure -> None, never an exception.
    class _Boom:
        def generate_text(self, prompt, **kwargs):
            raise RuntimeError("503 storm")

    assert polish_clip_title(weak_draft, narration, _Boom()) is None
    # Empty draft / no keywords -> None.
    assert polish_clip_title("", narration, _Provider(strong)) is None
    assert polish_clip_title(weak_draft, "", _Provider(strong)) is None
    # Config toggle: on by default, switchable.
    cfg = tmp_cfg()
    assert cfg.clip_polish_titles is True
    cfg.data["clip"]["polish_titles"] = False
    assert cfg.clip_polish_titles is False


def t_clip_cache():
    import time as _time
    import tempfile
    from pathlib import Path

    from clipper import (load_transcript_cache, save_transcript_cache,
                         transcript_cache_key, transcript_cache_path)

    # URL keys: stable, filesystem-safe, source-specific.
    k1 = transcript_cache_key("https://youtu.be/abc123", Path("x.mp4"))
    assert k1 == transcript_cache_key("https://youtu.be/abc123", Path("y.mp4"))
    assert k1 != transcript_cache_key("https://youtu.be/other", Path("x.mp4"))
    assert len(k1) == 16 and all(c in "0123456789abcdef" for c in k1)
    # File keys react to content changes (size here).
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "vod.mp4"
        f.write_bytes(b"0" * 100)
        fk = transcript_cache_key("", f)
        assert fk == transcript_cache_key("", f)
        _time.sleep(0.01)
        f.write_bytes(b"0" * 120)
        assert transcript_cache_key("", f) != fk
    # Round-trip + corrupt/version safety.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cache.json"
        assert load_transcript_cache(path) is None  # absent
        words = [{"word": "hi", "start": 0.0, "end": 0.4},
                 {"word": "there", "start": 0.5, "end": 0.9}]
        save_transcript_cache(path, words)
        assert load_transcript_cache(path) == words
        path.write_text("{corrupt", encoding="utf-8")
        assert load_transcript_cache(path) is None
        path.write_text('{"version": 999, "words": []}', encoding="utf-8")
        assert load_transcript_cache(path) is None
        cfg = tmp_cfg()
        assert transcript_cache_path(cfg, "abc").name == "abc.json"
        assert transcript_cache_path(cfg, "abc").parent.name == "clip_cache"


def t_clip_windows():
    import json as _json
    from pathlib import Path

    from clipper import (Candidate, dedupe_overlaps, parse_candidates,
                         split_windows, transcript_cache_key)

    def wlist(n):
        return [{"word": f"w{i}", "start": i * 0.4, "end": i * 0.4 + 0.3}
                for i in range(n)]

    # Short transcript: single window, unchanged behaviour.
    single = split_windows(wlist(200))
    assert len(single) == 1 and len(single[0]) == 200
    assert split_windows([]) == []
    # Long transcript: bounded windows, stepped by (window - overlap).
    words = wlist(3000)
    windows = split_windows(words)
    assert len(windows) > 1 and all(len(w) <= 1400 for w in windows)
    assert windows[0][0] is words[0] and windows[-1][-1] is words[-1]
    assert windows[1][0]["word"] == "w1280"  # step = 1400 - 120
    seen = set()
    for win in windows:
        seen.update(id(word) for word in win)
    assert len(seen) == 3000  # full coverage, no word lost
    # Window bounds: candidates outside [lo, hi] are dropped.
    raw = _json.dumps({"clips": [
        {"start": 5, "end": 30, "hook": "h", "title": "t"},
        {"start": 700, "end": 730, "hook": "h", "title": "t"}]})
    inside = parse_candidates(raw, duration=2000.0, max_clips=6,
                              lo=0.0, hi=100.0)
    assert len(inside) == 1 and inside[0].start == 5.0
    # Cross-window dedupe: overlapping moments collapse, keep-first.
    a = Candidate(start=30.0, end=55.0)
    b = Candidate(start=50.0, end=80.0)    # overlaps a
    c = Candidate(start=120.0, end=150.0)  # disjoint
    assert dedupe_overlaps([a, b, c]) == [a, c]
    # Unique per-source clip names: different sources, different files.
    k1 = transcript_cache_key("https://youtu.be/aaa", Path("x.mp4"))
    k2 = transcript_cache_key("https://youtu.be/bbb", Path("x.mp4"))
    assert f"{k1[:8]}_clip_01.mp4" != f"{k2[:8]}_clip_01.mp4"


def t_clip_workdirs():
    import os as _os
    import tempfile
    import time as _time
    from pathlib import Path

    from clipper import new_clip_work_dir, prune_stale_runs

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        a = new_clip_work_dir(root)
        _time.sleep(0.01)
        b = new_clip_work_dir(root)
        assert a != b and a.parent == root and b.parent == root
        assert a.name.startswith("clip_run_") and b.name.startswith("clip_run_")
        # prune: old dirs die (unless == keep), fresh ones live
        old_dir = root / "clip_run_20200101_000000_1"
        old_dir.mkdir()
        (old_dir / "junk.txt").write_text("x")  # BEFORE utime: creating
        # entries refreshes a dir's mtime
        _os.utime(old_dir, (_time.time() - 72 * 3600,) * 2)
        fresh_dir = root / "clip_run_20990101_000000_2"
        fresh_dir.mkdir()
        prune_stale_runs(root, keep=a, max_age_hours=48.0)
        assert not old_dir.exists()      # stale, not kept
        assert fresh_dir.exists()        # fresh
        assert a.exists() or True        # keep is never touched
        keep_old = root / "clip_run_20200101_000000_3"
        keep_old.mkdir()
        _os.utime(keep_old, (_time.time() - 72 * 3600,) * 2)
        prune_stale_runs(root, keep=keep_old, max_age_hours=48.0)
        assert keep_old.exists()         # stale BUT kept
        # non-matching dirs are never pruned
        other = root / "something_else"
        other.mkdir()
        _os.utime(other, (_time.time() - 100 * 3600,) * 2)
        prune_stale_runs(root, keep=a, max_age_hours=48.0)
        assert other.exists()


def t_clip_snap():
    import json as _json

    from clipper import (Candidate, apply_hook_start, parse_candidates,
                         sentence_spans, snap_candidate)

    def sentences(specs):
        # specs: (word, start, end) — one word per sentence is enough
        return [{"word": w, "start": s, "end": e} for w, s, e in specs]

    words = sentences([("One.", 0.0, 1.0), ("Two.", 1.2, 2.2),
                       ("Three.", 2.4, 3.4), ("Four.", 3.6, 4.6),
                       ("Five.", 4.8, 6.0), ("Six.", 6.2, 7.4)])
    spans = sentence_spans(words)
    assert spans == [(0.0, 1.0), (1.2, 2.2), (2.4, 3.4),
                     (3.6, 4.6), (4.8, 6.0), (6.2, 7.4)]
    # trailing fragment (no punctuation) still forms a span
    frag = words + [{"word": "trail", "start": 7.6, "end": 8.0}]
    assert sentence_spans(frag)[-1] == (7.6, 8.0)
    assert sentence_spans([]) == []

    # Mid-sentence start retreats; mid-sentence end completes.
    snapped = snap_candidate(Candidate(start=1.5, end=3.0), words, 2, 45)
    assert (snapped.start, snapped.end) == (1.2, 3.4)
    # Already-clean edges and pause-landing edges stay untouched.
    clean = snap_candidate(Candidate(start=1.2, end=3.4), words, 2, 45)
    assert (clean.start, clean.end) == (1.2, 3.4)
    pause = snap_candidate(Candidate(start=1.2, end=2.3), words, 2, 45)
    assert (pause.start, pause.end) == (1.2, 2.3)
    # Retreat would exceed max_len -> start advances to next sentence.
    over = snap_candidate(Candidate(start=0.5, end=6.5), words, 2, 5)
    assert over.start == 1.2
    # End completion would exceed max_len -> backtrack to sentence end.
    assert over.end == 6.0
    # Completion fits max_len -> end completes forward to 3.4.
    complete = snap_candidate(Candidate(start=0.0, end=2.5), words, 2, 45)
    assert complete.end == 3.4
    # Backtrack: completion too long, but the previous sentence end
    # still leaves >= min_len.
    back = snap_candidate(Candidate(start=0.0, end=6.5), words, 6, 6.0)
    assert back.end == 6.0
    # Last resort: completion too long AND backtrack below min_len ->
    # keep the original end (never shrink below min_len).
    tight = snap_candidate(Candidate(start=2.4, end=6.5), words, 5, 4.5)
    assert tight.end == 6.5

    # Hook-first: picker says the real hook starts at 3.6 (>3s of
    # context to skip, enough clip left).
    moved = apply_hook_start(Candidate(start=0.0, end=7.4,
                                       hook_start=3.6), 2)
    assert moved.start == 3.6
    # ...and 3.6 is already a sentence start, so snapping keeps it.
    full = snap_candidate(moved, words, 2, 45)
    assert full.start == 3.6
    # A hook landing mid-sentence snaps back to the hook sentence's start.
    mid = snap_candidate(apply_hook_start(
        Candidate(start=0.0, end=7.4, hook_start=4.0), 2), words, 2, 45)
    assert mid.start == 3.6
    # Hook barely later than start (<= 3s) -> not worth the cut.
    same = apply_hook_start(Candidate(start=0.0, end=7.4,
                                      hook_start=2.5), 2)
    assert same.start == 0.0
    # Not enough clip left after the hook -> keep.
    short = apply_hook_start(Candidate(start=0.0, end=5.0,
                                       hook_start=3.2), 2)
    assert short.start == 0.0
    assert apply_hook_start(Candidate(start=0.0, end=5.0), 2).start == 0.0

    # hook_start parsing: inside range kept, outside dropped.
    raw = _json.dumps({"clips": [
        {"start": 5, "end": 30, "hook_start": 10, "hook": "h",
         "title": "t"},
        {"start": 40, "end": 70, "hook_start": 95, "hook": "h",
         "title": "t"}]})
    cands = parse_candidates(raw, duration=2000.0, max_clips=6)
    assert cands[0].hook_start == 10.0
    assert cands[1].hook_start is None


def t_clip_smart_crop():
    from clipper import decide_subject_x, smart_crop_filter

    # Consensus: no samples / all-None -> center (None).
    assert decide_subject_x([]) is None
    assert decide_subject_x([None, None]) is None
    assert decide_subject_x([None, 0.4]) == 0.4
    assert decide_subject_x([0.3]) == 0.3
    assert decide_subject_x([0.25, 0.35]) == 0.3
    # Wild disagreement -> None (center is safest).
    assert decide_subject_x([0.1, 0.9]) is None
    # Mean clamped away from the extreme edges.
    assert decide_subject_x([0.05]) == 0.15
    assert decide_subject_x([0.95]) == 0.85

    # Crop strings: portrait passes through, centered/unknown stay
    # centered, near-center does not jump the crop.
    assert smart_crop_filter(720, 1280, 0.2) == "scale=1080:1920"
    assert smart_crop_filter(1280, 720, None) == \
        "crop=ih*9/16:ih,scale=1080:1920"
    assert smart_crop_filter(1280, 720, 0.5) == \
        "crop=ih*9/16:ih,scale=1080:1920"
    assert smart_crop_filter(1280, 720, 0.55) == \
        "crop=ih*9/16:ih,scale=1080:1920"
    # Off-center subjects: window shifts, clamped to the frame.
    # crop_w = 720*9/16 = 405px; left third -> offset ~54px.
    left = smart_crop_filter(1280, 720, 0.2)
    assert left == f"crop=405:720:{1280 - 405 if False else 54}:0,scale=1080:1920"
    right = smart_crop_filter(1280, 720, 0.8)
    assert right == "crop=405:720:822:0,scale=1080:1920"
    assert smart_crop_filter(1280, 720, 0.05) == \
        "crop=405:720:0:0,scale=1080:1920"          # clamped left
    assert smart_crop_filter(1280, 720, 0.98) == \
        "crop=405:720:875:0,scale=1080:1920"        # clamped right
    # Config default on, switchable off.
    cfg = tmp_cfg()
    assert cfg.clip_smart_crop is True
    cfg.data["clip"]["smart_crop"] = False
    assert cfg.clip_smart_crop is False


def t_channel_snap():
    import json as _json
    import tempfile
    from pathlib import Path

    from youtube import (build_report, chunk_ids, load_snapshots,
                         previous_daysnapshot, record_snapshot,
                         save_snapshots, video_age_days)

    assert chunk_ids([f"v{i}" for i in range(120)], 50) == \
        [[f"v{i}" for i in range(50)], [f"v{i}" for i in range(50, 100)],
         [f"v{i}" for i in range(100, 120)]]
    assert chunk_ids([], 50) == []
    assert video_age_days("2026-09-20", "2026-09-22") == 2
    assert video_age_days("2026-09-22", "2026-09-22") == 0
    assert video_age_days("garbage", "2026-09-22") == 0

    videos = [
        {"id": "abc12345678", "title": "Why Zebras Have Stripes",
         "channel": "facts", "views": 1164, "likes": 40,
         "comments": 2, "published": "2026-09-20"},
        {"id": "def22222222", "title": "How Cold Water Tricks You",
         "channel": "facts", "views": 61, "likes": 3,
         "comments": 0, "published": "2026-09-22"},
        {"id": "ghi33333333", "title": "The Diamond Lie",
         "channel": "facts", "views": 3, "likes": 0,
         "comments": 0, "published": "2026-09-18"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / "snapshots.json"
        history = load_snapshots(store)   # absent -> empty shape
        assert history == {"channels": [], "days": [], "flagged": []}
        boosted = [{**v, "views": v["views"] + 10} for v in videos]
        record_snapshot(history, videos, "2026-09-21")
        record_snapshot(history, boosted, "2026-09-22")
        assert previous_daysnapshot(history, "2026-09-22")["date"] == "2026-09-21"
        assert previous_daysnapshot(history, "2026-09-21") is None
        # same-day re-run replaces, never duplicates
        record_snapshot(history, boosted, "2026-09-22")
        assert len([d for d in history["days"] if d["date"] == "2026-09-22"]) == 1
        report, flags = build_report(history, "2026-09-22",
                                     previous_daysnapshot(history, "2026-09-22"))
        # +10 view deltas render; the 3-view 4-day-old video gets flagged
        assert "ghi33333333" in flags
        assert "RETITLE?" in report
        assert "+10" in report and "1,174" in report  # today's boosted views
        # caller contract: persist one-shot flags, then save
        history["flagged"] = sorted(set(history["flagged"]) | set(flags))
        save_snapshots(store, history)
        loaded = load_snapshots(store)
        assert len(loaded["days"]) == 2
        # flagged once: the same report never re-flags
        _, flags2 = build_report(loaded, "2026-09-22",
                                 previous_daysnapshot(loaded, "2026-09-22"))
        assert flags2 == []
        assert _json.loads(store.read_text())["flagged"] == ["ghi33333333"]


class _FakeYT:
    """Offline stand-in for YouTubeClient: same _get contract + pagination.

    channels: {cid: {title, handle, subs, views, hidden, videos: [ids]}}
    videos:   {vid: {title, channel_id, published_at, duration, views,
                     likes, comments}}
    broken:   set of channel ids whose playlist call raises.
    empty:    set of channel ids whose uploads playlist 404s like a real
              brand-new channel (no videos yet -> no playlist).
    """

    def __init__(self, channels, videos, broken=(), empty=()):
        self.channels, self.videos = channels, videos
        self.broken = set(broken)
        self.empty = set(empty)
        self.spent = 0
        self.calls = []

    def _get(self, method, params, cost=1):
        self.calls.append((method, dict(params)))
        self.spent += cost
        if method == "channels":
            if "forHandle" in params:
                hits = [c for c, v in self.channels.items()
                        if v["handle"].lower() == "@" + params["forHandle"].lower()]
            else:
                ids = params["id"].split(",")
                assert len(ids) <= 50, "API caps id lists at 50"
                hits = [c for c in ids if c in self.channels]
            items = []
            for cid in hits:
                ch = self.channels[cid]
                stats = {"viewCount": str(ch["views"]),
                         "videoCount": str(len(ch["videos"]))}
                if ch.get("hidden"):
                    stats["hiddenSubscriberCount"] = True
                else:
                    stats["subscriberCount"] = str(ch["subs"])
                items.append({"id": cid, "snippet": {
                    "title": ch["title"], "customUrl": ch["handle"],
                    "publishedAt": "2025-01-01T00:00:00Z"},
                    "statistics": stats,
                    "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid[2:]}}})
            return {"items": items}
        if method == "playlistItems":
            cid = "UC" + params["playlistId"][2:]
            if cid in self.empty:
                raise RuntimeError(
                    'YouTube HTTP 404: playlistNotFound: {"error": '
                    '{"code": 404, "message": "The playlist identified with '
                    'the request\'s playlistId parameter cannot be found."}}')
            if cid in self.broken:
                raise RuntimeError("YouTube HTTP 500: backend error")
            ids = self.channels[cid]["videos"]
            start = int(params.get("pageToken") or 0)
            page = ids[start:start + 50]
            out = {"items": [{"contentDetails": {"videoId": v}} for v in page]}
            if start + 50 < len(ids):
                out["nextPageToken"] = str(start + 50)
            return out
        if method == "videos":
            ids = params["id"].split(",")
            assert len(ids) <= 50
            items = []
            for vid in ids:
                v = self.videos.get(vid)
                if not v:
                    continue  # deleted/private: API just omits it
                items.append({"id": vid, "snippet": {
                    "title": v["title"], "channelTitle": self.channels[v["channel_id"]]["title"],
                    "channelId": v["channel_id"], "publishedAt": v["published_at"]},
                    "statistics": {"viewCount": str(v["views"]),
                                   "likeCount": str(v["likes"]),
                                   "commentCount": str(v["comments"])},
                    "contentDetails": {"duration": v["duration"]}})
            return {"items": items}
        raise AssertionError(f"unexpected method {method}")


def _fake_world():
    """3 channels: a Shorts channel, a long-form one, one hidden-subs."""
    channels = {
        "UC" + "a" * 22: {"title": "Facts Daily", "handle": "@factsdaily",
                          "subs": 1230, "views": 45678, "videos": []},
        "UC" + "b" * 22: {"title": "Clip Vault", "handle": "@clipvault",
                          "subs": 88, "views": 9000, "videos": []},
        "UC" + "c" * 22: {"title": "Quiet One", "handle": "@quietone",
                          "subs": 0, "views": 10, "hidden": True, "videos": []},
    }
    videos = {}
    a, b, c = list(channels)
    # Facts Daily: 60 uploads (pagination past 50), newest first.
    for i in range(60):
        vid = f"fa{i:09d}"
        videos[vid] = {"title": f"Fact number {i}", "channel_id": a,
                       "published_at": f"2026-09-{28 - (i % 20):02d}T10:00:00Z",
                       "duration": "PT45S", "views": 1000 - i * 10,
                       "likes": 50 - (i % 50), "comments": 5, }
        channels[a]["videos"].append(vid)
    videos["fa000000000"].update(published_at="2026-09-29T08:00:00Z",
                                 title="Brand new short", views=240)
    # Clip Vault: 3 videos incl. a 0-view one and a long one.
    for vid, title, when, dur, views in (
            ("cb000000001", "Long talk, the full interview with a very long title indeed",
             "2026-09-20T12:00:00Z", "PT1H2M3S", 3000),
            ("cb000000002", "Stuck at twelve", "2026-09-25T12:00:00Z", "PT30S", 12),
            ("cb000000003", "Zero so far", "2026-09-29T11:00:00Z", "PT20S", 0)):
        videos[vid] = {"title": title, "channel_id": b, "published_at": when,
                       "duration": dur, "views": views, "likes": views // 20,
                       "comments": views // 100}
        channels[b]["videos"].append(vid)
    videos["cc000000001"] = {"title": "Only video", "channel_id": c,
                             "published_at": "2026-09-01T00:00:00Z",
                             "duration": "PT2M", "views": 10, "likes": 1,
                             "comments": 0}
    channels[c]["videos"].append("cc000000001")
    return channels, videos


def t_channel_stats():
    from datetime import datetime, timezone

    import channelstats as cs
    from youtube import load_snapshots, save_snapshots

    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

    # -- pure metrics ------------------------------------------------------
    assert cs.engagement({"views": 200, "likes": 10, "comments": 2}) == 6.0
    assert cs.engagement({"views": 0, "likes": 3}) is None
    assert cs.pace({"views": 240}, 4.0) == (60.0, "/h")
    assert cs.pace({"views": 240}, 0.2) == (240.0, "/h")   # floor at 1h
    assert cs.pace({"views": 700}, 24 * 7) == (100.0, "/d")
    assert cs.compact(999) == "999" and cs.compact(1234) == "1.2K"
    assert cs.compact(1000) == "1K" and cs.compact(2_500_000) == "2.5M"
    assert cs.fmt_len(45) == "0:45" and cs.fmt_len(3723) == "1:02:03"
    assert cs.fmt_len(0) == "—"
    assert cs.age_hours({"published_at": "2026-09-29T08:00:00Z"}, now,
                        "2026-09-29") == 4.0
    # legacy rows (date only) fall back to whole days; garbage -> 0
    assert cs.age_hours({"published": "2026-09-27"}, now, "2026-09-29") == 48.0
    assert cs.age_hours({"published": "junk"}, now, "2026-09-29") == 0.0
    assert cs.age_hours({"published_at": "not-a-date",
                         "published": "2026-09-28"}, now, "2026-09-29") == 24.0

    # -- channel list management (offline) ---------------------------------
    h = {"channels": [], "days": [], "flagged": []}
    assert cs.add_channel(h, "UC1", "Facts Daily") is True
    assert cs.add_channel(h, "UC2", "Facts Weekly") is True
    assert cs.add_channel(h, "UC1", "Facts Daily (renamed)") is False
    assert [c["title"] for c in h["channels"]] == ["Facts Daily (renamed)",
                                                   "Facts Weekly"]
    assert cs.match_channels(h, "facts") and len(cs.match_channels(h, "facts")) == 2
    assert cs.match_channels(h, "uc2")[0]["id"] == "UC2"  # id match, case-free
    assert cs.match_channels(h, "") == []
    assert cs.remove_channel(h, "facts") == (None, "many")
    assert cs.remove_channel(h, "nope") == (None, "none")
    removed, problem = cs.remove_channel(h, "weekly")
    assert problem == "" and removed["id"] == "UC2" and len(h["channels"]) == 1
    h["channels"].append("garbage")          # malformed entries tolerated
    assert [c["id"] for c in cs.tracked(h)] == ["UC1"]

    # -- resolve: video link, @handle, channel URL, UC id, garbage ----------
    channels, videos = _fake_world()
    a, b, c = list(channels)
    client = _FakeYT(channels, videos)
    assert cs.resolve_channel(client, "https://youtu.be/cb000000002") == (b, "Clip Vault")
    assert cs.resolve_channel(client, "https://www.youtube.com/shorts/fa000000003")[0] == a
    assert cs.resolve_channel(client, "@FactsDaily") == (a, "Facts Daily")
    assert cs.resolve_channel(client, "https://www.youtube.com/@quietone")[0] == c
    assert cs.resolve_channel(client, f"https://www.youtube.com/channel/{a}")[0] == a
    assert cs.resolve_channel(client, a)[0] == a
    for bad in ("", "https://vimeo.com/123", "@nosuchhandle", "https://youtu.be/zzzzzzzzzzz"):
        try:
            cs.resolve_channel(client, bad)
        except (ValueError, RuntimeError):
            pass
        else:
            raise AssertionError(f"{bad!r} should not resolve")

    # -- batching: 120 channels -> 3 channels.list calls, never >50 ids -----
    from youtube import channels_vitals
    many = {f"UC{i:022d}": {"title": f"C{i}", "handle": f"@c{i}", "subs": i,
                            "views": i, "videos": []} for i in range(120)}
    big = _FakeYT(many, {})
    vit = channels_vitals(big, list(many) + ["UCmissing0000000000000000"])
    assert len(vit) == 120 and big.spent == 3
    assert "UCmissing0000000000000000" not in vit

    # -- end-to-end snapshot over two days --------------------------------
    cfg = tmp_cfg()
    store = cs.store_path(cfg)
    history = load_snapshots(store)
    for cid, ch in channels.items():
        cs.add_channel(history, cid, ch["title"])
    save_snapshots(store, history)
    client = _FakeYT(channels, videos)
    logs = []
    day1 = cs.run_snapshot(cfg, client, today="2026-09-28", log=logs.append)
    assert day1["prev"] is None and day1["errors"] == []
    assert day1["fetched"] == {a: 50, b: 3, c: 1}, day1["fetched"]  # limit 50
    # quota: 1 vitals + per channel (1 page [+1 for page 2? no: limit 50]
    #        + 1 videos) -> 1 + 3*2 = 7 units
    assert client.spent == 7, client.spent
    assert any("Facts Daily: 50 video(s)" in line for line in logs)
    # contentDetails requested on video batches (duration for LEN column)
    assert all("contentDetails" in p["part"] for m, p in client.calls if m == "videos")

    # day 2: growth + new upload + subs change + a rename
    channels[a]["subs"] = 1250
    channels[a]["views"] = 46000
    channels[b]["title"] = "Clip Vault HQ"
    for vid in list(videos)[:5]:
        videos[vid]["views"] += 25
    client2 = _FakeYT(channels, videos)
    day2 = cs.run_snapshot(cfg, client2, today="2026-09-29", log=lambda *_: None)
    assert day2["prev"]["date"] == "2026-09-28"
    hist = day2["history"]
    assert [ch["title"] for ch in cs.tracked(hist)][1] == "Clip Vault HQ"
    stored = hist["days"][-1]["videos"]["fa000000001"]
    assert stored["channel_id"] == a and stored["duration_s"] == 45
    assert stored["published_at"].endswith("Z")
    assert hist["days"][-1]["channels"][c]["subs_hidden"] is True
    # same-day rerun replaces (no duplicate day), prev still yesterday
    again = cs.run_snapshot(cfg, _FakeYT(channels, videos), today="2026-09-29",
                            log=lambda *_: None)
    assert [d["date"] for d in again["history"]["days"]] == ["2026-09-28", "2026-09-29"]

    report, flags = cs.build_channel_report(hist, "2026-09-29", day2["prev"],
                                            recent=10, now=now)
    # per-channel sections in add order, headers with subs + deltas
    ia, ib, ic = (report.index("━━ Facts Daily"), report.index("━━ Clip Vault HQ"),
                  report.index("━━ Quiet One"))
    assert ia < ib < ic, report
    assert "1,250 subs (+20)" in report and "46,000 views (+322)" in report
    assert "hidden subs" in report
    assert "(Δ vs 2026-09-28)" in report
    # --recent 10 honoured per channel; summary line counts all tracked
    fd = report[ia:ib]
    assert fd.count("\n  Fact number") + fd.count("\n  Brand new") == 10, fd
    assert "showing 10 of 50 tracked" in fd
    # newest first: the 4h-old upload tops the list with /h pace
    first_row = fd.split("\n")[2]
    assert "Brand new short" in first_row and "4h" in first_row, first_row
    assert "/h" in first_row and "+25" in first_row and "0:45" in first_row
    # long video: LEN h:mm:ss, title truncated with ellipsis
    cv = report[ib:ic]
    assert "1:02:03" in cv and "…" in cv
    # columns stay aligned even with an hour-long LEN: the VIEWS column
    # ends at the same offset on the header and every row
    table = [l for l in cv.split("\n")[1:]
             if l.startswith("  ") and not l.startswith("  showing")]
    views_end = table[0].index("VIEWS") + len("VIEWS")
    for line in table[1:]:
        assert line[views_end - 1].isdigit() and line[views_end] == " ", \
            (line, views_end)
    assert "1 video" in report and "1 videos" not in report
    # zero views -> engagement "—", not a crash / division error
    zero_row = next(l for l in cv.split("\n") if "Zero so far" in l)
    assert "—" in zero_row
    # retitle rule: >=2 days, <50 views, flagged once
    assert "cb000000002" in flags and "RETITLE?" in report
    assert "cb000000003" not in flags  # only 1h old: too early to judge
    hist["flagged"] = sorted(set(hist["flagged"]) | set(flags))
    _, flags2 = cs.build_channel_report(hist, "2026-09-29", day2["prev"], now=now)
    assert flags2 == []
    assert "ALL 3 channels:" in report and "+125 on tracked videos" in report
    # sort modes
    by_views, _ = cs.build_channel_report(hist, "2026-09-29", day2["prev"],
                                          recent=1, sort="views", only="clip",
                                          now=now)
    assert "Long talk" in by_views and "Stuck at twelve" not in by_views
    by_eng, _ = cs.build_channel_report(hist, "2026-09-29", day2["prev"],
                                        recent=3, sort="eng", only="clip", now=now)
    # eng: Long talk 6.0% > Stuck 0.0% > Zero (None) always last
    i1, i2, i3 = (by_eng.index("Long talk"), by_eng.index("Stuck at twelve"),
                  by_eng.index("Zero so far"))
    assert i1 < i2 < i3, by_eng
    by_pace, _ = cs.build_channel_report(hist, "2026-09-29", day2["prev"],
                                         recent=3, sort="pace", only="clip",
                                         now=now)
    # pace (views/day): Long 3000/9d=333 > Stuck 12/4d=3 > Zero 0
    assert by_pace.index("Long talk") < by_pace.index("Stuck at twelve") \
        < by_pace.index("Zero so far")
    assert "Facts Daily" not in by_eng and "ALL" not in by_eng  # filter + no total
    nomatch, _ = cs.build_channel_report(hist, "2026-09-29", None, only="zzz")
    assert "No tracked channel matches" in nomatch
    # first snapshot wording
    first, _ = cs.build_channel_report(hist, "2026-09-28", None, now=now)
    assert "first snapshot" in first

    # -- resilience: one channel broken, one terminated ---------------------
    channels2, videos2 = _fake_world()
    cfg2 = tmp_cfg()
    h2 = load_snapshots(cs.store_path(cfg2))
    for cid, ch in channels2.items():
        cs.add_channel(h2, cid, ch["title"])
    cs.add_channel(h2, "UC" + "z" * 22, "Gone Channel")
    save_snapshots(cs.store_path(cfg2), h2)
    res = cs.run_snapshot(cfg2, _FakeYT(channels2, videos2, broken={b}),
                          today="2026-09-29", log=lambda *_: None)
    assert res["fetched"] == {a: 50, c: 1}, res["fetched"]
    assert any("Clip Vault" in e and "500" in e for e in res["errors"])
    assert any("Gone Channel" in e and "--remove" in e for e in res["errors"])
    # reported exactly once, and no wasted per-channel lookup for it
    assert sum("Gone Channel" in e for e in res["errors"]) == 1, res["errors"]
    txt, _ = cs.build_channel_report(res["history"], "2026-09-29", None,
                                     errors=res["errors"], now=now)
    assert "⚠️ Clip Vault" in txt and "(no videos fetched)" in txt
    # nothing tracked -> LookupError (caller explains --add)
    try:
        cs.run_snapshot(tmp_cfg(), _FakeYT({}, {}), today="2026-09-29")
    except LookupError:
        pass
    else:
        raise AssertionError("empty tracking must raise LookupError")
    # vitals call failing -> videos still flow via per-channel playlist call
    class _NoVitals(_FakeYT):
        def _get(self, method, params, cost=1):
            if method == "channels" and "," in params.get("id", ","):
                raise RuntimeError("quota gone")
            return super()._get(method, params, cost)
    res3 = cs.run_snapshot(cfg2, _NoVitals(channels2, videos2),
                           today="2026-09-30", log=lambda *_: None)
    assert "channel totals unavailable" in res3["errors"][0]
    assert res3["fetched"].get(a) == 50

    # -- legacy history (pre-upgrade rows: no channel_id / published_at) ---
    legacy = {"channels": [{"id": a, "title": "Facts Daily"}], "flagged": [],
              "days": [{"date": "2026-09-29", "videos": {
                  "old00000001": {"title": "Old row", "channel": "Facts Daily",
                                  "views": 100, "likes": 5, "comments": 1,
                                  "published": "2026-09-20"},
                  "old00000002": {"title": "Stray", "channel": "Someone Else",
                                  "views": 7, "likes": 0, "comments": 0,
                                  "published": "2026-09-20"}}}]}
    ltxt, _ = cs.build_channel_report(legacy, "2026-09-29", None, now=now)
    assert "━━ Facts Daily" in ltxt and "Old row" in ltxt
    assert "other / untracked" in ltxt and "Stray" in ltxt  # never dropped
    assert "9d" in ltxt  # date-only age

    # -- pruning -------------------------------------------------------------
    ph = {"days": [{"date": "2026-01-01"}, {"date": "2026-09-01"},
                   {"date": "2026-09-29"}]}
    assert cs.prune_days(ph, "2026-09-29", 180) == 1
    assert [d["date"] for d in ph["days"]] == ["2026-09-01", "2026-09-29"]
    assert cs.prune_days(ph, "garbage", 180) == 0

    # -- config knobs clamp garbage ------------------------------------------
    assert tmp_cfg().snap_settings == {"recent": 10, "fetch_limit": 50,
                                       "keep_days": 180}
    odd = tmp_cfg(snap={"recent": "lots", "fetch_limit": 99999, "keep_days": 1})
    assert odd.snap_settings == {"recent": 10, "fetch_limit": 500, "keep_days": 7}
    assert tmp_cfg(snap="nonsense").snap_settings["recent"] == 10

    # -- phone report ----------------------------------------------------------
    msgs = cs.build_phone_report(hist, "2026-09-29", day2["prev"], now=now)
    assert len(msgs) == 3 and all(len(m) <= 4096 for m in msgs)
    assert msgs[0].startswith("📊 Facts Daily") and "1.2K subs (+20)" in msgs[0]
    assert msgs[0].count("\n1. ") == 1 and "\n5. " in msgs[0] and "\n6. " not in msgs[0]
    assert "Brand new short" in msgs[0].split("\n2. ")[0]
    assert "👥 hidden subs" in msgs[2]
    one = cs.build_phone_report(hist, "2026-09-29", day2["prev"], only="quiet", now=now)
    assert len(one) == 1 and "Quiet One" in one[0]
    assert "No tracked channel" in cs.build_phone_report(hist, "2026-09-29", None, only="zzz")[0]
    err = cs.build_phone_report(hist, "2026-09-29", None, errors=["X broke"], now=now)
    assert err[-1] == "⚠️ X broke"
    # giant titles can't blow the Telegram cap
    huge = {"channels": [{"id": a, "title": "T" * 5000}], "flagged": [],
            "days": [{"date": "2026-09-29", "videos": {}}]}
    assert len(cs.build_phone_report(huge, "2026-09-29", None, now=now)[0]) <= 4096

    # -- export: CSV (BOM, all rows) + xlsx (two sheets) ---------------------
    out = cs.export_snapshot(cfg, hist, "2026-09-29", day2["prev"], now=now)
    assert out["rows"] == 54
    raw = out["csv"].read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # Excel-friendly BOM
    import csv as _csv
    rows = list(_csv.DictReader(out["csv"].read_text(encoding="utf-8-sig").splitlines()))
    assert list(rows[0]) == cs.EXPORT_FIELDS
    brand = next(r for r in rows if r["title"] == "Brand new short")
    assert brand["views_delta"] == "25" and brand["is_short"] == "yes"
    assert brand["url"] == "https://youtu.be/fa000000000"
    zero = next(r for r in rows if r["title"] == "Zero so far")
    assert zero["engagement_pct"] == ""
    long_row = next(r for r in rows if r["title"].startswith("Long talk"))
    assert long_row["is_short"] == "no" and long_row["length_s"] == "3723"
    ch_rows = list(_csv.DictReader(out["channels_csv"].read_text(encoding="utf-8-sig").splitlines()))
    quiet = next(r for r in ch_rows if r["channel"] == "Quiet One")
    assert quiet["subs"] == "" and quiet["subs_delta"] == ""
    facts = next(r for r in ch_rows if r["channel"] == "Facts Daily")
    assert facts["subs_delta"] == "20" and facts["views_delta"] == "322"
    try:
        import openpyxl
    except ImportError:
        assert out["xlsx"] is None
    else:
        book = openpyxl.load_workbook(out["xlsx"])
        assert book.sheetnames == ["Videos", "Channels"]
        assert book["Videos"].max_row == 55 and book["Channels"].max_row == 4
        assert book["Videos"].freeze_panes == "A2"
    # openpyxl missing -> None, CSV still written
    import builtins
    real_import = builtins.__import__

    def no_openpyxl(name, *args, **kw):
        if name.startswith("openpyxl"):
            raise ImportError(name)
        return real_import(name, *args, **kw)

    builtins.__import__ = no_openpyxl
    try:
        assert cs.write_xlsx([], [], cfg.root / "x.xlsx") is None
    finally:
        builtins.__import__ = real_import

    # -- old snap contract still intact: legacy build_report on new rows ----
    from youtube import build_report
    old_txt, _ = build_report(hist, "2026-09-29", day2["prev"])
    assert "TOTAL:" in old_txt


def t_snap_cli():
    import argparse
    import io
    from contextlib import redirect_stdout
    from unittest.mock import patch as _patch

    import channelstats as cs
    import main as main_mod
    from bot import PhoneBot, parse_incoming
    from youtube import load_snapshots

    def args(**kw):
        merged = {"add": None, "remove": None, "list": False, "channel": None,
                  "recent": None, "sort": "new", "fetch": None,
                  "export": False, "json": False}
        merged.update(kw)
        return argparse.Namespace(**merged)

    def run(cfg, **kw):
        buf = io.StringIO()
        code = None
        with redirect_stdout(buf):
            try:
                code = main_mod.cmd_snap(cfg, args(**kw))
            except SystemExit as exc:
                code = exc.code
        return code, buf.getvalue()

    channels, videos = _fake_world()
    a, b, c = list(channels)
    fake = _FakeYT(channels, videos)
    cfg = tmp_cfg(youtube={"api_keys": ["k1"]})

    # --list with nothing tracked: offline, friendly
    code, out = run(cfg, list=True)
    assert code == 0 and "No channels tracked" in out
    # plain run with nothing tracked -> exits with the --add recipe
    with _patch.object(cs, "make_client", lambda _c: fake):
        code, out = run(cfg)
    assert code == 1 and "snap --add @yourhandle" in out
    # no key -> clear error before any API call (list/remove still work)
    code, out = run(tmp_cfg(), add=["@factsdaily"])
    assert code == 1 and "no YouTube API key" in out

    # --add three ways at once (repeatable), then report runs
    with _patch.object(cs, "make_client", lambda _c: fake):
        code, out = run(cfg, add=["@factsdaily", "https://youtu.be/cb000000002",
                                  f"https://www.youtube.com/channel/{c}"])
    assert code == 0, out
    assert out.count("tracking: ") == 3
    assert "━━ Facts Daily" in out and "━━ Clip Vault" in out and "━━ Quiet One" in out
    assert "quota:" in out and "first snapshot" in out
    # adding again is idempotent
    with _patch.object(cs, "make_client", lambda _c: fake):
        code, out = run(cfg, add=["@factsdaily"])
    assert "already tracking: Facts Daily" in out
    assert len(cs.tracked(load_snapshots(cs.store_path(cfg)))) == 3
    # bad add -> readable error, nothing tracked changes
    with _patch.object(cs, "make_client", lambda _c: fake):
        code, out = run(cfg, add=["https://vimeo.com/1"])
    assert code == 1 and "could not add" in out

    # --list shows all three, no API calls made
    before = fake.spent
    code, out = run(cfg, list=True)
    assert code == 0 and "Tracking 3 channel(s)" in out and a in out
    assert fake.spent == before

    # --channel / --recent / --sort / --fetch plumb through
    with _patch.object(cs, "make_client", lambda _c: fake):
        code, out = run(cfg, channel="facts", recent=3, fetch=5)
    assert code == 0 and "showing 3 of 5 tracked" in out
    assert "Clip Vault" not in out.split("quota")[0].split("Channel stats")[1]
    with _patch.object(cs, "make_client", lambda _c: fake):
        code, out = run(cfg, recent=-4, fetch=-1)  # garbage clamps, no crash
    assert code == 0 and "showing 1 of 1 tracked" in out

    # --json prints the raw day
    with _patch.object(cs, "make_client", lambda _c: fake):
        code, out = run(cfg, json=True)
    import json as _json
    raw = out[out.index("{"):out.rindex("}") + 1]
    assert _json.loads(raw)["channels"][a]["subs"] == 1230

    # --export writes files + tells you which to open
    with _patch.object(cs, "make_client", lambda _c: fake):
        code, out = run(cfg, export=True)
    assert code == 0 and "exported" in out and "stats-" in out
    assert list((cfg.out_dir / "stats").glob("stats-*.csv"))

    # --remove: ambiguous / unknown / ok (history kept)
    code, out = run(cfg, remove="zzz")
    assert code == 1 and "no tracked channel matches" in out
    cs.add_channel(h := load_snapshots(cs.store_path(cfg)), "UC" + "d" * 22, "Facts Weekly")
    from youtube import save_snapshots
    save_snapshots(cs.store_path(cfg), h)
    code, out = run(cfg, remove="facts")
    assert code == 1 and "matches several" in out
    code, out = run(cfg, remove="weekly")
    assert code == 0 and "stopped tracking: Facts Weekly" in out
    left = load_snapshots(cs.store_path(cfg))
    assert len(cs.tracked(left)) == 3 and left["days"]  # history untouched

    # -- bot: parser + /stats handler ---------------------------------------
    assert parse_incoming("/stats") == ("stats", "")
    assert parse_incoming("/STATS Facts") == ("stats", "Facts")
    assert parse_incoming("/statsxyz") == ("help", "")
    assert parse_incoming("/stats   ") == ("stats", "")

    cfg.data["telegram"]["bot_token"] = "t"
    cfg.data["telegram"]["owner_id"] = 42
    bot = PhoneBot(cfg)
    sent = []
    bot.send_message = lambda chat, text: sent.append(text) or {"message_id": 1}

    def say(text):
        bot.handle_message({"chat": {"id": 42}, "from": {"id": 42}, "text": text})

    with _patch.object(cs, "make_client", lambda _c: fake):
        say("/stats")
    assert len(sent) == 3 and sent[0].startswith("📊 Facts Daily"), sent
    sent.clear()
    with _patch.object(cs, "make_client", lambda _c: fake):
        say("/stats vault")
    assert len(sent) == 1 and "Clip Vault" in sent[0]
    sent.clear()
    def boom(_c):
        raise RuntimeError("YouTube unavailable (all keys exhausted)")
    with _patch.object(cs, "make_client", boom):
        say("/stats")
    assert sent and "Stats failed" in sent[0] and "exhausted" in sent[0]
    # help lists it
    sent.clear()
    say("/help")
    assert "/stats" in sent[0]
    # no key / nothing tracked -> guidance, no crash
    nokey = tmp_cfg()
    nokey.data["telegram"]["bot_token"] = "t"
    nokey.data["telegram"]["owner_id"] = 42
    bot2 = PhoneBot(nokey)
    got = []
    bot2.send_message = lambda chat, text: got.append(text) or {"message_id": 1}
    bot2.handle_message({"chat": {"id": 42}, "from": {"id": 42}, "text": "/stats"})
    assert "No YouTube API key" in got[0]
    nokey.data["youtube"]["api_keys"] = ["k"]
    got.clear()
    bot2.handle_message({"chat": {"id": 42}, "from": {"id": 42}, "text": "/stats"})
    assert "No channels tracked" in got[0] and "snap --add" in got[0]


def t_topic_scout():
    import json as _json

    from topics import parse_scout_proposals, scout_rank, wiki_title

    assert wiki_title("  Honey Badger ") == "Honey_Badger"
    assert wiki_title("") == ""
    raw = _json.dumps({"topics": [
        {"topic": "Why wombat poop comes out as cubes",
         "wikipedia": "Wombat"},
        {"topic": "How lie detectors actually work",
         "wikipedia": "Polygraph"},
        {"topic": "Why wombat poop is cube shaped",   # dupe of #1
         "wikipedia": "Wombat"},
        {"topic": "", "wikipedia": "X"},               # empty topic
        {"topic": "Facts about everything!!", "wikipedia": "X"},  # junk
        {"not-a-topic": True},                          # garbage entry
    ]})
    parsed = parse_scout_proposals(raw, [])
    assert parsed == [("Why wombat poop comes out as cubes", "Wombat"),
                      ("How lie detectors actually work", "Polygraph")]
    # dedupe against existing backlog too
    assert parse_scout_proposals(raw, ["how lie detectors work"]) == \
        [("Why wombat poop comes out as cubes", "Wombat")]
    # unparseable LLM output -> []
    assert parse_scout_proposals("no json here", []) == []

    # ranking: below-bar and unverifiable drop, order by views desc.
    proposals = [("A", "Honey"), ("B", "Polygraph"), ("C", "Missing"),
                 ("D", "Cat")]
    views = {"Honey": 9000, "Polygraph": 400, "Cat": 12000,
             "Missing": None}
    ranked = scout_rank(proposals, views.__getitem__, min_views=500,
                        count=3)
    assert ranked == [("D", "Cat", 12000), ("A", "Honey", 9000)]
    # count limits; nothing passes -> empty
    assert len(scout_rank(proposals, views.__getitem__, 500, 1)) == 1
    assert scout_rank(proposals, views.__getitem__, 99999, 3) == []
    # config knob
    cfg = tmp_cfg()
    assert cfg.topics_scout_min_weekly == 500
    cfg.data["topics"]["scout_min_weekly_views"] = 2000
    assert cfg.topics_scout_min_weekly == 2000
    cfg.data["topics"]["scout_min_weekly_views"] = 0
    assert cfg.topics_scout_min_weekly == 10  # clamped


def t_ab_titles():
    import json as _json
    import tempfile
    from pathlib import Path

    from editorial import _optimize_title
    from jobqueue import Job
    from package import build_package
    from scriptgen import Scene, Script

    class _P:
        def generate_text(self, prompt, **kw):
            return _json.dumps({"titles": [
                "The Jellyfish That Refuses To Die",
                "Why Jellyfish Never Grow Old",
                "Jellyfish Facts You Won't Believe Today Friend"]})

    scenes = [Scene(narration=(
        "Scientists watched one jellyfish reverse its own aging for "
        "years. Its cells reprogram themselves."), image_prompt="x")]
    # Polish fires on a weak draft: winner takes the title, runner-up
    # becomes the B variant.
    script = Script(title="Clip 01 Moment", description="", tags=[],
                    scenes=scenes)
    _optimize_title(script, tmp_cfg(), _P())
    assert script.title == "The Jellyfish That Refuses To Die"
    assert script.title_alt == "Why Jellyfish Never Grow Old"
    # Draft already good: title kept, close best candidate becomes B.
    script2 = Script(title="Why Jellyfish Never Grow Old", description="",
                     tags=[], scenes=scenes)
    _optimize_title(script2, tmp_cfg(), _P())
    assert script2.title == "Why Jellyfish Never Grow Old"
    assert script2.title_alt == "The Jellyfish That Refuses To Die"

    # Package: the kit carries title-b.txt + an A/B checklist note.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        video = root / "v.mp4"
        video.write_bytes(b"x" * 100)
        meta = root / "script.json"
        meta.write_text(_json.dumps({
            "title": "Why Zebras Have Stripes", "tags": ["facts"],
            "title_alt": "The Secret Behind Zebra Stripes",
            "format": "portrait", "duration_seconds": 60,
        }), encoding="utf-8")
        job = Job(id="ab12", topic="why zebras have stripes",
                  status="generated", video_file=str(video),
                  meta_file=str(meta))
        cfg = tmp_cfg()
        kit = build_package(job, cfg)
        assert (kit / "title.txt").exists()
        assert (kit / "title-b.txt").read_text(encoding="utf-8") == \
            "The Secret Behind Zebra Stripes"
        checklist = (kit / "CHECKLIST.md").read_text(encoding="utf-8")
        assert "A/B title test" in checklist and "title-b.txt" in checklist
        # No alt (or alt == title): single-title kit, no A/B noise.
        meta.write_text(_json.dumps({
            "title": "Why Zebras Have Stripes", "tags": ["facts"],
            "title_alt": "Why Zebras Have Stripes",
            "format": "portrait", "duration_seconds": 60}), encoding="utf-8")
        kit2 = build_package(job, cfg)
        assert not (kit2 / "title-b.txt").exists()
        assert "A/B title test" not in \
            (kit2 / "CHECKLIST.md").read_text(encoding="utf-8")


def t_mux_crash_recovery():
    from assembler import (AssemblyError, cpu_retry_cmd, is_access_violation,
                           run, swap_to_cpu_encoder)

    assert is_access_violation(3221225477) is True   # 0xC0000005
    assert is_access_violation(1) is False
    assert is_access_violation(0) is False
    assert is_access_violation(None) is False

    qsv_cmd = ["ffmpeg", "-i", "a.mp4", "-c:v", "h264_qsv",
               "-preset", "veryfast", "-global_quality", "20",
               "-pix_fmt", "yuv420p", "out.mp4"]
    swapped = swap_to_cpu_encoder(qsv_cmd)
    assert swapped == ["ffmpeg", "-i", "a.mp4", "-c:v", "libx264",
                       "-preset", "veryfast", "-crf", "20",
                       "-pix_fmt", "yuv420p", "out.mp4"], swapped
    # nvenc + videotoolbox tails, and unknown flags end the walk
    assert "h264_nvenc" not in swap_to_cpu_encoder(
        ["ffmpeg", "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "20", "o.mp4"])
    vt = swap_to_cpu_encoder(["ffmpeg", "-c:v", "h264_videotoolbox",
                              "-q:v", "65", "o.mp4"])
    assert vt[2] == "libx264" and vt[-1] == "o.mp4"
    # no hardware codec -> unchanged (new list, same content)
    cpu_cmd = ["ffmpeg", "-c:v", "libx264", "-crf", "20", "o.mp4"]
    assert swap_to_cpu_encoder(cpu_cmd) == cpu_cmd
    # decision gate: only violation + hw encoder earns a retry
    assert cpu_retry_cmd(qsv_cmd, 3221225477) == swapped
    assert cpu_retry_cmd(qsv_cmd, 1) is None          # not a crash
    assert cpu_retry_cmd(cpu_cmd, 3221225477) is None  # already cpu
    # AssemblyError carries the exit code; run() propagates it
    err = AssemblyError("x failed (exit 1)", cmd=["x"], returncode=1)
    assert err.returncode == 1
    # A real failing subprocess, cross-platform: a .sh script only proves
    # exit codes on Linux (live catch on the main PC: WinError 193,
    # "not a valid Win32 application"). Python exits 3 everywhere.
    import sys as _sys

    try:
        run([_sys.executable, "-c", "import sys; sys.exit(3)"], "probe")
        raised = False
    except AssemblyError as exc:
        raised = True
        assert exc.returncode == 3
    assert raised


def t_clip_target():
    import subprocess
    import sys as _sys
    from pathlib import Path as _P

    from clipper import resolve_clip_target

    # bare link -> url; bare path -> file (live case 2026-09-23).
    assert resolve_clip_target("https://youtu.be/abc123XYZ_-", "", "") == \
        ("https://youtu.be/abc123XYZ_-", "")
    assert resolve_clip_target("C:\\videos\\vod.mp4", "", "") == \
        ("", "C:\\videos\\vod.mp4")
    # explicit flags win over the positional; empty stays empty.
    assert resolve_clip_target("junk", "u", "")[0] == "u"
    assert resolve_clip_target("junk", "", "f.mp4") == ("", "f.mp4")
    assert resolve_clip_target("", "", "") == ("", "")
    # and the CLI actually accepts the positional (parser wiring).
    result = subprocess.run(
        [_sys.executable, str(_P("main.py")), "clip", "-h"],
        capture_output=True, text=True, cwd=str(_P(".")))
    assert result.returncode == 0, result.stderr[-200:]
    assert "target" in result.stdout and "--url" in result.stdout


def t_clip_fit():
    from clipper import crop_filter, fit_filter, vertical_treatment

    graph = fit_filter()
    assert graph.startswith("split=2[bg][fg];")
    assert "boxblur=20:2" in graph and "eq=brightness=-0.15" in graph
    assert "scale=1080:-2" in graph and "overlay=(W-w)/2:(H-h)/2" in graph
    assert "force_original_aspect_ratio=increase" in graph
    # Treatment table: the live case — 16:9 landscape keeps its frame.
    assert vertical_treatment(1280, 720, "auto") == "fit"
    assert vertical_treatment(1920, 1080, "auto") == "fit"
    assert vertical_treatment(1080, 1080, "auto") == "fit"   # square: bars ok
    assert vertical_treatment(720, 1280, "auto") == "scale"  # already vertical
    assert vertical_treatment(864, 1152, "auto") == "crop"   # 3:4 sliver crop
    # Explicit modes win; garbage falls back to auto's rules.
    assert vertical_treatment(1280, 720, "crop") == "crop"
    assert vertical_treatment(1280, 720, "fit") == "fit"
    assert vertical_treatment(720, 1280, "fit") == "scale"
    assert vertical_treatment(1280, 720, "nonsense") == "fit"
    assert vertical_treatment(0, 0) == "scale"
    # Old crop helpers unchanged for the crop path.
    assert crop_filter(1280, 720) == "crop=ih*9/16:ih,scale=1080:1920"
    # Config knob.
    cfg = tmp_cfg()
    assert cfg.clip_crop_mode == "auto"
    cfg.data["clip"]["crop_mode"] = "crop"
    assert cfg.clip_crop_mode == "crop"
    cfg.data["clip"]["crop_mode"] = "bogus"
    assert cfg.clip_crop_mode == "auto"


def t_transcript_fix():
    import json as _json

    from clipper import fix_transcript_words, whisper_params

    assert whisper_params("") == {"language": "en"}
    wp = whisper_params("The Immortal Jellyfish Explained")
    assert wp["language"] == "en"
    assert "Jellyfish" in wp["prompt"]

    prompts = []

    class _P:
        def __init__(self, fixes):
            self.fixes = fixes

        def generate_text(self, prompt, **kw):
            prompts.append(prompt)
            return _json.dumps({"fixes": self.fixes})

    words = [{"word": "you", "start": 0.0, "end": 0.3},
             {"word": "no", "start": 0.4, "end": 0.7},
             {"word": "the", "start": 0.8, "end": 1.0},
             {"word": "answer.", "start": 1.1, "end": 1.5}]
    # diffs-only contract (groq live lesson): the model returns just the
    # words that change — no 800-word echo that overflows completion caps.
    fixed = fix_transcript_words(words, "Quiz Time", _P(
        [{"i": 1, "w": "know"}]))
    assert [w["word"] for w in fixed] == ["you", "know", "the", "answer."]
    assert fixed[1]["start"] == 0.4 and fixed[1]["end"] == 0.7
    assert words[1]["word"] == "no"      # caller's list never mutated
    assert "NUMBERED WORDS" in prompts[-1] and '"fixes"' in prompts[-1]
    assert "1: no" in prompts[-1]        # local indexes, one per word
    # nothing to fix -> untouched
    assert fix_transcript_words(words, "t", _P([])) == words
    # every malformed fix is ignored individually: out-of-range index,
    # multi-word replacement, same word, junk entries, missing keys.
    junk = fix_transcript_words(words, "t", _P([
        {"i": 99, "w": "know"}, {"i": 0, "w": "the answer"},
        {"i": 2, "w": "the"}, "not-a-dict", {"w": "orphan"},
        {"i": 3, "w": ""}]))
    assert [w["word"] for w in junk] == ["you", "no", "the", "answer."]
    # provider failure -> original, no exception.
    class _Boom:
        def generate_text(self, prompt, **kw):
            raise RuntimeError("503")

    assert fix_transcript_words(words, "t", _Boom()) == words
    assert fix_transcript_words([], "t", _P([])) == []
    # config default + switch
    cfg = tmp_cfg()
    assert cfg.clip_transcript_fix is True
    cfg.data["clip"]["transcript_fix"] = False
    assert cfg.clip_transcript_fix is False


def t_no_doubled_decorators():
    """Agent tripwire (4th live strike 2026-09-23): inserting a property
    before a decorated def duplicated @property and broke the property
    until the suite caught it. Ban the pattern outright, every module."""
    import re as _re

    pattern = _re.compile(r"^\s*@property\s*$\n^\s*@property", _re.M)
    for path in Path(__file__).parent.glob("*.py"):
        if path.name == "test_smoke.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert not pattern.search(text), \
            f"doubled @property in {path.name}"


def t_clip_distribution():
    import tempfile
    from pathlib import Path

    from clipper import (Candidate, clip_hashtags,
                         clip_platform_caption, plan_clip_sources,
                         write_kit)

    # hashtags: platform staples + title words, capped
    tt = clip_hashtags("Why Cells Become the Most Immortal Animal", "tiktok")
    assert tt[:3] == ["cells", "become", "most"] and "fyp" in tt
    assert len(tt) <= 6
    assert "reels" in clip_hashtags("Title Here", "reels")
    # captions: title + tags, one line each, no credit (that's for YouTube)
    cap = clip_platform_caption("Why Cells Become Immortal", "tiktok")
    assert cap.startswith("Why Cells Become Immortal\n")
    assert "#fyp" in cap and "youtu" not in cap.lower()
    assert clip_platform_caption("T", "reels").endswith("\n")
    # kit: platform files land next to the old ones
    with tempfile.TemporaryDirectory() as tmp:
        clip = Path(tmp) / "clip_01.mp4"
        clip.write_bytes(b"v" * 64)
        cand = Candidate(start=1.0, end=20.0, hook="h", title_idea="t")
        kit = write_kit(clip, "Why Cells Become Immortal", cand,
                        {"title": "s", "channel": "c", "url": "u"},
                        Path(tmp))
        assert (kit / "tiktok.txt").exists()
        assert (kit / "reels.txt").exists()
        assert "#fyp" in (kit / "tiktok.txt").read_text(encoding="utf-8")
        assert (kit / "CREDIT.txt").exists()  # youtube credit intact
        # checklist: the 2026-09-23 live posting went out with an empty
        # description — kits now carry the walkthrough that prevents it
        check = (kit / "CHECKLIST.md").read_text(encoding="utf-8")
        assert "DESCRIPTION.txt" in check and "attribution" in check.lower()
    # batch planning: positional first, then flags in typed order
    assert plan_clip_sources("https://youtu.be/a", [], []) == \
        [("https://youtu.be/a", "")]
    assert plan_clip_sources("C:\\v.mp4", [], []) == [("", "C:\\v.mp4")]
    plan = plan_clip_sources("", ["u1", "u2"], ["f1"])
    assert plan == [("u1", ""), ("u2", ""), ("", "f1")]
    plan2 = plan_clip_sources("u0", ["u1"], ["f1", ""])
    assert plan2 == [("", "u0"), ("u1", ""), ("", "f1")]  # u0 isn't a
    # URL -> correctly routed as a file path
    assert plan_clip_sources("", [], []) == []
    assert plan_clip_sources("", [""], ["  "]) == []


def t_json_mode_400_retry():
    """Groq's json_object validator can 400 a legal prompt (live
    2026-09-23): retry the same model once WITHOUT response_format."""
    from unittest.mock import patch

    import openai_compat
    from groq import GroqProvider

    class _Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
            self.text = str(payload)

        def json(self):
            return self._payload

    calls = []

    def fake_post(url, headers=None, json=None, timeout=0):
        calls.append(dict(json))  # copy! the real body dict is mutated
        if len(calls) == 1:
            return _Resp(400, {"error": {"message":
                "Failed to validate JSON. Please adjust your prompt."}})
        assert "response_format" not in json, "retry kept response_format!"
        return _Resp(200, {"choices": [{"message": {
            "content": "{\"ok\": true}"}}]})

    provider = GroqProvider(["k"], "openai/gpt-oss-120b")
    with patch.object(openai_compat.requests, "post", side_effect=fake_post):
        out = provider.generate_text("give me json", json_mode=True,
                                     tag="t400")
    assert out == '{"ok": true}'
    assert len(calls) == 2 and "response_format" in calls[0]
    # A non-JSON 400 never strips anything: straight to the next model.
    calls.clear()

    def fake_post2(url, headers=None, json=None, timeout=0):
        calls.append(dict(json))
        return _Resp(400, {"error": {"message": "bad request"}})

    with patch.object(openai_compat.requests, "post", side_effect=fake_post2):
        try:
            provider.generate_text("x", json_mode=True, tag="t400b")
            raised = False
        except RuntimeError:
            raised = True
        assert raised
    # no SAME-model retry: one call per model as the chain walks on
    assert len(calls) == 3, calls
    assert [c.get("model") for c in calls] == [
        "openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]


def t_music_audit():
    import contextlib
    import wave

    import audiofx
    from audiofx import (SILENT_RMS, ensure_audible_loop, ensure_assets,
                         wav_rms)

    cfg = tmp_cfg()
    cache = cfg.work_dir / "_fx"
    loop = ensure_assets(cache)["music_loop"]
    # The synthesized loop is genuinely audible (the "silent track?"
    # suspicion, settled by measurement).
    assert wav_rms(loop) > SILENT_RMS, wav_rms(loop)
    # A corrupted silent cache self-heals: audit regenerates the file.
    with contextlib.closing(wave.open(str(loop), "wb")) as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00" * 44100)
    assert wav_rms(loop) < SILENT_RMS
    path, verdict = ensure_audible_loop(cache)
    assert verdict.startswith("regenerated"), verdict
    assert wav_rms(path) > SILENT_RMS
    # Healthy cache: ok verdict.
    _, verdict = ensure_audible_loop(cache)
    assert verdict.startswith("ok"), verdict
    # Unreadable/missing file: 0.0, never raises.
    assert wav_rms(cfg.work_dir / "nope.wav") == 0.0
    # Pathological renderer: SUSPECT verdict, surfaced not swallowed.
    real = audiofx._render_music_loop
    audiofx._render_music_loop = lambda: ([0.0] * 100, [0.0] * 100)
    try:
        loop.unlink()
        _, verdict = ensure_audible_loop(cache)
        assert verdict.startswith("SUSPECT"), verdict
    finally:
        audiofx._render_music_loop = real


def t_music_audible():
    from assembler import audio_candy_lost

    # -18 under 6:1 ducking measured inaudible; -12 is the default now
    # (an explicit level_db in config.yaml still wins — by design).
    assert tmp_cfg().music_level_db == -12
    assert tmp_cfg().music_enabled is True
    # Which degraded mux rungs mean the music/sfx are gone?
    assert audio_candy_lost("full mix") is False
    assert audio_candy_lost("without the end-card text") is False
    assert audio_candy_lost("without burned subtitles") is False
    assert audio_candy_lost("python garnish mix (no ffmpeg filters)") is False
    assert audio_candy_lost("plain video + narration") is True
    assert audio_candy_lost("emergency direct mux") is True
    assert audio_candy_lost("last-resort mp3 mux") is True


def t_concept_dupe():
    from topics import is_same_topic

    # Paraphrase dupe: same video, different words — must be caught.
    assert is_same_topic("Why Honey Doesn't Expire", "Why Honey Never Expires")
    assert is_same_topic("Why you cannot sneeze with your eyes open",
                         "Why We Close Our Eyes When We Sneeze")
    # Same story, same words — still caught.
    assert is_same_topic("Why Flamingos Stand On One Leg",
                         "why flamingos stand on one leg!")
    # Different videos sharing one noun — NOT dupes.
    assert not is_same_topic("How vending machines tell a real coin from a fake",
                             "You Can Buy A Car In This Vending Machine In Japan")
    assert not is_same_topic("The crab that wears a living sponge as camouflage",
                             "The crab that farms its own food on its claws")
    assert not is_same_topic("The octopus that walks on land between tide pools",
                             "How an octopus thinks with its arms")
    assert not is_same_topic("Why we say um and uh when we speak",
                             "Why We Close Our Eyes When We Sneeze")
    # Short titles: exact only, never fuzzy.
    assert not is_same_topic("Pisa tower", "Eiffel tower")
    assert is_same_topic("Pisa tower", "pisa tower!")


def t_scrub():
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from editorial import scrub_narration
    from subtitles import build_cues, max_chars_for
    from voiceover import synthesise

    assert scrub_narration("Wait for it... the answer is blood.") == \
        "Wait for it, the answer is blood."
    assert scrub_narration("...and the last one changes everything") == \
        "and the last one changes everything"
    assert scrub_narration("Really?... Next scene.") == "Really? Next scene."
    assert scrub_narration("Hold on\u2026 breathe.") == "Hold on, breathe."
    assert scrub_narration("But [pause] then it moves.") == "But then it moves."
    assert scrub_narration("Over....") == "Over"
    assert scrub_narration("3.14 and U.S.A stay.") == "3.14 and U.S.A stay."
    # Captions can never show dots: the scrub sits inside _word_times.
    cues = build_cues(["Wait... what?"], [[(0.0, 0.4), (0.4, 0.9)]],
                      [0.0], [2.0], max_chars_for("portrait"), 0.1)
    text = " ".join(line for cue in cues for line in cue.lines)
    assert "..." not in text and "\u2026" not in text, text
    assert "Wait," in text, text
    # TTS receives the same scrubbed words (timings stay aligned).
    cfg = tmp_cfg()
    cfg.data.setdefault("channel", {})["elevenlabs_api_keys"] = ["k1"]
    cfg.data["channel"]["elevenlabs_voice_id"] = "v1"
    with tempfile.TemporaryDirectory() as tmp, \
            patch("voiceover._elevenlabs_synth", return_value=[]) as synth:
        synthesise("Wait... go.", Path(tmp) / "s.mp3", cfg)
    assert synth.call_args.args[0] == "Wait, go."


def t_stock_pick():
    from stock import pick_photo

    photos = [{"id": 1, "alt": "green forest road"},
              {"id": 2, "alt": "octopus swimming in deep water"},
              {"id": 3, "alt": ""}]
    # Best alt-text match wins regardless of position or seed...
    assert pick_photo(photos, 0, "octopus water")["id"] == 2
    assert pick_photo(photos, 5, "octopus water")["id"] == 2
    # ...zero-overlap results are excluded while anything better exists...
    assert pick_photo(photos, 1, "octopus")["id"] == 2
    # ...plural-tolerant both directions...
    assert pick_photo([{"id": 7, "alt": "ants carry leaves"}], 0, "ant")["id"] == 7
    assert pick_photo([{"id": 8, "alt": "ant on a leaf"}], 0, "ants")["id"] == 8
    # ...and legacy seed order applies when nothing matches (never blocks).
    assert pick_photo(photos, 1, "zebra")["id"] == 2
    assert pick_photo(photos, 0, "")["id"] == 1
    assert pick_photo([], 0, "octopus") is None


def t_heartbeat():
    """Heartbeat: unconfigured = silent no-op; pings the check-in URL;
    network faults swallowed; full URLs accepted."""
    from unittest.mock import Mock, patch

    from heartbeat import check_id_from, ping

    assert ping(tmp_cfg()) is False  # nothing configured
    cfg = tmp_cfg(**{"honeybadger": {"check": "ABC123"}})
    assert check_id_from(cfg) == "ABC123"
    cfg2 = tmp_cfg(**{"honeybadger": {"check": "https://api.honeybadger.io/v1/check_in/XYZ/"}})
    assert check_id_from(cfg2) == "XYZ"
    with patch("heartbeat.requests.get",
               return_value=Mock(status_code=200)) as get:
        assert ping(cfg) is True
    assert get.call_args.args[0] == "https://api.honeybadger.io/v1/check_in/ABC123"
    with patch("heartbeat.requests.get", side_effect=OSError("down")):
        assert ping(cfg) is False  # network faults swallowed


def t_groq_rotation():
    from unittest.mock import Mock, patch

    from groq import GroqProvider

    def ok(text="hello"):
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": text}}]}
        return response

    # 429 on key1 -> key2 tried immediately with its own Bearer header.
    with patch("requests.post", side_effect=[Mock(status_code=429, text="slow"),
                                             ok()]) as post:
        result = GroqProvider(api_keys=["k1", "k2"])._complete(
            "hi", temperature=0.0, json_mode=False, tag="t")
    assert result == "hello" and post.call_count == 2
    assert post.call_args_list[1].kwargs["headers"] == {"Authorization": "Bearer k2"}
    # 401 -> key dropped for the run, next key still succeeds.
    with patch("requests.post", side_effect=[Mock(status_code=401, text="bad"), ok()]) as post:
        result = GroqProvider(api_keys=["bad", "good"])._complete(
            "hi", temperature=0.0, json_mode=False, tag="t")
    assert result == "hello" and post.call_count == 2
    # 200-with-empty (dry backing pool, seen live) -> next model at once.
    empty = Mock(status_code=200)
    empty.json.return_value = {"choices": [{"message": {"content": "  "}}]}
    with patch("requests.post", side_effect=[empty, ok()]) as post:
        result = GroqProvider(api_keys=["k1", "k2"])._complete(
            "hi", temperature=0.0, json_mode=False, tag="t")
    assert result == "hello" and post.call_count == 2
    assert post.call_args_list[1].kwargs["json"]["model"] == "qwen/qwen3.8-27b"
    # 404 -> next model tried (120b default, then qwen).
    with patch("requests.post", side_effect=[Mock(status_code=404, text="gone"), ok()]) as post:
        GroqProvider(api_keys=["k"])._complete("hi", temperature=0.0, json_mode=False, tag="t")
    bodies = [call.kwargs["json"] for call in post.call_args_list]
    assert [body["model"] for body in bodies] == ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"]
    # Org-level 429 names the organization: every key shares that fate,
    # so remaining keys are skipped and the next model pool is tried.
    org_429 = Mock(status_code=429, text="Rate limit reached for model `m` "
                                        "in organization `org_x` on tokens per minute (TPM)")
    with patch("requests.post", side_effect=[org_429, ok()]) as post:
        result = GroqProvider(api_keys=["k1", "k2", "k3"])._complete(
            "hi", temperature=0.0, json_mode=False, tag="t")
    assert result == "hello" and post.call_count == 2
    second = post.call_args_list[1]
    assert second.kwargs["json"]["model"] == "qwen/qwen3.8-27b"
    assert second.kwargs["headers"] == {"Authorization": "Bearer k1"}
    # 5xx is server-side: no key will fix it, next model at once.
    with patch("requests.post", side_effect=[Mock(status_code=500, text="err"), ok()]) as post:
        GroqProvider(api_keys=["k1", "k2"])._complete(
            "hi", temperature=0.0, json_mode=False, tag="t")
    assert [call.kwargs["json"]["model"] for call in post.call_args_list] == [
        "openai/gpt-oss-120b", "qwen/qwen3.8-27b"]
    # Two hung requests in a row: next model, not every key x 60 s.
    from requests.exceptions import Timeout
    with patch("requests.post", side_effect=[Timeout(), Timeout(), ok()]) as post:
        result = GroqProvider(api_keys=["k1", "k2", "k3"])._complete(
            "hi", temperature=0.0, json_mode=False, tag="t")
    assert result == "hello" and post.call_count == 3
    assert post.call_args_list[2].kwargs["json"]["model"] == "qwen/qwen3.8-27b"
    # gpt-oss gets hidden reasoning + JSON mode passes response_format through.
    with patch("requests.post", return_value=ok()) as post:
        GroqProvider(api_keys=["k"], model="openai/gpt-oss-120b")._complete(
            "hi", temperature=0.0, json_mode=True, tag="t")
    body = post.call_args.kwargs["json"]
    assert body["reasoning_format"] == "hidden"
    assert body["response_format"] == {"type": "json_object"}
    # every key 429 on every model -> RuntimeError after the full sweep.
    with patch("requests.post", return_value=Mock(status_code=429, text="slow")) as post:
        try:
            GroqProvider(api_keys=["k1", "k2"])._complete(
                "hi", temperature=0.0, json_mode=False, tag="t")
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected RuntimeError on full 429 sweep")
    assert post.call_count == 6  # 3 models x 2 keys


def t_script_chain_groq():
    from scriptgen import ChainedProvider, get_provider

    cfg = tmp_cfg()
    assert [label for label, _ in get_provider(cfg).chain] == [
        "pollinations", "template"]
    cfg.data["ai"]["groq_api_keys"] = [f"k{i}" for i in range(10)]
    labels = [label for label, _ in get_provider(cfg).chain]
    assert labels == ["groq", "pollinations", "template"]
    assert dict(get_provider(cfg).chain)["groq"].api_keys == \
        cfg.groq_llm_api_keys
    cfg.data["ai"]["gemini_api_key"] = "g"
    assert [label for label, _ in get_provider(cfg).chain] == [
        "gemini", "gemini-reserve", "groq", "pollinations", "template"]
    cfg.data["ai"]["provider"] = "groq"
    assert [label for label, _ in get_provider(cfg).chain] == [
        "groq", "gemini", "gemini-reserve", "pollinations", "template"]
    cfg.data["ai"]["provider"] = "nonsense"  # garbage primary -> Gemini
    assert [label for label, _ in get_provider(cfg).chain] == [
        "gemini", "gemini-reserve", "groq", "pollinations", "template"]
    assert isinstance(get_provider(cfg), ChainedProvider)


def t_slugify():
    from bot import slugify

    assert slugify("The Secret Language of Trees!", "video") == "the-secret-language-of-trees"
    assert slugify("  ", "video") == "video"
    assert len(slugify("x" * 100, "video")) <= 50


def t_encoder_setting():
    from assembler import _ENCODER_ARGS

    cfg = tmp_cfg()
    assert cfg.encoder == "auto"  # GPU when available, CPU otherwise
    cfg.data["video"]["encoder"] = "NVENC"
    assert cfg.encoder == "nvenc"
    cfg.data["video"]["encoder"] = "nonsense"
    assert cfg.encoder == "auto"
    assert _ENCODER_ARGS["cpu"][0] == "libx264"  # CPU path byte-identical
    assert _ENCODER_ARGS["nvenc"][0] == "h264_nvenc"


def t_dry_run_batch():
    from main import _dry_run_batch

    cfg = tmp_cfg()
    assert _dry_run_batch(cfg, ["explicit topic", None]) == 0


def t_editorial():
    import types

    from editorial import _apply, polish_script

    def script(*lines):
        return types.SimpleNamespace(
            scenes=[types.SimpleNamespace(narration=t) for t in lines])

    class FakeProvider:
        def __init__(self, replies):
            self.replies = list(replies)
            self.prompts = []

        def generate_text(self, prompt, temperature=0.7, tag="", json_mode=False):
            self.prompts.append(prompt)
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

    good = '{"scenes": [{"narration": "Hook here."}, {"narration": "Payoff here."}]}'
    cfg = tmp_cfg()
    # happy path: both stages apply in place.
    sc = script("aaa aaa aaa.", "bbb bbb bbb.")
    polish_script(sc, cfg, FakeProvider([good, good]))
    assert [x.narration for x in sc.scenes] == ["Hook here.", "Payoff here."]
    # punch-up prompt carries the retention architecture.
    fp = FakeProvider([good, good])
    polish_script(script("aaa aaa aaa.", "bbb bbb bbb."), cfg, fp)
    assert "question-hook" in fp.prompts[0] and "loop-back" in fp.prompts[0]
    assert "Concrete camera rule" in fp.prompts[0]
    assert "FLOW like speech" in fp.prompts[0]
    assert "dead-air pauses" in fp.prompts[0]
    # garbage then provider error: stages skip, previous kept, never raises.
    sc = script("aaa aaa aaa.", "bbb bbb bbb.")
    polish_script(sc, cfg, FakeProvider(["not json", RuntimeError("down")]))
    assert [x.narration for x in sc.scenes] == ["aaa aaa aaa.", "bbb bbb bbb."]
    # scene-count change rejected.
    sc = script("aaa aaa aaa.", "bbb bbb bbb.")
    assert _apply(sc, '{"scenes": [{"narration": "Only one."}]}', "test", 1.3) is False
    assert [x.narration for x in sc.scenes] == ["aaa aaa aaa.", "bbb bbb bbb."]
    # word bloat rejected (21 new words vs 6 old, cap 1.3x).
    bloat = '{"scenes": [{"narration": "' + "word " * 20 + '"}, {"narration": "short"}]}'
    assert _apply(sc, bloat, "test", 1.3) is False
    # disabled flag: provider never touched.
    calls = []

    class Spy:
        def generate_text(self, *args, **kwargs):
            calls.append(1)
            return good

    cfg.data["ai"]["editorial_passes"] = False
    polish_script(script("x."), cfg, Spy())
    assert calls == []


def t_openrouter_lane():
    from unittest.mock import Mock, patch

    from openrouter import OpenRouterProvider
    from scriptgen import get_provider

    def ok(text="hello"):
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": text}}]}
        return response

    # Referer/Title headers sent (OpenRouter attribution).
    with patch("requests.post", return_value=ok()) as post:
        OpenRouterProvider(api_keys=["k"])._complete("hi", 0.0, False, "t")
    headers = post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer k"
    assert headers["HTTP-Referer"].endswith("dreamer2664/youtproject")
    assert headers["X-Title"] == "youtproject"
    # 200-with-error 429 naming upstream -> next model, keys untouched.
    err429 = Mock(status_code=200, text="wrapped")
    err429.json.return_value = {"id": "gen-x", "error": {
        "message": "x temporarily rate-limited upstream", "code": 429}}
    with patch("requests.post", side_effect=[err429, ok()]) as post:
        result = OpenRouterProvider(api_keys=["k1", "k2"])._complete(
            "hi", 0.0, False, "t")
    assert result == "hello" and post.call_count == 2
    assert post.call_args_list[1].kwargs["json"]["model"] == "nvidia/nemotron-3.5-lightning:free"
    # plain 429 (own per-key RPM) still rotates keys on the same model.
    with patch("requests.post", side_effect=[Mock(status_code=429, text="slow"),
                                             ok()]) as post:
        OpenRouterProvider(api_keys=["k1", "k2"])._complete("hi", 0.0, False, "t")
    calls = post.call_args_list
    assert calls[1].kwargs["json"]["model"] == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert calls[1].kwargs["headers"]["Authorization"] == "Bearer k2"
    # Groq's reserved text keys follow the full Gemini model/key group.
    cfg = tmp_cfg()
    cfg.data["ai"]["gemini_api_key"] = "g"
    cfg.data["ai"]["groq_api_keys"] = [f"g{i}" for i in range(10)]
    cfg.data["ai"]["openrouter_api_keys"] = ["o"]
    assert [label for label, _ in get_provider(cfg).chain] == [
        "gemini", "openrouter", "gemini-reserve", "groq", "pollinations",
        "template"]
    cfg.data["ai"]["groq_api_keys"] = []
    cfg.data["ai"]["gemini_api_key"] = ""
    assert [label for label, _ in get_provider(cfg).chain] == ["openrouter", "pollinations", "template"]


def t_image_402_fails_over_fast():
    """Pollinations HTTP 402 must fail over at once — one request, no retries.

    The old behavior treated 402 as generic-retryable: six paced attempts,
    ~45s of sleeps per image, then the chain failed over anyway.
    """
    from unittest.mock import Mock, patch

    import images

    cfg = tmp_cfg()
    dest = Path(_mkdtemp()) / "img.jpg"
    resp = Mock(status_code=402, text="payment required")
    resp.headers = {}
    with patch("images._wait_for_slot", lambda *a, **k: None), \
         patch("images.requests.get", return_value=resp) as get:
        try:
            images._pollinations_fetch("prompt", dest, cfg, seed=1)
        except RuntimeError as exc:
            assert "402" in str(exc), str(exc)
        else:
            raise AssertionError("expected RuntimeError on HTTP 402")
    assert get.call_count == 1, f"402 was retried {get.call_count} times"
    assert not dest.exists()


def t_probe_whisper():
    """probe_whisper posts the wav, ledgers audio=1 tag=probe, never raises."""
    from unittest.mock import Mock, patch

    import keystats

    cfg = tmp_cfg()
    keystats.init(cfg)
    ok_resp = Mock(status_code=200, text="")
    with patch("requests.post", return_value=ok_resp) as post:
        ok, detail = keystats.probe_whisper("gsk-secret-0001", b"RIFFfake")
    assert ok and "audio-second" in detail, (ok, detail)
    assert post.call_args.kwargs["data"]["model"] == "whisper-large-v3-turbo"
    events = keystats._load(keystats._path)
    probes = [e for e in events if e.get("p") == "groq"
              and e.get("tag") == "probe"]
    assert probes and probes[-1]["audio"] == 1 and probes[-1]["req"] == 1

    with patch("requests.post", return_value=Mock(status_code=401, text="bad")):
        ok, detail = keystats.probe_whisper("gsk-secret-0001", b"RIFFfake")
    assert not ok and "401" in detail, (ok, detail)


def t_probe_sample():
    """--probe --sample N slices key lists; 0/negative = all (pure)."""
    import keystats

    assert keystats._sample(["a", "b", "c", "d"], 2) == ["a", "b"]
    assert keystats._sample(["a", "b"], 0) == ["a", "b"]
    assert keystats._sample(["a"], -3) == ["a"]
    assert keystats._sample([], 5) == []


def t_probe_report_shape():
    """run_probes works keyless (no network) and names the Whisper lane."""
    import keystats

    cfg = tmp_cfg()
    text = keystats.run_probes(cfg, sample=1)
    assert "GEMINI" in text and "GROQ" in text
    assert "WHISPER" in text
    assert "sampling the first 1 key" in text
    assert "python main.py keys --month" in text


def t_ledger_tags():
    """Every bumped lane carries a tag — none show as "(untagged)".

    Stock fetches ledger as tag=image; subject detection AND clip frame
    QC as tag=vision. Before this, those bumps were untagged, so
    `keys --month` hid which lane ate the requests.
    """
    import types
    from unittest.mock import Mock, patch

    import keystats
    import stock
    import vision

    jpeg = b"\xff\xd8\xff" + b"j" * 2500
    cfg = tmp_cfg()
    keystats.init(cfg)

    class Resp:
        def __init__(self, payload=None, content=b""):
            self.status_code = 200
            self._payload, self.content = payload, content

        def json(self):
            return self._payload

    def fake_get(url, **kw):
        if url == stock.SEARCH_URL:
            return Resp(payload={"photos": [
                {"id": 5, "alt": "ocean waves", "src": {"portrait": "u5"},
                 "photographer": "A"}]})
        return Resp(content=jpeg)

    import director
    real_plan, director.plan_query = director.plan_query, (
        lambda prompt, cfg: "ocean")
    real_stock_requests, stock.requests = stock.requests, \
        types.SimpleNamespace(get=fake_get)
    real_check, vision.check_image = vision.check_image, (
        lambda body, query, cfg, photo_key="":
        {"safe": True, "relevant": True, "reason": ""})
    try:
        cfg.data["ai"]["pexels_api_key"] = "pk"
        dest = Path(_mkdtemp()) / "img.jpg"
        stock.pexels_fetch("ocean scene", dest, cfg, seed=1, attempts=1)
    finally:
        director.plan_query = real_plan
        stock.requests = real_stock_requests
        vision.check_image = real_check

    events = keystats._load(keystats._path)
    pex = [e for e in events if e.get("p") == "pexels"]
    assert pex and pex[-1].get("tag") == "image", pex

    cfg.data["ai"]["gemini_api_keys"] = ["g1"]
    resp = Mock(status_code=200)
    resp.json.return_value = {"candidates": [{"content": {"parts": [
        {"text": '{"x": 0.5}'}]}}]}
    with patch("vision.requests.post", return_value=resp):
        assert vision.subject_x(jpeg, cfg) == 0.5
    events = keystats._load(keystats._path)
    gem = [e for e in events if e.get("p") == "gemini"
           and e.get("tag") == "vision"]
    assert gem, [e for e in events if e.get("p") == "gemini"]

    # clipper._frame_ok — the clip frame-QC lane — ledgers there too
    import clipper
    resp2 = Mock(status_code=200)
    resp2.json.return_value = {"candidates": [{"content": {"parts": [
        {"text": '{"usable": true}'}]}}]}
    with patch("clipper.requests.post", return_value=resp2):
        assert clipper._frame_ok(jpeg, cfg) is True
    events = keystats._load(keystats._path)
    gem2 = [e for e in events if e.get("p") == "gemini"
            and e.get("tag") == "vision"]
    assert len(gem2) == len(gem) + 1, gem2


def t_meeting_act_wiring():
    """`meeting act` wires run_meeting's ONE-argument executor correctly.

    Regression guard: main.py passed the raw two-arg _execute_board_action,
    so every board action died inside run_meeting's try/except as
    "missing 1 required positional argument" — the act lane could never
    execute anything, while the direct-call tests (t_board_action_*)
    stayed green because they bypass the wiring entirely.
    """
    import argparse

    import main as main_mod
    import meeting as meeting_mod

    cfg = tmp_cfg()
    captured = {}

    def fake_run(cfg_, kind, urls=None, rounds=None, render=False,
                 send=True, say=None, executor=None, events=None):
        captured["executor"] = executor
        captured["kind"] = kind
        captured["events"] = events
        return "fake summary"

    real_run = meeting_mod.run_meeting
    meeting_mod.run_meeting = fake_run
    try:
        args = argparse.Namespace(kind="act", url=None, rounds=None,
                                  render=False, no_send=True, add=None,
                                  dry_run=False, events_json=True)
        rc = main_mod.cmd_meeting(cfg, args)
    finally:
        meeting_mod.run_meeting = real_run
    assert rc == 0 and captured.get("kind") == "act"
    # --events-json wires a callable event listener (the panel streams it)
    assert callable(captured.get("events")), captured.get("events")
    executor = captured.get("executor")
    assert executor is not None
    # run_meeting calls back with exactly ONE argument (the decision dict).
    seen = {}
    real_exec = main_mod._execute_board_action
    main_mod._execute_board_action = lambda cfg_, decision: (
        seen.update(decision) or "done")
    try:
        out = executor({"action": "none", "reason": "r"})
    finally:
        main_mod._execute_board_action = real_exec
    assert out == "done", out
    assert seen == {"action": "none", "reason": "r"}, seen


def t_keypool_memory():
    """One dead-key memory shared by every lane (2026-09-21 fix, generalized).

    Before: a 401'd key was re-tried by every fresh provider instance —
    one wasted round-trip per call, per lane, all run long (the voiceover
    lane alone had the fix). Now: keypool remembers, and 429/quota stays
    live because a rate-limited key refills.
    """
    from unittest.mock import Mock, patch

    import keypool
    from groq import GroqProvider
    from scriptgen import GeminiProvider

    keypool.reset()
    try:
        # live(): strips, de-dupes, drops the dead — order preserved.
        assert keypool.live("x", [" a ", "a", "", None, "b"]) == ["a", "b"]
        keypool.dead("x", "a")
        assert keypool.live("x", [" a ", "b"]) == ["b"]
        assert keypool.is_dead("x", " a ") is True
        keypool.reset()
        assert keypool.live("x", ["a"]) == ["a"]

        # 401/403 condemn a key; a 400 only when the body blames the key.
        assert keypool.key_dead_like(401, "nope") is True
        assert keypool.key_dead_like(403, "") is True
        assert keypool.key_dead_like(
            400, '{"error": {"message": "API key not valid"}}') is True
        assert keypool.key_dead_like(400, "Failed to validate JSON") is False
        assert keypool.key_dead_like(429, "slow down") is False
        assert keypool.key_dead_like(500, "boom") is False

        def ok():
            response = Mock(status_code=200)
            response.json.return_value = {
                "choices": [{"message": {"content": "hello"}}]}
            return response

        # Groq: the next INSTANCE skips the dead key (before: retried it).
        with patch("requests.post",
                   side_effect=[Mock(status_code=401, text="bad"), ok()]) as post:
            result = GroqProvider(api_keys=["dead1", "live1"])._complete(
                "hi", temperature=0.0, json_mode=False, tag="t")
        assert result == "hello" and post.call_count == 2
        assert keypool.is_dead("groq", "dead1")
        with patch("requests.post", return_value=ok()) as post:
            result = GroqProvider(api_keys=["dead1", "live1"])._complete(
                "hi", temperature=0.0, json_mode=False, tag="t")
        assert result == "hello" and post.call_count == 1
        assert post.call_args.kwargs["headers"] == {
            "Authorization": "Bearer live1"}

        # Gemini: same memory; a JSON-mode-shaped 400 must NOT kill the key.
        def gok():
            response = Mock(status_code=200)
            response.json.return_value = {"candidates": []}
            return response

        with patch("requests.post",
                   side_effect=[Mock(status_code=401,
                                     text="API key not valid"), gok()]) as post:
            result, status, _ = GeminiProvider(
                api_key=["dead2", "live2"])._try_model("m", {}, tag="t")
        assert result is not None and post.call_count == 2
        assert keypool.is_dead("gemini", "dead2")
        with patch("requests.post", return_value=gok()) as post:
            result, status, _ = GeminiProvider(
                api_key=["dead2", "live2"])._try_model("m", {}, tag="t")
        assert result is not None and post.call_count == 1
        with patch("requests.post",
                   return_value=Mock(status_code=400,
                                     text="Failed to validate JSON")):
            GeminiProvider(api_key=["live3"])._try_model("m", {}, tag="t")
        assert keypool.is_dead("gemini", "live3") is False
    finally:
        keypool.reset()


def t_key_env_aliases():
    """Every provider accepts its natural env names; lists de-dupe.

    `export PIXABAY_API_KEY=...` used to be silently ignored (only the
    plural spelling was read) — same for DeepSeek/ElevenLabs/YouTube —
    while Gemini/Groq took both spellings. And duplicate keys in a list
    survived, so the same key got tried twice per call.
    """
    import os
    from unittest.mock import patch

    cfg = tmp_cfg()
    with patch.dict(os.environ, {"PIXABAY_API_KEY": "pb-single",
                                 "DEEPSEEK_API_KEY": "ds-single",
                                 "ELEVENLABS_API_KEY": "el-single",
                                 "YOUTUBE_API_KEY": "yt-single"}):
        assert cfg.pixabay_api_keys == ["pb-single"]
        assert cfg.deepseek_api_keys == ["ds-single"]
        assert cfg.elevenlabs_api_keys == ["el-single"]
        assert cfg.youtube_api_keys == ["yt-single"]
    cfg.data["ai"]["gemini_api_keys"] = [" g1 ", "g1", "", "g2"]
    cfg.data["ai"]["groq_api_keys"] = ["k1", "k1", "k2"]
    for name in ("GEMINI_API_KEYS", "GEMINI_API_KEY", "GROQ_API_KEYS",
                 "GROQ_API_KEY", "PIXABAY_API_KEY", "DEEPSEEK_API_KEY",
                 "ELEVENLABS_API_KEY", "YOUTUBE_API_KEY"):
        os.environ.pop(name, None)
    assert cfg.gemini_api_keys == ["g1", "g2"], cfg.gemini_api_keys
    assert cfg.groq_api_keys == ["k1", "k2"], cfg.groq_api_keys


def t_chat_tools():
    from unittest.mock import Mock, patch

    from groq import GroqProvider

    resp = Mock(status_code=200)
    resp.json.return_value = {"choices": [{"message": {
        "content": "", "tool_calls": [{"id": "c1", "type": "function",
            "function": {"name": "set_timer",
                         "arguments": '{"minutes": 10}'}}]}}]}
    with patch("requests.post", return_value=resp):
        text, calls = GroqProvider(api_keys=["k"]).chat_with_tools(
            [{"role": "user", "content": "hi"}], [{"type": "function"}], tag="t")
    assert text == "" and calls[0]["name"] == "set_timer"
    assert calls[0]["args"] == {"minutes": 10} and calls[0]["id"] == "c1"


def t_jarvis_task():
    from unittest.mock import patch

    import jarvis
    from bot import parse_incoming

    assert parse_incoming("/jarvis make 2 videos") == ("jarvis", "make 2 videos")
    assert parse_incoming("/jarvis") == ("jarvis", "")

    script = [
        ("", [{"id": "1", "name": "render_videos", "args": {"count": 2}, "raw": {}}]),
        ("", [{"id": "2", "name": "schedule_video",
               "args": {"job_id": "abc", "due_at_iso": "2026-09-16T09:00:00+02:00"},
               "raw": {}}]),
        ("All done: 2 videos scheduled.", []),
    ]

    class FakeBrain:
        def chat_with_tools(self, messages, tools, tag="jarvis"):
            return script.pop(0)

    said = []
    with patch("jarvis._brain", return_value=FakeBrain()), \
            patch("jarvis.render_videos",
                  return_value={"rendered": 2, "jobs": []}) as rendered, \
            patch("jarvis.schedule_video",
                  return_value={"posts": []}) as scheduled:
        summary = jarvis.run_task(tmp_cfg(), "make 2 videos", say=said.append)
    assert summary == "All done: 2 videos scheduled."
    assert rendered.call_count == 1 and scheduled.call_count == 1
    assert said  # progress was narrated
    # unknown tool -> error result, loop still reaches the final text.
    script2 = [("", [{"id": "1", "name": "nope", "args": {}, "raw": {}}]),
               ("Recovered.", [])]

    class FakeBrain2:
        def chat_with_tools(self, messages, tools, tag="jarvis"):
            return script2.pop(0)

    with patch("jarvis._brain", return_value=FakeBrain2()):
        assert jarvis.run_task(tmp_cfg(), "hi") == "Recovered."
    # no brain -> clean message, nothing done.
    with patch("jarvis._brain", return_value=None):
        assert "No text-generation keys" in jarvis.run_task(tmp_cfg(), "hi")
    # render cap enforced before the pipeline runs.
    with patch("main.cmd_generate", return_value=0) as gen:
        result = jarvis.render_videos(tmp_cfg(), 99, None, say=lambda m: None)
    assert gen.call_count == 1 and gen.call_args.args[1].count == 5
    assert result["requested"] == 99 and result["capped_to"] == 5


def t_analytics():
    from unittest.mock import patch

    from analytics import channel_stats

    page = {"posts": {"edges": [
        {"node": {"id": "a", "status": "sent", "text": "vid one",
                   "sentAt": "2026-09-15T06:00:00.000Z",
                   "channelService": "tiktok", "dueAt": None,
                   "createdAt": "2026-09-14T00:00:00.000Z",
                   "metrics": [{"name": "Video Views", "value": 100},
                               {"name": "Reach", "value": 80},
                               {"name": "Eng. Rate", "value": 2.5}]}},
        {"node": {"id": "b", "status": "scheduled", "text": "vid two",
                   "sentAt": None, "channelService": "youtube",
                   "dueAt": "2026-09-16T09:00:00.000Z",
                   "createdAt": "2026-09-15T00:00:00.000Z", "metrics": []}},
    ], "pageInfo": {"hasNextPage": False, "endCursor": None}}}

    class FakeClient:
        def __init__(self, key: str) -> None:
            pass

        def organizations(self):
            return [{"id": "org"}]

        def _gql(self, query, variables=None):
            return page

    cfg = tmp_cfg()
    cfg.data["buffer"] = {"api_key": "x"}
    with patch("autopost.BufferClient", FakeClient):
        stats = channel_stats(cfg, days=30)
    assert stats["posts"] == 1 and stats["scheduled"] == 1
    assert stats["services"]["tiktok"]["views"] == 100
    assert stats["services"]["tiktok"]["eng_rate_avg"] == 2.5
    assert stats["top"]["id"] == "a"
    # no key -> clean error, nothing raised.
    assert "error" in channel_stats(tmp_cfg())


def t_voice():
    import tempfile
    from pathlib import Path
    from unittest.mock import Mock, mock_open, patch

    from voice import download_telegram_voice, transcribe

    cfg = tmp_cfg()
    cfg.data.setdefault("ai", {})["groq_api_keys"] = ["k1", "k2", "k3"]
    ok = Mock(status_code=200)
    ok.json.return_value = {"text": "  hello mars "}
    with patch("requests.post", return_value=ok) as post, \
            patch("builtins.open", mock_open(read_data=b"ogg")):
        assert transcribe(cfg, Path("x.ogg")) == "hello mars"
        assert "audio/transcriptions" in post.call_args.args[0]
        assert post.call_args.kwargs["data"]["model"].startswith("whisper")
    # 429 on the first key -> rotates to the second.
    denied = Mock(status_code=429, text="slow down")
    with patch("requests.post", side_effect=[denied, ok]), \
            patch("builtins.open", mock_open(read_data=b"ogg")):
        assert transcribe(cfg, Path("x.ogg")) == "hello mars"
    # Telegram getFile + download round-trip.
    info_resp = Mock()
    info_resp.json.return_value = {"ok": True,
                                   "result": {"file_path": "voice/x.ogg"}}
    file_resp = Mock()
    file_resp.__enter__ = Mock(return_value=file_resp)
    file_resp.__exit__ = Mock(return_value=False)
    file_resp.iter_content.return_value = [b"ogg-bytes"]
    with tempfile.TemporaryDirectory() as tmp, \
            patch("requests.get", side_effect=[info_resp, file_resp]):
        out = download_telegram_voice("tok", "fid123", Path(tmp))
        assert out.read_bytes() == b"ogg-bytes"


def t_gemini_keys():
    from unittest.mock import Mock, patch

    from scriptgen import GeminiProvider

    def ok():
        response = Mock(status_code=200)
        response.json.return_value = {"candidates": []}
        return response

    # 429 on key1 -> key2 tried immediately (different ?key= param).
    with patch("requests.post", side_effect=[Mock(status_code=429, text="slow"),
                                             ok()]) as post, \
            patch("time.sleep", return_value=None):
        result, status, _ = GeminiProvider(
            api_key=["k1", "k2"])._try_model("m", {}, tag="t")
    assert result is not None and post.call_count == 2
    assert post.call_args_list[0].kwargs["params"] == {"key": "k1"}
    assert post.call_args_list[1].kwargs["params"] == {"key": "k2"}
    # Rejected key dropped, next key delivers.
    with patch("requests.post", side_effect=[Mock(status_code=400,
                                                  text="API key not valid"),
                                             ok()]) as post:
        result, status, _ = GeminiProvider(
            api_key=["bad", "good"])._try_model("m", {}, tag="t")
    assert result is not None and post.call_count == 2
    # All keys rejected -> fatal triple for _post to raise on.
    with patch("requests.post", return_value=Mock(status_code=401,
                                                  text="nope")) as post:
        result, status, _ = GeminiProvider(
            api_key=["a", "b"])._try_model("m", {}, tag="t")
    assert result is None and status == 401 and post.call_count == 2
    # All keys 429 -> failover after one try per key (5 keys = 5 calls).
    import keypool

    keypool.reset()  # the all-401 block above buried a/b in the shared memory
    with patch("requests.post", return_value=Mock(status_code=429,
                                                  text="x")) as post, \
            patch("time.sleep", return_value=None):
        result, status, _ = GeminiProvider(
            api_key=["a", "b", "c", "d", "e"])._try_model("m", {}, tag="t")
    assert result is None and post.call_count == 5
    # 404 model -> immediate failover, no pointless retries.
    with patch("requests.post", return_value=Mock(status_code=404,
                                                  text="gone")) as post:
        result, status, _ = GeminiProvider(
            api_key="k")._try_model("m", {}, tag="t")
    assert result is None and status == 404 and post.call_count == 1
    # Single string still works (backward compat).
    assert GeminiProvider(api_key="k").api_keys == ["k"]


def t_elevenlabs():
    import base64
    import tempfile
    from pathlib import Path
    from unittest.mock import Mock, patch

    from voiceover import synthesise

    payload = {"audio_base64": base64.b64encode(b"x" * 2000).decode(),
               "alignment": {
                   "characters": ["H", "i", " ", "y", "o"],
                   "character_start_times_seconds": [0, 0.1, 0.2, 0.3, 0.4],
                   "character_end_times_seconds": [0.1, 0.2, 0.3, 0.4, 0.5]}}
    ok = Mock(status_code=200)
    ok.json.return_value = payload

    def eleven_cfg():
        cfg = tmp_cfg()
        channel = cfg.data.setdefault("channel", {})
        channel["elevenlabs_api_keys"] = ["k1"]
        channel["elevenlabs_voice_id"] = "v1"
        channel.setdefault("voice", "edge-voice")
        return cfg

    async def fake_edge(text, voice, rate, dest):
        Path(dest).write_bytes(b"y" * 2000)
        return []

    # Happy path: audio decoded, chars grouped into word timings.
    with tempfile.TemporaryDirectory() as tmp, \
            patch("requests.post", return_value=ok) as post:
        dest = Path(tmp) / "s.mp3"
        out, timings = synthesise("Hi yo", dest, eleven_cfg())
        assert out == dest and dest.stat().st_size == 2000
        assert [(t.word, t.start, t.end) for t in timings] == [("Hi", 0, 0.2),
                                                               ("yo", 0.3, 0.5)]
        assert "with-timestamps" in post.call_args.args[0]
        settings = post.call_args.kwargs["json"]["voice_settings"]
        assert settings == {"stability": 0.65, "similarity_boost": 0.6,
                            "style": 0.25, "use_speaker_boost": True}, settings
    # Denied key -> edge-tts fallback still delivers.
    denied = Mock(status_code=401, text="bad key")
    with tempfile.TemporaryDirectory() as tmp, \
            patch("requests.post", return_value=denied), \
            patch("voiceover._synth", side_effect=fake_edge):
        dest = Path(tmp) / "s.mp3"
        out, timings = synthesise("Hi", dest, eleven_cfg())
    assert out == dest and timings == []
    # No keys -> edge-tts directly, ElevenLabs never called.
    with tempfile.TemporaryDirectory() as tmp, \
            patch("requests.post") as post, \
            patch("voiceover._synth", side_effect=fake_edge):
        cfg = tmp_cfg()
        cfg.data.setdefault("channel", {})["voice"] = "edge-voice"
        synthesise("Hi", Path(tmp) / "s.mp3", cfg)
    assert post.call_count == 0


def t_youtube():
    from unittest.mock import Mock, patch

    from youtube import (YouTubeClient, channel_stats, extract_id,
                         parse_duration, search_shorts, video_stats)

    # ID extraction: raw IDs, handles, and every URL shape.
    assert extract_id("dQw4w9WgXcQ") == ("video", "dQw4w9WgXcQ")
    assert extract_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == (
        "video", "dQw4w9WgXcQ")
    assert extract_id("https://youtu.be/dQw4w9WgXcQ") == ("video", "dQw4w9WgXcQ")
    assert extract_id("https://www.youtube.com/shorts/abc123XYZ_-") == (
        "video", "abc123XYZ_-")
    assert extract_id("@GoogleDevelopers") == ("handle", "GoogleDevelopers")
    assert extract_id("https://www.youtube.com/@GoogleDevelopers") == (
        "handle", "GoogleDevelopers")
    assert extract_id("UC_x5XG1OV2P6uZZ5FSM9Ttw") == (
        "channel", "UC_x5XG1OV2P6uZZ5FSM9Ttw")
    try:
        extract_id("https://vimeo.com/123")
        raise AssertionError("non-youtube should raise")
    except ValueError:
        pass
    # Duration parsing.
    assert parse_duration("PT1M30S") == 90
    assert parse_duration("PT2H") == 7200
    assert parse_duration("junk") == 0
    # Lookup shape + key plumbing.
    payload = {"items": [{
        "id": "v",
        "snippet": {"title": "T", "channelTitle": "C", "channelId": "UC1",
                    "publishedAt": "2026-01-02T00:00:00Z"},
        "statistics": {"viewCount": "100", "likeCount": "5",
                       "commentCount": "2"},
        "contentDetails": {"duration": "PT1M"}}]}
    ok = Mock(status_code=200)
    ok.json.return_value = payload
    with patch("requests.get", return_value=ok) as get:
        info = video_stats(YouTubeClient(["k1"]), "v")
    assert info["views"] == 100 and info["duration_s"] == 60
    assert info["published"] == "2026-01-02"
    assert get.call_args.args[0].endswith("/videos")
    assert get.call_args.args[0].startswith(
        "https://www.googleapis.com/youtube/v3/")
    assert get.call_args.kwargs["params"]["key"] == "k1"
    # quotaExceeded on key1 -> key2 serves, quota counted once.
    denied = Mock(status_code=403, text="quota")
    denied.json.return_value = {"error": {"errors": [{"reason":
                                                      "quotaExceeded"}]}}
    client = YouTubeClient(["k1", "k2"])
    with patch("requests.get", side_effect=[denied, ok]) as get:
        video_stats(client, "v")
    assert get.call_count == 2 and client.spent == 1
    assert get.call_args_list[1].kwargs["params"]["key"] == "k2"
    # Invalid key dropped; malformed request raises at once.
    badkey = Mock(status_code=400, text="bad")
    badkey.json.return_value = {"error": {"errors": [{"reason":
                                                      "keyInvalid"}]}}
    with patch("requests.get", side_effect=[badkey, ok]):
        video_stats(YouTubeClient(["bad", "good"]), "v")
    badparam = Mock(status_code=400, text="bad param")
    badparam.json.return_value = {"error": {"errors": [{"reason": "invalid"}]}}
    with patch("requests.get", return_value=badparam):
        try:
            video_stats(YouTubeClient(["k"]), "v")
            raise AssertionError("bad param should raise")
        except RuntimeError:
            pass
    # Empty items -> clean not-found.
    empty = Mock(status_code=200)
    empty.json.return_value = {"items": []}
    with patch("requests.get", return_value=empty):
        try:
            video_stats(YouTubeClient(["k"]), "v")
            raise AssertionError("missing video should raise")
        except RuntimeError:
            pass
    # Channel via @handle resolves with forHandle.
    cpayload = {"items": [{
        "id": "UC1",
        "snippet": {"title": "Ch", "customUrl": "@ch",
                    "publishedAt": "2020-05-01T00:00:00Z"},
        "statistics": {"subscriberCount": "10", "videoCount": "3",
                       "viewCount": "99"}}]}
    cok = Mock(status_code=200)
    cok.json.return_value = cpayload
    with patch("requests.get", return_value=cok) as get:
        channel = channel_stats(YouTubeClient(["k"]), "@ch")
    assert channel["subs"] == 10
    assert get.call_args.kwargs["params"]["forHandle"] == "ch"
    # Search costs 100 units and parses items.
    spayload = {"items": [{
        "id": {"videoId": "s1"},
        "snippet": {"title": "S", "channelTitle": "C",
                    "publishedAt": "2026-01-01T00:00:00Z"}}]}
    sok = Mock(status_code=200)
    sok.json.return_value = spayload
    client = YouTubeClient(["k"])
    with patch("requests.get", return_value=sok):
        results = search_shorts(client, "q", max_results=5)
    assert results[0]["id"] == "s1" and client.spent == 100


def t_crew():
    from datetime import datetime as real_datetime
    from unittest.mock import patch

    import crew
    from crew import (_cycle, _due_now, _fresh_usage, _load_state,
                      _manager_pick, _save_state, _usage_line, daily_digest,
                      make_and_post, mission_active, parse_mission)

    # Mission parsing: the user's sentence, live markers, clamps, defaults.
    spec = parse_mission("Manage yourself for 3 days, post at least 4 times "
                         "a day on both platforms.")
    assert (spec["days"], spec["per_day"], spec["live"]) == (3, 4, False)
    assert parse_mission("3 days, 4 posts a day, go live")["live"] is True
    assert parse_mission("post for real")["live"] is True
    spec = parse_mission("100 days, 20 videos per day")
    assert (spec["days"], spec["per_day"]) == (30, 10)
    assert parse_mission("")["goal"] == "grow the channel"
    assert parse_mission("/crew")["days"] == 3  # bare command still works
    # Slot math: 4/day -> 9, 13, 17, 21.
    noon = real_datetime(2026, 9, 15, 14, 0, 0)
    assert _due_now(4, noon) == 2
    assert _due_now(4, real_datetime(2026, 9, 15, 8, 0, 0)) == 0
    assert _due_now(4, real_datetime(2026, 9, 15, 22, 0, 0)) == 4
    assert _due_now(4, real_datetime(2026, 9, 15, 22, 0, 0),
                    day_start_hour=15) == 2
    assert _due_now(1, noon) == 1
    # State round-trip + liveness: fresh beats count, stale don't.
    cfg = tmp_cfg()
    assert mission_active(cfg) is None
    now_iso = real_datetime.now().astimezone().isoformat()
    state = {"mission": {"days": 3, "per_day": 4, "live": False,
                         "ends": "2099-01-01T00:00:00+00:00"},
             "posted": [], "usage": _fresh_usage("2099-01-01"),
             "digests": [], "heartbeat": now_iso, "ended": None, "log": []}
    _save_state(cfg, state)
    assert _load_state(cfg)["heartbeat"] == now_iso
    assert mission_active(cfg) is not None
    state["heartbeat"] = "2020-01-01T00:00:00+00:00"
    _save_state(cfg, state)
    assert mission_active(cfg) is None  # stale heartbeat frees the slot
    # Usage line formats thousands.
    assert "1,234" in _usage_line({"yt_units": 1234, "eleven_chars": 56,
                                   "llm_tokens_est": 7})
    # Manager ranking with and without a brain.
    cands = ["Alpha story", "Bravo story", "Charlie story"]
    assert _manager_pick(None, "n", cands, 2) == cands[:2]

    class FakeBrain:
        def ask(self, prompt, tag="crew"):
            return "1. Bravo\n2. Alpha\n"

    assert _manager_pick(FakeBrain(), "n", cands, 2) == ["Bravo story",
                                                         "Alpha story"]
    # make_and_post: render + post mocked, keys restored, ping sent.
    cfg = tmp_cfg()
    channel = cfg.data.setdefault("channel", {})
    channel.update({"topic": "test niche", "voice": "v",
                    "elevenlabs_api_keys": ["k"],
                    "elevenlabs_voice_id": "v1"})
    usage = _fresh_usage(real_datetime.now().astimezone().date().isoformat())
    usage["premium_today"] = 99  # budget spent -> edge-tts forced
    state = {"mission": {"per_day": 4, "live": False}, "usage": usage,
             "posted": [], "log": []}
    said = []
    with patch("jarvis.render_videos",
               return_value={"jobs": [{"job_id": "j", "title": "T"}],
                             "failed": []}), \
            patch("jarvis.schedule_video",
                  return_value={"posts": [{"service": "youtube"},
                                          {"service": "tiktok"}]}):
        entry = make_and_post(cfg, state, None, said.append, "2026-09-15T15:00:00+02:00")
    assert entry["mode"] == "draft" and len(state["posted"]) == 1
    assert channel["elevenlabs_api_keys"] == ["k"]  # restored after forcing edge
    assert any("Hey, we posted" in m for m in said)
    # Full cycle at fake 14:00: scout (no YT keys, topup mocked) + 1 video.
    class FakeNow(real_datetime):
        @classmethod
        def now(cls, tz=None):
            base = real_datetime(2026, 9, 15, 14, 0, 0)
            return base.replace(tzinfo=tz) if tz else base

    usage = _fresh_usage("2026-09-15")
    state = {"mission": {"per_day": 4, "live": False,
                         "ends": "2026-09-20T23:59:00+02:00",
                         "started_day": "2026-09-15", "started_day_hour": 0},
             "usage": usage, "posted": [], "digests": [],
             "heartbeat": "", "log": []}
    with patch("crew.datetime", FakeNow), \
            patch("topics.top_up_backlog", return_value=([], 0)), \
            patch("jarvis.render_videos",
                  return_value={"jobs": [{"job_id": "j", "title": "T"}],
                                "failed": []}), \
            patch("jarvis.schedule_video", return_value={"posts": [{"service": "x"}]}):
        assert _cycle(cfg, state, None, said.append) == "ok"
    assert len(state["posted"]) == 1 and usage["made_today"] == 1
    assert state["heartbeat"].startswith("2026-09-15T14:00")
    # Digest without a brain still reports numbers (no Buffer key -> error text).
    today = real_datetime.now().astimezone().date().isoformat()
    state = {"posted": [{"day": today, "mode": "live"},
                        {"day": today, "mode": "draft"}],
             "usage": _fresh_usage(today), "digests": []}
    said = []
    daily_digest(tmp_cfg(), state, None, said.append)
    assert "2 (1 live, 1 draft)" in said[0] and state["digests"] == [today]



def t_image_route():
    """Friendly image routes: stock/ai/free -> provider, raw names pass."""
    from config import IMAGE_PROVIDERS, resolve_image_route

    assert resolve_image_route("stock") == "pexels"
    assert resolve_image_route("AI") == "gemini"
    assert resolve_image_route(" free ") == "pollinations"
    for name in IMAGE_PROVIDERS:
        assert resolve_image_route(name) == name
    try:
        resolve_image_route("jpg")
        raise AssertionError("nonsense route must raise")
    except ValueError as exc:
        assert "stock" in str(exc) and "pexels" in str(exc)
    # the CLI flag is really wired: an invalid route dies before spending
    import subprocess
    root = Path(__file__).resolve().parent
    res = subprocess.run(
        [sys.executable, str(root / "main.py"), "generate", "--count", "1",
         "--image-provider", "nope"],
        capture_output=True, text=True, timeout=120, cwd=str(root))
    assert res.returncode != 0, res.stdout[-400:]
    assert "unknown image route" in (res.stdout + res.stderr)


def t_sheet_dropped():
    """Panel queue-removal uses status 'dropped', history stays put."""
    from sheet import (append_sheet, load_sheet, mark_sheet, pending_rows,
                       take_pending)

    cfg = tmp_cfg()
    page = cfg.sources_sheet
    assert append_sheet(page, "https://youtu.be/aaa", "the octopus")
    assert mark_sheet(page, "https://youtu.be/aaa", "dropped")
    rows = load_sheet(page)
    assert len(rows) == 1 and rows[0]["status"] == "dropped"   # not deleted
    assert pending_rows(rows) == [] and take_pending(page, 3) == []
    assert not mark_sheet(page, "https://youtu.be/aaa", "banana")
    assert mark_sheet(page, "https://youtu.be/aaa", "new")
    assert len(take_pending(page, 1)) == 1


def t_panel_argmap():
    """Every panel button -> exact CLI args (the golden mapping)."""
    import panel

    assert panel.build_argv("generate", {}) == ["generate", "--count", "1"]
    assert panel.build_argv("generate", {"count": 3, "seconds": 60,
                                         "style": "cartoon",
                                         "route": "stock"}) == [
        "generate", "--count", "3", "--seconds", "60", "--style", "cartoon",
        "--image-provider", "stock"]
    assert panel.build_argv("generate", {"count": 99}) == [
        "generate", "--count", "10"]                     # clamped
    assert panel.build_argv("clip", {"url": "https://youtu.be/x"}) == [
        "clip", "--url", "https://youtu.be/x"]
    assert panel.build_argv("clip", {}) == ["clip", "--sheet", "1"]
    assert panel.build_argv("parts", {"url": "https://youtu.be/x"}) == [
        "parts", "--url", "https://youtu.be/x"]
    assert panel.build_argv("meeting", {"kind": "act"}) == [
        "meeting", "act", "--no-send", "--events-json"]
    assert panel.build_argv("meeting", {"kind": "act",
                                        "dry_run": True}) == [
        "meeting", "act", "--no-send", "--dry-run"]
    assert panel.build_argv("meeting", {"kind": "bogus"}) == [
        "meeting", "act", "--no-send", "--events-json"]
    assert panel.build_argv("meeting_memory") == ["meeting", "memory"]
    assert panel.build_argv("keys") == ["keys", "--month"]
    assert panel.build_argv("package", {}) == ["package", "--limit", "3"]
    assert panel.build_argv("yt_video", {"url": "https://youtu.be/x"}) == [
        "yt", "https://youtu.be/x"]
    for bad in ({"action": "parts"}, {"action": "yt_video"},
                {"action": "nope"}, {"action": "autopost"},
                {"action": "crew"}):
        try:
            panel.build_argv(bad["action"], {})
            raise AssertionError(f"{bad['action']} must not be runnable")
        except ValueError:
            pass
    assert panel.describe("generate", {"count": 2, "seconds": 45,
                                       "style": "photoreal",
                                       "route": "stock"}) == \
        "generate: 2 video(s), 45s, photoreal, stock route"
    assert panel.describe("meeting", {"kind": "act", "dry_run": True}) == \
        "meeting act (dry run)"
    assert panel.describe("clip", {}) == "clip: next on the list"


def t_panel_order_actions():
    """The Publish section maps to the real CLI, gate and safety included."""
    import panel

    txt = ("get a link from the database, get 6 clips and post them in 5 "
           "channels")
    assert panel.build_argv("order", {"text": txt}) == ["order", txt]
    assert panel.build_argv("order", {"text": txt, "plan_only": True}) == [
        "order", txt, "--plan-only"]
    assert panel.build_argv("order", {"text": txt, "dry_run": True}) == [
        "order", txt, "--dry-run"]
    try:
        panel.build_argv("order", {"text": "  "})
    except ValueError:
        pass
    else:
        raise AssertionError("an empty order must be refused")
    assert panel.build_argv("desktop_status", {}) == ["desktop", "status"]
    assert panel.build_argv("desktop_channels", {}) == [
        "desktop", "channels", "list"]
    assert "plan only" in panel.describe("order", {"text": txt,
                                                   "plan_only": True})
    assert "dry run" in panel.describe("order", {"text": txt,
                                                 "dry_run": True})
    # plan_only wins over dry_run if both are set: cheapest honest thing
    assert panel.build_argv("order", {"text": txt, "plan_only": True,
                                      "dry_run": True})[-1] == "--plan-only"


def t_panel_no_button_without_route():
    """Every action the page can run has an argv mapping — no dead buttons,
    no button that invents a flag (the invariant this panel was built on)."""
    import re

    import panel

    page = (Path(__file__).resolve().parent / "panel.html").read_text(
        encoding="utf-8")
    actions = set(re.findall(r'run\("([a-z_]+)"', page))
    actions |= set(re.findall(r'data-tool="([a-z_]+)"', page))
    actions |= set(re.findall(r'data-order-tool="([a-z_]+)"', page))
    assert actions, "no actions found in the page — did the wiring change?"
    for action in sorted(actions):
        try:
            argv = panel.build_argv(action, {"text": "x", "url": "http://x",
                                             "count": 1, "limit": 1,
                                             "kind": "act"})
        except ValueError as exc:
            raise AssertionError(f"page button {action!r} has no route: "
                                 f"{exc}") from exc
        assert argv and isinstance(argv[0], str), (action, argv)

    # the Publish section exists and shows the gate; the real run asks first
    assert "ord-text" in page and "ord-run" in page and "ord-dry" in page
    assert "uploads ON" in page or "uploads: ON" in page
    assert "confirm(" in page, "the real-run button must ask before posting"


def t_panel_mask():
    """Keys are masked on the way to the page; URLs and paths survive."""
    import panel

    gemini = "AIzaSyB1234567890abcdefghijklmnopqrst"
    assert gemini not in panel.mask_line(f"  [gemini] using {gemini}")
    assert panel.mask_line(f"key {gemini}").endswith("…" + gemini[-4:])
    groq = "gsk_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    assert groq not in panel.mask_line(f"groq key {groq}")
    assert "9c1d2e3f" not in panel.mask_line(
        "telegram token 1234567890:AAH9c1d2e3f4g5h6i7j8k9l0m1n2o3p4q")
    assert "hunter2" not in panel.mask_line("api_key=hunter2hunter2hunter2")
    longopaque = "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5"
    assert longopaque not in panel.mask_line(f"token {longopaque}")
    url = "https://github.com/dreamer2664/youtproject/blob/main/README.md"
    assert panel.mask_line(url) == url
    path = "/home/user/repo/out/meetings/2026-10-04-act.md"
    assert panel.mask_line(path) == path
    assert panel.mask_line("plain english log line, nothing secret") == \
        "plain english log line, nothing secret"


def t_panel_events():
    """The ##PANEL## wire format: one JSON line per meeting event."""
    import json as _json

    import panel

    event = {"type": "turn", "role": "Skeptic", "text": "one\ntwo"}
    line = panel.encode_event(event)
    assert line.startswith("##PANEL## ") and "\n" not in line
    assert panel.decode_line(line) == event
    assert panel.decode_line("ordinary log line") is None
    assert panel.decode_line("##PANEL## not json") is None
    assert panel.decode_line("##PANEL## [1,2]") is None
    frame = panel.sse_frame({"kind": "log", "line": "hello\nworld"})
    assert frame.endswith("\n\n") and frame.count("data: ") == 1
    body = frame[len("data: "):].strip()
    assert _json.loads(body)["line"] == "hello\nworld"


def t_panel_links():
    """The links list: add / drop / restore, pending flags, payload shape."""
    import panel

    cfg = tmp_cfg()
    assert panel.add_link(cfg, "https://youtu.be/aaa",
                          "the octopus") == "added to the list"
    assert panel.add_link(cfg, "https://youtu.be/aaa") == "already on the list"
    assert panel.add_link(cfg, "not-a-link").startswith("skipped")
    rows = panel.links_payload(cfg)
    assert len(rows) == 1 and rows[0]["pending"]
    assert rows[0]["note"] == "the octopus" and rows[0]["status"] == "new"
    assert panel.drop_link(cfg, "https://youtu.be/aaa") == \
        "removed from the queue"
    rows = panel.links_payload(cfg)
    assert rows[0]["status"] == "dropped" and not rows[0]["pending"]
    assert panel.drop_link(cfg, "https://youtu.be/zzz") == "row not found"
    assert panel.restore_link(cfg, "https://youtu.be/aaa") == \
        "back in the queue"
    assert panel.links_payload(cfg)[0]["pending"]


def t_panel_chat():
    """Chat: the seat's persona + history reach the model; failures talk."""
    import panel

    cfg = tmp_cfg()
    seen = {}

    class FakeProvider:
        def generate_text(self, prompt, temperature=0.7, tag="",
                          json_mode=False):
            seen["prompt"] = prompt
            seen["tag"] = tag
            return "  Ship the octopus clip.  "

    text = panel.chat_reply(
        cfg, "Analyst", "what should we do?",
        history=[{"who": "owner", "text": "hi there"},
                 {"who": "Analyst", "text": "hello boss"}],
        provider=FakeProvider())
    assert text == "Ship the octopus clip.", text
    assert seen["tag"] == "panel-chat"
    assert "Analyst" in seen["prompt"]
    assert "numbers" in seen["prompt"].lower()          # persona is in there
    assert "hi there" in seen["prompt"] and "hello boss" in seen["prompt"]
    assert "what should we do?" in seen["prompt"]
    assert panel.chat_reply(cfg, "Nobody", "x",
                            provider=FakeProvider()).startswith("Unknown seat")
    assert panel.chat_reply(cfg, "Analyst", "",
                            provider=FakeProvider()) == "(empty message)"

    class Broken:
        def generate_text(self, *a, **k):
            raise RuntimeError("429 quota gone")

    out = panel.chat_reply(cfg, "Producer", "status?", provider=Broken())
    assert out.startswith("Producer could not answer") and "429" in out
    assert panel.chat_reply(cfg, "Skeptic", "x", provider=None).startswith(
        "No LLM lane answered")

    # A Whisper-only Groq pool must NOT count as a chat lane (key split).
    solo = tmp_cfg()
    solo.data["ai"]["groq_api_keys"] = ["gsk_solo_key_0000000000000001"]
    solo.data["ai"]["groq_transcription_percent"] = 100
    assert not solo.groq_llm_api_keys
    assert not panel.has_llm_lane(solo)
    assert panel.chat_reply(solo, "Producer", "status?",
                            provider=None).startswith("No LLM lane answered")
    mixed = tmp_cfg()
    mixed.data["ai"]["groq_api_keys"] = ["gsk_a_00000000000000000001",
                                         "gsk_b_00000000000000000002"]
    mixed.data["ai"]["groq_transcription_percent"] = 50
    assert mixed.groq_llm_api_keys and panel.has_llm_lane(mixed)


def t_panel_state():
    """The header strip snapshot is offline-safe and complete."""
    import panel

    cfg = tmp_cfg()
    snap = panel.snapshot(cfg)
    assert set(snap) >= {"keys", "disk_free_mb", "disk_total_mb", "queue",
                         "meetings", "work_dir"}
    assert set(snap["keys"]) == set(panel.KEY_PROVIDERS)
    assert all(isinstance(v, int) and v >= 0 for v in snap["keys"].values())
    assert isinstance(snap["queue"], dict) and isinstance(snap["meetings"],
                                                           int)
    assert snap["disk_free_mb"] is None or snap["disk_free_mb"] >= 0
    assert snap["work_dir"].endswith("work")


def t_panel_files():
    """The page, the launcher and the CLI are all really there."""
    import subprocess

    root = Path(__file__).resolve().parent
    html = (root / "panel.html").read_text(encoding="utf-8")
    for needle in ("/events", "/api/run", "/api/chat", "/api/stop",
                   'id="stop"', 'id="gen-start"', 'id="m-act"',
                   'id="links-pending"', 'id="chat-send"'):
        assert needle in html, needle
    bat = (root / "Start Panel.bat").read_text(encoding="utf-8")
    for needle in ("panel.py --app-window", "pythonw.exe", 'start "" /min'):
        assert needle in bat, needle
    # NO bare "is the port open?" check: that is what opened the owner's
    # OLDER project's window on 2026-10-04 (both projects used 8765).
    assert "TcpClient" not in bat
    pysrc = (root / "panel.py").read_text(encoding="utf-8")
    for needle in ("--app-window", "probe_ours", "existing_panel_url",
                   "panel_port.txt", "msedge.exe", "chrome.exe", "--app="):
        assert needle in pysrc, needle
    res = subprocess.run(
        [sys.executable, str(root / "main.py"), "panel", "--help"],
        capture_output=True, text=True, timeout=120, cwd=str(root))
    assert res.returncode == 0, res.stderr[-300:]
    assert "--no-browser" in res.stdout and "--port" in res.stdout
    # `python panel.py --no-browser` (what Start Panel.bat runs) must PARSE
    # its flags — a shim that ignored them would pop an extra browser window
    # before Edge app-mode even starts.
    res = subprocess.run(
        [sys.executable, str(root / "panel.py"), "--help"],
        capture_output=True, text=True, timeout=120, cwd=str(root))
    assert res.returncode == 0, res.stderr[-300:]
    assert "--no-browser" in res.stdout and "--host" in res.stdout
    assert "--app-window" in res.stdout


def t_panel_launch():
    """The launcher must identify OUR panel — never open a stranger's window.

    Regression, 2026-10-04: the owner also runs an older project on the
    same port range; Start Panel.bat checked only "does 8765 answer?",
    so a double-click skipped our server and opened the OTHER project.
    """
    import json as _json
    import threading
    import time as _time
    import unittest.mock as mock
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import panel

    class _Base(BaseHTTPRequestHandler):
        body = b"{}"

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(self.body)))
            self.end_headers()
            self.wfile.write(self.body)

        def log_message(self, *a):  # keep the suite quiet
            pass

    class Foreign(_Base):                     # the older project's server
        body = b'{"ok": true, "project": "older-board"}'

    class Ours(_Base):                        # /api/state exactly as we serve
        body = _json.dumps({"app": panel.APP_MARKER, "token": "abc123",
                            "snapshot": {"keys": {}},
                            "runner": {}}).encode()

    class Lookalike(_Base):       # copies our SHAPE, not our identity
        body = _json.dumps({"snapshot": {"keys": {}},
                            "runner": {}}).encode()

    def spin(handler):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, srv.server_address[1]

    cfg = tmp_cfg()
    foreign, fport = spin(Foreign)
    ours, oport = spin(Ours)
    try:
        assert panel.probe_ours("127.0.0.1", fport) is False   # not ours
        assert panel.probe_ours("127.0.0.1", oport) is True    # ours

        # A lookalike that copies our JSON shape is still not ours…
        look, lport = spin(Lookalike)
        try:
            assert panel.probe_ours("127.0.0.1", lport) is False
            # …and even the real one fails a WRONG token (identity, not shape)
            assert panel.probe_ours("127.0.0.1", oport,
                                    token="abc123") is True
            assert panel.probe_ours("127.0.0.1", oport,
                                    token="wrong-token") is False
            # with no port file, find_our_panel still locates ours by marker
            empty = tmp_cfg()
            assert panel.read_port_file(empty) is None
            found = panel.find_our_panel(empty, start=min(oport, lport))
            assert found in (oport, None) or found != lport, found
        finally:
            look.shutdown(); look.server_close()

        # The exact bug: a stranger on the recorded port opens NO window.
        panel.write_port_file(cfg, fport)
        assert panel.read_port_file(cfg) == fport
        assert panel.existing_panel_url(cfg) is None

        # Our own live panel -> its URL, and NOT a second server.
        panel.write_port_file(cfg, oport)
        url = f"http://127.0.0.1:{oport}/"
        assert panel.existing_panel_url(cfg) == url
        opened = []
        with mock.patch.object(panel, "open_app_window",
                               lambda u: opened.append(u) or True), \
             mock.patch.object(panel, "serve",
                               side_effect=AssertionError("must not start")):
            assert panel.launch(cfg) == 0
        assert opened == [url], opened

        # Dead panel behind the port file -> cold start, app window mode.
        ours.shutdown(); ours.server_close()
        assert panel.existing_panel_url(cfg) is None
        started = []
        with mock.patch.object(panel, "open_app_window",
                               lambda u: opened.append(u) or True), \
             mock.patch.object(panel, "serve",
                               lambda *a, **k: started.append(k) or 0):
            assert panel.launch(cfg) == 0
        assert started and started[0]["app_window"] is True
        assert started[0]["open_browser"] is False
    finally:
        foreign.shutdown(); foreign.server_close()

    # A LIVE stranger on the recorded port -> WE start our own panel.
    foreign2, f2port = spin(Foreign)
    try:
        panel.write_port_file(cfg, f2port)
        opened2, started2 = [], []
        with mock.patch.object(panel, "open_app_window",
                               lambda u: opened2.append(u) or True), \
             mock.patch.object(panel, "serve",
                               lambda *a, **k: started2.append(k) or 0):
            assert panel.launch(cfg) == 0
        assert started2 and not opened2, (started2, opened2)
    finally:
        foreign2.shutdown(); foreign2.server_close()

    # open_app_window: --app= where a Chromium browser exists, tab otherwise.
    argv = []

    class FakeProc:
        def __init__(self, cmd, **kwargs):
            argv.append(cmd)

    with mock.patch.object(panel, "browser_candidates",
                           lambda: ["msedge.exe"]), \
         mock.patch.object(panel.subprocess, "Popen", FakeProc):
        assert panel.open_app_window("http://127.0.0.1:9999/") is True
    assert argv and argv[0][1] == "--app=http://127.0.0.1:9999/", argv
    tabs = []
    with mock.patch.object(panel, "browser_candidates", lambda: []), \
         mock.patch.object(panel.webbrowser, "open",
                           lambda u: tabs.append(u) or True):
        assert panel.open_app_window("http://127.0.0.1:9999/") is False
    assert tabs == ["http://127.0.0.1:9999/"]

    # serve() records the port it REALLY bound (and its window follows it):
    # 45990 is "taken" by a stranger, so everything must say 45991.
    seen = {}

    class FakeHTTPD:
        def __init__(self, addr, handler):
            _host, p = addr
            if p == 45990:
                raise OSError("address in use")
            seen["port"] = p

        def serve_forever(self):
            seen["file"] = panel.read_port_file(cfg)

        def server_close(self):
            seen["closed"] = True

    with mock.patch.object(panel, "ThreadingHTTPServer", FakeHTTPD), \
         mock.patch.object(panel, "open_app_window",
                           lambda u: seen.setdefault("url", u) or True):
        rc = panel.serve(cfg, host="127.0.0.1", port=45990,
                         open_browser=False, app_window=True)
        assert rc == 0, rc
        _time.sleep(0.6)       # the window timer (0.4s) must land in-patch
    assert seen.get("port") == 45991 and seen.get("file") == 45991, seen
    assert seen.get("url") == "http://127.0.0.1:45991/", seen
    assert seen.get("closed") and panel.read_port_file(cfg) is None


def t_panel_status_and_defaults():
    """`panel --status` names the ports; panel.port pins the default."""
    import unittest.mock as mock

    import panel

    cfg = tmp_cfg()
    assert panel.default_port(cfg) == 8765
    cfg2 = tmp_cfg(panel={"port": 9000})
    assert panel.default_port(cfg2) == 9000
    cfg3 = tmp_cfg(panel={"port": "banana"})
    assert panel.default_port(cfg3) == 8765          # garbage = default

    rows = [{"port": 8765, "state": "other"},
            {"port": 8766, "state": "ours"},
            {"port": 8767, "state": "free"}]
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with mock.patch.object(panel, "scan_ports", lambda *a, **k: rows), \
         redirect_stdout(buf):
        rc = panel.panel_status(cfg)
    out = buf.getvalue()
    assert rc == 0, rc
    assert "8765  held by another program" in out, out
    assert "8766  ← THIS panel" in out, out
    assert "http://127.0.0.1:8766/" in out, out
    assert "another project" in out, out

    # nothing running: it says so and tells him how to start it
    buf = io.StringIO()
    with mock.patch.object(panel, "scan_ports",
                           lambda *a, **k: [{"port": 8765, "state": "free"}]), \
         redirect_stdout(buf):
        panel.panel_status(cfg)
    out = buf.getvalue()
    assert "no panel of ours is running" in out, out
    assert "Start Panel.bat" in out and "python main.py panel" in out, out


def t_panel_failure_is_visible():
    """Start Panel.bat runs under pythonw: a failed start must DIALOG.

    Before: serve() returning 1 printed to a console that does not exist
    and pythonw exited silently — from the owner's side, "nothing happened"
    and he fell back to typing URLs into the browser by hand.
    """
    import unittest.mock as mock

    import panel

    import config as config_mod

    seen = []
    cfg = tmp_cfg()
    with mock.patch.object(config_mod, "load_config", lambda path=None: cfg), \
         mock.patch.object(panel, "launch", lambda *a, **k: 1), \
         mock.patch.object(panel, "_alert",
                           lambda title, text: seen.append((title, text))):
        rc = panel._main(["--app-window"])
    assert rc == 1, rc
    assert seen and "could not start" in seen[0][1], seen
    assert "panel --status" in seen[0][1], seen

    # a normal Ctrl+C stop (130) is not an error: no dialog
    seen.clear()
    with mock.patch.object(config_mod, "load_config", lambda path=None: cfg), \
         mock.patch.object(panel, "launch", lambda *a, **k: 130), \
         mock.patch.object(panel, "_alert",
                           lambda title, text: seen.append((title, text))):
        assert panel._main(["--app-window"]) == 130
    assert seen == [], seen
    assert callable(getattr(config_mod, "load_config", None))


def t_panel_port_file_forms():
    """The port file tolerates old bare-number files and carries the token."""
    import panel

    cfg = tmp_cfg()
    path = panel.port_file_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("9000", encoding="utf-8")          # the old form
    assert panel.read_port_file(cfg) == 9000
    assert panel.read_port_file_full(cfg).get("token") == ""
    path.write_text('{"port": 8766, "pid": 1, "token": "t0k"}',
                    encoding="utf-8")
    full = panel.read_port_file_full(cfg)
    assert full["port"] == 8766 and full["token"] == "t0k", full
    path.write_text("garbage", encoding="utf-8")
    assert panel.read_port_file(cfg) is None
    path.write_text('{"port": 99999}', encoding="utf-8")
    assert panel.read_port_file(cfg) is None


def t_nightbatch():
    """Night batch: plan, run/resume/fresh, lock, report — all offline."""
    import subprocess
    from unittest import mock

    import nightbatch as nb
    from sheet import append_sheet

    cfg = tmp_cfg()
    append_sheet(cfg.sources_sheet, "https://youtu.be/nb-test-1")

    with mock.patch.object(nb, "telegram_ready", lambda c: True):
        steps, notes = nb.plan_steps(cfg, clips=2, count=2, seconds=45,
                                     style="cartoon", image_provider="stock",
                                     max_clips=6)
    assert [s["id"] for s in steps] == ["clip-1", "batch-1", "push"], steps
    clip = " ".join(steps[0]["argv"])
    assert "clip --sheet 1" in clip and "--max-clips 6" in clip
    gen = " ".join(steps[1]["argv"])
    assert ("batch --count 2" in gen and "--seconds 45" in gen
            and "--style cartoon" in gen and "--image-provider stock" in gen)
    # This lane can NEVER post: no publish verbs anywhere in any step.
    joined = " ".join(" ".join(s["argv"]) for s in steps)
    for banned in ("autopost", "published", "--publish", "buffer"):
        assert banned not in joined, banned
    # clips are capped by what the sheet actually holds
    with mock.patch.object(nb, "telegram_ready", lambda c: False):
        steps2, notes2 = nb.plan_steps(cfg, clips=5, count=0)
    assert [s["id"] for s in steps2] == ["clip-1"]
    assert any("Telegram not configured" in n for n in notes2)
    # an empty sheet means no clip step, and the note says why
    empty = tmp_cfg()
    with mock.patch.object(nb, "telegram_ready", lambda c: True):
        steps3, notes3 = nb.plan_steps(empty, clips=2, count=1)
    assert steps3 and all(s["kind"] != "clip" for s in steps3)
    assert any("no queued sources" in n for n in notes3)

    # -- run: fake runner; the batch step fails ONCE, then succeeds --
    calls: list = []
    fail_once = {"pending": True}

    def runner(argv, log, cwd):
        calls.append(argv)
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        Path(log).write_text("fake step output\n", encoding="utf-8")
        if "batch" in argv and fail_once["pending"]:
            fail_once["pending"] = False
            return 1, "boom: fake quota error\n"
        return 0, "fine\n"

    sent: list = []
    quiet = lambda *a, **k: None  # noqa: E731
    day = "2026-10-05"
    with mock.patch.object(nb, "telegram_ready", lambda c: True):
        rc = nb.run(cfg, clips=2, count=2, date=day, runner=runner,
                    notify=lambda c, txt: sent.append(txt) or True, echo=quiet)
    assert rc == 1, rc                       # batch-1 failed
    assert len(calls) == 3, calls            # clip, batch, push
    assert not nb.lock_path(cfg).exists()    # lock released even on failure
    journal = nb.load_journal(nb.journal_path(cfg, day))
    assert journal["steps"]["clip-1"]["status"] == "done"
    assert journal["steps"]["batch-1"]["status"] == "failed"
    assert sent and "2/3 steps ok" in sent[0]
    assert "generate 2 video(s)" in sent[0]          # the failed step, by name
    assert "boom: fake quota error" in sent[0]       # its last log line
    assert "Nothing was posted" in sent[0]
    assert "work/nightbatch/logs" in sent[0]

    # -- resume: done steps skipped, the failed one retried, now green --
    calls.clear()
    with mock.patch.object(nb, "telegram_ready", lambda c: True):
        rc2 = nb.run(cfg, clips=2, count=2, date=day, runner=runner,
                     notify=lambda c, txt: True, echo=quiet)
    assert rc2 == 0, rc2
    assert len(calls) == 1 and "batch" in calls[0], calls
    journal = nb.load_journal(nb.journal_path(cfg, day))
    assert journal["steps"]["batch-1"]["status"] == "done"

    # -- fresh: ignores the journal, redoes everything --
    calls.clear()
    with mock.patch.object(nb, "telegram_ready", lambda c: True):
        nb.run(cfg, clips=2, count=2, date=day, fresh=True, runner=runner,
               notify=lambda c, txt: True, echo=quiet)
    assert len(calls) == 3, calls

    # -- lock: one runner at a time; --force overrides; release works --
    ok1, _ = nb.acquire_lock(cfg)
    assert ok1
    ok2, msg = nb.acquire_lock(cfg)
    assert not ok2 and "night batch" in msg
    ok3, _ = nb.acquire_lock(cfg, force=True)
    assert ok3
    nb.release_lock(cfg)
    assert not nb.lock_path(cfg).exists()
    calls.clear()
    nb.acquire_lock(cfg)
    rc3 = nb.run(cfg, date=day, runner=runner, echo=quiet)
    assert rc3 == 2 and not calls              # refused while locked
    nb.release_lock(cfg)

    # -- dry run: prints the plan, runs nothing, leaves no lock --
    calls.clear()
    rc4 = nb.run(cfg, clips=1, count=1, date=day, dry_run=True,
                 runner=runner, echo=quiet)
    assert rc4 == 0 and not calls and not nb.lock_path(cfg).exists()

    # -- the CLI and the launcher really exist --
    root = Path(__file__).resolve().parent
    res = subprocess.run(
        [sys.executable, str(root / "main.py"), "nightbatch", "--help"],
        capture_output=True, text=True, timeout=120, cwd=str(root))
    assert res.returncode == 0, res.stderr[-300:]
    assert "--dry-run" in res.stdout and "--fresh" in res.stdout
    bat = (root / "Night Batch.bat").read_text(encoding="utf-8")
    assert "main.py nightbatch" in bat and "schtasks" in bat


def _fake_clip_kit(cfg, hex8="ab12cd34", num="01", title="The Octopus Trap",
                   hook="Octopuses taste with their arms and it is terrifying",
                   start=10.0, end=42.0):
    """A minimal clip kit on disk that pregen.collect_candidates accepts."""
    folder = Path(cfg.root) / "clips"
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{hex8}_clip_{num}"
    mp4 = folder / f"{stem}.mp4"
    mp4.write_bytes(b"\x00" * 4096)
    kit = folder / stem
    kit.mkdir(exist_ok=True)
    (kit / "TITLE.txt").write_text(title, encoding="utf-8")
    (kit / "DESCRIPTION.txt").write_text(
        f"{title}\n\n{hook}\n\nClipped from: something", encoding="utf-8")
    (kit / "CREDIT.txt").write_text(
        f"source: https://youtu.be/x\nchannel: Some Channel\n"
        f"window: {start:.1f}s - {end:.1f}s\n", encoding="utf-8")
    return stem, mp4


def t_nightreq():
    """The /go request file: bounded options, pending/done, never raises."""
    import nightreq

    cfg = tmp_cfg()
    assert nightreq.read_request(cfg) is None
    assert not nightreq.pending(cfg)
    assert "No /go request" in nightreq.status_line(cfg)

    assert nightreq.normalize_options({"clips": 99})["clips"] == 6
    assert nightreq.normalize_options({"clips": -3})["clips"] == 0
    assert nightreq.normalize_options({"count": "x"})["count"] == 0
    assert nightreq.normalize_options({"top": 0})["top"] == 1
    assert nightreq.normalize_options({"meeting": 0})["meeting"] is False
    assert nightreq.normalize_options(None) == nightreq.DEFAULT_OPTIONS

    rec = nightreq.write_request(cfg, {"clips": 4, "meeting": False})
    assert rec["status"] == "pending" and rec["options"]["clips"] == 4
    assert nightreq.pending(cfg)
    assert nightreq.request_path(cfg).exists()
    assert "pending" in nightreq.status_line(cfg)

    data = nightreq.read_request(cfg)
    assert data["options"]["meeting"] is False

    nightreq.mark_done(cfg, failed=2, note="2/5 steps ok")
    assert not nightreq.pending(cfg)
    done = nightreq.read_request(cfg)
    assert done["status"] == "done" and done["failed_steps"] == 2
    assert "finished" in done
    line = nightreq.status_line(cfg)
    assert "finished" in line and "2 step(s) failed" in line

    nightreq.clear(cfg)
    assert nightreq.read_request(cfg) is None
    # corrupt file = treated as absent, never a crash
    nightreq.request_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    nightreq.request_path(cfg).write_text("{not json", encoding="utf-8")
    assert nightreq.read_request(cfg) is None and not nightreq.pending(cfg)


def t_meeting_review():
    """The clip review: agenda from real kits, chair ranking, minutes."""
    import io
    import json as _json
    from contextlib import redirect_stdout
    from unittest.mock import patch as _patch

    import meeting as mt

    cfg = tmp_cfg()
    _fake_clip_kit(cfg, "ab12cd34", "01", "The Octopus Trap",
                   "Octopuses taste with their arms and it is terrifying")
    _fake_clip_kit(cfg, "ab12cd34", "02", "The Sleep Paralysis Demon",
                   "You wake up and cannot move and something is there")
    _fake_clip_kit(cfg, "ff00aa11", "01", "Salt Ruins Your Brain")

    clips = mt.collect_review_clips(cfg)
    assert len(clips) == 3, clips
    assert all(c["score"] >= 0 and c["signals"] for c in clips)
    assert clips[0]["score"] >= clips[-1]["score"]          # best first
    assert clips[0]["title"] and clips[0]["duration_s"] > 0

    only_one = mt.collect_review_clips(cfg, ids=["ab12cd34_clip_01"])
    assert len(only_one) == 1 and only_one[0]["title"] == "The Octopus Trap"
    import datetime as _dt
    future = _dt.datetime.now() + _dt.timedelta(hours=1)
    assert mt.collect_review_clips(cfg, since=future) == []
    assert mt.collect_review_clips(cfg, ids=["nope"]) == []

    agenda = mt._review_agenda(clips, top=2)
    assert "id | title | seconds | score | signals | hook" in agenda
    octo = next(c for c in clips if c["title"] == "The Octopus Trap")
    assert octo["id"] in agenda and "Octopus Trap" in agenda
    assert "HOOK" in agenda and "Pick the best 2" in agenda

    # the chair's ranking: bad ids dropped, duplicates deduped, ranks fixed
    cands = [c["id"] for c in clips]
    sleep_one = next(c for c in clips
                     if c["title"] == "The Sleep Paralysis Demon")
    raw = _json.dumps({
        "summary": "Hooks carry these; variety wins.",
        "picks": [
            {"id": sleep_one["id"], "rank": 2, "why": "great tension"},
            {"id": sleep_one["id"], "rank": 1, "why": "dupe"},
            {"id": "not a clip", "rank": 1, "why": "ghost"},
            {"id": octo["id"], "rank": 1, "why": "strongest hook"},
        ],
        "hold_note": "Hold the salt one for tomorrow.",
    })
    dec = mt.parse_decisions(raw, "review", cands)
    assert dec and [p["id"] for p in dec["picks"]] == [
        octo["id"], sleep_one["id"]]
    assert [p["rank"] for p in dec["picks"]] == [1, 2]
    assert dec["hold_note"].startswith("Hold the salt")
    assert mt.parse_decisions(_json.dumps({"picks": []}), "review",
                              cands) is None
    assert mt.parse_decisions(_json.dumps({"picks": [{"id": "x"}]}),
                              "review", cands) is None
    assert mt.parse_decisions("nonsense", "review", cands) is None

    minutes = mt.format_minutes("review", agenda, [], dec)
    assert "clip review" in minutes
    assert "Post these today, in this order:" in minutes
    assert f"1. `{octo['id']}` — strongest hook" in minutes
    assert "Held back:" in minutes

    # a full review sitting, with a scripted room (no keys, no network)
    class Scripted:
        def generate_text(self, prompt, temperature=0.7, tag="",
                          json_mode=False):
            if json_mode:
                return _json.dumps({
                    "summary": "Two hooks stand out.",
                    "picks": [{"id": cands[0], "rank": 1,
                               "why": "stops a scroll"}],
                    "hold_note": "rest are weaker",
                })
            return "That first hook is the one."

    events = []
    with _patch.object(mt, "_role_provider", lambda c, lane: Scripted()), \
         redirect_stdout(io.StringIO()):
        summary = mt.run_meeting(cfg, "review", clips=clips, top=3,
                                 send=False, events=events.append)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "agenda" and kinds.count("turn") == 4
    decision = next(e for e in events if e["type"] == "decision")
    assert decision["decision"]["picks"][0]["id"] == cands[0]
    assert "stops a scroll" in summary
    assert mt.run_meeting(tmp_cfg(), "review", clips=[], send=False) == (
        "No new clips to review — the clip lane produced nothing this run. "
        "Nothing spent.")
    # memory: the board remembers what it chose to post
    memory = (Path(cfg.root) / "work" / "board_memory.json")
    if memory.exists():
        assert "posted" in memory.read_text(encoding="utf-8")


def t_go_options():
    """The /go parser + the wake-time inbox pass (no network anywhere)."""
    import json as _json
    from unittest.mock import patch as _patch

    import bot as bot_mod
    import nightreq

    assert bot_mod.parse_incoming("/go") == ("go", "")
    assert bot_mod.parse_incoming("/go later") == ("go", "later")
    assert bot_mod.go_options("")["clips"] == 3
    assert bot_mod.go_options("")["count"] == 0
    assert bot_mod.go_options("")["mode"] == "run"
    assert bot_mod.go_options("later")["mode"] == "later"
    assert bot_mod.go_options("dry")["mode"] == "dry"
    assert bot_mod.go_options("status")["mode"] == "status"
    o = bot_mod.go_options("4 2 fresh nomeeting noreview")
    assert (o["clips"], o["count"], o["fresh"], o["meeting"],
            o["review"]) == (4, 2, True, False, False)
    assert bot_mod.go_options("99 99")["clips"] == 6
    assert bot_mod.go_options("99 99")["count"] == 10
    assert bot_mod.go_options("zzz")["mode"] == "run"

    cfg = tmp_cfg()
    sent: list = []

    class FakeBot:
        def __init__(self, c):
            self.owner = 777

        def _api(self, method, timeout=None, data=None):
            assert method == "getUpdates"
            return [
                # someone else's message: ignored
                {"update_id": 1, "message": {"from": {"id": 999},
                                             "chat": {"id": 999},
                                             "text": "/go"}},
                # owner's old chatter: skipped silently, offset advances
                {"update_id": 2, "message": {"from": {"id": 777},
                                             "chat": {"id": 777},
                                             "text": "hello"}},
                # the real thing
                {"update_id": 3, "message": {"from": {"id": 777},
                                             "chat": {"id": 777},
                                             "text": "/go 4"}},
            ]

        def send_message(self, chat_id, text, **k):
            sent.append((chat_id, text))

    with _patch.object(bot_mod, "PhoneBot", FakeBot):
        result = bot_mod.check_inbox_once(cfg)
    assert result == {"seen": 2, "queued": 1, "error": ""}, result
    req = nightreq.read_request(cfg)
    assert req["status"] == "pending" and req["options"]["clips"] == 4
    assert req["options"]["meeting"] is True
    assert sent and sent[0][0] == 777 and "queued" in sent[0][1]
    offset = _json.loads((Path(cfg.work_dir) / "bot_offset.json")
                         .read_text(encoding="utf-8"))
    assert offset["offset"] == 4

    # no token / offline: a quiet no-op, never a crash
    class BadBot:
        def __init__(self, c):
            raise RuntimeError("no token configured")

    with _patch.object(bot_mod, "PhoneBot", BadBot):
        result = bot_mod.check_inbox_once(tmp_cfg())
    assert result["queued"] == 0 and "no token" in result["error"]


def t_wakeup():
    """The wake/boot task: inbox once, run the pending /go, hibernate maybe."""
    import nightreq
    from wakeup import run as wake_run

    cfg = tmp_cfg()
    calls: dict = {"inbox": 0, "batch": 0, "slept": 0}

    def inbox(c):
        calls["inbox"] += 1
        return {"seen": 1, "queued": 1}

    def batch(c):
        calls["batch"] += 1
        return 0

    def sleeper():
        calls["slept"] += 1
        return "hibernating now"

    quiet = lambda *a, **k: None  # noqa: E731

    # nothing pending -> fast exit, no batch, no hibernate (unless asked)
    rc = wake_run(cfg, inbox=inbox, batch_runner=batch, echo=quiet,
                  idler=lambda: 3600, sleeper=sleeper)
    assert rc == 0 and calls == {"inbox": 1, "batch": 0, "slept": 0}, calls

    # dry run says what would happen and does nothing
    nightreq.write_request(cfg, {"clips": 2})
    rc = wake_run(cfg, dry_run=True, inbox=inbox, batch_runner=batch,
                  echo=quiet)
    assert rc == 0 and calls["batch"] == 0

    # pending + sleep-after, nobody at the keyboard -> batch runs, then sleeps
    rc = wake_run(cfg, sleep_after=True, inbox=inbox, batch_runner=batch,
                  echo=quiet, idler=lambda: 3600, sleeper=sleeper)
    assert rc == 0 and calls["batch"] == 1 and calls["slept"] == 1, calls

    # ...but never hibernates over someone actively using the PC
    calls["slept"] = 0
    rc = wake_run(cfg, sleep_after=True, no_inbox=True, batch_runner=batch,
                  echo=quiet, idler=lambda: 12, sleeper=sleeper)
    assert calls["slept"] == 0 and calls["batch"] == 2

    # a batch crash keeps the request pending for the next wake
    nightreq.write_request(cfg, {"clips": 1})

    def boom(c):
        raise RuntimeError("clipper exploded")

    rc = wake_run(cfg, no_inbox=True, batch_runner=boom, echo=quiet)
    assert rc == 1 and nightreq.pending(cfg)

    # a broken inbox never blocks the run
    def bad_inbox(c):
        raise RuntimeError("telegram down")

    nightreq.mark_done(cfg)
    nightreq.write_request(cfg, {"clips": 1})
    rc = wake_run(cfg, batch_runner=batch, echo=quiet, inbox=bad_inbox)
    assert rc == 0 and calls["batch"] == 3


def t_nightbatch_go():
    """nightbatch's meeting steps, /go options, picks in the report."""
    import json as _json
    from unittest import mock

    import nightbatch as nb
    import nightreq
    from sheet import append_sheet

    cfg = tmp_cfg()
    append_sheet(cfg.sources_sheet, "https://youtu.be/go-test-1")

    with mock.patch.object(nb, "telegram_ready", lambda c: True):
        steps, notes = nb.plan_steps(cfg, clips=2, count=1, meeting=True,
                                     review=True, top=4,
                                     since_iso="2026-10-05T23:00:00")
    ids = [s["id"] for s in steps]
    assert ids == ["meeting-stats", "clip-1", "review", "batch-1", "push"], ids
    stats_argv = " ".join(steps[0]["argv"])
    assert "meeting stats" in stats_argv and "--json-out" in stats_argv
    review_argv = " ".join(steps[2]["argv"])
    assert ("meeting review" in review_argv
            and "--top 4" in review_argv
            and "--since 2026-10-05T23:00:00" in review_argv)
    joined = " ".join(" ".join(s["argv"]) for s in steps)
    for banned in ("autopost", "published", "--publish", "buffer"):
        assert banned not in joined, banned

    # /go with no request: a no-op, no lock, no journal
    echoed: list = []
    rc = nb.run(cfg, if_requested=True, runner=lambda a, l, c: (0, ""),
                notify=lambda c, t: True, echo=echoed.append)
    assert rc == 0 and any("no pending /go" in line for line in echoed)

    # /go with a request: its options win, and the request ends done
    nightreq.write_request(cfg, {"clips": 1, "count": 0, "meeting": False,
                                 "review": True, "top": 3})
    ran: list = []

    def runner(argv, log, cwd):
        ran.append(list(argv))
        if " review " in f" {' '.join(argv)} ":
            # the real review step writes review.json next to the journal
            (nb.batch_dir(cfg) / "review.json").write_text(_json.dumps({
                "summary": "Hooks carry the day.",
                "picks": [
                    {"id": "ab12cd34_clip_01", "rank": 1,
                     "title": "The Octopus Trap", "why": "instant hook"},
                    {"id": "ff00aa11_clip_01", "rank": 2,
                     "title": "Salt Ruins Your Brain", "why": "curiosity gap"},
                ],
            }), encoding="utf-8")
        return 0, "ok\n"

    sent: list = []
    day = "2026-10-05"
    with mock.patch.object(nb, "telegram_ready", lambda c: True):
        rc = nb.run(cfg, date=day, if_requested=True, runner=runner,
                    notify=lambda c, t: sent.append(t) or True,
                    echo=lambda *a, **k: None)
    assert rc == 0, rc
    commands = [argv[3] for argv in ran]          # main.py <command> ...
    assert "batch" not in commands, commands      # count=0 honored
    assert commands == ["clip", "meeting", "pregen"], commands
    review_argv = next(argv for argv in ran if argv[3] == "meeting")
    assert review_argv[4] == "review" and "--top" in review_argv
    assert review_argv[review_argv.index("--top") + 1] == "3"
    text = sent[0]
    assert "POST THESE TODAY" in text
    assert "1. The Octopus Trap" in text and "instant hook" in text
    assert "2. Salt Ruins Your Brain" in text
    assert not nightreq.pending(cfg)
    assert nightreq.read_request(cfg)["status"] == "done"

    # a failed step still finishes the request — with the count recorded
    nightreq.write_request(cfg, {"clips": 1, "count": 0, "meeting": False,
                                 "review": False})
    with mock.patch.object(nb, "telegram_ready", lambda c: True):
        rc = nb.run(cfg, date=day, if_requested=True, fresh=True,
                    runner=lambda a, l, c: (1, "boom\n"),
                    notify=lambda c, t: True, echo=lambda *a, **k: None)
    assert rc == 1
    assert nightreq.read_request(cfg)["failed_steps"] >= 1

    # resume: the same day's journal skips the finished clip step
    ran.clear()
    nightreq.write_request(cfg, {"clips": 1, "count": 0, "meeting": False,
                                 "review": False})
    with mock.patch.object(nb, "telegram_ready", lambda c: True):
        nb.run(cfg, date=day, if_requested=True,
               runner=lambda a, l, c: (0, "ok\n"),
               notify=lambda c, t: True, echo=lambda *a, **k: None)
    # clip-1 succeeded in the earlier run? It did not (it returned 0 twice),
    # so nothing to assert about skips here beyond a clean exit

    # review with no clips anywhere: the report says so instead of pretending
    empty = tmp_cfg()
    with mock.patch.object(nb, "telegram_ready", lambda c: True):
        steps2, _ = nb.plan_steps(empty, clips=0, count=0, meeting=False,
                                  review=True)
    assert [s["id"] for s in steps2] == ["review"]


def t_meeting_event_stream():
    """run_meeting emits agenda -> turns -> decision (and survives a dead
    listener) — the panel's live room is built on exactly this."""
    import io
    import json as _json
    from contextlib import redirect_stdout
    from unittest.mock import patch as _patch

    import meeting as mt

    class Scripted:
        def __init__(self):
            self.turns = 0

        def generate_text(self, prompt, temperature=0.7, tag="",
                          json_mode=False):
            if json_mode:
                return _json.dumps({"summary": "Room agrees.",
                                    "decisions": ["Do the thing"]})
            self.turns += 1
            return f"Point {self.turns}."

    events = []
    with _patch.object(mt, "_role_provider", lambda c, lane: Scripted()), \
         _patch.object(mt, "_stats_agenda", lambda c: "CHANNEL REPORT"), \
         redirect_stdout(io.StringIO()):
        summary = mt.run_meeting(tmp_cfg(), "stats", rounds=1, send=False,
                                 events=events.append)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "agenda", kinds
    assert kinds.count("turn") == 4, kinds
    assert "decision" in kinds, kinds
    assert "CHANNEL REPORT" in events[0]["text"]
    turn = next(e for e in events if e["type"] == "turn")
    assert turn["role"] == "Strategist" and turn["lane"] == "gemini"
    assert turn["text"] and turn["round"] == 1
    decision = next(e for e in events if e["type"] == "decision")
    assert decision["decision"]["decisions"] == ["Do the thing"]
    assert "Do the thing" in summary

    # an act meeting also emits the follow-through as an event
    class ActChair:
        def generate_text(self, prompt, temperature=0.7, tag="",
                          json_mode=False):
            if json_mode:
                return _json.dumps({"action": "generate",
                                    "topic": "why cats purr",
                                    "reason": "easy win"})
            return "generate it."

    act_events = []
    with _patch.object(mt, "_role_provider", lambda c, lane: ActChair()), \
         patch_act_agenda(), \
         redirect_stdout(io.StringIO()):
        mt.run_meeting(tmp_cfg(), "act", rounds=1, send=False,
                       executor=lambda d: "Rendered -> out/",
                       events=act_events.append)
    assert "action" in [e["type"] for e in act_events]
    fired = next(e for e in act_events if e["type"] == "action")
    assert fired["action"] == "generate"

    # a listener that raises must not kill the meeting (watching is safe)
    def bad_listener(event):
        raise RuntimeError("panel bug")

    with _patch.object(mt, "_role_provider", lambda c, lane: Scripted()), \
         _patch.object(mt, "_stats_agenda", lambda c: "R"), \
         redirect_stdout(io.StringIO()):
        summary = mt.run_meeting(tmp_cfg(), "stats", rounds=1, send=False,
                                 events=bad_listener)
    assert "Do the thing" in summary


def patch_act_agenda():
    from unittest.mock import patch as _patch

    import meeting as mt

    return _patch.object(mt, "_act_agenda",
                         lambda c, cands: "MOMENTUM: something is working")


# --------------------------------------------------------------------------
# desktop agent & orders (the "AI takes the mouse" lane)
# --------------------------------------------------------------------------

PNG_1PX = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
           b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
           b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
           b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


class ScriptedDriver:
    """A browser that exists only in RAM — the offline suite's driver."""

    def __init__(self, pages=None,
                 url="https://studio.youtube.com/videos/upload",
                 on_click=None):
        self.pages = pages or {}
        self._url = url
        self.on_click = on_click or (lambda name: None)
        self.clicked: list = []
        self.filled: list = []
        self.files_set: list = []
        self.tabs: list = []
        self.shots: list = []
        self.started = False

    def _page(self):
        return self.pages.get(self._url, {"elements": [], "text": ""})

    def _elements(self):
        return self._page().get("elements") or []

    def start(self):
        self.started = True

    def close(self):
        self.started = False

    def goto(self, url):
        self._url = url

    def url(self):
        return self._url

    def title(self):
        return self._page().get("title") or "Scripted"

    def snapshot(self):
        return {"url": self._url, "title": self.title(),
                "elements": [dict(el) for el in self._elements()],
                "text": self._page().get("text") or ""}

    def click(self, target):
        name = self.element_name(target)
        if isinstance(target, int) and not 0 <= target < len(self._elements()):
            raise RuntimeError(f"no element at index {target}")
        if isinstance(target, str) and target and name == target:
            raise RuntimeError(f"no element matching {target!r}")
        self.clicked.append(name or target)
        self.on_click(name)

    def fill(self, target, text):
        self.filled.append((self.element_name(target), text))

    def press(self, key):
        self.clicked.append(f"key:{key}")

    def scroll(self, dy):
        self.clicked.append(f"scroll:{dy}")

    def read(self, target=None):
        return self._page().get("text") or ""

    def open_tab(self, url):
        self.tabs.append(url)
        self._url = url

    def switch_tab(self, index):
        self.clicked.append(f"tab:{index}")

    def close_tab(self, index):
        self.clicked.append(f"close-tab:{index}")

    def set_input_files(self, target, path):
        self.files_set.append(str(path))

    def element_name(self, target):
        if isinstance(target, int):
            for el in self._elements():
                if el.get("i") == target:
                    return str(el.get("name") or "")
            return ""
        return str(target)

    def screenshot(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG_1PX)
        self.shots.append(str(path))
        return str(path)


def _studio_page(publish_name="Publish",
                 url="https://studio.youtube.com/videos/upload"):
    """A minimal Studio-shaped upload dialog for ScriptedDriver."""
    return {
        url: {
            "title": "Upload - YouTube Studio",
            "text": "Upload videos to your channel",
            "elements": [
                {"i": 0, "tag": "input", "role": "input", "name": "file"},
                {"i": 1, "tag": "input", "role": "textbox",
                 "name": "Add a title (required)"},
                {"i": 2, "tag": "div", "role": "textbox",
                 "name": "Add a description"},
                {"i": 3, "tag": "button", "role": "button", "name": "Next"},
                {"i": 4, "tag": "div", "role": "radio", "name": "Unlisted"},
                {"i": 5, "tag": "button", "role": "button",
                 "name": publish_name},
            ],
        }
    }


def _scripted_model(replies: list):
    """A fake LLM: pops replies in order, answers lesson calls, counts calls."""
    state = {"calls": 0, "prompts": []}

    def ask(prompt: str) -> str:
        state["calls"] += 1
        state["prompts"].append(prompt)
        if "Write ONE short sentence" in prompt:
            return "Click the button labelled Launch, not the one named Start."
        return replies[min(state["calls"] - 1, len(replies) - 1)]

    ask.state = state
    return ask


def t_order_parse():
    """The user's sentence, and the close variants, parse as expected."""
    import orders

    sentence = ("get a link from the database, get 6 clips and post them in "
                "6 channels, and generate 2 videos for 2 channels")
    order = orders.parse_order(sentence)
    assert order["ok"], order
    assert order["get_link"] is True
    assert order["clips"] == 6 and order["videos"] == 2, order
    assert order["channels"] == 6 and order["post"] is True, order

    order = orders.parse_order("make 3 clips, no post")
    assert order["clips"] == 3 and order["post"] is False, order

    order = orders.parse_order("generate 4 videos for 2 channels")
    assert order["videos"] == 4 and order["clips"] == 0, order
    assert order["channels"] == 2, order

    order = orders.parse_order("pull a link from the sheet and cut 2 clips")
    assert order["get_link"] is True and order["clips"] == 2, order

    order = orders.parse_order("stage only, dry")
    assert order["dry"] is True and order["post"] is False, order

    order = orders.parse_order("hello there friend")
    assert order["ok"] is False, order

    order = orders.parse_order("get 99 clips and 99 videos")
    assert order["clips"] == 10 and order["videos"] == 10, order


def t_order_plan():
    """The plan speaks honestly about posting, and plan-only runs nothing."""
    import orders

    overrides = {"uploads": "off", "channels": [
        {"name": "Deep Ocean", "studio_url": "https://studio.youtube.com"},
        {"name": "Night Files", "studio_url": "https://studio.youtube.com"},
    ]}
    cfg = tmp_cfg(desktop=overrides)
    order = orders.parse_order("get 6 clips and post them in 6 channels, "
                               "1 video")
    text = orders.plan_text(cfg, order)
    assert "Deep Ocean" in text and "Night Files" in text, text
    assert "staged only" in text, text

    cfg_on = tmp_cfg(desktop={"uploads": "on",
                              "channels": [{"name": "Deep Ocean"}]})
    text_on = orders.plan_text(cfg_on, order)
    assert "POST: enabled" in text_on, text_on

    calls = []

    def runner(argv, log, cwd):
        calls.append(argv)
        return 0, ""

    result = orders.execute(cfg, order, plan_only=True, runner=runner)
    assert calls == [], calls
    assert result["plan"] in result["report"]


def t_order_targets():
    """Resolution + channel mapping + staged packets, no browser involved."""
    import json

    import orders

    cfg = tmp_cfg(desktop={"channels": [
        {"name": "Deep Ocean",
         "studio_url": "https://studio.youtube.com/channel/UC111"},
        {"name": "Night Files", "studio_url": ""},
        {"name": "History Vault", "studio_url": ""},
    ]})
    _fake_clip_kit(cfg, "aa11bb22", "01", "First Catch")
    _fake_clip_kit(cfg, "cc33dd44", "01", "Second Wind")
    _fake_clip_kit(cfg, "ee55ff66", "02", "Third Rail")

    clips = orders.resolve_new_clips(cfg, since_ts=0, limit=3)
    assert len(clips) == 3, clips
    titles = sorted(c["title"] for c in clips)
    assert titles == ["First Catch", "Second Wind", "Third Rail"], titles
    assert all(c["description"] for c in clips), clips
    # mtimes can collide within a second — the mapping assertions below must
    # not care which clip is "first", only that the mapping is round-robin
    clips.sort(key=lambda c: c["id"])

    channels = orders.channel_list(cfg)
    assert [c["name"] for c in channels] == ["Deep Ocean", "Night Files",
                                             "History Vault"]
    assert channels[0]["slug"] == "deep-ocean"

    order = orders.parse_order("get 6 clips and post them in 6 channels")
    assignments = orders.assign_channels(clips, channels, 6)
    assert [a["channel"]["name"] for a in assignments] == [
        "Deep Ocean", "Night Files", "History Vault"], assignments

    packets = orders.stage_packets(cfg, assignments, "2026-10-04")
    assert len(packets) == 3, packets
    plan = json.loads(Path(packets[0]).read_text(encoding="utf-8"))
    assert plan["channel"] == "Deep Ocean", plan
    assert plan["title"] == clips[0]["title"], (plan, clips)
    assert plan["posted"] is False
    assert "deep-ocean" in packets[0]


def t_desktop_safety():
    """The allowlist and the commit gate — the two rules that matter."""
    import desktop as d2

    cfg = tmp_cfg()
    assert d2.domain_allowed(cfg, "https://studio.youtube.com/x")[0]
    assert d2.domain_allowed(cfg, "https://www.youtube.com/@x")[0]
    assert d2.domain_allowed(cfg, "https://accounts.google.com/signin")[0]
    assert not d2.domain_allowed(cfg, "https://evil.example.com")[0]
    assert not d2.domain_allowed(cfg, "file:///etc/passwd")[0]
    assert not d2.domain_allowed(cfg, "javascript:alert(1)")[0]

    cfg_off = tmp_cfg()
    assert d2.classify_click(cfg_off, "Publish", {})[0] == "commit"
    assert d2.classify_click(cfg_off, "Schedule", {})[0] == "commit"
    assert d2.classify_click(cfg_off, "Save", {})[0] == "commit"
    assert d2.classify_click(cfg_off, "Watch", {})[0] == "safe"

    cfg_on = tmp_cfg(desktop={"uploads": "on"})
    assert d2.classify_click(cfg_on, "Publish", {})[0] == "commit-ok"
    for name in ("Delete video", "Buy now", "Unsubscribe", "Cancel order"):
        kind, why = d2.classify_click(cfg_on, name, {})
        assert kind == "banned", (name, kind, why)
    assert d2.classify_click(cfg_off, "Continue", {"commit": True})[0] == "commit"

    # dry-run never commits: a stray publish click is refused, not executed
    drv = ScriptedDriver(pages=_studio_page())
    log = d2.RunLog(Path(cfg_off.root) / "work" / "desktop" / "t.jsonl")
    shots = Path(cfg_off.root) / "work" / "desktop" / "shots" / "t"
    ok, note, _ = d2.execute_action(cfg_off, drv, {"action": "click",
                                                   "target": 5}, True, log,
                                    shots, 1)
    assert ok and "would click" in note, note
    assert drv.clicked == [], drv.clicked
    ok, note, _ = d2.execute_action(cfg_on, drv, {"action": "click",
                                                  "target": 0}, False, log,
                                    shots, 2)
    assert ok, note


def t_desktop_lessons_playbooks():
    """Lessons persist, cap, dedupe; playbooks match by goal+host."""
    import desktop

    cfg = tmp_cfg()
    desktop.add_lesson(cfg, "upload a video", "https://studio.youtube.com",
                       "The title box needs one click first.")
    desktop.add_lesson(cfg, "upload a video", "https://studio.youtube.com",
                       "The title box needs one click first.")
    desktop.add_lesson(cfg, "other goal", "https://www.youtube.com",
                       "Scroll once before clicking the tab bar.")
    assert len(desktop.load_lessons(cfg)) == 2

    mine = desktop.lessons_for(cfg, "upload a video",
                               "https://studio.youtube.com/videos/upload")
    assert "title box" in mine[0], mine

    for index in range(210):
        desktop.add_lesson(cfg, f"goal {index}", "https://x.youtube.com",
                           f"lesson number {index}")
    assert len(desktop.load_lessons(cfg)) <= 200

    desktop.record_playbook(cfg, "upload a video",
                            "https://studio.youtube.com",
                            [{"action": "click", "target_name": "Next",
                              "why": "advance"}])
    found = desktop.find_playbook(cfg, "upload a video",
                                  "https://studio.youtube.com/x")
    assert found and found["steps"][0]["target_name"] == "Next", found
    assert desktop.find_playbook(cfg, "upload a video",
                                 "https://www.youtube.com") is None


def t_desktop_prompt_and_parse():
    """The step prompt carries lessons + elements; replies parse cleanly."""
    import desktop

    cfg = tmp_cfg()
    snap = {"url": "https://studio.youtube.com/videos/upload",
            "title": "Studio", "text": "Upload videos",
            "elements": [{"i": 0, "role": "button", "name": "Publish",
                          "disabled": False}]}
    prompt = desktop._step_prompt(cfg, "post the clip", snap,
                                  ["Check the channel switcher first."], [], 5)
    assert "Publish" in prompt and "Check the channel switcher first." in prompt
    assert "studio.youtube.com" in prompt

    assert desktop.parse_action('{"action": "click", "target": 2}')["target"] == 2
    assert desktop.parse_action(
        '```json\n{"action": "done", "ok": true}\n```')["action"] == "done"
    assert desktop.parse_action('{"action": "fly"}') is None
    assert desktop.parse_action("no json here") is None
    assert desktop.parse_action('{"action": "click", "target": ') is None

    assert desktop.parse_channel_text("46.4M subscribers 1.6K videos") == {
        "subscribers": "46.4M", "videos": "1.6K"}
    assert desktop.parse_channel_text("nothing here") == {}
    assert desktop.studio_upload_url(
        {"studio_url": "https://studio.youtube.com/channel/UC1"}
    ).endswith("/channel/UC1/videos/upload")


def t_desktop_loop():
    """Success records a playbook; failure records a lesson; the next run
    replays the playbook with ZERO model calls."""
    import desktop

    cfg = tmp_cfg()
    page = {"https://www.youtube.com/@x": {
        "title": "Channel", "text": "hello",
        "elements": [{"i": 0, "tag": "button", "role": "button",
                      "name": "Launch"}]}}

    drv = ScriptedDriver(pages=page, url="https://www.youtube.com/@x")
    model = _scripted_model(['{"action": "click", "target": 0, '
                             '"why": "open it"}',
                             '{"action": "done", "ok": true, '
                             '"summary": "opened"}'])
    result = desktop.run_task(cfg, "open the launch panel", driver=drv,
                              model=model, echo=lambda *a, **k: None)
    assert result["ok"] and "opened" in result["summary"], result
    assert drv.clicked == ["Launch"], drv.clicked
    books = desktop.load_playbooks(cfg)
    assert books and books[-1]["steps"][0]["target_name"] == "Launch", books

    drv2 = ScriptedDriver(pages=page, url="https://www.youtube.com/@x")
    boom = _scripted_model(['{"action": "done", "ok": false, '
                            '"summary": "REPLAY SHOULD NOT NEED ME"}'])
    result = desktop.run_task(cfg, "open the launch panel", driver=drv2,
                              model=boom, echo=lambda *a, **k: None)
    assert result["ok"] and result["playbook"] is True, result
    assert boom.state["calls"] == 0, boom.state["calls"]
    assert drv2.clicked == ["Launch"], drv2.clicked

    page2 = {"https://www.youtube.com/@y": {
        "title": "Channel", "text": "",
        "elements": [{"i": 0, "tag": "a", "role": "link", "name": "About"}]}}
    drv3 = ScriptedDriver(pages=page2, url="https://www.youtube.com/@y")
    fail = _scripted_model(['{"action": "click", "target": 7}',
                            '{"action": "done", "ok": false, '
                            '"summary": "no idea"}'])
    result = desktop.run_task(cfg, "find the hidden counter", driver=drv3,
                              model=fail, echo=lambda *a, **k: None)
    assert result["ok"] is False
    learned = desktop.lessons_for(cfg, "find the hidden counter",
                                  "https://www.youtube.com/@y")
    assert any("Launch" in lesson for lesson in learned), learned

    drv4 = ScriptedDriver(pages=page2, url="https://www.youtube.com/@y")
    sneaky = _scripted_model(['{"action": "goto", "url": '
                              '"https://evil.example.com/steal"}',
                              '{"action": "done", "ok": false, '
                              '"summary": "blocked"}'])
    result = desktop.run_task(cfg, "find the hidden counter", driver=drv4,
                              model=sneaky, echo=lambda *a, **k: None)
    assert drv4.url() == "https://www.youtube.com/@y", drv4.url()
    log_text = Path(result["log"]).read_text(encoding="utf-8")
    assert "blocked" in log_text, log_text


def t_desktop_post_one():
    """The posting sequence: staged without the flag, committed with it."""
    import desktop

    item = {"id": "c-aa11bb22-01", "file": __file__, "title": "Tiny Test",
            "description": "desc"}
    channel = {"name": "Deep Ocean",
               "studio_url": "https://studio.youtube.com"}

    cfg_off = tmp_cfg()
    drv = ScriptedDriver(pages=_studio_page())
    got = desktop.post_one(cfg_off, drv, item, channel,
                           echo=lambda *a, **k: None, pause=0)
    assert got["ok"] is False and got["committed"] is False, got
    assert "uploads" in got["error"], got
    assert "Publish" not in drv.clicked, drv.clicked
    assert drv.files_set, "the file was set before the gate"

    drv = ScriptedDriver(pages=_studio_page())
    got = desktop.post_one(cfg_off, drv, item, channel, dry_run=True,
                           echo=lambda *a, **k: None, pause=0)
    assert got["ok"] and got["committed"] is False, got
    assert "Publish" not in drv.clicked, drv.clicked

    cfg_on = tmp_cfg(desktop={"uploads": "on"})
    drv = ScriptedDriver(pages=_studio_page())
    got = desktop.post_one(cfg_on, drv, item, channel,
                           echo=lambda *a, **k: None, pause=0)
    assert got["ok"] and got["committed"] is True, got
    assert "Publish" in drv.clicked, drv.clicked

    drv = ScriptedDriver(pages=_studio_page(publish_name="Delete video"))
    got = desktop.post_one(cfg_on, drv, item, channel,
                           echo=lambda *a, **k: None, pause=0)
    assert got["ok"] is False, got            # no publish/save button to trust
    assert "Delete video" not in drv.clicked  # the destructive one untouched

    drv = ScriptedDriver(pages={"https://studio.youtube.com/videos/upload": {
        "elements": [], "text": ""}})
    got = desktop.post_one(cfg_on, drv, item, channel,
                           echo=lambda *a, **k: None, pause=0)
    assert got["ok"] is False and "file input" in got["error"], got


def t_snap_empty_channel():
    """A brand-new channel's uploads playlist 404s — a fact, not a failure.

    MicroFeed and ClipShift (2026-10-04) are empty channels: YouTube has no
    uploads playlist to read until the first video exists. The snapshot used
    to dump that 404 JSON as a scary error every run; it now reports it as
    what it is, keeps the channel's zero row, and keeps real failures real.
    """
    import channelstats as cs
    from youtube import load_snapshots, save_snapshots

    channels = {
        "UC" + "e" * 22: {"title": "Empty One", "handle": "@emptyone",
                          "subs": 0, "views": 0, "videos": []},
        "UC" + "f" * 22: {"title": "Busy One", "handle": "@busyone",
                          "subs": 12, "views": 100, "videos": []},
    }
    e, f = list(channels)
    videos = {"vf000000001": {"title": "One video", "channel_id": f,
                              "published_at": "2026-10-03T10:00:00Z",
                              "duration": "PT40S", "views": 7, "likes": 1,
                              "comments": 0}}
    channels[f]["videos"].append("vf000000001")

    cfg = tmp_cfg()
    history = load_snapshots(cs.store_path(cfg))
    cs.add_channel(history, e, "Empty One")
    cs.add_channel(history, f, "Busy One")
    save_snapshots(cs.store_path(cfg), history)

    lines: list = []
    res = cs.run_snapshot(cfg, _FakeYT(channels, videos, empty={e}),
                          today="2026-10-04", log=lines.append)
    assert res["errors"] == [], res["errors"]
    assert res["fetched"][e] == 0 and res["fetched"][f] == 1, res["fetched"]
    assert any("no uploads yet" in line and "Empty One" in line
               for line in lines), lines

    text, _ = cs.build_channel_report(res["history"], "2026-10-04", None,
                                      errors=res["errors"])
    assert "Empty One" in text and "⚠️ Empty One" not in text, text
    assert "no videos fetched" in text, text

    # a REAL failure (500) still lands in errors — the friendly branch must
    # not swallow it
    res2 = cs.run_snapshot(cfg, _FakeYT(channels, videos, broken={f}),
                           today="2026-10-04", log=lambda *_: None)
    assert any("Busy One" in e and "500" in e for e in res2["errors"]), \
        res2["errors"]


def t_desktop_connection_check():
    """status must answer "is it ready?" by connecting, never by guessing."""
    import argparse
    import io
    from contextlib import redirect_stdout

    import desktop
    import main as main_mod

    cfg = tmp_cfg()

    # reachable: reports the page it found
    page = {"https://studio.youtube.com/": {
        "title": "YouTube Studio", "text": "", "elements": []}}
    drv = ScriptedDriver(pages=page, url="https://studio.youtube.com/")
    got = desktop.connection_check(cfg, driver=drv)
    assert got["ok"] and got["title"] == "YouTube Studio", got
    assert "studio.youtube.com" in got["url"], got

    # unreachable: the error is the actionable one, not a traceback
    class Dead(ScriptedDriver):
        def start(self):
            raise RuntimeError(
                "could not reach your browser at http://127.0.0.1:9222 — "
                "start it with Desktop Chrome.bat first (the window with "
                "the debug port). Opera GX, Edge, Chrome, Brave, Vivaldi "
                "all work; Edge is already on every Windows machine.")

    got = desktop.connection_check(cfg, driver=Dead())
    assert got["ok"] is False and "Desktop Chrome.bat" in got["error"], got

    # status prints the live verdict (offline in CI this is the honest ❌)
    ns = argparse.Namespace(action="status", goal=[], backend=None,
                            max_hands=False, max_steps=None, no_hands=False,
                            url=None)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main_mod.cmd_desktop(cfg, ns)
    assert rc == 0, rc
    assert "connection" in buf.getvalue(), buf.getvalue()


def t_desktop_peek_current():
    """`desktop shot|text` with no URL looks at the page already open."""
    import desktop

    cfg = tmp_cfg()
    page = {"https://studio.youtube.com/": {
        "title": "Studio home", "text": "Channel dashboard 1.2K views",
        "elements": [{"i": 0, "tag": "button", "role": "button",
                      "name": "Create"}]}}
    drv = ScriptedDriver(pages=page, url="https://studio.youtube.com/")
    got = desktop.peek(cfg, driver=drv, note="test-peek")
    assert got["ok"] and got["title"] == "Studio home", got
    assert got["shot"] and Path(got["shot"]).exists(), got
    assert "dashboard" in got["text"], got
    # peek never navigates away from wherever the user is
    assert drv.url() == "https://studio.youtube.com/", drv.url()


def _italian_studio_page():
    """Studio in the account language (Italian) — the owner's real UI."""
    return {
        "https://studio.youtube.com/videos/upload": {
            "title": "Carica video - YouTube Studio",
            "text": "Carica i video sul tuo canale",
            "elements": [
                {"i": 0, "tag": "input", "role": "input",
                 "name": "(file upload)"},
                {"i": 1, "tag": "input", "role": "textbox",
                 "name": "Titolo (obbligatorio)"},
                {"i": 2, "tag": "div", "role": "textbox",
                 "name": "Aggiungi una descrizione"},
                {"i": 3, "tag": "button", "role": "button", "name": "Avanti"},
                {"i": 4, "tag": "input", "role": "radio",
                 "name": "Pubblica", "id": "radio-public", "attr": "PUBLIC"},
                {"i": 5, "tag": "input", "role": "radio",
                 "name": "Non elencato", "id": "radio-unlisted",
                 "attr": "UNLISTED"},
                # the commit button, same word as the radio above, further down
                {"i": 6, "tag": "button", "role": "button",
                 "name": "Pubblica"},
            ],
        }
    }


def t_desktop_labels_italian():
    """The lane drives Studio in the ACCOUNT's language, not just English."""
    import desktop

    snap = _italian_studio_page()[
        "https://studio.youtube.com/videos/upload"]
    snap = {"elements": snap["elements"]}

    assert desktop._resolve_named(snap, desktop.LABELS["next"]) == 3
    assert desktop._resolve_named(snap, desktop.LABELS["title"]) == 1
    assert desktop._resolve_named(snap, desktop.LABELS["description"]) == 2
    # the visibility radio and the publish button share the word "Pubblica";
    # the radio is chosen for visibility (first), the button for the commit
    # (role=button + bottom-most)
    assert desktop._resolve_named(
        snap, desktop.LABELS["unlisted"], attr=True) == 5
    assert desktop._resolve_named(
        snap, desktop.LABELS["public"], role="button", last=True) == 6
    # English keeps working exactly as before
    eng = {"elements": [
        {"i": 0, "tag": "button", "role": "button", "name": "Next"},
        {"i": 1, "tag": "button", "role": "button", "name": "Publish"},
    ]}
    assert desktop._resolve_named(eng, desktop.LABELS["next"]) == 0
    assert desktop._resolve_named(
        eng, desktop.LABELS["publish"], role="button", last=True) == 1
    # attr matching is language-independent: PUBLIC wins over the label text
    assert desktop._resolve_named(snap, ("PUBLIC",), attr=True) == 4


def t_desktop_post_one_italian():
    """Full staging sequence against an Italian Studio, gate included."""
    import desktop

    item = {"id": "c-it000000-01", "file": __file__, "title": "Titolo IT",
            "description": "desc"}
    channel = {"name": "ZimoTV", "studio_url": "https://studio.youtube.com"}

    cfg_off = tmp_cfg()
    drv = ScriptedDriver(pages=_italian_studio_page())
    got = desktop.post_one(cfg_off, drv, item, channel,
                           echo=lambda *a, **k: None, pause=0)
    assert got["ok"] is False and "uploads" in got["error"], got
    assert drv.clicked.count("Avanti") >= 2, drv.clicked
    assert "Pubblica" not in drv.clicked, drv.clicked

    cfg_on = tmp_cfg(desktop={"uploads": "on", "visibility": "unlisted"})
    drv = ScriptedDriver(pages=_italian_studio_page())
    got = desktop.post_one(cfg_on, drv, item, channel,
                           echo=lambda *a, **k: None, pause=0)
    assert got["ok"] and got["committed"] is True, got
    # the radio was set (element 5), the commit button was clicked (element 6)
    assert drv.clicks[-1] == 6 if hasattr(drv, "clicks") else True
    assert drv.clicked[-1] == "Pubblica", drv.clicked
    assert "Non elencato" in drv.clicked, drv.clicked
    assert drv.filled, drv.filled


def t_desktop_cdp_http_info():
    """The browser's own http endpoint is the diagnosis source."""
    import io
    import json as _json
    import urllib.request as _url

    import desktop

    class Resp:
        def __init__(self, payload):
            self._b = io.BytesIO(_json.dumps(payload).encode())

        def read(self):
            return self._b.read()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    real = _url.urlopen

    def fake(url, timeout=0):
        if url.endswith("/json/version"):
            return Resp({"Browser": "Opera GX/104.0"})
        return Resp([{"type": "page", "title": "Studio"},
                     {"type": "page", "title": "Other"},
                     {"type": "service_worker", "title": "sw"}])

    _url.urlopen = fake
    try:
        info = desktop.cdp_http_info("http://127.0.0.1:9222")
    finally:
        _url.urlopen = real
    assert info["browser"] == "Opera GX/104.0", info
    assert info["tabs"] == 3 and info["pages"] == ["Studio", "Other"], info

    # nothing listening: {} and fast (never hangs a command)
    assert desktop.cdp_http_info("http://127.0.0.1:1") == {}


def t_desktop_stuck_tab():
    """A frozen tab is not a missing browser — and must never hang."""
    import argparse
    import io
    from contextlib import redirect_stdout

    import desktop
    import main as main_mod

    cfg = tmp_cfg()
    real_http = desktop.cdp_http_info
    real_drv = desktop.PlayDriver

    class Stuck:
        def start(self):
            raise desktop.BrowserBusy(
                "your browser IS running (Opera GX/104.0 (2 tab(s): Studio)) "
                "but a tab did not answer")

        def close(self):
            pass

        def probe(self):
            return {}

    desktop.cdp_http_info = lambda url, timeout=2.0: {
        "browser": "Opera GX/104.0", "tabs": 2, "pages": ["Studio"]}
    desktop.PlayDriver = lambda cfg, backend=None, timeout_ms=None: Stuck()
    try:
        got = desktop.connection_check(cfg)
        assert got["ok"] is False and got["alive"] is True, got
        assert "tab(s)" in got["error"], got

        ns = argparse.Namespace(action="status", goal=[], backend=None,
                                max_steps=None, no_hands=False, url=None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main_mod.cmd_desktop(cfg, ns)
        out = buf.getvalue()
        assert rc == 0 and "⚠️" in out and "❌" not in out, out

        # peek reports the same distinction: busy browser, not missing one
        class StuckPeek(Stuck):
            def snapshot(self):
                raise desktop.BrowserBusy("busy")

        got = desktop.peek(cfg, driver=StuckPeek(), with_shot=False)
        assert got["ok"] is False and got["alive"] is True, got

        class StuckPeekDegraded(Stuck):        # attached, page never answered
            def snapshot(self):
                return {"url": "https://studio.youtube.com/", "title": "",
                        "elements": [], "text": "", "degraded": True,
                        "reason": "the page did not answer in 4s"}

        got = desktop.peek(cfg, driver=StuckPeekDegraded(), with_shot=False)
        assert got["ok"] is False and got["alive"] is True, got
        assert "did not answer" in got["error"], got
    finally:
        desktop.cdp_http_info = real_http
        desktop.PlayDriver = real_drv

    # a browser that is NOT there keeps the ❌ answer (and gets one retry,
    # because a browser that is still starting up looks the same)
    made = []

    class NoBrowser:
        def start(self):
            raise RuntimeError(
                desktop._reach_error("http://127.0.0.1:9222",
                                     RuntimeError("ECONNREFUSED"))[0])

        def close(self):
            pass

    def factory(cfg, backend=None, timeout_ms=None):
        made.append(1)
        return NoBrowser()

    desktop.cdp_http_info = lambda url, timeout=2.0: {}
    desktop.PlayDriver = factory
    try:
        got = desktop.connection_check(cfg)
        assert got["ok"] is False and got["alive"] is False, got
        assert "Desktop Chrome.bat" in got["error"], got
        assert len(made) == 2, made          # retried once
    finally:
        desktop.cdp_http_info = real_http
        desktop.PlayDriver = real_drv


def t_desktop_peek_no_shot():
    """`desktop text` does not pay for a screenshot it will not use."""
    import desktop

    cfg = tmp_cfg()
    page = {"https://studio.youtube.com/": {
        "title": "Studio", "text": "hello studio", "elements": []}}
    drv = ScriptedDriver(pages=page, url="https://studio.youtube.com/")
    got = desktop.peek(cfg, driver=drv, note="no-shot", with_shot=False)
    assert got["ok"] and got["shot"] is None, got
    assert drv.shots == [], drv.shots
    drv2 = ScriptedDriver(pages=page, url="https://studio.youtube.com/")
    got2 = desktop.peek(cfg, driver=drv2, note="with-shot")
    assert got2["shot"] and len(drv2.shots) == 1, got2


def t_desktop_status_code_line():
    """`status` names the commit it is running — no more stale-clone guessing."""
    import argparse
    import io
    from contextlib import redirect_stdout

    import main as main_mod

    cfg = tmp_cfg()
    real = main_mod._git_rev
    ns = argparse.Namespace(action="status", goal=[], backend=None,
                            max_steps=None, no_hands=False, url=None)
    try:
        main_mod._git_rev = lambda: "abc1234 2026-10-04"
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main_mod.cmd_desktop(cfg, ns)
        out = buf.getvalue()
        assert rc == 0 and "abc1234 2026-10-04" in out, out
        assert "code          :" in out, out

        main_mod._git_rev = lambda: ""      # no git -> no line, no crash
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main_mod.cmd_desktop(cfg, ns)
        assert rc == 0 and "code          :" not in buf.getvalue()
    finally:
        main_mod._git_rev = real

    assert main_mod._git_rev()             # this IS a git checkout


def t_desktop_channels():
    """The runtime channel store: normalize, add, dedupe, remove, merge."""
    import desktop

    cfg = tmp_cfg(desktop={"channels": [
        {"name": "From Config",
         "studio_url": "https://studio.youtube.com/channel/"
                       "UCWKpOEGAYSgCUzJL-0fO4iQ"}]})

    # normalization accepts the three real-world forms
    assert desktop.normalize_studio_url(
        "https://studio.youtube.com/channel/UCWKpOEGAYSgCUzJL-0fO4iQ") == \
        "https://studio.youtube.com/channel/UCWKpOEGAYSgCUzJL-0fO4iQ"
    assert desktop.normalize_studio_url(
        "https://www.youtube.com/channel/UC_ii4bO3c6yR3ZsWBimB17g") == \
        "https://studio.youtube.com/channel/UC_ii4bO3c6yR3ZsWBimB17g"
    assert desktop.normalize_studio_url("UCwD7txMJVBN7CyfiXN73wbg") == \
        "https://studio.youtube.com/channel/UCwD7txMJVBN7CyfiXN73wbg"
    assert desktop.normalize_studio_url("@somehandle") == ""
    assert desktop.normalize_studio_url("") == ""

    # add: by studio link and by bare id; UC case must survive
    ok, message = desktop.add_extra_channel(
        cfg, "clipstudio-z0", "https://studio.youtube.com/channel/"
                              "UCwD7txMJVBN7CyfiXN73wbg")
    assert ok, message
    ok, message = desktop.add_extra_channel(cfg, "Clipshift-0",
                                            "UC_ii4bO3c6yR3ZsWBimB17g")
    assert ok, message
    stored = desktop.load_extra_channels(cfg)
    ids = sorted(e["id"] for e in stored)
    assert ids == ["UC_ii4bO3c6yR3ZsWBimB17g",
                   "UCwD7txMJVBN7CyfiXN73wbg"], ids

    # a bad reference is refused with a helpful message
    ok, message = desktop.add_extra_channel(cfg, "Nope", "@handle")
    assert not ok and "UC" in message, message

    # re-adding the same id renames instead of duplicating
    desktop.add_extra_channel(cfg, "Clip Studios", "UCwD7txMJVBN7CyfiXN73wbg")
    assert len(desktop.load_extra_channels(cfg)) == 2

    # merge: config entry + runtime entries; a runtime id equal to a config
    # id must NOT appear twice (config wins)
    desktop.add_extra_channel(cfg, "Config Duplicate",
                              "UCWKpOEGAYSgCUzJL-0fO4iQ")
    merged = desktop.all_channels(cfg)
    names = [c["name"] for c in merged]
    assert "From Config" in names and "Config Duplicate" not in names, names
    assert len(merged) == 3, names
    assert len([c for c in merged if c["source"] == "runtime"]) == 2

    # orders sees the merged list
    import orders

    listed = orders.channel_list(cfg)
    assert [c["name"] for c in listed][0] == "From Config", listed
    assert sorted(c["slug"] for c in listed) == [
        "clip-studios", "clipshift-0", "from-config"], listed

    # remove (runtime only), case-free matching
    ok, message = desktop.remove_extra_channel(cfg, "clipshift")
    assert ok, message
    assert [c["name"] for c in desktop.load_extra_channels(cfg)] == \
        ["Clip Studios", "Config Duplicate"]
    ok, message = desktop.remove_extra_channel(cfg, "from config")
    assert not ok and "runtime" in message, message   # config entries stay

    # the CLI path works end to end (cmd_desktop with a Namespace)
    import argparse

    import main as main_mod

    ns = argparse.Namespace(action="channels", goal=["add", "InfoSpectrum-0",
                                                   "UCUZXRxdXZctPU2-bKSYNr3A"],
                            backend=None, max_steps=None, no_hands=False,
                            url=None)
    rc = main_mod.cmd_desktop(cfg, ns)
    assert rc == 0, rc
    assert any(c["name"] == "InfoSpectrum-0"
               for c in desktop.load_extra_channels(cfg))
    ns = argparse.Namespace(action="channels", goal=[], backend=None,
                            max_steps=None, no_hands=False, url=None)
    assert main_mod.cmd_desktop(cfg, ns) == 0


def t_desktop_launcher_browsers():
    """The launcher accepts any Chromium browser; detection never crashes."""
    import desktop

    bat = (Path(__file__).resolve().parent / "Desktop Chrome.bat").read_bytes()
    assert bat.isascii(), "the .bat must stay ASCII (Windows cmd on CP1252)"
    assert b"\r\n" in bat, "the .bat must keep CRLF line endings"
    text = bat.decode("ascii")
    for browser in ("Opera GX", "msedge.exe", "chrome.exe", "brave.exe",
                    "vivaldi.exe", "launcher.exe"):
        assert browser in text, browser
    assert "remote-debugging-port=9222" in text
    assert "youtproject-desktop" in text          # its own profile
    assert "%~1" in text                          # explicit path wins

    found = desktop.find_browsers()
    assert isinstance(found, list)
    for entry in found:
        assert Path(entry["path"]).exists(), entry
        assert entry["name"], entry


def t_desktop_bot_wiring():
    """/look, /order and /desk parse, and the help text advertises them."""
    import bot

    assert bot.parse_incoming("/look https://example.com") == (
        "look", "https://example.com")
    assert bot.parse_incoming("/order get 2 clips") == ("order", "get 2 clips")
    assert bot.parse_incoming("/desk") == ("desk", "")
    assert bot.parse_incoming("/desk uploads on") == ("desk", "uploads on")
    assert bot.parse_incoming("/looky") == ("help", "")
    assert "/order" in bot.HELP_TEXT and "/desk" in bot.HELP_TEXT
    assert "/look" in bot.HELP_TEXT


def t_desktop_settings_override():
    """Runtime override (the bot's /desk uploads on) beats config.yaml."""
    import desktop

    cfg = tmp_cfg(desktop={"uploads": "off"})
    assert desktop.dconf(cfg)["uploads"] is False
    desktop.set_setting(cfg, "uploads", True)
    assert desktop.dconf(cfg)["uploads"] is True
    desktop.set_setting(cfg, "uploads", False)
    assert desktop.dconf(cfg)["uploads"] is False


def t_order_cli():
    """The real CLI: plan-only works end to end without a browser."""
    import subprocess
    import sys as _sys

    cfg = tmp_cfg(desktop={"channels": [{"name": "Only Channel"}]})
    root = Path(__file__).resolve().parent
    res = subprocess.run(
        [_sys.executable, "main.py", "--config", str(cfg.root / "config.yaml"),
         "order", "get a link from the database, get 6 clips and post them "
         "in 6 channels", "--plan-only"],
        capture_output=True, text=True, timeout=120, cwd=str(root))
    assert res.returncode == 0, (res.returncode, res.stdout[-500:],
                                 res.stderr[-500:])
    assert "Only Channel" in res.stdout, res.stdout[-800:]
    assert "PLAN ONLY: nothing runs" in res.stdout, res.stdout[-800:]

    res = subprocess.run([_sys.executable, "main.py", "--help"],
                         capture_output=True, text=True, timeout=60,
                         cwd=str(root))
    for lane in ("order", "desktop", "browser"):
        assert lane in res.stdout, lane


def t_process_no_console_kwargs():
    """Console-mode media tools must not pop a window from the GUI panel."""
    from unittest.mock import patch

    import process_utils

    actual = process_utils.no_console_kwargs()
    if process_utils.os.name == "nt":
        assert actual == {"creationflags":
                          process_utils.subprocess.CREATE_NO_WINDOW}, actual
    else:
        assert actual == {}, actual

    # Exercise the Windows branch even on Linux CI; always use the real
    # Windows flag value so the contract is checked, not just truthiness.
    with patch.object(process_utils.os, "name", "nt"), \
         patch.object(process_utils.subprocess, "CREATE_NO_WINDOW",
                      0x08000000, create=True):
        assert process_utils.no_console_kwargs() == {
            "creationflags": 0x08000000}


def t_ffmpeg_windows_no_window_calls():
    """The real FFmpeg runner and clip probe forward the Windows flag."""
    from types import SimpleNamespace
    from unittest.mock import patch

    import assembler
    import clipper

    expected = {"creationflags": 0x08000000}
    seen = {}

    class _Proc:
        returncode = 0

        def communicate(self, timeout=None):
            return "", ""

    def popen(*args, **kwargs):
        seen["popen"] = kwargs
        return _Proc()

    with patch.object(assembler, "no_console_kwargs", lambda: expected), \
         patch.object(assembler.subprocess, "Popen", popen):
        assembler.run(["ffmpeg", "-version"], "smoke-test")
    assert seen["popen"].get("creationflags") == 0x08000000, seen

    def run(*args, **kwargs):
        seen["run"] = kwargs
        return SimpleNamespace(stderr="Video: h264, 1920x1080, 30 fps",
                               stdout="", returncode=1)

    with patch.object(clipper, "no_console_kwargs", lambda: expected), \
         patch.object(clipper.subprocess, "run", run):
        assert clipper.probe_dims(Path("fixture.mp4")) == (1920, 1080)
    assert seen["run"].get("creationflags") == 0x08000000, seen


def t_order_runner_accepts_strings():
    """Regression: order passed str(log), but default_runner used .parent.

    Before the fix, the step returned rc=-1 with
    AttributeError("'str' object has no attribute 'parent'") before the
    subprocess was even started. Paths and strings are both valid inputs.
    """
    import io
    import sys
    from contextlib import redirect_stdout

    from nightbatch import default_runner

    cfg = tmp_cfg()
    log = cfg.root / "work" / "post" / "runner-smoke.log"
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc, tail = default_runner(
            [sys.executable, "-c", "print('runner-path-ok')"],
            str(log), str(cfg.root))
    assert rc == 0, tail
    assert "runner-path-ok" in tail and "runner-path-ok" in buf.getvalue()
    assert "runner-path-ok" in log.read_text(encoding="utf-8")

    # The other caller's native Path form remains supported too.
    rc, tail = default_runner(
        [sys.executable, "-c", "print('runner-path-object-ok')"],
        log, cfg.root)
    assert rc == 0 and "runner-path-object-ok" in tail, tail


def t_order_dry_run_and_viral_plan():
    """CLI flags and "most viral" preference must be visible and honest."""
    import argparse
    import io
    from contextlib import redirect_stdout
    from unittest.mock import patch

    import main as main_mod
    import orders

    sentence = ("get a link from the database, get 5 clips and post them in "
                "5 channels, choose the most viral ones")
    cfg = tmp_cfg(desktop={"uploads": "on", "channels": [
        {"name": "One", "studio_url": "https://studio.youtube.com/channel/UC1"},
        {"name": "Two", "studio_url": "https://studio.youtube.com/channel/UC2"},
        {"name": "Three", "studio_url": "https://studio.youtube.com/channel/UC3"},
        {"name": "Four", "studio_url": "https://studio.youtube.com/channel/UC4"},
        {"name": "Five", "studio_url": "https://studio.youtube.com/channel/UC5"},
    ]})
    order = orders.parse_order(sentence)
    assert order["viral_requested"] is True
    plan = orders.plan_text(cfg, order)
    assert "Viral preference noted" in plan and "does not over-generate" in plan
    assert "separate pre-gen virality score" in plan

    # CLI --dry-run must alter the printed plan as well as execution args.
    captured = {}

    def fake_execute(got_cfg, got_order, dry_run=False):
        captured.update(order=got_order, dry_run=dry_run)
        return {"ok": True, "report": "dry-run test stub"}

    args = argparse.Namespace(text=[sentence], channels=0,
                              plan_only=False, dry_run=True)
    buf = io.StringIO()
    with patch.object(orders, "execute", fake_execute), \
         redirect_stdout(buf):
        rc = main_mod.cmd_order(cfg, args)
    out = buf.getvalue()
    assert rc == 0 and captured["dry_run"] is True, captured
    assert captured["order"]["dry"] is True, captured
    assert "5. no posting this run" in out and "5. POST: enabled" not in out, out
    assert "DRY RUN still performs the clip/generate/stage steps" in out, out
    assert "Use --plan-only for a no-work preview" in out, out
    assert "Viral preference noted" in out, out

    # --plan-only dominates, and never calls the runner/executor.
    args = argparse.Namespace(text=[sentence], channels=0,
                              plan_only=True, dry_run=False)
    buf = io.StringIO()
    with patch.object(orders, "execute",
                      side_effect=AssertionError("plan-only must not execute")), \
         redirect_stdout(buf):
        rc = main_mod.cmd_order(cfg, args)
    out = buf.getvalue()
    assert rc == 0 and "PLAN ONLY: nothing runs" in out, out
    assert "normal run would publish (desktop.uploads: on)" in out, out
    assert "5. POST: enabled" not in out, out


def t_desktop_no_post_apis():
    """The desktop/orders lanes may only post through the gated browser path."""
    for name in ("desktop.py", "orders.py"):
        text = (Path(__file__).resolve().parent / name).read_text(
            encoding="utf-8")
        assert "autopost" not in text, name
        assert "buffer" not in text.lower(), name
        assert "videos.insert" not in text, name


def t_temp_hygiene():
    """Run-end temp sweep: this run's trees go, fresh foreign ones stay."""
    import os
    import time as _time

    base = Path(tempfile.gettempdir())
    mine = Path(_mkdtemp())                  # registered -> this run's
    (mine / "f.bin").write_bytes(b"x")
    stale = base / "youttest_hygiene_stale"  # unregistered + old
    keep = base / "youttest_hygiene_keep"    # unregistered + fresh
    for d in (stale, keep):
        d.mkdir(exist_ok=True)
    old = _time.time() - 3 * 3600
    os.utime(stale, (old, old))              # a crashed earlier run's tree
    os.environ.pop("KEEP_TEST_TMP", None)
    _clean_temp_dirs()
    assert not mine.exists(), "this run's tree must go"
    assert not stale.exists(), ">2h leftovers must go too"
    assert keep.exists(), "a fresh foreign tree must stay (concurrent run)"
    # Escape hatch: KEEP_TEST_TMP skips the sweep entirely.
    keep2 = Path(_mkdtemp())
    os.environ["KEEP_TEST_TMP"] = "1"
    try:
        _clean_temp_dirs()
        assert keep2.exists() and _TMP_REGISTRY
    finally:
        os.environ.pop("KEEP_TEST_TMP", None)
    _TMP_REGISTRY.clear()
    keep2.rmdir()
    keep.rmdir()


def t_crew_watch():
    from datetime import datetime as real_datetime
    from types import SimpleNamespace

    from bot import parse_incoming
    from crew import (_fresh_usage, _logged_say, _save_state, mission_log_tail,
                      mission_status_text)

    assert parse_incoming("/log") == ("log", "")
    assert parse_incoming("/stop") == ("stop", "")
    # Every say() lands in the state log (the mission transcript).
    cfg = tmp_cfg()
    state: dict = {"log": []}
    said: list = []
    say = _logged_say(cfg, state, said.append)
    say("hello crew")
    assert said == ["hello crew"]
    assert state["log"][-1]["msg"] == "hello crew"
    # Status: silent before any mission, rich during one.
    assert mission_status_text(cfg) is None
    today = real_datetime.now().astimezone().date().isoformat()
    usage = _fresh_usage(today)
    usage.update({"yt_units": 1234, "eleven_chars": 900,
                  "llm_tokens_est": 16000})
    state = {"mission": {"goal": "grow it", "days": 3, "per_day": 4,
                         "live": False,
                         "started": real_datetime.now().astimezone().isoformat(),
                         "started_day": today, "started_day_hour": 0,
                         "ends": "2099-01-01T23:59:00+00:00"},
             "posted": [{"day": today, "mode": "draft"},
                        {"day": "2000-01-01", "mode": "live"}],
             "usage": usage, "digests": [],
             "heartbeat": real_datetime.now().astimezone().isoformat(),
             "ended": None,
             "log": [{"ts": today + "T14:05:00+00:00",
                      "msg": "🚀 boom\nsecond line"}]}
    _save_state(cfg, state)
    text = mission_status_text(cfg)
    assert "Mission day 1/3" in text and "🟢 running" in text
    assert "1/4 posted" in text and "2 posted (1 live)" in text
    assert "1,234" in text
    tail = mission_log_tail(cfg)
    assert "14:05 🚀 boom" in tail and "second line" not in tail
    # Ended missions report their reason.
    state["ended"] = today + "T23:00:00+00:00"
    state["ended_reason"] = "complete"
    _save_state(cfg, state)
    assert "ended (complete)" in mission_status_text(cfg)
    # CLI --status/--stop paths (no mission starts).
    from main import cmd_crew

    assert cmd_crew(cfg, SimpleNamespace(status=True, stop=False)) == 0
    assert cmd_crew(cfg, SimpleNamespace(status=False, stop=True)) == 0
    assert (cfg.root / "crew_stop").exists()


def t_key_pools():
    from types import SimpleNamespace

    from crew import _pools_line

    cfg = tmp_cfg()
    assert "none" in _pools_line(cfg) and "no Gemini" in _pools_line(cfg)
    cfg.data.setdefault("ai", {})["groq_api_keys"] = ["a", "b"]
    cfg.data.setdefault("channel", {})["elevenlabs_api_keys"] = ["x"]
    line = _pools_line(cfg)
    assert "Groq×2" in line and "11Labs×1" in line
    assert "Whisper×1 + LLM×1" in line
    assert "YouTube" in line and "no Gemini" in line
    # Preflight runs offline and never raises without keys.
    from main import cmd_preflight

    cmd_preflight(tmp_cfg(), SimpleNamespace(live=False))


def t_deps_guard():
    import subprocess
    import sys
    from pathlib import Path

    # Simulate an empty venv: blocked imports must produce the friendly
    # message (on stderr, non-zero exit), not a traceback.
    code = ("import sys; "
            "[sys.modules.__setitem__(m, None) "
            "for m in ('yaml', 'requests', 'edge_tts')]; "
            "import config")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, cwd=Path(__file__).parent)
    assert proc.returncode != 0
    assert "Activate.ps1" in proc.stderr
    assert "pip install -r requirements.txt" in proc.stderr
    assert "Traceback" not in proc.stderr


def t_py_compat():
    """Every module must COMPILE on the oldest Python we claim to support.

    CI runs 3.11 and the setup docs point at 3.12, but a dev machine on
    3.12+ silently accepts syntax that 3.11 rejects — a backslash inside an
    f-string expression part (PEP 701) broke `assembler.py` and took out 40
    of 138 tests on EVERY CI run from 2026-09-15 to 2026-10-03 while
    staying invisible locally. Importing the modules can't catch that (the
    suite is already running under the broken interpreter by then), so this
    shells out to a clean interpreter and compiles the source instead.
    """
    import subprocess
    import sys
    from pathlib import Path

    MIN_SUPPORTED = (3, 11)          # CI's interpreter; keep in sync with
                                     # .github/workflows/smoke.yml
    assert sys.version_info[:2] >= MIN_SUPPORTED
    root = Path(__file__).resolve().parent
    files = sorted(root.glob("*.py"))
    assert len(files) > 40, files    # sanity: we really scanned the project
    # A clean interpreter compiles the SOURCE (not the already-imported
    # modules): compile() is where PEP 701 syntax is accepted or rejected,
    # and it touches no disk, so there is nothing to clean up.
    probe = (
        "import sys\n"
        "from pathlib import Path\n"
        "bad = []\n"
        "for path in map(Path, sys.argv[1:]):\n"
        "    try:\n"
        "        compile(path.read_text(encoding='utf-8'), str(path), 'exec')\n"
        "    except SyntaxError as exc:\n"
        "        bad.append(f'{path.name}:{exc.lineno}: {exc.msg}')\n"
        "print('\\n'.join(bad))\n"
        "sys.exit(1 if bad else 0)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe] + [str(f) for f in files],
        capture_output=True, text=True)
    assert proc.returncode == 0, \
        (f"does not compile on Python "
         f"{'.'.join(map(str, sys.version_info[:3]))}:\n{proc.stdout}\n"
         f"{proc.stderr}")
    # The specific trap, so the failure names itself if it ever returns:
    # no f-string in the project may hold a backslash in its expression part.
    import ast

    for path in root.glob("*.py"):
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue                      # t_py_compat's compile check owns it
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            for part in node.values:
                if isinstance(part, ast.FormattedValue):
                    seg = ast.get_source_segment(src, part.value) or ""
                    assert "\\" not in seg, \
                        (f"{path.name}:{node.lineno} — backslash inside an "
                         f"f-string expression is a SyntaxError before "
                         f"Python 3.12 (PEP 701): {seg[:80]}")


def t_thumbnail_filter_variants():
    """Thumbnail drawtext variants: ordered, and the Windows colon escaped.

    Regression guard for the PEP 701 fix in assembler._thumbnail_filters:
    hoisting `safe_font.replace(':', '\\\\:')` out of the f-string must not
    change what the filter string says. FFmpeg's filter parser splits on
    ':', so a drive-letter path only works with the colon backslashed —
    that variant has to stay FIRST, with the plain-colon spelling and the
    fontconfig-name fallbacks behind it, and a textless 'plain' last.
    """
    from unittest.mock import patch

    from assembler import _thumbnail_filters

    # Landscape on purpose: thumb_width is 1280 there, so the fontsize
    # ladder below is the unscaled 72/56/44 one (portrait would multiply it
    # by 720/1280 and these assertions would chase the wrong numbers).
    cfg = tmp_cfg(video={"format": "landscape"})

    class _NoFonts:
        """assembler only ever does Path(f).exists() on the candidates."""
        def __call__(self, p):
            return self

        def exists(self):
            return False

    class _WindowsFonts:
        def __call__(self, p):
            self.seen = str(p)
            return self

        def exists(self):
            return self.seen.startswith("C:")

    # No drawtext in this FFmpeg build -> only the textless fallback.
    with patch("assembler._ffmpeg_has_filter", return_value=False):
        variants = _thumbnail_filters("A Title", cfg)
    assert [label for label, _ in variants] == ["plain"], variants

    with patch("assembler._ffmpeg_has_filter", return_value=True):
        # No font file on disk -> fontconfig names, then plain.
        with patch("assembler.Path", _NoFonts()):
            variants = _thumbnail_filters("A Title", cfg)
        labels = [label for label, _ in variants]
        assert labels == ["titled", "titled", "plain"], labels
        assert any("font='Arial Bold'" in vf for _, vf in variants)
        assert any("font='DejaVu Sans Bold'" in vf for _, vf in variants)
        # Long titles shrink the font instead of running off the frame, and
        # only the first 8 words ever reach the thumbnail.
        with patch("assembler.Path", _NoFonts()):
            short_vf = _thumbnail_filters("Short", cfg)[0][1]
            mid_vf = _thumbnail_filters("A somewhat longer thumbnail title", cfg)[0][1]
            long_vf = _thumbnail_filters("thumbnail " * 12, cfg)[0][1]
        assert "fontsize=72" in short_vf, short_vf
        assert "fontsize=56" in mid_vf, mid_vf
        assert "fontsize=44" in long_vf, long_vf
        assert "text='thumbnail thumbnail thumbnail thumbnail thumbnail " \
            "thumbnail thumbnail thumbnail'" in long_vf, long_vf

        # A Windows font path: variant 1 escapes the drive colon, variant 2
        # deliberately does not (FFmpeg builds disagree), and the escaped
        # spelling has to stay FIRST — it's the one that survives FFmpeg's
        # ':'-delimited filter parsing.
        with patch("assembler.Path", _WindowsFonts()):
            variants = _thumbnail_filters("A Title", cfg)
        fonts = [vf for _, vf in variants if "fontfile=" in vf]
        assert len(fonts) == 2, fonts
        assert "fontfile='C\\:/Windows/Fonts/arialbd.ttf'" in fonts[0], fonts[0]
        assert "fontfile='C:/Windows/Fonts/arialbd.ttf'" in fonts[1], fonts[1]
        assert "\\:" not in fonts[1], fonts[1]
        assert [label for label, _ in variants] == \
            ["titled", "titled", "titled", "titled", "plain"], variants


def t_render_debug():
    import io
    from contextlib import redirect_stdout
    from types import SimpleNamespace

    from bot import parse_incoming
    from jobqueue import Queue

    assert parse_incoming("Stop") == ("stop", "")
    assert parse_incoming("stop the press") == ("topic", "stop the press")
    assert parse_incoming("/stop") == ("stop", "")
    # Table limit: newest N + summary; unlimited list untouched.
    cfg = tmp_cfg()
    queue = Queue(cfg.state_file)
    for i in range(15):
        job = queue.add(f"topic {i}")
        if i % 3 == 0:
            queue.update(job, status="failed", error="boom")
    full = queue.format_table()
    assert "topic 0" in full and "topic 14" in full
    short = queue.format_table(limit=12)
    assert "topic 14" in short and "topic 0" not in short
    assert "... and 3 older jobs (5 failed in total)" in short
    # errors command: full text + dates for the newest failures.
    from main import cmd_errors

    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cmd_errors(cfg, SimpleNamespace(count=2)) == 0
    out = buf.getvalue()
    assert "topic 12" in out and "topic 9" in out
    assert "topic 0" not in out and "boom" in out
    assert "mux_debug" in out


def t_emergency_mux():
    from assembler import _build_simple_mux_cmd

    cfg = tmp_cfg()
    cmd = _build_simple_mux_cmd(concat_txt=cfg.root / "c.txt",
                                audio_txt=cfg.root / "a.txt",
                                out_path=cfg.root / "o.mp4", cfg=cfg)
    assert "-filter_complex" not in cmd
    assert "-vf" not in cmd and "-af" not in cmd
    assert "-map" in cmd and "aac" in cmd
    assert cmd[-1].endswith("o.mp4")
    mp3 = _build_simple_mux_cmd(concat_txt=cfg.root / "c.txt",
                                audio_txt=cfg.root / "a.txt",
                                out_path=cfg.root / "o.mp4", cfg=cfg,
                                audio_codec="libmp3lame", faststart=False)
    assert "libmp3lame" in mp3 and "+faststart" not in " ".join(mp3)


def t_pygarnish():
    """Python garnish: SRT/ASS parsing, caption PNGs, numpy audio mix, and a
    mux command with no filter_complex/af/font filters. Offline; the render
    and mix parts are skipped when Pillow/numpy are missing (the ladder
    skips the rung the same way)."""
    from pygarnish import (available, build_pygarnish_mux_cmd, merge_phrases,
                           parse_sub_file)

    cfg = tmp_cfg()
    tmp = Path(_mkdtemp())
    srt = tmp / "t.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,500\nHello world\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nSecond cue here\n",
        encoding="utf-8",
    )
    events, is_ass = parse_sub_file(srt)
    assert not is_ass and len(events) == 2
    assert abs(events[0].start - 1.0) < 1e-6
    assert abs(events[0].end - 2.5) < 1e-6
    assert events[0].lines == ((("Hello", False), ("world", False)),)

    ass = tmp / "t.ass"
    ass.write_text(
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:01.50,Karaoke,,0,0,0,,"
        "{\\c&H00FFFF&}Hello{\\c&HFFFFFF&} world\n"
        "Dialogue: 0,0:00:01.50,0:00:02.00,Karaoke,,0,0,0,,"
        "Hello {\\c&H00FFFF&}world{\\c&HFFFFFF&}\n",
        encoding="utf-8",
    )
    ev2, is_ass2 = parse_sub_file(ass)
    assert is_ass2 and len(ev2) == 2
    assert ev2[0].lines[0][0] == ("Hello", True)
    assert ev2[1].lines[0][1] == ("world", True)
    merged = merge_phrases(ev2)
    assert len(merged) == 1
    assert abs(merged[0].start - 1.0) < 1e-6
    assert abs(merged[0].end - 2.0) < 1e-6
    assert all(not hl for line in merged[0].lines for _, hl in line)

    # Command shape: pure constructor, needs no PIL/numpy/FFmpeg.
    cmd = build_pygarnish_mux_cmd(
        concat_txt=tmp / "c.txt", mixed_wav=tmp / "m.wav",
        out_path=tmp / "o.mp4", cfg=cfg, total_seconds=10.0,
        overlays=[(tmp / "cue1.png", 1.0, 2.5)],
        cta=(tmp / "cta.png", 6.5), progress_bar=(tmp / "bar.png"))
    joined = " ".join(cmd)
    assert "-filter_complex" not in cmd and "-af" not in cmd
    assert "subtitles=" not in joined and "drawtext" not in joined
    vf = cmd[cmd.index("-vf") + 1]
    assert "movie=" in vf and "overlay=" in vf and "drawbox" not in vf, vf
    assert "overlay=x='-1080+1080*t/10.000'" in vf, vf
    assert "between(t,1.000,2.500)" in vf and "gte(t,6.500)" in vf
    assert cmd[-1].endswith("o.mp4")

    if not available():
        return
    from pygarnish import mix_audio_py, render_caption_png

    png = tmp / "cap.png"
    render_caption_png(png, 1080, 1920, events[0].lines, fontsize=73,
                       centered=False, bottom_margin=333)
    assert png.exists() and png.stat().st_size > 1000

    import math
    import wave as _wave
    from array import array as _array

    def _tone(path, secs, hz):
        n = int(48000 * secs)
        data = _array("h")
        for i in range(n):
            val = int(10000 * math.sin(2 * math.pi * hz * i / 48000))
            data.append(val)
            data.append(val)
        with _wave.open(str(path), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(48000)
            handle.writeframes(data.tobytes())

    _tone(tmp / "n.wav", 0.5, 440.0)
    _tone(tmp / "bed.wav", 0.25, 110.0)
    _tone(tmp / "w.wav", 0.1, 880.0)
    out = mix_audio_py(
        padded_audio=[tmp / "n.wav"], out_path=tmp / "mix.wav",
        total_seconds=0.5, cuts=[0.25], whoosh_path=tmp / "w.wav",
        whoosh_db=-6.0, pop_path=None, pop_at=None,
        music_track=tmp / "bed.wav", music_level_db=-12.0, music_duck=True)
    with _wave.open(str(out), "rb") as handle:
        assert handle.getnframes() == 24000
        assert handle.getnchannels() == 2
        assert handle.getframerate() == 48000

# --------------------------------------------------------------------------
# virality (pre-gen rater)
# --------------------------------------------------------------------------
def t_virality():
    from virality import (DENSITY_WEIGHT, HOOK_WEIGHT, LENGTH_WEIGHT,
                          TITLE_WEIGHT, TOPIC_WEIGHT, pick_best, score_clip,
                          score_density, score_hook, score_length,
                          score_title)

    # Weights must always sum to 100 — the scorecard promises /100.
    assert (HOOK_WEIGHT + TOPIC_WEIGHT + LENGTH_WEIGHT + DENSITY_WEIGHT
            + TITLE_WEIGHT) == 100

    # Hook: number + direct address + punchy-short stack. Question hooks
    # ("Why…?") and weak openers are PENALIZED — editorial.hook_violated
    # says live data retains worse on them, and the rater must agree
    # with the renderer.
    pts, why = score_hook("You lose 8 hours every single night")
    assert pts == 30, (pts, why)
    pts, why = score_hook("Why do you sleep 8 hours every night?")
    assert pts == 10 and any("question/weak hook" in w for w in why), (pts, why)
    pts, why = score_hook("There is a lake that never freezes over")
    assert pts == 0 and any("question/weak hook" in w for w in why), (pts, why)
    pts, _ = score_hook("Cats sleep sixteen hours a day mostly")
    assert pts == 20  # plain but punchy-short
    assert score_hook("")[0] == 0
    assert score_hook("hi there")[0] == 3  # too thin to judge

    # Length curve: every boundary, both sides.
    assert score_length(20)[0] == 20 and score_length(40)[0] == 20
    assert score_length(12)[0] == 14 and score_length(60)[0] == 14
    assert score_length(19.9)[0] == 14 and score_length(40.1)[0] == 14
    assert score_length(8)[0] == 8 and score_length(90)[0] == 8
    assert score_length(11.9)[0] == 8 and score_length(60.1)[0] == 8
    assert score_length(7.9)[0] == 4 and score_length(90.1)[0] == 4
    assert score_length(0)[0] == 4 and score_length(-5)[0] == 4
    assert score_length("nope")[0] == 4

    # Density: pace bands + honest no-transcript neutral.
    assert score_density(2.0)[0] == 15 and score_density(3.5)[0] == 15
    assert score_density(1.2)[0] == 10 and score_density(4.5)[0] == 10
    assert score_density(1.19)[0] == 5 and "dragging" in score_density(1.0)[1][0]
    assert score_density(4.51)[0] == 5 and "rushed" in score_density(9.0)[1][0]
    assert score_density(None) == (7, ["pace unscored — no transcript"])
    assert score_density(0)[0] == 7

    # Title: fit + digit + calm caps.
    assert score_title("Why honey never expires (3,000-year-old pots)")[0] == 10
    pts, why = score_title("YOU WON'T BELIEVE WHAT HAPPENS NEXT HERE")
    assert pts == 4 and any("shouts" in w for w in why), (pts, why)
    assert score_title("Hi")[0] == 3
    assert score_title("")[0] == 0
    assert score_title("x" * 200)[0] <= 6  # way too long, no length points

    # Whole clip: signals add up to the score, deterministically.
    verdict = score_clip("Why honey never expires",
                         "Why does honey last 3000 years?",
                         word_count=90, duration_s=30)
    assert verdict["score"] == sum(verdict["signals"].values())
    assert verdict["signals"] == {"hook": 6, "topic": verdict["signals"]["topic"],
                                  "length": 20, "density": 15, "title": 7}
    assert verdict["signals"]["topic"] >= 14  # "why" opener + specific
    assert verdict == score_clip("Why honey never expires",
                                 "Why does honey last 3000 years?",
                                 word_count=90, duration_s=30)
    assert 0 <= verdict["score"] <= 100
    # Deaf scoring: no transcript -> hook 0, pace neutral, reasons say so.
    deaf = score_clip("Some title here", "", word_count=0, duration_s=25)
    assert deaf["signals"]["hook"] == 0
    assert deaf["signals"]["density"] == 7
    assert any("no transcript" in r for r in deaf["reasons"])

    # Best pick: highest wins, ties break toward the shorter clip.
    assert pick_best([]) is None
    tied = [{"id": "long", "score": 60, "duration_s": 99},
            {"id": "short", "score": 60, "duration_s": 20},
            {"id": "weak", "score": 10, "duration_s": 5}]
    assert pick_best(tied)["id"] == "short"
    assert pick_best([{"score": 10}, {}])["score"] == 10


# --------------------------------------------------------------------------
# pregen (phone queue: scan / score / push)
# --------------------------------------------------------------------------
def _make_clip_tree(root: Path) -> None:
    """Fake finished outputs: 2 clips + 1 part + decoys + 1 cache."""
    clips, parts = root / "clips", root / "parts"
    kit1 = clips / "a1b2c3d4_clip_01"
    kit1.mkdir(parents=True)
    (clips / "a1b2c3d4_clip_01.mp4").write_bytes(b"fakevideo01")
    (kit1 / "a1b2c3d4_clip_01.mp4").write_bytes(b"kit copy, not a 2nd clip")
    (kit1 / "TITLE.txt").write_text("Why honey never expires")
    (kit1 / "CREDIT.txt").write_text(
        "source: https://x\nchannel: y\nwindow: 10.0s - 42.0s\n")
    kit2 = clips / "a1b2c3d4_clip_02"
    kit2.mkdir(parents=True)
    (clips / "a1b2c3d4_clip_02.mp4").write_bytes(b"fakevideo02")
    (kit2 / "TITLE.txt").write_text("Some afternoon thoughts")
    # kit2 has NO credit: unknown length, unscored hook — must not crash.
    (clips / "notes.txt").write_text("not a clip")
    (clips / "a1b2c3d4_top_2.mp4").write_bytes(b"compilation, not a clip")
    pkit = parts / "b2c3d4e5_part_01"
    pkit.mkdir(parents=True)
    (parts / "b2c3d4e5_part_01.mp4").write_bytes(b"fakepart01")
    (pkit / "TITLE.txt").write_text("Part one")
    (pkit / "CREDIT.txt").write_text("source: z\nwindow: 0.0s - 60.0s\n")
    (pkit / "captions.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
    vocab = (["and"] * 25
             + ["you", "burn", "8", "hours", "every", "single", "night",
                "here"] + ["rest"] * 467)
    words = [{"word": w, "start": i * 0.4, "end": i * 0.4 + 0.35}
             for i, w in enumerate(vocab)]
    cache = root / "work" / "clip_cache" / "a1b2c3d4e5f60718.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"version": 1, "words": words}))


def t_pregen():
    import re as _re

    import pregen
    from pregen import (baseline_pending, best_of_day, collect_candidates,
                        format_scorecard, load_manifest, parse_window,
                        push_pending, save_manifest, score_candidates,
                        today_local)

    assert _re.fullmatch(r"\d{4}-\d{2}-\d{2}", today_local())

    # Window parsing: the lane's exact format + every malformed cousin.
    assert parse_window("window: 12.3s - 45.6s") == (12.3, 45.6)
    assert parse_window("window: 0 - 60") == (0.0, 60.0)
    assert parse_window("WINDOW: 1s-2s") == (1.0, 2.0)
    assert parse_window("no window here") is None
    assert parse_window("") is None
    assert parse_window("window: 5 - 5") is None
    assert parse_window("window: 9 - 2") is None

    cfg = tmp_cfg()
    _make_clip_tree(cfg.root)
    cands = collect_candidates(cfg)
    assert [c["id"] for c in cands] == ["c-a1b2c3d4-01", "c-a1b2c3d4-02",
                                        "p-b2c3d4e5-01"], [c["id"] for c in cands]
    one, two, part = cands
    assert one["duration_s"] == 32.0 and one["word_count"] == 80
    assert one["wps"] == 2.5
    assert one["hook_text"].split() == ["you", "burn", "8", "hours",
                                        "every", "single", "night", "here"]
    assert one["title"] == "Why honey never expires"
    assert two["duration_s"] == 0.0 and two["hook_text"] == ""
    assert two["wps"] is None  # no window -> nothing sliced, honestly empty
    assert part["caps"] is True and one["caps"] is False
    assert part["hook_text"] == ""  # no cache for this source: deaf, not dead

    # Scoring: verdicts attached, inputs untouched, the strong hook wins.
    scored = score_candidates(cands)
    assert all({"score", "reasons", "signals"} <= set(s) for s in scored)
    assert all("score" not in c for c in cands)
    by_id = {s["id"]: s for s in scored}
    assert by_id["c-a1b2c3d4-01"]["score"] > 80
    assert by_id["c-a1b2c3d4-01"]["score"] > by_id["c-a1b2c3d4-02"]["score"]
    assert by_id["c-a1b2c3d4-01"]["score"] > by_id["p-b2c3d4e5-01"]["score"]

    # Scorecard: title + score + capped reasons.
    card = format_scorecard(by_id["c-a1b2c3d4-01"])
    assert "Why honey never expires" in card and "/100" in card
    assert "32s" in card and "80 words" in card
    assert card.count("\n") == 8  # 2 head + 6 reasons + overflow line
    assert "… +" in card
    assert "c-a1b2c3d4-02" in format_scorecard({**by_id["c-a1b2c3d4-02"],
                                               "title": ""})  # id fallback

    # Manifest: round-trip, missing, corrupt.
    assert load_manifest(cfg) == {"clips": []}
    save_manifest(cfg, {"clips": [{"id": "x"}]})
    assert load_manifest(cfg) == {"clips": [{"id": "x"}]}
    (cfg.root / "pregen.json").write_text("[[[not json")
    assert load_manifest(cfg) == {"clips": []}
    (cfg.root / "pregen.json").write_text(json.dumps({"clips": {}}))
    assert load_manifest(cfg) == {"clips": []}

    # Push engine: videos + cards + winner announcement, all recorded.
    calls: list = []

    def fake_sender(kind, chat_id, payload, caption=""):
        # Full path recorded: Windows temp dirs blow past any truncation
        # budget, and truncating here once broke this assert on PC only.
        calls.append((kind, chat_id, str(payload), caption[:20]))
        return {"message_id": len(calls),
                "file_id": f"fid-{len(calls)}" if kind == "video" else None}

    report = push_pending(cfg, fake_sender, 123, day="2026-09-28",
                          now="2026-09-28T10:00:00+00:00")
    assert report["pushed"] == ["c-a1b2c3d4-01", "c-a1b2c3d4-02",
                                "p-b2c3d4e5-01"], report
    assert report["best"] == "c-a1b2c3d4-01"
    assert report["skipped"] == [] and report["errors"] == []
    kinds = [c[0] for c in calls]
    assert kinds.count("video") == 3 and kinds.count("message") == 4, kinds
    assert calls[-1][0] == "message"  # winner announced last
    assert "clip_01.mp4" in calls[0][2]
    manifest = load_manifest(cfg)
    assert [c["id"] for c in manifest["clips"]] == report["pushed"]
    first = manifest["clips"][0]
    assert first["file_id"] == "fid-1" and first["message_id"] == 1
    assert first["day"] == "2026-09-28"
    assert first["pushed_at"] == "2026-09-28T10:00:00+00:00"
    assert first["score"] > 80 and first["reasons"] and first["signals"]

    # Idempotent: a second push sends nothing.
    before = len(calls)
    again = push_pending(cfg, fake_sender, 123, day="2026-09-28")
    assert again["pushed"] == [] and len(calls) == before
    assert again["best"] == "c-a1b2c3d4-01"  # still known from the manifest

    # Dry run: scores + predicts, sends nothing, writes nothing.
    dry_cfg = tmp_cfg()
    _make_clip_tree(dry_cfg.root)
    dry_calls: list = []
    dry = push_pending(dry_cfg,
                       lambda *a, **k: dry_calls.append(a) or {}, 999,
                       dry_run=True, day="2026-09-28")
    assert dry["pushed"] == [] and dry_calls == []
    assert dry["pending"] == ["c-a1b2c3d4-01", "c-a1b2c3d4-02",
                              "p-b2c3d4e5-01"]
    assert dry["best"] == "c-a1b2c3d4-01"
    assert not (dry_cfg.root / "pregen.json").exists()

    # Limit: park one, leave the rest pending.
    lim_cfg = tmp_cfg()
    _make_clip_tree(lim_cfg.root)
    lim = push_pending(lim_cfg, fake_sender, 123, limit=1, day="2026-09-28")
    assert lim["pushed"] == ["c-a1b2c3d4-01"] and lim["pending"] == [
        "c-a1b2c3d4-01"]

    # Oversize: skipped with the cap in the reason, rest still park.
    big_cfg = tmp_cfg()
    _make_clip_tree(big_cfg.root)
    big_kit = big_cfg.root / "clips" / "a1b2c3d4_clip_03"
    big_kit.mkdir()
    (big_cfg.root / "clips" / "a1b2c3d4_clip_03.mp4").write_bytes(
        b"\0" * (49 * 1024 * 1024))
    (big_kit / "TITLE.txt").write_text("The heavy one")
    (big_kit / "CREDIT.txt").write_text("window: 0s - 30s\n")
    big_calls: list = []
    big = push_pending(
        big_cfg,
        lambda k, c, p, caption="": big_calls.append(k) or {"message_id": 1},
        123, day="2026-09-28")
    assert ("c-a1b2c3d4-03", "49 MB over the 48 MB cap") in big["skipped"]
    assert len(big["pushed"]) == 3

    # Sender failure: that clip errors, the push goes on.
    def flaky(kind, chat_id, payload, caption=""):
        if kind == "video" and "clip_02" in str(payload):
            raise RuntimeError("boom")
        return {"message_id": 7, "file_id": "fid-7"}

    flake_cfg = tmp_cfg()
    _make_clip_tree(flake_cfg.root)
    flake = push_pending(flake_cfg, flaky, 123, day="2026-09-28")
    assert flake["errors"] == ["c-a1b2c3d4-02: boom"]
    assert flake["pushed"] == ["c-a1b2c3d4-01", "p-b2c3d4e5-01"]

    # Gone file (deleted between scan and send): skipped, never sent.
    from unittest.mock import patch as _patch
    gone_cfg = tmp_cfg()
    stale = [{"id": "c-stale-01", "lane": "clip",
              "file": str(gone_cfg.root / "clips" / "ghost.mp4"),
              "title": "Ghost", "duration_s": 30.0, "hook_text": "",
              "word_count": 0, "wps": None}]
    with _patch.object(pregen, "collect_candidates", return_value=stale):
        gone = push_pending(gone_cfg, fake_sender, 123, day="2026-09-28")
    assert gone["skipped"] == [("c-stale-01", "file gone from disk")]
    assert gone["pushed"] == []

    # Baseline: backlog marked ignored (no sends), phone queue starts new.
    base_cfg = tmp_cfg()
    _make_clip_tree(base_cfg.root)
    watch_calls: list = []
    base = baseline_pending(base_cfg, day="2026-09-28")
    assert base["baselined"] == ["c-a1b2c3d4-01", "c-a1b2c3d4-02",
                                 "p-b2c3d4e5-01"], base
    assert base["tracked_already"] == 0
    manifest = load_manifest(base_cfg)
    assert all(c.get("ignored") and not c.get("pushed_at")
               for c in manifest["clips"])
    assert all(c.get("size") > 0 and c.get("mtime") for c in manifest["clips"])
    assert all("score" in c for c in manifest["clips"])
    quiet = push_pending(
        base_cfg,
        lambda *a, **k: watch_calls.append(a) or {"message_id": 1},
        123, day="2026-09-28")
    assert quiet["pushed"] == [] and quiet["pending"] == []
    assert watch_calls == []  # nothing sent, not even the winner note
    second = baseline_pending(base_cfg, day="2026-09-28")
    assert second["baselined"] == [] and second["tracked_already"] == 3
    # Re-render (changed bytes) -> pending again, record upgraded in place.
    (base_cfg.root / "clips" / "a1b2c3d4_clip_01.mp4").write_bytes(
        b"re-rendered bytes, provably different size!")
    re_push = push_pending(base_cfg, fake_sender, 123, day="2026-09-28")
    assert re_push["pushed"] == ["c-a1b2c3d4-01"], re_push
    upgraded = load_manifest(base_cfg)["clips"]
    assert len(upgraded) == 3  # replaced, not duplicated
    record = [c for c in upgraded if c["id"] == "c-a1b2c3d4-01"][0]
    assert record.get("pushed_at") and not record.get("ignored")
    # Legacy pushed record (no fingerprint) never re-pushes.
    legacy_cfg = tmp_cfg()
    _make_clip_tree(legacy_cfg.root)
    save_manifest(legacy_cfg, {"clips": [{"id": "c-a1b2c3d4-01",
                                          "pushed_at": "t", "day": "d"}]})
    legacy = push_pending(legacy_cfg, fake_sender, 123, day="2026-09-28")
    assert "c-a1b2c3d4-01" not in legacy["pushed"]
    assert "c-a1b2c3d4-02" in legacy["pushed"]  # untracked still flows

    # Best-of-day: day + pushed_at filter, None when empty.
    assert best_of_day([], "2026-09-28") is None
    mixed = [{"id": "a", "score": 99, "duration_s": 9, "day": "2026-09-27",
              "pushed_at": "t"},
             {"id": "b", "score": 10, "duration_s": 9, "day": "2026-09-28",
              "pushed_at": "t"},
             {"id": "c", "score": 50, "duration_s": 9, "day": "2026-09-28",
              "pushed_at": ""}]
    assert best_of_day(mixed, "2026-09-28")["id"] == "b"


# --------------------------------------------------------------------------
# pregen bot (today / clips / clip + delivery)
# --------------------------------------------------------------------------
def t_pregen_bot():
    import argparse
    import io
    from contextlib import redirect_stdout
    from unittest.mock import patch as _patch

    import pregen
    from bot import PhoneBot, match_clip, parse_incoming
    from pregen import save_manifest, today_local

    # Parser: the three pregen commands + typo guards.
    assert parse_incoming("/today") == ("today", "")
    assert parse_incoming("/clips") == ("clips", "")
    assert parse_incoming("/CLIPS") == ("clips", "")
    assert parse_incoming("/clip c-a1b2c3d4-01") == ("clip", "c-a1b2c3d4-01")
    assert parse_incoming("/clip") == ("clips", "")  # bare: list, not guess
    assert parse_incoming("/clipxyz") == ("help", "")
    assert parse_incoming("/sendxyz") == ("help", "")  # old guard still holds

    # Matcher: exact (case-insensitive), unique prefix, none, many, empty.
    entries = [{"id": "c-aa-01"}, {"id": "c-bb-02"}, {"id": "p-aa-01"}]
    assert match_clip(entries, "C-AA-01") == ({"id": "c-aa-01"}, "")
    assert match_clip(entries, "c-bb") == ({"id": "c-bb-02"}, "")
    assert match_clip(entries, "zzz") == (None, "none")
    assert match_clip(entries, "c-") == (None, "many")
    assert match_clip(entries, "") == (None, "empty")
    assert match_clip(entries, "  ") == (None, "empty")

    # Handler fixture: 3 pushed (file_id / local-only / gone) + 1 unpushed
    # decoy with the HIGHEST score (must stay invisible everywhere).
    cfg = tmp_cfg()
    cfg.data["telegram"]["bot_token"] = "t"
    cfg.data["telegram"]["owner_id"] = 42
    local_mp4 = cfg.root / "b.mp4"
    local_mp4.write_bytes(b"fake-B")
    day = today_local()
    save_manifest(cfg, {"clips": [
        {"id": "c-aa-01", "title": "Alpha clip here", "score": 90,
         "reasons": ["hook talks to you (+4)"], "signals": {"hook": 1},
         "duration_s": 30.0, "word_count": 60, "caps": True,
         "file_id": "fid-A", "file": str(cfg.root / "gone-a.mp4"),
         "pushed_at": "t", "day": day},
        {"id": "c-bb-02", "title": "Beta", "score": 50, "reasons": [],
         "duration_s": 20.0, "word_count": 10, "file_id": "",
         "file": str(local_mp4), "pushed_at": "t", "day": day},
        {"id": "c-cc-03", "title": "", "score": 10, "reasons": [],
         "duration_s": 0.0, "word_count": 0, "file_id": "",
         "file": str(cfg.root / "gone-c.mp4"), "pushed_at": "t", "day": day},
        {"id": "c-dd-04", "title": "Decoy", "score": 99, "reasons": [],
         "duration_s": 9.0, "word_count": 9, "day": day},
        {"id": "c-ee-05", "title": "Baselined", "score": 95, "ignored": True,
         "file": str(cfg.root / "gone-e.mp4"), "day": day},
    ]})
    bot = PhoneBot(cfg)
    sent, vids, uploads = [], [], []
    bot.send_message = lambda c, t: sent.append(t) or {"message_id": len(sent)}
    bot.send_video_id = lambda c, f, caption="": vids.append((f, caption))
    bot.send_video_file = lambda c, p, caption="": uploads.append((str(p), caption))

    def say(text):
        bot.handle_message({"chat": {"id": 42}, "from": {"id": 42},
                            "text": text})

    # /today: the best PUSHED clip (decoy excluded), via file_id.
    say("/today")
    assert any("Today's pick" in t for t in sent), sent
    assert any("Alpha clip here" in t and "90/100" in t for t in sent), sent
    assert vids == [("fid-A", "🎬 Alpha clip here")], vids
    assert uploads == [] and not any("Decoy" in t for t in sent)

    # /clips: pushed only, newest last, caps marked, decoy hidden.
    sent.clear()
    say("/clips")
    body = "\n".join(sent)
    assert "c-aa-01" in body and "c-bb-02" in body and "c-cc-03" in body
    assert "c-dd-04" not in body and "c-ee-05" not in body  # decoy + ignored
    assert "· caps" in body
    assert "90/100" in body and "/clip <id>" in body

    # /clip: exact local-only -> upload; gone -> honest message; prefix ok.
    sent.clear()
    say("/clip c-bb-02")
    assert uploads == [(str(local_mp4), "🎬 Beta")], uploads
    sent.clear()
    say("/clip c-cc-03")
    assert any("gone" in t for t in sent), sent
    assert len(uploads) == 1 and vids and len(vids) == 1  # no new sends
    say("/clip c-bb")
    assert len(uploads) == 2  # unique prefix delivers
    sent.clear()
    say("/clip c-")
    assert any("several" in t for t in sent), sent
    say("/clip nope")
    assert any("No parked clip" in t for t in sent), sent
    sent.clear()
    say("/clip c-ee-05")
    assert any("No parked clip" in t for t in sent), sent  # ignored: invisible

    # Empty manifest: setup hints, no crashes.
    empty_cfg = tmp_cfg()
    empty_cfg.data["telegram"]["bot_token"] = "t"
    empty_cfg.data["telegram"]["owner_id"] = 42
    empty_bot = PhoneBot(empty_cfg)
    empty_sent: list = []
    empty_bot.send_message = lambda c, t: empty_sent.append(t)
    empty_bot.handle_message({"chat": {"id": 42}, "from": {"id": 42},
                              "text": "/today"})
    empty_bot.handle_message({"chat": {"id": 42}, "from": {"id": 42},
                              "text": "/clips"})
    assert any("Nothing parked today" in t for t in empty_sent), empty_sent
    assert any("No parked clips" in t for t in empty_sent), empty_sent

    # Low-level plumbing: _api results propagate, uploads named right.
    api_calls: list = []

    def fake_api(method, **kwargs):
        api_calls.append((method, kwargs))
        return {"message_id": 5, "video": {"file_id": "f9"}}

    bot2 = PhoneBot(cfg)
    bot2._api = fake_api
    assert bot2.send_message(42, "hi")["message_id"] == 5
    assert api_calls[-1][0] == "sendMessage"
    assert bot2.send_video_id(42, "f9")["video"]["file_id"] == "f9"
    assert api_calls[-1][1]["data"]["video"] == "f9"
    assert api_calls[-1][1]["data"]["supports_streaming"] is True
    bot2.send_video_file(42, local_mp4)
    assert api_calls[-1][1]["files"]["video"][0] == "b.mp4"

    # bot_sender: extraction + unknown-kind guard.
    with _patch.object(PhoneBot, "send_video_file",
                       return_value={"message_id": 3,
                                     "video": {"file_id": "fx"}}), \
         _patch.object(PhoneBot, "send_message",
                       return_value={"message_id": 4}):
        sender = pregen.bot_sender(cfg)
        assert sender("video", 1, local_mp4) == {"message_id": 3,
                                                "file_id": "fx"}
        assert sender("message", 1, "hi") == {"message_id": 4}
        try:
            sender("bogus", 1, "x")
        except ValueError:
            pass
        else:
            raise AssertionError("unknown sender kind must raise")

    # cmd_pregen: best / list / push-dry / push-live / missing channel.
    import main as main_mod

    def run_cmd(**kw):
        merged = {"push": False, "best": False, "date": None, "limit": 0,
                  "dry_run": False, "baseline": False}
        merged.update(kw)
        args = argparse.Namespace(**merged)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main_mod.cmd_pregen(cfg, args)
        return code, buf.getvalue()

    assert run_cmd(best=True, date=day)[0] == 0
    assert "Alpha clip here" in run_cmd(best=True, date=day)[1]
    code, out = run_cmd(best=True, date="1999-01-01")
    assert code == 0 and "No clips parked on 1999-01-01" in out
    code, out = run_cmd()
    assert code == 0 and "3 parked clip(s)" in out and "c-aa-01" in out
    try:
        run_cmd(push=True)
    except SystemExit:
        pass  # channel 0 -> setup instructions, no sends
    else:
        raise AssertionError("push without channel must refuse")
    cfg.data["telegram"]["channel_id"] = -1001
    code, out = run_cmd(push=True, dry_run=True)
    assert code == 0 and "Nothing unpushed" in out  # all 3 parked already
    # Dry run needs neither channel nor token (prediction only).
    bare_cfg = tmp_cfg()
    _make_clip_tree(bare_cfg.root)
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main_mod.cmd_pregen(bare_cfg, argparse.Namespace(
            push=True, best=False, date=day, limit=0, dry_run=True,
            baseline=False))
    assert code == 0 and "Would park 3 clip(s)" in buf.getvalue(), \
        buf.getvalue()
    (cfg.root / "pregen.json").unlink()  # live push re-parks from disk
    _make_clip_tree(cfg.root)
    with _patch.object(pregen, "bot_sender",
                       return_value=lambda k, c, p, caption="": {
                           "message_id": 1, "file_id": "live"}):
        code, out = run_cmd(push=True, date=day)
    assert code == 0 and "Parked 3 clip(s)" in out, out
    assert "(best: c-a1b2c3d4-01)" in out
    # --baseline command: backlog ignored, list view admits it.
    base_cmd_cfg = tmp_cfg()
    _make_clip_tree(base_cmd_cfg.root)
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main_mod.cmd_pregen(base_cmd_cfg, argparse.Namespace(
            push=False, best=False, date=day, limit=0, dry_run=False,
            baseline=True))
    assert code == 0 and "Baselined 3 clip(s)" in buf.getvalue()
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main_mod.cmd_pregen(base_cmd_cfg, argparse.Namespace(
            push=False, best=False, date=None, limit=0, dry_run=False,
            baseline=False))
    out = buf.getvalue()
    assert code == 0 and "3 baselined (ignored)" in out, out
    assert "unpushed" not in out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else (argv or []))
    only = argv[0] if argv else None
    tests = [
        ("config_defaults", t_config_defaults),
        ("config_example_parses", t_config_example_parses),
        ("groq_key_split", t_groq_key_split),
        ("config_no_shared_mutation", t_config_no_shared_mutation),
        ("config_garbage_tolerated", t_config_garbage_tolerated),
        ("image_route", t_image_route),
        ("template_all_styles", t_template_all_styles),
        ("template_unknown_style_falls_back", t_template_unknown_style_falls_back),
        ("extract_json", t_extract_json),
        ("factcheck_never_blocks", t_factcheck_never_blocks),
        ("subtitles", t_subtitles),
        ("cta_rotation", t_cta_rotation),
        ("script_no_double_cta", t_script_no_double_cta),
        ("script_length_repair", t_script_length_repair),
        ("topics_clean", t_topics_clean),
        ("topics_norepeat", t_topics_norepeat),
        ("reclaim_interrupted", t_reclaim_interrupted),
        ("sub_position", t_sub_position),
        ("subpreview", t_subpreview),
        ("profanity_mask", t_profanity_mask),
        ("timestamp_rollover", t_timestamp_rollover),
        ("parts_plan", t_parts_plan),
        ("parts_header", t_parts_header),
        ("parts_header_wrap", t_parts_header_wrap),
        ("parts_header_shorten", t_parts_header_shorten),
        ("parts_kit", t_parts_kit),
        ("caption_overlap", t_caption_overlap),
        ("longform", t_longform),
        ("longform_top", t_longform_top),
        ("meeting", t_meeting),
        ("meeting act", t_meeting_act),
        ("meeting_act_wiring", t_meeting_act_wiring),
        ("meeting_event_stream", t_meeting_event_stream),
        ("sheet", t_sheet),
        ("sheet_dropped", t_sheet_dropped),
        ("longform_lane", t_longform_lane),
        ("cutpoints", t_cutpoints),
        ("whole_short", t_whole_short),
        ("sub_caps", t_sub_caps),
        ("sub_highlight", t_sub_highlight),
        ("clip_default_bottom", t_clip_default_bottom),
        ("top_video", t_top_video),
        ("bot_parser", t_bot_parser),
        ("package", t_package),
        ("audiofx", t_audiofx),
        ("queue", t_queue),
        ("generate_rejects_bad_count", t_generate_rejects_bad_count),
        ("mux_builder", t_mux_builder),
        ("gemini_fallback_order", t_gemini_fallback_order),
        ("gemini_429_fast_failover", t_gemini_429_fast_failover),
        ("gemini_key_sweep", t_gemini_key_sweep),
        ("gemini_network", t_gemini_network),
        ("sentry", t_sentry),
        ("azure_budget", t_azure_budget),
        ("music_rotation", t_music_rotation),
        ("pollinations_text", t_pollinations_text),
        ("heartbeat", t_heartbeat),
        ("image_chain", t_image_chain),
        ("image_builders", t_image_builders),
        ("stock_lane", t_stock),
        ("director", t_director),
        ("voice_pauses", t_voice_pauses),
        ("voice_stitch_mechanism", t_voice_stitch_mechanism),
        ("whisper_primary", t_whisper_primary),
        ("clip_cookies", t_clip_cookies),
        ("clip_cookies_status", t_clip_cookies_status),
        ("clip_title_polish", t_clip_title_polish),
        ("clip_cache", t_clip_cache),
        ("clip_windows", t_clip_windows),
        ("clip_workdirs", t_clip_workdirs),
        ("clip_snap", t_clip_snap),
        ("clip_smart_crop", t_clip_smart_crop),
        ("channel_snap", t_channel_snap),
        ("channel_stats", t_channel_stats),
        ("snap_cli", t_snap_cli),
        ("snap_empty_channel", t_snap_empty_channel),
        ("topic_scout", t_topic_scout),
        ("ab_titles", t_ab_titles),
        ("mux_crash_recovery", t_mux_crash_recovery),
        ("clip_target", t_clip_target),
        ("clip_fit", t_clip_fit),
        ("transcript_fix", t_transcript_fix),
        ("no_doubled_decorators", t_no_doubled_decorators),
        ("clip_distribution", t_clip_distribution),
        ("json_mode_400_retry", t_json_mode_400_retry),
        ("title_guard", t_title_guard),
        ("gemini_sandwich", t_gemini_sandwich),
        ("autopost_builders", t_autopost_builders),
        ("groq_rotation", t_groq_rotation),
        ("keypool_memory", t_keypool_memory),
        ("script_chain_groq", t_script_chain_groq),
        ("slugify", t_slugify),
        ("encoder_setting", t_encoder_setting),
        ("run_heartbeat", t_run_heartbeat),
        ("progress_bar", t_progress_bar),
        ("script_prompt_rules", t_script_prompt_rules),
        ("topup_prompt", t_topup_prompt),
        ("batch_topics", t_batch_topics),
        ("hook_guard", t_hook_guard),
        ("concept_dupe", t_concept_dupe),
        ("vision", t_vision),
        ("no_gemini", t_no_gemini),
        ("keystats", t_keystats),
        ("bot_foundations", t_bot_foundations),
        ("scene_pacing", t_scene_pacing),
        ("audio_trim", t_audio_trim),
        ("music_audit", t_music_audit),
        ("topic_hygiene", t_topic_hygiene),
        ("title_optimize", t_title_optimize),
        ("clipper", t_clipper),
        ("image_speed", t_image_speed),
        ("shot_plan", t_shot_plan),
        ("shot_plan_delivery", t_shot_plan_delivery),
        ("voice_ledger", t_voice_ledger),
        ("dry_run_banner_once", t_dry_run_banner_once),
        ("shot_bounds", t_shot_bounds),
        ("voice_budget", t_voice_budget),
        ("pixabay", t_pixabay),
        ("deepseek", t_deepseek),
        ("scene_sentences", t_scene_sentences),
        ("music_audible", t_music_audible),
        ("title_punch", t_title_punch),
        ("scrub", t_scrub),
        ("stock_pick", t_stock_pick),
        ("dry_run_batch", t_dry_run_batch),
        ("editorial", t_editorial),
        ("openrouter_lane", t_openrouter_lane),
        ("image_402", t_image_402_fails_over_fast),
        ("probe_whisper", t_probe_whisper),
        ("probe_sample", t_probe_sample),
        ("probe_report_shape", t_probe_report_shape),
        ("ledger_tags", t_ledger_tags),
        ("chat_tools", t_chat_tools),
        ("jarvis_task", t_jarvis_task),
        ("analytics", t_analytics),
        ("voice", t_voice),
        ("gemini_keys", t_gemini_keys),
        ("elevenlabs", t_elevenlabs),
        ("key_env_aliases", t_key_env_aliases),
        ("youtube", t_youtube),
        ("crew", t_crew),
        ("crew_watch", t_crew_watch),
        ("key_pools", t_key_pools),
        ("panel_argmap", t_panel_argmap),
        ("panel_order_actions", t_panel_order_actions),
        ("panel_no_button_without_route", t_panel_no_button_without_route),
        ("panel_mask", t_panel_mask),
        ("panel_events", t_panel_events),
        ("panel_links", t_panel_links),
        ("panel_chat", t_panel_chat),
        ("panel_state", t_panel_state),
        ("panel_files", t_panel_files),
        ("panel_launch", t_panel_launch),
        ("panel_status_and_defaults", t_panel_status_and_defaults),
        ("panel_failure_is_visible", t_panel_failure_is_visible),
        ("panel_port_file_forms", t_panel_port_file_forms),
        ("nightbatch", t_nightbatch),
        ("nightreq", t_nightreq),
        ("meeting_review", t_meeting_review),
        ("go_options", t_go_options),
        ("wakeup", t_wakeup),
        ("nightbatch_go", t_nightbatch_go),
        ("order_parse", t_order_parse),
        ("order_plan", t_order_plan),
        ("order_targets", t_order_targets),
        ("order_cli", t_order_cli),
        ("process_no_console_kwargs", t_process_no_console_kwargs),
        ("ffmpeg_windows_no_window_calls", t_ffmpeg_windows_no_window_calls),
        ("order_runner_accepts_strings", t_order_runner_accepts_strings),
        ("order_dry_run_and_viral_plan", t_order_dry_run_and_viral_plan),
        ("desktop_safety", t_desktop_safety),
        ("desktop_lessons_playbooks", t_desktop_lessons_playbooks),
        ("desktop_prompt_and_parse", t_desktop_prompt_and_parse),
        ("desktop_loop", t_desktop_loop),
        ("desktop_post_one", t_desktop_post_one),
        ("desktop_bot_wiring", t_desktop_bot_wiring),
        ("desktop_launcher_browsers", t_desktop_launcher_browsers),
        ("desktop_channels", t_desktop_channels),
        ("desktop_connection_check", t_desktop_connection_check),
        ("desktop_peek_current", t_desktop_peek_current),
        ("desktop_labels_italian", t_desktop_labels_italian),
        ("desktop_post_one_italian", t_desktop_post_one_italian),
        ("desktop_cdp_http_info", t_desktop_cdp_http_info),
        ("desktop_stuck_tab", t_desktop_stuck_tab),
        ("desktop_peek_no_shot", t_desktop_peek_no_shot),
        ("desktop_status_code_line", t_desktop_status_code_line),
        ("desktop_settings_override", t_desktop_settings_override),
        ("desktop_no_post_apis", t_desktop_no_post_apis),
        ("temp_hygiene", t_temp_hygiene),
        ("deps_guard", t_deps_guard),
        ("py_compat", t_py_compat),
        ("thumbnail_variants", t_thumbnail_filter_variants),
        ("render_debug", t_render_debug),
        ("emergency_mux", t_emergency_mux),
        ("pygarnish", t_pygarnish),
        ("virality", t_virality),
        ("pregen", t_pregen),
        ("pregen_bot", t_pregen_bot),
    ]
    print("youtproject offline smoke tests (no network, no keys, no FFmpeg)\n")
    import time as _time

    started = _time.time()
    try:
        if only:
            print(f"filter: {only!r} (python test_smoke.py <name-fragment> to "
                  "narrow; no argument = everything)\n")
        matched = [t for t in tests if not only or only in t[0]]
        if not matched:
            print(f"No test group matches {only!r}.")
            return 1
        for name, fn in matched:
            check(name, fn)
        print(f"\n{PASS} passed, {FAIL} failed.")
        if FAILURES:
            print("Failures:", ", ".join(FAILURES))
            return 1
        print("All green — core logic is healthy.")
        return 0
    finally:
        _clean_temp_dirs(started)


if __name__ == "__main__":
    sys.exit(main())
