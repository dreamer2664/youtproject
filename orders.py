"""Plain-language orders — "get 6 clips and post them in 6 channels".

The user types one sentence; this module turns it into real work:

    order → parse → resolve files (sheet/clips/kits) → map onto channels
          → stage upload packets → (desktop lane) post → report

The parse never guesses silently: everything it understood is printed back as
a plan first. `pipeline.run()` is the durable executor, and the only browser
commit is `desktop.post_one()`. This module retains parsing, exact channel
resolution, per-item staging, and a compatibility `execute()` wrapper.

`--plan-only` runs no steps at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from desktop import dconf

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
    url_match = re.search(r"https?://[^\s,;<>]+", raw, re.I)
    source_url = url_match.group(0).rstrip(".,);]") if url_match else ""
    order = {"raw": raw, "get_link": bool(source_url), "source_url": source_url,
             "clips": 0, "parts": 0, "videos": 0, "channels": 0,
             "channel_names": [], "channel_selection_explicit": False,
             "visibility": "", "topic": "", "post": False,
             "dry": False, "confirm_publish": False,
             "rights_confirmed": False, "altered_content": None,
             "made_for_kids": None,
             "viral_requested": bool(re.search(r"\b(viral|virality)\b", low)),
             "understood": []}

    num = r"(\d{1,2})"
    match = re.search(num + r"\s+clips?\b", low) or re.search(
        r"\bclips?\b\D{0,12}" + num, low)
    if match:
        order["clips"] = max(0, min(10, int(match.group(1))))
    elif re.search(r"\b(get|grab|take|make|pull|cut)\b[^.]{0,20}\bclip", low):
        order["clips"] = 1
    part_match = re.search(num + r"\s+parts?\b", low)
    if part_match:
        order["parts"] = max(1, min(50, int(part_match.group(1))))
    elif (re.search(r"\b(split|divide)\b[^.]{0,35}\bparts?\b", low)
          or re.search(r"\bparts?\b[^.]{0,25}\b(split|divide|make)\b", low)):
        order["parts"] = 1
    match = re.search(num + r"\s+(?:videos?|shorts?)\b", low)
    if match:
        order["videos"] = max(0, min(10, int(match.group(1))))
    elif re.search(r"\b(generate|make|create|render)\b[^.]{0,25}\b(video|short)\b", low):
        order["videos"] = 1
    match = re.search(r"(?:in|to|across|on|for)\s+" + num + r"\s+channels",
                      low)
    if match:
        order["channels"] = max(1, min(50, int(match.group(1))))

    if re.search(r"\b(link|url|source)\b", low) and re.search(
            r"\b(get|grab|take|pull|use|fetch|from)\b", low):
        order["get_link"] = True
    if re.search(r"\b(database|the db|sheet|list of links|backlog)\b", low):
        order["get_link"] = True
    if order["clips"] > 0 or order["parts"] > 0:
        order["get_link"] = True          # source-derived lanes need a source

    no_post = (bool(re.search(r"\b(don'?t|do not|no)\s+post", low))
               or bool(re.search(r"\b(stage only|just stage|without posting|"
                                 r"no posting|dry)\b", low)))
    if re.search(r"\b(dry|plan only|plan-only)\b", low):
        order["dry"] = True
    if (re.search(r"\b(post|publish|upload)\b", low) and not no_post):
        order["post"] = True
        order["understood"].append("posting requested")

    order["understood"] = [
        (f"source: {source_url}" if source_url else
         "a source link from the sheet" if order["get_link"] else None),
        (f"{order['clips']} clips" if order["clips"] else None),
        (f"split into parts (max {order['parts']})" if order["parts"] else None),
        (f"{order['videos']} generated videos" if order["videos"] else None),
        (f"post to {order['channels']} channels" if order["post"]
         and order["channels"] else ("post" if order["post"] else "stage only")),
    ]
    order["understood"] = [part for part in order["understood"] if part]
    order["ok"] = bool(order["clips"] or order["parts"] or order["videos"]
                       or order["get_link"])
    return order


def is_youtube_source(url: str) -> bool:
    """Accept ordinary YouTube web URLs only; reject credentials/secrets."""
    try:
        parsed = urlparse(str(url or "").strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
        query_keys = {key.casefold() for key, _ in parse_qsl(
            parsed.query, keep_blank_values=True)}
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    if parsed.username or parsed.password or port not in (None, 80, 443):
        return False
    sensitive = {"token", "access_token", "refresh_token", "api_key", "key",
                 "authorization", "auth", "password", "secret", "client_secret",
                 "credential", "cookie", "session", "sig", "signature"}
    if query_keys & sensitive or any(key.endswith("_token") for key in query_keys):
        return False
    return (host == "youtu.be" or host == "youtube.com"
            or host.endswith(".youtube.com"))


def resolve_selected_channels(cfg, order: dict) -> list[dict]:
    """Resolve explicit channel names/IDs; otherwise honor the count preview.

    A posting preview always lists the resulting channel identities before the
    user is asked to confirm. Explicit refs must match exactly—never fuzzy
    match a channel name for a publish action.
    """
    available = channel_list(cfg)
    refs = [str(x).strip() for x in (order.get("channel_names") or [])
            if str(x).strip()]
    if refs:
        picked = []
        seen = set()
        for ref in refs:
            folded = ref.casefold()
            matches = [c for c in available
                       if folded in {str(c.get("name") or "").casefold(),
                                     str(c.get("id") or "").casefold(),
                                     str(c.get("slug") or "").casefold()}]
            if len(matches) != 1:
                raise ValueError(f"channel {ref!r} matched {len(matches)} profiles; "
                                 "select an exact configured channel name/ID")
            key = matches[0].get("id") or matches[0].get("name", "").casefold()
            if key in seen:
                raise ValueError(f"duplicate channel selection: {ref!r}")
            seen.add(key)
            picked.append(matches[0])
        return picked
    if order.get("channel_selection_explicit"):
        return []
    count = int(order.get("channels") or 0)
    return available[:count] if count else available


def plan_text(cfg, order: dict) -> str:
    """The exact intent summary shown before an order can publish."""
    conf = dconf(cfg)
    channels = channel_list(cfg)
    try:
        selected = (resolve_selected_channels(cfg, order)
                    if order.get("post") else [])
    except ValueError as exc:
        selected = []
        selection_error = str(exc)
    else:
        selection_error = ""
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
    if order.get("free_only"):
        lines.append("Provider policy: explicit --free-only — Pollinations/template text, public YouTube captions (Groq Whisper/Gemini disabled), free stock/Pollinations images, and edge-tts.")
    else:
        primary = str(getattr(cfg, "ai_provider", "gemini") or "gemini")
        lines.append(f"Provider policy: configured provider chain (AI primary: {primary}); Groq Whisper first for transcription, with public YouTube captions as fallback. Provider quotas/account limits apply; pass --free-only to opt into keyless routes.")
    if order["get_link"]:
        if order.get("source_file"):
            lines.append(f"1. use the supplied local source file: {order['source_file']}")
        elif order.get("source_url"):
            lines.append(f"1. use the supplied source: {order['source_url']}")
        else:
            lines.append("1. take the next queued source from the sheet")
    if order["clips"]:
        lines.append(f"2. cut up to {order['clips']} clips from that source")
    if order.get("parts"):
        cap = int(order["parts"] or 0)
        lines.append("3. split that source into parts"
                     + (f" (cap {cap})" if cap > 1 else
                        f" (configured cap {getattr(cfg, 'parts_max_parts', 50)})"))
    if order["videos"]:
        if order.get("topic"):
            lines.append(f"4. generate {order['videos']} video(s) from the "
                         f"specified topic/prompt: {order['topic']}")
        else:
            lines.append(f"4. generate {order['videos']} video(s) from the "
                         "topic backlog")
    visibility = str(order.get("visibility") or conf["visibility"]).lower()
    if visibility not in ("public", "private", "unlisted"):
        selection_error = f"unsupported visibility {visibility!r}"
    source_work = bool(order.get("clips") or order.get("parts"))
    total_known = int(order.get("clips") or 0) + int(order.get("videos") or 0)
    if order.get("parts"):
        total_known += int(order["parts"]) if int(order["parts"]) > 1 else 0
    if selection_error:
        lines.append(f"❌ Channel/setting selection error: {selection_error}")
    if not order.get("post"):
        lines.append("Targets: none — this request is stage-only.")
    elif selected:
        names = [f"{c['name']} ({c['id']})" if c.get("id") else c["name"]
                 for c in selected]
        map_note = ("round-robin; each item goes to one selected channel"
                    if len(selected) > 1 else "all items go to this channel")
        count_text = (f"up to {total_known} item(s)" if total_known else
                      "the resulting part item(s)" if order.get("parts") else
                      "no upload items")
        lines.append(f"Target: {count_text} → {', '.join(names)} ({map_note}).")
        lines.append(f"Visibility for each upload: {visibility}.")
    else:
        lines.append("No channel profiles selected/configured — output can only "
                     "be staged; no upload will be attempted.")
    if source_work:
        lines.append("Source-use attestation: "
                     + ("owner confirmed rights" if order.get("rights_confirmed")
                        else "not confirmed; source-derived items cannot be uploaded"))
    if order["post"] and not order["dry"]:
        altered = order.get("altered_content")
        kids = order.get("made_for_kids")
        lines.append("YouTube altered-content answer: "
                     + ("Yes" if altered is True else "No" if altered is False
                        else "not selected — posting blocked"))
        lines.append("YouTube audience answer: "
                     + ("Made for kids" if kids is True else
                        "Not made for kids" if kids is False else
                        "not selected — posting blocked"))
    if order.get("plan_only"):
        if not order["post"] or not (total_known or order.get("parts")):
            suffix = "this order requests no posting"
        elif not selected:
            suffix = "no selected channels, so posting is unavailable"
        elif conf["uploads"]:
            suffix = "normal run requires explicit publish confirmation"
        else:
            suffix = "uploads gate is off; normal run stages only"
        lines.append(f"5. PLAN ONLY: nothing runs; {suffix}")
    elif order["post"] and not order["dry"]:
        if conf["uploads"] and order.get("confirm_publish"):
            lines.append(f"5. POST: explicitly confirmed — local Studio browser will "
                         f"upload with visibility={visibility}.")
        elif conf["uploads"]:
            lines.append("5. POST: not yet authorized — review this exact plan and "
                         "confirm before the browser may commit.")
        else:
            lines.append("5. POST: staged only — desktop.uploads is off.")
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
    lines.append("No publish click happens unless desktop.uploads is on, the "
                 "exact channel/visibility plan is confirmed, and required "
                 "source-rights, altered-content, and audience answers are present.")
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


def resolve_new_videos(cfg, since_ts: float = 0.0, limit: int = 10,
                       pipeline_id: str = "") -> list[dict]:
    """Generated videos for this pipeline, or fresh legacy jobs."""
    from jobqueue import Queue

    jobs = Queue(cfg.state_file).jobs
    if pipeline_id:
        fresh = [j for j in jobs if getattr(j, "pipeline_id", "") == pipeline_id
                 and j.status in ("generated", "packaged", "published")]
    else:
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
        selected_video = video if video.exists() else Path(job.video_file)
        try:
            checksum = _sha256(selected_video) if selected_video.is_file() else ""
        except OSError:
            checksum = ""
        items.append({
            "kind": "video", "id": job.id,
            "job_id": job.id, "pipeline_id": getattr(job, "pipeline_id", ""),
            "file": str(selected_video), "sha256": checksum,
            "title": _read(kit / "title.txt") or job.title or job.topic,
            "description": _read(kit / "description.txt"),
            "tags": [t.strip() for t in
                     (_read(kit / "tags.txt") or "").split(",") if t.strip()],
            "kit": str(kit),
        })
    return items


def resolve_output_kits(out_dir: Path, kind: str,
                        limit: int = 50) -> list[dict]:
    """Read complete clip/part upload kits from an isolated job output dir."""
    root = Path(out_dir)
    found = []
    if not root.is_dir():
        return found
    for video in sorted(root.glob("*.mp4"), key=lambda p: p.name):
        kit = root / video.stem
        title = _read(kit / "TITLE.txt")
        description = _read(kit / "DESCRIPTION.txt")
        if not title or not description or not (kit / "CHECKLIST.md").is_file():
            continue
        try:
            if video.stat().st_size <= 0:
                continue
            checksum = _sha256(video)
        except OSError:
            continue
        found.append({"kind": kind, "id": video.stem, "file": str(video),
                      "sha256": checksum, "title": title,
                      "description": description, "tags": [],
                      "kit": str(kit)})
    return found[:max(0, int(limit))]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    """Durably replace a JSON record; a crash cannot leave half-valid JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    with tmp.open("xb") as fh:
        fh.write(encoded)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def pipeline_root(cfg) -> Path:
    return Path(cfg.work_dir) / "pipeline"


def pipeline_job_dir(cfg, job_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(job_id or ""))
    if not safe or safe != str(job_id):
        raise ValueError("invalid pipeline job id")
    return pipeline_root(cfg) / "jobs" / safe


def pipeline_job_path(cfg, job_id: str) -> Path:
    return pipeline_job_dir(cfg, job_id) / "job.json"


def load_pipeline_job(cfg, job_id: str) -> dict | None:
    """Load the live journal, falling back to the newest valid checkpoint."""
    path = pipeline_job_path(cfg, job_id)

    def read_valid(candidate: Path) -> dict | None:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if (isinstance(data, dict) and data.get("id") == job_id
                and data.get("schema") == 1
                and isinstance(data.get("steps"), dict)):
            return data
        return None

    live = read_valid(path)
    if live is not None:
        return live
    checkpoints = pipeline_job_dir(cfg, job_id) / "checkpoints"
    for backup in sorted(checkpoints.glob("*.json"), key=lambda p: p.name,
                         reverse=True):
        recovered = read_valid(backup)
        if recovered is None:
            continue
        recovered["recovered_from"] = str(backup)
        try:
            # Restore the known-good snapshot before any resume step proceeds.
            _atomic_json(path, recovered)
        except OSError:
            pass
        return recovered
    return None


def _save_pipeline_job(cfg, state: dict) -> None:
    """Save the live journal plus a rolling set of immutable checkpoints."""
    folder = pipeline_job_dir(cfg, str(state["id"]))
    state["updated_at"] = time.time()
    state["sequence"] = int(state.get("sequence") or 0) + 1
    _atomic_json(folder / "job.json", state)
    checkpoints = folder / "checkpoints"
    checkpoints.mkdir(parents=True, exist_ok=True)
    snap = checkpoints / f"{state['sequence']:06d}.json"
    _atomic_json(snap, state)
    old = sorted(checkpoints.glob("*.json"), key=lambda p: p.name)
    for path in old[:-30]:
        try:
            path.unlink()
        except OSError:
            pass


def _new_pipeline_job(cfg, order: dict, job_id: str | None = None) -> dict:
    identity = job_id or uuid.uuid4().hex[:16]
    if load_pipeline_job(cfg, identity):
        raise ValueError(f"pipeline job {identity} already exists; use --resume")
    now = time.time()
    state = {
        "schema": 1, "id": identity, "status": "queued",
        "created_at": now, "updated_at": now, "sequence": 0,
        "request": dict(order), "source": {},
        "steps": {}, "items": {"clips": [], "parts": [], "videos": []},
        "assignments": [], "posts": [], "consent": None, "error": "",
    }
    _save_pipeline_job(cfg, state)
    return state


def _load_or_create_pipeline_job(cfg, order: dict | None, job_id: str | None,
                                 resume: bool) -> dict:
    if resume:
        if not job_id:
            raise ValueError("--resume requires a pipeline job id")
        state = load_pipeline_job(cfg, job_id)
        if state is None:
            raise ValueError(f"pipeline job {job_id} was not found or is corrupt")
        if order and str(order.get("raw") or "") != \
                str((state.get("request") or {}).get("raw") or ""):
            raise ValueError("resume request does not match the saved job")
        return state
    if order is None:
        raise ValueError("a new pipeline run needs an order")
    return _new_pipeline_job(cfg, order, job_id)


def _acquire_order_lock(cfg, job_id: str) -> Path:
    path = pipeline_root(cfg) / "active.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"pid": os.getpid(), "job_id": job_id,
                          "started_at": time.time()})
    try:
        with path.open("x", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    except FileExistsError as exc:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            pid = int(current.get("pid") or 0)
            detail = f"job {current.get('job_id', '?')} pid {pid}"
        except Exception:
            pid, detail = 0, "an unreadable lock file"
        # Reclaim only a lock whose owning process is demonstrably gone; save
        # it for inspection instead of deleting evidence of the interruption.
        dead = False
        if pid > 0:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                dead = True
            except PermissionError:
                dead = False
            except OSError:
                dead = False
        if dead:
            stale = path.with_name(f"active.stale-{int(time.time())}.lock")
            try:
                os.replace(path, stale)
            except OSError:
                raise RuntimeError(f"stale pipeline lock could not be archived ({detail})") from exc
            return _acquire_order_lock(cfg, job_id)
        raise RuntimeError(f"another pipeline may be active ({detail}); "
                           "inspect it before removing the lock") from exc
    return path


def _verify_recorded_items(items: list[dict]) -> tuple[bool, str]:
    for item in items:
        path = Path(str(item.get("file") or ""))
        if not path.is_file():
            return False, f"artifact missing: {path}"
        try:
            if path.stat().st_size <= 0:
                return False, f"artifact is empty: {path}"
            expected = str(item.get("sha256") or "")
            if expected and _sha256(path) != expected:
                return False, f"artifact checksum changed: {path}"
        except OSError as exc:
            return False, f"could not verify artifact {path}: {exc}"
    return True, ""


# --------------------------------------------------------------------------
# staging


def stage_packets(cfg, assignments: list[dict], day: str,
                  job_id: str = "", visibility: str = "unlisted",
                  consent: dict | None = None) -> list[str]:
    """Atomically write one manifest per item/channel assignment."""
    root = Path(cfg.root) / "work" / "post" / day / (job_id or "legacy")
    written = []
    for assign in assignments:
        item, channel = assign["item"], assign.get("channel")
        folder = root / (channel["slug"] if channel else "unassigned") / \
                 str(item.get("id") or "unknown")
        try:
            path = Path(str(item.get("file") or ""))
            stat = path.stat() if path.is_file() else None
            payload = {
                "schema": 1, "job_id": job_id,
                "file": str(path), "size": stat.st_size if stat else 0,
                "sha256": item.get("sha256") or
                          (_sha256(path) if stat else ""),
                "title": item.get("title"),
                "description": item.get("description"),
                "tags": item.get("tags") or [], "kind": item.get("kind"),
                "id": item.get("id"),
                "channel": (channel or {}).get("name") or "",
                "channel_id": (channel or {}).get("id") or "",
                "studio_url": (channel or {}).get("studio_url") or "",
                "visibility": visibility, "consent": consent,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "posted": False, "status": "staged",
            }
            manifest = folder / "plan.json"
            try:
                previous = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                previous = {}
            if (isinstance(previous, dict)
                    and previous.get("sha256") == payload["sha256"]
                    and previous.get("channel_id") == payload["channel_id"]
                    and previous.get("status") in
                    ("committed", "uncertain", "failed_precommit", "duplicate_blocked")):
                for key in ("posted", "status", "post_key", "post_result",
                            "reconciled_at"):
                    if key in previous:
                        payload[key] = previous[key]
            _atomic_json(manifest, payload)
            written.append(str(manifest))
        except OSError:
            continue
    return written


# --------------------------------------------------------------------------
# the whole thing


def execute(cfg, order: dict, dry_run: bool = False, runner=None,
            driver=None, echo=print, plan_only: bool = False,
            open_driver: bool = True) -> dict:
    """Compatibility entry point; delegate all execution to pipeline.run."""
    if plan_only or order.get("plan_only"):
        plan = plan_text(cfg, order)
        return {"ok": bool(order.get("ok")), "day": time.strftime("%Y-%m-%d"),
                "order": order, "steps": [], "clips": [], "parts": [],
                "videos": [], "packets": [], "posts": [],
                "report": plan, "plan": plan}
    from pipeline import run

    return run(cfg, order, dry_run=dry_run, runner=runner, driver=driver,
               echo=echo, open_driver=open_driver)


def build_report(cfg, order: dict, result: dict) -> str:
    """The Telegram-facing summary of one order."""
    conf = dconf(cfg)
    lines = [f"🧾 Order {time.strftime('%Y-%m-%d %H:%M')} — "
             f"\"{order.get('raw')}\"", ""]
    for done in result.get("steps") or []:
        mark = "✅" if done["rc"] == 0 else "❌"
        tail = "" if done["rc"] == 0 else f" — {done.get('tail', '').strip().splitlines()[-1][:120] if done.get('tail', '').strip() else 'see log'}"
        lines.append(f"{mark} {done['label']}{tail}")
    for key, label in (("clips", "clip(s)"), ("parts", "part(s)"),
                       ("videos", "video(s)")):
        requested = int(order.get(key) or 0)
        actual = len(result.get(key) or [])
        if requested and actual < requested:
            lines.append(f"⚠️ requested {requested} {label}, produced {actual}; "
                         "only produced items were staged/mapped.")
    if result.get("clips"):
        lines.append(f"✂️ clips ready: {len(result['clips'])} — "
                     + ", ".join(c["id"] for c in result["clips"]))
    if result.get("parts"):
        lines.append(f"🧩 parts ready: {len(result['parts'])} — "
                     + ", ".join(c["id"] for c in result["parts"]))
    if result.get("videos"):
        lines.append(f"🎬 videos ready: {len(result['videos'])} — "
                     + ", ".join(v["id"] for v in result["videos"]))
    if result.get("packets"):
        lines.append(f"📦 staged: {len(result['packets'])} packets in "
                     f"work/post/{result.get('day')}/")
    for post in result.get("posts") or []:
        channel_name = post.get("channel") or "upload"
        if post.get("committed"):
            lines.append(f"⬆️ posted to {channel_name}: "
                         f"{post.get('title', post.get('item'))}")
        elif post.get("ok"):
            lines.append(f"🅿️ {channel_name}: {post.get('error') or 'staged'}")
        else:
            lines.append(f"⚠️ {channel_name} failed: {post.get('error')}")
            if post.get("shot"):
                lines.append(f"   screenshot: {post['shot']}")
            if post.get("uncertain") and post.get("key"):
                lines.append("   uncertain commit — check Studio first, then reconcile with: "
                             f"python main.py pipeline-reconcile --job-id "
                             f"{result.get('job_id', '<job-id>')} --key "
                             f"{post['key']} --outcome committed|not-posted")
    if result.get("post_note"):
        lines.append(f"ℹ️ posting: {result['post_note']}")
    for error in result.get("errors") or []:
        lines.append(f"⚠️ {str(error)[:240]}")
    if not result.get("ok", True):
        lines.append("⚠️ Order incomplete — see missing items or channel failures above.")
    lines.append("")
    lines.append("Any automated upload in this job uses the existing local "
                 "YouTube Studio browser lane; no API or third-party publisher is used.")
    return "\n".join(lines)
