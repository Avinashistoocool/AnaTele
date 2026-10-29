import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import sys

PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parent


def default_data_dir():
    """Source checkouts keep ./data; installed or frozen builds use a per-user directory."""
    if not getattr(sys, "frozen", False) and (ROOT / "pyproject.toml").exists() and os.access(ROOT, os.W_OK):
        return ROOT / "data"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AnaTele"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "anatele"


DATA = Path(os.environ.get("TELEMETRY_DATA_DIR") or default_data_dir()).resolve()
DB = DATA / "platform.db"
MAX_UPLOAD = 100 * 1024 * 1024


def utc_now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connection():
    DATA.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def initialize():
    for folder in ("uploads", "datasets", "worker_logs"):
        (DATA / folder).mkdir(parents=True, exist_ok=True)
    with connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS profiles(
            id TEXT PRIMARY KEY, drone_key TEXT NOT NULL, version INTEGER NOT NULL,
            created_at TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(drone_key,version)
        );
        CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, filename TEXT NOT NULL, format TEXT NOT NULL,
            size INTEGER NOT NULL, sha256 TEXT NOT NULL, path TEXT NOT NULL,
            profile_id TEXT, status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT,
            manifest TEXT
        );
        """)


def row_dict(row):
    if row is None:
        return None
    result = dict(row)
    for key in ("body", "manifest"):
        if result.get(key):
            result[key] = json.loads(result[key])
    return result


def get_job(job_id):
    with connection() as db:
        return row_dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def list_jobs():
    with connection() as db:
        return [row_dict(row) for row in db.execute(
            "SELECT id,filename,format,size,status,progress,created_at,finished_at,error,profile_id "
            "FROM jobs ORDER BY created_at DESC LIMIT 100")]


def list_jobs_full(limit=500):
    with connection() as db:
        return [row_dict(row) for row in db.execute(
            "SELECT id,filename,format,status,created_at,profile_id,manifest FROM jobs "
            "WHERE status IN ('complete','partial') ORDER BY created_at LIMIT ?", (limit,))]


def get_profile(profile_id):
    with connection() as db:
        return row_dict(db.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone())


def list_profiles():
    with connection() as db:
        return [row_dict(row) for row in db.execute("SELECT * FROM profiles ORDER BY created_at DESC")]


def save_profile(body):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM profiles WHERE drone_key=?",
                                 (body["drone_key"],)).fetchone()[0]
            profile_id = str(uuid.uuid4())
            db.execute("INSERT INTO profiles VALUES(?,?,?,?,?)",
                       (profile_id, body["drone_key"], version, utc_now(), json.dumps(body)))
            db.commit()
        except Exception:
            db.rollback()
            raise
    return get_profile(profile_id)


def enqueue(job_id, filename, kind, size, digest, path, profile_id):
    with connection() as db, db:
        db.execute(
            "INSERT INTO jobs(id,filename,format,size,sha256,path,profile_id,status,created_at)"
            " VALUES(?,?,?,?,?,?,?,'queued',?)",
            (job_id, filename, kind, size, digest, str(path), profile_id, utc_now()),
        )


def claim():
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if row:
            db.execute("UPDATE jobs SET status='processing',started_at=?,progress=1 WHERE id=?",
                       (utc_now(), row["id"]))
        db.commit()
    return row["id"] if row else None


def progress(job_id, percent):
    with connection() as db, db:
        db.execute("UPDATE jobs SET progress=? WHERE id=? AND status='processing'",
                   (min(95, percent), job_id))


def finish(job_id, manifest):
    with connection() as db, db:
        db.execute("UPDATE jobs SET status=?,progress=100,finished_at=?,manifest=?,error=NULL WHERE id=?",
                   ("partial" if manifest["partial"] else "complete", utc_now(), json.dumps(manifest), job_id))


def fail(job_id, error):
    with connection() as db, db:
        db.execute("UPDATE jobs SET status='failed',finished_at=?,error=? WHERE id=?",
                   (utc_now(), error[:4000], job_id))


def recover_interrupted():
    with connection() as db, db:
        db.execute(
            "UPDATE jobs SET status='failed',error='Worker stopped during analysis. Re-upload to retry.',"
            "finished_at=? WHERE status='processing'", (utc_now(),)
        )
