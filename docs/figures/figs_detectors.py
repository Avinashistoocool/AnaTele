"""Data figures 19-24 and 32: vibration, EKF/compass, rule logic and hypotheses."""
import math
import random

import matplotlib.pyplot as plt

from figs_basics import _arrow, _blank, _box
from figs_phases import longest_run, phase_times
from guide_common import ACCENT, GREY, PALETTE, manifest, messages, save


def fig19_vibration_nominal_vs_bad():
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5), sharey=True)
    for ax, scenario in zip(axes, ("nominal", "vibration")):
        rows = messages(scenario)["VIBRATION"]
        p = phase_times(scenario)
        for axis, color in zip("xyz", PALETTE):
            ax.plot([r["t"] for r in rows], [r[f"vibration_{axis}"] for r in rows], color=color, lw=1.2, label=axis)
        ax.axhline(30, color=ACCENT, ls="--")
        ax.axhline(60, color=PALETTE[5], ls=":")
        ax.text(p["armed_us"], 31, "30 m/s\u00b2 for 2 s", color=ACCENT, fontsize=10)
        ax.text(p["armed_us"], 61, "60 m/s\u00b2 for 1 s", color=PALETTE[5], fontsize=10)
        peak = max(max(r[f"vibration_{a}"] for a in "xyz") for r in rows)
        ax.set_title(f"{scenario}: peak {peak:.1f} m/s\u00b2")
        ax.set_xlim(p["armed_us"] - 2, p["disarmed_us"] + 2)
        ax.set_xlabel("time since log start (s)")
        ax.legend(title="axis", loc="upper right")
    axes[0].set_ylabel("vibration (m/s\u00b2)")
    fig.suptitle("Vibration: a healthy drone vs one with a damaged propeller", weight="bold")
    return save(fig, "19_vibration_nominal_vs_bad.png")


def fig20_ekf_compass_ratio():
    rows = messages("compass_interference")["EKF_STATUS_REPORT"]
    p = phase_times("compass_interference")
    t, v = [r["t"] for r in rows], [r["compass_variance"] for r in rows]
    window = [(a, b) for a, b in zip(t, v) if p["takeoff_us"] <= a <= p["pre_event_end_us"]]
    length, start, end = longest_run(*zip(*window), 1.0)
    fig, ax = plt.subplots(figsize=(13, 5.5))
    ax.plot(t, v, "o-", ms=3, color=PALETTE[0], label="EKF compass test ratio")
    ax.axhline(1.0, color=ACCENT, ls="--", lw=2)
    ax.text(p["armed_us"], 1.03, "1.0 = measurement disagrees with prediction more than expected", color=ACCENT)
    if start is not None:
        ax.axvspan(start, end, color=ACCENT, alpha=0.2)
        ax.text(start + 0.5, max(v) * 0.97, f"longest run above 1.0: {length:.1f} s (\u2265 2 s needed)", fontsize=11)
    ax.set_xlim(p["armed_us"] - 2, p["disarmed_us"] + 2)
    ax.set_xlabel("time since log start (s)")
    ax.set_ylabel("test ratio")
    ax.set_title(f"Compass interference: the EKF stops trusting the compass (max {max(v):.2f})")
    ax.legend(loc="upper left")
    return save(fig, "20_ekf_compass_ratio.png")


def fig21_heading_disagreement():
    data = messages("compass_interference")
    att, gps = data["ATTITUDE"], data["GLOBAL_POSITION_INT"]
    p = phase_times("compass_interference")
    t, yaw, track = [], [], []
    for a, g in zip(att, gps):
        speed = math.hypot(g["vx"], g["vy"]) / 100
        t.append(a["t"])
        yaw.append(math.degrees(a["yaw"]) % 360)
        track.append(math.degrees(math.atan2(g["vy"], g["vx"])) % 360 if speed > 2 else float("nan"))
    fig, ax = plt.subplots(figsize=(13, 5.5))
    ax.plot(t, yaw, color=PALETTE[0], lw=1.5, label="heading the autopilot believes (yaw)")
    ax.plot(t, track, ".", ms=3, color=ACCENT, label="direction of travel from GPS (when moving > 2 m/s)")
    ax.set_xlim(p["takeoff_us"], p["disarmed_us"])
    ax.set_ylim(0, 360)
    ax.set_xlabel("time since log start (s)")
    ax.set_ylabel("degrees from north")
    ax.set_title("Compass interference: believed heading drifts away from the real path")
    ax.legend(loc="upper left", fontsize=10)
    ax.text(0.99, 0.02, "Heading and travel direction need not match exactly (wind, sideways flight),\n"
            "but a growing, persistent gap points at the compass.", transform=ax.transAxes, ha="right", fontsize=10)
    return save(fig, "21_heading_disagreement.png")


def fig22_persistence_rule():
    rng = random.Random(4)
    t = [i * 0.1 for i in range(120)]
    v = [18 + 4 * math.sin(x / 1.3) + rng.gauss(0, 2) for x in t]
    for i in range(22, 25):
        v[i] = 34 + rng.random() * 3
    for i in range(70, 96):
        v[i] = 36 + rng.gauss(0, 2)
    fig, ax = plt.subplots(figsize=(13, 5))
    ax.plot(t, v, color=PALETTE[0], lw=1.5)
    ax.axhline(30, color=ACCENT, ls="--")
    ax.axvspan(t[22], t[24], color=GREY, alpha=0.3)
    ax.axvspan(t[70], t[95], color=ACCENT, alpha=0.25)
    ax.text(t[23], 41, "blip: 0.2 s\nignored", ha="center")
    ax.text(t[82], 44, "run: 2.5 s \u2265 2 s\n\u2192 TRIGGERED", ha="center", color=ACCENT, weight="bold")
    ax.text(0, 31, "threshold 30", color=ACCENT)
    ax.set_ylim(5, 50)
    ax.set_xlabel("time (s)")
    ax.set_ylabel("signal")
    ax.set_title("Persistence: a value must stay above the line long enough to count (illustration)")
    return save(fig, "22_persistence_rule.png")


def fig23_rule_states():
    fig, ax = plt.subplots(figsize=(14, 6.5))
    _blank(ax)
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 7)
    _box(ax, 5.2, 5.7, 3.6, 0.9, "Does this rule apply\nto this flight?", color="#f4f4f4")
    _box(ax, 0.3, 3.6, 3.2, 1.0, "NOT APPLICABLE\ne.g. no profile chosen", color="#eeeeee", weight="bold")
    _box(ax, 5.2, 3.6, 3.6, 0.9, "Is the needed data there,\nand good enough?", color="#f4f4f4")
    _box(ax, 10.3, 3.6, 3.4, 1.0, "UNASSESSABLE\nmissing or too sparse", color="#fdf3d8", weight="bold")
    _box(ax, 5.2, 1.6, 3.6, 0.9, "Is the condition met\n(e.g. long enough)?", color="#f4f4f4")
    _box(ax, 1.0, 0.2, 3.6, 1.0, "NOT TRIGGERED\nchecked, looked fine", color="#eaf6ef", weight="bold")
    _box(ax, 9.4, 0.2, 3.6, 1.0, "TRIGGERED\nchecked, condition met", color="#fff3ee", edge=ACCENT, weight="bold")
    _arrow(ax, (5.2, 6.1), (3.5, 4.4))
    ax.text(3.9, 5.4, "no")
    _arrow(ax, (7.0, 5.7), (7.0, 4.5))
    ax.text(7.15, 5.0, "yes")
    _arrow(ax, (8.8, 4.05), (10.3, 4.1))
    ax.text(9.4, 4.25, "no")
    _arrow(ax, (7.0, 3.6), (7.0, 2.5))
    ax.text(7.15, 3.0, "yes")
    _arrow(ax, (5.2, 2.0), (3.6, 1.2))
    ax.text(4.0, 1.8, "no")
    _arrow(ax, (8.8, 2.0), (10.4, 1.2))
    ax.text(9.8, 1.8, "yes")
    ax.set_title("Every rule ends in exactly one of four states. Missing data is never 'healthy'.")
    return save(fig, "23_rule_states.png")


def fig24_hypothesis_scoring():
    hyps = manifest("motor_failure")["incidents"][0]["hypotheses"]
    from telemetry_platform.rules import load_pack

    pack = load_pack()
    weights = {}
    for h in pack.get("hypotheses", []):
        for item in h.get("support", []):
            weights[(h["id"], item["rule"])] = (item.get("weight", 1), item.get("family"))
    fig, ax = plt.subplots(figsize=(13, 6))
    names = []
    for row, h in enumerate(hyps):
        left = 0.0
        fams = {}
        for s in h["support"]:
            w, fam = weights.get((h["id"], s["rule_id"]), (1, s.get("family")))
            fams.setdefault(fam, []).append((s["rule_id"], w))
        for i, (fam, items) in enumerate(sorted(fams.items(), key=lambda kv: str(kv[0]))):
            raw = sum(w for _, w in items)
            capped = min(raw, 1.5)
            ax.barh(row, capped, left=left, color=PALETTE[i % 3], edgecolor="white")
            ax.text(left + capped / 2, row, f"{fam}\n{raw:g}" + (" \u2192 1.5" if raw > 1.5 else ""), ha="center",
                    va="center", fontsize=9, color="white", weight="bold")
            left += capped
        neg = round(left - h["score"], 6)
        if neg > 0:
            ax.barh(row, -neg, color=GREY)
            ax.text(-neg / 2, row, f"against\n\u2212{neg:g}", ha="center", va="center", fontsize=9)
        ax.text(max(left, 0) + 0.05, row, f"score {h['score']:g}: {h['label']}", va="center", fontsize=11,
                weight="bold" if h["label"] == "strong support" else "normal")
        names.append(h["title"].replace(" or ", "\nor ").replace("/thrust", "/\nthrust"))
    ax.axvline(1, color=GREY, ls=":")
    ax.axvline(2, color=ACCENT, ls="--")
    ax.text(1, len(hyps) - 0.45, "\u2265 1 some", color="#555")
    ax.text(2, len(hyps) - 0.45, "\u2265 2 strong", color=ACCENT)
    ax.set_yticks(range(len(hyps)))
    ax.set_yticklabels(names, fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(-1.5, 4.8)
    ax.set_xlabel("score = sum over families of min(1.5, family weights) \u2212 contradictions")
    ax.set_title("Motor-failure flight: how AnaTele ranked the explanations (real output)")
    return save(fig, "24_hypothesis_scoring.png")


def fig32_four_states_examples():
    examples = []
    for scenario in ("nominal", "motor_failure", "compass_interference", "vibration", "radio_failsafe"):
        for r in manifest(scenario)["rule_results"]:
            examples.append((scenario, r))
    wanted = {}
    for scenario, r in examples:
        wanted.setdefault(r["state"], (scenario, r))
    fig, ax = plt.subplots(figsize=(14, 6.5))
    _blank(ax)
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 8.4)
    colours = {"triggered": "#fff3ee", "not_triggered": "#eaf6ef", "unassessable": "#fdf3d8", "not_applicable": "#eeeeee"}
    order = ["triggered", "not_triggered", "unassessable", "not_applicable"]
    for i, state in enumerate(order):
        y = 6.4 - i * 2.0
        if state not in wanted:
            _box(ax, 0.2, y, 13.6, 1.7, f"{state.upper()}: no example in the five simulated flights "
                 "(every rule had the data it needed)", color=colours[state], size=10)
            continue
        scenario, r = wanted[state]
        detail = (r.get("detail") or "").replace("\u2013", "-")
        detail = detail[:150] + ("\u2026" if len(detail) > 150 else "")
        _box(ax, 0.2, y, 13.6, 1.7, "", color=colours[state], edge=ACCENT if state == "triggered" else "#444")
        ax.text(0.45, y + 1.3, f"{state.upper().replace('_', ' ')}  \u00b7  rule {r.get('rule_id')}  \u00b7  "
                f"flight: {scenario}", fontsize=11, weight="bold", va="center")
        ax.text(0.45, y + 0.55, detail, fontsize=9.5, va="center", wrap=True)
    ax.set_title("Real rule results from the simulated flights, one per state")
    return save(fig, "32_four_states_examples.png")


FIGURES = [(f.__name__[3:], f) for f in (fig19_vibration_nominal_vs_bad, fig20_ekf_compass_ratio,
                                          fig21_heading_disagreement, fig22_persistence_rule, fig23_rule_states,
                                          fig24_hypothesis_scoring, fig32_four_states_examples)]
