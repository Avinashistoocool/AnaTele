"""File-only adapters. No arbitrary mavlink_connection strings or network transports."""
import csv
import math
import struct
from datetime import datetime, timezone
from pathlib import Path

from pymavlink import DFReader
from pymavlink.dialects.v20 import ardupilotmega as mav

MAX_RECORDS = 1_000_000
RECOVERY_LIMIT = 65536


class ParseError(ValueError):
    pass


def clean(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").rstrip("\x00")
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)) or type(value).__module__ == "array":
        return [clean(item) for item in value]
    return value


def sniff(path, kind):
    with Path(path).open("rb") as stream:
        prefix = stream.read(4096)
    if not prefix:
        raise ParseError("Empty file")
    if kind == "bin" and not prefix.startswith(b"\xa3\x95"):
        raise ParseError("Expected an ArduPilot DataFlash binary header (A3 95)")
    if kind == "log" and b"FMT," not in prefix and b"FMT, " not in prefix:
        raise ParseError("Expected DataFlash text with FMT records; arbitrary .log files are unsupported")
    if kind == "tlog" and not any(prefix[i] in (0xFD, 0xFE) for i in range(8, len(prefix))):
        raise ParseError("No plausible timestamp-prefixed MAVLink frames found")


def read_tlog(path, diagnostic):
    parser = mav.MAVLink(None)
    size = Path(path).stat().st_size
    skipped_start = None
    with Path(path).open("rb") as stream:
        while stream.tell() < size:
            start = stream.tell()
            prefix = stream.read(10)
            if len(prefix) < 10:
                diagnostic("truncated", start, "Trailing bytes cannot form a timestamped frame")
                break
            time_us = struct.unpack(">Q", prefix[:8])[0]
            marker, payload_size = prefix[8], prefix[9]
            plausible = 946684800000000 <= time_us <= 4102444800000000
            if marker not in (0xFD, 0xFE) or not plausible:
                if skipped_start is None:
                    skipped_start = start
                if start - skipped_start >= RECOVERY_LIMIT:
                    raise ParseError("More than 64 KiB without a plausible timestamped frame; recovery stopped")
                stream.seek(start + 1)
                continue
            header_length = 10 if marker == 0xFD else 6
            tail = stream.read(header_length - 2)
            if len(tail) != header_length - 2:
                diagnostic("truncated", start, "Incomplete MAVLink header")
                break
            header = prefix[8:] + tail
            signed = marker == 0xFD and bool(header[2] & 1)
            frame_length = header_length + payload_size + 2 + (13 if signed else 0)
            body = stream.read(frame_length - header_length)
            if len(body) != frame_length - header_length:
                diagnostic("truncated", start, "Incomplete MAVLink payload/checksum/signature")
                break
            frame = header + body
            if marker == 0xFD and header[2] & ~1:
                diagnostic("unsupported_flags", start, "Unsupported MAVLink2 incompatibility flag; frame excluded")
                continue
            try:
                message = parser.decode(bytearray(frame))
            except mav.MAVError as error:
                diagnostic("invalid_frame", start, str(error))
                if skipped_start is None:
                    skipped_start = start
                stream.seek(start + 1)
                continue
            if skipped_start is not None:
                diagnostic("recovered_span", skipped_start, f"Resynchronized after {start - skipped_start} bytes")
                skipped_start = None
            name = message.get_type()
            if name == "UNKNOWN" or name.startswith("UNKNOWN_"):
                diagnostic("unknown_definition", start, "Unknown message definition; not treated as CRC-validated evidence")
                continue
            payload = clean(message.to_dict())
            if name == "PARAM_VALUE":
                payload["wire_param_value_hex"] = frame[header_length:header_length + 4].hex()
            if name == "STATUSTEXT":
                payload["wire_text_hex"] = frame[header_length + 1:header_length + min(payload_size, 51)].ljust(50, b"\x00").hex()
            yield {
                "time_us": time_us, "clock": "capture_utc", "source": f"{message.get_srcSystem()}:{message.get_srcComponent()}",
                "message": name, "payload": payload, "offset": start, "length": 8 + frame_length,
                "sequence": message.get_seq(), "quality": "crc_valid;signature_unverified" if signed else "crc_valid;unsigned",
            }
    if skipped_start is not None:
        diagnostic("unrecovered_tail", skipped_start, "Trailing corrupt region was not recovered")


def read_dataflash(path, kind, diagnostic):
    reader = DFReader.DFReader_binary(str(path), zero_time_base=True) if kind == "bin" else DFReader.DFReader_text(
        str(path), zero_time_base=True)
    last_time = 0
    try:
        while True:
            before = reader.offset
            message = reader.recv_msg()
            after = reader.offset
            if message is None:
                if before < Path(path).stat().st_size:
                    diagnostic("dataflash_tail", before, "DataFlash reader stopped with unparsed bytes; inspect original")
                break
            payload = clean(message.to_dict())
            name = message.get_type()
            if kind == "bin":
                start = after - message.fmt.len
                if start > before:
                    diagnostic("recovered_span", before, f"DataFlash reader skipped {start - before} bytes")
                length = message.fmt.len
            else:
                start, length = before, after - before
            clock = "boot_us"
            if isinstance(payload.get("TimeUS"), (int, float)):
                last_time = int(payload["TimeUS"])
            elif isinstance(payload.get("TimeMS"), (int, float)):
                last_time = int(payload["TimeMS"]) * 1000
            else:
                clock = "boot_us_inherited"
            yield {
                "time_us": last_time, "clock": clock, "source": "dataflash", "message": name,
                "payload": payload, "offset": start, "length": length, "sequence": None,
                "quality": "format_decoded;not_authenticated",
            }
    finally:
        if getattr(reader, "data_map", None) is not None:
            reader.data_map.close()
        if getattr(reader, "filehandle", None) is not None:
            reader.filehandle.close()


def read_csv(path, diagnostic):
    required = {"flight_id", "drone_id", "sequence", "timestamp", "latitude_deg", "longitude_deg",
                "altitude_m", "battery_pct", "mode"}
    seen = {}
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or set(reader.fieldnames) != required or len(reader.fieldnames) != len(required):
            raise ParseError("CSV requires exactly: " + ", ".join(sorted(required)))
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ParseError(f"CSV line {reader.line_num}: malformed row")
            try:
                point = {key: value.strip() for key, value in row.items()}
                dt = datetime.fromisoformat(point["timestamp"].replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    raise ValueError("timezone required")
                for key in ("latitude_deg", "longitude_deg", "altitude_m", "battery_pct"):
                    point[key] = float(point[key])
                    if not math.isfinite(point[key]):
                        raise ValueError(f"{key} must be finite")
                if not -90 <= point["latitude_deg"] <= 90 or not -180 <= point["longitude_deg"] <= 180:
                    raise ValueError("coordinates out of range")
                if not 0 <= point["battery_pct"] <= 100 or point["altitude_m"] < 0:
                    raise ValueError("battery/AGL altitude out of range")
                point["sequence"] = int(point["sequence"])
                if point["sequence"] < 0 or not point["flight_id"] or not point["drone_id"] or not point["mode"]:
                    raise ValueError("IDs, mode, and a nonnegative sequence are required")
            except ValueError as error:
                raise ParseError(f"CSV line {reader.line_num}: {error}") from error
            key = (point["flight_id"], point["drone_id"], point["sequence"])
            if key in seen:
                if point != seen[key]:
                    raise ParseError(f"CSV line {reader.line_num}: conflicting duplicate sample")
                diagnostic("duplicate_csv", reader.line_num, "Identical sample ignored; locator is CSV line")
                continue
            seen[key] = point
            delta = dt.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
            time_us = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
            yield {
                "time_us": time_us, "clock": "sample_utc", "source": f"{point['flight_id']}/{point['drone_id']}",
                "message": "CSV_SAMPLE", "payload": point, "offset": reader.line_num,
                "length": 0, "sequence": point["sequence"], "quality": "csv_validated;operator_schema",
            }


def records(path, kind, diagnostic):
    sniff(path, kind)
    iterator = (read_tlog(path, diagnostic) if kind == "tlog" else
                read_csv(path, diagnostic) if kind == "csv" else read_dataflash(path, kind, diagnostic))
    for index, record in enumerate(iterator, 1):
        if index > MAX_RECORDS:
            raise ParseError("MVP record limit of 1,000,000 exceeded; file not published")
        record["id"] = index
        yield record
