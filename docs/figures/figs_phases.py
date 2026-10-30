"""Data figures 09-18: phases, tilt error and motors, from real simulated flights and AnaTele's own analysis."""
import math

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch

from guide_common import ACCENT, GREY, PALETTE, manifest, messages, rel, save
from telemetry_platform.derived import FRAME_LAYOUTS, PHASE_CONFIG, tilt_error_deg

MOTOR_NAMES = {1: "M1 front-right", 2: "M2 back-left", 3: "M3 front-left", 4: "M4 back-right"}


def phase_times(scenario):
    m = manifest(scenario)
    p = m["phases"][0]
    return {k: rel(m, p.get(k)) for k in ("armed_us", "takeoff_us", "airborne_end_us", "pre_event_end_us",
                                          "critical_event_us", "disarmed_us")}


def altitude(scenario):
    rows = messages(scenario)["GLOBAL_POSITION_INT"]
    return [r["t"] for r in rows], [r["relative_alt"] / 1000 for r in rows]


def tracking(scenario):
    data = messages(scenario)
    att, nav = data["ATTITUDE"], data["NAV_CONTROLLER_OUTPUT"]
    t, desired, actual, err = [], [], [], []
    for a, n in zip(att, nav):
        d = (n["nav_roll"], n["nav_pitch"])
        r = (math.degrees(a["roll"]), math.degrees(a["pitch"]))
        t.append(a["t"])
        desired.append(d)
        actual.append(r)
        err.append(tilt_error_deg(d, r))
    return t, desired, actual, err


def motors(scenario):
    rows = messages(scenario)["SERVO_OUTPUT_RAW"]
    return [r["t"] for r in rows], {k: [r[f"servo{k}_raw"] for r in rows] for k in range(1, 5)}


def longest_run(t, v, limit, max_gap=1.0):
    best, start, prev = (0.0, None, None), None, None
    for ti, vi in zip(t, v):
        if vi > limit and start is not None and prev is not None and ti - prev <= max_gap:
            if ti - start > best[0]:
                best = (ti - start, start, ti)
        elif vi > limit:
            start = ti
        else:
            start = None
        prev = ti
    return best


def _phase_shading(ax, p, ymax):
    ax.axvspan(p["armed_us"], p["takeoff_us"], color=PALETTE[1], alpha=0.15)
    ax.axvspan(p["takeoff_us"], p["airborne_end_us"], color=PALETTE[2], alpha=0.12)
    ax.axvspan(p["airborne_end_us"], p["disarmed_us"], color=PALETTE[3], alpha=0.15)
    for x, label in ((p["armed_us"], "armed"), (p["takeoff_us"], "takeoff"), (p["disarmed_us"], "disarmed")):
        ax.axvline(x, color="#555", ls=":", lw=1)
        ax.text(x, ymax, label, rotation=90, va="top", ha="right", fontsize=10, color="#333")


def fig09_altitude_phases():
    t, alt = altitude("nominal")
    p = phase_times("nominal")
    fig, ax = plt.subplots(figsize=(13, 5.5))
    ax.plot(t, alt, color=PALETTE[0], lw=2, label="height above home (GLOBAL_POSITION_INT)")
    _phase_shading(ax, p, max(alt) + 2)
    ax.text((p["armed_us"] + p["takeoff_us"]) / 2, -1.2, "on ground,\narmed", ha="center", fontsize=9)
    ax.text((p["takeoff_us"] + p["airborne_end_us"]) / 2, 1, "airborne (inferred)", ha="center", fontsize=11)
    ax.text((p["airborne_end_us"] + p["disarmed_us"]) / 2, -1.2, "landed", ha="center", fontsize=9)
    ax.set_ylim(-2, max(alt) + 2.5)
    ax.set_xlabel("time since log start (s)")
    ax.set_ylabel("altitude (m)")
    ax.set_title("Nominal flight: phases found by AnaTele from altitude and throttle")
    ax.legend(loc="upper right")
    return save(fig, "09_altitude_phases.png")


def fig10_takeoff_detection():
    t, alt = altitude("nominal")
    p = phase_times("nominal")
    lo, hi = p["armed_us"] - 1, p["takeoff_us"] + 5
    pairs = [(a, b) for a, b in zip(t, alt) if lo <= a <= hi]
    fig, ax = plt.subplots(figsize=(12, 5.5))
    ax.plot(*zip(*pairs), "o-", ms=3, color=PALETTE[0], label="altitude samples")
    base = PHASE_CONFIG["baseline_window_s"]
    ax.axvspan(p["armed_us"], p["armed_us"] + base, color=GREY, alpha=0.2)
    ax.text(p["armed_us"] + 0.1, 3.4, f"baseline\n(first {base:g} s armed)", fontsize=10)
    start, rise = PHASE_CONFIG["takeoff_start_m"], PHASE_CONFIG["takeoff_rise_m"]
    ax.axhline(start, color=PALETTE[2], ls="--")
    ax.axhline(rise, color=ACCENT, ls="--")
    ax.text(hi - 0.1, start + 0.08, f"start: {start:g} m above baseline", ha="right", color=PALETTE[2])
    ax.text(hi - 0.1, rise + 0.08, f"confirm: {rise:g} m above baseline", ha="right", color=ACCENT)
    cross = next(a for a, b in pairs if b > rise)
    ax.add_patch(FancyArrowPatch((cross, rise), (p["takeoff_us"], start), arrowstyle="-|>", mutation_scale=16,
                                 color="#333", connectionstyle="arc3,rad=0.3"))
    ax.plot([p["takeoff_us"]], [start], "o", ms=10, color=ACCENT)
    ax.text(p["takeoff_us"] - 0.15, start + 0.6, f"takeoff = T+0\n({p['takeoff_us']:.2f} s)", ha="right", fontsize=11)
    ax.set_xlim(lo, hi)
    ax.set_xlabel("time since log start (s)")
    ax.set_ylabel("altitude above armed baseline (m)")
    ax.set_title("Finding the takeoff: confirm at 1 m, then step back to the last sample at 0.2 m")
    ax.legend(loc="upper left")
    return save(fig, "10_takeoff_detection.png")


def fig11_upset_detection():
    t, _, actual, err = tracking("motor_failure")
    p = phase_times("motor_failure")
    fig, axes = plt.subplots(2, 1, figsize=(13, 7), sharex=True)
    axes[0].plot(t, [a[0] for a in actual], label="roll", color=PALETTE[0])
    axes[0].plot(t, [a[1] for a in actual], label="pitch", color=PALETTE[1])
    axes[0].set_ylabel("angle (deg)")
    axes[0].legend(loc="upper left")
    axes[1].plot(t, err, color=PALETTE[5], label="tilt tracking error")
    axes[1].axhline(30, color=GREY, ls="--")
    axes[1].set_ylabel("tilt error (deg)")
    axes[1].set_xlabel("time since log start (s)")
    for ax in axes:
        ax.axvspan(p["takeoff_us"], p["pre_event_end_us"], color=PALETTE[2], alpha=0.12)
        ax.axvline(p["critical_event_us"], color=ACCENT, lw=2)
        ax.set_xlim(p["armed_us"] - 2, p["disarmed_us"] + 3)
    guard = PHASE_CONFIG["event_guard_s"]
    axes[1].text(p["pre_event_end_us"] - 0.5, max(err) * 0.95,
                 f"pre-event window ends {guard:g} s\nbefore the crash report", ha="right", fontsize=10)
    axes[0].text(p["critical_event_us"] + 0.3, 0, "Crash: Disarming\nAngErr=31>30", color=ACCENT, fontsize=10)
    axes[0].set_title("Motor-failure flight: the pre-event window is what rules look at")
    return save(fig, "11_upset_detection.png")


def fig12_desired_vs_actual():
    t, desired, actual, _ = tracking("nominal")
    p = phase_times("nominal")
    fig, axes = plt.subplots(2, 1, figsize=(13, 7), sharex=True)
    for ax, i, name in ((axes[0], 0, "roll"), (axes[1], 1, "pitch")):
        ax.plot(t, [d[i] for d in desired], color=ACCENT, lw=2, label=f"desired {name} (what the autopilot asked for)")
        ax.plot(t, [a[i] for a in actual], color=PALETTE[0], lw=1.2, label=f"actual {name} (what happened)")
        ax.set_ylabel(f"{name} (deg)")
        ax.legend(loc="upper right", fontsize=10)
        ax.set_xlim(p["takeoff_us"], p["airborne_end_us"])
    axes[1].set_xlabel("time since log start (s)")
    axes[0].set_title("Nominal flight: the drone follows its commands closely")
    return save(fig, "12_desired_vs_actual.png")


def fig13_tilt_error_geometry():
    fig, ax = plt.subplots(figsize=(8, 7))
    ax.set_aspect("equal")
    ax.set_axis_off()
    desired, actual = (10.0, 0.0), (-25.0, 0.0)  # pure roll, so this 2D view (from behind) is exact
    err = tilt_error_deg(desired, actual)
    for (r, pch), color, label in ((desired, ACCENT, "desired"), (actual, PALETTE[0], "actual")):
        y = -math.sin(math.radians(r))
        z = math.cos(math.radians(pch)) * math.cos(math.radians(r))
        ax.add_patch(FancyArrowPatch((0, 0), (y * 3, z * 3), arrowstyle="-|>", mutation_scale=22, color=color, lw=3))
        ax.text(y * 3.25, z * 3.25, f"{label}\nroll {r:g}\u00b0, pitch {pch:g}\u00b0", ha="center", color=color, fontsize=11)
    ax.plot([-2.5, 2.5], [0, 0], color="#555", lw=6)
    ax.text(0, -0.5, "drone body", ha="center", color="#555")
    ax.text(0, 1.2, f"angle between them\n= {err:.1f}\u00b0", ha="center", fontsize=13, weight="bold")
    ax.set_xlim(-3.5, 3.5)
    ax.set_ylim(-1, 4)
    ax.set_title("Tilt error: the angle between where thrust should point and where it points")
    return save(fig, "13_tilt_error_geometry.png")


def fig14_tilt_error_timeseries():
    t, _, _, err = tracking("motor_failure")
    p = phase_times("motor_failure")
    window = [(a, b) for a, b in zip(t, err) if p["takeoff_us"] <= a <= p["pre_event_end_us"]]
    length, start, end = longest_run(*zip(*window), 30)
    fig, ax = plt.subplots(figsize=(13, 5.5))
    ax.plot(t, err, color=PALETTE[0], lw=1.3)
    ax.axhline(30, color=ACCENT, ls="--", lw=2)
    ax.text(p["armed_us"], 31.5, "30\u00b0 threshold", color=ACCENT)
    if start is not None:
        ax.axvspan(start, end, color=ACCENT, alpha=0.25)
        ax.text(end + 0.5, 40, f"longest run above 30\u00b0: {length:.1f} s\n(\u2265 0.5 s needed) \u2192 triggered",
                fontsize=11)
    peak = max(e for _, e in window)
    ax.axvline(p["pre_event_end_us"], color="#555", ls=":")
    ax.set_xlim(p["armed_us"], p["disarmed_us"] + 2)
    ax.set_xlabel("time since log start (s)")
    ax.set_ylabel("tilt tracking error (deg)")
    ax.set_title(f"Motor-failure flight: tracking loss (peak before the event {peak:.1f}\u00b0)")
    return save(fig, "14_tilt_error_timeseries.png")


def _motor_plot(scenario, title, name, highlight=False):
    t, outs = motors(scenario)
    p = phase_times(scenario)
    fig, ax = plt.subplots(figsize=(13, 5.5))
    for k in range(1, 5):
        ax.plot(t, outs[k], lw=1.3, label=MOTOR_NAMES[k], color=PALETTE[k - 1])
    if highlight:
        ax.axhline(1950, color=ACCENT, ls="--")
        ax.axhline(1150, color=GREY, ls="--")
        ax.text(p["armed_us"], 1960, "ceiling 1950 \u00b5s (spin max 0.95)", color=ACCENT)
        ax.text(p["armed_us"], 1160, "floor 1150 \u00b5s (spin min 0.15)", color="#555")
    ax.set_xlim(p["armed_us"] - 1, p["disarmed_us"] + 2)
    ax.set_xlabel("time since log start (s)")
    ax.set_ylabel("motor command (PWM \u00b5s)")
    ax.set_title(title)
    ax.legend(loc="upper left", ncol=4, fontsize=10)
    return fig, ax, t, outs, p


def fig15_motor_outputs_nominal():
    fig, ax, t, outs, p = _motor_plot("nominal", "Nominal flight: four motors work together near hover", "15")
    hover = [sum(outs[k][i] for k in outs) / 4 for i, x in enumerate(t) if p["takeoff_us"] + 3 < x < p["airborne_end_us"] - 3]
    ax.axhline(sum(hover) / len(hover), color="#333", ls=":")
    ax.text(p["disarmed_us"], sum(hover) / len(hover) + 15, f"mean in flight {sum(hover) / len(hover):.0f} \u00b5s",
            ha="right")
    return save(fig, "15_motor_outputs_nominal.png")


def fig16_motor_outputs_failure():
    fig, ax, t, outs, p = _motor_plot("motor_failure", "Motor-failure flight: M3 weakens, the others compensate",
                                      "16", highlight=True)
    from telemetry_platform.simulator import Simulator

    sim = Simulator("motor_failure", 1)
    sim.run(240.0)
    failure = sim.events["failure_t"]
    ax.axvline(failure, color=PALETTE[5], lw=2)
    ax.text(failure + 0.3, 1050, "M3 starts losing thrust\n(health 1 \u2212 0.06 \u00b7 \u0394t)", color=PALETTE[5])
    return save(fig, "16_motor_outputs_failure.png")


def fig17_mixer_matrix():
    _, layout = FRAME_LAYOUTS[(1, 1)]
    cols = ["throttle", "roll right", "pitch forward", "yaw (turn CW)"]
    rows = []
    for k in range(1, 5):
        x, y, spin = layout[k]
        rows.append([1, -x, -y, spin])
    fig, ax = plt.subplots(figsize=(10, 5.5))
    ax.imshow(rows, cmap="RdBu", vmin=-1.5, vmax=1.5)
    for i, row in enumerate(rows):
        for j, v in enumerate(row):
            ax.text(j, i, {1: "+ more", -1: "\u2212 less", 0: "0"}[v], ha="center", va="center", fontsize=13, weight="bold")
    ax.set_xticks(range(4))
    ax.set_xticklabels(cols)
    ax.set_yticks(range(4))
    ax.set_yticklabels([MOTOR_NAMES[k] for k in range(1, 5)])
    ax.grid(False)
    ax.set_title("The mixer: how each command changes each motor (QUAD/X)")
    fig.text(0.5, 0.01, "To roll right, the left motors push harder. To yaw clockwise, the CCW props speed up.",
             ha="center", fontsize=11)
    return save(fig, "17_mixer_matrix.png")


def fig18_saturation_fraction():
    t, outs = motors("motor_failure")
    p = phase_times("motor_failure")
    idx = [i for i, x in enumerate(t) if p["takeoff_us"] <= x <= p["pre_event_end_us"]]
    ceiling, floor = 1950 - 2, 1150 + 2
    fig, ax = plt.subplots(figsize=(11, 5.5))
    width = 0.38
    for k in range(1, 5):
        c = sum(outs[k][i] >= ceiling for i in idx) / len(idx)
        f = sum(outs[k][i] <= floor for i in idx) / len(idx)
        ax.bar(k - width / 2, c * 100, width, color=ACCENT, label="at ceiling" if k == 1 else None)
        ax.bar(k + width / 2, f * 100, width, color=PALETTE[0], label="at floor" if k == 1 else None)
        ax.text(k - width / 2, c * 100 + 1, f"{c * 100:.1f}%", ha="center", fontsize=10)
        ax.text(k + width / 2, f * 100 + 1, f"{f * 100:.1f}%", ha="center", fontsize=10)
    ax.set_xticks(range(1, 5))
    ax.set_xticklabels([MOTOR_NAMES[k] for k in range(1, 5)])
    ax.set_ylabel("% of airborne samples before the event")
    ax.set_title("Motor-failure flight: how often each motor hit its limit")
    ax.legend()
    ax.text(0.99, 0.95, "Rules ask for a *run*: at ceiling for \u2265 1 s\nor at floor for \u2265 2 s.",
            transform=ax.transAxes, ha="right", va="top", fontsize=10)
    return save(fig, "18_saturation_fraction.png")


FIGURES = [(f.__name__[3:], f) for f in (fig09_altitude_phases, fig10_takeoff_detection, fig11_upset_detection,
                                          fig12_desired_vs_actual, fig13_tilt_error_geometry,
                                          fig14_tilt_error_timeseries, fig15_motor_outputs_nominal,
                                          fig16_motor_outputs_failure, fig17_mixer_matrix, fig18_saturation_fraction)]
