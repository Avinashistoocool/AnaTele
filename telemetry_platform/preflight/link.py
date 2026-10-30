"""Thin MAVLink link used by AnaTele Preflight.

Wraps a pymavlink ``mavutil`` connection (or any object with the same small surface: ``recv_match``,
``mav.command_long_send``, ``mav.param_request_read_send``, ``mav.param_request_list_send``,
``mav.request_data_stream_send`` and ``close``) so tests can inject a fake vehicle. Every wait has a timeout.
"""
import time

from pymavlink.dialects.v20 import ardupilotmega as mavlink

GCS_SYSTEM, GCS_COMPONENT = 255, 190
SIMULATED = ("sim", "sim:", "simulate")


class LinkError(Exception):
    pass


def enum_name(enum, value, prefix=""):
    entry = mavlink.enums.get(enum, {}).get(value)
    name = entry.name if entry else str(value)
    return name[len(prefix):] if prefix and name.startswith(prefix) else name


def result_name(result):
    return "no COMMAND_ACK (timeout)" if result is None else enum_name("MAV_RESULT", result, "MAV_RESULT_")


def firmware_version(value):
    kinds = {0: "dev", 64: "alpha", 128: "beta", 192: "rc", 255: "official"}
    return f"{value >> 24 & 255}.{value >> 16 & 255}.{value >> 8 & 255} ({kinds.get(value & 255, value & 255)})"


def text_of(value):
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    return str(value).split("\x00")[0]


class Link:
    def __init__(self, conn, clock=None, description=""):
        self.conn = conn
        self.clock = clock or getattr(conn, "clock", None) or time.monotonic
        self.description = description
        self.target_system = None
        self.target_component = None
        self.heartbeat = None
        self.latest = {}
        self.statustexts = []
        self.on_text = None
        self.closed = False

    @classmethod
    def open(cls, device, baud=115200):
        """Open COM3 / /dev/ttyACM0 (USB serial), udpin:0.0.0.0:14550, udpout:..., tcp:host:port, or sim: (simulated)."""
        device = (device or "").strip()
        if device.lower() in SIMULATED:
            from .fake import FakeVehicle
            return cls(FakeVehicle(realtime=True), description="simulated vehicle")
        if not device:
            raise LinkError("No connection string given (e.g. COM3, udpin:0.0.0.0:14550, tcp:127.0.0.1:5760 or sim:)")
        from pymavlink import mavutil
        try:
            conn = mavutil.mavlink_connection(device, baud=int(baud), source_system=GCS_SYSTEM,
                                              source_component=GCS_COMPONENT, autoreconnect=False,
                                              dialect="ardupilotmega")
        except Exception as error:
            raise LinkError(f"Cannot open {device}: {error}") from error
        return cls(conn, description=f"{device} @ {baud}" if "udp" not in device and "tcp" not in device else device)

    @property
    def armed(self):
        return bool(self.heartbeat and self.heartbeat.base_mode & mavlink.MAV_MODE_FLAG_SAFETY_ARMED)

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.conn.close()
            except Exception:
                pass

    def _read(self, timeout):
        if self.closed:
            raise LinkError("Link is closed")
        try:
            msg = self.conn.recv_match(blocking=True, timeout=max(timeout, 0.01))
        except OSError as error:
            raise LinkError(f"Link read failed: {error}") from error
        if msg is None or msg.get_type() == "BAD_DATA":
            return None
        return self._dispatch(msg)

    def _dispatch(self, msg):
        kind, src, comp = msg.get_type(), msg.get_srcSystem(), msg.get_srcComponent()
        if self.target_system is None:
            if (kind == "HEARTBEAT" and msg.autopilot != mavlink.MAV_AUTOPILOT_INVALID
                    and msg.type != mavlink.MAV_TYPE_GCS):
                self.target_system, self.target_component = src, comp
            else:
                return None
        if src != self.target_system:
            return None
        if kind in ("HEARTBEAT", "COMMAND_ACK") and comp != self.target_component:
            return None
        if kind == "HEARTBEAT":
            self.heartbeat = msg
        elif kind == "STATUSTEXT":
            text = text_of(msg.text)
            self.statustexts.append((self.clock(), msg.severity, text))
            if self.on_text:
                self.on_text(msg.severity, text)
        self.latest[kind] = msg
        return msg

    def wait(self, types, timeout, condition=None):
        """Return the next message of one of ``types`` matching ``condition``, or None after ``timeout`` seconds."""
        deadline = self.clock() + timeout
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                return None
            msg = self._read(min(remaining, 0.5))
            if msg is not None and msg.get_type() in types and (condition is None or condition(msg)):
                return msg

    def pump(self, seconds):
        """Keep reading (and recording) messages for ``seconds``."""
        deadline = self.clock() + seconds
        while self.clock() < deadline:
            self._read(min(deadline - self.clock(), 0.5))

    def wait_heartbeat(self, timeout=10.0):
        return self.wait(["HEARTBEAT"], timeout,
                         lambda m: self.target_component is None or m.get_srcComponent() == self.target_component)

    def texts_since(self, since):
        return [text for t, _severity, text in self.statustexts if t >= since]

    def send_command(self, command, params=(), confirmation=0):
        p = [float(x) for x in params] + [0.0] * (7 - len(params))
        self.conn.mav.command_long_send(self.target_system, self.target_component, command, confirmation, *p[:7])

    def command(self, command, params=(), timeout=5.0, retries=0):
        """Send COMMAND_LONG and wait for its COMMAND_ACK (IN_PROGRESS keeps waiting). Returns MAV_RESULT or None."""
        for attempt in range(retries + 1):
            self.send_command(command, params, confirmation=attempt)
            deadline = self.clock() + timeout
            while True:
                ack = self.wait(["COMMAND_ACK"], deadline - self.clock(), lambda m: m.command == command)
                if ack is None:
                    break
                if ack.result != mavlink.MAV_RESULT_IN_PROGRESS:
                    return ack.result
        return None

    def request_streams(self, rate_hz=4):
        """Ask for telemetry streams (SYS_STATUS, GPS, EKF, MAG_CAL_*...); some links stream nothing until asked."""
        self.conn.mav.request_data_stream_send(self.target_system, self.target_component,
                                               mavlink.MAV_DATA_STREAM_ALL, rate_hz, 1)

    def request_message(self, message_id, timeout=3.0):
        name = mavlink.mavlink_map[message_id].msgname
        self.latest.pop(name, None)
        result = self.command(mavlink.MAV_CMD_REQUEST_MESSAGE, [message_id], timeout=timeout)
        if result not in (None, mavlink.MAV_RESULT_ACCEPTED):
            # older firmware: the dedicated capabilities request
            self.command(mavlink.MAV_CMD_REQUEST_AUTOPILOT_CAPABILITIES, [1], timeout=timeout)
        return self.latest.get(name) or self.wait([name], timeout)

    def param_read(self, name, timeout=2.0, retries=2):
        for _ in range(retries + 1):
            self.conn.mav.param_request_read_send(self.target_system, self.target_component, name.encode("ascii"), -1)
            msg = self.wait(["PARAM_VALUE"], timeout, lambda m: text_of(m.param_id) == name)
            if msg is not None:
                return float(msg.param_value)
        return None

    def param_list(self, idle_timeout=3.0, total_timeout=120.0, progress=None):
        """Fetch all parameters with PARAM_REQUEST_LIST, re-requesting missing indices up to twice."""
        values, seen, count = {}, set(), None
        self.conn.mav.param_request_list_send(self.target_system, self.target_component)
        deadline = self.clock() + total_timeout
        for attempt in range(3):
            while self.clock() < deadline:
                msg = self.wait(["PARAM_VALUE"], idle_timeout)
                if msg is None:
                    break
                values[text_of(msg.param_id)] = float(msg.param_value)
                if msg.param_index < 65535:
                    seen.add(msg.param_index)
                count = msg.param_count
                if progress and count:
                    progress(min(len(seen) / count, 1.0))
                if count and len(seen) >= count:
                    return values
            if count is None or attempt == 2:
                break
            for index in sorted(set(range(count)) - seen):
                self.conn.mav.param_request_read_send(self.target_system, self.target_component, b"", index)
        return values
