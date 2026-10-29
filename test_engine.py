"""Synthetic tests for flight phases, the declarative rule engine, missions, diffs and trends."""
import copy
import os
import struct
import tempfile
import unittest
from pathlib import Path

os.environ["TELEMETRY_DISABLE_WORKER"] = "1"

from fastapi.testclient import TestClient
from pymavlink.dialects.v20 import ardupilotmega as mav

from make_samples import BASE
from telemetry_platform import derived, missions, rules, store
from telemetry_platform.analysis import run
from telemetry_platform.api import app


class Capture:
    """Builds a timestamped Mission Planner capture."""

    def __init__(self):
        self.encoder = mav.MAVLink(None)
        self.frames, self.seq = [], {}

    def emit(self, second, message, source=(1, 1)):
        self.encoder.srcSystem, self.encoder.srcComponent = source
        self.encoder.seq = self.seq.get(source, 0)
        self.seq[source] = (self.encoder.seq + 1) % 256
        self.frames.append(struct.pack(">Q", BASE + int(second * 1e6)) + message.pack(self.encoder))

    def bytes(self):
        # Stable sort keeps same-timestamp order; real captures are written in arrival order.
        return b"".join(sorted(self.frames, key=lambda frame: frame[:8]))


def flight(vibration=lambda t: 10.0, crash=True, climb=True, landed_state=False, params=True, voltage=0,
           motors=lambda t: (1500, 1500, 1500, 1500)):
    """Armed at 1 s, climbs from 3 s, crash report at 20 s when requested. 10 Hz streams."""
    c = Capture()
    e = c.encoder
    c.emit(0, e.heartbeat_encode(mav.MAV_TYPE_GCS, mav.MAV_AUTOPILOT_INVALID, 0, 0, 4), (255, 190))
    c.emit(0, e.heartbeat_encode(2, 3, 0, 3, 3))
    c.emit(0.01, e.statustext_encode(6, b"ArduCopter V4.7.2 (fb773567)"))
    if params:
        values = [("SERVO1_FUNCTION", 33), ("SERVO2_FUNCTION", 34), ("SERVO3_FUNCTION", 35), ("SERVO4_FUNCTION", 36),
                  ("MOT_PWM_MIN", 1000), ("MOT_PWM_MAX", 2000), ("FRAME_CLASS", 1), ("FRAME_TYPE", 1),
                  ("WP_RADIUS_M", 2)]
        floats = [("MOT_SPIN_MIN", .15), ("MOT_SPIN_MAX", .95)]
        total = len(values) + len(floats)
        for index, (name, value) in enumerate(values):
            c.emit(.1, e.param_value_encode(name.encode(), value, mav.MAV_PARAM_TYPE_INT16, total, index))
        for index, (name, value) in enumerate(floats, len(values)):
            c.emit(.1, e.param_value_encode(name.encode(), value, mav.MAV_PARAM_TYPE_REAL32, total, index))
    end = 22 if crash else 30
    armed_until = 21 if crash else 28
    for step in range(0, int(end * 10)):
        t = step / 10
        armed = 1 <= t < armed_until
        if step % 10 == 0:
            c.emit(t, e.heartbeat_encode(2, 3, 128 if armed else 0, 3, 4))
        altitude = 0.0 if not climb or t < 3 else min(5.0, (t - 3) * 1.0)
        c.emit(t, e.global_position_int_encode(int(t * 1000), 100000000 + int(t * 50), 200000000, 100000,
                                               int(altitude * 1000), 0, 0, 0, 0))
        c.emit(t, e.vfr_hud_encode(0, 0, 0, 40 if armed and t >= 2.5 else 0, 100 + altitude, 0))
        roll = 0.0 if not crash or t < 18 else min(170, (t - 18) * 80)
        c.emit(t, e.attitude_encode(int(t * 1000), roll / 57.2958, 0, 0, 0, 0, 0))
        c.emit(t, e.nav_controller_output_encode(0, 0, 0, 0, 0, 0, 0, 0))
        outputs = motors(t) if armed and t >= 2.5 else (1000, 1000, 1000, 1000)
        c.emit(t, e.servo_output_raw_encode(int(t * 1e6), 0, *outputs, 0, 0, 0, 0))
        c.emit(t, e.sys_status_encode(0, 0, 0, 500, voltage, -1, -1, 0, 0, 0, 0, 0, 0))
        if step % 2 == 0:
            c.emit(t, e.vibration_encode(int(t * 1e6), vibration(t), vibration(t), vibration(t), int(t), 0, 0))
            c.emit(t, e.ekf_status_report_encode(831, 0, 0, 0, 0, 0.2))
        if landed_state and step % 5 == 0:
            c.emit(t, e.extended_sys_state_encode(0, 2 if 3 <= t < armed_until else 1))
    if crash:
        c.emit(16, e.statustext_encode(4, b"EKF3 IMU0 switching to compass 1"))
        c.emit(20, e.statustext_encode(2, b"Crash: Disarming: AngErr=160>30"))
    return c.bytes()


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = store.DATA, store.DB
        store.DATA = Path(self.temp.name)
        store.DB = store.DATA / "platform.db"
        store.initialize()
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.context.__exit__(None, None, None)
        store.DATA, store.DB = self.old
        self.temp.cleanup()

    def upload(self, blob, name="f.tlog", profile_id=None):
        data = {"profile_id": profile_id} if profile_id else {}
        response = self.client.post("/api/logs", files={"file": (name, blob)}, data=data)
        self.assertEqual(response.status_code, 202, response.text)
        job_id = response.json()["id"]
        self.assertEqual(store.claim(), job_id)
        run(job_id)
        job = store.get_job(job_id)
        self.assertIn(job["status"], ("complete", "partial"), job["error"])
        return job_id, job["manifest"]

    def rule(self, manifest, rule_id, episode=0):
        rows = [r for r in manifest["rule_results"] if r["rule_id"] == rule_id
                and r["scope"].get("episode", episode) == episode]
        self.assertTrue(rows, rule_id)
        return rows[0]

    def test_inferred_takeoff_without_landed_state(self):
        _, m = self.upload(flight())
        phase = m["phases"][0]
        self.assertEqual(phase["status"], "airborne_inferred")
        takeoff = (phase["takeoff_us"] - BASE) / 1e6
        self.assertTrue(2.9 <= takeoff <= 3.3, takeoff)
        self.assertLess(phase["pre_event_end_us"], phase["critical_event_us"])
        self.assertEqual(m["armed_episodes"][0]["airborne"], "inferred from altitude rise and demand")
        self.assertTrue(any(e["kind"] == "PHASE" and "Inferred takeoff" in e["text"] for e in m["events"]))

    def test_reported_landed_state_wins(self):
        _, m = self.upload(flight(landed_state=True))
        self.assertEqual(m["phases"][0]["status"], "airborne_reported")

    def test_ground_only_episode_is_not_applicable(self):
        _, m = self.upload(flight(climb=False, crash=False))
        self.assertEqual(m["phases"][0]["status"], "no_airborne_evidence")
        self.assertEqual(self.rule(m, "vibration-persistent-pre-event")["state"], "not_applicable")

    def test_vibration_persistence_versus_impact_peak(self):
        # Short pre-event spike (< 2 s) plus a large impact-only peak: peak rule fires, persistence does not.
        _, m = self.upload(flight(vibration=lambda t: 45.0 if 8 <= t < 9 else 80.0 if t >= 19 else 10.0))
        self.assertEqual(self.rule(m, "vibration-persistent-pre-event")["state"], "not_triggered")
        self.assertIn("event and after max 80", self.rule(m, "vibration-persistent-pre-event")["detail"])
        self.assertTrue(any(r["rule_id"] == "vibration-peak-sample" and r["state"] == "triggered" for r in m["rule_results"]))
        # Sustained > 30 for 3 s before the event triggers.
        _, m = self.upload(flight(vibration=lambda t: 40.0 if 8 <= t < 11 else 10.0))
        result = self.rule(m, "vibration-persistent-pre-event")
        self.assertEqual(result["state"], "triggered")
        self.assertTrue(result["evidence"])

    def test_clipping_increments_are_phase_scoped(self):
        _, m = self.upload(flight())
        result = self.rule(m, "clipping-pre-event")
        self.assertEqual(result["state"], "triggered")
        self.assertGreater(result["measurements"]["imu.0.clipping_counter"]["pre_event_increment"], 0)

    def test_power_gap_blocks_power_hypothesis(self):
        _, m = self.upload(flight())
        self.assertEqual(self.rule(m, "power-monitoring-gap")["state"], "triggered")
        power = next(h for h in m["incidents"][0]["hypotheses"] if h["id"] == "power-degradation")
        self.assertEqual(power["label"], "unassessable")
        _, m = self.upload(flight(voltage=12000))
        self.assertEqual(self.rule(m, "power-monitoring-gap")["state"], "not_triggered")

    def test_motor_mapping_and_yaw_imbalance(self):
        _, m = self.upload(flight(motors=lambda t: (1350, 1350, 1650, 1650)))
        result = self.rule(m, "motor-output-imbalance-pre-event")
        self.assertEqual(result["state"], "triggered")
        self.assertEqual(result["measurements"]["dominant_axis"], "yaw (CCW minus CW props)")
        self.assertIn("predominantly yaw-axis", result["detail"])
        self.assertEqual(m["motor_maps"]["1:1"]["frame"], "QUAD/X")
        balanced = self.upload(flight(motors=lambda t: (1500, 1500, 1500, 1500)))[1]
        self.assertEqual(self.rule(balanced, "motor-output-imbalance-pre-event")["state"], "not_triggered")
        unmapped = self.upload(flight(params=False))[1]
        self.assertEqual(self.rule(unmapped, "motor-output-imbalance-pre-event")["state"], "unassessable")

    def test_tracking_error_and_compass_sequence(self):
        _, m = self.upload(flight())
        sequence = [r for r in m["rule_results"] if r["rule_id"] == "compass-event-before-crash-check"][0]
        self.assertEqual(sequence["state"], "triggered")
        self.assertIn("4.0 s before", sequence["detail"])
        self.assertTrue(any(s["name"] == "control.tilt_tracking_error_deg" for s in m["signals"]))
        for incident in m["incidents"]:
            self.assertTrue(all(h["probability"] is None for h in incident["hypotheses"]))

    def test_all_rule_states_are_valid_and_semantics_typed(self):
        _, m = self.upload(flight())
        self.assertTrue(all(r["state"] in rules.STATES for r in m["rule_results"]))
        self.assertEqual(m["sources"][1]["semantic_pack"] if m["sources"][1]["key"] == "1:1" else
                         next(s for s in m["sources"] if s["key"] == "1:1")["semantic_pack"], "arducopter-4.7.2")
        snapshot = m["parameters"][0]
        self.assertEqual(snapshot["values"]["SERVO1_FUNCTION"]["value"], 33)
        self.assertEqual(snapshot["values"]["SERVO1_FUNCTION"]["encoding"], "c_cast_per_semantic_pack")

    def test_rule_pack_validation(self):
        pack = rules.load_pack()
        self.assertEqual(len(pack["sha256"]), 64)
        broken = copy.deepcopy(pack)
        broken["rules"][0]["detector"]["type"] = "python_exec"
        with self.assertRaises(rules.RuleError):
            rules.validate(broken)
        broken = copy.deepcopy(pack)
        broken["rules"].append(copy.deepcopy(broken["rules"][0]))
        with self.assertRaises(rules.RuleError):
            rules.validate(broken)

    def test_profile_expected_parameters_and_diff(self):
        profile = self.client.post("/api/profiles", json={
            "drone_key": "quad", "name": "Quad", "expected_parameters": {"FRAME_CLASS": 1, "MOT_PWM_MIN": 1100}}).json()
        self.assertEqual(self.client.post("/api/profiles", json={
            "drone_key": "quad", "name": "Quad", "expected_parameters": {"bad name": 1}}).status_code, 422)
        job_id, m = self.upload(flight(), profile_id=profile["id"])
        mismatch = self.rule(m, "profile-parameter-mismatch")
        self.assertEqual(mismatch["state"], "triggered")
        self.assertEqual(mismatch["measurements"]["differences"][0]["name"], "MOT_PWM_MIN")
        diff = self.client.get(f"/api/logs/{job_id}/parameter-diff").json()
        self.assertEqual([c["name"] for c in diff["changed"]], ["MOT_PWM_MIN"])
        other_id, _ = self.upload(flight(params=False), profile_id=profile["id"])
        diff = self.client.get(f"/api/logs/{job_id}/parameter-diff", params={"against_log": other_id})
        self.assertEqual(diff.status_code, 409)

    def test_compare_and_trends(self):
        profile = self.client.post("/api/profiles", json={"drone_key": "quad-7", "name": "Q7"}).json()
        a, _ = self.upload(flight(vibration=lambda t: 10.0), profile_id=profile["id"])
        b, _ = self.upload(flight(vibration=lambda t: 20.0), profile_id=profile["id"])
        comparison = self.client.get("/api/compare", params={"left": a, "right": b}).json()
        row = next(r for r in comparison["metrics"] if r["metric"] == "vibration_z_mean")
        self.assertAlmostEqual(row["delta"], 10.0, places=3)
        self.assertFalse(comparison["parameters"]["changed"])
        trends = self.client.get("/api/drones/quad-7/trends").json()
        self.assertEqual(len(trends["flights"]), 2)
        self.assertEqual([p["value"] for p in trends["series"]["vibration_z_mean"]], [10.0, 20.0])
        self.assertEqual(self.client.get("/api/drones/nobody/trends").json()["flights"], [])
        rules_api = self.client.get(f"/api/logs/{a}/rules", params={"state": "unassessable"}).json()
        self.assertTrue(all(r["state"] == "unassessable" for r in rules_api["items"]))
        self.assertIn("Rule coverage", self.client.get(f"/api/logs/{a}/report").text)


class MissionTests(unittest.TestCase):
    sources = {"1:1": {"role": "vehicle"}, "255:190": {"role": "ground_station"}}

    def record(self, rid, t, source, name, **payload):
        return {"id": rid, "time_us": t * 1_000_000, "source": source, "message": name, "epoch": 0,
                "payload": {"mavpackettype": name, "mission_type": 0, **payload}}

    def item(self, rid, t, seq, sender="255:190", target=(1, 1), x=100000000, y=200000000, command=16):
        return self.record(rid, t, sender, "MISSION_ITEM_INT", target_system=target[0], target_component=target[1],
                           seq=seq, frame=3, command=command, x=x, y=y, z=3.0, current=0, autocontinue=1,
                           param1=0, param2=0, param3=0, param4=0)

    def build(self, messages):
        builder = missions.Reconstructor(self.sources)
        for message in messages:
            builder.consume(message)
        builder.finish()
        return builder.transactions

    def test_broadcast_ack_and_retransmission(self):
        tx = self.build([
            self.record(1, 1, "255:190", "MISSION_COUNT", target_system=1, target_component=1, count=2),
            self.item(2, 1, 0), self.item(3, 1, 1), self.item(4, 1, 1),
            self.record(5, 2, "1:1", "MISSION_ACK", target_system=0, target_component=0, type=0),
        ])
        self.assertEqual(tx[0]["status"], "accepted_with_all_items_observed")
        self.assertEqual(tx[0]["retransmitted_items"], 1)
        self.assertTrue(any("broadcast" in note for note in tx[0]["notes"]))

    def test_partial_write_and_interrupted_upload_versions(self):
        tx = self.build([
            self.record(1, 1, "255:190", "MISSION_COUNT", target_system=1, target_component=1, count=3),
            self.item(2, 1, 0), self.item(3, 1, 1), self.item(4, 1, 2, x=100001000),
            self.record(5, 2, "1:1", "MISSION_ACK", target_system=255, target_component=190, type=0),
            self.record(6, 3, "255:190", "MISSION_WRITE_PARTIAL_LIST", target_system=1, target_component=1,
                        start_index=2, end_index=2),
            self.item(7, 3, 2, x=100002000),
            self.record(8, 4, "1:1", "MISSION_ACK", target_system=255, target_component=190, type=0),
            self.record(9, 5, "255:190", "MISSION_COUNT", target_system=1, target_component=1, count=3),
            self.item(10, 5, 0),
            self.record(11, 6, "1:1", "MISSION_ACK", target_system=255, target_component=190, type=mav.MAV_MISSION_ERROR),
        ])
        self.assertEqual([t["kind"] for t in tx], ["upload", "partial_write", "upload"])
        self.assertEqual(tx[2]["status"], "rejected_or_interrupted")
        versions = missions.plan_versions(tx)
        self.assertEqual([v["confidence"] for v in versions], ["confirmed_by_ack", "derived_from_baseline", "uncertain"])
        self.assertEqual(versions[1]["items"]["2"]["x"], 100002000)
        self.assertEqual(versions[1]["items"]["1"]["record_id"], 3)

    def test_partial_write_without_baseline_is_uncertain(self):
        tx = self.build([
            self.record(1, 1, "255:190", "MISSION_WRITE_PARTIAL_LIST", target_system=1, target_component=1,
                        start_index=1, end_index=1),
            self.item(2, 1, 1),
            self.record(3, 2, "1:1", "MISSION_ACK", target_system=255, target_component=190, type=0),
        ])
        self.assertEqual(missions.plan_versions(tx)[0]["confidence"], "uncertain")

    def test_waypoint_status_uses_accuracy_margin(self):
        tx = self.build([
            self.record(1, 1, "255:190", "MISSION_COUNT", target_system=1, target_component=1, count=3),
            self.item(2, 1, 0), self.item(3, 1, 1), self.item(4, 1, 2, x=100000300),
            self.record(5, 2, "1:1", "MISSION_ACK", target_system=255, target_component=190, type=0),
        ])
        versions = missions.plan_versions(tx)
        episodes = [{"source": "1:1", "epoch": 0, "start_us": 10_000_000, "end_us": 20_000_000}]
        phases = [{"takeoff_us": 11_000_000}]
        # Track ends ~2.9 m south of item 2 (300e-7 deg lat ≈ 3.3 m); item 1 is visited.
        track = {("1:1", 0): [(11_000_000 + i * 1_000_000, 10.0 + i * 0.0000004 / 9, 20.0, 100 + i) for i in range(10)]}
        near = missions.execution(versions, episodes, phases, [], track, lambda *a: (1.5, 99), (2.0, "test"))
        statuses = {i["seq"]: i["status"] for i in near[0]["items"]}
        self.assertEqual(statuses[1], "geometry_consistent_with_completion")
        self.assertEqual(statuses[2], "geometry_consistent_with_completion")
        far = missions.execution(versions, episodes, phases, [], track, lambda *a: (0.1, 99), (0.5, "test"))
        self.assertEqual({i["seq"]: i["status"] for i in far[0]["items"]}[2], "not_visited_in_observed_trajectory")
        reached = missions.execution(versions, episodes, phases,
                                     [{"source": "1:1", "t_us": 12_000_000, "kind": "MISSION_ITEM_REACHED", "seq": 2, "record_id": 7}],
                                     track, lambda *a: None, (0.5, "test"))
        self.assertEqual({i["seq"]: i["status"] for i in reached[0]["items"]}[2], "reported_complete")


class DerivedTests(unittest.TestCase):
    def test_tilt_error_is_thrust_vector_angle(self):
        self.assertAlmostEqual(derived.tilt_error_deg((0, 0), (0, 0)), 0)
        self.assertAlmostEqual(derived.tilt_error_deg((0, 0), (30, 0)), 30, places=5)
        self.assertAlmostEqual(derived.tilt_error_deg((0, 0), (180, 0)), 180, places=5)

    def test_quaternion_roll_pitch(self):
        import math
        half = math.radians(20) / 2
        roll, pitch = derived.quaternion_roll_pitch([math.cos(half), math.sin(half), 0, 0])
        self.assertAlmostEqual(roll, 20, places=5)
        self.assertAlmostEqual(pitch, 0, places=5)


if __name__ == "__main__":
    unittest.main()
