"""Single entry point for the desktop app, web server, simulator and replay tools.

    anatele                    desktop application
    anatele web [--port N]     web UI (localhost)
    anatele simulate [...]     generate synthetic .tlog flights
    anatele replay LOG [...]   render where the drone went (PNG/GIF)
    anatele analyse LOG|ANT    print findings and hypotheses to the terminal
    anatele export LOG -o F.ant  analyse a log and save a shareable .ant analysis
"""
import sys


def analyse_into_temp(log, temp, then=None):
    from pathlib import Path

    from . import store
    from .analysis import run
    from .desktop_import import import_file

    store.DATA, store.DB = Path(temp), Path(temp) / "platform.db"
    store.initialize()
    job_id = import_file(log)
    store.claim()
    run(job_id)
    job = store.get_job(job_id)
    if then and job["status"] in ("complete", "partial"):
        then(job_id)
    return job


def analyse(argv):
    import argparse
    import tempfile
    from pathlib import Path

    parser = argparse.ArgumentParser(prog="anatele analyse")
    parser.add_argument("log", help="Flight log (.tlog/.bin/.log/.csv) or shared analysis (.ant)")
    parser.add_argument("--json", action="store_true", help="Print the full manifest as JSON")
    args = parser.parse_args(argv)
    if Path(args.log).suffix.lower() == ".ant":
        from .antfile import AntError, read_ant
        try:
            meta, m = read_ant(args.log)
        except AntError as error:
            print(f"Cannot open {args.log}: {error}", file=sys.stderr)
            return 1
        job = {"status": meta["status"], "manifest": m}
        print(f"Shared analysis created {meta.get('created_at', '?')[:19]} by {meta.get('created_by', '?')}; "
              f"original log {'included' if meta.get('original_included') else 'not included'}")
        label = meta["log"]["filename"]
    else:
        with tempfile.TemporaryDirectory() as temp:
            job = analyse_into_temp(args.log, temp)
        label = Path(args.log).name
    if job["status"] not in ("complete", "partial"):
        print(f"Analysis failed: {job['error']}", file=sys.stderr)
        return 1
    m = job["manifest"]
    if args.json:
        import json
        print(json.dumps(m, indent=1, default=str))
        return 0
    t0 = m["first_time_us"]
    print(f"{label}: {m['record_count']:,} records, status {job['status']}")
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


def export(argv):
    import argparse
    import tempfile
    from pathlib import Path

    from .antfile import export_ant

    parser = argparse.ArgumentParser(prog="anatele export", description="Analyse a log and save a shareable .ant file")
    parser.add_argument("log")
    parser.add_argument("-o", "--out", help="Output .ant path (default: next to the log)")
    parser.add_argument("--include-original", action="store_true",
                        help="Embed the original log (enables raw bytes and re-analysis; contains GPS positions)")
    args = parser.parse_args(argv)
    out = Path(args.out or Path(args.log).with_suffix(".ant")).resolve()
    with tempfile.TemporaryDirectory() as temp:
        job = analyse_into_temp(args.log, temp, lambda job_id: export_ant(job_id, out, args.include_original))
    if job["status"] not in ("complete", "partial"):
        print(f"Analysis failed: {job['error']}", file=sys.stderr)
        return 1
    print(f"Saved {out} ({out.stat().st_size / 1048576:.1f} MiB, original log "
          f"{'included' if args.include_original else 'not included'})")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--worker-job"]:
        from .worker import main as worker
        return worker(["--job", argv[1]])
    command = argv[0] if argv and not argv[0].startswith("-") and argv[0] in ("web", "simulate", "replay", "analyse", "export", "desktop") else "desktop"
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
    if command == "export":
        return export(rest)
    sys.argv = [sys.argv[0], *rest]
    from .desktop import main as desktop
    return desktop()


if __name__ == "__main__":
    sys.exit(main())
