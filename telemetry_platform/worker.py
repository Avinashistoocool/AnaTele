import argparse
import json
import logging
import subprocess
import sys
import time

from . import store

TIMEOUT_SECONDS = 180
log = logging.getLogger("anatele.worker")


def worker_command(job_id):
    """Command for one isolated analysis process; a frozen build re-invokes its own executable."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--worker-job", job_id]
    return [sys.executable, "-m", "telemetry_platform.worker", "--job", job_id]


def worker_environment():
    import os
    return {**os.environ, "TELEMETRY_DATA_DIR": str(store.DATA)}


def loop(stop):
    while not stop.is_set():
        heartbeat = store.DATA / "worker_heartbeat.json"
        temporary = heartbeat.with_suffix(".tmp")
        temporary.write_text(json.dumps({"time": time.time()}), encoding="utf-8")
        temporary.replace(heartbeat)
        job_id = store.claim()
        if job_id is None:
            stop.wait(1)
            continue
        path = store.DATA / "worker_logs" / f"{job_id}.txt"
        with path.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(worker_command(job_id), cwd=store.ROOT, stdout=output,
                                       stderr=subprocess.STDOUT, env=worker_environment())
            started = time.monotonic()
            while process.poll() is None:
                # The scheduler remains alive while a parser is busy.
                temporary.write_text(json.dumps({"time": time.time(), "job": job_id}), encoding="utf-8")
                temporary.replace(heartbeat)
                if stop.wait(.5) or time.monotonic() - started > TIMEOUT_SECONDS:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    store.fail(job_id, "Analysis stopped: server shutdown or 180-second local-worker deadline")
                    break
            result = store.get_job(job_id)
            if result["status"] == "processing":
                store.fail(job_id, f"Parser process exited with code {process.returncode}; see its local worker log")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    args = parser.parse_args(argv)
    store.initialize()
    job = store.get_job(args.job)
    if job is None or job["status"] != "processing":
        print("Job must exist and be claimed before execution", file=sys.stderr)
        return 2
    try:
        from .analysis import run
        run(args.job)
    except Exception as error:
        # Process boundary: keep traceback and an explicit failed job, never a successful fallback.
        log.exception("Analysis failed for %s", args.job)
        store.fail(args.job, f"{type(error).__name__}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
