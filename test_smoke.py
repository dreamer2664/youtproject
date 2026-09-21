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

    tmp = Path(tempfile.mkdtemp(prefix="youttest_"))
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


def t_config_no_shared_mutation():
    """One Config's overrides must never leak into another (or DEFAULTS)."""
    from config import DEFAULTS, load_config

    tmp = Path(tempfile.mkdtemp(prefix="youttest_"))
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

    tmp = Path(tempfile.mkdtemp(prefix="youttest_"))
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

    tmp = Path(tempfile.mkdtemp(prefix="youttest_")) / "backlog.txt"
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
    tmp = Path(tempfile.mkdtemp(prefix="youttest_")) / "backlog.txt"
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

    tmp = Path(tempfile.mkdtemp(prefix="youttest_"))
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

    assets = ensure_assets(Path(tempfile.mkdtemp(prefix="youttest_")))
    for key in ("music_loop", "whoosh", "pop"):
        assert assets[key].exists() and assets[key].stat().st_size > 1000, key


def t_queue():
    from jobqueue import Queue

    tmp = Path(tempfile.mkdtemp(prefix="youttest_")) / "state.json"
    queue = Queue(tmp)
    job = queue.add("sharks")
    assert queue.get(job.id[:6]).id == job.id  # prefix lookup
    queue.update(job, status="generated", title="T")
    assert Queue(tmp).get(job.id).status == "generated"  # persisted
    assert len(Queue(tmp).pending()) == 1
    tmp.write_text("{corrupt", encoding="utf-8")
    assert Queue(tmp).jobs == []  # corrupt file -> moved aside, not fatal
    assert tmp.with_suffix(".corrupt.json").exists()


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
    tmp = Path(tempfile.mkdtemp(prefix="youttest_"))
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
    assert cfg.image_fallbacks == ["pollinations", "gemini"]
    assert cfg.image_model == "flux"
    assert resolve_chain(cfg) == ["pexels", "pollinations", "gemini"]
    assert provider_ready("pollinations", cfg) == (True, "anonymous")
    assert provider_ready("gemini", cfg)[0] is False
    assert provider_ready("pexels", cfg) == (False, "no Pexels key")
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

    tmp = Path(tempfile.mkdtemp(prefix="youttest_"))
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


def t_voice_ssml():
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from voiceover import build_ssml, split_sentences, synthesise

    assert split_sentences("Hello world. How are you? Fine!") == [
        "Hello world.", "How are you?", "Fine!"]
    assert split_sentences("  ") == []
    ssml = build_ssml("Fish & chips. Yum.", "en-X", "+40%", 250)
    assert ssml.startswith("<speak") and 'rate="+40%"' in ssml
    assert ssml.count('<break time="250ms"/>') == 1
    assert "Fish &amp; chips" in ssml
    # SSML rejected by the engine -> silent plain-text retry saves the scene.
    cfg = tmp_cfg()
    assert cfg.sentence_pause_ms == 250
    calls = []

    async def fake_synth(text, voice, rate, dest):
        calls.append(text)
        if text.startswith("<speak"):
            raise RuntimeError("SSML no")
        dest.write_bytes(b"x" * 2000)
        return []

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "s.mp3"
        with patch("voiceover._synth", side_effect=fake_synth), \
                patch("time.sleep"):
            synthesise("Hello. World.", dest, cfg)
        assert dest.stat().st_size == 2000
    assert len(calls) == 2 and not calls[1].startswith("<speak")


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
    cfg.data["ai"]["groq_api_keys"] = ["q"]
    cfg.data["ai"]["openrouter_api_keys"] = ["o"]
    chain = get_provider(cfg).chain
    assert [label for label, _ in chain] == [
        "gemini", "groq", "openrouter", "gemini-reserve", "pollinations",
        "template"]
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
    tmp = Path(tempfile.mkdtemp(prefix="youttest_"))
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
    cfg.data["ai"]["groq_api_keys"] = ["q"]
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
    assert sums["...x7f2"] == {"req": 2, "tok": 600, "chars": 0, "units": 0}
    # Pruning: events older than the keep window vanish on write.
    stale = {"t": (datetime.now(timezone.utc)
                   - timedelta(days=40)).isoformat(),
             "p": "gemini", "k": "...old1", "req": 9}
    keystats._write(keystats._path, events + [stale])
    assert all(event["k"] != "...old1"
               for event in keystats._load(keystats._path))

    # Dashboard: sections, masked keys, refill wording, no secrets.
    cfg.data["ai"]["gemini_api_key"] = "AIzaSyFULL-KEY-MATERIALL-x7f2"
    cfg.data["channel"]["elevenlabs_api_keys"] = ["sk-full-secret-3d10"]
    out = keystats.build_status(cfg)
    assert "GEMINI" in out and "...x7f2" in out and "resets" in out
    assert "ELEVENLABS" in out and "6,000" in out and "4,000 left" in out
    assert "AIzaSyFULL" not in out and "sk-full-secret" not in out
    # Groq pool line: shared org-wide limit shows a total.
    cfg.data["ai"]["groq_api_keys"] = ["gq-full-secret-bb18"]
    out = keystats.build_status(cfg)
    assert "GROQ" in out and "pool total" in out and "14,400" in out

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

    from topics import _clean, _looks_junky, pop_fresh_topic, propose_topics

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
    assert vision.TIMEOUT == 12 and vision.MAX_REQUESTS == 3
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
    assert [label for label, _ in get_provider(cfg).chain] == ["pollinations", "template"]
    cfg.data["ai"]["groq_api_keys"] = ["k1", "k2"]
    assert [label for label, _ in get_provider(cfg).chain] == ["groq", "pollinations", "template"]
    cfg.data["ai"]["gemini_api_key"] = "g"
    assert [label for label, _ in get_provider(cfg).chain] == ["gemini", "groq", "gemini-reserve", "pollinations", "template"]
    cfg.data["ai"]["provider"] = "groq"
    assert [label for label, _ in get_provider(cfg).chain] == ["groq", "gemini", "gemini-reserve", "pollinations", "template"]
    cfg.data["ai"]["provider"] = "nonsense"  # garbage primary -> gemini first
    assert [label for label, _ in get_provider(cfg).chain] == ["gemini", "groq", "gemini-reserve", "pollinations", "template"]
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
    # chain: openrouter sits after groq, before template.
    cfg = tmp_cfg()
    cfg.data["ai"]["gemini_api_key"] = "g"
    cfg.data["ai"]["groq_api_keys"] = ["q"]
    cfg.data["ai"]["openrouter_api_keys"] = ["o"]
    assert [label for label, _ in get_provider(cfg).chain] == [
        "gemini", "groq", "openrouter", "gemini-reserve", "pollinations",
        "template"]
    cfg.data["ai"]["groq_api_keys"] = []
    cfg.data["ai"]["gemini_api_key"] = ""
    assert [label for label, _ in get_provider(cfg).chain] == ["openrouter", "pollinations", "template"]


def t_broll():
    from unittest.mock import Mock, patch

    from broll import _pick_file, search_clips

    portrait_hd = {"id": 1, "file_type": "video/mp4", "width": 1080, "height": 1920,
                   "fps": 30, "link": "https://v/p.mp4"}
    landscape_4k = {"id": 2, "file_type": "video/mp4", "width": 3840, "height": 2160,
                    "fps": 30, "link": "https://v/l.mp4"}
    assert _pick_file({"video_files": [landscape_4k, portrait_hd]}) == portrait_hd
    assert _pick_file({"video_files": [landscape_4k]}) == landscape_4k
    assert _pick_file({"video_files": []}) is None
    assert _pick_file({}) is None

    body = {"videos": [{"id": 9, "url": "https://p/9", "duration": 16,
                        "image": "https://i/9.jpg", "video_files": [portrait_hd]}]}
    resp = Mock(status_code=200)
    resp.json.return_value = body
    with patch("broll.requests.get", return_value=resp) as get:
        clips = search_clips("KEY", "ocean waves", per_page=3)
    assert len(clips) == 1 and clips[0]["file"] == "https://v/p.mp4"
    assert clips[0]["height"] == 1920 and clips[0]["duration"] == 16
    assert get.call_args.kwargs["headers"] == {"Authorization": "KEY"}
    assert get.call_args.kwargs["params"]["orientation"] == "portrait"
    with patch("broll.requests.get",
               return_value=Mock(status_code=401, text="bad")):
        try:
            search_clips("BAD", "x")
        except RuntimeError as exc:
            assert "rejected" in str(exc)
        else:
            raise AssertionError("expected RuntimeError on 401")


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
        assert "No LLM keys" in jarvis.run_task(tmp_cfg(), "hi")
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
    cfg.data.setdefault("ai", {})["groq_api_keys"] = ["k1", "k2"]
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
    tmp = Path(tempfile.mkdtemp(prefix="youttest_"))
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

def main() -> int:
    tests = [
        ("config_defaults", t_config_defaults),
        ("config_example_parses", t_config_example_parses),
        ("config_no_shared_mutation", t_config_no_shared_mutation),
        ("config_garbage_tolerated", t_config_garbage_tolerated),
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
        ("voice_ssml", t_voice_ssml),
        ("title_guard", t_title_guard),
        ("gemini_sandwich", t_gemini_sandwich),
        ("autopost_builders", t_autopost_builders),
        ("groq_rotation", t_groq_rotation),
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
        ("shot_bounds", t_shot_bounds),
        ("scene_sentences", t_scene_sentences),
        ("music_audible", t_music_audible),
        ("title_punch", t_title_punch),
        ("scrub", t_scrub),
        ("stock_pick", t_stock_pick),
        ("dry_run_batch", t_dry_run_batch),
        ("editorial", t_editorial),
        ("openrouter_lane", t_openrouter_lane),
        ("broll", t_broll),
        ("chat_tools", t_chat_tools),
        ("jarvis_task", t_jarvis_task),
        ("analytics", t_analytics),
        ("voice", t_voice),
        ("gemini_keys", t_gemini_keys),
        ("elevenlabs", t_elevenlabs),
        ("youtube", t_youtube),
        ("crew", t_crew),
        ("crew_watch", t_crew_watch),
        ("key_pools", t_key_pools),
        ("deps_guard", t_deps_guard),
        ("render_debug", t_render_debug),
        ("emergency_mux", t_emergency_mux),
        ("pygarnish", t_pygarnish),
    ]
    print("youtproject offline smoke tests (no network, no keys, no FFmpeg)\n")
    for name, fn in tests:
        check(name, fn)
    print(f"\n{PASS} passed, {FAIL} failed.")
    if FAILURES:
        print("Failures:", ", ".join(FAILURES))
        return 1
    print("All green — core logic is healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
