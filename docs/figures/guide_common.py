"""Shared style and helpers for the AnaTele illustrated-guide figures."""
import functools
import sys
import tempfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "images" / "guide"

ACCENT = "#ff5722"
PALETTE = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9", "#D55E00", "#F0E442", "#000000"]
GREY = "#8a8a8a"

plt.rcParams.update({
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "savefig.facecolor": "white",
    "font.size": 12,
    "axes.titlesize": 14,
    "axes.titleweight": "bold",
    "axes.labelsize": 12,
    "legend.fontsize": 11,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.alpha": 0.25,
    "axes.prop_cycle": matplotlib.cycler(color=PALETTE),
})


def save(fig, name):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("wrote", path.relative_to(ROOT))
    return path


@functools.lru_cache(maxsize=None)
def sim_tlog(scenario, seed=1):
    """Write a deterministic simulated .tlog to a temp dir and return its path."""
    from telemetry_platform.simulator import Simulator

    folder = Path(tempfile.gettempdir()) / "anatele_guide_sims"
    folder.mkdir(exist_ok=True)
    target = folder / f"sim_{scenario}_{seed}.tlog"
    if not target.exists():
        target.write_bytes(Simulator(scenario, seed).run(240.0))
    return target


@functools.lru_cache(maxsize=None)
def messages(scenario, seed=1):
    """Return {msg_type: list of dicts} with a 't' key (seconds from first record) for a simulated flight."""
    from pymavlink import mavutil

    conn = mavutil.mavlink_connection(str(sim_tlog(scenario, seed)), dialect="ardupilotmega")
    out, t0 = {}, None
    while True:
        msg = conn.recv_match()
        if msg is None:
            break
        kind = msg.get_type()
        if kind == "BAD_DATA":
            continue
        stamp = getattr(msg, "_timestamp", 0.0)
        t0 = stamp if t0 is None else t0
        row = msg.to_dict()
        row["t"] = stamp - t0
        out.setdefault(kind, []).append(row)
    return out


@functools.lru_cache(maxsize=None)
def manifest(scenario, seed=1):
    """Run the real AnaTele analysis on a simulated flight and return its manifest."""
    import json

    cache = Path(tempfile.gettempdir()) / "anatele_guide_sims" / f"manifest_{scenario}_{seed}.json"
    if not cache.exists():
        from telemetry_platform.cli import analyse_into_temp

        with tempfile.TemporaryDirectory() as temp:
            job = analyse_into_temp(str(sim_tlog(scenario, seed)), temp)
        cache.write_text(json.dumps(job["manifest"], default=str), encoding="utf-8")
    return json.loads(cache.read_text(encoding="utf-8"))


def rel(m, us):
    return None if us is None else (us - m["first_time_us"]) / 1e6


def col(rows, key, scale=1.0):
    return [r["t"] for r in rows], [r[key] * scale for r in rows]
