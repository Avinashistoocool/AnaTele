"""Single entry point for the desktop app, web server, simulator and replay tools.

    anatele                    desktop application
    anatele web [--port N]     web UI (localhost)
    anatele simulate [...]     generate synthetic .tlog flights
    anatele replay LOG [...]   render where the drone went (PNG/GIF)
    anatele analyse LOG|ANT    print findings and hypotheses to the terminal
    anatele export LOG -o F.ant  analyse a log and save a shareable .ant analysis
    anatele preflight [...]    AnaTele Preflight: calibrations + checklist on a LIVE vehicle (docs/PREFLIGHT.md)
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


def preflight(argv):
    import argparse
    import time

    from .preflight.steps import STEP_IDS

    parser = argparse.ArgumentParser(
        prog="anatele preflight",
        description="AnaTele Preflight: calibrations and pre-flight checklist on a LIVE vehicle (separate from the "
                    "read-only analyser). Calibrations change vehicle state; the vehicle is never armed.")
    parser.add_argument("--connect", default="", help="COM3, /dev/ttyACM0, udpin:0.0.0.0:14550, tcp:host:port")
    parser.add_argument("--baud", type=int, default=115200, help="Serial baud rate (default 115200)")
    parser.add_argument("--simulate", action="store_true", help="Use the built-in simulated vehicle")
    parser.add_argument("--no-gui", action="store_true", help="Run in the terminal, prompting there for guided steps")
    parser.add_argument("--profile", help="Parameter profile JSON to compare against")
    parser.add_argument("--save-profile", metavar="FILE", help="Save the vehicle's current parameters as a profile and exit")
    parser.add_argument("--report-dir", help="Folder for the JSON + Markdown report")
    parser.add_argument("--skip", nargs="+", default=[], choices=STEP_IDS[1:], metavar="STEP",
                        help="Steps to skip: " + ", ".join(STEP_IDS[1:]))
    parser.add_argument("--props-removed", action="store_true",
                        help="Confirm ALL propellers are removed (enables the motor test; asked again in the terminal)")
    parser.add_argument("--motor-throttle", type=float, default=6.0, help="Motor test throttle percent (1-15, default 6)")
    args = parser.parse_args(argv)
    connect = "sim:" if args.simulate else args.connect
    if not args.no_gui and not args.save_profile:
        from .preflight.gui import main as gui
        return gui(connect, args.baud, args.profile, args.report_dir, args.skip)

    from pathlib import Path

    from .preflight import report
    from .preflight.link import Link, LinkError
    from .preflight.steps import ConsolePrompter, Options, Runner, load_profile, save_profile

    if not connect:
        parser.error("--connect is required in --no-gui mode (or use --simulate)")
    try:
        profile = load_profile(args.profile) if args.profile else None
        link = Link.open(connect, args.baud)
    except (OSError, ValueError, LinkError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    try:
        if args.save_profile:
            if link.wait_heartbeat(10) is None:
                print("No heartbeat from the vehicle.", file=sys.stderr)
                return 1
            saved = save_profile(link, args.save_profile, Path(args.save_profile).stem)
            print(f"Saved {len(saved['params'])} parameters to {args.save_profile}")
            return 0
        props = args.props_removed and "motors" not in args.skip
        if props:
            answer = input("Motor test: motors WILL SPIN. Type 'props off' to confirm ALL propellers are removed: ")
            props = answer.strip().lower() == "props off"
            if not props:
                print("Not confirmed: the motor test will be skipped.")
        options = Options(connect=connect, baud=args.baud, skip=set(args.skip), props_removed=props,
                          motor_throttle_pct=args.motor_throttle, profile=profile)

        def event(kind, **data):
            if kind == "step_started":
                print(f"\n[{data['index'] + 1}/8] {data['title']}")
            elif kind == "step_finished":
                print(f"    {data['result'].status}: {data['result'].message}")
            elif kind == "statustext":
                print(f"    vehicle: {data['text']}")
            elif kind == "log":
                print(f"    {data['text']}")
            elif kind == "progress" and data.get("text"):
                print(f"    {data['fraction'] * 100:3.0f}%  {data['text']}")

        started = time.time()
        runner = Runner(link, options, ConsolePrompter(), event)
        try:
            results = runner.run()
        except KeyboardInterrupt:
            runner.abort()
            print("\nAborted.")
            return 130
        built = report.build(results, runner.vehicle, options, started)
        folder = args.report_dir or (Path.home() / "AnaTele Preflight")
        json_path, md_path = report.write(built, folder)
        print(f"\nOverall: {built['overall']}\nReport: {json_path}\n        {md_path}")
        return 0 if built["overall"] in ("PASS", "WARN") else 1
    finally:
        link.close()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--worker-job"]:
        from .worker import main as worker
        return worker(["--job", argv[1]])
    command = argv[0] if argv and not argv[0].startswith("-") and argv[0] in ("web", "simulate", "replay", "analyse", "export", "preflight", "desktop") else "desktop"
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
    if command == "preflight":
        return preflight(rest)
    sys.argv = [sys.argv[0], *rest]
    from .desktop import main as desktop
    return desktop()


if __name__ == "__main__":
    sys.exit(main())
