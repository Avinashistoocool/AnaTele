"""Copy a log into the data directory and queue it (same validation as the web upload). Qt-free."""
import hashlib
import uuid
from pathlib import Path

from . import store

FORMATS = {"tlog", "bin", "log", "csv"}


def import_file(path, profile_id=None):
    """Copy a log into the data directory and queue it (same rules as the web upload)."""
    path = Path(path)
    kind = path.suffix.lower().lstrip(".")
    if kind not in FORMATS:
        raise ValueError("Supported: Mission Planner .tlog, DataFlash .bin/.log, mapped .csv")
    size = path.stat().st_size
    if size == 0:
        raise ValueError("Empty file")
    if size > store.MAX_UPLOAD:
        raise ValueError("Local upload limit is 100 MiB")
    job_id = str(uuid.uuid4())
    target = store.DATA / "uploads" / f"{job_id}.{kind}"
    digest = hashlib.sha256()
    with path.open("rb") as source, target.open("xb") as output:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
    store.enqueue(job_id, path.name, kind, size, digest.hexdigest(), target, profile_id)
    return job_id
