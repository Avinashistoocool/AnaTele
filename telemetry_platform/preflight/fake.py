"""FakeVehicle: a small simulated ArduCopter used by the tests and by ``anatele preflight --simulate``.

It speaks real MAVLink v2 messages (encoded and decoded with pymavlink) through the same small surface as a
``mavutil`` connection: ``recv_match``, ``mav.*_send`` and ``close``. Time is virtual by default (tests run in
milliseconds); ``realtime=True`` paces it with the wall clock for the GUI/CLI simulate mode.

Behaviour follows the ArduPilot sources: PREFLIGHT_CALIBRATION is refused while armed; the 6-position
accelerometer calibration repeatedly sends COMMAND_LONG(MAV_CMD_ACCELCAL_VEHICLE_POS, step) plus a
"Place vehicle ..." STATUSTEXT and advances when the GCS answers with the same position; compass calibration
streams MAG_CAL_PROGRESS then MAG_CAL_REPORT; the motor test arms the outputs while it runs.
"""
import heapq
import itertools
import time

from pymavlink.dialects.v20 import ardupilotmega as mavlink

ACCEL_TEXT = {1: "level", 2: "on its LEFT side", 3: "on its RIGHT side", 4: "nose DOWN", 5: "nose UP",
              6: "on its BACK"}
DEFAULT_PARAMS = {
    "FRAME_CLASS": 1, "FRAME_TYPE": 1, "BATT_MONITOR": 4, "BATT_CAPACITY": 5200, "BATT_LOW_VOLT": 14.0,
    "BATT_CRT_VOLT": 13.6, "BATT_FS_LOW_ACT": 2, "BATT_FS_CRT_ACT": 1, "FS_THR_ENABLE": 1, "FS_THR_VALUE": 975,
    "FS_GCS_ENABLE": 0, "FS_EKF_ACTION": 1, "FENCE_ENABLE": 1, "RTL_ALT": 1500, "ARMING_CHECK": 1,
    "MOT_SPIN_ARM": 0.1, "MOT_SPIN_MIN": 0.15, "COMPASS_USE": 1, "INS_ACC_ID": 2753028, "SYSID_THISMAV": 1,
}


class _Mav:
    """The GCS-to-vehicle half: what pymavlink's ``conn.mav`` offers, routed into the fake vehicle."""

    def __init__(self, vehicle):
        self.vehicle = vehicle

    def command_long_send(self, target_system, target_component, command, confirmation, *params):
        self.vehicle.receive("command", command, list(params))

    def param_request_read_send(self, target_system, target_component, param_id, param_index):
        name = param_id.decode() if isinstance(param_id, bytes) else param_id
        self.vehicle.receive("param_read", name, param_index)

    def param_request_list_send(self, target_system, target_component):
        self.vehicle.receive("param_list")

    def request_data_stream_send(self, target_system, target_component, stream_id, rate, start):
        self.vehicle.receive("streams", bool(start))


class FakeVehicle:
    def __init__(self, armed=False, realtime=False, mag_fail=False, mag_compasses=2, accel_fail_at=None,
                 prearm_texts=(), unresponsive=(), silent=False, params=None, gps_sats=14, battery_v=16.4,
                 battery_pct=92, motor_test_result=mavlink.MAV_RESULT_ACCEPTED, mav_type=mavlink.MAV_TYPE_QUADROTOR):
        self.armed = armed
        self.realtime = realtime
        self.mag_fail = mag_fail
        self.mag_compasses = mag_compasses
        self.accel_fail_at = accel_fail_at
        self.prearm_texts = list(prearm_texts)
        self.unresponsive = set(unresponsive)
        self.silent = silent
        self.params = dict(DEFAULT_PARAMS if params is None else params)
        self.gps_sats, self.battery_v, self.battery_pct = gps_sats, battery_v, battery_pct
        self.motor_test_result = motor_test_result
        self.mav_type = mav_type
        self.mav = _Mav(self)
        self.enc = mavlink.MAVLink(None, srcSystem=1, srcComponent=1)
        self.dec = mavlink.MAVLink(None)
        self.t = 0.0
        self.start = time.monotonic()
        self.events, self.counter = [], itertools.count()
        self.outbox = []
        self.commands = []
        self.streams = False
        self.accel_step = 0
        self.accel_result = None
        self.mag = None
        self.motor_until = 0.0
        self.closed = False
        self.next = {"heartbeat": 0.0, "telemetry": 0.0, "accel": 0.0, "mag": 0.0}

    # -- clock and transport

    def clock(self):
        return time.monotonic() - self.start if self.realtime else self.t

    def close(self):
        self.closed = True

    def later(self, delay, action, *args):
        heapq.heappush(self.events, (self.clock() + delay, next(self.counter), action, args))

    def receive(self, kind, *args):
        self.later(0.02, getattr(self, "on_" + kind), *args)

    def send(self, name, *fields):
        msg = getattr(self.enc, name + "_encode")(*fields)
        self.outbox.append(self.dec.decode(bytearray(msg.pack(self.enc))))

    def text(self, severity, text):
        self.send("statustext", severity, text.encode()[:50])

    def ack(self, command, result):
        self.send("command_ack", command, result, 0, 0, 255, 190)

    def recv_match(self, blocking=True, timeout=None, type=None, condition=None):
        end = self.clock() + (timeout or 0)
        while True:
            now = self.clock()
            self.run_due(now)
            if self.outbox:
                return self.outbox.pop(0)
            if now >= end or not blocking:
                return None
            upcoming = min([end, *self.next.values(), *(e[0] for e in self.events[:1])])
            step = max(upcoming - now, 0.001)
            if self.realtime:
                time.sleep(min(step, 0.05))
            else:
                self.t = now + step

    def run_due(self, now):
        while self.events and self.events[0][0] <= now:
            _, _, action, args = heapq.heappop(self.events)
            action(*args)
        if self.motor_until and now >= self.motor_until:
            self.motor_until = 0.0
            self.armed = False
            self.text(mavlink.MAV_SEVERITY_INFO, "finished motor test")
        if now >= self.next["heartbeat"]:
            self.next["heartbeat"] = now + 1.0
            if not self.silent:
                base = mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED | (mavlink.MAV_MODE_FLAG_SAFETY_ARMED if self.armed else 0)
                self.send("heartbeat", self.mav_type, mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA, base, 0,
                          mavlink.MAV_STATE_ACTIVE if self.armed else mavlink.MAV_STATE_STANDBY, 3)
        if now >= self.next["telemetry"]:
            self.next["telemetry"] = now + 0.5
            if self.streams:
                self.telemetry()
        if now >= self.next["accel"]:
            self.next["accel"] = now + 1.0
            position = self.accel_step or self.accel_result
            if position:
                self.send("command_long", 0, 0, mavlink.MAV_CMD_ACCELCAL_VEHICLE_POS, 0, position, 0, 0, 0, 0, 0, 0)
        if now >= self.next["mag"]:
            self.next["mag"] = now + 0.25
            if self.mag:
                self.mag_tick()

    # -- telemetry

    def telemetry(self):
        sensors = (mavlink.MAV_SYS_STATUS_SENSOR_3D_GYRO | mavlink.MAV_SYS_STATUS_SENSOR_3D_ACCEL
                   | mavlink.MAV_SYS_STATUS_SENSOR_3D_MAG | mavlink.MAV_SYS_STATUS_SENSOR_ABSOLUTE_PRESSURE
                   | mavlink.MAV_SYS_STATUS_SENSOR_GPS | mavlink.MAV_SYS_STATUS_SENSOR_RC_RECEIVER
                   | mavlink.MAV_SYS_STATUS_AHRS | mavlink.MAV_SYS_STATUS_PREARM_CHECK)
        health = sensors & ~(mavlink.MAV_SYS_STATUS_PREARM_CHECK if self.prearm_texts else 0)
        self.send("sys_status", sensors, sensors, health, 250, int(self.battery_v * 1000), 120, self.battery_pct,
                  0, 0, 0, 0, 0, 0)
        flags = (mavlink.EKF_ATTITUDE | mavlink.EKF_VELOCITY_HORIZ | mavlink.EKF_VELOCITY_VERT
                 | mavlink.EKF_POS_HORIZ_REL | mavlink.EKF_POS_HORIZ_ABS | mavlink.EKF_POS_VERT_ABS
                 | mavlink.EKF_PRED_POS_HORIZ_REL | mavlink.EKF_PRED_POS_HORIZ_ABS)
        self.send("ekf_status_report", flags, 0.05, 0.08, 0.04, 0.03, 0.0)
        self.send("gps_raw_int", int(self.clock() * 1e6), 3 if self.gps_sats >= 6 else 1, -353632620, 1491652370,
                  584000, 75, 120, 0, 0, self.gps_sats)
        self.send("battery_status", 0, 0, 0, 2500, [4100] * 4 + [65535] * 6, 120, 300, -1, self.battery_pct)
        self.send("rc_channels", int(self.clock() * 1000), 16, *([1500] * 16), 0, 0, 255)

    # -- GCS messages

    def on_streams(self, start):
        self.streams = start

    def on_param_read(self, name, index):
        names = list(self.params)
        if name:
            if name in self.params:
                self.param_value(names.index(name))
        elif 0 <= index < len(names):
            self.param_value(index)

    def param_value(self, index):
        name = list(self.params)[index]
        self.send("param_value", name.encode(), float(self.params[name]), mavlink.MAV_PARAM_TYPE_REAL32,
                  len(self.params), index)

    def on_param_list(self):
        for index in range(len(self.params)):
            self.later(0.005 * index, self.param_value, index)

    def on_command(self, command, params):
        self.commands.append((command, params))
        if command in self.unresponsive:
            return
        handler = {
            mavlink.MAV_CMD_REQUEST_MESSAGE: self.cmd_request_message,
            mavlink.MAV_CMD_PREFLIGHT_CALIBRATION: self.cmd_calibration,
            mavlink.MAV_CMD_ACCELCAL_VEHICLE_POS: self.cmd_accel_pos,
            mavlink.MAV_CMD_DO_START_MAG_CAL: self.cmd_start_mag,
            mavlink.MAV_CMD_DO_CANCEL_MAG_CAL: self.cmd_cancel_mag,
            mavlink.MAV_CMD_RUN_PREARM_CHECKS: self.cmd_prearm,
            mavlink.MAV_CMD_DO_MOTOR_TEST: self.cmd_motor_test,
        }.get(command)
        self.ack(command, handler(params) if handler else mavlink.MAV_RESULT_UNSUPPORTED)

    def cmd_request_message(self, p):
        if int(p[0]) == mavlink.MAVLINK_MSG_ID_AUTOPILOT_VERSION:
            self.later(0.05, self.send, "autopilot_version", 0xFFEF, 0x04060300 | 255, 0, 0, 0,
                       list(b"abc1234\x00"), [0] * 8, [0] * 8, 0x1209, 0x5741, 0)
            return mavlink.MAV_RESULT_ACCEPTED
        return mavlink.MAV_RESULT_UNSUPPORTED

    def cmd_calibration(self, p):
        if self.armed:
            self.text(mavlink.MAV_SEVERITY_NOTICE, "Disarm to allow calibration")
            return mavlink.MAV_RESULT_FAILED
        if p[0] == 1:
            self.t += 0 if self.realtime else 1.0
            return mavlink.MAV_RESULT_ACCEPTED
        if p[2] == 1:
            self.text(mavlink.MAV_SEVERITY_INFO, "Updating barometer calibration")
            self.text(mavlink.MAV_SEVERITY_INFO, "Barometer calibration complete")
            return mavlink.MAV_RESULT_ACCEPTED
        if p[4] == 2:
            self.text(mavlink.MAV_SEVERITY_INFO, "Trim OK: roll=0.31 pitch=-0.12 yaw=0.00")
            return mavlink.MAV_RESULT_ACCEPTED
        if p[4] == 1:
            self.accel_step, self.accel_result = 1, None
            self.accel_prompt()
            return mavlink.MAV_RESULT_ACCEPTED
        return mavlink.MAV_RESULT_UNSUPPORTED

    def accel_prompt(self):
        self.text(mavlink.MAV_SEVERITY_CRITICAL, f"Place vehicle {ACCEL_TEXT[self.accel_step]} and press any key.")
        self.next["accel"] = self.clock()

    def cmd_accel_pos(self, p):
        if not self.accel_step or int(p[0]) != self.accel_step:
            return mavlink.MAV_RESULT_FAILED
        self.later(0.8, self.accel_sampled)
        self.accel_step = 0
        self.sampling = int(p[0])
        return mavlink.MAV_RESULT_ACCEPTED

    def accel_sampled(self):
        step = self.sampling
        if self.accel_fail_at == step:
            self.accel_result = mavlink.ACCELCAL_VEHICLE_POS_FAILED
            self.text(mavlink.MAV_SEVERITY_CRITICAL, "Calibration FAILED")
        elif step == 6:
            self.accel_result = mavlink.ACCELCAL_VEHICLE_POS_SUCCESS
            self.text(mavlink.MAV_SEVERITY_CRITICAL, "Calibration successful")
        else:
            self.accel_step = step + 1
            self.accel_prompt()

    def cmd_start_mag(self, p):
        if self.armed:
            self.text(mavlink.MAV_SEVERITY_NOTICE, "Disarm to allow compass calibration")
            return mavlink.MAV_RESULT_FAILED
        self.mag = {i: 0 for i in range(self.mag_compasses)}
        self.mag_autosave = bool(p[2])
        return mavlink.MAV_RESULT_ACCEPTED

    def cmd_cancel_mag(self, p):
        self.mag = None
        return mavlink.MAV_RESULT_ACCEPTED

    def mag_tick(self):
        mask = (1 << self.mag_compasses) - 1
        for i in list(self.mag):
            pct = self.mag[i]
            if pct >= 100:
                failed = self.mag_fail and i == self.mag_compasses - 1
                status = mavlink.MAG_CAL_FAILED if failed else mavlink.MAG_CAL_SUCCESS
                self.send("mag_cal_report", i, mask, status, int(self.mag_autosave and not failed),
                          35.0 if failed else 8.4 + i, 12.0, -40.0, 85.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0, 0, 1.0)
            else:
                self.mag[i] = min(100, pct + 5 + 2 * i)
                self.send("mag_cal_progress", i, mask, mavlink.MAG_CAL_RUNNING_STEP_ONE if pct < 50
                          else mavlink.MAG_CAL_RUNNING_STEP_TWO, 1, self.mag[i] if self.mag[i] < 100 else 99,
                          [255] * 10, 0.0, 0.0, 0.0)

    def cmd_prearm(self, p):
        if self.armed:
            return mavlink.MAV_RESULT_TEMPORARILY_REJECTED
        for text in self.prearm_texts:
            self.later(0.1, self.text, mavlink.MAV_SEVERITY_CRITICAL, text)
        return mavlink.MAV_RESULT_ACCEPTED

    def cmd_motor_test(self, p):
        if self.motor_test_result != mavlink.MAV_RESULT_ACCEPTED:
            self.text(mavlink.MAV_SEVERITY_CRITICAL, "Motor Test: vehicle not landed")
            return self.motor_test_result
        self.armed = True
        self.motor_until = self.clock() + min(float(p[3]), 600.0)
        self.text(mavlink.MAV_SEVERITY_INFO, "starting motor test")
        return mavlink.MAV_RESULT_ACCEPTED
