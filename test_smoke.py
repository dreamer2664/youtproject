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

    # Empirically verified 2026-09-14: the rolling alias saturated (503)
    # and the 2.5/3.0 pinned IDs 404'd on v1beta, while 3.1-flash-lite
    # answered. Keep it first; reorder only on fresh evidence.
    assert GeminiProvider.FALLBACK_MODELS[0] == "gemini-3.1-flash-lite"
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
