"""Simulator scenarios end to end through the analysis engine, plus replay, CLI and desktop smoke tests."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

os.environ["TELEMETRY_DISABLE_WORKER"] = "1"
os.environ.setdefault("MPLBACKEND", "Agg")

from telemetry_platform import replay, simulator, store
from telemetry_platform.analysis import run
from telemetry_platform.desktop_import import import_file


class ScenarioTests(unittest.TestCase):
    manifests = {}

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.old = store.DATA, store.DB
        store.DATA = Path(cls.temp.name)
        store.DB = store.DATA / "platform.db"
        store.initialize()
        for name in simulator.SCENARIOS:
            path = store.DATA / f"sim_{name}.tlog"
            path.write_bytes(simulator.simulate(name))
            job_id = import_file(path)
            store.claim()
            run(job_id)
            cls.manifests[name] = store.get_job(job_id)

    @classmethod
    def tearDownClass(cls):
        store.DATA, store.DB = cls.old
        cls.temp.cleanup()

    def manifest(self, name):
        job = self.manifests[name]
        self.assertEqual(job["status"], "complete", job["error"])
        return job["manifest"]

    def triggered(self, name):
        return {r["rule_id"] for r in self.manifest(name)["rule_results"] if r["state"] == "triggered"}

    def test_simulation_is_deterministic_and_labelled(self):
        self.assertEqual(simulator.simulate("nominal", seed=3, duration=40), simulator.simulate("nominal", seed=3, duration=40))
        texts = [e["text"] for e in self.manifest("nominal")["events"]]
        self.assertTrue(any("SIMULATED FLIGHT" in text for text in texts))

    def test_nominal_flight_completes_mission(self):
        m = self.manifest("nominal")
        self.assertEqual(m["phases"][0]["status"], "airborne_inferred")
        self.assertIsNone(m["phases"][0]["critical_event_us"])
        statuses = [i["status"] for i in m["missions"]["execution"][0]["items"] if i["command"] == "NAV_WAYPOINT"]
        self.assertTrue(statuses and all(s == "reported_complete" for s in statuses))
        self.assertFalse(self.triggered("nominal") & {"crash-check-disarm", "vibration-persistent-pre-event",
                                                      "ekf-compass-innovation-pre-event", "power-monitoring-gap"})

    def test_motor_failure_ranks_thrust_hypothesis_first(self):
        m = self.manifest("motor_failure")
        self.assertIn("crash-check-disarm", self.triggered("motor_failure"))
        self.assertIn("motor-output-imbalance-pre-event", self.triggered("motor_failure"))
        self.assertEqual(m["incidents"][0]["hypotheses"][0]["id"], "thrust-or-configuration")
        statuses = {i["seq"]: i["status"] for i in m["missions"]["execution"][0]["items"]}
        # The aircraft came down near WP5: proximity may be consistent, but completion was never reported.
        self.assertNotEqual(statuses[5], "reported_complete")
        self.assertEqual(statuses[6], "completion_not_observed")

    def test_compass_interference_detected(self):
        triggered = self.triggered("compass_interference")
        self.assertIn("ekf-compass-innovation-pre-event", triggered)
        self.assertTrue(any(e["kind"] == "COMPASS_SELECTION" for e in self.manifest("compass_interference")["events"]))

    def test_vibration_detected_with_clipping(self):
        self.assertTrue({"vibration-persistent-pre-event", "clipping-pre-event"} <= self.triggered("vibration"))

    def test_radio_failsafe_prearm_and_rtl(self):
        m = self.manifest("radio_failsafe")
        self.assertIn("prearm-radio-failsafe", self.triggered("radio_failsafe"))
        self.assertIn("RTL", [e["text"] for e in m["events"] if e["kind"] == "MODE_CHANGE"])

    def test_replay_track_and_render(self):
        job = self.manifests["motor_failure"]
        track = replay.build_track(job)
        self.assertGreater(len(track), 100)
        self.assertLess(abs(track.east[0]) + abs(track.north[0]), 1e-6)
        self.assertTrue(any(m["label"] == "crash report" for m in track.markers))
        self.assertGreaterEqual(len(track.waypoints), 4)
        with tempfile.TemporaryDirectory() as temp:
            png = replay.render(track, Path(temp) / "path.png")
            gif = replay.render(track, Path(temp) / "path.gif", animate=True, fps=4, seconds=1)
            self.assertGreater(png.stat().st_size, 10_000)
            self.assertGreater(gif.stat().st_size, 10_000)


class CliTests(unittest.TestCase):
    def test_simulate_and_analyse_commands(self):
        from telemetry_platform.cli import main
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "flight.tlog"
            with redirect_stdout(io.StringIO()):
                main(["simulate", "--scenario", "vibration", "--out", str(target), "--duration", "90"])
            self.assertTrue(target.stat().st_size > 50_000)
            old = store.DATA, store.DB
            output = io.StringIO()
            try:
                with redirect_stdout(output):
                    self.assertEqual(main(["analyse", str(target)]), 0)
            finally:
                store.DATA, store.DB = old
            self.assertIn("Sustained vibration before event", output.getvalue())


@unittest.skipUnless(__import__("importlib").util.find_spec("PySide6"), "PySide6 not installed")
class DesktopSmokeTest(unittest.TestCase):
    def test_window_loads_simulated_flight(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from telemetry_platform import desktop
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as temp:
            old = store.DATA, store.DB
            store.DATA, store.DB = Path(temp), Path(temp) / "platform.db"
            try:
                store.initialize()
                path = Path(temp) / "sim.tlog"
                path.write_bytes(simulator.simulate("motor_failure"))
                job_id = import_file(path)
                store.claim()
                run(job_id)
                window = desktop.MainWindow()
                window.timer.stop()
                window.refresh_logs()
                window.select_job(job_id)
                app.processEvents()
                self.assertGreater(window.findings.rowCount(), 0)
                self.assertGreater(window.rules.rowCount(), 10)
                self.assertGreater(len(window.checks), 10)
                self.assertGreater(len(window.flight_path.track), 100)
                self.assertIn("Mechanical", window.stats["Top hypothesis"].text())
                window.close()
            finally:
                store.DATA, store.DB = old


if __name__ == "__main__":
    unittest.main()
