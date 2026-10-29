"""Flight-path reconstruction and 3D replay of where the aircraft went.

Positions are the recorded *estimated* positions (GLOBAL_POSITION_INT / POS), converted to local
east/north/up metres relative to the first armed position, so screenshots do not reveal absolute
coordinates. Nothing is interpolated across gaps longer than five seconds.
"""
import argparse
import math
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from . import store

GAP_US = 5_000_000
STATUS_COLORS = {"reported_complete": "#7ad17a", "geometry_consistent_with_completion": "#c8e07a",
                 "completion_not_observed": "#e0c35a", "not_visited_in_observed_trajectory": "#e05a5a",
                 "unassessable": "#909090"}
MODE_COLORS = {"AUTO": "#5ab4e0", "RTL": "#e0c35a", "LAND": "#c07ae0", "LOITER": "#7ad17a", "STABILIZE": "#b0b0b0",
               "GUIDED": "#5ae0c8", "ALT_HOLD": "#e0915a", "POSHOLD": "#7ad1b0"}


@dataclass
class Track:
    t: list = field(default_factory=list)
    east: list = field(default_factory=list)
    north: list = field(default_factory=list)
    up: list = field(default_factory=list)
    ids: list = field(default_factory=list)
    roll: list = field(default_factory=list)
    pitch: list = field(default_factory=list)
    yaw: list = field(default_factory=list)
    mode: list = field(default_factory=list)
    origin: tuple = None
    waypoints: list = field(default_factory=list)
    markers: list = field(default_factory=list)
    episode: int = None
    notes: list = field(default_factory=list)

    def __len__(self):
        return len(self.t)


def local(lat, lon, origin):
    north = (lat - origin[0]) * 111320.0
    east = (lon - origin[1]) * 111320.0 * math.cos(math.radians(origin[0]))
    return east, north


def build_track(job, episode=None, source=None, max_points=6000):
    """Return a Track for one armed episode (default: first crash episode, else first airborne one)."""
    manifest = job["manifest"]
    phases = manifest.get("phases") or []
    episodes = manifest["armed_episodes"]
    if episode is None:
        episode = next((i for i, p in enumerate(phases) if p.get("critical_event_us")), None)
        if episode is None:
            episode = next((i for i, p in enumerate(phases) if p.get("takeoff_us")), 0 if episodes else None)
    track = Track(episode=episode)
    if source is None:
        source = episodes[episode]["source"] if episode is not None and episodes else next(
            (s["key"] for s in manifest["sources"] if s["role"] in ("vehicle", "onboard_log")), None)
    if source is None:
        track.notes.append("No vehicle source.")
        return track
    if episode is not None and episodes:
        ep = episodes[episode]
        start, end = ep["start_us"] - 2_000_000, (ep["end_us"] or manifest["last_time_us"]) + 3_000_000
        epoch = ep["epoch"]
    else:
        start, end, epoch = manifest["first_time_us"], manifest["last_time_us"], None
    path = store.DATA / "datasets" / job["id"] / "signals.parquet"
    names = ["position.latitude", "position.longitude", "position.altitude_home",
             "attitude.roll_deg", "attitude.pitch_deg", "attitude.yaw_deg"]
    with duckdb.connect() as db:
        rows = db.execute(
            f"SELECT name, t_us, value, id FROM read_parquet('{str(path).replace(chr(39), chr(39) * 2)}') "
            "WHERE source=? AND (? IS NULL OR epoch=?) AND t_us BETWEEN ? AND ? AND name IN (?,?,?,?,?,?) ORDER BY t_us, id",
            [source, epoch, epoch, start, end, *names]).fetchall()
    series = {name: [] for name in names}
    for name, t, value, rid in rows:
        series[name].append((t, value, rid))
    lat, lon, alt = series["position.latitude"], series["position.longitude"], series["position.altitude_home"]
    if not lat:
        track.notes.append("No recorded positions in this episode.")
        return track
    lon_by_id = {rid: value for _, value, rid in lon}
    alt_by_id = {rid: value for _, value, rid in alt}
    points = [(t, la, lon_by_id[rid], alt_by_id.get(rid), rid) for t, la, rid in lat
              if rid in lon_by_id and la and lon_by_id[rid]]
    if not points:
        track.notes.append("Positions lacked longitude pairs.")
        return track
    stride = max(1, len(points) // max_points)
    points = points[::stride]
    origin = points[0][1], points[0][2]
    track.origin = origin

    def nearest(values, t):
        import bisect
        times = [v[0] for v in values]
        i = bisect.bisect_left(times, t)
        best = min((j for j in (i - 1, i) if 0 <= j < len(values)), key=lambda j: abs(values[j][0] - t), default=None)
        return values[best][1] if best is not None and abs(values[best][0] - t) < 1_000_000 else math.nan

    modes = sorted((e["t_us"], e["text"].split(" ")[0]) for e in manifest["events"]
                   if e["kind"] == "MODE_CHANGE" and e["source"] == source)
    for t, la, lo, up, rid in points:
        e, n = local(la, lo, origin)
        track.t.append(t)
        track.east.append(e)
        track.north.append(n)
        track.up.append(up if up is not None else math.nan)
        track.ids.append(rid)
        track.roll.append(nearest(series["attitude.roll_deg"], t))
        track.pitch.append(nearest(series["attitude.pitch_deg"], t))
        track.yaw.append(nearest(series["attitude.yaw_deg"], t))
        current = [m for mt, m in modes if mt <= t]
        track.mode.append(current[-1] if current else "unknown")

    execution = next((e for e in manifest["missions"].get("execution", []) if e["episode"] == episode), None)
    items = {}
    for tx in manifest["missions"]["transactions"]:
        for seq, item in tx["items"].items():
            items[(tx["id"], seq)] = item
    if execution:
        for row in execution["items"]:
            item = next((i for (tid, seq), i in items.items() if int(seq) == row["seq"] and i["record_id"] == row["record_id"]), None)
            if not item or item.get("frame") not in (0, 3, 5, 6, 10, 11) or not item.get("x"):
                continue
            scale = 1e-7 if item.get("mavpackettype") == "MISSION_ITEM_INT" else 1.0
            e, n = local(item["x"] * scale, item["y"] * scale, origin)
            track.waypoints.append({"seq": row["seq"], "east": e, "north": n, "up": float(item.get("z") or 0),
                                    "status": row["status"], "command": row["command"]})
    phase = phases[episode] if episode is not None and episode < len(phases) else {}
    for key, label, color in (("takeoff_us", "takeoff", "#7ad17a"), ("loss_of_control_onset_us", "attitude excursion", "#e0915a"),
                              ("critical_event_us", "crash report", "#e05a5a"), ("disarmed_us", "disarmed", "#b0b0b0")):
        if phase.get(key):
            i = min(range(len(track.t)), key=lambda k: abs(track.t[k] - phase[key]))
            track.markers.append({"label": label, "index": i, "color": color})
    return track


def segments(track):
    """Split indices at gaps > 5 s so the path is not drawn across missing data."""
    current = []
    for i in range(len(track)):
        if current and track.t[i] - track.t[current[-1]] > GAP_US:
            yield current
            current = []
        current.append(i)
    if current:
        yield current


def body_arms(track, i, size):
    roll, pitch, yaw = (math.radians(v) if not math.isnan(v) else 0.0 for v in (track.roll[i], track.pitch[i], track.yaw[i]))
    cr, sr, cp, sp, cy, sy = math.cos(roll), math.sin(roll), math.cos(pitch), math.sin(pitch), math.cos(yaw), math.sin(yaw)

    def rotate(x_fwd, y_right):
        # body FRD -> NED, then to ENU for plotting
        n = cy * cp * x_fwd + (cy * sp * sr - sy * cr) * y_right
        e = sy * cp * x_fwd + (sy * sp * sr + cy * cr) * y_right
        d = -sp * x_fwd + cp * sr * y_right
        return e, n, -d

    arms = []
    for fwd, right in ((1, 1), (-1, -1), (1, -1), (-1, 1)):
        e, n, u = rotate(fwd * size * .7071, right * size * .7071)
        arms.append((track.east[i] + e, track.north[i] + n, track.up[i] + u))
    nose = rotate(size * 1.2, 0)
    return arms, (track.east[i] + nose[0], track.north[i] + nose[1], track.up[i] + nose[2])


def draw(ax, track, index=None, style=None):
    """Draw the 3D flight path on a matplotlib 3D axis; index places the aircraft and trims the trail."""
    style = style or {"fg": "#dcdcdc", "bg": "#1e1e1e"}
    ax.clear()
    ax.set_facecolor(style["bg"])
    for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
        axis.set_pane_color((0.12, 0.12, 0.12, 1.0))
        axis.label.set_color(style["fg"])
        axis._axinfo["grid"]["color"] = (0.3, 0.3, 0.3, 1)
    ax.tick_params(colors=style["fg"], labelsize=8)
    ax.set_xlabel("East (m)")
    ax.set_ylabel("North (m)")
    ax.set_zlabel("Up, home-relative (m)")
    if not len(track):
        ax.text2D(.5, .5, " ".join(track.notes) or "No track", transform=ax.transAxes, color="#909090", ha="center")
        return
    last = len(track) - 1 if index is None else index
    all_e = track.east + [w["east"] for w in track.waypoints]
    all_n = track.north + [w["north"] for w in track.waypoints]
    ups = [u for u in track.up if not math.isnan(u)] + [w["up"] for w in track.waypoints]
    span = max(max(all_e) - min(all_e), max(all_n) - min(all_n), 10) / 2
    ce, cn = (max(all_e) + min(all_e)) / 2, (max(all_n) + min(all_n)) / 2
    ax.set_xlim(ce - span, ce + span)
    ax.set_ylim(cn - span, cn + span)
    ax.set_zlim(min(0, min(ups)), max(5, max(ups) * 1.15))
    floor = min(0, min(ups))
    seen = set()
    for chunk in segments(track):
        chunk = [i for i in chunk if i <= last]
        for a, b in zip(chunk, chunk[1:]):
            mode = track.mode[b]
            color = MODE_COLORS.get(mode, "#dcdcdc")
            ax.plot(track.east[a:b + 1], track.north[a:b + 1], track.up[a:b + 1], color=color, linewidth=1.8,
                    label=mode if mode not in seen else None)
            seen.add(mode)
        if chunk:
            ax.plot([track.east[i] for i in chunk], [track.north[i] for i in chunk], [floor] * len(chunk),
                    color="#555555", linewidth=.8)
    for w in track.waypoints:
        color = STATUS_COLORS.get(w["status"], "#909090")
        ax.scatter([w["east"]], [w["north"]], [w["up"]], color=color, s=40, depthshade=False, edgecolor="#000000")
        ax.plot([w["east"], w["east"]], [w["north"], w["north"]], [floor, w["up"]], color=color, linewidth=.6, linestyle=":")
        ax.text(w["east"], w["north"], w["up"] + span * .04, f"{w['seq']}", color=color, fontsize=8)
    for m in track.markers:
        i = m["index"]
        if i <= last:
            ax.scatter([track.east[i]], [track.north[i]], [track.up[i]], color=m["color"], s=55, marker="X", depthshade=False)
            ax.text(track.east[i], track.north[i], track.up[i] + span * .06, m["label"], color=m["color"], fontsize=8)
    if index is not None and not math.isnan(track.up[last]):
        arms, nose = body_arms(track, last, max(1.5, span * .05))
        for k, (e, n, u) in enumerate(arms):
            ax.plot([track.east[last], e], [track.north[last], n], [track.up[last], u],
                    color="#e05a5a" if k < 2 else "#ffffff", linewidth=2.5)
        ax.plot([track.east[last], nose[0]], [track.north[last], nose[1]], [track.up[last], nose[2]], color="#e0c35a", linewidth=2)
    if seen:
        legend = ax.legend(loc="upper left", fontsize=7, facecolor="#262626", edgecolor="#555", labelcolor=style["fg"])
        legend.set_title("flight mode", prop={"size": 7})
        legend.get_title().set_color(style["fg"])


def render(track, output, animate=False, fps=12, seconds=12, title=None):
    from matplotlib.figure import Figure
    figure = Figure(figsize=(8, 6), facecolor="#1e1e1e")
    ax = figure.add_subplot(111, projection="3d")
    if title:
        figure.suptitle(title, color="#dcdcdc", fontsize=10)
    output = Path(output)
    if not animate:
        draw(ax, track)
        ax.view_init(elev=28, azim=-60)
        figure.savefig(output, dpi=130, facecolor=figure.get_facecolor())
        return output
    from matplotlib.animation import FuncAnimation, PillowWriter
    frames = max(2, min(len(track), fps * seconds))
    indices = [round(k * (len(track) - 1) / (frames - 1)) for k in range(frames)]

    def update(k):
        draw(ax, track, indices[k])
        ax.view_init(elev=28, azim=-60 + 60 * k / frames)
        t = (track.t[indices[k]] - track.t[0]) / 1e6
        ax.set_title(f"t = {t:5.1f} s · {track.mode[indices[k]]} · alt {track.up[indices[k]]:.1f} m", color="#dcdcdc", fontsize=9)
    animation = FuncAnimation(figure, update, frames=frames)
    animation.save(output, writer=PillowWriter(fps=fps), dpi=80, savefig_kwargs={"facecolor": figure.get_facecolor()})
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description="Analyse a log and render where the drone went (PNG or animated GIF).")
    parser.add_argument("log", help=".tlog / .bin / .log file")
    parser.add_argument("--out", default=None, help="Output .png or .gif (default: <log>.gif)")
    parser.add_argument("--episode", type=int, default=None, help="Armed episode index (default: crash or first flight)")
    parser.add_argument("--fps", type=int, default=12)
    parser.add_argument("--seconds", type=int, default=12)
    args = parser.parse_args(argv)
    log = Path(args.log)
    out = Path(args.out) if args.out else log.with_suffix(".gif")
    with tempfile.TemporaryDirectory() as temp:
        old = store.DATA, store.DB
        store.DATA, store.DB = Path(temp), Path(temp) / "platform.db"
        try:
            from .analysis import run
            from .desktop_import import import_file
            store.initialize()
            job_id = import_file(log)
            store.claim()
            run(job_id)
            job = store.get_job(job_id)
            if job["status"] not in ("complete", "partial"):
                raise SystemExit(f"Analysis failed: {job['error']}")
            track = build_track(job, args.episode)
            render(track, out, animate=out.suffix.lower() == ".gif", fps=args.fps, seconds=args.seconds,
                   title=f"{log.name} · episode {track.episode} (synthetic data is labelled in the log)")
        finally:
            store.DATA, store.DB = old
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
