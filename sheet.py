"""The source sheet: paste YouTube links, the lanes eat them.

    python main.py sheet                    # where things stand
    python main.py sheet --add <link>       # queue a source (or paste rows
                                            #  straight into the CSV)
    python main.py meeting pick             # the boardroom argues + chooses
    python main.py clip --sheet             # clip the next queued source

The sheet is a tiny CSV (sources/sheet.csv by default) so it opens in
Excel AND stays hand-editable in notepad: one row per source,

    url,note,added,status,result
    https://youtube.com/watch?v=...,deep dive on aqueducts,2026-10-01,new,
    https://youtu.be/...,best channel ever,2026-10-02,clipped,clips/ — Rome

You only ever fill the first two columns (pasting a bare link on a line
is fine too); the tool fills `added` when you use `sheet --add` and owns
`status`/`result` from there on:

    new     queued (a bare empty status counts as new)
    picked  the boardroom chose it (the reason lands in `result`)
    clipped rendered — the row keeps which lane/channel did it
    failed  refused (missing transcript, dead link, ...) — result says why

Rows are NEVER deleted: the sheet is the history, the queue is
`status` new/picked, and `clip --sheet` takes `picked` rows first (the
board's decision outranks queue order), then the oldest `new` rows.
Runs are sequential, each marks its row, so two channels never clip the
same source. The old `topics/sources.txt` watchlist ('URL | note' lines)
still counts as candidates — it just never grows anymore.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

PENDING_STATUSES = ("", "new", "picked")
TAGS = {"picked": "picked", "new": "queued", "": "queued",
        "clipped": "clipped", "failed": "failed"}


class SheetError(RuntimeError):
    pass


# ------------------------------------------------------------- pure reads
def parse_sheet(text: str) -> list[dict]:
    """CSV text -> row dicts (pure, tested).

    Header row and non-http rows are skipped (a stray note line can't
    become a candidate). Missing columns default to empty; `status` is
    normalized to lowercase.
    """
    rows: list[dict] = []
    for raw in csv.reader((text or "").splitlines()):
        if not raw or not (raw[0] or "").strip():
            continue
        url = raw[0].strip()
        if url.lower() == "url":          # header
            continue
        if not url.lower().startswith(("http://", "https://")):
            continue
        rows.append({"url": url,
                     "note": (raw[1].strip() if len(raw) > 1 else ""),
                     "added": (raw[2].strip() if len(raw) > 2 else ""),
                     "status": (raw[3].strip().lower()
                                if len(raw) > 3 else ""),
                     "result": (raw[4].strip() if len(raw) > 4 else "")})
    return rows


def pending_rows(rows: list[dict]) -> list[dict]:
    """new + picked rows, board decisions first (pure, tested)."""
    picked = [r for r in rows if r["status"] == "picked"]
    new = [r for r in rows if r["status"] in ("", "new")]
    return picked + new


def sheet_table(rows: list[dict], path: Path | None = None) -> str:
    """The `sheet` command's one-glance listing (pure, tested)."""
    if not rows:
        where = f" — {path}" if path else ""
        return (f"  [sheet] the sheet is empty{where}\n"
                "  Paste links (one per row, 'url,note') into it, or:\n"
                "    python main.py sheet --add <youtube link>")
    counts = {"picked": 0, "queued": 0, "clipped": 0, "failed": 0}
    for row in rows:
        counts[TAGS.get(row["status"], "queued")] += 1
    lines = [f"  [sheet] today (the board's pick): {counts['picked']}   "
             f"queued: {counts['queued']}   clipped: {counts['clipped']}   "
             f"failed: {counts['failed']}"]
    order = {"picked": 0, "": 1, "new": 1, "clipped": 2, "failed": 2}
    for row in sorted(rows, key=lambda r: order.get(r["status"], 1)):
        tag = TAGS.get(row["status"], "queued")
        url = row["url"]
        url = url if len(url) <= 64 else url[:61] + "..."
        tail = row["result"] or row["note"]
        tail = tail if len(tail) <= 46 else tail[:45] + "…"
        when = row["added"] or "????-??-??"
        lines.append(f"  [{tag:<7}] {when}  {url}"
                     + (f"  — {tail}" if tail else ""))
    return "\n".join(lines)


# ------------------------------------------------------------- file ops
def _same_url(a: str, b: str) -> bool:
    return (a or "").strip().rstrip("/") == (b or "").strip().rstrip("/")


def load_sheet(path: Path) -> list[dict]:
    try:
        return parse_sheet(path.read_text(encoding="utf-8"))
    except OSError:
        return []


def ensure_sheet(path: Path) -> None:
    """Create the sheet with its header if it's missing (idempotent)."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("url,note,added,status,result\n",
                        encoding="utf-8")


def append_sheet(path: Path, url: str, note: str = "") -> bool:
    """Add a source row (deduped). Returns True if it was new."""
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise SheetError(f"that doesn't look like a link: {url[:60]!r}")
    rows = load_sheet(path)
    if any(_same_url(r["url"], url) for r in rows):
        return False
    ensure_sheet(path)
    with path.open("a", encoding="utf-8", newline="") as fh:
        csv.writer(fh, lineterminator="\n").writerow(
            [url, (note or "").strip(), date.today().isoformat(), "new", ""])
    return True


def take_pending(path: Path, count: int) -> list[tuple[str, str]]:
    """The next `count` queued (url, note) pairs — picks first."""
    count = max(0, int(count or 0))
    if count <= 0:
        return []
    return [(row["url"], row["note"])
            for row in pending_rows(load_sheet(path))[:count]]


def mark_sheet(path: Path, url: str, status: str,
               result: str = "") -> bool:
    """Set one row's status/result (best-effort, never raises).

    The row keeps its place and history; unknown statuses are refused.
    Returns True if a row was updated.
    """
    if status not in ("new", "picked", "clipped", "failed"):
        return False
    try:
        rows = load_sheet(path)
        hit = False
        for row in rows:
            if not hit and _same_url(row["url"], url):
                row["status"] = status
                row["result"] = (result or "").strip()[:200]
                hit = True
        if hit:
            with path.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh, lineterminator="\n")
                writer.writerow(["url", "note", "added", "status", "result"])
                writer.writerows([[r["url"], r["note"], r["added"],
                                   r["status"], r["result"]] for r in rows])
        return hit
    except OSError:
        return False
