import hashlib
import html
import json
import math
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import duckdb
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import compare as comparison, store
from .analysis import ENGINE_VERSION
from .worker import loop


@asynccontextmanager
async def lifespan(app):
    store.initialize()
    stop = threading.Event()
    worker = None
    if os.environ.get("TELEMETRY_DISABLE_WORKER") != "1":
        store.recover_interrupted()
        worker = threading.Thread(target=loop, args=(stop,), name="anatele-scheduler", daemon=False)
        worker.start()
    yield
    stop.set()
    if worker:
        worker.join(timeout=10)


app = FastAPI(title="AnaTele local investigation", version=ENGINE_VERSION, lifespan=lifespan)
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"] + [
    host.strip() for host in os.environ.get("ANATELE_ALLOWED_HOSTS", "").split(",") if host.strip()]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)


@app.middleware("http")
async def local_origin(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "Cross-origin writes are not allowed"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    return response


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    drone_key: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    frame: str = Field(default="", max_length=100)
    firmware: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=2000)
    expected_system_id: int | None = Field(default=None, ge=1, le=255)
    vibration_warning: float = Field(default=30, gt=0, le=500, allow_inf_nan=False)
    expected_parameters: dict[str, float] = Field(default_factory=dict, max_length=2000)

    @field_validator("expected_parameters")
    @classmethod
    def parameter_names(cls, value):
        for name, number in value.items():
            if not re.fullmatch(r"[A-Z0-9_]{1,16}", name):
                raise ValueError(f"Invalid ArduPilot parameter name: {name}")
            if not math.isfinite(number):
                raise ValueError(f"{name}: value must be finite")
        return value


def job_or_404(job_id):
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Unknown log")
    return job


def ready(job_id):
    job = job_or_404(job_id)
    if job["status"] not in ("complete", "partial"):
        raise HTTPException(409, f"Analysis is {job['status']}")
    return job


def query(job_id, table, sql, args=()):
    ready(job_id)
    path = store.DATA / "datasets" / job_id / (table + ".parquet")
    with duckdb.connect() as db:
        db.execute("SET memory_limit='256MB'")
        db.execute("SET threads=2")
        db.from_parquet(str(path)).create_view("samples")
        cursor = db.execute(sql, args)
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


@app.get("/api/health")
def health():
    heartbeat = store.DATA / "worker_heartbeat.json"
    active = False
    if heartbeat.exists():
        active = time.time() - heartbeat.stat().st_mtime < 10
    return {"status": "ok", "version": ENGINE_VERSION, "worker_active": active, "local_only": True}


@app.get("/api/profiles")
def profiles():
    return store.list_profiles()


@app.post("/api/profiles", status_code=201)
def create_profile(profile: Profile):
    return store.save_profile(profile.model_dump())


@app.get("/api/logs")
def logs():
    return store.list_jobs()


@app.post("/api/logs", status_code=202)
async def upload(file: UploadFile = File(...), profile_id: str | None = Form(default=None)):
    if profile_id and not store.get_profile(profile_id):
        raise HTTPException(400, "Unknown profile version")
    name = Path(file.filename or "unnamed").name
    kind = Path(name).suffix.lower().lstrip(".")
    if kind not in {"tlog", "bin", "log", "csv"}:
        raise HTTPException(415, "Supported: Mission Planner .tlog, DataFlash .bin/.log, mapped .csv")
    job_id = str(uuid.uuid4())
    target = store.DATA / "uploads" / (job_id + "." + kind)
    digest, size = hashlib.sha256(), 0
    try:
        with target.open("xb") as stream:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > store.MAX_UPLOAD:
                    raise HTTPException(413, "Local MVP upload limit is 100 MiB")
                digest.update(chunk)
                stream.write(chunk)
        if size == 0:
            raise HTTPException(400, "Empty file")
        store.enqueue(job_id, name, kind, size, digest.hexdigest(), target, profile_id or None)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    return {"id": job_id, "status": "queued"}


@app.get("/api/logs/{job_id}")
def detail(job_id: str):
    job = job_or_404(job_id)
    job.pop("path")
    if job["manifest"]:
        manifest = job["manifest"]
        manifest["event_count"] = len(manifest.pop("events"))
        parameters = manifest.pop("parameters")
        manifest["parameter_sets"] = [
            {key: value for key, value in snapshot.items() if key != "values"}
            | {"observed_names": len(snapshot["values"])}
            for snapshot in parameters
        ]
        manifest.pop("rule_results", None)
    job["profile"] = store.get_profile(job["profile_id"]) if job["profile_id"] else None
    return job


@app.get("/api/logs/{job_id}/events")
def events(job_id: str, source: str | None = None, severity: str | None = None,
           search: str = "", offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500),
           collapse: bool = True, epoch: int | None = None):
    rows = ready(job_id)["manifest"]["events"]
    rows = [event for event in rows if (not source or event["source"] == source)
            and (not severity or event["severity"] == severity)
            and (epoch is None or event["epoch"] == epoch)
            and (not search or search.lower() in (event["kind"] + " " + event["text"]).lower())]
    if collapse:
        groups, current = [], {}
        for event in rows:
            key = (event["source"], event["epoch"], event["kind"], event["severity"], event["text"])
            old = current.get(key)
            if old and 0 <= event["t_us"] - old["last_us"] <= 15_000_000:
                old["count"] += 1
                old["last_us"] = event["t_us"]
                old["evidence"].extend(event["evidence"])
            else:
                old = {**event, "count": 1, "last_us": event["t_us"], "evidence": list(event["evidence"])}
                groups.append(old)
                current[key] = old
        rows = groups
    return {"total": len(rows), "items": rows[offset:offset + limit]}


@app.get("/api/logs/{job_id}/parameters")
def parameters(job_id: str, source: str | None = None, search: str = ""):
    snapshots = ready(job_id)["manifest"]["parameters"]
    return [{**snapshot, "values": {name: value for name, value in snapshot["values"].items()
                                    if search.lower() in name.lower()}}
            for snapshot in snapshots if source is None or snapshot["source"] == source]


@app.get("/api/logs/{job_id}/rules")
def rule_results(job_id: str, state: str | None = None, rule_id: str | None = None):
    manifest = ready(job_id)["manifest"]
    rows = [r for r in manifest.get("rule_results", []) if (not state or r["state"] == state)
            and (not rule_id or r["rule_id"] == rule_id)]
    return {"rule_pack": manifest.get("rule_pack"), "items": rows}


@app.get("/api/logs/{job_id}/parameter-diff")
def parameter_diff(job_id: str, against_log: str | None = None, against_profile: str | None = None):
    manifest = ready(job_id)["manifest"]
    if against_log:
        result = comparison.parameter_diff(manifest, ready(against_log)["manifest"])
    else:
        profile_id = against_profile or ready(job_id)["profile_id"]
        profile = store.get_profile(profile_id) if profile_id else None
        if profile_id and profile is None:
            raise HTTPException(404, "Unknown profile version")
        result = comparison.parameter_diff(manifest, profile=profile)
    if "error" in result:
        raise HTTPException(409, result["error"])
    return result


@app.get("/api/compare")
def compare_logs(left: str, right: str, left_episode: int | None = None, right_episode: int | None = None):
    return comparison.compare(ready(left), ready(right), left_episode, right_episode)


@app.get("/api/drones/{drone_key}/trends")
def drone_trends(drone_key: str):
    return comparison.trends(drone_key)


@app.get("/api/logs/{job_id}/records/{record_id}")
def evidence(job_id: str, record_id: int):
    job = ready(job_id)
    rows = query(job_id, "observations", "SELECT * FROM samples WHERE id=?", [record_id])
    if not rows:
        raise HTTPException(404, "Record not found")
    record = rows[0]
    record["payload"] = json.loads(record.pop("payload_json"))
    record["locator_kind"] = "csv_line" if job["format"] == "csv" else "byte_range"
    record["artifact_sha256"] = job["sha256"]
    if record["length"]:
        with Path(job["path"]).open("rb") as stream:
            stream.seek(record["offset"])
            record["raw_hex_preview"] = stream.read(min(record["length"], 300)).hex(" ")
    return record


@app.get("/api/logs/{job_id}/series")
def series(job_id: str, signal: str, source: str, max_points: int = Query(1200, ge=20, le=4000),
           epoch: int | None = None):
    manifest = ready(job_id)["manifest"]
    if not any(item["name"] == signal and item["source"] == source for item in manifest["signals"]):
        raise HTTPException(404, "Signal/source not available")
    # Keep both extrema in each sequential bin, separately within each clock epoch.
    rows = query(job_id, "signals", """
        WITH ordered AS (
          SELECT *, row_number() OVER(PARTITION BY epoch ORDER BY t_us,id) AS rn,
                    count(*) OVER(PARTITION BY epoch) AS n
          FROM samples WHERE name=? AND source=? AND (? IS NULL OR epoch=?)
        ), buckets AS (
          SELECT *, floor((rn-1)/greatest(ceil(n/(?/2.0)),1)) AS bucket FROM ordered
        ), extrema AS (
          SELECT epoch,bucket,arg_min(id,value) AS min_id,arg_max(id,value) AS max_id FROM buckets GROUP BY epoch,bucket
        )
        SELECT b.id,b.t_us,b.epoch,b.value,b.unit FROM buckets b JOIN extrema e
        ON b.epoch=e.epoch AND b.bucket=e.bucket AND (b.id=e.min_id OR b.id=e.max_id)
        ORDER BY b.epoch,b.t_us,b.id LIMIT 8000
    """, [signal, source, epoch, epoch, max_points])
    return {"points": rows, "method": "min/max envelope per clock epoch; not used for diagnosis",
            "truncated": len(rows) == 8000, "signal": signal, "source": source}


@app.get("/api/logs/{job_id}/positions")
def positions(job_id: str, source: str, limit: int = Query(2000, ge=20, le=4000),
              epoch: int | None = None):
    rows = query(job_id, "signals", """
        WITH positions AS (
          SELECT id,t_us,epoch,
            max(CASE WHEN name='position.latitude' THEN value END) AS lat,
            max(CASE WHEN name='position.longitude' THEN value END) AS lon
          FROM samples WHERE source=? AND (? IS NULL OR epoch=?) AND name IN ('position.latitude','position.longitude')
          GROUP BY id,t_us,epoch
        ), numbered AS (
          SELECT *,row_number() OVER(ORDER BY epoch,t_us,id) AS rn,count(*) OVER() AS n
          FROM positions WHERE lat IS NOT NULL AND lon IS NOT NULL
        )
        SELECT id,t_us,epoch,lat,lon FROM numbered
        WHERE (rn-1)%greatest(ceil(n/?)::BIGINT,1)=0 ORDER BY epoch,t_us,id LIMIT 4000
    """, [source, epoch, epoch, limit])
    return {"points": rows, "notice": "Sampled recorded positions; not a navigation map or waypoint-completion proof."}


@app.get("/api/logs/{job_id}/original")
def original(job_id: str):
    job = job_or_404(job_id)
    return FileResponse(job["path"], media_type="application/octet-stream", filename=job["filename"])


@app.get("/api/logs/{job_id}/report", response_class=HTMLResponse)
def report(job_id: str):
    job = ready(job_id)
    manifest = job["manifest"]
    parts = [
        "<!doctype html><html lang='en'><meta charset='utf-8'><title>AnaTele investigation report</title>",
        "<style>body{font:15px/1.6 system-ui;max-width:950px;margin:40px auto;padding:20px}"
        "article{border:1px solid #aaa;padding:15px;margin:15px 0}code{overflow-wrap:anywhere}</style>",
        "<h1>AnaTele investigation report</h1>",
        "<p>Automated observations and investigation hypotheses. Not a crash verdict or airworthiness approval.</p>",
        f"<p>File: {html.escape(job['filename'])}<br>SHA-256: <code>{job['sha256']}</code><br>"
        f"Analysis engine: {manifest['version']}<br>Status: {job['status']}</p>",
    ]
    pack = manifest.get("rule_pack")
    if pack:
        parts.append(f"<p>Rule pack: {html.escape(pack['pack'])} v{pack['version']} "
                     f"(<code>{pack['sha256'][:16]}</code>)</p>")
    for source in manifest["sources"]:
        if source.get("semantic_pack"):
            parts.append(f"<p>Source {html.escape(source['key'])}: semantics {html.escape(source['semantic_pack'])} — "
                         f"{html.escape(source.get('semantic_match') or '')}</p>")
    t0 = manifest["first_time_us"] or 0
    if manifest.get("phases"):
        parts.append("<h2>Armed episodes and flight phases</h2><ul>")
        for index, phase in enumerate(manifest["phases"]):
            rel = lambda key: f"{(phase[key] - t0) / 1e6:.1f} s" if phase.get(key) else "—"
            parts.append(f"<li>Episode {index}: {html.escape(phase['status'])} ({html.escape(phase['method'] or 'n/a')}); "
                         f"armed {rel('armed_us')}, takeoff {rel('takeoff_us')}, pre-event end {rel('pre_event_end_us')}, "
                         f"crash report {rel('critical_event_us')}, disarmed {rel('disarmed_us')}. "
                         f"{html.escape(' '.join(phase['notes']))}</li>")
        parts.append("</ul><p>Times are relative to the first record.</p>")
    for incident in manifest.get("incidents", []):
        parts.append(f"<h2>Investigation hypotheses — episode {incident['episode']}</h2>"
                     f"<p>{html.escape(incident['method'])}</p><ol>")
        for h in incident["hypotheses"]:
            parts.append(f"<li><strong>{html.escape(h['title'])}</strong>: {html.escape(h['label'])}. "
                         f"Support: {html.escape(', '.join(s['title'] for s in h['support']) or 'none')}. "
                         f"Contradicting: {html.escape(', '.join(c['title'] + ' (' + c['reason'] + ')' for c in h['contradictions']) or 'none')}. "
                         f"Would discriminate: {html.escape(h['discriminating_evidence'])}</li>")
        parts.append("</ol>")
    parts.append("<h2>Findings</h2>")
    for finding in manifest["findings"]:
        parts.append(f"<article><h3>{html.escape(finding['title'])}</h3><p>{html.escape(finding['detail'])}</p>"
                     f"<p>Type: {finding['kind']} | Severity: {finding['severity']} | Source: {html.escape(finding['source'])}</p>")
        parts.append("<p>Evidence records: " + ", ".join(
            f"<a href='/api/logs/{job_id}/records/{number}'>{number}</a>" for number in finding["evidence"]) + "</p>")
        parts.append("<p>Missing: " + html.escape(", ".join(finding["missing"]) or "No additional requirements specified") + "</p></article>")
    if not manifest["findings"]:
        parts.append("<p>No supported rule triggered. This does not mean that the aircraft is healthy.</p>")
    results = manifest.get("rule_results", [])
    if results:
        parts.append("<h2>Rule coverage</h2><p>Not triggered is not the same as healthy; unassessable means the data "
                     "could not answer the question.</p><table border='1' cellpadding='4'><tr><th>Rule</th><th>Scope</th>"
                     "<th>State</th></tr>")
        for r in results:
            scope = r["scope"]
            label = scope.get("type", "") + " " + str(scope.get("episode", scope.get("signal", scope.get("source", ""))))
            parts.append(f"<tr><td>{html.escape(r['rule_id'])} v{r['rule_version']}</td><td>{html.escape(label)}</td>"
                         f"<td>{html.escape(r['state'])}</td></tr>")
        parts.append("</table>")
    for entry in manifest["missions"].get("execution", []):
        if not entry["items"]:
            continue
        parts.append(f"<h2>Mission items — episode {entry['episode']} (plan version {entry['plan_version']}, "
                     f"{html.escape(entry.get('plan_confidence') or '')})</h2><ul>")
        for item in entry["items"]:
            parts.append(f"<li>Item {item['seq']} {html.escape(item['command'])}: {html.escape(item['status'])}. "
                         f"{html.escape(item.get('note') or '')}</li>")
        parts.append("</ul>")
    parts.append("<h2>Limitations</h2><ul>" + "".join("<li>" + html.escape(item) + "</li>" for item in manifest["limitations"]) + "</ul>")
    parts.append("<p>Use your browser's Print command to save a PDF. Report links refer to this local server.</p></html>")
    return "".join(parts)


app.mount("/", StaticFiles(directory=store.PACKAGE / "static", html=True), name="frontend")
