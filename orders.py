"""Plain-language orders — "get 6 clips and post them in 6 channels".

The user types one sentence; this module turns it into real work:

    order → parse → resolve files (sheet/clips/kits) → map onto channels
          → stage upload packets → (desktop lane) post → report

The parse never guesses silently: everything it understood is printed back as
a plan first. Two rules are deliberate:

  * staging packets ALWAYS happens (no browser, no risk) — posting is the
    extra step that needs `desktop.uploads: on`;
  * the upload commit lives in desktop.post_one() and nowhere else, so there
    is exactly one place where "post" can happen and one place to gate it.

Nothing here uploads by itself. `--plan-only` runs no steps at all.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from desktop import (RunLog, dconf, make_driver, new_run, post_one)

# --------------------------------------------------------------------------
# parsing


def parse_order(text: str) -> dict:
    """Read an order sentence. Returns a dict; never raises.

    Recognized (case-insensitive, any order):
      get/grab/take a link from the database|sheet|db  → get_link
      get N clips / clip N videos                     → clips
      generate/make N videos                          → videos
      post (them) in/to N channels                    → post + channels
      don't post / stage only / no post               → post off
      dry / plan only                                 → dry
    """
    raw = re.sub(r"\s+", " ", (text or "").strip())
    low = raw.lower()
    order = {"raw": raw, "get_link": False, "clips": 0, "videos": 0,
             "channels": 0, "post": True, "dry": False,
             "viral_requested": bool(re.search(r"\b(viral|virality)\b", low)),
             "understood": []}

    num = r"(\d{1,2})"
    match = re.search(num + r"\s+clips?\b", low) or re.search(
        r"\bclips?\b\D{0,12}" + num, low)
    if match:
        order["clips"] = max(0, min(10, int(match.group(1))))
    elif re.search(r"\b(get|grab|take|make|pull|cut)\b[^.]{0,20}\bclip", low):
        order["clips"] = 1
    match = re.search(num + r"\s+(?:videos?|shorts?)\b", low)
    if match:
        order["videos"] = max(0, min(10, int(match.group(1))))
    match = re.search(r"(?:in|to|across|on|for)\s+" + num + r"\s+channels",
                      low)
    if match:
        order["channels"] = max(1, min(50, int(match.group(1))))

    if re.search(r"\b(link|url|source)\b", low) and re.search(
            r"\b(get|grab|take|pull|use|fetch|from)\b", low):
        order["get_link"] = True
    if re.search(r"\b(database|the db|sheet|list of links|backlog)\b", low):
        order["get_link"] = True
    if order["clips"] > 0:
        order["get_link"] = True          # the clip lane eats the sheet

    if re.search(r"\b(don'?t|do not|no)\s+post", low) or re.search(
            r"\b(stage only|just stage|without posting|no posting|dry)\b", low):
        order["post"] = False
    if re.search(r"\b(post|publish|upload)\b", low) and order["post"]:
        order["understood"].append("posting requested")
    if re.search(r"\b(dry|plan only|plan-only)\b", low):
        order["dry"] = True
    if re.search(r"\bpost\b", low) and order["post"]:
        order["post"] = True

    order["understood"] = [
        ("a source link from the sheet" if order["get_link"] else None),
        (f"{order['clips']} clips" if order["clips"] else None),
        (f"{order['videos']} generated videos" if order["videos"] else None),
        (f"post to {order['channels']} channels" if order["post"]
         and order["channels"] else ("post" if order["post"] else "stage only")),
    ]
    order["understood"] = [part for part in order["understood"] if part]
    order["ok"] = bool(order["clips"] or order["videos"] or order["get_link"])
    return order


def plan_text(cfg, order: dict) -> str:
    """The plan the user sees BEFORE anything runs."""
    conf = dconf(cfg)
    channels = channel_list(cfg)
    lines = [f"📋 Order: \"{order.get('raw')}\"", ""]
    if not order.get("ok"):
        lines.append("I could not find work in that sentence. Try: "
                     "\"get a link from the database, get 6 clips and post "
                     "them in 6 channels, and generate 2 videos\".")
        return "\n".join(lines)
    lines.append("I read that as:")
    for part in order.get("understood") or []:
        lines.append(f"  • {part}")
    lines.append("")
    if order["get_link"]:
        lines.append("1. take the next queued source from the sheet "
                     "(`clip --sheet 1`)")
    if order["clips"]:
        lines.append(f"2. cut up to {order['clips']} clips from it")
    if order["videos"]:
        lines.append(f"3. generate {order['videos']} video(s) from the "
                     "topic backlog")
    want = order["channels"] or len(channels)
    total = order["clips"] + order["videos"]
    if channels:
        lines.append(f"4. map {total} item(s) onto {min(want, len(channels))} "
                     f"channel(s): " + ", ".join(
                         c["name"] for c in channels[:max(1, want)]))
    else:
        lines.append("4. no channels configured yet (`desktop.channels:` in "
                     "config.yaml, or: python main.py desktop channels add "
                     "\"Name\" <studio link>) — packets will be staged in "
                     "work/post/ only")
    if order.get("plan_only"):
        if not order["post"] or total <= 0:
            suffix = "this order requests no posting"
        elif not channels:
            suffix = "no channels configured, so a normal run cannot post"
        elif conf["uploads"]:
            suffix = "normal run would publish (desktop.uploads: on)"
        else:
            suffix = "normal run would stage only (desktop.uploads: off)"
        lines.append(f"5. PLAN ONLY: nothing runs; {suffix}")
    elif order["post"] and not order["dry"]:
        if conf["uploads"]:
            lines.append("5. POST: enabled (desktop.uploads: on) — the "
                         "browser lane will upload and publish/schedule")
        else:
            lines.append("5. POST: staged only — desktop.uploads is off, so "
                         "the browser lane stops at the publish button")
    else:
        lines.append("5. no posting this run (staged only)")
    if order.get("viral_requested"):
        lines.append("")
        lines.append("⚠️ Viral preference noted: the clipper already asks its "
                     "moment-picker for the strongest standalone hooks/stories. "
                     "This order does not over-generate candidates or use the "
                     "separate pre-gen virality score to filter them; that score "
                     "is a heuristic, not a view prediction.")
    if order.get("plan_only"):
        lines.append("")
        lines.append("PLAN ONLY: no clip/generate/post steps run and no files "
                     "are changed.")
    elif order.get("dry"):
        lines.append("")
        lines.append("DRY RUN still performs the clip/generate/stage steps above "
                     "but suppresses the browser post. Use --plan-only for a "
                     "no-work preview; configured providers may still use "
                     "quota during processing.")
    lines.append("")
    lines.append("No publish click happens unless step 5 explicitly says "
                 "POST: enabled.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# channels & targets


def channel_list(cfg) -> list[dict]:
    """Every channel the desktop lane can post to: config.yaml entries PLUS
    runtime ones added with `desktop channels add` (deduped by channel id)."""
    from desktop import all_channels

    out = []
    for entry in all_channels(cfg):
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "channel"
        out.append({"name": name, "slug": slug,
                    "studio_url": str(entry.get("studio_url") or "").strip(),
                    "id": str(entry.get("id") or ""),
                    "source": str(entry.get("source") or "")})
    return out


def assign_channels(items: list[dict], channels: list[dict],
                    want: int = 0) -> list[dict]:
    """Round-robin items onto channels (6 clips / 6 channels = one each)."""
    if not channels:
        return [{"item": item, "channel": None} for item in items]
    pool = channels[:want] if want else channels
    assignments = []
    for index, item in enumerate(items):
        assignments.append({"item": item, "channel": pool[index % len(pool)]})
    return assignments


def resolve_new_clips(cfg, since_ts: float, limit: int) -> list[dict]:
    """Clips finished after `since_ts`, newest first, as upload items."""
    from pregen import collect_candidates

    found = []
    for cand in collect_candidates(cfg):
        try:
            mtime = Path(cand["file"]).stat().st_mtime
        except OSError:
            continue
        if mtime >= since_ts - 120:
            found.append(cand)

    def _order_key(cand: dict):
        try:
            return (Path(cand["file"]).stat().st_mtime, str(cand.get("id")))
        except OSError:
            return (0.0, str(cand.get("id")))

    found.sort(key=_order_key, reverse=True)   # newest first, id breaks ties
    items = []
    for cand in found[:limit]:
        kit = Path(cand.get("kit") or "")
        items.append({
            "kind": "clip", "id": cand["id"], "file": cand["file"],
            "title": _read(kit / "TITLE.txt") or cand.get("title", ""),
            "description": _read(kit / "DESCRIPTION.txt"),
            "tags": [], "kit": str(kit),
        })
    return items


def resolve_new_videos(cfg, since_ts: float, limit: int) -> list[dict]:
    """Generated videos finished after `since_ts`, newest first."""
    from jobqueue import Queue

    jobs = Queue(cfg.state_file).jobs
    fresh = [j for j in jobs
             if j.updated_at >= since_ts - 120 and j.status in
             ("generated", "packaged", "published")]
    fresh.sort(key=lambda j: j.updated_at, reverse=True)
    items = []
    for job in fresh[:limit]:
        kit = Path(job.package_dir) if job.package_dir else cfg.package_dir / job.id
        try:
            if not (kit / "video.mp4").exists():
                from package import build_package

                build_package(job, cfg)
                kit = Path(job.package_dir) if job.package_dir else kit
        except Exception:  # noqa: BLE001 - stage what exists, report the rest
            pass
        video = kit / "video.mp4"
        items.append({
            "kind": "video", "id": job.id,
            "file": str(video if video.exists() else job.video_file),
            "title": _read(kit / "title.txt") or job.title or job.topic,
            "description": _read(kit / "description.txt"),
            "tags": [t.strip() for t in
                     (_read(kit / "tags.txt") or "").split(",") if t.strip()],
            "kit": str(kit),
        })
    return items


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# --------------------------------------------------------------------------
# staging


def stage_packets(cfg, assignments: list[dict], day: str) -> list[str]:
    """One `plan.json` per item under work/post/<day>/<channel>/<id>/."""
    root = cfg.root / "work" / "post" / day
    written = []
    for assign in assignments:
        item, channel = assign["item"], assign.get("channel")
        folder = root / (channel["slug"] if channel else "unassigned") / item["id"]
        try:
            folder.mkdir(parents=True, exist_ok=True)
            payload = {
                "file": item.get("file"), "title": item.get("title"),
                "description": item.get("description"),
                "tags": item.get("tags") or [], "kind": item.get("kind"),
                "id": item.get("id"),
                "channel": (channel or {}).get("name") or "",
                "studio_url": (channel or {}).get("studio_url") or "",
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "posted": False,
            }
            (folder / "plan.json").write_text(
                json.dumps(payload, indent=1, ensure_ascii=False),
                encoding="utf-8")
            written.append(str(folder / "plan.json"))
        except OSError:
            continue
    return written


# --------------------------------------------------------------------------
# the whole thing


def execute(cfg, order: dict, dry_run: bool = False, runner=None,
            driver=None, echo=print, plan_only: bool = False,
            open_driver: bool = True) -> dict:
    """Run a parsed order. Returns {ok, report, ...} — never raises."""
    from nightbatch import default_runner

    runner = runner or default_runner
    day = time.strftime("%Y-%m-%d")
    start_ts = time.time()
    result = {"ok": True, "day": day, "order": order, "steps": [],
              "clips": [], "videos": [], "packets": [], "posts": [],
              "report": "", "plan": plan_text(cfg, order)}

    if plan_only or not order.get("ok"):
        result["report"] = result["plan"]
        return result

    def step(label: str, argv: list[str]) -> int:
        log = Path(cfg.root) / "work" / "post" / f"{day}-{len(result['steps'])}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        echo(f"  [orders] ▶ {label}")
        rc, tail = runner(argv, log, cfg.root)
        result["steps"].append({"label": label, "rc": rc,
                                "log": str(log), "tail": tail[-400:]})
        echo(f"  [orders] {'✅' if rc == 0 else '❌'} {label} (rc={rc})")
        if rc != 0:
            result["ok"] = False
        return rc

    import sys

    main_py = str(Path(__file__).resolve().parent / "main.py")

    # 1) source → clips
    if order["clips"]:
        rc = step(f"clip up to {order['clips']} from the sheet",
                  [sys.executable, "-u", main_py, "clip", "--sheet", "1",
                   "--max-clips", str(order["clips"])])
        if rc == 0:
            result["clips"] = resolve_new_clips(cfg, start_ts, order["clips"])

    # 2) generated videos
    if order["videos"]:
        rc = step(f"generate {order['videos']} video(s)",
                  [sys.executable, "-u", main_py, "batch", "--count",
                   str(order["videos"])])
        if rc == 0:
            result["videos"] = resolve_new_videos(cfg, start_ts,
                                                  order["videos"])

    # A successful subprocess can still produce fewer usable items than
    # requested (the clipper intentionally rejects weak/overlapping moments).
    # Report that as a partial order, not a full success.
    for key in ("clips", "videos"):
        requested = int(order.get(key) or 0)
        if requested and len(result[key]) < requested:
            result["ok"] = False

    items = result["clips"] + result["videos"]
    channels = channel_list(cfg)
    assignments = assign_channels(items, channels, order.get("channels") or 0)
    result["packets"] = stage_packets(cfg, assignments, day)

    # 3) post (the gated commit path)
    want_post = order["post"] and not (dry_run or order["dry"])
    if want_post and items:
        conf = dconf(cfg)
        if not channels:
            result["ok"] = False
            result["post_note"] = ("no channels configured — add "
                                   "desktop.channels to config.yaml")
        elif not conf["uploads"]:
            result["post_note"] = ("desktop.uploads is off — everything is "
                                   "staged; flip it (or /desk uploads on) to "
                                   "let the browser lane publish")
        else:
            r = _post_items(cfg, assignments, dry_run=False, driver=driver,
                            echo=echo, open_driver=open_driver)
            result["posts"] = r["posts"]
            if r.get("error"):
                result["ok"] = False
                result["post_note"] = r["error"]
            # A started browser is not a successful post: propagate per-channel
            # upload failures all the way to the CLI/panel exit status.
            if any(not post.get("ok") or not post.get("committed")
                   for post in result["posts"]):
                result["ok"] = False
    elif items and order["post"]:
        result["post_note"] = "dry run — staged only"

    result["report"] = build_report(cfg, order, result)
    try:
        out = cfg.root / "work" / "post" / day / "report.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(
            {k: v for k, v in result.items() if k != "order"} | {
                "order": order.get("raw")},
            indent=1, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return result


def _post_items(cfg, assignments: list[dict], dry_run: bool, driver=None,
                echo=print, open_driver: bool = True) -> dict:
    """Drive the browser lane for every assignment that has a channel."""
    run_id, log_path, shots = new_run(cfg)
    log = RunLog(log_path)
    owned = driver is None and open_driver
    if owned:
        driver = make_driver(cfg)
        try:
            driver.start()
        except Exception as exc:  # noqa: BLE001
            return {"posts": [], "error": f"browser lane: {str(exc)[:200]}"}
    posts = []
    try:
        for assign in assignments:
            channel, item = assign.get("channel"), assign["item"]
            if not channel:
                continue
            if not Path(str(item.get("file"))).exists():
                posts.append({"ok": False, "channel": channel["name"],
                              "item": item["id"],
                              "error": f"file missing: {item.get('file')}"})
                continue
            echo(f"  [orders] ▶ upload {item['id']} → {channel['name']}")
            got = post_one(cfg, driver, item, channel, dry_run=dry_run,
                           run_log=log, shots=shots, echo=echo)
            posts.append(got)
            echo(f"  [orders] {'✅' if got.get('ok') else '❌'} "
                 f"{channel['name']}: {got.get('error') or 'committed'}")
    finally:
        if owned:
            try:
                driver.close()
            except Exception:  # noqa: BLE001
                pass
    return {"posts": posts}


def build_report(cfg, order: dict, result: dict) -> str:
    """The Telegram-facing summary of one order."""
    conf = dconf(cfg)
    lines = [f"🧾 Order {time.strftime('%Y-%m-%d %H:%M')} — "
             f"\"{order.get('raw')}\"", ""]
    for done in result.get("steps") or []:
        mark = "✅" if done["rc"] == 0 else "❌"
        tail = "" if done["rc"] == 0 else f" — {done.get('tail', '').strip().splitlines()[-1][:120] if done.get('tail', '').strip() else 'see log'}"
        lines.append(f"{mark} {done['label']}{tail}")
    for key, label in (("clips", "clip(s)"), ("videos", "video(s)")):
        requested = int(order.get(key) or 0)
        actual = len(result.get(key) or [])
        if requested and actual < requested:
            lines.append(f"⚠️ requested {requested} {label}, produced {actual}; "
                         "only produced items were staged/mapped.")
    if result.get("clips"):
        lines.append(f"✂️ clips ready: {len(result['clips'])} — "
                     + ", ".join(c["id"] for c in result["clips"]))
    if result.get("videos"):
        lines.append(f"🎬 videos ready: {len(result['videos'])} — "
                     + ", ".join(v["id"] for v in result["videos"]))
    if result.get("packets"):
        lines.append(f"📦 staged: {len(result['packets'])} packets in "
                     f"work/post/{result.get('day')}/")
    for post in result.get("posts") or []:
        if post.get("committed"):
            lines.append(f"⬆️ posted to {post['channel']}: "
                         f"{post.get('title', post.get('item'))}")
        elif post.get("ok"):
            lines.append(f"🅿️ {post['channel']}: {post.get('error') or 'staged'}")
        else:
            lines.append(f"⚠️ {post['channel']} failed: {post.get('error')}")
            if post.get("shot"):
                lines.append(f"   screenshot: {post['shot']}")
    if result.get("post_note"):
        lines.append(f"ℹ️ posting: {result['post_note']}")
    if not result.get("ok", True):
        lines.append("⚠️ Order incomplete — see missing items or channel failures above.")
    lines.append("")
    lines.append("Uploads stay manual unless desktop.uploads is on and the "
                 "order says post.")
    return "\n".join(lines)
