"""Figures 25-31: missions, geometry, replay, simulator and the .ant file."""
import math

import matplotlib.pyplot as plt

from figs_basics import _arrow, _blank, _box
from figs_phases import altitude, phase_times, tracking
from guide_common import ACCENT, GREY, PALETTE, manifest, messages, save
from telemetry_platform.simulator import HOME

R = 6371008.8
STATUS_COLOURS = {"reported_complete": PALETTE[2], "geometry_consistent_with_completion": PALETTE[0],
                  "completion_not_observed": GREY, "not_visited_in_observed_trajectory": PALETTE[5]}


def local(lat, lon):
    n = math.radians(lat - HOME[0]) * R
    e = math.radians(lon - HOME[1]) * R * math.cos(math.radians(HOME[0]))
    return e, n


def mission_items(scenario):
    m = manifest(scenario)
    items = m["missions"]["transactions"][0]["items"]
    status = {i["seq"]: i for i in m["missions"]["execution"][0]["items"]}
    out = []
    for seq, item in sorted(items.items(), key=lambda kv: int(kv[0])):
        seq = int(seq)
        if seq == 0 or item["command"] != 16:
            continue
        e, n = local(item["x"] / 1e7, item["y"] / 1e7)
        out.append((seq, e, n, item["z"], status.get(seq, {})))
    return out


def fig25_mission_map():
    fig, axes = plt.subplots(1, 2, figsize=(14, 6.5))
    for ax, scenario in zip(axes, ("nominal", "motor_failure")):
        rows = messages(scenario)["GLOBAL_POSITION_INT"]
        path = [local(r["lat"] / 1e7, r["lon"] / 1e7) for r in rows if r["relative_alt"] > 200]
        ax.plot(*zip(*path), color=PALETTE[0], lw=2, label="flown path (height > 0.2 m)")
        ax.plot(0, 0, "s", ms=12, color="#333", label="home / launch")
        for seq, e, n, _, st in mission_items(scenario):
            colour = STATUS_COLOURS.get(st.get("status"), GREY)
            ax.add_patch(plt.Circle((e, n), st.get("radius_m", 2.0), color=colour, alpha=0.25))
            ax.plot(e, n, "o", ms=10, color=colour)
            ax.text(e + 1.5, n + 1.5, f"WP{seq}", fontsize=11, weight="bold")
        ax.plot(*path[-1], "X", ms=14, color=ACCENT, label="last airborne position")
        ax.set_aspect("equal")
        ax.set_xlabel("east of home (m)")
        ax.set_ylabel("north of home (m)")
        ax.set_title(scenario)
        ax.legend(loc="lower right", fontsize=9)
    fig.suptitle("Where the drone went: path, waypoints and their 2 m acceptance circles", weight="bold")
    fig.text(0.5, 0.01, "Green: autopilot reported the waypoint reached.  Blue: path passed inside the circle but "
             "no report was seen.", ha="center", fontsize=10)
    return save(fig, "25_mission_map.png")


def fig26_haversine():
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    ax = axes[0]
    _blank(ax)
    ax.add_patch(plt.Circle((0, 0), 1, fc="#eef4fa", ec="#333"))
    a, b = math.radians(60), math.radians(20)
    pa, pb = (math.cos(a), math.sin(a)), (math.cos(b), math.sin(b))
    arc = [(math.cos(t), math.sin(t)) for t in [b + (a - b) * i / 40 for i in range(41)]]
    ax.plot(*zip(*arc), color=ACCENT, lw=3, label="along the surface (haversine)")
    ax.plot(*zip(pa, pb), color=PALETTE[0], ls="--", lw=2, label="straight line through the Earth")
    ax.plot(*zip((0, 0), pa), color=GREY)
    ax.plot(*zip((0, 0), pb), color=GREY)
    ax.text(0.15, 0.2, "\u0394\u03c3", fontsize=14)
    ax.plot(*pa, "o", color="#333")
    ax.plot(*pb, "o", color="#333")
    ax.set_xlim(-1.2, 1.3)
    ax.set_ylim(-1.2, 1.3)
    ax.legend(loc="lower left", fontsize=10)
    ax.set_title("Distance on a sphere: d = R \u00b7 \u0394\u03c3")
    ax = axes[1]
    dists = [10 ** (i / 10) for i in range(0, 61)]
    errors = []
    for d in dists:
        lat2 = HOME[0] + math.degrees(d / R) * 0.6
        lon2 = HOME[1] + math.degrees(d / R) * 0.8 / math.cos(math.radians(HOME[0]))
        p1, p2 = math.radians(HOME[0]), math.radians(lat2)
        dl = math.radians(lon2 - HOME[1])
        h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        hav = 2 * R * math.asin(math.sqrt(h))
        flat = math.hypot((p2 - p1) * R, dl * R * math.cos(p1))
        errors.append(abs(hav - flat))
    ax.loglog(dists, [max(e, 1e-9) for e in errors], color=PALETTE[0], lw=2)
    ax.axhline(0.01, color=GREY, ls=":")
    ax.text(1.2, 0.012, "1 cm", color="#555")
    ax.axvspan(1, 2000, color=ACCENT, alpha=0.08)
    ax.text(1.5, max(errors) / 3, "typical small-drone\nmissions", color=ACCENT)
    ax.set_xlabel("distance between points (m)")
    ax.set_ylabel("|haversine \u2212 flat-earth| (m)")
    ax.set_title(f"Flat-earth error near lat {HOME[0]:.1f}\u00b0 (R = {R:,.1f} m)")
    return save(fig, "26_haversine.png")


def fig27_altitude_profile_mission():
    t, alt = altitude("nominal")
    reached = [(r["t"], r["seq"]) for r in messages("nominal")["MISSION_ITEM_REACHED"]]
    p = phase_times("nominal")
    fig, ax = plt.subplots(figsize=(13, 5.5))
    ax.plot(t, alt, color=PALETTE[0], lw=2)
    targets = {seq: z for seq, _, _, z, _ in mission_items("nominal")}
    for when, seq in reached:
        ax.axvline(when, color=PALETTE[2], ls=":")
        ax.text(when, max(alt) + 1.2, f"WP{seq} reached", rotation=90, va="top", ha="right", fontsize=9)
        if seq in targets:
            ax.plot(when, targets[seq], "o", color=ACCENT, ms=8)
    ax.plot([], [], "o", color=ACCENT, label="planned altitude of that waypoint")
    ax.set_xlim(p["armed_us"] - 2, p["disarmed_us"] + 2)
    ax.set_xlabel("time since log start (s)")
    ax.set_ylabel("height above home (m)")
    ax.set_title("Nominal mission: altitude with the moments each waypoint was reported reached")
    ax.legend(loc="lower center")
    return save(fig, "27_altitude_profile_mission.png")


def fig28_downsampling():
    t, _, actual, _ = tracking("motor_failure")
    roll = [a[0] for a in actual]
    max_points = 60
    stride = max(1, len(t) // max_points)
    fig, ax = plt.subplots(figsize=(13, 5.5))
    ax.plot(t, roll, color=GREY, lw=1, label=f"all samples ({len(t)})")
    ax.plot(t[::stride], roll[::stride], "o-", ms=4, color=ACCENT, lw=1.5,
            label=f"every {stride}th sample ({len(t[::stride])}) for drawing")
    ax.set_xlabel("time since log start (s)")
    ax.set_ylabel("roll (deg)")
    ax.set_title("Thinning a track for the 3D replay (stride = points \u00f7 max points)")
    ax.legend(loc="upper left")
    ax.text(0.99, 0.03, "Only the picture is thinned. Rules and statistics always use every sample.",
            transform=ax.transAxes, ha="right", fontsize=11, color=ACCENT)
    return save(fig, "28_downsampling.png")


def fig29_simulator_block():
    fig, ax = plt.subplots(figsize=(15, 5.5))
    _blank(ax)
    ax.set_xlim(0, 15)
    ax.set_ylim(0, 5.5)
    blocks = ["Mission\nwaypoints", "Guidance\n(where to go)", "Attitude\ncontrol", "Mixer\n(4 motors)",
              "Physics\n1.6 kg, 10 ms steps", "Sensors +\nMAVLink", ".tlog"]
    for i, b in enumerate(blocks):
        x = 0.15 + i * 2.12
        _box(ax, x, 2.4, 1.85, 1.4, b, color="#fff3ee" if i == 6 else "#f4f4f4", size=10)
        if i < len(blocks) - 1:
            _arrow(ax, (x + 1.86, 3.1), (x + 2.12, 3.1))
    _arrow(ax, (0.15 + 4 * 2.12 + 0.9, 2.4), (0.15 + 1 * 2.12 + 0.9, 2.4), style="-|>",
           connectionstyle="arc3,rad=0.35", color=PALETTE[0])
    ax.text(5.9, 0.7, "feedback: position, velocity, angles", color=PALETTE[0], ha="center")
    faults = ["motor_failure: M3 thrust fades", "compass_interference: heading error",
              "vibration: damaged prop noise", "radio_failsafe: RC signal lost"]
    for i, f in enumerate(faults):
        ax.text(0.2 + i * 3.7, 4.7, "\u26a0 " + f, fontsize=10, color=PALETTE[5])
    ax.set_title("The built-in simulator: a small physics model flying a real mission, with injectable faults")
    return save(fig, "29_simulator_block.png")


def fig30_scenarios_overview():
    fig, axes = plt.subplots(1, 5, figsize=(18, 4.2))
    specs = [("nominal", "altitude (m)", lambda d: [(r["t"], r["relative_alt"] / 1000) for r in d["GLOBAL_POSITION_INT"]]),
             ("motor_failure", "M3 command (\u00b5s)", lambda d: [(r["t"], r["servo3_raw"]) for r in d["SERVO_OUTPUT_RAW"]]),
             ("compass_interference", "EKF compass ratio", lambda d: [(r["t"], r["compass_variance"]) for r in d["EKF_STATUS_REPORT"]]),
             ("vibration", "vibration x (m/s\u00b2)", lambda d: [(r["t"], r["vibration_x"]) for r in d["VIBRATION"]]),
             ("radio_failsafe", "RC throttle (\u00b5s)", lambda d: [(r["t"], r["chan3_raw"]) for r in d["RC_CHANNELS"]])]
    for ax, (scenario, label, fn) in zip(axes, specs):
        pts = fn(messages(scenario))
        ax.plot(*zip(*pts), color=ACCENT if scenario != "nominal" else PALETTE[0], lw=1.2)
        ax.set_title(scenario, fontsize=12)
        ax.set_ylabel(label, fontsize=10)
        ax.set_xlabel("time (s)", fontsize=10)
        ax.tick_params(labelsize=9)
    fig.suptitle("The five simulated flights, each with the signal that tells its story", weight="bold")
    fig.tight_layout()
    return save(fig, "30_scenarios_overview.png")


def fig31_ant_file():
    fig, ax = plt.subplots(figsize=(13, 6.5))
    _blank(ax)
    ax.set_xlim(0, 13)
    ax.set_ylim(0, 7)
    ax.add_patch(plt.Rectangle((0.3, 0.3), 7.4, 6.2, fc="#fafafa", ec=ACCENT, lw=2.5))
    ax.text(0.5, 6.15, "flight.ant  (a ZIP file)", weight="bold", fontsize=13, color=ACCENT)
    entries = [("mimetype", "application/vnd.anatele.analysis+zip"),
               ("meta.json", "versions + SHA-256 of every other entry"),
               ("manifest.json", "findings, rule states, hypotheses, phases, missions"),
               ("observations.parquet", "every decoded record (evidence viewer)"),
               ("signals.parquet", "normalised time series"),
               ("report.html", "printable report, opens in any browser"),
               ("original.<ext>", "optional: the untouched log (has GPS!)")]
    for i, (name, desc) in enumerate(entries):
        y = 5.3 - i * 0.72
        _box(ax, 0.6, y, 2.6, 0.55, name, color="#fff3ee" if i == 1 else ("#eeeeee" if i == 6 else "white"), size=10)
        ax.text(3.35, y + 0.27, desc, va="center", fontsize=10)
    _box(ax, 8.5, 4.2, 4.2, 1.6, "Opening an .ant:\n1. check mimetype\n2. check sizes and SHA-256\n3. check JSON / Parquet shape",
         color="#eaf6ef", size=10)
    _box(ax, 8.5, 1.6, 4.2, 1.6, "Any change to any entry\n\u2192 hash mismatch\n\u2192 file rejected",
         color="#fdecea", size=10)
    _arrow(ax, (7.7, 4.9), (8.5, 4.9))
    _arrow(ax, (10.6, 4.2), (10.6, 3.2))
    return save(fig, "31_ant_file.png")


FIGURES = [(f.__name__[3:], f) for f in (fig25_mission_map, fig26_haversine, fig27_altitude_profile_mission,
                                          fig28_downsampling, fig29_simulator_block, fig30_scenarios_overview,
                                          fig31_ant_file)]
