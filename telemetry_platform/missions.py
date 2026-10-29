"""Mission transfer transactions, onboard-plan state and execution evidence.

Transfers are reconstructed from observed messages only. ArduPilot uploads are not atomic,
so an interrupted overwrite leaves the onboard plan uncertain rather than "the last accepted plan".
"""
import math

from pymavlink.dialects.v20 import ardupilotmega as mav

GLOBAL_FRAMES = {0, 3, 5, 6, 10, 11}  # GLOBAL, GLOBAL_RELATIVE_ALT, *_INT variants, TERRAIN_ALT
COMMAND_NAMES = {16: "NAV_WAYPOINT", 17: "NAV_LOITER_UNLIM", 18: "NAV_LOITER_TURNS", 19: "NAV_LOITER_TIME",
                 20: "NAV_RETURN_TO_LAUNCH", 21: "NAV_LAND", 22: "NAV_TAKEOFF", 82: "NAV_SPLINE_WAYPOINT",
                 177: "DO_JUMP", 178: "DO_CHANGE_SPEED", 183: "DO_SET_SERVO"}
RESULTS = {key: item.name for key, item in mav.enums["MAV_MISSION_RESULT"].items()}
DEFAULT_RADIUS_M = 2.0
MAX_POSITION_GAP_US = 5_000_000
LIMITATION = ("Transfers are reconstructed from captured messages; broadcast targets are matched to the only plausible "
              "transaction. Interrupted or rejected overwrites leave the onboard plan uncertain (ArduPilot uploads are "
              "not atomic). Waypoint statuses compare recorded estimated positions with planned coordinates; proximity is "
              "not proof of completion and a missing MISSION_ITEM_REACHED is not proof of failure.")


def command_name(command):
    return COMMAND_NAMES.get(command, f"command {command}")


def system(source):
    return source.split(":")[0] if ":" in source else source


def horizontal_m(lat1, lon1, lat2, lon2):
    radius = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def item_position(item):
    if item.get("frame") not in GLOBAL_FRAMES or item.get("x") in (None, 0) or item.get("y") in (None, 0):
        return None
    scale = 1e-7 if item.get("mavpackettype") == "MISSION_ITEM_INT" else 1.0
    return item["x"] * scale, item["y"] * scale


class Reconstructor:
    def __init__(self, sources):
        self.vehicles = {system(key) for key, value in sources.items() if value["role"] == "vehicle" and ":" in key}
        self.transactions = []
        self.active = {}
        self.diagnostics = []

    def new(self, kind, record, gcs, vehicle, mission_type, **extra):
        tx = {"id": len(self.transactions) + 1, "kind": kind,
              "direction": {"upload": "upload", "partial_write": "upload", "download": "download"}.get(kind, kind),
              "sender": record["source"], "gcs": gcs, "vehicle": vehicle, "target": f"{vehicle}" if kind == "download" else None,
              "mission_type": mission_type, "count": None, "items": {}, "requested": [], "retransmitted_items": 0,
              "conflicting_items": [], "status": "unconfirmed", "evidence": [record["id"]], "t_us": record["time_us"],
              "end_us": record["time_us"], "epoch": record["epoch"], "notes": [], **extra}
        self.transactions.append(tx)
        return tx

    def key(self, gcs, vehicle, mission_type, epoch):
        return (gcs, vehicle, mission_type, epoch)

    def find(self, gcs, vehicle, mission_type, epoch):
        """Exact match, or the single open transaction for the vehicle when the GCS address is broadcast/unknown."""
        if gcs is not None:
            tx = self.active.get(self.key(gcs, vehicle, mission_type, epoch))
            if tx is not None:
                return tx
        candidates = [tx for key, tx in self.active.items() if key[1] == vehicle and key[2] == mission_type and key[3] == epoch]
        if len(candidates) == 1:
            if gcs is None or gcs.split(":")[1] == "0" or gcs.startswith("0:"):
                candidates[0]["notes"].append("Matched a broadcast-addressed message to the only open transaction.")
            return candidates[0]
        return None

    def close(self, tx, status):
        tx["status"] = status
        for key, value in list(self.active.items()):
            if value is tx:
                del self.active[key]

    def consume(self, record):
        p, name, sender = record["payload"], record["message"], record["source"]
        mission_type, epoch = p.get("mission_type", 0), record["epoch"]
        from_vehicle = system(sender) in self.vehicles
        target = f"{p.get('target_system')}:{p.get('target_component')}" if "target_system" in p else None
        if from_vehicle:
            vehicle, gcs = system(sender), target if target and not target.startswith("0:") else None
        else:
            vehicle, gcs = str(p.get("target_system")), sender
            if vehicle == "0" and len(self.vehicles) == 1:
                vehicle = next(iter(self.vehicles))
        if name == "MISSION_COUNT":
            if from_vehicle:
                tx = self.find(gcs, vehicle, mission_type, epoch)
                if tx is None or tx["kind"] != "download":
                    tx = self.new("download", record, gcs, vehicle, mission_type)
                    self.active[self.key(gcs, vehicle, mission_type, epoch)] = tx
                tx["count"] = p.get("count")
                tx["evidence"].append(record["id"])
                return
            old = self.active.get(self.key(gcs, vehicle, mission_type, epoch))
            if old is not None and old["kind"] in ("upload", "partial_write"):
                self.close(old, "superseded_before_items" if not old["items"] else "restarted_before_completion")
                old["notes"].append(f"A new MISSION_COUNT (record {record['id']}) restarted the transfer before an ACK.")
            tx = self.new("upload", record, gcs, vehicle, mission_type, count=p.get("count"))
            tx["target"] = f"{p.get('target_system')}:{p.get('target_component')}"
            self.active[self.key(gcs, vehicle, mission_type, epoch)] = tx
        elif name == "MISSION_REQUEST_LIST" and not from_vehicle:
            tx = self.new("download", record, gcs, vehicle, mission_type)
            self.active[self.key(gcs, vehicle, mission_type, epoch)] = tx
        elif name == "MISSION_WRITE_PARTIAL_LIST" and not from_vehicle:
            tx = self.new("partial_write", record, gcs, vehicle, mission_type,
                          start_index=p.get("start_index"), end_index=p.get("end_index"))
            tx["target"] = target
            tx["count"] = (p["end_index"] - p["start_index"] + 1) if p.get("end_index", -1) >= p.get("start_index", 0) else None
            self.active[self.key(gcs, vehicle, mission_type, epoch)] = tx
        elif name == "MISSION_CLEAR_ALL" and not from_vehicle:
            tx = self.new("clear", record, gcs, vehicle, mission_type)
            self.active[self.key(gcs, vehicle, mission_type, epoch)] = tx
        elif name in ("MISSION_REQUEST", "MISSION_REQUEST_INT"):
            tx = self.find(gcs, vehicle, mission_type, epoch)
            if tx is None and not from_vehicle and name == "MISSION_REQUEST_INT":
                tx = self.new("item_read", record, gcs, vehicle, mission_type, count=1)
                self.active[self.key(gcs, vehicle, mission_type, epoch)] = tx
            if tx is not None:
                tx["requested"].append(p.get("seq"))
                tx["evidence"].append(record["id"])
        elif name in ("MISSION_ITEM", "MISSION_ITEM_INT"):
            tx = self.find(gcs, vehicle, mission_type, epoch)
            if tx is None:
                return
            expected_sender_vehicle = tx["kind"] in ("download", "item_read")
            if expected_sender_vehicle != from_vehicle:
                return
            seq = str(p.get("seq"))
            item = {**p, "record_id": record["id"]}
            old = tx["items"].get(seq)
            if old is not None:
                same = {k: v for k, v in old.items() if k != "record_id"} == p
                if same:
                    tx["retransmitted_items"] += 1
                else:
                    tx["conflicting_items"].append(seq)
            tx["items"][seq] = item
            tx["evidence"].append(record["id"])
            tx["end_us"] = record["time_us"]
            if tx["kind"] == "item_read":
                self.close(tx, "item_observed")
        elif name == "MISSION_ACK":
            tx = self.find(gcs, vehicle, mission_type, epoch)
            if tx is None:
                return
            # The receiver of a transfer sends the ACK: the vehicle for uploads, the GCS for downloads.
            receiver_is_vehicle = tx["kind"] in ("upload", "partial_write", "clear")
            if receiver_is_vehicle != from_vehicle:
                return
            tx["evidence"].append(record["id"])
            tx["end_us"] = record["time_us"]
            result = p.get("type")
            tx["ack"] = RESULTS.get(result, str(result))
            if tx["kind"] == "clear":
                self.close(tx, "accepted" if result == 0 else "rejected")
                return
            if tx["kind"] == "partial_write":
                wanted = {str(i) for i in range(tx["start_index"], tx["end_index"] + 1)} if tx["count"] else set()
            else:
                wanted = {str(i) for i in range(tx["count"] or 0)}
            complete = wanted <= set(tx["items"])
            if result == 0:
                self.close(tx, "accepted_with_all_items_observed" if complete else "accepted_but_items_missing")
            else:
                self.close(tx, "rejected_or_interrupted")

    def finish(self):
        for tx in list(self.active.values()):
            self.close(tx, tx["status"] if tx["status"] != "unconfirmed" else "unconfirmed")
            tx["notes"].append("No acknowledgement observed before the end of the capture.")


def plan_versions(transactions):
    versions, current = [], None
    for tx in sorted(transactions, key=lambda t: (t["epoch"], t["end_us"])):
        if tx["mission_type"] not in (0, None):
            continue
        version = None
        if tx["kind"] == "upload" and tx["status"] == "accepted_with_all_items_observed":
            version = {"items": tx["items"], "confidence": "confirmed_by_ack", "basis": "full upload accepted"}
        elif tx["kind"] == "download" and tx["status"] == "accepted_with_all_items_observed":
            version = {"items": tx["items"], "confidence": "observed_onboard", "basis": "full download"}
        elif tx["kind"] == "partial_write" and tx["status"] == "accepted_with_all_items_observed":
            if current and current["confidence"] != "uncertain":
                merged = dict(current["items"])
                merged.update(tx["items"])
                version = {"items": merged, "confidence": "derived_from_baseline", "basis": f"partial write onto version {current['id']}"}
            else:
                version = {"items": tx["items"], "confidence": "uncertain", "basis": "partial write without a known baseline"}
        elif tx["kind"] == "clear" and tx["status"] == "accepted":
            version = {"items": {}, "confidence": "confirmed_by_ack", "basis": "mission cleared"}
        elif tx["kind"] in ("upload", "partial_write") and tx["items"]:
            version = {"items": current["items"] if current else {}, "confidence": "uncertain",
                       "basis": f"transfer {tx['id']} ({tx['status']}) may have partially overwritten the plan"}
        if version is None:
            continue
        current = {"id": len(versions) + 1, "transaction": tx["id"], "t_us": tx["end_us"], "epoch": tx["epoch"],
                   "vehicle": tx["vehicle"], **version}
        versions.append(current)
    return versions


def execution(versions, episodes, phases, events, positions, h_acc, radius_param):
    results = []
    for index, episode in enumerate(episodes):
        source = episode["source"]
        if ":" not in source:
            continue
        candidates = [v for v in versions if v["vehicle"] == system(source) and v["epoch"] == episode["epoch"]
                      and v["t_us"] <= episode["start_us"]]
        entry = {"episode": index, "source": source, "epoch": episode["epoch"], "plan_version": None, "items": [],
                 "notes": []}
        results.append(entry)
        if not candidates:
            entry["notes"].append("No observed plan before this episode; the onboard mission is unknown.")
            continue
        plan = candidates[-1]
        entry["plan_version"] = plan["id"]
        entry["plan_confidence"] = plan["confidence"]
        end = (episode["end_us"] or episode["start_us"]) + 2_000_000
        phase = phases[index] if index < len(phases) else None
        start = (phase or {}).get("takeoff_us") or episode["start_us"]
        reached = {}
        current = []
        for event in events:
            if event["source"] != source or not episode["start_us"] <= event["t_us"] <= end:
                continue
            if event["kind"] == "MISSION_ITEM_REACHED":
                reached.setdefault(event.get("seq"), event["record_id"])
            elif event["kind"] == "MISSION_CURRENT":
                current.append(event.get("seq"))
        entry["mission_current_sequence"] = current
        track = [p for p in positions.get((source, episode["epoch"]), []) if start <= p[0] <= end]
        gaps = [b[0] - a[0] for a, b in zip(track, track[1:])]
        coverage_ok = bool(track) and (max(gaps) if gaps else 0) <= MAX_POSITION_GAP_US
        radius, radius_basis = radius_param
        for seq in sorted(plan["items"], key=int):
            item = plan["items"][seq]
            if seq == "0":
                continue
            row = {"seq": int(seq), "command": command_name(item.get("command")), "record_id": item["record_id"],
                   "status": "unassessable", "nearest_m": None, "h_acc_m": None, "radius_m": radius, "evidence": [item["record_id"]]}
            if int(seq) in reached:
                row["status"] = "reported_complete"
                row["evidence"].append(reached[int(seq)])
            target = item_position(item)
            if item.get("command") == 22:
                row["note"] = "Takeoff is a vertical command, not a horizontal waypoint."
                if row["status"] != "reported_complete":
                    row["status"] = "completion_not_observed"
            elif target and track:
                best = min(track, key=lambda p: horizontal_m(p[1], p[2], *target))
                distance = horizontal_m(best[1], best[2], *target)
                row["nearest_m"], row["nearest_t_us"] = round(distance, 2), best[0]
                row["evidence"].append(best[3])
                accuracy = h_acc(source, episode["epoch"], best[0])
                if accuracy:
                    row["h_acc_m"] = round(accuracy[0], 2)
                    row["evidence"].append(accuracy[1])
                margin = radius + (accuracy[0] if accuracy else 0)
                if row["status"] != "reported_complete":
                    if distance <= margin:
                        row["status"] = "geometry_consistent_with_completion"
                    elif coverage_ok:
                        row["status"] = "not_visited_in_observed_trajectory"
                    else:
                        row["status"] = "completion_not_observed"
                row["note"] = (f"Nearest recorded estimated position {distance:.1f} m (horizontal, no interpolation); "
                               f"acceptance radius {radius:g} m ({radius_basis})"
                               + (f", GPS h_acc {accuracy[0]:.2f} m at that time." if accuracy else "; GPS accuracy unknown."))
            elif target is None:
                row["note"] = "Non-positional or non-global item; proximity not assessed."
                if row["status"] != "reported_complete":
                    row["status"] = "completion_not_observed" if current else "unassessable"
            else:
                row["note"] = "No recorded positions during the episode."
            entry["items"].append(row)
        if not coverage_ok:
            entry["notes"].append("Position coverage has gaps > 5 s or no samples; 'not visited' cannot be asserted.")
    return results


def summarize(messages, sources, episodes, phases, events, positions, h_acc, radius_param):
    builder = Reconstructor(sources)
    for record in messages:
        builder.consume(record)
    builder.finish()
    versions = plan_versions(builder.transactions)
    return {
        "transactions": builder.transactions,
        "plan_versions": [{k: v for k, v in version.items() if k != "items"} | {"item_count": len(version["items"])}
                          for version in versions],
        "execution": execution(versions, episodes, phases, events, positions, h_acc, radius_param),
        "limitation": LIMITATION,
    }
