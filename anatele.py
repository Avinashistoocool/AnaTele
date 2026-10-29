"""Launch AnaTele: python anatele.py [web|simulate|replay|analyse] ... (no argument = desktop app)."""
import sys

from telemetry_platform.cli import main

if __name__ == "__main__":
    sys.exit(main())
