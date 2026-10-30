"""Preflight steps and the sequential Runner.

Each step returns a StepResult with one of PASS / WARN / FAIL / SKIPPED / NEEDS_USER and a message.
The Runner executes the steps in order, reports progress through ``on_event(kind, **data)``, pauses for
the human through a Prompter during guided steps (compass and accelerometer calibration), and can be
aborted. The vehicle is never armed: the Runner refuses to start or continue if the vehicle reports armed.
"""
import fnmatch
import json
import queue
import threading
import time
from dataclasses import dataclass, field

from pymavlink.dialects.v20 import ardupilotmega as mavlink

from .link import Link, LinkError, enum_name, firmware_version, result_name

PASS, WARN, FAIL, SKIPPED, NEEDS_USER = "PASS", "WARN", "FAIL", "SKIPPED", "NEEDS_USER"
SEVERITY = {PASS: 0, SKIPPED: 0, WARN: 1, NEEDS_USER: 2, FAIL: 3}

ACCEL_POSITIONS = {1: "LEVEL", 2: "on its LEFT side", 3: "on its RIGHT side", 4: "NOSE DOWN", 5: "NOSE UP",
                   6: "on its BACK (upside down)"}
ACCEL_SUCCESS = mavlink.ACCELCAL_VEHICLE_POS_SUCCESS
ACCEL_FAILED = mavlink.ACCELCAL_VEHICLE_POS_FAILED
ACCEL_TEXTS = (("place vehicle level", 1), ("on its left", 2), ("on its right", 3), ("nose down", 4),
               ("nose up", 5), ("on its back", 6), ("calibration successful", ACCEL_SUCCESS),
               ("calibration failed", ACCEL_FAILED), ("calibration cancelled", ACCEL_FAILED))
MULTIROTORS = {mavlink.MAV_TYPE_QUADROTOR, mavlink.MAV_TYPE_HEXAROTOR, mavlink.MAV_TYPE_OCTOROTOR,
               mavlink.MAV_TYPE_TRICOPTER, mavlink.MAV_TYPE_COAXIAL, mavlink.MAV_TYPE_DODECAROTOR,
               getattr(mavlink, "MAV_TYPE_DECAROTOR", 35)}
FRAME_MOTORS = {1: 4, 2: 6, 3: 8, 4: 8, 5: 6, 7: 3, 12: 12, 14: 10}  # ArduCopter FRAME_CLASS -> motor count
DEFAULT_PROFILE_PATTERNS = ("FRAME_CLASS", "FRAME_TYPE", "BATT_*", "FS_*", "FENCE_*", "RTL_*", "ARMING_*",
                            "MOT_SPIN_*", "MOT_PWM_*", "MOT_BAT_*", "THR_FS*", "RC_FS*", "BRD_SAFETY*")


class Aborted(Exception):
    pass


@dataclass
class StepResult:
    id: str
    title: str
    status: str
    message: str
    details: dict = field(default_factory=dict)
    started: float = 0.0
    finished: float = 0.0

    def as_dict(self):
        return {"id": self.id, "title": self.title, "status": self.status, "message": self.message,
                "details": self.details, "started": self.started, "finished": self.finished,
                "duration_s": round(self.finished - self.started, 2)}


@dataclass
class Options:
    connect: str = ""
    baud: int = 115200
    skip: set = field(default_factory=set)
    props_removed: bool = False
    motor_throttle_pct: float = 6.0
    motor_seconds: float = 2.0
    motor_count: int | None = None
    profile: dict | None = None
    heartbeat_timeout: float = 10.0
    ack_timeout: float = 5.0
    calibration_timeout: float = 30.0
    mag_timeout: float = 300.0
    mag_start_timeout: float = 15.0
    accel_timeout: float = 600.0
    accel_request_timeout: float = 15.0
    health_listen: float = 4.0
    prearm_listen: float = 3.0


# ---------------------------------------------------------------- prompts

class Prompter:
    """Pauses for the human. ``ask`` shows a prompt, ``answer`` polls (non-blocking) for the chosen option."""

    def __init__(self):
        self.answers = queue.Queue()
        self.current = None

    def ask(self, key, text, options):
        self.clear()
        self.current = (key, text, tuple(options))
        self.show(key, text, tuple(options))

    def show(self, key, text, options):
        pass

    def respond(self, option):
        self.answers.put(option)

    def answer(self):
        try:
            return self.answers.get_nowait()
        except queue.Empty:
            return None

    def clear(self):
        self.current = None
        while self.answer() is not None:
            pass
        self.hide()

    def hide(self):
        pass


class AutoPrompter(Prompter):
    """For tests and simulate mode: answers 'Next'/'Yes' immediately unless a scripted answer is given."""

    def __init__(self, answers=None):
        super().__init__()
        self.script = dict(answers or {})
        self.asked = []

    def show(self, key, text, options):
        self.asked.append(key)
        answer = self.script.get(key, next((o for o in ("Next", "Yes") if o in options), None))
        if answer is not None:
            self.respond(answer)


class ConsolePrompter(Prompter):
    """Terminal prompts for headless mode: Enter = Next/Yes, 'n' = No, 'c' = Cancel."""

    def __init__(self, write=print, read=input):
        super().__init__()
        self.write, self.read = write, read
        self.thread = None

    def show(self, key, text, options):
        keys = {"Next": "Enter", "Yes": "Enter/y", "No": "n", "Cancel": "c"}
        self.write(f"\n>>> {text}\n    [{', '.join(f'{keys.get(o, o)} = {o}' for o in options)}]")
        if self.thread is None:
            self.thread = threading.Thread(target=self._reader, daemon=True)
            self.thread.start()

    def _reader(self):
        while True:
            try:
                line = self.read().strip().lower()
            except (EOFError, OSError):
                return
            if self.current is None:
                continue
            options = self.current[2]
            if line in ("c", "cancel") and "Cancel" in options:
                self.respond("Cancel")
            elif line in ("n", "no") and "No" in options:
                self.respond("No")
            elif line in ("", "y", "yes", "next"):
                choice = next((o for o in ("Next", "Yes") if o in options), None)
                if choice:
                    self.respond(choice)


# ---------------------------------------------------------------- context and helpers

class Context:
    def __init__(self, runner):
        self.runner = runner
        self.options = runner.options
        self.prompter = runner.prompter
        self.link = runner.link
        self.vehicle = {}
        self.ready_confirmed = False

    def log(self, text):
        self.runner.emit("log", text=text)

    def progress(self, fraction, text=""):
        self.runner.emit("progress", fraction=max(0.0, min(1.0, fraction)), text=text)

    def check_abort(self):
        if self.runner.aborted.is_set():
            raise Aborted()

    def now(self):
        return self.link.clock()

    def ask(self, key, text, options):
        if not self.prompter:
            return None
        self.prompter.ask(key, text, options)
        return True

    def wait_answer(self, timeout, while_waiting=None):
        """Poll the prompter while pumping the link; returns the option chosen or None on timeout."""
        deadline = self.now() + timeout
        while self.now() < deadline:
            self.check_abort()
            answer = self.prompter.answer()
            if answer is not None:
                self.prompter.clear()
                return answer
            msg = self.link.wait(["COMMAND_LONG", "STATUSTEXT", "HEARTBEAT"], 0.2)
            if msg is not None and while_waiting:
                while_waiting(msg)
        self.prompter.clear()
        return None

    def confirm_still(self):
        if self.ready_confirmed or not self.prompter:
            return True
        self.ask("still", "Put the vehicle on a level, stable surface and do not touch it during gyro, "
                          "barometer and level calibration. Press Next to start.", ("Next", "Cancel"))
        answer = self.wait_answer(self.options.accel_timeout)
        self.ready_confirmed = answer == "Next"
        return self.ready_confirmed


def result(status, message, **details):
    return status, message, details


# ---------------------------------------------------------------- steps

def step_connect(ctx):
    opt = ctx.options
    if ctx.link is None:
        ctx.log(f"Opening {opt.connect} (baud {opt.baud})")
        ctx.link = ctx.runner.link = Link.open(opt.connect, opt.baud)
    hb = ctx.link.wait_heartbeat(opt.heartbeat_timeout)
    if hb is None:
        return result(FAIL, f"No HEARTBEAT within {opt.heartbeat_timeout:.0f} s. Check the port/baud rate, "
                            "that no other program holds the port, and the telemetry radio link.")
    link = ctx.link
    info = {"system_id": link.target_system, "component_id": link.target_component,
            "vehicle_type": enum_name("MAV_TYPE", hb.type, "MAV_TYPE_"),
            "autopilot": enum_name("MAV_AUTOPILOT", hb.autopilot, "MAV_AUTOPILOT_"),
            "mav_type": hb.type, "armed": link.armed, "connection": link.description or opt.connect}
    try:
        link.request_streams()
        version = link.request_message(mavlink.MAVLINK_MSG_ID_AUTOPILOT_VERSION, timeout=opt.ack_timeout)
    except LinkError:
        version = None
    if version is not None:
        info["firmware"] = firmware_version(version.flight_sw_version)
        info["board_version"] = version.board_version
        git = bytes(version.flight_custom_version).rstrip(b"\x00").decode("ascii", "replace")
        if git:
            info["firmware_git"] = git
    ctx.vehicle.update(info)
    text = (f"{info['vehicle_type']} / {info['autopilot']}, firmware {info.get('firmware', 'unknown')}, "
            f"system {info['system_id']}")
    if link.armed:
        return result(FAIL, f"Vehicle is ARMED ({text}). Disarm it before running preflight; nothing was sent.", **info)
    if hb.autopilot != mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA:
        return result(WARN, f"Connected to a non-ArduPilot autopilot ({text}); steps follow ArduPilot behaviour.", **info)
    if version is None:
        return result(WARN, f"Connected ({text}); AUTOPILOT_VERSION not received.", **info)
    return result(PASS, f"Connected: {text}, disarmed.", **info)


def calibration(ctx, label, params):
    since = ctx.now()
    res = ctx.link.command(mavlink.MAV_CMD_PREFLIGHT_CALIBRATION, params, timeout=ctx.options.calibration_timeout)
    texts = ctx.link.texts_since(since)
    return res, texts


def step_gyro_baro(ctx):
    if not ctx.confirm_still():
        return result(NEEDS_USER, "Cancelled before calibration: keep the vehicle still and level, then run again.")
    # ArduPilot handles param1 (gyro) and param3 (baro) in separate branches and returns after the first one,
    # so they are sent as two commands.
    ctx.progress(0.1, "Gyro calibration: keep still")
    gyro, texts = calibration(ctx, "gyro", [1, 0, 0, 0, 0, 0, 0])
    ctx.progress(0.6, "Barometer calibration")
    baro, texts2 = calibration(ctx, "baro", [0, 0, 1, 0, 0, 0, 0])
    details = {"gyro_result": result_name(gyro), "baro_result": result_name(baro), "statustext": texts + texts2}
    if gyro == mavlink.MAV_RESULT_ACCEPTED and baro == mavlink.MAV_RESULT_ACCEPTED:
        return result(PASS, "Gyro and barometer calibrated.", **details)
    if mavlink.MAV_RESULT_ACCEPTED in (gyro, baro):
        return result(WARN, f"Gyro: {result_name(gyro)}, barometer: {result_name(baro)}.", **details)
    return result(FAIL, f"Gyro: {result_name(gyro)}, barometer: {result_name(baro)}. "
                        "The vehicle must be disarmed and completely still.", **details)


def step_level(ctx):
    if not ctx.confirm_still():
        return result(NEEDS_USER, "Cancelled: place the vehicle level and run again.")
    ctx.progress(0.2, "Level calibration: keep level and still")
    res, texts = calibration(ctx, "level", [0, 0, 0, 0, 2, 0, 0])
    details = {"result": result_name(res), "statustext": texts}
    if res == mavlink.MAV_RESULT_ACCEPTED:
        trim = next((t for t in texts if t.lower().startswith("trim ok")), "")
        return result(PASS, f"Level (AHRS trim) calibrated. {trim}".strip(), **details)
    hint = next((t for t in texts if "trim" in t.lower()), "")
    return result(FAIL, f"Level calibration {result_name(res)}. {hint or 'Vehicle must be level (within 10 deg) and still; accelerometers must be calibrated first.'}",
                  **details)


def step_compass(ctx):
    opt, link = ctx.options, ctx.link
    if not ctx.prompter:
        return result(NEEDS_USER, "Compass calibration needs a person to rotate the vehicle; run interactively.")
    ctx.ask("compass", "COMPASS CALIBRATION: pick the vehicle up and slowly rotate it so that every side "
                       "(top, bottom, nose, tail, left, right) points at the ground in turn, until progress reaches "
                       "100%. Keep away from metal, cars and laptops.", ("Cancel",))
    since = ctx.now()
    res = link.command(mavlink.MAV_CMD_DO_START_MAG_CAL, [0, 1, 1, 0, 0, 0, 0], timeout=opt.ack_timeout)
    if res != mavlink.MAV_RESULT_ACCEPTED:
        ctx.prompter.clear()
        texts = link.texts_since(since)
        return result(FAIL, f"Compass calibration did not start ({result_name(res)}). {' '.join(texts[-3:])}".strip(),
                      result=result_name(res), statustext=texts)
    started = ctx.now()
    deadline = started + opt.mag_timeout
    progress, reports, failed_at, mask = {}, {}, {}, 0
    last_progress = None
    try:
        while True:
            ctx.check_abort()
            if ctx.prompter.answer() == "Cancel":
                link.command(mavlink.MAV_CMD_DO_CANCEL_MAG_CAL, [0], timeout=opt.ack_timeout)
                return result(NEEDS_USER, "Compass calibration cancelled by the user; previous calibration kept.",
                              progress=progress, reports=reports)
            now = ctx.now()
            if now > deadline:
                link.command(mavlink.MAV_CMD_DO_CANCEL_MAG_CAL, [0], timeout=opt.ack_timeout)
                return result(FAIL, f"Compass calibration did not finish within {opt.mag_timeout:.0f} s and was cancelled.",
                              progress=progress, reports=reports)
            if last_progress is None and not reports and now - started > opt.mag_start_timeout:
                link.command(mavlink.MAV_CMD_DO_CANCEL_MAG_CAL, [0], timeout=opt.ack_timeout)
                return result(FAIL, "Calibration was accepted but no MAG_CAL_PROGRESS arrived; the telemetry stream "
                                    "may be disabled (SRn_EXTRA3). Cancelled.")
            msg = link.wait(["MAG_CAL_PROGRESS", "MAG_CAL_REPORT"], 0.25)
            if msg is not None and msg.get_type() == "MAG_CAL_PROGRESS":
                last_progress = now
                mask |= msg.cal_mask
                progress[msg.compass_id] = {"pct": msg.completion_pct, "attempt": msg.attempt}
                failed_at.pop(msg.compass_id, None)
                reports.pop(msg.compass_id, None)
                ctx.progress(sum(p["pct"] for p in progress.values()) / (100.0 * max(1, bin(mask).count("1"))),
                             "Rotate the vehicle: " + ", ".join(f"compass {i + 1}: {p['pct']}%"
                                                                for i, p in sorted(progress.items())))
            elif msg is not None and (msg.compass_id in progress or mask >> msg.compass_id & 1):
                # reports for a compass not in this calibration are left over from an earlier one
                mask |= msg.cal_mask
                status = enum_name("MAG_CAL_STATUS", msg.cal_status, "MAG_CAL_")
                reports[msg.compass_id] = {"status": status, "fitness": round(float(msg.fitness), 2),
                                           "autosaved": bool(msg.autosaved),
                                           "offsets": [round(float(v), 1) for v in (msg.ofs_x, msg.ofs_y, msg.ofs_z)]}
                if msg.cal_status != mavlink.MAG_CAL_SUCCESS:
                    failed_at.setdefault(msg.compass_id, now)
            expected = [i for i in range(8) if mask >> i & 1] or list(progress)
            if expected and all(i in reports for i in expected):
                # a failed compass is retried by the vehicle (retry=1): wait briefly for a new attempt
                if all(now - failed_at[i] > 3.0 for i in failed_at):
                    break
    finally:
        ctx.prompter.clear()
    good = [i for i, r in reports.items() if r["status"] == "SUCCESS"]
    summary = ", ".join(f"compass {i + 1}: {r['status']} (fitness {r['fitness']})" for i, r in sorted(reports.items()))
    if len(good) == len(reports):
        unsaved = [i for i in good if not reports[i]["autosaved"]]
        if unsaved:
            return result(WARN, f"{summary}. Not saved for compass {', '.join(str(i + 1) for i in unsaved)}.", reports=reports)
        return result(PASS, f"{summary}. Reboot the vehicle to apply the new offsets.", reports=reports, reboot_required=True)
    return result(FAIL, f"{summary}. Recalibrate away from magnetic interference; check compass orientation.",
                  reports=reports)


def accel_signal(msg):
    if msg.get_type() == "COMMAND_LONG" and msg.command == mavlink.MAV_CMD_ACCELCAL_VEHICLE_POS:
        return int(msg.param1)
    if msg.get_type() == "STATUSTEXT":
        text = str(msg.text).lower()
        for needle, position in ACCEL_TEXTS:
            if needle in text:
                return position
    return None


def step_accel(ctx):
    opt, link = ctx.options, ctx.link
    if not ctx.prompter:
        return result(NEEDS_USER, "Accelerometer calibration needs a person to place the vehicle; run interactively.")
    since = ctx.now()
    ctx.progress(0.02, "Starting accelerometer calibration (gyro calibration first; keep still)")
    res = link.command(mavlink.MAV_CMD_PREFLIGHT_CALIBRATION, [0, 0, 0, 0, 1, 0, 0], timeout=opt.calibration_timeout)
    if res != mavlink.MAV_RESULT_ACCEPTED:
        return result(FAIL, f"Accelerometer calibration did not start ({result_name(res)}).",
                      statustext=link.texts_since(since))
    deadline = ctx.now() + opt.accel_timeout
    done = []
    state = {"pos": None, "requested": False}

    def watch(msg):
        position = accel_signal(msg)
        if position is None:
            return
        if position in (ACCEL_SUCCESS, ACCEL_FAILED):
            # after a finished calibration the vehicle keeps repeating its result: ignore one left over from an earlier run
            if msg.get_type() == "COMMAND_LONG" and not state["requested"]:
                return
        else:
            state["requested"] = True
        state["pos"] = position

    def wait_request(previous):
        limit = min(deadline, ctx.now() + opt.accel_request_timeout)
        while ctx.now() < limit:
            ctx.check_abort()
            msg = link.wait(["COMMAND_LONG", "STATUSTEXT"], 0.25)
            if msg is not None:
                watch(msg)
            if state["pos"] is not None and state["pos"] != previous:
                return state["pos"]
        return None

    position = wait_request(None)
    while True:
        if position is None:
            return result(FAIL, "The vehicle stopped requesting positions (no ACCELCAL_VEHICLE_POS). Reboot the vehicle "
                                "and try again.", positions_done=done, statustext=link.texts_since(since))
        if position == ACCEL_SUCCESS:
            ctx.progress(1.0, "Accelerometer calibration successful")
            return result(PASS, "6-position accelerometer calibration successful. Reboot the vehicle to apply.",
                          positions_done=done, statustext=link.texts_since(since), reboot_required=True)
        if position == ACCEL_FAILED:
            return result(FAIL, "Accelerometer calibration FAILED (the vehicle moved, or a position was wrong). "
                                "Reboot and repeat.", positions_done=done, statustext=link.texts_since(since))
        name = ACCEL_POSITIONS.get(position, f"position {position}")
        ctx.progress(len(done) / 6.0, f"Place vehicle {name}")
        ctx.ask(f"accel_{position}", f"ACCELEROMETER CALIBRATION ({position}/6): place the vehicle {name} and hold it "
                                     "still, then press Next.", ("Next", "Cancel"))
        answer = ctx.wait_answer(max(0.0, deadline - ctx.now()), watch)
        if state["pos"] == ACCEL_FAILED:
            position = ACCEL_FAILED
            continue
        if answer != "Next":
            why = "cancelled by the user" if answer == "Cancel" else "timed out waiting for the user"
            return result(NEEDS_USER, f"Accelerometer calibration {why}. The vehicle keeps waiting for positions: "
                                      "reboot it to abandon the calibration.", positions_done=done)
        ack = link.command(mavlink.MAV_CMD_ACCELCAL_VEHICLE_POS, [position], timeout=opt.ack_timeout)
        if ack != mavlink.MAV_RESULT_ACCEPTED:
            ctx.log(f"Vehicle answered {result_name(ack)} for position {name}; asking again.")
            position = wait_request(None) if state["pos"] is None else state["pos"]
            continue
        done.append(ACCEL_POSITIONS.get(position, str(position)))
        ctx.progress(len(done) / 6.0, f"Sampling {name}: hold still")
        position = wait_request(position)


def sensor_names(bits):
    names = []
    for value, entry in sorted(mavlink.enums["MAV_SYS_STATUS_SENSOR"].items()):
        if value and bits & value and value & (value - 1) == 0:
            names.append(entry.name.replace("MAV_SYS_STATUS_", "").replace("SENSOR_", ""))
    return names


def step_health(ctx):
    opt, link = ctx.options, ctx.link
    since = ctx.now()
    link.request_streams()
    for t in ("SYS_STATUS", "EKF_STATUS_REPORT", "GPS_RAW_INT", "BATTERY_STATUS", "RC_CHANNELS"):
        link.latest.pop(t, None)
    ctx.progress(0.1, "Listening to telemetry")
    link.pump(opt.health_listen)
    ctx.progress(0.6, "Running pre-arm checks")
    prearm_cmd = link.command(mavlink.MAV_CMD_RUN_PREARM_CHECKS, [], timeout=opt.ack_timeout)
    link.pump(opt.prearm_listen)
    fails, warns, details = [], [], {"run_prearm_checks": result_name(prearm_cmd)}
    sys_status = link.latest.get("SYS_STATUS")
    if sys_status is None:
        fails.append("no SYS_STATUS received (telemetry streams disabled?)")
    else:
        enabled = sys_status.onboard_control_sensors_present & sys_status.onboard_control_sensors_enabled
        unhealthy = enabled & ~sys_status.onboard_control_sensors_health
        prearm_bit = mavlink.MAV_SYS_STATUS_PREARM_CHECK
        bad = sensor_names(unhealthy & ~prearm_bit)
        details["unhealthy_sensors"] = bad
        if bad:
            fails.append("unhealthy: " + ", ".join(bad))
        if enabled & prearm_bit:
            details["prearm_ok"] = not unhealthy & prearm_bit
            if unhealthy & prearm_bit:
                fails.append("pre-arm checks failing")
        if sys_status.voltage_battery not in (0, 65535):
            details["battery_v"] = round(sys_status.voltage_battery / 1000.0, 2)
        if sys_status.battery_remaining >= 0:
            details["battery_pct"] = sys_status.battery_remaining
    battery = link.latest.get("BATTERY_STATUS")
    if battery is not None:
        cells = [v for v in battery.voltages if v not in (0, 65535)]
        if cells and "battery_v" not in details:
            details["battery_v"] = round(sum(cells) / 1000.0, 2)
        if battery.battery_remaining >= 0:
            details["battery_pct"] = battery.battery_remaining
    if "battery_v" not in details:
        warns.append("battery voltage unknown")
    elif details.get("battery_pct", 100) < 30:
        warns.append(f"battery at {details['battery_pct']}%")
    ekf = link.latest.get("EKF_STATUS_REPORT")
    if ekf is None:
        warns.append("no EKF_STATUS_REPORT")
    else:
        variances = {k: round(float(getattr(ekf, k)), 3) for k in
                     ("velocity_variance", "pos_horiz_variance", "pos_vert_variance", "compass_variance")}
        details["ekf"] = {"flags": ekf.flags, **variances}
        if not ekf.flags & mavlink.EKF_ATTITUDE:
            fails.append("EKF has no attitude solution")
        if not ekf.flags & mavlink.EKF_POS_HORIZ_ABS:
            warns.append("EKF has no absolute horizontal position")
        high = [k for k, v in variances.items() if v >= 0.8]
        mid = [k for k, v in variances.items() if 0.5 <= v < 0.8]
        if high:
            fails.append("EKF variance high: " + ", ".join(high))
        if mid:
            warns.append("EKF variance elevated: " + ", ".join(mid))
    gps = link.latest.get("GPS_RAW_INT")
    if gps is None:
        warns.append("no GPS_RAW_INT")
    else:
        hdop = None if gps.eph == 65535 else gps.eph / 100.0
        details["gps"] = {"fix": enum_name("GPS_FIX_TYPE", gps.fix_type, "GPS_FIX_TYPE_"),
                          "satellites": gps.satellites_visible, "hdop": hdop}
        if gps.fix_type < 3:
            warns.append(f"GPS fix {details['gps']['fix']}")
        if gps.satellites_visible < 6:
            warns.append(f"only {gps.satellites_visible} satellites")
        if hdop is not None and hdop > 2.0:
            warns.append(f"HDOP {hdop:.1f}")
    rc = link.latest.get("RC_CHANNELS")
    if rc is None or rc.chancount == 0:
        warns.append("no RC_CHANNELS (receiver not connected or not bound?)")
    else:
        details["rc"] = {"channels": rc.chancount, "rssi": rc.rssi}
    prearm = sorted({t for t in link.texts_since(since) if t.lower().startswith("prearm")})
    details["prearm_messages"] = prearm
    if prearm:
        fails.extend(prearm)
    if fails:
        return result(FAIL, "; ".join(fails + warns), **details)
    if warns:
        return result(WARN, "; ".join(warns), **details)
    return result(PASS, f"Sensors healthy, EKF good, GPS {details['gps']['fix']} with {details['gps']['satellites']} "
                        f"satellites, battery {details.get('battery_v')} V, no PreArm messages.", **details)


def step_motors(ctx):
    opt, link = ctx.options, ctx.link
    if not opt.props_removed:
        return result(SKIPPED, "Motor test not run: confirm that ALL PROPELLERS ARE REMOVED to enable it.")
    mav_type = ctx.vehicle.get("mav_type", link.heartbeat.type if link.heartbeat else None)
    if mav_type not in MULTIROTORS:
        return result(SKIPPED, f"Motor test is only run for multicopters (vehicle is {ctx.vehicle.get('vehicle_type')}).")
    count, note = opt.motor_count, ""
    if not count:
        frame = link.param_read("FRAME_CLASS", timeout=opt.ack_timeout / 2)
        count = FRAME_MOTORS.get(int(frame)) if frame is not None else None
        if not count:
            count, note = 4, " (FRAME_CLASS unknown, assumed 4 motors)"
    throttle = max(1.0, min(15.0, opt.motor_throttle_pct))
    seconds = max(0.5, min(5.0, opt.motor_seconds))
    results = {}
    for motor in range(1, count + 1):
        ctx.check_abort()
        letter = chr(ord("A") + motor - 1)
        ctx.progress((motor - 1) / count, f"Motor {letter} ({motor}/{count}) at {throttle:.0f}% for {seconds:.0f} s")
        since = ctx.now()
        res = link.command(mavlink.MAV_CMD_DO_MOTOR_TEST, [motor, mavlink.MOTOR_TEST_THROTTLE_PERCENT, throttle,
                                                           seconds, 1, 0, 0], timeout=opt.ack_timeout)
        results[letter] = result_name(res)
        if res != mavlink.MAV_RESULT_ACCEPTED:
            texts = link.texts_since(since)
            wait_disarmed(ctx)
            return result(FAIL, f"Motor test refused for motor {letter}: {result_name(res)}. {' '.join(texts[-2:])}".strip(),
                          motors=results, statustext=texts)
        link.pump(seconds + 0.5)
    wait_disarmed(ctx)
    details = {"motors": results, "count": count, "throttle_pct": throttle, "seconds": seconds}
    if ctx.prompter:
        ctx.ask("motors_ok", f"Did the {count} motors spin one at a time in test order (A, B, C...) and in the correct "
                             "direction?", ("Yes", "No"))
        answer = ctx.wait_answer(120)
        details["user_confirmed"] = answer
        if answer == "No":
            return result(FAIL, "User reported wrong motor order or direction: check motor wiring / SERVOn_FUNCTION "
                                "and propeller direction." + note, **details)
        if answer == "Yes":
            return result(PASS, f"{count} motors spun in order at {throttle:.0f}% and were confirmed by the user.{note}",
                          **details)
    return result(WARN, f"{count} motors commanded at {throttle:.0f}%; order and direction not confirmed.{note}", **details)


def wait_disarmed(ctx):
    # the motor test arms the outputs internally; wait for the vehicle to report disarmed again
    deadline = ctx.now() + 10
    while ctx.now() < deadline:
        hb = ctx.link.wait_heartbeat(1.5)
        if hb is not None and not ctx.link.armed:
            return True
    return False


def compare_params(expected, actual, tolerance=1e-4):
    mismatches, missing = [], []
    for name, value in expected.items():
        have = actual.get(name)
        if have is None:
            missing.append(name)
        elif abs(have - float(value)) > max(tolerance, abs(float(value)) * tolerance):
            mismatches.append({"name": name, "expected": float(value), "actual": have})
    return mismatches, missing


def step_params(ctx):
    opt, link = ctx.options, ctx.link
    profile = opt.profile
    if not profile or not profile.get("params"):
        return result(SKIPPED, "No parameter profile loaded.")
    expected = profile["params"]
    actual = {}
    if len(expected) > 40:
        actual = link.param_list(progress=lambda f: ctx.progress(f, "Downloading parameters"))
    else:
        for index, name in enumerate(expected):
            ctx.check_abort()
            ctx.progress(index / len(expected), f"Reading {name}")
            value = link.param_read(name, timeout=opt.ack_timeout / 2)
            if value is not None:
                actual[name] = value
    mismatches, missing = compare_params(expected, actual, float(profile.get("tolerance", 1e-4)))
    details = {"profile": profile.get("name", ""), "checked": len(expected), "mismatches": mismatches, "missing": missing}
    if mismatches:
        text = ", ".join(f"{m['name']}={m['actual']:g} (expected {m['expected']:g})" for m in mismatches[:8])
        more = f" and {len(mismatches) - 8} more" if len(mismatches) > 8 else ""
        return result(FAIL, f"{len(mismatches)} parameter(s) differ from profile: {text}{more}.", **details)
    if missing:
        return result(WARN, f"Not found on vehicle: {', '.join(missing)}.", **details)
    return result(PASS, f"All {len(expected)} parameters match profile '{profile.get('name', '')}'.", **details)


STEPS = [
    ("connect", "Connect and heartbeat", step_connect),
    ("gyro_baro", "Gyro and barometer calibration", step_gyro_baro),
    ("level", "Level (board) calibration", step_level),
    ("compass", "Compass calibration (guided)", step_compass),
    ("accel", "Accelerometer 6-position calibration (guided)", step_accel),
    ("health", "Pre-arm health", step_health),
    ("motors", "Motor test (props off)", step_motors),
    ("params", "Parameters vs profile", step_params),
]
STEP_IDS = [s[0] for s in STEPS]


# ---------------------------------------------------------------- profiles

def load_profile(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if "params" not in data or not isinstance(data["params"], dict):
        raise ValueError("Profile JSON needs a 'params' object of NAME: value")
    data["params"] = {str(k): float(v) for k, v in data["params"].items()}
    return data


def build_profile(values, name="profile", patterns=DEFAULT_PROFILE_PATTERNS):
    selected = {k: v for k, v in sorted(values.items()) if any(fnmatch.fnmatchcase(k, p) for p in patterns)}
    return {"name": name, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "tolerance": 1e-4,
            "patterns": list(patterns), "params": selected}


def save_profile(link, path, name="profile", patterns=DEFAULT_PROFILE_PATTERNS, progress=None):
    values = link.param_list(progress=progress)
    if not values:
        raise LinkError("No parameters received from the vehicle")
    profile = build_profile(values, name, patterns)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(profile, handle, indent=1)
    return profile


# ---------------------------------------------------------------- runner

class Runner:
    def __init__(self, link=None, options=None, prompter=None, on_event=None):
        self.link = link
        self.options = options or Options()
        self.prompter = prompter
        self.on_event = on_event
        self.aborted = threading.Event()
        self.results = []
        self.context = None

    def emit(self, kind, **data):
        if self.on_event:
            self.on_event(kind, **data)

    def abort(self):
        self.aborted.set()
        if self.prompter:
            self.prompter.respond("Cancel")

    def run(self, only=None):
        """Run all steps (or ``only`` the given ids) in order. Returns the list of StepResult."""
        ctx = self.context = Context(self)
        self.results = []
        stop_reason = None
        if self.link is not None:
            self.link.on_text = lambda severity, text: self.emit("statustext", severity=severity, text=text)
        for index, (step_id, title, func) in enumerate(STEPS):
            started = time.time()
            if only is not None and step_id not in only and step_id != "connect":
                continue
            if stop_reason:
                status, message, details = SKIPPED, stop_reason, {}
            elif step_id in self.options.skip and step_id != "connect":
                status, message, details = SKIPPED, "Skipped on request.", {}
            else:
                self.emit("step_started", index=index, id=step_id, title=title)
                try:
                    if step_id != "connect" and ctx.link.armed:
                        status, message, details = FAIL, "Vehicle became ARMED: preflight stopped.", {}
                    else:
                        status, message, details = func(ctx)
                except Aborted:
                    status, message, details = SKIPPED, "Run aborted by the user.", {}
                except LinkError as error:
                    status, message, details = FAIL, f"Link error: {error}", {}
                except Exception as error:  # a step bug must not leave the vehicle mid-sequence silently
                    status, message, details = FAIL, f"Internal error: {type(error).__name__}: {error}", {}
                if ctx.link is not None and self.link is None:
                    self.link = ctx.link
                if self.link is not None and self.link.on_text is None:
                    self.link.on_text = lambda severity, text: self.emit("statustext", severity=severity, text=text)
                if self.aborted.is_set():
                    stop_reason = "Run aborted by the user."
                elif step_id == "connect" and status == FAIL:
                    stop_reason = "Not run: connection step failed (or vehicle armed)."
                elif status == FAIL and "ARMED" in message:
                    stop_reason = "Not run: vehicle armed."
            res = StepResult(step_id, title, status, message, details, started, time.time())
            self.results.append(res)
            self.emit("step_finished", index=index, result=res)
        self.emit("finished", results=self.results)
        return self.results

    @property
    def vehicle(self):
        return self.context.vehicle if self.context else {}


def overall(results):
    worst = max((SEVERITY[r.status] for r in results), default=0)
    return {0: PASS, 1: WARN, 2: NEEDS_USER, 3: FAIL}[worst]
