"""Single entry point for the desktop app, web server, simulator and replay tools.

    anatele                    desktop application
    anatele web [--port N]     web UI (localhost)
    anatele simulate [...]     generate synthetic .tlog flights
    anatele replay LOG [...]   render where the drone went (PNG/GIF)
    anatele analyse LOG        print findings and hypotheses to the terminal
"""
import sys


def analyse(argv):
    import argparse
    import tempfile
    from pathlib import Path

    from . import store
    from .analysis import run
    from .desktop_import import import_file

    parser = argparse.ArgumentParser(prog="anatele analyse")
    parser.add_argument("log")
    parser.add_argument("--json", action="store_true", help="Print the full manifest as JSON")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as temp:
        store.DATA, store.DB = Path(temp), Path(temp) / "platform.db"
        store.initialize()
        job_id = import_file(args.log)
        store.claim()
        run(job_id)
        job = store.get_job(job_id)
    if job["status"] not in ("complete", "partial"):
        print(f"Analysis failed: {job['error']}", file=sys.stderr)
        return 1
    m = job["manifest"]
    if args.json:
        import json
        print(json.dumps(m, indent=1, default=str))
        return 0
    t0 = m["first_time_us"]
    print(f"{Path(args.log).name}: {m['record_count']:,} records, status {job['status']}")
    for source in m["sources"]:
        print(f"  source {source['key']:<10} {source['role']:<20} {source.get('semantic_pack') or ''}")
    for index, phase in enumerate(m.get("phases", [])):
        rel = lambda key: f"{(phase[key] - t0) / 1e6:.1f}s" if phase.get(key) else "-"
        print(f"  episode {index}: {phase['status']}, takeoff {rel('takeoff_us')}, crash report {rel('critical_event_us')}")
    print("\nFindings:")
    for f in m["findings"]:
        print(f"  [{f['severity']:<8}] {f['title']}")
    for incident in m.get("incidents", []):
        print(f"\nHypotheses for episode {incident['episode']} (screening order, not probability):")
        for rank, h in enumerate(incident["hypotheses"], 1):
            print(f"  {rank}. {h['title']} — {h['label']}")
    counts = {}
    for r in m.get("rule_results", []):
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    print("\nRule states:", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--worker-job"]:
        from .worker import main as worker
        return worker(["--job", argv[1]])
    command = argv[0] if argv and not argv[0].startswith("-") and argv[0] in ("web", "simulate", "replay", "analyse", "desktop") else "desktop"
    rest = argv[1:] if argv and argv[0] == command else argv
    if command == "web":
        from .server import main as web
        return web(rest)
    if command == "simulate":
        from .simulator import main as simulate
        return simulate(rest)
    if command == "replay":
        from .replay import main as replay
        return replay(rest)
    if command == "analyse":
        return analyse(rest)
    sys.argv = [sys.argv[0], *rest]
    from .desktop import main as desktop
    return desktop()


if __name__ == "__main__":
    sys.exit(main())
