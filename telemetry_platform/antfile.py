""".ant — a shareable, self-contained AnaTele analysis ("ANalysed Telemetry").

An .ant file is a ZIP container (like .docx/.odt) whose first entry is an uncompressed
``mimetype`` file containing ``application/vnd.anatele.analysis+zip``:

    mimetype               signature
    meta.json              format version, AnaTele/engine/rule-pack versions, per-file SHA-256
    manifest.json          findings, hypotheses, rule states, phases, missions, metrics, limitations
    observations.parquet   every decoded record (evidence viewer)
    signals.parquet        normalised time series (plots, statistics, flight path)
    report.html            printable report, readable without AnaTele
    original.<ext>         optional: the untouched source log (contains GPS positions)

Opening an .ant file never re-runs analysis and never executes anything from it. It is treated
as untrusted input: member names are whitelisted, sizes are capped while streaming, hashes are
verified and the JSON/Parquet structure is checked before anything is registered.
SHA-256 values prove the members are internally consistent, not who created the file.
"""
import hashlib
import json
import re
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, store

MIMETYPE = "application/vnd.anatele.analysis+zip"
FORMAT_VERSION = 1
FORMATS = {"tlog", "bin", "log", "csv"}
MAX_ANT = 1024 * 1024 * 1024
LIMITS = {
    "mimetype": 256, "meta.json": 1024 * 1024, "manifest.json": 64 * 1024 * 1024,
    "observations.parquet": 768 * 1024 * 1024, "signals.parquet": 768 * 1024 * 1024,
    "report.html": 32 * 1024 * 1024,
}
MAX_TOTAL = 1536 * 1024 * 1024
PARQUET_COLUMNS = {
    "observations.parquet": {"id", "t_us", "clock", "source", "epoch", "message", "offset", "length",
                             "sequence", "native_time_us", "quality", "payload_json"},
    "signals.parquet": {"id", "t_us", "epoch", "source", "name", "value", "unit"},
}
HEX64 = re.compile(r"[0-9a-f]{64}")


class AntError(ValueError):
    pass


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def safe_filename(name):
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", Path(str(name)).name).strip(" .")
    return (name or "shared-log")[:150]


# ---------------------------------------------------------------- export
def export_ant(job_id, out_path, include_original=False):
    job = store.get_job(job_id)
    if job is None or job["status"] not in ("complete", "partial") or not job.get("manifest"):
        raise AntError("Only finished analyses can be saved as .ant")
    original = Path(job["path"]) if job["path"] else None
    if include_original and (original is None or not original.is_file()):
        raise AntError("The original log is not available for this analysis")
    folder = store.DATA / "datasets" / job_id
    manifest = job["manifest"]
    files = {name: digest(folder / name) for name in ("observations.parquet", "signals.parquet")}
    profile = store.get_profile(job["profile_id"]) if job["profile_id"] else None
    if profile is None:
        profile = (manifest.get("shared_from") or {}).get("profile")
    from .api import report
    report_html = report(job_id).encode("utf-8")
    files["report.html"] = hashlib.sha256(report_html).hexdigest()
    manifest_bytes = json.dumps(manifest, separators=(",", ":"), allow_nan=False).encode("utf-8")
    files["manifest.json"] = hashlib.sha256(manifest_bytes).hexdigest()
    original_name = f"original.{job['format']}" if include_original else None
    if original_name:
        files[original_name] = job["sha256"]
    meta = {
        "format": "anatele-analysis", "format_version": FORMAT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(), "created_by": f"AnaTele {__version__}",
        "engine_version": manifest.get("version"), "rule_pack": manifest.get("rule_pack"),
        "status": job["status"],
        "log": {"filename": job["filename"], "format": job["format"], "size": job["size"], "sha256": job["sha256"]},
        "original_included": bool(include_original),
        "profile": profile["body"] if profile and "body" in profile else profile,
        "files": files,
    }
    out_path = Path(out_path)
    temp = out_path.with_name(out_path.name + ".part")
    try:
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr(zipfile.ZipInfo("mimetype"), MIMETYPE, compress_type=zipfile.ZIP_STORED)
            archive.writestr("meta.json", json.dumps(meta, indent=1))
            archive.writestr("manifest.json", manifest_bytes)
            for name in ("observations.parquet", "signals.parquet"):
                archive.write(folder / name, name)
            archive.writestr("report.html", report_html)
            if original_name:
                if digest(original) != job["sha256"]:
                    raise AntError("The stored original log no longer matches its recorded SHA-256")
                archive.write(original, original_name)
        temp.replace(out_path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return out_path


# ---------------------------------------------------------------- import
def _require(condition, message):
    if not condition:
        raise AntError(message)


def _list_of(value, kind, field):
    _require(isinstance(value, list) and all(isinstance(item, kind) for item in value), f"manifest.{field} is malformed")


def check_manifest(m):
    _require(isinstance(m, dict), "manifest.json is not an object")
    _require(isinstance(m.get("version"), str), "manifest.version missing")
    _require(isinstance(m.get("partial"), bool), "manifest.partial missing")
    _require(m.get("first_time_us") is None or isinstance(m.get("first_time_us"), int), "manifest.first_time_us malformed")
    for field in ("sources", "signals", "events", "findings", "parameters", "phases", "rule_results", "incidents"):
        _list_of(m.get(field, []), dict, field)
    _list_of(m.get("limitations", []), str, "limitations")
    _require(isinstance(m.get("missions", {}), dict), "manifest.missions malformed")
    for source in m.get("sources", []):
        _require(isinstance(source.get("key"), str), "manifest.sources malformed")
    for signal in m.get("signals", []):
        _require(isinstance(signal.get("name"), str) and isinstance(signal.get("source"), str), "manifest.signals malformed")
    for finding in m.get("findings", []):
        for key in ("title", "detail", "kind", "severity", "source"):
            _require(isinstance(finding.get(key), str), f"finding.{key} malformed")
        _list_of(finding.get("evidence", []), int, "findings.evidence")
        _list_of(finding.get("missing", []), str, "findings.missing")


def check_meta(meta):
    _require(isinstance(meta, dict) and meta.get("format") == "anatele-analysis", "Not an AnaTele analysis")
    _require(meta.get("format_version") == FORMAT_VERSION,
             f"Unsupported .ant format version {meta.get('format_version')!r}; update AnaTele")
    log = meta.get("log")
    _require(isinstance(log, dict) and log.get("format") in FORMATS, "meta.log malformed")
    _require(isinstance(log.get("filename"), str), "meta.log.filename malformed")
    _require(isinstance(log.get("sha256"), str) and HEX64.fullmatch(log["sha256"]), "meta.log.sha256 malformed")
    _require(isinstance(log.get("size"), int) and log["size"] >= 0, "meta.log.size malformed")
    _require(isinstance(meta.get("status"), str), "meta.status malformed")
    files = meta.get("files")
    _require(isinstance(files, dict) and all(isinstance(v, str) and HEX64.fullmatch(v) for v in files.values()),
             "meta.files malformed")
    _require(meta.get("profile") is None or isinstance(meta["profile"], dict), "meta.profile malformed")


def _read(archive, info, limit):
    with archive.open(info) as stream:
        data = stream.read(limit + 1)
    _require(len(data) <= limit, f"{info.filename} exceeds the size limit")
    return data


def _extract(archive, info, target, limit, budget):
    h, size = hashlib.sha256(), 0
    with archive.open(info) as stream, target.open("xb") as output:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            _require(size <= limit and size <= budget, f"{info.filename} exceeds the size limit")
            h.update(chunk)
            output.write(chunk)
    return h.hexdigest(), size


def _check_parquet(path, name):
    import duckdb
    try:
        with duckdb.connect() as db:
            db.execute("SET memory_limit='256MB'")
            columns = {row[0] for row in db.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()}
    except Exception as error:
        raise AntError(f"{name} is not a readable Parquet file") from error
    _require(columns == PARQUET_COLUMNS[name], f"{name} has unexpected columns")


def read_ant(path):
    """Validate an .ant file without importing it. Returns (meta, manifest)."""
    path = Path(path)
    _require(path.is_file(), "File not found")
    _require(path.stat().st_size <= MAX_ANT, ".ant files larger than 1 GiB are not accepted")
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as error:
        raise AntError("Not an .ant file (not a ZIP container)") from error
    with archive:
        meta, manifest, _ = _validate(archive)
    return meta, manifest


def _validate(archive):
    infos = archive.infolist()
    _require(infos and infos[0].filename == "mimetype", "Not an .ant file (missing signature)")
    names = [info.filename for info in infos]
    _require(len(names) == len(set(names)), "Duplicate entries in .ant file")
    originals = [n for n in names if n.startswith("original.")]
    allowed = set(LIMITS) | {f"original.{kind}" for kind in FORMATS}
    _require(set(names) <= allowed and len(originals) <= 1, "Unexpected entries in .ant file")
    for info in infos:
        _require(not info.flag_bits & 0x1, "Encrypted .ant entries are not supported")
        _require(info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED), "Unsupported compression")
    for required in ("meta.json", "manifest.json", "observations.parquet", "signals.parquet"):
        _require(required in names, f"{required} missing from .ant file")
    _require(_read(archive, infos[0], LIMITS["mimetype"]).decode("ascii", "replace").strip() == MIMETYPE,
             "Not an .ant file (wrong signature)")
    try:
        meta = json.loads(_read(archive, archive.getinfo("meta.json"), LIMITS["meta.json"]))
        raw_manifest = _read(archive, archive.getinfo("manifest.json"), LIMITS["manifest.json"])
        manifest = json.loads(raw_manifest)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AntError("Corrupt JSON in .ant file") from error
    check_meta(meta)
    check_manifest(manifest)
    _require(hashlib.sha256(raw_manifest).hexdigest() == meta["files"].get("manifest.json"),
             "manifest.json does not match its SHA-256 (file altered or damaged)")
    if originals:
        _require(meta.get("original_included") and originals[0] == f"original.{meta['log']['format']}",
                 "Original log entry does not match meta.json")
    return meta, manifest, originals[0] if originals else None


def import_ant(path):
    """Register a shared analysis as a new local recording. Returns the new job id."""
    path = Path(path)
    _require(path.is_file(), "File not found")
    _require(path.stat().st_size <= MAX_ANT, ".ant files larger than 1 GiB are not accepted")
    store.initialize()
    job_id = str(uuid.uuid4())
    staging = store.DATA / "datasets" / f".import-{job_id}"
    final = store.DATA / "datasets" / job_id
    upload = None
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as error:
        raise AntError("Not an .ant file (not a ZIP container)") from error
    try:
        with archive:
            meta, manifest, original_name = _validate(archive)
            staging.mkdir(parents=True)
            budget = MAX_TOTAL
            for name in ("observations.parquet", "signals.parquet"):
                sha, size = _extract(archive, archive.getinfo(name), staging / name, LIMITS[name], budget)
                budget -= size
                _require(sha == meta["files"].get(name), f"{name} does not match its SHA-256 (file altered or damaged)")
                _check_parquet(staging / name, name)
            if original_name:
                upload = store.DATA / "uploads" / f"{job_id}.{meta['log']['format']}"
                sha, size = _extract(archive, archive.getinfo(original_name), upload, store.MAX_UPLOAD, budget)
                _require(sha == meta["log"]["sha256"] and size == meta["log"]["size"],
                         "Original log does not match its SHA-256 (file altered or damaged)")
        manifest["parquet_sha256"] = {name: meta["files"][name] for name in ("observations.parquet", "signals.parquet")}
        manifest["artifact_sha256"] = meta["log"]["sha256"]
        manifest["profile_id"] = None
        manifest["shared_from"] = {
            "file": safe_filename(path.name), "imported_at": store.utc_now(),
            "created_at": str(meta.get("created_at", ""))[:40], "created_by": str(meta.get("created_by", ""))[:60],
            "original_included": bool(original_name), "profile": meta.get("profile"),
        }
        if not original_name:
            manifest.setdefault("limitations", []).append(
                "Opened from a shared .ant file without the original log: raw bytes and re-analysis are unavailable.")
        if manifest.get("drone_key") is not None and not isinstance(manifest["drone_key"], str):
            manifest["drone_key"] = None
        staging.rename(final)
        store.insert_imported(job_id, safe_filename(meta["log"]["filename"]), meta["log"]["format"],
                              meta["log"]["size"], meta["log"]["sha256"], upload or "",
                              "partial" if manifest["partial"] else "complete", manifest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(final, ignore_errors=True)
        if upload:
            upload.unlink(missing_ok=True)
        raise
    return job_id
