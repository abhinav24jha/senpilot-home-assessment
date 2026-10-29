"""One process, durable atomic job records; SMTP uncertainty is never auto-retried."""
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import os


def now():
    return datetime.now(timezone.utc).isoformat()


def json_default(value):
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(type(value).__name__)


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, default=json_default, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextmanager
def process_lock(data_dir: Path):
    # macOS/Linux local deployment. Kernel releases the lock even after a crash.
    import fcntl
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / ".agent.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another agent process is using this data directory") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class JobStore:
    def __init__(self, data_dir: Path):
        self.root = data_dir / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)

    def identity(self, incoming):
        identity = "\0".join((incoming.sender.lower(), incoming.message_id, incoming.subject, incoming.body))
        return hashlib.sha256(identity.encode()).hexdigest()[:24]

    def path(self, job_id):
        if len(job_id) != 24 or any(c not in "0123456789abcdef" for c in job_id):
            raise ValueError("Invalid job ID")
        return self.root / job_id

    def read(self, job_id):
        path = self.path(job_id) / "job.json"
        return json.loads(path.read_text()) if path.exists() else None

    def save(self, job):
        job["updated_at"] = now()
        atomic_json(self.path(job["id"]) / "job.json", job)

    def list(self):
        return [json.loads(p.read_text()) for p in sorted(self.root.glob("*/job.json"))]
