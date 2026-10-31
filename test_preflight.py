"""AnaTele Preflight against the simulated FakeVehicle: full run, safety refusals, failures and timeouts."""
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from pymavlink.dialects.v20 import ardupilotmega as mavlink

from telemetry_platform.preflight import report
from telemetry_platform.preflight.fake import DEFAULT_PARAMS, FakeVehicle
from telemetry_platform.preflight.link import Link
from telemetry_platform.preflight.steps import (
    FAIL, NEEDS_USER, PASS, SKIPPED, WARN, AutoPrompter, Options, Runner, build_profile, load_profile, overall,
    save_profile,
)


def run(vehicle=None, prompter=None, **options):
    vehicle = vehicle or FakeVehicle()
    runner = Runner(Link(vehicle), Options(**options), AutoPrompter() if prompter is None else prompter)
    results = {r.id: r for r in runner.run()}
    return results, vehicle, runner


def commands(vehicle, command):
    return [params for cmd, params in vehicle.commands if cmd == command]


class PreflightTests(unittest.TestCase):
    def test_full_run_passes(self):
        profile = build_profile(DEFAULT_PARAMS, "quad")
        results, vehicle, runner = run(props_removed=True, profile=profile)
        self.assertEqual([r.status for r in results.values()], [PASS] * 8, {k: r.message for k, r in results.items()})
        self.assertEqual(runner.vehicle["firmware"], "4.6.3 (official)")
        calibrations = commands(vehicle, mavlink.MAV_CMD_PREFLIGHT_CALIBRATION)
        self.assertIn([1, 0, 0, 0, 0, 0, 0], calibrations)  # gyro
        self.assertIn([0, 0, 1, 0, 0, 0, 0], calibrations)  # baro
        self.assertIn([0, 0, 0, 0, 2, 0, 0], calibrations)  # level
        self.assertIn([0, 0, 0, 0, 1, 0, 0], calibrations)  # 6-position accel
        self.assertEqual(commands(vehicle, mavlink.MAV_CMD_DO_START_MAG_CAL), [[0, 1, 1, 0, 0, 0, 0]])
        positions = [int(p[0]) for p in commands(vehicle, mavlink.MAV_CMD_ACCELCAL_VEHICLE_POS)]
        self.assertEqual(positions, [1, 2, 3, 4, 5, 6])
        motors = commands(vehicle, mavlink.MAV_CMD_DO_MOTOR_TEST)
        self.assertEqual([int(p[0]) for p in motors], [1, 2, 3, 4])
        self.assertTrue(all(p[1] == mavlink.MOTOR_TEST_THROTTLE_PERCENT and p[2] <= 7 and p[3] == 2 for p in motors))
        self.assertFalse(vehicle.armed)
        self.assertEqual(len(results["compass"].details["reports"]), 2)

    def test_armed_vehicle_refused(self):
        results, vehicle, _ = run(FakeVehicle(armed=True), props_removed=True)
        self.assertEqual(results["connect"].status, FAIL)
        self.assertIn("ARMED", results["connect"].message)
        self.assertTrue(all(r.status == SKIPPED for k, r in results.items() if k != "connect"))
        self.assertEqual([c for c, _ in vehicle.commands if c != mavlink.MAV_CMD_REQUEST_MESSAGE], [])

    def test_motor_test_skipped_without_confirmation(self):
        results, vehicle, _ = run()
        self.assertEqual(results["motors"].status, SKIPPED)
        self.assertIn("PROPELLERS", results["motors"].message)
        self.assertEqual(commands(vehicle, mavlink.MAV_CMD_DO_MOTOR_TEST), [])

    def test_motor_test_refused_by_vehicle(self):
        vehicle = FakeVehicle(motor_test_result=mavlink.MAV_RESULT_FAILED)
        results, _, _ = run(vehicle, props_removed=True, skip={"compass", "accel"})
        self.assertEqual(results["motors"].status, FAIL)
        self.assertIn("not landed", results["motors"].message)

    def test_profile_mismatch_reported(self):
        profile = build_profile({**DEFAULT_PARAMS, "FRAME_TYPE": 3, "BATT_CAPACITY": 3000}, "other")
        profile["params"]["BATT_NOT_THERE"] = 1
        results, _, _ = run(profile=profile, skip={"compass", "accel"})
        names = {m["name"] for m in results["params"].details["mismatches"]}
        self.assertEqual(results["params"].status, FAIL)
        self.assertEqual(names, {"FRAME_TYPE", "BATT_CAPACITY"})
        self.assertEqual(results["params"].details["missing"], ["BATT_NOT_THERE"])

    def test_large_profile_uses_param_list_and_save(self):
        vehicle = FakeVehicle(params={f"P{i:03d}": i for i in range(60)})
        link = Link(vehicle)
        self.assertIsNotNone(link.wait_heartbeat(5))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "all.json"
            saved = save_profile(link, path, "all", patterns=("P*",))
            self.assertEqual(len(saved["params"]), 60)
            profile = load_profile(path)
        results, _, _ = run(vehicle, profile=profile, skip={"compass", "accel", "gyro_baro", "level", "health"})
        self.assertEqual(results["params"].status, PASS, results["params"].message)

    def test_mag_cal_failure_reported(self):
        results, _, _ = run(FakeVehicle(mag_fail=True), skip={"accel"})
        self.assertEqual(results["compass"].status, FAIL)
        self.assertIn("compass 2: FAILED", results["compass"].message)
        self.assertEqual(results["compass"].details["reports"][0]["status"], "SUCCESS")

    def test_mag_cal_cancel(self):
        results, vehicle, _ = run(prompter=AutoPrompter({"compass": "Cancel"}), skip={"accel"})
        self.assertEqual(results["compass"].status, NEEDS_USER)
        self.assertTrue(commands(vehicle, mavlink.MAV_CMD_DO_CANCEL_MAG_CAL))

    def test_accel_failure_and_cancel(self):
        results, _, _ = run(FakeVehicle(accel_fail_at=3), skip={"compass"})
        self.assertEqual(results["accel"].status, FAIL)
        self.assertEqual(len(results["accel"].details["positions_done"]), 3)
        results, vehicle, _ = run(prompter=AutoPrompter({"accel_4": "Cancel"}), skip={"compass"})
        self.assertEqual(results["accel"].status, NEEDS_USER)
        self.assertEqual([int(p[0]) for p in commands(vehicle, mavlink.MAV_CMD_ACCELCAL_VEHICLE_POS)], [1, 2, 3])

    def test_guided_steps_need_a_human(self):
        results, _, _ = run(prompter=False)
        self.assertEqual(results["compass"].status, NEEDS_USER)
        self.assertEqual(results["accel"].status, NEEDS_USER)

    def test_timeouts(self):
        results, _, _ = run(FakeVehicle(silent=True), heartbeat_timeout=3)
        self.assertEqual(results["connect"].status, FAIL)
        self.assertIn("No HEARTBEAT", results["connect"].message)
        vehicle = FakeVehicle(unresponsive={mavlink.MAV_CMD_PREFLIGHT_CALIBRATION, mavlink.MAV_CMD_DO_START_MAG_CAL})
        results, _, _ = run(vehicle, skip={"accel"})
        self.assertEqual(results["gyro_baro"].status, FAIL)
        self.assertIn("timeout", results["gyro_baro"].message)
        self.assertEqual(results["compass"].status, FAIL)
        self.assertLess(vehicle.clock(), 200)

    def test_prearm_messages_and_weak_gps(self):
        vehicle = FakeVehicle(prearm_texts=["PreArm: Compass not calibrated"], gps_sats=4)
        results, _, _ = run(vehicle, skip={"compass", "accel"})
        health = results["health"]
        self.assertEqual(health.status, FAIL)
        self.assertIn("PreArm: Compass not calibrated", health.details["prearm_messages"])
        self.assertIn("4 satellites", health.message)
        results, _, _ = run(FakeVehicle(gps_sats=7, battery_pct=20), skip={"compass", "accel"})
        self.assertEqual(results["health"].status, WARN)

    def test_report_and_cli(self):
        results, _, runner = run(skip={"compass"})
        built = report.build(list(results.values()), runner.vehicle, runner.options, 0)
        self.assertEqual(built["overall"], overall(list(results.values())))
        with tempfile.TemporaryDirectory() as temp:
            json_path, md_path = report.write(built, temp)
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))["steps"][0]["id"], "connect")
            self.assertIn("| 1 | Connect and heartbeat | **PASS**", md_path.read_text(encoding="utf-8"))
            from telemetry_platform.cli import main
            out = StringIO()
            with redirect_stdout(out):
                code = main(["preflight", "--simulate", "--no-gui", "--skip", "gyro_baro", "level", "compass",
                             "accel", "health", "--report-dir", temp])
            self.assertEqual(code, 0, out.getvalue())
            self.assertIn("Overall: PASS", out.getvalue())
            self.assertEqual(len(list(Path(temp).glob("preflight-*.json"))), 2)


class PortScanTests(unittest.TestCase):
    def fake_ports(self):
        from types import SimpleNamespace as P
        return [P(device="COM3", description="Standard Serial over Bluetooth link (COM3)", manufacturer="Microsoft",
                  product=None, hwid="BTHENUM\\{...}", vid=None),
                P(device="COM7", description="USB Serial Port", manufacturer="FTDI", product=None,
                  hwid="USB VID:PID=0403:6015", vid=0x0403),
                P(device="COM9", description="ArduPilot (COM9)", manufacturer="ArduPilot Project", product=None,
                  hwid="USB VID:PID=1209:5741", vid=0x1209)]

    def test_ports_ranked_and_bluetooth_skipped(self):
        from unittest import mock

        from telemetry_platform.preflight import ports
        with mock.patch("serial.tools.list_ports.comports", return_value=self.fake_ports()):
            self.assertEqual([p.device for p in ports.list_ports()], ["COM9", "COM7", "COM3"])
            devices = [d for d, _ in ports.candidates(115200)]
        self.assertEqual(devices[0], "COM9")
        self.assertNotIn("COM3", devices)
        self.assertEqual(devices[-1], ports.UDP_DEFAULT)

    def test_auto_detect_picks_first_port_with_heartbeat(self):
        from unittest import mock

        from telemetry_platform.preflight import ports
        from telemetry_platform.preflight.link import LinkError

        def opener(device, baud=115200):
            if device == "COM9":
                raise LinkError("access denied")
            return Link(FakeVehicle(silent=device != "COM7"))

        with mock.patch("serial.tools.list_ports.comports", return_value=self.fake_ports()), \
                mock.patch.object(Link, "open", side_effect=opener):
            link = ports.auto_detect(timeout=1)
            self.assertIn("COM7", link.description)
            with mock.patch.object(Link, "open", side_effect=LinkError("busy")):
                with self.assertRaises(LinkError):
                    ports.auto_detect(timeout=1, include_udp=False)


@unittest.skipUnless(os.environ.get("QT_QPA_PLATFORM") == "offscreen", "needs QT_QPA_PLATFORM=offscreen")
class PreflightGuiTests(unittest.TestCase):
    def test_window_builds(self):
        from PySide6.QtWidgets import QApplication

        from telemetry_platform.desktop import preflight_command
        from telemetry_platform.preflight.gui import PreflightWindow
        QApplication.instance() or QApplication([])
        window = PreflightWindow("sim:")
        self.assertEqual(window.table.rowCount(), 8)
        self.assertEqual(window.connection_text(), "sim:")
        window.refresh_ports("auto")
        self.assertEqual(window.connection_text(), "auto")
        window.connect_edit.setEditText("tcp:127.0.0.1:5760")
        self.assertEqual(window.connection_text(), "tcp:127.0.0.1:5760")
        self.assertFalse(window.props.isChecked())
        self.assertEqual(preflight_command()[-1], "preflight")
        window.close()


if __name__ == "__main__":
    unittest.main()
