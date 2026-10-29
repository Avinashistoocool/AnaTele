import csv
import hashlib
import json
import math
import re
import struct
from collections import Counter
from importlib.metadata import version
from pathlib import Path

import duckdb
from pymavlink import mavutil
from pymavlink.dialects.v20 import ardupilotmega as mav

from . import derived, missions, rules, semantics, store
from .parsers import ParseError, records

ENGINE_VERSION = "0.2.0"
SERIES_POINT_LIMIT = 6_000_000
DEFAULT_WP_RADIUS = missions.DEFAULT_RADIUS_M
DERIVED_PREFIXES = ("motor.", "control.tilt_tracking_error_deg")
SERIES_PREFIXES = ("attitude.", "position.altitude_home", "position.latitude", "position.longitude", "control.",
                   "imu.", "output.", "rc.", "gps.", "ekf.", "battery.")
EKF_FLAG_NAMES = {1: "attitude", 2: "horizontal velocity", 4: "vertical velocity", 8: "horizontal position (rel)",
                  16: "horizontal position (abs)", 32: "vertical position (abs)", 1024: "uninitialized", 32768: "GPS glitching"}
VOLATILE_PARAMETERS = set(semantics.get("ardupilot-common")["volatile_parameters"])
OBS_COLUMNS = {
    "id": "BIGINT", "t_us": "BIGINT", "clock": "VARCHAR", "source": "VARCHAR", "epoch": "INTEGER",
    "message": "VARCHAR", "offset": "BIGINT", "length": "BIGINT", "sequence": "INTEGER",
    "native_time_us": "BIGINT", "quality": "VARCHAR", "payload_json": "VARCHAR",
}
SIGNAL_COLUMNS = {
    "id": "BIGINT", "t_us": "BIGINT", "source": "VARCHAR", "epoch": "INTEGER",
    "name": "VARCHAR", "value": "DOUBLE", "unit": "VARCHAR",
}


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def sql_path(path):
    return "'" + str(path).replace("'", "''") + "'"


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def parquet(csv_path, output, columns):
    with duckdb.connect() as db:
        db.execute("SET memory_limit='256MB'")
        db.execute("SET threads=2")
        db.read_csv(str(csv_path), header=True, columns=columns).create_view("source")
        db.execute(f"COPY source TO {sql_path(output)} (FORMAT PARQUET, COMPRESSION ZSTD)")


class Analysis:
    def __init__(self, profile, signal_writer, rule_pack=None):
        self.profile = profile or {}
        self.writer = signal_writer
        self.rule_pack = rule_pack or rules.load_pack()
        self.series = derived.SeriesStore()
        self.series_points = 0
        self.motor_maps = {}
        self.phases = []
        self.rule_results = []
        self.incidents = []
        self.flight_metrics = []
        self.ekf_flags = {}
        self.sources = {}
        self.events = []
        self.findings = []
        self.params = {}
        self.diagnostics = []
        self.diagnostic_counts = Counter()
        self.counts = Counter()
        self.signals = {}
        self.chunks = {}
        self.clips = {}
        self.peaks = {}
        self.episodes = []
        self.last_capture = None
        self.capture_epoch = 0
        self.first_time = None
        self.last_time = None
        self.mission_messages = []
        self.partial = False

    def diagnostic(self, kind, offset, detail):
        self.diagnostic_counts[kind] += 1
        if kind != "duplicate_csv":
            self.partial = True
        if len(self.diagnostics) < 500:
            self.diagnostics.append({"kind": kind, "offset": offset, "detail": detail})

    def event(self, record, kind, text, severity="info", evidence=None):
        if len(self.events) >= 20000:
            raise ParseError("MVP event limit exceeded; file not published")
        event = {
            "id": len(self.events) + 1, "record_id": record["id"], "evidence": evidence or [record["id"]],
            "t_us": record["time_us"], "source": record["source"], "epoch": record["epoch"],
            "kind": kind, "text": text, "severity": severity, "clock": record["clock"],
        }
        self.events.append(event)
        return event

    def finding(self, title, detail, source, severity, evidence, kind="observation", missing=None):
        self.findings.append({
            "id": len(self.findings) + 1, "title": title, "detail": detail, "source": source,
            "severity": severity, "kind": kind, "evidence": list(dict.fromkeys(evidence))[:100],
            "missing": missing or [], "calibrated_probability": None, "rule_version": ENGINE_VERSION,
        })

    def signal(self, record, name, value, unit, persist=True, memory=True):
        if not finite(value):
            return
        if persist:
            self.writer.writerow({
                "id": record["id"], "t_us": record["time_us"], "source": record["source"],
                "epoch": record["epoch"], "name": name, "value": value, "unit": unit,
            })
            key = (record["source"], name)
            if key not in self.signals:
                self.signals[key] = {"source": key[0], "name": name, "unit": unit, "count": 0}
            self.signals[key]["count"] += 1
        if memory and name.startswith(SERIES_PREFIXES):
            if self.series_points >= SERIES_POINT_LIMIT:
                if "series_limit" not in self.diagnostic_counts:
                    self.diagnostic("series_limit", record["offset"], "In-memory derived-indicator limit reached; later samples not assessed")
                return
            self.series_points += 1
            self.series.add(record["source"], record["epoch"], name, record["time_us"], float(value), record["id"], unit)

    def param(self, source, name):
        """Latest typed value of a parameter for a source (any epoch), or None when unknown/unverified."""
        best = None
        for (key, _), snapshot in self.params.items():
            item = snapshot["values"].get(name) if key == source else None
            if item and item.get("value") is not None and (best is None or item["t_us"] >= best["t_us"]):
                best = item
        return best["value"] if best else None

    def text(self, record, text, severity, evidence):
        source = self.sources[record["source"]]
        match = re.search(r"Ardu(?:Copter|Plane|Rover|Sub)\s+V?(\d+\.\d+\.\d+)(?:[^(]*\(([0-9a-fA-F]{6,})\))?", text)
        if match:
            source["firmware_reported"] = match.group(1)
            source["firmware_evidence"] = evidence
            if match.group(2):
                source["firmware_git"] = match.group(2).lower()
        lower = text.lower()
        kind = "STATUSTEXT"
        if "crash:" in lower and "disarm" in lower:
            kind, severity = "CRASH_CHECK_DISARM", "critical"
        elif "switching to compass" in lower:
            kind = "COMPASS_SELECTION"
        elif "lane switch" in lower:
            kind = "EKF_LANE_SWITCH"
        elif "yaw" in lower and ("reset" in lower or "fallback" in lower):
            kind = "YAW_EVENT"
        elif "prearm:" in lower:
            kind = "PREARM_BLOCK"
        self.event(record, kind, text, severity, evidence)

    def statustext(self, record):
        payload = record["payload"]
        severity = {0: "critical", 1: "critical", 2: "critical", 3: "error", 4: "warning"}.get(payload.get("severity"), "info")
        raw = bytes.fromhex(payload["wire_text_hex"])
        text_id = int(payload.get("id", 0))
        if text_id == 0:
            self.text(record, raw.split(b"\0", 1)[0].decode("utf-8", errors="replace"), severity, [record["id"]])
            return
        key = (record["source"], record["epoch"], text_id)
        sequence = int(payload.get("chunk_seq", 0))
        if sequence >= 32:
            self.diagnostic("text_chunk_limit", record["offset"], "STATUSTEXT chunk sequence exceeds MVP limit")
            return
        if sequence == 0 and key in self.chunks and 0 in self.chunks[key]["parts"]:
            self.diagnostic("incomplete_text", record["offset"], "STATUSTEXT ID restarted before earlier text completed")
            del self.chunks[key]
        state = self.chunks.setdefault(key, {"parts": {}, "ids": {}, "terminal": None})
        if sequence in state["parts"] and state["parts"][sequence] != raw:
            self.diagnostic("conflicting_text", record["offset"], "Conflicting STATUSTEXT chunks")
            del self.chunks[key]
            return
        state["parts"][sequence], state["ids"][sequence] = raw, record["id"]
        if b"\0" in raw:
            state["terminal"] = sequence
        terminal = state["terminal"]
        if terminal is not None and all(index in state["parts"] for index in range(terminal + 1)):
            text = b"".join(state["parts"][index] for index in range(terminal + 1)).split(b"\0", 1)[0]
            self.text(record, text.decode("utf-8", errors="replace"), severity, list(state["ids"].values()))
            del self.chunks[key]

    def set_armed(self, record, source, armed, text_suffix=""):
        if armed == source["armed"]:
            return
        self.event(record, "ARM_STATE", ("Armed" if armed else "Disarmed") + text_suffix +
                   (" (first observed state)" if source["armed"] is None else ""), "warning" if armed else "info")
        if armed:
            episode = {"source": source["key"], "epoch": record["epoch"], "start_us": record["time_us"],
                       "end_us": None, "start_record": record["id"], "end_record": None, "airborne": "unconfirmed"}
            self.episodes.append(episode)
            source["open_episode"] = len(self.episodes) - 1
        elif source["open_episode"] is not None:
            self.episodes[source["open_episode"]]["end_us"] = record["time_us"]
            self.episodes[source["open_episode"]]["end_record"] = record["id"]
            source["open_episode"] = None
        source["armed"] = armed

    def consume(self, record):
        payload, name, key = record["payload"], record["message"], record["source"]
        source = self.sources.setdefault(key, {
            "key": key, "role": "onboard_log" if key == "dataflash" else "unclassified",
            "messages": 0, "epochs": 0, "capabilities": None, "firmware_reported": None, "firmware_git": None,
            "firmware_evidence": [], "last_boot_ms": None, "boot_epoch": 0, "last_native_us": None,
            "armed": None, "mode": None, "open_episode": None, "epoch_ids": [], "last_epoch": None,
            "autopilot": None, "vehicle_type": None, "message_types": Counter(), "mission_seq": None,
            "desired_attitude_source": None,
        })
        source["messages"] += 1
        source["message_types"][name] += 1
        self.counts[name] += 1
        time_us = record["time_us"]
        self.first_time = time_us if self.first_time is None else min(self.first_time, time_us)
        self.last_time = time_us if self.last_time is None else max(self.last_time, time_us)
        native_time = None
        if record["clock"] == "capture_utc":
            if self.last_capture is not None and time_us < self.last_capture - 2_000_000:
                self.capture_epoch += 1
                self.diagnostic("capture_clock_regression", record["offset"], "Capture time moved backwards >2 s; analysis epochs separated")
            self.last_capture = time_us
            boot = payload.get("time_boot_ms")
            if finite(boot):
                native_time = int(boot) * 1000
                old = source["last_boot_ms"]
                if old is not None and boot < old - 10000:
                    if old > 0xF0000000 and boot < 100000:
                        self.diagnostic("boot_counter_wrap", record["offset"], "Possible uint32 boot-ms wrap")
                    else:
                        source["boot_epoch"] += 1
                        self.diagnostic("possible_boot_reset", record["offset"], "Boot time regressed >10 s; could be reboot or delayed data")
                source["last_boot_ms"] = boot
        elif record["clock"].startswith("boot_us"):
            native_time = time_us
            old = source["last_native_us"]
            if old is not None and time_us < old - 1_000_000:
                source["boot_epoch"] += 1
                self.diagnostic("possible_boot_reset", record["offset"], "Onboard time regressed >1 s; epochs separated")
            source["last_native_us"] = time_us
        record["native_time_us"] = native_time
        record["epoch"] = self.capture_epoch * 10000 + source["boot_epoch"]
        if record["epoch"] not in source["epoch_ids"]:
            source["epoch_ids"].append(record["epoch"])
        source["epochs"] = len(source["epoch_ids"])
        if source["last_epoch"] is not None and source["last_epoch"] != record["epoch"]:
            if source["open_episode"] is not None:
                self.episodes[source["open_episode"]]["boundary_note"] = "Clock discontinuity; end time unknown"
            source["armed"], source["mode"], source["open_episode"] = None, None, None
        source["last_epoch"] = record["epoch"]

        if name == "HEARTBEAT":
            autopilot, vehicle_type = payload.get("autopilot"), payload.get("type")
            if vehicle_type == mav.MAV_TYPE_GCS:
                source["role"] = "ground_station"
            elif autopilot not in (None, mav.MAV_AUTOPILOT_INVALID):
                source["role"] = "vehicle"
                source["autopilot"], source["vehicle_type"] = autopilot, vehicle_type
                armed = bool(payload.get("base_mode", 0) & mav.MAV_MODE_FLAG_SAFETY_ARMED)
                mode_num = int(payload.get("custom_mode", 0))
                mode = (mavutil.mode_mapping_acm.get(mode_num, f"mode {mode_num}")
                        if autopilot == mav.MAV_AUTOPILOT_ARDUPILOTMEGA and vehicle_type in (2, 3, 4, 13, 14, 15, 29)
                        else f"custom mode {mode_num}")
                self.set_armed(record, source, armed)
                if mode != source["mode"]:
                    self.event(record, "MODE_CHANGE", mode)
                    source["mode"] = mode
        elif name == "RADIO_STATUS" and source["role"] == "unclassified":
            source["role"] = "radio_status_source"
        elif name == "AUTOPILOT_VERSION":
            source["capabilities"] = int(payload.get("capabilities", 0))
        elif name == "EXTENDED_SYS_STATE":
            state = payload.get("landed_state")
            self.event(record, "LANDED_STATE", {0: "Unknown", 1: "On ground", 2: "In air", 3: "Taking off", 4: "Landing"}.get(state, str(state)))
            if state in (2, 3, 4) and source["open_episode"] is not None:
                self.episodes[source["open_episode"]]["airborne"] = "reported by landed-state message"
        elif name == "STATUSTEXT":
            self.statustext(record)
        elif name == "MSG":
            self.text(record, str(payload.get("Message", "")), "info", [record["id"]])
        elif name == "MODE":
            mode = payload.get("Mode", payload.get("ModeNum", "unknown"))
            self.event(record, "MODE_CHANGE", str(mode))
        elif name == "CSV_SAMPLE":
            source["role"] = "operator_csv"
            if payload["mode"] != source["mode"]:
                self.event(record, "MODE_CHANGE", payload["mode"] + " (operator CSV)")
                source["mode"] = payload["mode"]
        elif name == "ARM":
            state = payload.get("ArmState")
            if state in (0, 1):
                self.set_armed(record, source, bool(state), " (onboard ARM record)")
            else:
                self.event(record, "ARM_STATE", f"Onboard ARM state: {state if state is not None else 'unknown'}")
        elif name == "EKF_STATUS_REPORT":
            flags = int(payload.get("flags", 0))
            old = self.ekf_flags.get(key)
            if old is not None and old != flags:
                lost = [label for bit, label in EKF_FLAG_NAMES.items() if bit < 1024 and old & bit and not flags & bit]
                gained = [label for bit, label in EKF_FLAG_NAMES.items() if bit < 1024 and flags & bit and not old & bit]
                bad = [label for bit, label in EKF_FLAG_NAMES.items() if bit >= 1024 and flags & bit and not old & bit]
                parts = ([f"lost: {', '.join(lost)}"] if lost else []) + ([f"gained: {', '.join(gained)}"] if gained else []) + \
                        ([f"set: {', '.join(bad)}"] if bad else [])
                if parts:
                    self.event(record, "EKF_STATUS", f"EKF status flags 0x{old:x} -> 0x{flags:x} ({'; '.join(parts)})",
                               "warning" if lost or bad else "info")
            self.ekf_flags[key] = flags
        elif name in ("ERR", "EV"):
            self.event(record, "ONBOARD_EVENT", f"{name}: {json.dumps(payload)}",
                       "warning" if name == "ERR" else "info")

        if name in ("PARAM_VALUE", "PARM"):
            param_name = payload.get("param_id") if name == "PARAM_VALUE" else payload.get("Name")
            value = payload.get("param_value") if name == "PARAM_VALUE" else payload.get("Value")
            if param_name:
                params = self.params.setdefault((key, record["epoch"]), {
                    "source": key, "epoch": record["epoch"], "values": {}, "indices": set(), "reported_counts": set(),
                })
                old = params["values"].get(param_name)
                if old is not None and old["raw_value"] != value:
                    if param_name in VOLATILE_PARAMETERS:
                        params["volatile_updates"] = params.get("volatile_updates", 0) + 1
                    else:
                        self.event(record, "PARAMETER_CHANGE", f"{param_name}: {old['raw_value']} -> {value}",
                                   "notice", [old["record_id"], record["id"]])
                params["values"][param_name] = {
                    "raw_value": value, "param_type": payload.get("param_type"),
                    "wire_hex": payload.get("wire_param_value_hex"), "record_id": record["id"],
                    "t_us": time_us, "encoding": "dataflash_numeric" if name == "PARM" else "unverified",
                }
                if name == "PARAM_VALUE":
                    count, index = payload.get("param_count"), payload.get("param_index")
                    if isinstance(count, int) and count > 0:
                        params["reported_counts"].add(count)
                    if isinstance(index, int) and 0 <= index < 65535:
                        params["indices"].add(index)
        elif name == "PARAM_SET":
            self.event(record, "PARAMETER_REQUEST", f"Requested {payload.get('param_id')} = {payload.get('param_value')}; not a confirmed change")

        if name.startswith("MISSION_"):
            self.mission_messages.append({**record, "payload": payload})
            if len(self.mission_messages) > 20000:
                raise ParseError("Mission record limit exceeded")
            if name in ("MISSION_ITEM_REACHED", "MISSION_ACK", "MISSION_CLEAR_ALL"):
                event = self.event(record, name, json.dumps(payload))
                event["seq"] = payload.get("seq")
            elif name == "MISSION_CURRENT" and payload.get("seq") != source["mission_seq"]:
                event = self.event(record, name, f"Current mission item {payload.get('seq')}"
                                   + (" (first observed)" if source["mission_seq"] is None else ""))
                event["seq"] = payload.get("seq")
                source["mission_seq"] = payload.get("seq")
        if name in ("COMMAND_LONG", "COMMAND_INT", "COMMAND_ACK"):
            self.event(record, name, json.dumps(payload))
        self.extract_signals(record)
        return record

    def extract_signals(self, r):
        p, name = r["payload"], r["message"]
        mappings = {
            "ATTITUDE": [(axis, f"attitude.{axis}_deg", "deg", 180 / math.pi) for axis in ("roll", "pitch", "yaw")],
            "GLOBAL_POSITION_INT": [("lat", "position.latitude", "deg", 1e-7), ("lon", "position.longitude", "deg", 1e-7),
                                    ("alt", "position.altitude_msl", "m", .001), ("relative_alt", "position.altitude_home", "m", .001)],
            "GPS_RAW_INT": [("fix_type", "gps.fix_type", "enum", 1)],
            "RADIO_STATUS": [("rssi", "radio.rssi", "device units", 1), ("remrssi", "radio.remote_rssi", "device units", 1),
                             ("rxerrors", "radio.rx_errors_counter", "count", 1)],
            "ATT": [(field, f"attitude.{axis}_deg", "deg", 1) for field, axis in (("Roll", "roll"), ("Pitch", "pitch"), ("Yaw", "yaw"))],
            "CSV_SAMPLE": [("latitude_deg", "position.latitude", "deg", 1), ("longitude_deg", "position.longitude", "deg", 1),
                           ("altitude_m", "position.altitude_agl", "m", 1), ("battery_pct", "battery.remaining", "%", 1)],
        }
        for field, signal, unit, scale in mappings.get(name, []):
            if finite(p.get(field)):
                self.signal(r, signal, p[field] * scale, unit)
        source = self.sources[r["source"]]
        desired = None
        if name == "NAV_CONTROLLER_OUTPUT":
            desired = (p.get("nav_roll"), p.get("nav_pitch"), "NAV_CONTROLLER_OUTPUT nav_roll/nav_pitch")
        elif name == "ATTITUDE_TARGET" and isinstance(p.get("q"), list) and len(p["q"]) == 4 and not p.get("type_mask", 0) & 128:
            roll, pitch = derived.quaternion_roll_pitch(p["q"])
            desired = (roll, pitch, "ATTITUDE_TARGET quaternion")
        elif name == "ATT":
            desired = (p.get("DesRoll"), p.get("DesPitch"), "ATT DesRoll/DesPitch")
        if desired and finite(desired[0]) and finite(desired[1]):
            if source["desired_attitude_source"] in (None, desired[2]):
                source["desired_attitude_source"] = desired[2]
                self.signal(r, "attitude.desired_roll_deg", desired[0], "deg")
                self.signal(r, "attitude.desired_pitch_deg", desired[1], "deg")
        if name == "VFR_HUD" and finite(p.get("throttle")):
            self.signal(r, "control.throttle_pct", p["throttle"], "%")
        elif name == "CTUN" and finite(p.get("ThO")):
            self.signal(r, "control.throttle_pct", p["ThO"] * 100, "%")
        if name == "GPS_RAW_INT" and finite(p.get("h_acc")) and p["h_acc"] > 0:
            self.signal(r, "gps.h_acc_m", p["h_acc"] / 1000, "m")
        elif name == "GPS":
            if finite(p.get("Status")):
                self.signal(r, "gps.fix_type", p["Status"], "enum")
        elif name == "GPA" and finite(p.get("HAcc")) and p["HAcc"] > 0:
            self.signal(r, "gps.h_acc_m", p["HAcc"], "m")
        if name == "POS":
            for field, signal, unit in (("Lat", "position.latitude", "deg"), ("Lng", "position.longitude", "deg"),
                                        ("RelHomeAlt", "position.altitude_home", "m")):
                if finite(p.get(field)):
                    self.signal(r, signal, p[field], unit)
        if name == "EKF_STATUS_REPORT":
            for field, signal in (("compass_variance", "ekf.compass_ratio"), ("velocity_variance", "ekf.velocity_ratio"),
                                  ("pos_horiz_variance", "ekf.pos_horiz_ratio"), ("pos_vert_variance", "ekf.pos_vert_ratio")):
                if finite(p.get(field)):
                    self.signal(r, signal, p[field], "test ratio")
        elif name == "XKF4" and finite(p.get("C")):
            for field, label in (("SM", "compass"), ("SV", "velocity"), ("SP", "pos_horiz"), ("SH", "pos_vert")):
                if finite(p.get(field)):
                    self.signal(r, f"ekf.core{int(p['C'])}.{label}_ratio", p[field], "test ratio")
        if name in ("RC_CHANNELS", "RC_CHANNELS_RAW"):
            for channel in range(1, 19):
                value = p.get(f"chan{channel}_raw")
                if finite(value) and value not in (0, 65535):
                    self.signal(r, f"rc.ch{channel}", value, "us")
        if name in ("SERVO_OUTPUT_RAW", "RCOU"):
            for channel in range(1, 17):
                value = p.get(f"servo{channel}_raw" if name == "SERVO_OUTPUT_RAW" else f"C{channel}")
                self.signal(r, f"output.ch{channel}", value, "us")
        if name == "SYS_STATUS":
            # ArduPilot reports 0 V when no monitor is configured; that is absence, not a measurement.
            if finite(p.get("voltage_battery")) and 0 < p["voltage_battery"] != 65535:
                self.signal(r, "battery.voltage", p["voltage_battery"] * .001, "V")
            if finite(p.get("current_battery")) and p["current_battery"] != -1 and p.get("voltage_battery", 0) > 0:
                self.signal(r, "battery.current", p["current_battery"] * .01, "A")
            if finite(p.get("battery_remaining")) and p["battery_remaining"] >= 0:
                self.signal(r, "battery.remaining", p["battery_remaining"], "%")
        if name == "BAT":
            instance = int(p.get("Instance", 0))
            for field, unit in (("Volt", "V"), ("Curr", "A")):
                if finite(p.get("Volt")) and p["Volt"] > 0:
                    self.signal(r, f"battery.{instance}.{field.lower()}", p.get(field), unit)
        if name in ("VIBRATION", "VIBE"):
            instance = int(p.get("IMU", 0)) if name == "VIBE" else 0
            for axis in ("x", "y", "z"):
                value = p.get(f"vibration_{axis}" if name == "VIBRATION" else "Vibe" + axis.upper())
                self.signal(r, f"imu.{instance}.vibration.{axis}", value, "m/s^2")
                if finite(value):
                    key = (r["source"], r["epoch"], instance, axis)
                    if key not in self.peaks or value > self.peaks[key]["value"]:
                        self.peaks[key] = {"value": value, "record_id": r["id"], "t_us": r["time_us"]}
            counters = {instance: p.get("Clip")} if name == "VIBE" else {i: p.get(f"clipping_{i}") for i in range(3)}
            for imu, count in counters.items():
                if not finite(count):
                    continue
                self.signal(r, f"imu.{imu}.clipping_counter", count, "count")
                key = (r["source"], r["epoch"], imu)
                old = self.clips.get(key)
                if old and count > old["count"]:
                    self.event(r, "CLIPPING_INCREMENT", f"IMU {imu}: +{int(count - old['count'])} clips between observations",
                               "warning", [old["record"], r["id"]])
                elif old and count < old["count"]:
                    self.event(r, "COUNTER_RESET", f"IMU {imu} clipping counter decreased; interval excluded",
                               "notice", [old["record"], r["id"]])
                self.clips[key] = {"count": count, "record": r["id"]}

    def finalize(self):
        for key in self.chunks:
            self.diagnostic("incomplete_text", 0, f"Unfinished STATUSTEXT assembly for {key}")
        for source in self.sources.values():
            pack, note = semantics.select(source) if source["role"] in ("vehicle", "onboard_log") else (None, "not a vehicle source")
            source["semantic_pack"], source["semantic_match"] = pack, note
            source["message_types"] = dict(source["message_types"])
        parameter_sets = []
        for (source_key, epoch), snapshot in self.params.items():
            caps = self.sources[source_key]["capabilities"]
            pack = semantics.get(self.sources[source_key]["semantic_pack"])
            for name, item in snapshot["values"].items():
                kind = item["param_type"]
                item["value"] = None
                if item["encoding"] == "dataflash_numeric" or kind == mav.MAV_PARAM_TYPE_REAL32:
                    item["value"] = item["raw_value"]
                    if kind == mav.MAV_PARAM_TYPE_REAL32:
                        item["encoding"] = "real32"
                elif caps is not None and (caps & mav.MAV_PROTOCOL_CAPABILITY_PARAM_ENCODE_C_CAST
                                          and caps & mav.MAV_PROTOCOL_CAPABILITY_PARAM_ENCODE_BYTEWISE):
                    item["encoding"] = "ambiguous_capabilities"
                elif caps is not None and caps & mav.MAV_PROTOCOL_CAPABILITY_PARAM_ENCODE_C_CAST:
                    item["value"], item["encoding"] = item["raw_value"], "c_cast"
                elif caps is not None and caps & mav.MAV_PROTOCOL_CAPABILITY_PARAM_ENCODE_BYTEWISE:
                    formats = {1: "B", 2: "b", 3: "H", 4: "h", 5: "I", 6: "i"}
                    if kind in formats and item["wire_hex"]:
                        fmt = "<" + formats[kind]
                        item["value"] = struct.unpack(fmt, bytes.fromhex(item["wire_hex"])[:struct.calcsize(fmt)])[0]
                        item["encoding"] = "bytewise"
                elif pack and pack.get("param_encoding") == "c_cast":
                    value = semantics.c_cast_value(item["raw_value"], kind)
                    item["value"] = value
                    item["encoding"] = "c_cast_per_semantic_pack" if value is not None else "c_cast_implausible"
                meta = semantics.metadata(self.sources[source_key]["semantic_pack"], name)
                if meta:
                    item["meta"] = meta
            counts = sorted(snapshot["reported_counts"])
            expected = counts[0] if len(counts) == 1 else None
            covered = len(set(range(expected)) & snapshot["indices"]) if expected else None
            if expected and covered == expected and len(snapshot["values"]) == expected:
                snapshot["completeness"] = "observed index coverage complete; snapshot not atomic"
            elif expected and len(snapshot["values"]) >= expected:
                snapshot["completeness"] = ("all reported names observed (indices not reported, e.g. FTP/bulk download); "
                                            "snapshot not atomic")
            else:
                snapshot["completeness"] = "partial or unknown"
            snapshot["expected_count"], snapshot["covered_indices"] = expected, covered
            snapshot["reported_counts"] = counts
            del snapshot["indices"]
            parameter_sets.append(snapshot)

        for source_key, source in self.sources.items():
            if source["role"] not in ("vehicle", "onboard_log"):
                continue
            self.motor_maps[source_key] = derived.motor_mapping(lambda name, s=source_key: self.param(s, name))
            for epoch in source["epoch_ids"]:
                derived.derive_motors(self.series, source_key, epoch, self.motor_maps[source_key])
        derived.derive_tracking(self.series)
        for index, episode in enumerate(self.episodes):
            phase = derived.infer_phases(self.series, episode, self.events, self.last_time)
            self.phases.append(phase)
            if phase["status"] == "airborne_reported":
                episode["airborne"] = "reported by landed-state message"
            elif phase["status"] == "airborne_inferred":
                episode["airborne"] = "inferred from altitude rise and demand"
                self.event_at(episode["source"], episode["epoch"], phase["takeoff_us"], phase["takeoff_record"],
                              "PHASE", "Inferred takeoff (altitude rise with demand; no landed-state message)")
            elif phase["status"] == "no_airborne_evidence":
                episode["airborne"] = "no airborne evidence"
            if phase["loss_of_control_onset_us"]:
                self.event_at(episode["source"], episode["epoch"], phase["loss_of_control_onset_us"], phase["excursion_record"],
                              "PHASE", f"Attitude exceeded {derived.PHASE_CONFIG['attitude_excursion_deg']:g} deg (possible loss of control or impact)",
                              "warning")
        self.rule_results, self.incidents = rules.evaluate(self.rule_pack, self, parameter_sets)
        self.flight_metrics = [self.metrics(index, episode, phase) for index, (episode, phase)
                               in enumerate(zip(self.episodes, self.phases))]
        for item in self.rule_results:
            if item["state"] == "triggered":
                self.findings.append({
                    "id": len(self.findings) + 1, "title": item["title"], "detail": item["detail"],
                    "source": item["scope"].get("source"), "severity": item["severity"], "kind": item["kind"],
                    "evidence": item["evidence"], "missing": item["missing"], "calibrated_probability": None,
                    "rule_id": item["rule_id"], "rule_version": item["rule_version"], "rule_result": item["id"],
                    "t_us": item.get("t_us"), "support": item.get("support", []), "contradictions": item.get("contradictions", []),
                })
        self.events.sort(key=lambda e: (e["t_us"], e["id"]))
        return parameter_sets

    def persist_derived(self):
        for (source, epoch, name), series in self.series.data.items():
            if not name.startswith(DERIVED_PREFIXES):
                continue
            for i in range(len(series.t)):
                record = {"id": series.ids[i], "time_us": series.t[i], "source": source, "epoch": epoch}
                self.signal(record, name, series.v[i], series.unit, memory=False)

    def metrics(self, index, episode, phase):
        """Comparable per-episode summary used for trends and side-by-side comparison."""
        row = {"episode": index, "source": episode["source"], "epoch": episode["epoch"], "phase_status": phase["status"],
               "armed_s": round((episode["end_us"] - episode["start_us"]) / 1e6, 2) if episode["end_us"] else None,
               "airborne_s": None, "pre_event_s": None, "crash_check": phase["critical_event_us"] is not None,
               "rule_states": {r["rule_id"]: r["state"] for r in self.rule_results
                               if r["scope"].get("type") == "episode" and r["scope"].get("episode") == index}}
        windows = derived.phase_windows(phase, "airborne_pre_event") if phase["takeoff_us"] else []
        if phase["takeoff_us"]:
            row["airborne_s"] = round((phase["airborne_end_us"] - phase["takeoff_us"]) / 1e6, 2)
            row["pre_event_s"] = round(sum(e - s for s, e in windows) / 1e6, 2)
        if not windows:
            return row
        for pattern, label in (("imu.*.vibration.*", "vibration"), ("ekf.*compass_ratio", "ekf_compass_ratio"),
                               ("control.tilt_tracking_error_deg", "tilt_error_deg"), ("control.throttle_pct", "throttle_pct")):
            for (_, _, name), series in self.series.match(pattern, episode["source"], episode["epoch"]):
                stats = derived.window_stats(series, windows)
                if stats:
                    key = label if label != "vibration" else "vibration_" + name.split(".")[-1]
                    row[key + "_mean"], row[key + "_max"] = round(stats["mean"], 3), round(stats["max"], 3)
        outputs = {}
        for (_, _, name), series in self.series.match("motor.*.output_frac", episode["source"], episode["epoch"]):
            stats = derived.window_stats(series, windows)
            if stats:
                outputs[name.split(".")[1]] = round(stats["mean"], 3)
        if outputs:
            row["motor_mean_output"] = outputs
            row["motor_spread"] = round(max(outputs.values()) - min(outputs.values()), 3)
        return row

    def event_at(self, source, epoch, t_us, record_id, kind, text, severity="info"):
        record = {"id": record_id, "time_us": t_us, "source": source, "epoch": epoch, "clock": "derived"}
        return self.event(record, kind, text, severity)

    def position_track(self):
        tracks = {}
        for (source, epoch, name), lat in self.series.data.items():
            if name != "position.latitude":
                continue
            lon = self.series.get(source, epoch, "position.longitude")
            if lon is None:
                continue
            points = []
            for i in range(len(lat.t)):
                j = lon.nearest(lat.t[i], 0)
                if j is not None and lat.v[i] and lon.v[j]:
                    points.append((lat.t[i], lat.v[i], lon.v[j], lat.ids[i]))
            tracks[(source, epoch)] = points
        return tracks

    def h_acc(self, source, epoch, t_us):
        series = self.series.get(source, epoch, "gps.h_acc_m")
        index = series.nearest(t_us, 2_000_000) if series else None
        return (series.v[index], series.ids[index]) if index is not None else None

    def mission_summary(self):
        radius_param = (DEFAULT_WP_RADIUS, "default assumption; WP_RADIUS_M not observed")
        for source in self.sources:
            for name in ("WP_RADIUS_M", "WPNAV_RADIUS"):
                value = self.param(source, name)
                if value is not None:
                    radius_param = ((value / 100 if name == "WPNAV_RADIUS" else value), f"{name} parameter")
                    break
        return missions.summarize(self.mission_messages, self.sources, self.episodes, self.phases, self.events,
                                  self.position_track(), self.h_acc, radius_param)


def run(job_id):
    job = store.get_job(job_id)
    if job is None:
        raise ValueError("Unknown job")
    profile = store.get_profile(job["profile_id"]) if job["profile_id"] else None
    directory = store.DATA / "datasets" / job_id
    directory.mkdir(parents=True, exist_ok=True)
    obs_csv, sig_csv = directory / "observations.csv", directory / "signals.csv"
    count = 0
    with obs_csv.open("w", newline="", encoding="utf-8") as obs_stream, sig_csv.open("w", newline="", encoding="utf-8") as sig_stream:
        obs_writer = csv.DictWriter(obs_stream, fieldnames=OBS_COLUMNS)
        sig_writer = csv.DictWriter(sig_stream, fieldnames=SIGNAL_COLUMNS)
        obs_writer.writeheader()
        sig_writer.writeheader()
        engine = Analysis(profile["body"] if profile else None, sig_writer)
        for record in records(job["path"], job["format"], engine.diagnostic):
            record = engine.consume(record)
            obs_writer.writerow({
                "id": record["id"], "t_us": record["time_us"], "clock": record["clock"],
                "source": record["source"], "epoch": record["epoch"], "message": record["message"],
                "offset": record["offset"], "length": record["length"], "sequence": record["sequence"],
                "native_time_us": record["native_time_us"], "quality": record["quality"],
                "payload_json": json.dumps(record["payload"], allow_nan=False),
            })
            count += 1
            if count % 1000 == 0:
                store.progress(job_id, max(1, min(85, int(record["offset"] / job["size"] * 85))))
        if count == 0:
            raise ParseError("No usable records decoded")
        params = engine.finalize()
        engine.persist_derived()
        mission_summary = engine.mission_summary()
    store.progress(job_id, 90)
    parquet(obs_csv, directory / "observations.parquet", OBS_COLUMNS)
    parquet(sig_csv, directory / "signals.parquet", SIGNAL_COLUMNS)
    obs_csv.unlink()
    sig_csv.unlink()
    manifest = {
        "version": ENGINE_VERSION, "parser": {"pymavlink": version("pymavlink"), "dialect": "ardupilotmega v20"},
        "profile_id": job["profile_id"], "record_count": count, "partial": engine.partial,
        "sources": list(engine.sources.values()), "message_counts": dict(engine.counts),
        "signals": list(engine.signals.values()), "events": engine.events, "findings": engine.findings,
        "parameters": params, "armed_episodes": engine.episodes, "missions": mission_summary,
        "phases": engine.phases, "rule_results": engine.rule_results, "incidents": engine.incidents,
        "flight_metrics": engine.flight_metrics,
        "motor_maps": {key: {k: v for k, v in value.items()} for key, value in engine.motor_maps.items()},
        "rule_pack": {"pack": engine.rule_pack["pack"], "version": engine.rule_pack["version"],
                      "sha256": engine.rule_pack["sha256"], "rules": len(engine.rule_pack["rules"])},
        "semantic_packs": {key: {"id": value["id"], "sha256": value["sha256"]} for key, value in semantics.packs().items()},
        "drone_key": (profile or {}).get("drone_key"),
        "diagnostics": engine.diagnostics, "diagnostic_counts": dict(engine.diagnostic_counts),
        "first_time_us": engine.first_time, "last_time_us": engine.last_time,
        "artifact_sha256": job["sha256"],
        "limitations": [
            "Read-only local prototype, not an airworthiness assessment.",
            "Telemetry captures only streamed/recorded messages. Absence is not a healthy result.",
            "No calibrated root-cause probabilities, cross-log clock alignment, per-core EKF innovation analysis or motor-failure diagnosis.",
            "Flight phases without landed-state messages are inferred from home-relative altitude and demand; labels say so.",
            "Hypothesis ordering is an expert-weighted screening order, not a probability.",
            "Motor commands are not measured thrust; tilt tracking error pairs desired/estimated attitude sampled at different instants.",
            "Clocks/boot resets are conservatively segmented heuristics; capture time is not sensor time.",
            "No packet-loss percentage is inferred from sequence gaps.",
            "DataFlash unit/format definitions remain in raw evidence; only the named common signals are normalized.",
            "Position plots are estimated/sample positions, not ground truth or proven waypoint completion.",
        ],
    }
    manifest["parquet_sha256"] = {
        name: file_digest(directory / name)
        for name in ("observations.parquet", "signals.parquet")
    }
    store.finish(job_id, manifest)
