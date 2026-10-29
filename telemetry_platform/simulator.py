"""Synthetic ArduCopter flight simulator that writes Mission Planner style .tlog captures.

A simple 6-DoF quad (four motors, PD attitude control with ArduPilot-like roll/pitch-over-yaw
mixer priority, waypoint guidance, drag and wind) flown through an AUTO mission. Scenarios inject
faults so the analysis engine can be exercised end to end:

    nominal               mission flown, RTL and landing
    motor_failure         one motor/ESC degrades gradually mid-mission -> saturation, loss of control
    compass_interference  motor-current magnetic interference -> yaw error, toilet-bowling, EKF failsafe LAND
    vibration             damaged propeller -> high vibration and accelerometer clipping
    radio_failsafe        PreArm radio warnings, then RC loss in flight -> RTL

Output is clearly synthetic (STATUSTEXT announces it) and is not evidence about any real aircraft.
"""
import argparse
import math
import random
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pymavlink.dialects.v20 import ardupilotmega as mav

SCENARIOS = ("nominal", "motor_failure", "compass_interference", "vibration", "radio_failsafe")
G = 9.81
DT = 0.01
MODES = {"STABILIZE": 0, "AUTO": 3, "LOITER": 5, "RTL": 6, "LAND": 9}
# Same motor numbering/geometry as derived.FRAME_LAYOUTS (x right, y forward, +1 = CCW prop).
LAYOUT = {1: (1, 1, 1), 2: (-1, -1, 1), 3: (-1, 1, -1), 4: (1, -1, -1)}
HOME = (47.397742, 8.545594, 488.0)  # public SITL default field (Zurich); not a real flight site


@dataclass
class Airframe:
    mass: float = 1.6
    arm: float = 0.18
    inertia: tuple = (0.022, 0.022, 0.04)
    yaw_coefficient: float = 0.016
    hover: float = 0.38
    spin_min: float = 0.15
    spin_max: float = 0.95
    drag: float = 0.35

    @property
    def max_thrust(self):
        return self.mass * G / (4 * self.hover)


@dataclass
class State:
    n: float = 0.0
    e: float = 0.0
    u: float = 0.0
    vn: float = 0.0
    ve: float = 0.0
    vu: float = 0.0
    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = math.radians(20)
    p: float = 0.0
    q: float = 0.0
    r: float = 0.0
    outputs: list = field(default_factory=lambda: [0.0] * 4)
    health: list = field(default_factory=lambda: [1.0] * 4)


def wrap(angle):
    return (angle + math.pi) % (2 * math.pi) - math.pi


def clamp(value, low, high):
    return max(low, min(high, value))


class Capture:
    def __init__(self, start_us):
        self.mav = mav.MAVLink(None)
        self.frames, self.seq, self.start = [], {}, start_us

    def emit(self, t, message, source=(1, 1)):
        self.mav.srcSystem, self.mav.srcComponent = source
        self.mav.seq = self.seq.get(source, 0)
        self.seq[source] = (self.mav.seq + 1) % 256
        self.frames.append((t, len(self.frames), struct.pack(">Q", self.start + int(t * 1e6)) + message.pack(self.mav)))

    def bytes(self):
        return b"".join(frame for _, _, frame in sorted(self.frames))


class Simulator:
    def __init__(self, scenario="nominal", seed=1, start=None, mission=None, wind=(1.5, -0.8)):
        if scenario not in SCENARIOS:
            raise ValueError(f"Unknown scenario {scenario}; choose from {', '.join(SCENARIOS)}")
        self.scenario, self.rng = scenario, random.Random(seed)
        start = start or datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        self.out = Capture(int(start.timestamp() * 1e6))
        self.frame, self.s = Airframe(), State()
        self.wind = wind
        self.mission = mission or [("TAKEOFF", 0, 0, 10), ("WP", 40, 0, 10), ("WP", 40, 35, 12),
                                   ("WP", -5, 45, 12), ("WP", -20, 10, 10), ("RTL", 0, 0, 0)]
        self.armed, self.mode, self.item, self.crashed, self.landed_at = False, "STABILIZE", 1, False, None
        self.disarmed_at = None
        self.yaw_error, self.compass_ratio, self.compass_switched = 0.0, 0.05, False
        self.clips, self.rc_ok, self.desired = 0, True, (0.0, 0.0, self.s.yaw)
        self.tilt_bad_since, self.ratio_high_since, self.prev_v = None, None, (0.0, 0.0, 0.0)
        self.events = {"failure_t": None}
        self.wp_radius = 2.0
        self.battery = 16.7

    # ------------------------------------------------------------------ messaging helpers
    def text(self, t, message, severity=6):
        self.out.emit(t, self.out.mav.statustext_encode(severity, message.encode()[:50]))

    def heartbeat(self, t):
        base = mav.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED | (mav.MAV_MODE_FLAG_SAFETY_ARMED if self.armed else 0)
        self.out.emit(t, self.out.mav.heartbeat_encode(mav.MAV_TYPE_QUADROTOR, mav.MAV_AUTOPILOT_ARDUPILOTMEGA,
                                                       base, MODES[self.mode], 4 if self.armed else 3))

    def set_mode(self, t, mode):
        if mode != self.mode:
            self.mode = mode
            self.heartbeat(t)

    def boot(self):
        o = self.out.mav
        for t in (0, 1):
            self.out.emit(t, o.heartbeat_encode(mav.MAV_TYPE_GCS, mav.MAV_AUTOPILOT_INVALID, 0, 0, 0), (255, 190))
        self.text(0.2, "ArduCopter V4.7.2 (fb773567)")
        self.text(0.2, "SIMULATED FLIGHT - AnaTele synthetic data", 5)
        self.text(0.2, "Frame: QUAD/X")
        self.out.emit(0.3, o.autopilot_version_encode(64495, (4 << 24) | (7 << 16) | (2 << 8) | 255, 0, 0, 0,
                                                      [0] * 8, [0] * 8, [0] * 8, 0, 0, 0))
        ints = [("SERVO1_FUNCTION", 33), ("SERVO2_FUNCTION", 34), ("SERVO3_FUNCTION", 35), ("SERVO4_FUNCTION", 36),
                ("MOT_PWM_MIN", 1000), ("MOT_PWM_MAX", 2000), ("FRAME_CLASS", 1), ("FRAME_TYPE", 1),
                ("FS_THR_ENABLE", 1), ("FS_THR_VALUE", 975), ("FS_GCS_ENABLE", 0), ("FS_CRASH_CHECK", 1),
                ("BATT_MONITOR", 4), ("ARMING_SKIPCHK", 0), ("RCMAP_THROTTLE", 3), ("COMPASS_USE", 1),
                ("COMPASS_USE2", 1), ("EK3_SRC1_YAW", 1), ("AUTO_OPTIONS", 0)]
        floats = [("MOT_SPIN_MIN", self.frame.spin_min), ("MOT_SPIN_MAX", self.frame.spin_max), ("MOT_SPIN_ARM", .1),
                  ("MOT_THST_HOVER", self.frame.hover), ("WP_RADIUS_M", self.wp_radius), ("WP_SPD", 5.0),
                  ("FS_EKF_THRESH", 0.8)]
        total = len(ints) + len(floats)
        for index, (name, value) in enumerate(ints):
            kind = mav.MAV_PARAM_TYPE_INT16 if value > 127 else mav.MAV_PARAM_TYPE_INT8
            self.out.emit(1 + index * .01, o.param_value_encode(name.encode(), value, kind, total, index))
        for index, (name, value) in enumerate(floats, len(ints)):
            self.out.emit(1 + index * .01, o.param_value_encode(name.encode(), value, mav.MAV_PARAM_TYPE_REAL32, total, index))
        self.upload_mission(4.0)

    def upload_mission(self, t):
        o = self.out.mav
        items = [(16, 0, 0, 0, HOME[2])] + [({"TAKEOFF": 22, "WP": 16, "RTL": 20}[kind], n, e, alt, None)
                                            for kind, n, e, alt in self.mission]
        self.out.emit(t, o.mission_count_encode(1, 1, len(items), 0), (255, 190))
        for seq, (command, n, e, alt, absolute) in enumerate(items):
            t += .05
            self.out.emit(t, o.mission_request_int_encode(255, 190, seq, 0))
            lat, lon = self.latlon(n, e)
            frame = mav.MAV_FRAME_GLOBAL if seq == 0 else mav.MAV_FRAME_GLOBAL_RELATIVE_ALT
            x, y = (0, 0) if command == 20 else (int(lat * 1e7), int(lon * 1e7))
            z = absolute if absolute is not None else alt
            self.out.emit(t + .01, o.mission_item_int_encode(1, 1, seq, frame, command, 0, 1, 0, 0, 0, 0, x, y, z, 0),
                          (255, 190))
        self.out.emit(t + .05, o.mission_ack_encode(255, 190, mav.MAV_MISSION_ACCEPTED, 0))
        self.text(t + .06, "Flight plan received")

    def latlon(self, n, e):
        return HOME[0] + n / 111320.0, HOME[1] + e / (111320.0 * math.cos(math.radians(HOME[0])))

    # ------------------------------------------------------------------ guidance & control
    def target(self):
        kind, n, e, alt = self.mission[self.item - 1]
        if kind == "TAKEOFF":
            return 0.0, 0.0, alt
        return n, e, alt

    def guidance(self, t):
        """Velocity demand (north, east, up) and desired heading for AUTO / RTL / LAND."""
        s = self.s
        if self.mode == "RTL":
            home = math.hypot(s.n, s.e)
            if home < 2.0 and math.hypot(s.vn, s.ve) < 1.0:
                self.text(t, "RTL: landing")
                self.set_mode(t, "LAND")
            else:
                self.rtl_alt = max(getattr(self, "rtl_alt", 0.0), 10.0, s.u if home > 5 else 0.0)
                tn, te, talt = 0.0, 0.0, self.rtl_alt
                return self.fly_to(tn, te, talt)
        if self.mode == "LAND":
            vn, ve = clamp(-0.5 * s.vn, -1, 1), clamp(-0.5 * s.ve, -1, 1)
            return vn, ve, -1.5 if s.u > 4 else -0.5, None
        tn, te, talt = self.target()
        vn, ve, vu, bearing = self.fly_to(tn, te, talt)
        if self.mission[self.item - 1][0] == "TAKEOFF":
            vn = ve = 0.0
            bearing = None
        return vn, ve, vu, bearing

    def fly_to(self, tn, te, talt):
        s = self.s
        dn, de = tn - s.n, te - s.e
        distance = math.hypot(dn, de)
        speed = min(5.0, 0.8 * distance)
        vn, ve = (dn / distance * speed, de / distance * speed) if distance > 0.1 else (0.0, 0.0)
        vu = clamp(1.2 * (talt - s.u), -1.5, 2.0)
        return vn, ve, vu, math.atan2(de, dn) if distance > 3 else None

    def advance_mission(self, t):
        s = self.s
        kind, n, e, alt = self.mission[self.item - 1]
        reached = (kind == "TAKEOFF" and s.u >= alt - 0.3) or (
            kind == "WP" and math.hypot(n - s.n, e - s.e) <= self.wp_radius and abs(alt - s.u) < 2)
        if reached:
            self.out.emit(t, self.out.mav.mission_item_reached_encode(self.item))
            self.text(t, f"Reached command #{self.item}")
            self.item += 1
            kind = self.mission[self.item - 1][0]
            self.text(t, f"Mission: {self.item} {'WP' if kind == 'WP' else 'RTL' if kind == 'RTL' else kind.title()}")
            if kind == "RTL":
                self.set_mode(t, "RTL")
            if self.scenario == "motor_failure" and self.item == 3 and self.events["failure_t"] is None:
                self.events["failure_t"] = t + 2.0

    def control(self, t, vn_d, ve_d, vu_d, bearing):
        s, f = self.s, self.frame
        an = clamp(1.4 * (vn_d - s.vn), -4, 4)
        ae = clamp(1.4 * (ve_d - s.ve), -4, 4)
        estimated_yaw = s.yaw + self.yaw_error
        a_fwd = an * math.cos(estimated_yaw) + ae * math.sin(estimated_yaw)
        a_right = -an * math.sin(estimated_yaw) + ae * math.cos(estimated_yaw)
        pitch_d = clamp(-math.atan2(a_fwd, G), -0.52, 0.52)
        roll_d = clamp(math.atan2(a_right, G), -0.52, 0.52)
        yaw_d = bearing if bearing is not None else self.desired[2]
        self.desired = (roll_d, pitch_d, yaw_d)
        au = clamp(2.5 * (vu_d - s.vu), -4, 5)
        tilt = max(0.3, math.cos(s.roll) * math.cos(s.pitch))
        collective = f.mass * (G + au) / tilt / (4 * f.max_thrust)
        ix, iy, iz = f.inertia
        tau_r = ix * (100 * (roll_d - s.roll) - 16 * s.p)
        tau_p = iy * (100 * (pitch_d - s.pitch) - 16 * s.q)
        tau_y = iz * (9 * wrap(yaw_d - estimated_yaw) - 6 * s.r)
        lever = f.arm * 0.7071
        rp = [(-x * tau_r + y * tau_p) / (4 * lever * f.max_thrust) for x, y, _ in LAYOUT.values()]
        yw = [spin * tau_y / (4 * f.yaw_coefficient * f.max_thrust) for _, _, spin in LAYOUT.values()]
        low, high = f.spin_min, f.spin_max
        collective = clamp(collective, low, high)
        span = max(rp) - min(rp)
        if span > high - low:
            rp = [v * (high - low) / span for v in rp]
        shift = max(0.0, max(collective + v for v in rp) - high) - max(0.0, low - min(collective + v for v in rp))
        base = [collective - shift + v for v in rp]
        headroom = min(min(high - b for b in base), min(b - low for b in base))
        biggest = max(abs(v) for v in yw) or 1.0
        scale = clamp(headroom / biggest, 0.0, 1.0)
        s.outputs = [clamp(b + scale * v, low, high) for b, v in zip(base, yw)]

    # ------------------------------------------------------------------ physics
    def physics(self, t, powered):
        s, f = self.s, self.frame
        forces = [s.health[i] * f.max_thrust * s.outputs[i] if powered else 0.0 for i in range(4)]
        lever = f.arm * 0.7071
        tau_r = sum(-x * F * lever for (x, _, _), F in zip(LAYOUT.values(), forces))
        tau_p = sum(y * F * lever for (_, y, _), F in zip(LAYOUT.values(), forces))
        tau_y = sum(spin * f.yaw_coefficient * F for (_, _, spin), F in zip(LAYOUT.values(), forces))
        if self.scenario == "vibration" and powered:
            tau_r += self.rng.gauss(0, 0.01)
            tau_p += self.rng.gauss(0, 0.01)
        ix, iy, iz = f.inertia
        s.p += (tau_r / ix - 0.5 * s.p) * DT
        s.q += (tau_p / iy - 0.5 * s.q) * DT
        s.r += (tau_y / iz - 0.8 * s.r) * DT
        s.roll = wrap(s.roll + s.p * DT)
        s.pitch = clamp(s.pitch + s.q * DT, -math.pi / 2 + 0.01, math.pi / 2 - 0.01)
        s.yaw = wrap(s.yaw + s.r * DT)
        thrust = sum(forces) / f.mass
        a_fwd = -thrust * math.sin(s.pitch) * math.cos(s.roll)
        a_right = thrust * math.sin(s.roll)
        a_up = thrust * math.cos(s.pitch) * math.cos(s.roll) - G
        gust = 0.6 * math.sin(t / 7.0)
        wn, we = self.wind[0] + gust, self.wind[1]
        an = a_fwd * math.cos(s.yaw) - a_right * math.sin(s.yaw) - f.drag * (s.vn - wn)
        ae = a_fwd * math.sin(s.yaw) + a_right * math.cos(s.yaw) - f.drag * (s.ve - we)
        au = a_up - f.drag * s.vu
        on_ground = s.u <= 0.0
        if on_ground and au <= 0:
            s.vn = s.ve = s.vu = 0.0
            if not powered:
                s.p = s.q = s.r = 0.0
            return (0.0, 0.0, 0.0)
        s.vn += an * DT
        s.ve += ae * DT
        s.vu += au * DT
        s.n += s.vn * DT
        s.e += s.ve * DT
        s.u += s.vu * DT
        if s.u < 0:
            impact = -s.vu
            s.u, s.vn, s.ve, s.vu = 0.0, 0.0, 0.0, 0.0
            s.p = s.q = s.r = 0.0
            if impact > 2.5 and not self.crashed:
                self.events["impact"] = (t, impact)
        return (an, ae, au)

    # ------------------------------------------------------------------ faults
    def faults(self, t):
        s = self.s
        throttle = sum(s.outputs) / 4 if self.armed else 0.0
        if self.scenario == "motor_failure" and self.events["failure_t"] and t >= self.events["failure_t"]:
            # Degrading motor/ESC: thrust falls steadily until the controller runs out of authority.
            s.health[2] = max(0.3, 1.0 - 0.06 * (t - self.events["failure_t"]))
        if self.scenario == "compass_interference":
            self.compass_ratio = 0.04 + max(0.0, throttle - 0.2) * 6.0 * (0.9 if self.compass_switched else 1.0)
            if self.armed and s.u > 1:
                drift = (0.035 if not self.compass_switched else 0.02) * max(0.0, throttle - 0.25) * 4
                self.yaw_error = clamp(self.yaw_error + drift * DT * 10, -1.4, 1.4)
            if self.compass_ratio > 1.0:
                self.ratio_high_since = self.ratio_high_since or t
            else:
                self.ratio_high_since = None
            if self.ratio_high_since and t - self.ratio_high_since > 4 and not self.compass_switched:
                self.compass_switched = True
                self.yaw_error *= 0.6
                self.text(t, "EKF3 IMU0 switching to compass 1", 4)
            if self.ratio_high_since and t - self.ratio_high_since > 25 and self.mode in ("AUTO", "RTL"):
                self.text(t, "EKF variance", 2)
                self.text(t, "EKF Failsafe: changed to LAND Mode", 2)
                self.set_mode(t, "LAND")
        else:
            self.compass_ratio = 0.03 + 0.15 * throttle + abs(self.rng.gauss(0, 0.02))
        if self.scenario == "radio_failsafe" and self.armed and self.item >= 3 and self.rc_ok and s.u > 5:
            self.rc_ok = False
            self.text(t, "Radio Failsafe", 4)
            self.set_mode(t, "RTL")

    def crash_check(self, t):
        if not self.armed or self.mode not in ("AUTO", "RTL", "LAND", "LOITER") or self.s.u < 0.05 and self.s.vu == 0:
            self.tilt_bad_since = None
            return
        roll_d, pitch_d, _ = self.desired
        error = math.degrees(math.acos(clamp(
            math.cos(self.s.roll) * math.cos(self.s.pitch) * math.cos(roll_d) * math.cos(pitch_d)
            + math.sin(self.s.pitch) * math.cos(self.s.roll) * math.sin(pitch_d) * math.cos(roll_d)
            + math.sin(self.s.roll) * math.sin(roll_d), -1, 1)))
        if error > 30:
            self.tilt_bad_since = self.tilt_bad_since or t
            if t - self.tilt_bad_since >= 2.0:
                accel = math.sqrt(sum(v * v for v in self.prev_v))
                self.text(t, f"Crash: Disarming: AngErr={int(min(error, 180))}>30, Accel={min(accel, 2.9):.1f}<3.0", 2)
                self.crashed = True
                self.disarm(t)
        else:
            self.tilt_bad_since = None

    def disarm(self, t):
        self.armed = False
        self.disarmed_at = t
        self.heartbeat(t)

    # ------------------------------------------------------------------ telemetry
    def telemetry(self, t, accel):
        o, s = self.out.mav, self.s
        ms = int(t * 1000)
        lat, lon = self.latlon(s.n, s.e)
        noise = self.rng.gauss
        roll_d, pitch_d, yaw_d = self.desired
        yaw_reported = wrap(s.yaw + self.yaw_error)
        self.out.emit(t, o.attitude_encode(ms, s.roll + noise(0, .002), s.pitch + noise(0, .002), yaw_reported, s.p, s.q, s.r))
        self.out.emit(t, o.global_position_int_encode(ms, int(lat * 1e7), int(lon * 1e7), int((HOME[2] + s.u) * 1000),
                                                      int(s.u * 1000), int(s.vn * 100), int(s.ve * 100), int(-s.vu * 100),
                                                      int(math.degrees(yaw_reported) % 360 * 100)))
        pwm = [int(1000 + 1000 * (u if self.armed else 0)) for u in s.outputs]
        if self.armed and s.u <= 0.02 and self.mode == "STABILIZE":
            pwm = [1100] * 4
        self.out.emit(t, o.servo_output_raw_encode(int(t * 1e6) & 0xFFFFFFFF, 0, *pwm, 0, 0, 0, 0))
        throttle = int(100 * (sum(s.outputs) / 4 if self.armed else 0))
        speed = math.hypot(s.vn, s.ve)
        self.out.emit(t, o.vfr_hud_encode(speed, speed, int(math.degrees(yaw_reported) % 360), throttle, HOME[2] + s.u, s.vu))
        tn, te, _ = self.target() if self.item <= len(self.mission) else (0, 0, 0)
        bearing = int(math.degrees(math.atan2(te - s.e, tn - s.n)))
        self.out.emit(t, o.nav_controller_output_encode(math.degrees(roll_d), math.degrees(pitch_d), int(math.degrees(yaw_d)),
                                                        bearing, int(min(65535, math.hypot(tn - s.n, te - s.e))), 0, 0, 0))
        if int(t * 10) % 2 == 0:
            values = [1500, 1500, 1001 if not self.armed else 1500, 1500] + [1500] * 4
            count = 16 if self.rc_ok else 0
            if not self.rc_ok:
                values = [0] * 8
            self.out.emit(t, o.rc_channels_encode(ms, count, *values, *([65535] * 10), 200 if self.rc_ok else 0))
        if int(t * 10) % 5 == 0:
            base = 4 + 22 * (sum(s.outputs) / 4 if self.armed and s.u > 0.1 else 0)
            if self.scenario == "vibration" and self.armed and s.u > 0.5:
                base = 38 + 12 * math.sin(t / 3) + noise(0, 3)
                if self.rng.random() < 0.6:
                    self.clips += self.rng.randint(1, 6)
            if "impact" in self.events and abs(self.events["impact"][0] - t) < 0.6:
                base, self.clips = 70, self.clips + 40
            if self.scenario == "motor_failure" and self.events["failure_t"] and t > self.events["failure_t"] and self.armed:
                base += 12
            vib = [max(0.0, base * k + noise(0, 1.0)) for k in (.45, .55, 1.0)]
            self.out.emit(t, o.vibration_encode(int(t * 1e6), *vib, self.clips, 0, 0))
            self.out.emit(t, o.ekf_status_report_encode(831, 0.05, 0.03, 0.02, self.compass_ratio, 0, 0))
            self.out.emit(t, o.gps_raw_int_encode(int(t * 1e6), 3, int(lat * 1e7 + noise(0, 3)), int(lon * 1e7 + noise(0, 3)),
                                                  int((HOME[2] + s.u) * 1000), 70, 110, int(speed * 100), 0, 14,
                                                  0, int(380 + abs(noise(0, 40))), 600, 300, 0, 0))
            current = 1.5 + (32 * sum(s.outputs) / 4 if self.armed else 0)
            self.battery -= current * 0.5 / 3600 * 0.9
            voltage = self.battery - 0.025 * current
            self.out.emit(t, o.sys_status_encode(0x57719c2f, 0x57719c2f, 0x57719c2f, 500, int(voltage * 1000),
                                                 int(current * 100), int(clamp((self.battery - 13.2) / 3.5 * 100, 0, 100)),
                                                 0, 0, 0, 0, 0, 0))
        if int(t * 10) % 10 == 0:
            self.heartbeat(t)
            self.out.emit(t, o.heartbeat_encode(mav.MAV_TYPE_GCS, mav.MAV_AUTOPILOT_INVALID, 0, 0, 0), (255, 190))
            self.out.emit(t, o.mission_current_encode(self.item))
            self.out.emit(t, o.radio_status_encode(190, 180, 95, 30, 28, 0, 0), (51, 68))

    # ------------------------------------------------------------------ run
    def run(self, duration=240.0):
        self.boot()
        arm_at = 20.0
        if self.scenario == "radio_failsafe":
            for k, t in enumerate((8.0, 12.0, 16.0)):
                self.text(t, "PreArm: Radio failsafe on", 4)
            arm_at = 24.0
        self.heartbeat(0.5)
        step, t, accel = 0, 0.0, (0.0, 0.0, 0.0)
        while t < duration:
            t = round(step * DT, 4)
            step += 1
            if not self.armed and not self.crashed and self.landed_at is None and t >= arm_at:
                self.armed = True
                self.heartbeat(t)
                self.text(t, "Arming motors")
            if self.armed and self.mode == "STABILIZE" and t >= arm_at + 3:
                self.set_mode(t, "AUTO")
                self.text(t, "Mission: 1 Takeoff")
            powered = self.armed
            if self.armed and self.mode != "STABILIZE":
                self.faults(t)
                vn, ve, vu, bearing = self.guidance(t)
                self.control(t, vn, ve, vu, bearing)
                if self.mode == "AUTO":
                    self.advance_mission(t)
            elif self.armed:
                self.s.outputs = [0.1] * 4
                powered = False
            accel = self.physics(t, powered)
            self.prev_v = accel
            self.crash_check(t)
            if self.armed and self.mode == "LAND" and self.s.u <= 0.02 and t > arm_at + 10:
                self.landed_at = self.landed_at or t
                if t - self.landed_at > 2:
                    self.text(t, "Land complete" if not self.crashed else "Disarming")
                    self.disarm(t)
            if step % 10 == 0:
                self.telemetry(t, accel)
            if self.disarmed_at is not None and t - self.disarmed_at > 8:
                if self.crashed:
                    self.text(t, "PreArm: Compasses inconsistent", 4)
                break
        return self.out.bytes()


def simulate(scenario="nominal", seed=1, duration=240.0):
    return Simulator(scenario, seed).run(duration)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate synthetic ArduCopter .tlog captures for AnaTele.")
    parser.add_argument("--scenario", choices=SCENARIOS + ("all",), default="all")
    parser.add_argument("--out", default="simulations", help="Output .tlog file (single scenario) or directory (all)")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--duration", type=float, default=240.0)
    args = parser.parse_args(argv)
    names = SCENARIOS if args.scenario == "all" else (args.scenario,)
    out = Path(args.out)
    for name in names:
        target = out / f"sim_{name}.tlog" if args.scenario == "all" or out.suffix != ".tlog" else out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(simulate(name, args.seed, args.duration))
        print(f"{name:<22} -> {target} ({target.stat().st_size / 1024:.0f} KiB)")


if __name__ == "__main__":
    main()
