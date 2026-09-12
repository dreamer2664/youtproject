"""Job queue persisted to state.json.

A job is one video, tracked through:
    queued -> generated -> packaged -> published
                                  -> failed

"packaged" means an upload-ready folder exists under upload/<id>/.
"published" is set by YOU after uploading manually in YouTube Studio:
    python main.py published <id> <youtube-url>
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Job:
    id: str
    topic: str
    status: str = "queued"
    title: str = ""
    video_file: str = ""
    meta_file: str = ""
    package_dir: str = ""
    published_url: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class Queue:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.jobs: list[Job] = []
        self.load()

    # -- persistence -----------------------------------------------------
    def load(self) -> None:
        if not self.path.exists():
            self.jobs = []
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            # Tolerate jobs written by older versions with extra/missing keys.
            jobs = []
            for item in raw.get("jobs", []):
                known = {f for f in Job.__dataclass_fields__}
                jobs.append(Job(**{k: v for k, v in item.items() if k in known}))
            self.jobs = jobs
        except (json.JSONDecodeError, TypeError) as exc:
            # Never lose the queue because of one bad write: move it aside.
            backup = self.path.with_suffix(".corrupt.json")
            self.path.rename(backup)
            print(f"[queue] state.json was unreadable ({exc}); moved to {backup.name}")
            self.jobs = []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"jobs": [asdict(j) for j in self.jobs]}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    # -- operations ------------------------------------------------------
    def add(self, topic: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], topic=topic)
        self.jobs.append(job)
        self.save()
        return job

    def get(self, job_id: str) -> Job | None:
        for job in self.jobs:
            if job.id == job_id or job.id.startswith(job_id):
                return job
        return None

    def update(self, job: Job, **fields: Any) -> Job:
        for key, value in fields.items():
            setattr(job, key, value)
        job.updated_at = time.time()
        self.save()
        return job

    def pending(self) -> list[Job]:
        """Generated videos waiting to be packaged for upload."""
        return [j for j in self.jobs if j.status == "generated"]

    def in_status(self, status: str) -> list[Job]:
        return [j for j in self.jobs if j.status == status]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for job in self.jobs:
            out[job.status] = out.get(job.status, 0) + 1
        return out

    def format_table(self) -> str:
        if not self.jobs:
            return "Queue is empty. Run: python main.py generate"
        lines = [
            f"{'ID':<13}{'STATUS':<12}TITLE",
            "-" * 78,
        ]
        for job in self.jobs:
            title = job.title or job.topic
            lines.append(f"{job.id:<13}{job.status:<12}{title[:52]}")
            if job.package_dir:
                lines.append(f"{'':<13}\u2514\u2500 kit: {job.package_dir}")
            if job.published_url:
                lines.append(f"{'':<13}\u2514\u2500 {job.published_url}")
            if job.error:
                lines.append(f"{'':<13}\u2514\u2500 error: {job.error[:60]}")
        return "\n".join(lines)
