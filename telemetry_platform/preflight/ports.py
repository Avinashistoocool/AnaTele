"""Find the vehicle automatically: list serial ports (best candidates first) and probe them for a MAVLink heartbeat.

Probing only *listens*; nothing is sent to the vehicle until a port has answered with an autopilot heartbeat.
"""
import re
from dataclasses import dataclass

# USB vendor ids: ArduPilot/ChibiOS boards (pid.codes, CubePilot, Holybro, mRo...) first, then common
# USB-serial bridges used by telemetry radios (FTDI, Silicon Labs CP210x, WCH CH340, STM32 VCP, 3DR).
AUTOPILOT_VIDS = {0x1209, 0x2DAE, 0x3162, 0x35A7, 0x26AC}
BRIDGE_VIDS = {0x0403, 0x10C4, 0x1A86, 0x0483, 0x067B}
UDP_DEFAULT = "udpin:0.0.0.0:14550"


@dataclass
class Port:
    device: str
    description: str
    rank: int  # 0 = autopilot USB, 1 = USB-serial bridge (radio), 2 = other, 3 = Bluetooth

    @property
    def label(self):
        kind = ("flight controller (USB)", "USB serial / telemetry radio", "serial port", "Bluetooth serial")[self.rank]
        return f"{self.device}  -  {self.description} [{kind}]"


def list_ports():
    """Serial ports, most likely flight controller first. Empty list if pyserial is unavailable."""
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        return []
    found = []
    for p in lp.comports():
        text = " ".join(str(x) for x in (p.description, p.manufacturer, p.product, p.hwid) if x)
        lowered = text.lower()
        if p.vid in AUTOPILOT_VIDS or any(k in lowered for k in ("ardupilot", "pixhawk", "cube", "px4", "chibios")):
            rank = 0
        elif "bluetooth" in lowered or "bthenum" in lowered:
            rank = 3
        elif p.vid in BRIDGE_VIDS or p.vid:
            rank = 1
        else:
            rank = 2
        found.append(Port(p.device, p.description or p.device, rank))
    return sorted(found, key=lambda port: (port.rank, [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", port.device)]))


def candidates(baud=115200, include_bluetooth=False, include_udp=True):
    """Ordered (connection string, baud) pairs to probe."""
    out = []
    for port in list_ports():
        if port.rank == 3 and not include_bluetooth:
            continue
        bauds = [baud] if port.rank == 0 else list(dict.fromkeys([baud, 57600, 115200]))
        out += [(port.device, b) for b in bauds]
    if include_udp:
        out.append((UDP_DEFAULT, baud))
    return out


def auto_detect(baud=115200, timeout=2.5, include_bluetooth=False, include_udp=True, log=None):
    """Return an open Link to the first candidate that sends an autopilot heartbeat, or raise LinkError."""
    from .link import Link, LinkError

    tried = []
    for device, rate in candidates(baud, include_bluetooth, include_udp):
        if log:
            log(f"Scanning {device}" + ("" if device.startswith("udp") else f" @ {rate}") + " ...")
        try:
            link = Link.open(device, rate)
        except LinkError as error:
            tried.append(f"{device}: {error}")
            continue
        try:
            if link.wait_heartbeat(timeout) is not None:
                link.description = (f"{device} @ {rate}" if not device.startswith("udp") else device) + " (auto-detected)"
                return link
        except LinkError as error:
            tried.append(f"{device}: {error}")
        link.close()
        tried.append(f"{device}: no heartbeat")
    detail = "; ".join(tried) if tried else "no serial ports found"
    raise LinkError("Auto-detect found no vehicle (" + detail + "). Is it powered and plugged in, "
                    "and is Mission Planner closed? Bluetooth ports are skipped; enter them by hand.")
