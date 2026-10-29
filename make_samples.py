"""Generate clearly synthetic MAVLink/DataFlash/CSV fixtures, never real flight evidence."""
import csv
import struct
from datetime import datetime, timezone
from pathlib import Path

from pymavlink.dialects.v20 import ardupilotmega as mav

BASE = int(datetime(2026, 10, 29, 12, tzinfo=timezone.utc).timestamp() * 1_000_000)


def tlog_bytes():
    encoder = mav.MAVLink(None)
    sequences = {}
    frames = []

    def emit(second, message, source=(1, 1)):
        encoder.srcSystem, encoder.srcComponent = source
        encoder.seq = sequences.get(source, 0)
        sequences[source] = (encoder.seq + 1) % 256
        frames.append(struct.pack(">Q", BASE + int(second * 1e6)) + message.pack(encoder))

    emit(0, encoder.heartbeat_encode(mav.MAV_TYPE_GCS, mav.MAV_AUTOPILOT_INVALID, 128, 0, 4), (255, 190))
    emit(0, encoder.heartbeat_encode(mav.MAV_TYPE_QUADROTOR, mav.MAV_AUTOPILOT_ARDUPILOTMEGA, 0, 0, 3))
    emit(.01, encoder.statustext_encode(6, b"ArduCopter V4.7.2"))
    emit(.02, encoder.autopilot_version_encode(
        mav.MAV_PROTOCOL_CAPABILITY_PARAM_ENCODE_C_CAST, (4 << 24) | (7 << 16) | (2 << 8) | 255,
        0, 0, 0, [0] * 8, [0] * 8, [0] * 8, 0, 0, 123,
    ))
    parameters = [("FS_THR_VALUE", 975), ("BATT_MONITOR", 0), ("ARMING_SKIPCHK", 128),
                  ("FS_GCS_ENABLE", 0), ("RCMAP_THROTTLE", 3), ("FRAME_TYPE", 1)]
    for index, (name, value) in enumerate(parameters):
        emit(.1 + index * .01, encoder.param_value_encode(name.encode(), value, mav.MAV_PARAM_TYPE_INT32, len(parameters), index))
    for second in (1, 2, 3):
        emit(second, encoder.statustext_encode(4, b"PreArm: Radio failsafe on"))
        emit(second, encoder.rc_channels_encode(second * 1000, 8, 1500, 1500, 1000, 1500,
             1500, 1500, 1500, 1500, *([65535] * 10), 200))
    for second, count in ((5, 4), (7, 3)):
        emit(second, encoder.mission_count_encode(1, 1, count), (255, 190))
        for index in range(count):
            emit(second + .01 * (index + 1), encoder.mission_item_int_encode(
                1, 1, index, mav.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                mav.MAV_CMD_NAV_TAKEOFF if index == 1 else mav.MAV_CMD_NAV_WAYPOINT,
                0, 1, 0, 0, 0, 0, 100000000 + index * 1000, 200000000 + index * 1000, 3,
            ), (255, 190))
        emit(second + .2, encoder.mission_ack_encode(255, 190, mav.MAV_MISSION_ACCEPTED))
    emit(10, encoder.heartbeat_encode(2, 3, 128, 3, 4))
    emit(10.1, encoder.extended_sys_state_encode(0, mav.MAV_LANDED_STATE_IN_AIR))
    for second in range(11, 21):
        emit(second, encoder.global_position_int_encode(second * 1000, 100000000 + (second - 11) * 100,
                                                       200000000 + (second - 11) * 100, 103000, 3000, 0, 0, 0, 0))
        emit(second, encoder.attitude_encode(second * 1000, .05, .02, .1, 0, 0, 0))
        emit(second, encoder.vibration_encode(second * 1_000_000, 4, 5, 66 if second == 14 else 12, 100 + (second - 11) * 2, 0, 0))
        if second == 15:
            emit(second + .01, encoder.statustext_encode(4, b"EKF3 IMU0 switching to compass 1"))
    emit(20.1, encoder.statustext_encode(2, b"Crash: Disarming: AngErr=160>30"))
    emit(20.2, encoder.heartbeat_encode(2, 3, 0, 3, 3))
    emit(21, encoder.statustext_encode(4, b"PreArm: Compasses inconsistent"))
    emit(22, encoder.radio_status_encode(180, 170, 95, 10, 10, 2, 0), (51, 68))
    return b"".join(frames)


def dataflash_bytes():
    output = bytearray()

    def raw(message_type, body):
        output.extend(bytes([0xA3, 0x95, message_type]) + body)

    def fmt(message_type, length, name, layout, columns):
        raw(128, struct.pack("<BB4s16s64s", message_type, length, name.encode(), layout.encode(), columns.encode()))

    fmt(128, 89, "FMT", "BBnNZ", "Type,Length,Name,Format,Columns")
    fmt(129, 75, "MSG", "QZ", "TimeUS,Message")
    fmt(130, 28, "VIBE", "QBfffI", "TimeUS,IMU,VibeX,VibeY,VibeZ,Clip")
    fmt(131, 31, "PARM", "QNf", "TimeUS,Name,Value")
    raw(129, struct.pack("<Q64s", 1_000_000, b"ArduCopter V4.7.2"))
    raw(131, struct.pack("<Q16sf", 1_000_001, b"BATT_MONITOR", 0))
    raw(130, struct.pack("<QBfffI", 2_000_000, 0, 1, 2, 12, 100))
    raw(130, struct.pack("<QBfffI", 3_000_000, 0, 1, 2, 66, 105))
    raw(129, struct.pack("<Q64s", 4_000_000, b"Crash: Disarming: AngErr=160>30"))
    return bytes(output)


def generate(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "demo.tlog").write_bytes(tlog_bytes())
    (folder / "demo.bin").write_bytes(dataflash_bytes())
    (folder / "demo.log").write_text(
        "FMT, 128, 89, FMT, BBnNZ, Type,Length,Name,Format,Columns\n"
        "FMT, 129, 75, MSG, QZ, TimeUS,Message\n"
        "FMT, 130, 28, VIBE, QBfffI, TimeUS,IMU,VibeX,VibeY,VibeZ,Clip\n"
        "MSG, 1000000, ArduCopter V4.7.2\n"
        "VIBE, 2000000, 0, 1, 2, 12, 100\n"
        "VIBE, 3000000, 0, 1, 2, 66, 105\n", encoding="utf-8",
    )
    with (folder / "demo.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["flight_id", "drone_id", "sequence", "timestamp", "latitude_deg", "longitude_deg",
                         "altitude_m", "battery_pct", "mode"])
        writer.writerow(["SYNTHETIC", "A", 0, "2026-10-29T12:00:00Z", 10, 20, 3, 90, "AUTO"])
        writer.writerow(["SYNTHETIC", "A", 1, "2026-10-29T12:00:01Z", 10.00001, 20.00001, 3, 89, "AUTO"])


if __name__ == "__main__":
    generate(Path(__file__).parent / "samples")
    print("Generated synthetic demo.tlog, demo.bin, demo.log and demo.csv under samples.")
