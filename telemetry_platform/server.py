"""Web server launcher. Binds to localhost unless explicitly told otherwise (e.g. inside a container)."""
import argparse
import os
import socket
import sys

import uvicorn

from . import store


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the AnaTele web UI.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("ANATELE_PORT", 8766)))
    parser.add_argument("--host", default=os.environ.get("ANATELE_HOST", "127.0.0.1"),
                        help="Bind address. Anything other than 127.0.0.1 exposes an unauthenticated service; "
                             "only do this inside a container or behind an authenticating reverse proxy.")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding to {args.host}. AnaTele has no login; keep it on a private network "
              "or behind an authenticating proxy.", file=sys.stderr, flush=True)
    store.initialize()
    with (store.DATA / "server.lock").open("a+b") as lock:
        lock.seek(0)
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                parser.error("AnaTele is already running with this data directory")
        else:
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                parser.error("AnaTele is already running with this data directory")
        family = socket.AF_INET6 if ":" in args.host else socket.AF_INET
        with socket.socket(family) as probe:
            try:
                probe.bind((args.host, args.port))
            except OSError:
                parser.error(f"Port {args.port} is in use on {args.host}; choose a different --port")
        print(f"AnaTele web UI: http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port} "
              f"(data: {store.DATA})", flush=True)
        uvicorn.run("telemetry_platform.api:app", host=args.host, port=args.port,
                    log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
