"""In-memory time series, flight-phase inference and derived indicators.

Everything here is computed from full-resolution decoded observations (never plot envelopes).
Thresholds are analysis choices recorded in the output, not firmware behaviour.
"""
import bisect
import fnmatch
import math
from array import array
from statistics import median

PHASE_CONFIG = {
    "takeoff_rise_m": 1.0,       # altitude rise above the armed baseline that indicates flight
    "takeoff_start_m": 0.2,      # backtrack to the last sample at or below this rise
    "baseline_window_s": 2.0,
    "demand_window_s": 5.0,
    "min_throttle_pct": 10.0,
    "attitude_excursion_deg": 60.0,
    "event_guard_s": 3.0,        # pre-event window ends this long before a critical report
}
ATTITUDE_PAIR_S = 0.25
MOTOR_FUNCTIONS = {33 + index: index + 1 for index in range(8)}
# ArduPilot motor numbering; (x = right, y = forward) arm positions and +1 = CCW prop.
FRAME_LAYOUTS = {
    (1, 1): ("QUAD/X", {1: (1, 1, 1), 2: (-1, -1, 1), 3: (-1, 1, -1), 4: (1, -1, -1)}),
    (1, 0): ("QUAD/PLUS", {1: (1, 0, 1), 2: (-1, 0, 1), 3: (0, 1, -1), 4: (0, -1, -1)}),
}


class Series:
    __slots__ = ("t", "v", "ids", "unit")

    def __init__(self, unit):
        self.t, self.v, self.ids, self.unit = array("q"), array("d"), array("q"), unit

    def add(self, t_us, value, record_id):
        if self.t and t_us < self.t[-1]:
            # Out-of-order capture times are kept in order to support bisect lookups.
            index = bisect.bisect_right(self.t, t_us)
            self.t.insert(index, t_us)
            self.v.insert(index, value)
            self.ids.insert(index, record_id)
            return
        self.t.append(t_us)
        self.v.append(value)
        self.ids.append(record_id)

    def window(self, start=None, end=None):
        lo = 0 if start is None else bisect.bisect_left(self.t, start)
        hi = len(self.t) if end is None else bisect.bisect_right(self.t, end)
        return range(lo, hi)

    def nearest(self, t_us, tolerance_us):
        index = bisect.bisect_left(self.t, t_us)
        best = None
        for candidate in (index - 1, index):
            if 0 <= candidate < len(self.t) and abs(self.t[candidate] - t_us) <= tolerance_us:
                if best is None or abs(self.t[candidate] - t_us) < abs(self.t[best] - t_us):
                    best = candidate
        return best


class SeriesStore:
    def __init__(self):
        self.data = {}

    def add(self, source, epoch, name, t_us, value, record_id, unit):
        key = (source, epoch, name)
        series = self.data.get(key)
        if series is None:
            series = self.data[key] = Series(unit)
        series.add(t_us, value, record_id)

    def get(self, source, epoch, name):
        return self.data.get((source, epoch, name))

    def match(self, pattern, source=None, epoch=None):
        return [(key, series) for key, series in self.data.items()
                if (source is None or key[0] == source) and (epoch is None or key[1] == epoch)
                and fnmatch.fnmatchcase(key[2], pattern)]

    def names(self, source=None):
        return {key[2] for key in self.data if source is None or key[0] == source}


def tilt_vector(roll_deg, pitch_deg):
    roll, pitch = math.radians(roll_deg), math.radians(pitch_deg)
    return (math.sin(pitch) * math.cos(roll), -math.sin(roll), math.cos(pitch) * math.cos(roll))


def tilt_error_deg(desired, actual):
    a, b = tilt_vector(*desired), tilt_vector(*actual)
    dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))
    return math.degrees(math.acos(dot))


def quaternion_roll_pitch(q):
    w, x, y, z = q
    roll = math.degrees(math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)))
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x)))))
    return roll, pitch


def _first_index(series, start, end, predicate):
    for index in series.window(start, end):
        if predicate(series.v[index]):
            return index
    return None


def infer_phases(store, episode, events, end_fallback, config=PHASE_CONFIG):
    """Return a phase description for one armed episode. Labels inferred values as such."""
    source, epoch = episode["source"], episode["epoch"]
    start, end = episode["start_us"], episode["end_us"] or end_fallback
    phase = {
        "source": source, "epoch": epoch, "armed_us": start, "disarmed_us": episode["end_us"],
        "status": "unassessable", "method": None, "takeoff_us": None, "airborne_end_us": None,
        "loss_of_control_onset_us": None, "critical_event_us": None, "pre_event_end_us": None,
        "evidence": [episode["start_record"]], "notes": [], "config": dict(config),
        "takeoff_record": None, "excursion_record": None,
    }
    critical = [e for e in events if e["source"] == source and e["epoch"] == epoch
                and e["kind"] == "CRASH_CHECK_DISARM" and start <= e["t_us"] <= end + 2_000_000]
    if critical:
        phase["critical_event_us"] = critical[0]["t_us"]
        phase["evidence"] += critical[0]["evidence"]

    reported = [e for e in events if e["source"] == source and e["epoch"] == epoch and start <= e["t_us"] <= end
                and e["kind"] == "LANDED_STATE" and e["text"] in ("In air", "Taking off")]
    altitude = store.get(source, epoch, "position.altitude_home")
    if reported:
        phase.update(status="airborne_reported", method="landed-state report", takeoff_us=reported[0]["t_us"],
                     takeoff_record=reported[0]["record_id"])
        phase["evidence"].append(reported[0]["record_id"])
    elif altitude is None or not len(altitude.window(start, end)):
        phase["notes"].append("No home-relative altitude in this armed episode; airborne state cannot be inferred.")
        return phase
    else:
        indices = altitude.window(start, end)
        baseline_end = altitude.t[indices[0]] + int(config["baseline_window_s"] * 1e6)
        baseline_values = [altitude.v[i] for i in altitude.window(start, baseline_end)]
        baseline = median(baseline_values)
        phase["baseline_altitude_m"] = baseline
        rise = _first_index(altitude, start, end, lambda v: v - baseline >= config["takeoff_rise_m"])
        if rise is None:
            phase.update(status="no_airborne_evidence", method="altitude-rise inference")
            phase["notes"].append(
                f"Home-relative altitude never rose {config['takeoff_rise_m']:g} m above the armed baseline. "
                "A very low hover or ground-only episode cannot be distinguished from altitude-estimate noise.")
            return phase
        rise_t = altitude.t[rise]
        demand = _demand_evidence(store, source, epoch, rise_t - int(config["demand_window_s"] * 1e6), rise_t, config)
        takeoff_index = rise
        for index in reversed(range(indices[0], rise)):
            if altitude.v[index] - baseline <= config["takeoff_start_m"]:
                takeoff_index = index
                break
        phase["evidence"] += [altitude.ids[takeoff_index], altitude.ids[rise]]
        if demand is None:
            phase.update(status="no_airborne_evidence", method="altitude-rise inference")
            phase["notes"].append("Altitude rose without observed throttle/motor demand; treated as estimate drift.")
            return phase
        phase["evidence"].append(demand)
        phase.update(status="airborne_inferred", method="altitude rise with throttle/motor demand",
                     takeoff_us=altitude.t[takeoff_index], takeoff_record=altitude.ids[takeoff_index])
        phase["notes"].append("Takeoff time is inferred from home-relative altitude and demand; no landed-state message.")

    takeoff = phase["takeoff_us"]
    excursion = _attitude_excursion(store, source, epoch, takeoff, end, config["attitude_excursion_deg"])
    if excursion:
        phase["loss_of_control_onset_us"], phase["excursion_record"] = excursion
        phase["evidence"].append(excursion[1])
    candidates = [t for t in (phase["loss_of_control_onset_us"],
                              phase["critical_event_us"] and phase["critical_event_us"] - int(config["event_guard_s"] * 1e6))
                  if t is not None]
    if altitude is not None and phase["status"] == "airborne_inferred":
        above = [i for i in altitude.window(takeoff, end) if altitude.v[i] - phase["baseline_altitude_m"] >= 0.5]
        last_above = altitude.t[above[-1]] if above else end
    else:
        last_above = end
    phase["airborne_end_us"] = min(candidates + [last_above, end]) if candidates else min(last_above, end)
    phase["pre_event_end_us"] = min(candidates) if candidates else phase["airborne_end_us"]
    if phase["pre_event_end_us"] <= takeoff:
        phase["notes"].append("Critical event or attitude excursion began at/just after takeoff; no pre-event airborne window.")
        phase["pre_event_end_us"] = takeoff
    phase["evidence"] = list(dict.fromkeys(phase["evidence"]))
    return phase


def _demand_evidence(store, source, epoch, start, end, config):
    throttle = store.get(source, epoch, "control.throttle_pct")
    if throttle is not None:
        index = _first_index(throttle, start, end, lambda v: v >= config["min_throttle_pct"])
        if index is not None:
            return throttle.ids[index]
    for key, series in store.match("motor.*.output_frac", source, epoch):
        index = _first_index(series, start, end, lambda v: v >= 0.25)
        if index is not None:
            return series.ids[index]
    return None


def _attitude_excursion(store, source, epoch, start, end, limit):
    best = None
    for axis in ("roll", "pitch"):
        series = store.get(source, epoch, f"attitude.{axis}_deg")
        if series is None:
            continue
        index = _first_index(series, start, end, lambda v: abs(v) > limit)
        if index is not None and (best is None or series.t[index] < best[0]):
            best = (series.t[index], series.ids[index])
    return best


def phase_windows(phase, name):
    """Return [(start, end)] for a phase name used by rules."""
    if phase is None or phase["takeoff_us"] is None:
        return []
    if name == "airborne":
        return [(phase["takeoff_us"], phase["airborne_end_us"])]
    if name == "airborne_pre_event":
        return [(phase["takeoff_us"], phase["pre_event_end_us"])] if phase["pre_event_end_us"] > phase["takeoff_us"] else []
    if name == "airborne_with_event":
        end = phase["critical_event_us"] or phase["disarmed_us"] or phase["airborne_end_us"]
        return [(phase["takeoff_us"], end)] if end > phase["takeoff_us"] else []
    if name == "event_and_after":
        return [(phase["pre_event_end_us"], phase["disarmed_us"] or phase["airborne_end_us"])]
    if name == "armed_pre_takeoff":
        return [(phase["armed_us"], phase["takeoff_us"])] if phase["takeoff_us"] > phase["armed_us"] else []
    raise ValueError(f"Unknown phase {name}")


def derive_tracking(store):
    """Approximate thrust-vector (tilt) tracking error from desired vs estimated roll/pitch."""
    produced = 0
    for (source, epoch, name), desired_roll in list(store.data.items()):
        if name != "attitude.desired_roll_deg":
            continue
        desired_pitch = store.get(source, epoch, "attitude.desired_pitch_deg")
        roll, pitch = store.get(source, epoch, "attitude.roll_deg"), store.get(source, epoch, "attitude.pitch_deg")
        if desired_pitch is None or roll is None or pitch is None:
            continue
        for i in range(len(desired_roll.t)):
            t = desired_roll.t[i]
            j = desired_pitch.nearest(t, 1000)
            a, b = roll.nearest(t, int(ATTITUDE_PAIR_S * 1e6)), pitch.nearest(t, int(ATTITUDE_PAIR_S * 1e6))
            if j is None or a is None or b is None:
                continue
            error = tilt_error_deg((desired_roll.v[i], desired_pitch.v[j]), (roll.v[a], pitch.v[b]))
            store.add(source, epoch, "control.tilt_tracking_error_deg", t, error, desired_roll.ids[i], "deg")
            produced += 1
    return produced


def motor_mapping(param):
    """Return motor mapping from parameter accessor param(name) -> value or None."""
    functions = {}
    for channel in range(1, 17):
        value = param(f"SERVO{channel}_FUNCTION")
        if value is not None and int(value) in MOTOR_FUNCTIONS:
            functions[MOTOR_FUNCTIONS[int(value)]] = channel
    pwm_min, pwm_max = param("MOT_PWM_MIN"), param("MOT_PWM_MAX")
    if not pwm_min or not pwm_max:
        pwm_min, pwm_max = param("RC3_MIN"), param("RC3_MAX")
    spin_min, spin_max = param("MOT_SPIN_MIN"), param("MOT_SPIN_MAX")
    frame_class, frame_type = param("FRAME_CLASS"), param("FRAME_TYPE")
    layout = FRAME_LAYOUTS.get((int(frame_class), int(frame_type))) if frame_class is not None and frame_type is not None else None
    mapping = {"motors": functions, "pwm_min": pwm_min, "pwm_max": pwm_max, "spin_min": spin_min, "spin_max": spin_max,
               "frame": layout[0] if layout else None, "layout": layout[1] if layout else None, "limitations": []}
    if not functions:
        mapping["limitations"].append("No SERVOn_FUNCTION motor assignment observed; outputs cannot be mapped to motors.")
    if None in (pwm_min, pwm_max, spin_min, spin_max) or not pwm_max or pwm_max <= pwm_min:
        mapping["limitations"].append("Motor PWM range or spin limits unknown; floor/ceiling saturation unassessable.")
        mapping["floor"] = mapping["ceiling"] = None
    else:
        span = pwm_max - pwm_min
        mapping["floor"], mapping["ceiling"] = pwm_min + spin_min * span, pwm_min + spin_max * span
    if layout is None:
        mapping["limitations"].append("Frame geometry not in the supported table; roll/pitch/yaw output decomposition skipped.")
    return mapping


def derive_motors(store, source, epoch, mapping):
    """Per-motor normalized output and saturation flags, plus a crude mixer decomposition."""
    if not mapping["motors"] or mapping["floor"] is None:
        return 0
    channels = {motor: store.get(source, epoch, f"output.ch{channel}") for motor, channel in mapping["motors"].items()}
    if any(series is None for series in channels.values()):
        return 0
    reference = channels[min(channels)]
    span = mapping["pwm_max"] - mapping["pwm_min"]
    produced = 0
    for i in range(len(reference.t)):
        t, rid = reference.t[i], reference.ids[i]
        values = {}
        for motor, series in channels.items():
            j = series.nearest(t, 1000)
            if j is None:
                break
            values[motor] = series.v[j]
        if len(values) != len(channels):
            continue
        if max(values.values()) <= mapping["pwm_min"] + 1:
            continue  # disarmed / stopped outputs are not a saturation observation
        for motor, value in values.items():
            store.add(source, epoch, f"motor.{motor}.output_frac", t, (value - mapping["pwm_min"]) / span, rid, "fraction")
            store.add(source, epoch, f"motor.{motor}.at_floor", t, 1.0 if value <= mapping["floor"] + 2 else 0.0, rid, "flag")
            store.add(source, epoch, f"motor.{motor}.at_ceiling", t, 1.0 if value >= mapping["ceiling"] - 2 else 0.0, rid, "flag")
        layout = mapping["layout"]
        if layout and set(layout) == set(values):
            for axis, column in (("right_minus_left", 0), ("front_minus_back", 1), ("ccw_minus_cw", 2)):
                weights = [layout[m][column] for m in values]
                if not any(weights):
                    continue
                demand = sum(layout[m][column] * values[m] for m in values) / sum(abs(w) for w in weights)
                store.add(source, epoch, f"motor.mix.{axis}", t, demand, rid, "us")
        produced += 1
    return produced


def window_stats(series, windows):
    values = [series.v[i] for start, end in windows for i in series.window(start, end)]
    if not values:
        return None
    ordered = sorted(values)
    return {"n": len(values), "mean": sum(values) / len(values), "max": ordered[-1], "min": ordered[0],
            "p95": ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]}
