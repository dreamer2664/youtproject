"""Night batch: one unattended run that does the day's work while you sleep.

Why this exists (owner's workflow, 2026-10-04): generating a video takes
~15 minutes and clipping a source ~10, and with several channels that adds
up to two hours of sitting at the PC. But none of it needs a human — it
needs a *PC that is on*. So this command is built to be fired by Windows
Task Scheduler at night:

    nightbatch = clip the sheet queue -> generate videos -> park clips on
    Telegram -> report to Telegram

Design rules:

* one step at a time, each in its own subprocess (`main.py clip` /
  `main.py batch` / `main.py pregen`) so a crash in one step can never
  kill the batch, and each has its own log file;
* a journal (`work/nightbatch/<date>.json`) is written after EVERY step,
  and a re-run the same day resumes: finished steps are skipped, failed
  ones retried. A power cut at 02:10 costs you one step, not the night;
* a lock file refuses concurrent runs (two batches racing over the sheet
  and the key pools is how you get double work);
* it can NOT post anything. The only lanes it touches are clip, batch and
  pregen (park files on Telegram) — there is no autopost, no Buffer, no
  publish path anywhere in this module. Uploads stay manual, by design.

Usage:

    python main.py nightbatch                 # 2 clips + 2 videos + park + report
    python main.py nightbatch --dry-run       # show the plan, run nothing
    python main.py nightbatch --clips 3 --count 1
    python main.py nightbatch --fresh         # ignore today's journal, redo all

Windows: double-click `Night Batch.bat` (or let Task Scheduler run it at
01:30 — see OPERATIONS.md "The night batch").
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Wall-clock estimates for --dry-run only; real durations land in the journal.
CLIP_EST_MIN = 10
GEN_EST_MIN = 15
PUSH_EST_MIN = 1
MEETING_EST_MIN = 4

# A lock older than this many hours is treated as stale (a crashed run must
# not block the whole day). Same-day locks younger than this need --force.
LOCK_STALE_HOURS = 6


# --------------------------------------------------------------------- paths
def batch_dir(cfg) -> Path:
    return Path(cfg.work_dir) / "nightbatch"


def journal_path(cfg, date: str) -> Path:
    return batch_dir(cfg) / f"{date}.json"


def lock_path(cfg) -> Path:
    return batch_dir(cfg) / "lock.json"


def log_path(cfg, date: str, step_id: str) -> Path:
    return batch_dir(cfg) / "logs" / f"{date}-{step_id}.log"


# ---------------------------------------------------------------------- lock
def acquire_lock(cfg, force: bool = False) -> tuple[bool, str]:
    """Take the single-runner lock. Returns (ok, message)."""
    path = lock_path(cfg)
    if path.exists() and not force:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a corrupt lock must not brick runs
            data = {}
        age_h = None
        try:
            age_h = (datetime.now().timestamp()
                     - path.stat().st_mtime) / 3600.0
        except OSError:
            pass
        if age_h is not None and age_h < LOCK_STALE_HOURS:
            started = data.get("started", "?")
            pid = data.get("pid", "?")
            return (False,
                    f"another night batch looks alive (pid {pid}, since "
                    f"{started}, {age_h:.1f}h ago). Wait for it, or re-run "
                    "with --force if you know it died.")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pid": os.getpid(),
                               "started": datetime.now().isoformat(
                                   timespec="seconds")}),
                   encoding="utf-8")
    tmp.replace(path)
    return True, ""


def release_lock(cfg) -> None:
    try:
        lock_path(cfg).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------- plan
def plan_steps(cfg, clips: int = 2, count: int = 2, seconds: int | None = None,
               style: str | None = None, image_provider: str | None = None,
               max_clips: int = 0, push: bool = True,
               meeting: bool = False, review: bool = False, top: int = 5,
               since_iso: str | None = None) -> tuple[list[dict], list[str]]:
    """Build the step list (and human notes). Pure — no side effects.

    Each step: {id, kind, label, argv, est_min}. `argv` is a full command
    line for this same interpreter, so the run is just subprocesses.

    With `meeting=True` the boardroom opens with its stats review; with
    `review=True` a second boardroom sitting ranks the finished clips by
    hook + overall quality and writes the upload plan (review.json next
    to the journal).
    """
    steps: list[dict] = []
    notes: list[str] = []

    if meeting:
        steps.append({
            "id": "meeting-stats", "kind": "meeting",
            "label": "boardroom: stats review (opens the night)",
            "argv": [sys.executable, "-u", str(ROOT / "main.py"),
                     "meeting", "stats", "--json-out",
                     str(batch_dir(cfg) / "stats-meeting.json")],
            "est_min": MEETING_EST_MIN})

    pending = 0
    if clips > 0:
        try:
            from sheet import load_sheet, pending_rows

            pending = len(pending_rows(load_sheet(Path(cfg.sources_sheet))))
        except Exception:  # noqa: BLE001 - a missing sheet is not fatal
            pending = 0
        if pending == 0:
            notes.append("clip: sheet has no queued sources — add links "
                         "(panel or `main.py sheet --add <link>`), skipping.")
        take = min(int(clips), pending)
        for index in range(1, take + 1):
            argv = [sys.executable, "-u", str(ROOT / "main.py"), "clip",
                    "--sheet", "1"]
            if max_clips and max_clips > 0:
                argv += ["--max-clips", str(int(max_clips))]
            label = f"clip source {index} of {take} from the sheet"
            steps.append({"id": f"clip-{index}", "kind": "clip", "label": label,
                          "argv": argv, "est_min": CLIP_EST_MIN})

    if review:
        argv = [sys.executable, "-u", str(ROOT / "main.py"),
                "meeting", "review", "--json-out",
                str(batch_dir(cfg) / "review.json"),
                "--top", str(max(1, int(top)))]
        if since_iso:
            argv += ["--since", since_iso]
        steps.append({
            "id": "review", "kind": "meeting",
            "label": f"boardroom: rank the new clips, pick the best {top}",
            "argv": argv, "est_min": MEETING_EST_MIN})

    if count > 0:
        argv = [sys.executable, "-u", str(ROOT / "main.py"), "batch",
                "--count", str(int(count))]
        if seconds:
            argv += ["--seconds", str(int(seconds))]
        if style:
            argv += ["--style", str(style)]
        if image_provider:
            argv += ["--image-provider", str(image_provider)]
        steps.append({"id": "batch-1", "kind": "generate",
                      "label": f"generate {int(count)} video(s) from the "
                               "topic backlog",
                      "argv": argv, "est_min": GEN_EST_MIN * int(count)})

    if push and any(s["kind"] == "clip" for s in steps):
        if telegram_ready(cfg):
            steps.append({"id": "push", "kind": "pregen",
                          "label": "park new clips on Telegram (phone pull)",
                          "argv": [sys.executable, "-u", str(ROOT / "main.py"),
                                   "pregen", "--push"],
                          "est_min": PUSH_EST_MIN})
        else:
            notes.append("pregen: Telegram not configured — skipping the "
                         "phone park step.")

    if not steps:
        notes.append("nothing to do: no queued sources and --count 0.")
    return steps, notes


def telegram_ready(cfg) -> bool:
    try:
        return bool(cfg.telegram_token and cfg.telegram_owner)
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------- journal
def load_journal(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("steps"), dict):
            return data
    except Exception:  # noqa: BLE001 - fresh journal on any corruption
        pass
    return {"steps": {}}


def save_journal(path: Path, journal: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(journal, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(path)


# -------------------------------------------------------------------- runner
def default_runner(argv: list[str], log: Path,
                   cwd: Path) -> tuple[int, str]:
    """Run one step, tee its output to console AND the log. Never raises.

    Returns (returncode, tail). A missing executable or a crash mid-step
    comes back as rc=-1 with the reason — the batch keeps its footing.
    """
    tail: list[str] = []
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w", encoding="utf-8", errors="replace") as fh:
            proc = subprocess.Popen(
                argv, cwd=str(cwd), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1)
            assert proc.stdout is not None
            for line in proc.stdout:
                print(line, end="")
                fh.write(line)
                tail.append(line)
                if len(tail) > 40:
                    tail.pop(0)
            rc = proc.wait()
        return rc, "".join(tail[-6:])
    except Exception as exc:  # noqa: BLE001 - a step crash is data, not death
        return -1, f"{exc!r}"


# ------------------------------------------------------------------ snapshot
def _mp4s(root: Path) -> set[str]:
    try:
        return {str(p) for p in Path(root).rglob("*.mp4")}
    except OSError:
        return set()


# -------------------------------------------------------------------- report
def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a missing decision is just no picks
        return {}
    return data if isinstance(data, dict) else {}


def build_report(date: str, records: list[dict], new_clips: int,
                 new_videos: int, notes: list[str],
                 journal: Path, picks: list[dict] | None = None,
                 stats_summary: str = "",
                 review_note: str = "") -> str:
    """The morning message. Pure function, easy to test."""
    ran = [r for r in records if r["status"] in ("done", "failed")]
    ok = [r for r in ran if r["status"] == "done"]
    bad = [r for r in ran if r["status"] == "failed"]
    skipped = [r for r in records if r["status"] == "skipped"]
    head = (f"🌙 Night batch {date} — {len(ok)}/{len(ran)} steps ok"
            if ran else f"🌙 Night batch {date} — nothing ran")
    lines = [head, ""]
    for rec in records:
        if rec["status"] == "skipped":
            icon = "↩︎"
        elif rec["status"] == "done":
            icon = "✅"
        else:
            icon = "❌"
        mins = rec.get("minutes")
        when = f" ({mins:.0f}m)" if mins else ""
        lines.append(f"{icon} {rec['label']}{when}")
    lines.append("")
    lines.append(f"New on disk: {new_clips} clip(s), {new_videos} video(s).")
    if stats_summary:
        lines.append("")
        lines.append(f"📊 Boardroom (stats): {stats_summary}")
    if picks:
        lines.append("")
        lines.append("🎯 POST THESE TODAY (strongest first):")
        for pick in picks:
            title = pick.get("title") or pick.get("id") or "?"
            why = (pick.get("why") or "").strip()
            lines.append(f"  {pick.get('rank', '?')}. {title}"
                         + (f" — {why}" if why else ""))
    elif review_note:
        lines.append("")
        lines.append(f"🎬 Clip review: {review_note}")
    if skipped:
        lines.append(f"Resumed: {len(skipped)} step(s) were already done "
                     "(skipped).")
    if notes:
        lines.append("")
        lines += [f"ℹ️ {n}" for n in notes]
    if bad:
        lines.append("")
        lines.append("Failures (full logs in work/nightbatch/logs/):")
        for rec in bad:
            detail = (rec.get("tail") or "").strip().splitlines()
            why = detail[-1][:160] if detail else f"rc={rec.get('rc')}"
            lines.append(f"  • {rec['label']} — {why}")
    lines.append("")
    lines.append("Nothing was posted anywhere — uploads stay manual. "
                 f"Journal: {journal.name}")
    return "\n".join(lines)


def notify_telegram(cfg, text: str) -> bool:
    """Best-effort report send; a dead network never fails the batch."""
    try:
        import requests

        token, owner = cfg.telegram_token, cfg.telegram_owner
        if not (token and owner):
            return False
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": owner, "text": text[:4000]}, timeout=15)
        return bool(resp.ok)
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------------- run
def run(cfg, clips: int = 2, count: int = 2, seconds: int | None = None,
        style: str | None = None, image_provider: str | None = None,
        max_clips: int = 0, push: bool = True, report: bool = True,
        fresh: bool = False, force: bool = False, dry_run: bool = False,
        date: str | None = None, runner=None, notify=None,
        echo=print, meeting: bool = False, review: bool = False,
        top: int = 5, if_requested: bool = False) -> int:
    """Run the night batch once. Returns a process exit code.

    `if_requested`: the /go flow. Reads work/nightrun/request.json; with
    no pending request it exits 0 without doing anything (so a wake task
    that fires early is a no-op). With one, the request's options WIN
    over the flags, and the request is marked done when the run ends —
    even when steps failed, because the report says what failed and a
    nightly retry loop would be worse.
    """
    date = date or datetime.now().strftime("%Y-%m-%d")
    runner = runner or default_runner
    notify = notify or notify_telegram

    if if_requested:
        import nightreq

        request = nightreq.read_request(cfg)
        if not request or request.get("status") != "pending":
            echo("  [nightbatch] no pending /go request — nothing to do.")
            return 0
        opts = request.get("options") or {}
        clips = int(opts.get("clips", clips))
        count = int(opts.get("count", count))
        meeting = bool(opts.get("meeting", meeting))
        review = bool(opts.get("review", review))
        top = int(opts.get("top", top))
        echo(f"  [nightbatch] /go request from {request.get('created', '?')}"
             f" — clips={clips}, count={count}, meeting={meeting}, "
             f"review={review}")

    steps, notes = plan_steps(cfg, clips=clips, count=count, seconds=seconds,
                              style=style, image_provider=image_provider,
                              max_clips=max_clips, push=push,
                              meeting=meeting, review=review, top=top,
                              since_iso=datetime.now().isoformat(
                                  timespec="seconds"))

    if dry_run:
        echo(f"  [nightbatch] plan for {date} (dry run — nothing executes)")
        for note in notes:
            echo(f"  [nightbatch] note: {note}")
        total = sum(s["est_min"] for s in steps)
        for step in steps:
            echo(f"    {step['id']:<8} ~{step['est_min']:>2}m  {step['label']}")
        echo(f"  [nightbatch] {len(steps)} step(s), ~{total} minutes; "
             "journal: " + str(journal_path(cfg, date).relative_to(
                 Path(cfg.root))))
        echo("  [nightbatch] nothing public: clip + batch + pregen only")
        return 0

    ok, message = acquire_lock(cfg, force=force)
    if not ok:
        echo(f"  [nightbatch] {message}")
        return 2

    journal_file = journal_path(cfg, date)
    journal = {"date": date, "steps": {}} if fresh else load_journal(
        journal_file)
    journal["date"] = date
    before_clips = _mp4s(Path(cfg.root) / "clips")
    before_videos = _mp4s(Path(cfg.out_dir))
    records: list[dict] = []
    try:
        echo(f"  [nightbatch] {len(steps)} step(s) for {date} — "
             "logs in work/nightbatch/logs/")
        for step in steps:
            prior = journal["steps"].get(step["id"], {})
            if prior.get("status") == "done":
                echo(f"  [nightbatch] ↩︎ {step['id']} already done — skip "
                     "(resume; --fresh to redo)")
                records.append({"id": step["id"], "label": step["label"],
                                "status": "skipped",
                                "minutes": prior.get("minutes")})
                continue
            echo(f"\n  [nightbatch] ▶ {step['id']}: {step['label']}")
            started = datetime.now()
            log = log_path(cfg, date, step["id"])
            rc, tail = runner(step["argv"], log, ROOT)
            minutes = (datetime.now() - started).total_seconds() / 60.0
            status = "done" if rc == 0 else "failed"
            journal["steps"][step["id"]] = {
                "status": status, "rc": rc, "minutes": round(minutes, 1),
                "label": step["label"], "log": str(log)}
            save_journal(journal_file, journal)  # crash-safe: after EACH step
            echo(f"  [nightbatch] {'✅' if rc == 0 else '❌'} {step['id']} "
                 f"({minutes:.0f}m, rc={rc}) — log: {log.name}")
            records.append({"id": step["id"], "label": step["label"],
                            "status": status, "minutes": minutes,
                            "rc": rc, "tail": tail})
    finally:
        release_lock(cfg)

    new_clips = len(_mp4s(Path(cfg.root) / "clips") - before_clips)
    new_videos = len(_mp4s(Path(cfg.out_dir)) - before_videos)
    review_json = _read_json(batch_dir(cfg) / "review.json")
    stats_json = _read_json(batch_dir(cfg) / "stats-meeting.json")
    picks = review_json.get("picks") or []
    review_note = ""
    if review and not picks:
        review_note = (review_json.get("summary")
                       or "the boardroom produced no ranking this run")
    text = build_report(date, records, new_clips, new_videos, notes,
                        journal_file, picks=picks,
                        stats_summary=str(stats_json.get("summary") or ""),
                        review_note=review_note)
    echo("\n" + text)
    if report:
        if notify(cfg, text):
            echo("  [nightbatch] report sent to Telegram.")
        elif telegram_ready(cfg):
            echo("  [nightbatch] report send failed — read it above.")
    failed = [r for r in records if r["status"] == "failed"]
    if if_requested:
        import nightreq

        nightreq.mark_done(cfg, failed=len(failed),
                           note=text.splitlines()[0] if text else "")
        echo("  [nightbatch] /go request marked done.")
    return 1 if failed else 0
