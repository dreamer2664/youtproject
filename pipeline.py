"""Durable local job orchestration for generate/clip/parts + Studio uploads.

The panel/CLI collect an exact publish intent and require a second, visible
confirmation before this module reaches the browser commit path. Rendering and
upload attempts are journaled per job; an interrupted commit is never retried
blindly because Studio may already have accepted it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from process_utils import no_console_kwargs

import orders


LEDGER_SCHEMA = 1
CHECKPOINT_KEEP = 30


def _main_py() -> str:
    return str(Path(__file__).resolve().parent / "main.py")


def _json_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _save_ledger(cfg, ledger: dict) -> None:
    root = orders.pipeline_root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    ledger["schema"] = LEDGER_SCHEMA
    ledger["sequence"] = int(ledger.get("sequence") or 0) + 1
    ledger["updated_at"] = time.time()
    snapshots = root / "ledger-backups"
    snapshots.mkdir(parents=True, exist_ok=True)
    orders._atomic_json(snapshots / f"{ledger['sequence']:06d}.json", ledger)
    orders._atomic_json(root / "upload-ledger.json", ledger)
    old = sorted(snapshots.glob("*.json"), key=lambda p: p.name)
    for path in old[:-CHECKPOINT_KEEP]:
        try:
            path.unlink()
        except OSError:
            pass


def _load_ledger(cfg) -> dict:
    path = orders.pipeline_root(cfg) / "upload-ledger.json"

    def read_valid(candidate: Path) -> dict | None:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if (isinstance(data, dict) and data.get("schema") == LEDGER_SCHEMA
                and isinstance(data.get("entries"), dict)):
            return data
        return None

    data = read_valid(path)
    if data is not None:
        return data
    backups = orders.pipeline_root(cfg) / "ledger-backups"
    candidates = sorted(backups.glob("*.json"), key=lambda p: p.name,
                        reverse=True)
    if not path.exists() and not candidates:
        return {"schema": LEDGER_SCHEMA, "sequence": 0, "entries": {}}
    for backup in candidates:
        recovered = read_valid(backup)
        if recovered is None:
            continue
        # Preserve the bad live file for inspection, then restore the last
        # valid complete ledger. Never reinterpret corruption as an empty DB.
        corrupt = path.with_name(f"upload-ledger.corrupt-{int(time.time())}.json")
        try:
            shutil.copy2(path, corrupt)
        except OSError:
            pass
        recovered["recovered_from"] = str(backup)
        try:
            orders._atomic_json(path, recovered)
        except OSError as exc:
            raise RuntimeError(f"could not restore upload ledger backup: {exc}") from exc
        return recovered
    raise RuntimeError("upload ledger and all backups are unreadable; publishing is blocked")


def _duplicate_key(item: dict, channel: dict, visibility: str) -> str:
    import hashlib

    # Visibility is mutable metadata, not a new upload identity: changing it
    # must not let the same bytes be uploaded twice to the same channel.
    raw = "|".join((str(item.get("sha256") or ""),
                    str(channel.get("id") or channel.get("name") or "").casefold()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _media_probe(path: Path) -> tuple[bool, str]:
    """Validate the file, stream metadata, duration and full local decode."""
    if not path.is_file():
        return False, f"video file missing: {path}"
    try:
        if path.stat().st_size <= 0:
            return False, f"video file is empty: {path}"
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
            **no_console_kwargs())
        if probe.returncode != 0:
            return False, f"ffprobe failed: {probe.stderr.strip()[:180]}"
        info = json.loads(probe.stdout or "{}")
        streams = info.get("streams") if isinstance(info, dict) else None
        video = next((s for s in (streams or [])
                      if s.get("codec_type") == "video"), None)
        if not video:
            return False, "media has no video stream"
        if not video.get("codec_name") or int(video.get("width") or 0) <= 0 \
                or int(video.get("height") or 0) <= 0:
            return False, "video stream is missing a codec or valid dimensions"
        from assembler import ffprobe_duration

        duration = float(ffprobe_duration(path))
        if duration <= 0:
            return False, f"video duration is not positive: {path}"
        # ffprobe reads headers/metadata; a null decode catches truncated or
        # corrupt frames before an irreversible browser upload.
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            decoded = subprocess.run(
                [ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
                capture_output=True, text=True, timeout=120,
                **no_console_kwargs())
            if decoded.returncode != 0:
                return False, f"ffmpeg decode failed: {decoded.stderr.strip()[:180]}"
    except Exception as exc:  # noqa: BLE001 - invalid media is not uploadable
        return False, f"media QA could not validate {path}: {str(exc)[:180]}"
    return True, ""


def _verify_items(items: list[dict], *, probe_media: bool = False) -> tuple[bool, str]:
    for item in items:
        path = Path(str(item.get("file") or ""))
        if probe_media:
            valid, error = _media_probe(path)
            if not valid:
                return False, error
            title = str(item.get("title") or "").strip()
            description = str(item.get("description") or "")
            tags = item.get("tags") or []
            if not title or len(title) > 100:
                return False, f"upload title is empty or exceeds 100 characters: {path.name}"
            if len(description) > 5000:
                return False, f"upload description exceeds 5000 characters: {path.name}"
            if sum(len(str(tag)) + 1 for tag in tags) > 500:
                return False, f"upload tags exceed 500 characters: {path.name}"
        else:
            if not path.is_file():
                return False, f"artifact missing: {path}"
            try:
                if path.stat().st_size <= 0:
                    return False, f"artifact is empty: {path}"
            except OSError as exc:
                return False, f"could not stat artifact {path}: {exc}"
        expected = str(item.get("sha256") or "")
        if expected:
            try:
                actual = orders._sha256(path)
            except OSError as exc:
                return False, f"could not hash artifact {path}: {exc}"
            if actual != expected:
                return False, f"artifact checksum changed: {path}"
        else:
            try:
                item["sha256"] = orders._sha256(path)
            except OSError as exc:
                return False, f"could not hash artifact {path}: {exc}"
    return True, ""


def _resolve_source(cfg, order: dict, state: dict) -> dict:
    """Resolve and persist exactly one source for clip/parts operations."""
    saved = state.get("source") or {}
    if saved:
        if saved.get("file") and not Path(saved["file"]).is_file():
            raise FileNotFoundError(f"saved pipeline source is missing: {saved['file']}")
        if saved.get("url") and not orders.is_youtube_source(saved["url"]):
            raise ValueError("the YouTube-only pipeline refuses non-YouTube source URLs")
        return saved
    source_file = str(order.get("source_file") or "").strip()
    source_url = str(order.get("source_url") or "").strip()
    if source_file:
        path = Path(source_file).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"source file does not exist: {path}")
        source = {"file": str(path), "url": "", "sheet": False,
                  "title": path.stem}
    elif source_url:
        if not orders.is_youtube_source(source_url):
            raise ValueError("the YouTube-only pipeline refuses non-YouTube source URLs")
        source = {"file": "", "url": source_url, "sheet": False,
                  "title": source_url}
    elif order.get("clips") or order.get("parts") or order.get("get_link"):
        from sheet import take_pending

        rows = take_pending(Path(cfg.sources_sheet), 1)
        if not rows:
            raise RuntimeError("no queued source is available in sources/sheet.csv")
        url, note = rows[0]
        if not orders.is_youtube_source(url):
            raise ValueError("the source sheet entry is not a YouTube URL; "
                             "nothing was downloaded")
        source = {"file": "", "url": url, "sheet": True,
                  "note": note, "title": note or url}
    else:
        return {}
    state["source"] = source
    orders._save_pipeline_job(cfg, state)
    return source


def _source_args(source: dict) -> list[str]:
    if source.get("file"):
        return ["--file", str(source["file"])]
    if source.get("url"):
        return ["--url", str(source["url"])]
    return []


def _run_step(cfg, state: dict, name: str, label: str, argv: list[str],
              runner, echo) -> tuple[int, str]:
    job_dir = orders.pipeline_job_dir(cfg, state["id"])
    logs = job_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"{name}.log"
    step = state.setdefault("steps", {}).setdefault(name, {})
    step.update({"status": "running", "label": label, "argv": argv,
                 "started_at": time.time(), "log": str(log)})
    state["status"] = "running"
    orders._save_pipeline_job(cfg, state)
    echo(f"  [pipeline] ▶ {label}")
    try:
        rc, tail = runner(argv, log, cfg.root)
    except Exception as exc:  # noqa: BLE001
        rc, tail = -1, f"runner raised: {exc!r}"
    step.update({"status": "done" if rc == 0 else "failed",
                 "returncode": int(rc), "tail": str(tail)[-1000:],
                 "finished_at": time.time()})
    orders._save_pipeline_job(cfg, state)
    echo(f"  [pipeline] {'✅' if rc == 0 else '❌'} {label} (rc={rc})")
    return int(rc), str(tail)


def _stored_items(state: dict, kind: str) -> list[dict]:
    items = (state.get("items") or {}).get(kind) or []
    return [dict(item) for item in items if isinstance(item, dict)]


def _store_items(cfg, state: dict, kind: str, items: list[dict]) -> None:
    state.setdefault("items", {})[kind] = _json_copy(items)
    orders._save_pipeline_job(cfg, state)


def _render_source_steps(cfg, state: dict, order: dict, source: dict,
                         runner, echo, result: dict) -> bool:
    """Run clip and parts in isolated job directories, resumably."""
    job_dir = orders.pipeline_job_dir(cfg, state["id"])
    main_py = _main_py()
    source_args = _source_args(source)
    if (order.get("clips") or order.get("parts")) and not source_args:
        raise RuntimeError("clip/parts needs a supplied source or a queued sheet link")

    for kind, request_count, folder_name in (
        ("clips", int(order.get("clips") or 0), "clips"),
        ("parts", int(order.get("parts") or 0), "parts"),
    ):
        if not request_count:
            continue
        out_dir = job_dir / "outputs" / folder_name
        existing = _stored_items(state, kind)
        step = (state.get("steps") or {}).get(kind) or {}
        if step.get("status") == "done" and existing:
            valid, why = _verify_items(existing)
            if not valid:
                raise RuntimeError(f"saved {kind} output needs owner recovery: {why}")
            result[kind] = existing
            continue

        # If a previous process stopped during this stage, use only complete
        # kits from the isolated output directory, then rerun into that same
        # unique directory. Kit writers are transactional, so old good kits
        # survive a failed replacement.
        argv = [os.sys.executable, "-u", main_py, "clip" if kind == "clips" else "parts"]
        argv += source_args
        argv += ["--out", str(out_dir)]
        if kind == "clips":
            argv += ["--max-clips", str(max(1, min(10, request_count)))]
        elif request_count > 1:
            argv += ["--max-parts", str(max(1, min(50, request_count)))]
        # Respect the selected order policy. By default, the child clipper
        # uses configured providers (Groq Whisper first; YouTube captions as
        # fallback, and the configured script provider for moment selection).
        # The strict keyless route is an explicit per-order opt-in.
        if order.get("free_only"):
            argv.append("--free-only")
        rc, tail = _run_step(cfg, state, kind,
                             f"{'clip up to ' + str(request_count) if kind == 'clips' else 'split source into parts'}",
                             argv, runner, echo)
        limit = max(1, request_count) if kind == "clips" else 50
        items = orders.resolve_output_kits(out_dir,
                                           "clip" if kind == "clips" else "part",
                                           limit=limit)
        _store_items(cfg, state, kind, items)
        result[kind] = items
        if source.get("sheet") and rc == 0:
            from sheet import mark_sheet

            mark_sheet(Path(cfg.sources_sheet), source["url"], "clipped",
                       f"pipeline {state['id']} — {kind}")
        if rc != 0:
            result["ok"] = False
            result.setdefault("errors", []).append(
                f"{kind} stage returned {rc}: {tail[-240:]}")
        if kind == "clips" and len(items) < request_count:
            result["ok"] = False
            result.setdefault("errors", []).append(
                f"requested up to {request_count} clips; found {len(items)} complete kit(s)")
        if kind == "parts" and not items:
            result["ok"] = False
            result.setdefault("errors", []).append("parts stage produced no complete kits")
    return result.get("ok", True)


def _render_generated(cfg, state: dict, order: dict, runner, echo,
                      result: dict) -> bool:
    requested = int(order.get("videos") or 0)
    if not requested:
        return True
    main_py = _main_py()
    items = orders.resolve_new_videos(cfg, limit=max(10, requested),
                                      pipeline_id=state["id"])
    if len(items) < requested:
        missing = requested - len(items)
        argv = [os.sys.executable, "-u", main_py, "batch", "--count",
                str(missing), "--sleep", "1", "--pipeline-id", state["id"]]
        if order.get("free_only"):
            argv.append("--free-only")
        topic = str(order.get("topic") or "").strip()
        if topic:
            argv += ["--topic", topic]
        rc, tail = _run_step(cfg, state, "videos",
                             f"generate {missing} video(s) for this pipeline job",
                             argv, runner, echo)
        items = orders.resolve_new_videos(cfg, limit=max(10, requested),
                                          pipeline_id=state["id"])
        if rc != 0:
            result["ok"] = False
            result.setdefault("errors", []).append(
                f"video generation returned {rc}: {tail[-240:]}")
    items = items[:requested]
    valid, why = _verify_items(items)
    if not valid:
        result["ok"] = False
        result.setdefault("errors", []).append(why)
    _store_items(cfg, state, "videos", items)
    result["videos"] = items
    if len(items) < requested:
        result["ok"] = False
        result.setdefault("errors", []).append(
            f"requested {requested} generated video(s); found {len(items)} complete job(s)")
    step = state.setdefault("steps", {}).setdefault("videos", {})
    if len(items) >= requested and all(Path(i["file"]).is_file() for i in items):
        step.update(status="done", finished_at=time.time())
        orders._save_pipeline_job(cfg, state)
    else:
        step.update(status="partial", finished_at=time.time())
        orders._save_pipeline_job(cfg, state)
    return len(items) >= requested and result.get("ok", True)


def _load_ledger_entry(ledger: dict, key: str) -> dict | None:
    entry = (ledger.get("entries") or {}).get(key)
    return entry if isinstance(entry, dict) else None


def _post_assignments(cfg, state: dict, assignments: list[dict],
                      visibility: str, runner_driver=None, open_driver: bool = True,
                      echo=print) -> list[dict]:
    import desktop

    run_id, log_path, shots = desktop.new_run(cfg)
    log = desktop.RunLog(log_path)
    owned = runner_driver is None and open_driver
    driver = runner_driver
    posts = []
    if owned:
        try:
            driver = desktop.make_driver(cfg)
            driver.start()
        except Exception as exc:  # noqa: BLE001
            state["status"] = "failed"
            state["error"] = f"browser startup failed: {str(exc)[:240]}"
            orders._save_pipeline_job(cfg, state)
            return [{"ok": False, "committed": False,
                     "error": state["error"]}]
    try:
        for assignment in assignments:
            item, channel = assignment["item"], assignment.get("channel")
            if not channel:
                posts.append({"ok": False, "committed": False,
                              "error": "no channel selected"})
                break
            okay, why = _verify_items([item], probe_media=True)
            if not okay:
                posts.append({"ok": False, "committed": False,
                              "channel": channel.get("name"),
                              "item": item.get("id"), "error": why})
                break
            key = _duplicate_key(item, channel, visibility)
            per_job = state.setdefault("posts", [])
            prior = next((p for p in per_job if p.get("key") == key), None)
            if prior and prior.get("status") == "committed":
                posts.append({**prior.get("result", {}), "skipped": True})
                continue
            if prior and prior.get("status") in ("uploading", "uncertain"):
                message = (f"upload state for {item.get('id')} → "
                           f"{channel.get('name')} is uncertain; inspect Studio "
                           "and reconcile before retrying")
                posts.append({"ok": False, "committed": False, "uncertain": True,
                              "key": key, "channel": channel.get("name"),
                              "item": item.get("id"), "error": message})
                state["status"] = "needs_owner"
                state["error"] = message
                orders._save_pipeline_job(cfg, state)
                break

            ledger = _load_ledger(cfg)
            old = _load_ledger_entry(ledger, key)
            if old and old.get("status") == "committed":
                if old.get("job_id") == state["id"]:
                    recovered = {"key": key, "status": "committed",
                                 "channel": channel.get("name"),
                                 "item": item.get("id"), "result": old.get("result", {})}
                    per_job.append(recovered)
                    orders._save_pipeline_job(cfg, state)
                    posts.append({**old.get("result", {}), "skipped": True})
                    continue
                message = (f"duplicate prevented: the same video checksum was "
                           f"already committed to {channel.get('name')}")
                posts.append({"ok": False, "committed": False,
                              "duplicate": True, "key": key,
                              "channel": channel.get("name"),
                              "item": item.get("id"), "error": message})
                state["status"] = "needs_owner"
                state["error"] = message
                orders._save_pipeline_job(cfg, state)
                break
            if old and old.get("status") in ("uploading", "uncertain"):
                message = (f"duplicate protection found an unresolved upload for "
                           f"{channel.get('name')}; inspect Studio before retrying")
                posts.append({"ok": False, "committed": False, "uncertain": True,
                              "key": key, "channel": channel.get("name"),
                              "item": item.get("id"), "error": message})
                state["status"] = "needs_owner"
                state["error"] = message
                orders._save_pipeline_job(cfg, state)
                break

            post_record = {"key": key, "item": item.get("id"),
                           "file": item.get("file"), "sha256": item.get("sha256"),
                           "channel": channel.get("name"),
                           "channel_id": channel.get("id"),
                           "visibility": visibility, "status": "uploading",
                           "started_at": time.time(), "result": {}}
            if prior:
                prior.update(post_record)
            else:
                per_job.append(post_record)
            state["status"] = "publishing"
            orders._save_pipeline_job(cfg, state)
            ledger.setdefault("entries", {})[key] = {
                **post_record, "job_id": state["id"]}
            # Persist both journals before the non-idempotent Studio sequence.
            _save_ledger(cfg, ledger)
            echo(f"  [pipeline] upload {item.get('title')!r} → "
                 f"{channel.get('name')} ({visibility})")
            got = desktop.post_one(
                cfg, driver, item, channel, dry_run=False, run_log=log,
                shots=shots, echo=echo, visibility=visibility,
                altered_content=state["request"].get("altered_content"),
                made_for_kids=state["request"].get("made_for_kids"))
            if got.get("committed") and got.get("ok"):
                status = "committed"
            elif got.get("commit_attempted"):
                status = "uncertain"
            else:
                status = "failed_precommit"
            post_record.update(status=status, finished_at=time.time(),
                               result=got)
            ledger["entries"][key] = {**post_record, "job_id": state["id"]}
            _save_ledger(cfg, ledger)
            orders._save_pipeline_job(cfg, state)
            posts.append({**got, "key": key})
            if status != "committed":
                state["status"] = "needs_owner" if status == "uncertain" else "failed"
                state["error"] = got.get("error") or status
                orders._save_pipeline_job(cfg, state)
                break
    finally:
        if owned and driver is not None:
            try:
                driver.close()
            except Exception:  # noqa: BLE001
                pass
    return posts


def _update_packet_manifests(packet_paths: list[str], posts: list[dict]) -> None:
    """Keep the staged per-channel packet aligned with the upload journal."""
    by_target = {}
    for post in posts:
        item_id = str(post.get("item") or "")
        channel = str(post.get("channel") or "")
        if not item_id or not channel:
            continue
        status = ("committed" if post.get("committed") else
                  "uncertain" if post.get("uncertain") else
                  "duplicate_blocked" if post.get("duplicate") else
                  "failed_precommit")
        by_target[(item_id, channel)] = (status, post)
    for raw_path in packet_paths:
        path = Path(raw_path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        target = by_target.get((str(payload.get("id") or ""),
                                str(payload.get("channel") or "")))
        if not target:
            continue
        status, post = target
        payload.update(status=status, posted=(status == "committed"),
                       post_key=post.get("key", ""),
                       post_result=_json_copy(post))
        try:
            orders._atomic_json(path, payload)
        except OSError:
            pass


def _consent_record(order: dict, selected: list[dict], visibility: str,
                    job_id: str) -> dict:
    return {
        "action": "generate/clip/parts and upload through local YouTube Studio",
        "request": str(order.get("raw") or ""), "job_id": job_id,
        "channels": [{"name": c.get("name"), "id": c.get("id") or ""}
                     for c in selected],
        "visibility": visibility,
        "altered_content": order.get("altered_content"),
        "made_for_kids": order.get("made_for_kids"),
        "source_rights_attested": bool(order.get("rights_confirmed")),
        "confirmed_at": time.time(),
        "confirmation": "CLI --confirm-publish or panel exact-plan confirmation",
    }


def run(cfg, order: dict, *, dry_run: bool = False,
        runner=None, driver=None, echo=print, open_driver: bool = True,
        resume: bool = False, job_id: str | None = None) -> dict:
    """Execute or resume one local pipeline request; never blind-retry a commit."""
    from nightbatch import default_runner
    from desktop import dconf

    runner = runner or default_runner
    order = dict(order or {})
    if dry_run:
        order["dry"] = True
    if resume and job_id:
        order["resume"] = job_id
    try:
        state = orders._load_or_create_pipeline_job(cfg, order, job_id, resume)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "report": f"Pipeline job refused: {exc}",
                "steps": [], "clips": [], "parts": [], "videos": [],
                "packets": [], "posts": []}
    saved_order = dict(state.get("request") or {})
    # A resumed job always uses the immutable request/consent saved at start.
    if resume:
        order = saved_order
        if dry_run:
            order["dry"] = True
    else:
        state["request"] = _json_copy(order)
        orders._save_pipeline_job(cfg, state)

    result = {"ok": True, "job_id": state["id"],
              "day": time.strftime("%Y-%m-%d"), "order": order,
              "steps": [], "clips": [], "parts": [], "videos": [],
              "packets": [], "posts": [], "errors": [], "report": "",
              "plan": orders.plan_text(cfg, order)}
    if not order.get("ok"):
        result["ok"] = False
        result["errors"].append("no recognized generation, clipping, or parts work")
        state.update(status="failed", error=result["errors"][-1])
        return _finish(cfg, state, order, result)
    try:
        lock = orders._acquire_order_lock(cfg, state["id"])
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["report"] = f"Pipeline is locked: {exc}"
        return result

    try:
        if order.get("post") and not order.get("dry"):
            try:
                selected = orders.resolve_selected_channels(cfg, order)
            except ValueError as exc:
                result["ok"] = False
                result["errors"].append(str(exc))
                state.update(status="failed", error=str(exc))
                orders._save_pipeline_job(cfg, state)
                return _finish(cfg, state, order, result)
        else:
            selected = []

        try:
            source = _resolve_source(cfg, order, state)
        except Exception as exc:  # noqa: BLE001
            result["ok"] = False
            result["errors"].append(str(exc))
            state.update(status="failed", error=str(exc))
            orders._save_pipeline_job(cfg, state)
            return _finish(cfg, state, order, result)

        _render_source_steps(cfg, state, order, source, runner, echo, result)
        _render_generated(cfg, state, order, runner, echo, result)
        for kind in ("clips", "parts", "videos"):
            result[kind] = result.get(kind) or _stored_items(state, kind)
        items = result["clips"] + result["parts"] + result["videos"]
        valid, why = _verify_items(items)
        if not valid:
            result["ok"] = False
            result["errors"].append(why)
        visibility = str(order.get("visibility") or dconf(cfg)["visibility"]).lower()
        if visibility not in ("public", "private", "unlisted"):
            result["ok"] = False
            result["errors"].append(f"invalid visibility: {visibility}")
            visibility = "unlisted"

        assignments = orders.assign_channels(items, selected)
        state["assignments"] = [{
            "item": a["item"], "channel": a.get("channel"),
        } for a in assignments]
        _consent = state.get("consent")
        if not _consent and order.get("confirm_publish"):
            _consent = _consent_record(order, selected, visibility, state["id"])
            state["consent"] = _consent
        result["packets"] = orders.stage_packets(
            cfg, assignments, result["day"], job_id=state["id"],
            visibility=visibility, consent=_consent)
        state["packet_paths"] = list(result["packets"])
        state["staged_at"] = time.time()
        orders._save_pipeline_job(cfg, state)

        post_note = ""
        wants_post = bool(order.get("post") and not order.get("dry"))
        conf = dconf(cfg)
        if wants_post and items:
            if not selected:
                post_note = "no configured channel is selected; artifacts remain staged"
            elif not conf["uploads"]:
                post_note = "desktop.uploads is off; artifacts remain staged"
            elif not order.get("confirm_publish") and not state.get("consent"):
                post_note = "missing exact-plan confirmation; artifacts remain staged"
            elif bool(order.get("clips") or order.get("parts")) \
                    and not order.get("rights_confirmed"):
                post_note = "source rights not attested; source-derived uploads are blocked"
            elif order.get("altered_content") not in (True, False):
                post_note = "choose the per-job altered-content answer before upload"
            elif order.get("made_for_kids") not in (True, False):
                post_note = "choose the per-job audience answer before upload"
            elif not result["ok"]:
                post_note = "a render/QA stage is incomplete; nothing was uploaded"
            else:
                try:
                    media_ok, media_error = _verify_items(items, probe_media=True)
                except Exception as exc:  # noqa: BLE001
                    media_ok, media_error = False, str(exc)
                if not media_ok:
                    post_note = f"media QA failed; nothing was uploaded: {media_error}"
                    result["ok"] = False
                else:
                    result["posts"] = _post_assignments(
                        cfg, state, assignments, visibility,
                        runner_driver=driver, open_driver=open_driver, echo=echo)
                    _update_packet_manifests(result["packets"], result["posts"])
                    if any(not post.get("committed") for post in result["posts"]):
                        result["ok"] = False
        elif wants_post:
            post_note = "no complete output items were produced"
            result["ok"] = False
        elif items and order.get("post"):
            post_note = "dry run — files were rendered and staged; no browser action"

        if post_note:
            result["post_note"] = post_note
        result["steps"] = [
            {"label": step.get("label", key),
             "rc": step.get("returncode", 0 if step.get("status") == "done" else 1),
             "tail": step.get("tail", ""), "log": step.get("log", "")}
            for key, step in (state.get("steps") or {}).items()]
        result["report"] = orders.build_report(cfg, order, result)
        result["report"] += f"\nPipeline job: {state['id']} (resume with `--resume {state['id']}`)."
        if state.get("status") == "needs_owner" or any(
                p.get("uncertain") or p.get("duplicate")
                for p in result["posts"]):
            state.update(status="needs_owner", error=state.get("error") or
                         post_note or "upload requires owner review")
        elif result["posts"] and all(p.get("committed") for p in result["posts"]):
            state.update(status="published", completed_at=time.time(), error="")
        elif result["ok"]:
            state.update(status="ready", error="")
        else:
            state.update(status="partial", error="; ".join(result["errors"])[-500:])
        _finish(cfg, state, order, result)
        return result
    except Exception as exc:  # noqa: BLE001 - journal every unexpected failure
        result["ok"] = False
        result.setdefault("errors", []).append(
            f"pipeline exception: {type(exc).__name__}: {str(exc)[:300]}")
        if state.get("status") not in ("needs_owner", "published"):
            state.update(status="failed", error=result["errors"][-1])
        result["steps"] = [
            {"label": step.get("label", key),
             "rc": step.get("returncode", 1), "tail": step.get("tail", ""),
             "log": step.get("log", "")}
            for key, step in (state.get("steps") or {}).items()]
        _finish(cfg, state, order, result)
        return result
    finally:
        try:
            lock_data = json.loads(lock.read_text(encoding="utf-8"))
            if lock_data.get("pid") == os.getpid() and lock_data.get("job_id") == state["id"]:
                lock.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def _finish(cfg, state: dict, order: dict, result: dict) -> dict:
    """Write a crash-safe report and final journal state."""
    if not result.get("report"):
        result["report"] = orders.build_report(cfg, order, result)
        result["report"] += f"\nPipeline job: {state['id']} (resume with `--resume {state['id']}`)."
    state["last_report"] = result["report"][-8000:]
    orders._save_pipeline_job(cfg, state)
    path = Path(cfg.root) / "work" / "post" / result.get("day", "") / \
           state["id"] / "report.json"
    try:
        orders._atomic_json(path, {
            "schema": 1, "job_id": state["id"], "status": state.get("status"),
            "report": result["report"], "request": order.get("raw"),
            "steps": result.get("steps", []), "items": result.get("clips", [])
                     + result.get("parts", []) + result.get("videos", []),
            "posts": result.get("posts", []), "consent": state.get("consent"),
            "error": state.get("error", ""),
        })
    except OSError:
        pass
    return result


def reconcile(cfg, job_id: str, key: str, outcome: str) -> tuple[bool, str]:
    """Owner-confirmed reconciliation after checking Studio; never guesses."""
    if outcome not in ("committed", "not-posted"):
        return False, "outcome must be committed or not-posted"
    state = orders.load_pipeline_job(cfg, job_id)
    if state is None:
        return False, f"pipeline job {job_id} not found"
    posts = state.get("posts") or []
    entry = next((p for p in posts if p.get("key") == key), None)
    if entry is None:
        return False, "that upload key is not in this job"
    if entry.get("status") not in ("uploading", "uncertain"):
        return False, f"upload is {entry.get('status')}; reconciliation is not needed"
    ledger = _load_ledger(cfg)
    old = _load_ledger_entry(ledger, key)
    if old is None or old.get("job_id") != job_id:
        return False, "matching global ledger entry was not found; refusing to guess"
    new_status = "committed" if outcome == "committed" else "failed_precommit"
    stamp = time.time()
    entry["status"] = new_status
    entry["reconciled_at"] = stamp
    entry["reconciled_by"] = "owner confirmation after checking Studio"
    old.update(status=new_status, reconciled_at=stamp,
               reconciled_by="owner confirmation after checking Studio")
    if outcome == "committed":
        verified = {"ok": True, "committed": True, "uncertain": False,
                    "channel": entry.get("channel", ""),
                    "item": entry.get("item", ""),
                    "title": "manually verified in Studio",
                    "manual_reconciliation": True}
        entry["result"] = verified
        old["result"] = verified
    else:
        verified = {"ok": False, "committed": False, "uncertain": False,
                    "channel": entry.get("channel", ""),
                    "item": entry.get("item", ""),
                    "error": "owner confirmed no upload exists in Studio"}
        entry["result"] = verified
        old["result"] = verified
    _update_packet_manifests(state.get("packet_paths") or [],
                             [{**verified, "key": key}])
    _save_ledger(cfg, ledger)
    orders._save_pipeline_job(cfg, state)
    return True, f"job {job_id}: upload {key[:12]} marked {new_status}; safe to resume"
