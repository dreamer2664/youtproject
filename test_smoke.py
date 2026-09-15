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
    """The final-mux command builder: full mix has every garnish, the
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
        **base, with_burn=True, with_progress=True, with_cta=True,
        with_candy=True)
    vf = full[full.index("-vf") + 1]
    assert "subtitles=" in vf and "drawbox" in vf, vf
    if _ffmpeg_has_filter("drawtext") and _drawtext_font_arg() is not None:
        assert "drawtext" in vf, vf
    graph = full[full.index("-filter_complex") + 1]
    assert "amerge" in graph, graph
    assert full.count("-i") == 5  # video + narration + music + whoosh + pop

    nocta = _build_mux_cmd(**base, with_burn=True, with_progress=True,
                           with_cta=False, with_candy=True)
    assert nocta.count("-i") == 4  # pop dropped with the card
    assert "drawtext" not in nocta[nocta.index("-vf") + 1]

    mini = _build_mux_cmd(**base, with_burn=False, with_progress=False,
                          with_cta=False, with_candy=False)
    assert "-vf" not in mini
    mgraph = mini[mini.index("-filter_complex") + 1]
    assert mgraph.startswith("[1:a]volume=1.0,afade"), mgraph
    assert "amerge" not in mgraph
    assert mini[-1] == str(tmp / "out.mp4")


def t_gemini_fallback_order():
    from scriptgen import GeminiProvider

    # Empirically verified 2026-09-15: the 2.5 IDs 404 ("no longer
    # available"), gemini-3-flash 404s on v1beta, and the rolling full-flash
    # alias plus 3.1-flash-lite 503 under afternoon load — while
    # flash-lite-latest answers 200. Keep it first; reorder only on
    # fresh evidence.
    assert GeminiProvider.FALLBACK_MODELS[0] == "gemini-flash-lite-latest"
    assert len(set(GeminiProvider.FALLBACK_MODELS)) == len(GeminiProvider.FALLBACK_MODELS)


def t_image_chain():
    from images import describe_chain, provider_ready, resolve_chain

    cfg = tmp_cfg()
    assert cfg.image_provider == "pollinations"
    assert cfg.image_fallbacks == ["gemini"]
    assert cfg.image_model == "flux"
    assert resolve_chain(cfg) == ["pollinations", "gemini"]
    assert provider_ready("pollinations", cfg) == (True, "anonymous")
    assert provider_ready("gemini", cfg)[0] is False
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
    assert post.call_args_list[1].kwargs["json"]["model"] == "openai/gpt-oss-120b"
    # 404 -> next model tried (qwen default, then 120b).
    with patch("requests.post", side_effect=[Mock(status_code=404, text="gone"), ok()]) as post:
        GroqProvider(api_keys=["k"])._complete("hi", temperature=0.0, json_mode=False, tag="t")
    bodies = [call.kwargs["json"] for call in post.call_args_list]
    assert [body["model"] for body in bodies] == ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"]
    # Org-level 429 names the organization: every key shares that fate,
    # so remaining keys are skipped and the next model pool is tried.
    org_429 = Mock(status_code=429, text="Rate limit reached for model `m` "
                                        "in organization `org_x` on tokens per minute (TPM)")
    with patch("requests.post", side_effect=[org_429, ok()]) as post:
        result = GroqProvider(api_keys=["k1", "k2", "k3"])._complete(
            "hi", temperature=0.0, json_mode=False, tag="t")
    assert result == "hello" and post.call_count == 2
    second = post.call_args_list[1]
    assert second.kwargs["json"]["model"] == "openai/gpt-oss-120b"
    assert second.kwargs["headers"] == {"Authorization": "Bearer k1"}
    # 5xx is server-side: no key will fix it, next model at once.
    with patch("requests.post", side_effect=[Mock(status_code=500, text="err"), ok()]) as post:
        GroqProvider(api_keys=["k1", "k2"])._complete(
            "hi", temperature=0.0, json_mode=False, tag="t")
    assert [call.kwargs["json"]["model"] for call in post.call_args_list] == [
        "qwen/qwen3.8-27b", "openai/gpt-oss-120b"]
    # Two hung requests in a row: next model, not every key x 60 s.
    from requests.exceptions import Timeout
    with patch("requests.post", side_effect=[Timeout(), Timeout(), ok()]) as post:
        result = GroqProvider(api_keys=["k1", "k2", "k3"])._complete(
            "hi", temperature=0.0, json_mode=False, tag="t")
    assert result == "hello" and post.call_count == 3
    assert post.call_args_list[2].kwargs["json"]["model"] == "openai/gpt-oss-120b"
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
    assert post.call_count == 4  # 2 models x 2 keys


def t_script_chain_groq():
    from scriptgen import ChainedProvider, get_provider

    cfg = tmp_cfg()
    assert [label for label, _ in get_provider(cfg).chain] == ["template"]
    cfg.data["ai"]["groq_api_keys"] = ["k1", "k2"]
    assert [label for label, _ in get_provider(cfg).chain] == ["groq", "template"]
    cfg.data["ai"]["gemini_api_key"] = "g"
    assert [label for label, _ in get_provider(cfg).chain] == ["gemini", "groq", "template"]
    cfg.data["ai"]["provider"] = "groq"
    assert [label for label, _ in get_provider(cfg).chain] == ["groq", "gemini", "template"]
    cfg.data["ai"]["provider"] = "nonsense"  # garbage primary -> gemini first
    assert [label for label, _ in get_provider(cfg).chain] == ["gemini", "groq", "template"]
    assert isinstance(get_provider(cfg), ChainedProvider)


def t_slugify():
    from bot import slugify

    assert slugify("The Secret Language of Trees!", "video") == "the-secret-language-of-trees"
    assert slugify("  ", "video") == "video"
    assert len(slugify("x" * 100, "video")) <= 50


def t_encoder_setting():
    from assembler import _ENCODER_ARGS

    cfg = tmp_cfg()
    assert cfg.encoder == "cpu"
    cfg.data["video"]["encoder"] = "NVENC"
    assert cfg.encoder == "nvenc"
    cfg.data["video"]["encoder"] = "nonsense"
    assert cfg.encoder == "cpu"
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

        def generate_text(self, prompt, temperature=0.7, tag="", json_mode=False):
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
    assert post.call_args_list[1].kwargs["json"]["model"] == "z-ai/glm-5.2:free"
    # plain 429 (own per-key RPM) still rotates keys on the same model.
    with patch("requests.post", side_effect=[Mock(status_code=429, text="slow"),
                                             ok()]) as post:
        OpenRouterProvider(api_keys=["k1", "k2"])._complete("hi", 0.0, False, "t")
    calls = post.call_args_list
    assert calls[1].kwargs["json"]["model"] == "nvidia/nemotron-3-super-120b-a12b:free"
    assert calls[1].kwargs["headers"]["Authorization"] == "Bearer k2"
    # chain: openrouter sits after groq, before template.
    cfg = tmp_cfg()
    cfg.data["ai"]["gemini_api_key"] = "g"
    cfg.data["ai"]["groq_api_keys"] = ["q"]
    cfg.data["ai"]["openrouter_api_keys"] = ["o"]
    assert [label for label, _ in get_provider(cfg).chain] == [
        "gemini", "groq", "openrouter", "template"]
    cfg.data["ai"]["groq_api_keys"] = []
    cfg.data["ai"]["gemini_api_key"] = ""
    assert [label for label, _ in get_provider(cfg).chain] == ["openrouter", "template"]


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
        ("image_chain", t_image_chain),
        ("image_builders", t_image_builders),
        ("autopost_builders", t_autopost_builders),
        ("groq_rotation", t_groq_rotation),
        ("script_chain_groq", t_script_chain_groq),
        ("slugify", t_slugify),
        ("encoder_setting", t_encoder_setting),
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
