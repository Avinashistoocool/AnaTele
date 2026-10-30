"""Concept figures 01-08: the drone, logs, time and the pipeline."""
import math

import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyArrowPatch, FancyBboxPatch

from guide_common import ACCENT, GREY, PALETTE, messages, save
from telemetry_platform.derived import FRAME_LAYOUTS


def _box(ax, x, y, w, h, text, color="#f4f4f4", edge="#444", size=11, weight="normal"):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                                fc=color, ec=edge, lw=1.4))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=size, weight=weight, wrap=True)


def _arrow(ax, a, b, color="#444", style="-|>", lw=1.6, **kw):
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle=style, mutation_scale=16, color=color, lw=lw, **kw))


def _blank(ax):
    ax.set_axis_off()
    ax.set_aspect("equal")


def fig01_quad_layout():
    _, layout = FRAME_LAYOUTS[(1, 1)]
    fig, ax = plt.subplots(figsize=(8, 8))
    _blank(ax)
    for motor, (x, y, spin) in layout.items():
        ax.plot([0, x], [0, y], color="#555", lw=6, solid_capstyle="round", zorder=1)
        ax.add_patch(Circle((x, y), 0.42, fc="#fff3ee" if y > 0 else "#eef4fa", ec="#333", lw=1.5, zorder=2))
        ax.text(x, y + 0.05, f"M{motor}", ha="center", va="center", fontsize=18, weight="bold", zorder=3)
        direction = "CCW" if spin > 0 else "CW"
        ax.text(x, y - 0.2, direction, ha="center", va="center", fontsize=11, color=PALETTE[0] if spin > 0 else PALETTE[5], zorder=3)
        theta = (40, 320) if spin > 0 else (320, 40)
        arc = FancyArrowPatch((x + 0.5 * math.cos(math.radians(theta[0])), y + 0.5 * math.sin(math.radians(theta[0]))),
                              (x + 0.5 * math.cos(math.radians(theta[1])), y + 0.5 * math.sin(math.radians(theta[1]))),
                              connectionstyle=f"arc3,rad={0.9 if spin > 0 else -0.9}", arrowstyle="-|>",
                              mutation_scale=14, color=PALETTE[0] if spin > 0 else PALETTE[5], lw=1.4, zorder=2)
        ax.add_patch(arc)
    ax.add_patch(FancyBboxPatch((-0.35, -0.25), 0.7, 0.5, boxstyle="round,pad=0.02", fc="#333", ec="#333", zorder=3))
    ax.text(0, 0, "flight\ncontroller", color="white", ha="center", va="center", fontsize=9, zorder=4)
    _arrow(ax, (0, 0.35), (0, 1.0), color=ACCENT, lw=3)
    ax.text(0, 1.12, "FRONT", ha="center", color=ACCENT, weight="bold", fontsize=13)
    ax.set_xlim(-1.8, 1.8)
    ax.set_ylim(-1.7, 1.7)
    ax.set_title("A QUAD/X quadcopter seen from above (ArduPilot motor numbers)")
    ax.text(0, -1.62, "Diagonal pairs spin the same way (M1+M2 CCW, M3+M4 CW) so their twisting forces cancel.",
            ha="center", fontsize=11, color="#333")
    return save(fig, "01_quad_layout.png")


def fig02_roll_pitch_yaw():
    fig, axes = plt.subplots(1, 3, figsize=(14, 5))
    titles = ["Roll: tilt left / right", "Pitch: nose up / down", "Yaw: turn left / right"]
    for ax, title, kind in zip(axes, titles, ("roll", "pitch", "yaw")):
        _blank(ax)
        ax.set_xlim(-1.6, 1.6)
        ax.set_ylim(-1.3, 1.5)
        ax.set_title(title)
        if kind == "yaw":
            ax.plot([-1, 1], [-1, 1], color="#555", lw=5)
            ax.plot([-1, 1], [1, -1], color="#555", lw=5)
            for x, y in ((1, 1), (-1, -1), (-1, 1), (1, -1)):
                ax.add_patch(Circle((x, y), 0.25, fc="white", ec="#333"))
            ax.add_patch(FancyArrowPatch((0.7, 0.0), (-0.7, 0.0), connectionstyle="arc3,rad=-0.9",
                                         arrowstyle="-|>", mutation_scale=20, color=ACCENT, lw=2.5))
            ax.text(0, -1.25, "Top view. Axis points straight up\nthrough the centre.", ha="center", fontsize=10)
        else:
            angle = math.radians(18)
            dx, dy = math.cos(angle), math.sin(angle)
            ax.plot([-1.2 * dx, 1.2 * dx], [-1.2 * dy, 1.2 * dy], color="#555", lw=6, solid_capstyle="round")
            ax.plot([-1.2, 1.2], [0, 0], color=GREY, lw=1, ls="--")
            for s in (-1, 1):
                ax.plot([s * 1.2 * dx - 0.3, s * 1.2 * dx + 0.3], [s * 1.2 * dy + 0.12] * 2, color="#333", lw=3)
            ax.add_patch(FancyArrowPatch((1.0, -0.15), (0.95, 0.45), connectionstyle="arc3,rad=0.3",
                                         arrowstyle="-|>", mutation_scale=18, color=ACCENT, lw=2.5))
            ax.text(0.2, -0.45, "18\u00b0", fontsize=12, color=ACCENT)
            view = "Seen from behind" if kind == "roll" else "Seen from the side (nose on the right)"
            ax.text(0, -1.25, view + ".\nThe dashed line is level.", ha="center", fontsize=10)
    fig.suptitle("The three rotation angles every log records", weight="bold", fontsize=15)
    return save(fig, "02_roll_pitch_yaw.png")


def fig03_thrust_balance():
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.5), gridspec_kw={"width_ratios": [1, 1.3]})
    ax = axes[0]
    _blank(ax)
    ax.set_xlim(-2, 2)
    ax.set_ylim(-2.2, 2.2)
    ax.plot([-1.3, 1.3], [0, 0], color="#555", lw=6)
    for x in (-1.1, -0.4, 0.4, 1.1):
        _arrow(ax, (x, 0.1), (x, 0.9), color=PALETTE[0], lw=2.5)
    ax.text(0, 1.15, "thrust from 4 motors", ha="center", color=PALETTE[0])
    _arrow(ax, (0, -0.1), (0, -1.6), color=PALETTE[5], lw=3)
    ax.text(0.1, -1.9, "weight = m g = 1.6 kg \u00d7 9.81 = 15.7 N", ha="center", color=PALETTE[5])
    ax.set_title("Hover: total thrust = weight")
    ax = axes[1]
    throttle = [i / 100 for i in range(0, 101)]
    hover = 0.38
    ax.plot([t * 100 for t in throttle], [t / hover for t in throttle], color=PALETTE[0], lw=2.5,
            label="thrust \u00f7 weight (simulator, linear)")
    ax.axhline(1, color=GREY, ls="--")
    ax.axvline(hover * 100, color=ACCENT, lw=2)
    ax.axvspan(0, 15, color=GREY, alpha=0.15)
    ax.axvspan(95, 100, color=GREY, alpha=0.15)
    ax.text(hover * 100 + 1, 2.3, "hover throttle\nMOT_THST_HOVER = 0.38", color=ACCENT)
    ax.text(1, 2.45, "below\nspin min\n0.15", fontsize=9, color="#555")
    ax.text(95.5, 0.2, "above\nspin\nmax\n0.95", fontsize=9, color="#555")
    ax.text(55, 0.6, "climb when thrust > weight", fontsize=10)
    ax.set_xlabel("motor output (% of range)")
    ax.set_ylabel("thrust / weight")
    ax.set_xlim(0, 100)
    ax.set_title("More output = more thrust")
    ax.legend(loc="upper left")
    return save(fig, "03_thrust_balance.png")


def fig04_what_is_a_log():
    fig, ax = plt.subplots(figsize=(14, 5.5))
    _blank(ax)
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 6)
    sensors = ["Gyroscope", "Accelerometer", "Barometer", "Compass", "GPS", "RC receiver"]
    for i, name in enumerate(sensors):
        _box(ax, 0.2, 5.0 - i * 0.9, 2.4, 0.7, name, color="#eef4fa")
        _arrow(ax, (2.6, 5.35 - i * 0.9), (4.0, 3.0))
    _box(ax, 4.0, 2.0, 2.8, 2.0, "Flight controller\n(ArduPilot)\nestimates, decides,\ndrives motors",
         color="#fff3ee", edge=ACCENT, weight="bold")
    _box(ax, 7.8, 3.6, 2.8, 1.3, "MAVLink telemetry\n(radio to the ground)", color="#f4f4f4")
    _box(ax, 7.8, 1.1, 2.8, 1.3, "DataFlash log\n(SD card on board)", color="#f4f4f4")
    _arrow(ax, (6.8, 3.4), (7.8, 4.2))
    _arrow(ax, (6.8, 2.6), (7.8, 1.8))
    _box(ax, 11.3, 3.6, 2.5, 1.3, ".tlog\n(Mission Planner)", color="#eaf6ef")
    _box(ax, 11.3, 1.1, 2.5, 1.3, ".bin / .log", color="#eaf6ef")
    _arrow(ax, (10.6, 4.25), (11.3, 4.25))
    _arrow(ax, (10.6, 1.75), (11.3, 1.75))
    ax.text(12.55, 0.45, "AnaTele reads both", ha="center", color=ACCENT, weight="bold")
    ax.set_title("Where a flight log comes from")
    return save(fig, "04_what_is_a_log.png")


def fig05_message_stream():
    data = messages("motor_failure")
    kinds = ["ATTITUDE", "GLOBAL_POSITION_INT", "SERVO_OUTPUT_RAW", "RC_CHANNELS", "VIBRATION",
             "EKF_STATUS_REPORT", "GPS_RAW_INT", "HEARTBEAT", "STATUSTEXT", "MISSION_ITEM_REACHED"]
    fig, axes = plt.subplots(2, 1, figsize=(14, 7.5), gridspec_kw={"height_ratios": [3, 1.3]})
    for ax, (lo, hi) in zip(axes, ((0, 80), (30, 32))):
        for row, kind in enumerate(kinds):
            times = [m["t"] for m in data.get(kind, []) if lo <= m["t"] <= hi]
            ax.vlines(times, row - 0.38, row + 0.38, color=PALETTE[row % len(PALETTE)], lw=0.8 if hi - lo > 10 else 2)
        ax.set_yticks(range(len(kinds)))
        ax.set_yticklabels(kinds, fontsize=10)
        ax.set_xlim(lo, hi)
        ax.invert_yaxis()
        ax.grid(axis="y", alpha=0)
    axes[0].set_title("A log is a stream of typed messages (simulated motor-failure flight)")
    axes[0].set_xlabel("time since log start (s)")
    axes[1].set_xlabel("zoom: 30 s to 32 s; each tick is one message")
    for t, text in ((20.0, "arm"), (71.4, "crash disarm")):
        axes[0].axvline(t, color=ACCENT, ls="--")
        axes[0].text(t + 0.5, -0.6, text, color=ACCENT)
    fig.tight_layout()
    return save(fig, "05_message_stream.png")


def fig06_sample_rates():
    data = messages("nominal")
    span = max(m["t"] for rows in data.values() for m in rows)
    rates = sorted(((k, len(v) / span) for k, v in data.items() if len(v) > 5), key=lambda kv: -kv[1])
    fig, ax = plt.subplots(figsize=(12, 6))
    names, values = zip(*rates)
    bars = ax.barh(names, values, color=[ACCENT if v >= 9 else PALETTE[0] for v in values])
    for bar, v in zip(bars, values):
        ax.text(v + 0.1, bar.get_y() + bar.get_height() / 2, f"{v:.1f} Hz", va="center", fontsize=10)
    ax.invert_yaxis()
    ax.set_xlabel("messages per second (Hz)")
    ax.set_title(f"Message rates in the nominal simulated .tlog ({span:.0f} s)")
    ax.text(0.98, 0.04, "Fast streams (orange) show attitude and motors;\nslow ones (blue) show health and status.",
            transform=ax.transAxes, ha="right", fontsize=10, color="#333")
    return save(fig, "06_sample_rates.png")


def fig07_time_bases():
    fig, ax = plt.subplots(figsize=(14, 6))
    ax.set_xlim(-5, 105)
    ax.set_ylim(-0.5, 4.2)
    ax.set_yticks([0.5, 1.7, 2.9])
    ax.set_yticklabels(["epoch\n(AnaTele)", "boot time\n(time_boot_ms)", "capture time\n(.tlog stamp)"])
    ax.grid(alpha=0)
    ax.plot([0, 100], [2.9, 2.9], color=PALETTE[0], lw=3)
    ax.text(0, 3.2, "wall clock when the ground station received each message", color=PALETTE[0])
    ax.plot([0, 55], [1.7, 1.7], color=PALETTE[2], lw=3)
    ax.plot([60, 100], [1.7, 1.7], color=PALETTE[2], lw=3)
    ax.text(0, 1.95, "0 ms \u2192 counts up", color=PALETTE[2])
    ax.text(60, 1.95, "reboot: back to 0 ms", color=PALETTE[5], weight="bold")
    ax.axvline(57.5, color=PALETTE[5], ls="--")
    ax.add_patch(FancyBboxPatch((0, 0.2), 55, 0.6, boxstyle="round,pad=0.01", fc="#eef4fa", ec=PALETTE[0]))
    ax.add_patch(FancyBboxPatch((60, 0.2), 40, 0.6, boxstyle="round,pad=0.01", fc="#fff3ee", ec=ACCENT))
    ax.text(27.5, 0.5, "epoch 0", ha="center", weight="bold")
    ax.text(80, 0.5, "epoch 1", ha="center", weight="bold")
    ax.set_xlabel("real time \u2192")
    ax.set_xticks([])
    ax.set_title("Two clocks, and why AnaTele splits a log into epochs")
    ax.text(50, 3.8, "Inside one epoch, boot time only goes forward, so samples can be ordered and compared safely.",
            ha="center", fontsize=11)
    return save(fig, "07_time_bases.png")


def fig08_pipeline():
    fig, ax = plt.subplots(figsize=(15, 4.8))
    _blank(ax)
    ax.set_xlim(0, 15)
    ax.set_ylim(0, 4.5)
    steps = [("Parse", ".tlog / .bin\n\u2192 records"), ("Normalise", "sources,\nepochs, units"),
             ("Derive", "phases, tilt error,\nmotor flags"), ("Rules", "versioned pack\n\u2192 4 states"),
             ("Hypotheses", "for / against\n\u2192 ranked"), ("Report", "timeline, 3D replay,\n.ant file")]
    for i, (title, body) in enumerate(steps):
        x = 0.2 + i * 2.48
        _box(ax, x, 1.4, 2.1, 1.9, "", color="#fff3ee" if i in (3, 4) else "#f4f4f4",
             edge=ACCENT if i in (3, 4) else "#444")
        ax.text(x + 1.05, 2.85, title, ha="center", weight="bold", fontsize=13)
        ax.text(x + 1.05, 2.0, body, ha="center", va="center", fontsize=10)
        if i < len(steps) - 1:
            _arrow(ax, (x + 2.12, 2.35), (x + 2.46, 2.35))
    ax.text(7.5, 0.6, "Every number in the report links back to the exact log record (and raw bytes) it came from.",
            ha="center", fontsize=11, color="#333")
    ax.set_title("What AnaTele does to a log, step by step")
    return save(fig, "08_pipeline.png")


FIGURES = [(f.__name__[3:], f) for f in (fig01_quad_layout, fig02_roll_pitch_yaw, fig03_thrust_balance,
                                          fig04_what_is_a_log, fig05_message_stream, fig06_sample_rates,
                                          fig07_time_bases, fig08_pipeline)]
