"""Regression test against the user's real Mission Planner capture (skipped when unavailable).

The log contains private GPS positions, so it is not copied into the repository.
Set ANATELE_REAL_TLOG to its path, or keep it at the default Downloads location.
"""
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ["TELEMETRY_DISABLE_WORKER"] = "1"

from telemetry_platform import store
from telemetry_platform.analysis import run

ROOT = Path(__file__).parent
SPEC = json.loads((ROOT / "fixtures" / "real_tlog_expectations.json").read_text(encoding="utf-8"))
PATH = Path(os.environ.get("ANATELE_REAL_TLOG", os.path.expandvars(SPEC["file"]["default_path"])))


@unittest.skipUnless(PATH.is_file(), f"Real tlog not available at {PATH}")
class RealTlogRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.old = store.DATA, store.DB
        store.DATA = Path(cls.temp.name)
        store.DB = store.DATA / "platform.db"
        store.initialize()
        target = store.DATA / "uploads" / "real.tlog"
        shutil.copyfile(PATH, target)
        store.enqueue("real", PATH.name, "tlog", target.stat().st_size,
                      hashlib.sha256(target.read_bytes()).hexdigest(), target, None)
        store.claim()
        run("real")
        cls.job = store.get_job("real")
        cls.m = cls.job["manifest"]
        cls.expect = SPEC["expect"]

    @classmethod
    def tearDownClass(cls):
        store.DATA, store.DB = cls.old
        cls.temp.cleanup()

    def offset(self, t_us):
        return (t_us - self.m["first_time_us"]) / 1e6

    def test_ingest_and_sources(self):
        self.assertEqual(self.job["status"], self.expect["status"])
        self.assertEqual(self.m["record_count"], self.expect["record_count"])
        sources = {s["key"]: s for s in self.m["sources"]}
        self.assertEqual({k: v["role"] for k, v in sources.items()}, self.expect["sources"])
        for key, pack in self.expect["semantic_pack"].items():
            self.assertEqual(sources[key]["semantic_pack"], pack)
            self.assertTrue(sources[key]["firmware_git"].startswith(self.expect["firmware_git"]))

    def test_phases(self):
        self.assertEqual(len(self.m["phases"]), len(self.expect["episodes"]))
        for phase, want in zip(self.m["phases"], self.expect["episodes"]):
            self.assertEqual(phase["status"], want["phase_status"])
            if "takeoff_offset_s" in want:
                low, high = want["takeoff_offset_s"]
                self.assertTrue(low <= self.offset(phase["takeoff_us"]) <= high, self.offset(phase["takeoff_us"]))
                low, high = want["crash_offset_s"]
                self.assertTrue(low <= self.offset(phase["critical_event_us"]) <= high)
                self.assertLess(phase["pre_event_end_us"], phase["critical_event_us"])

    def test_events(self):
        for want in self.expect["events"]:
            hits = [e for e in self.m["events"] if e["kind"] == want["kind"]
                    and want.get("text_contains", "") in e["text"]]
            if "count" in want:
                self.assertEqual(len(hits), want["count"], want)
            if "min_count" in want:
                self.assertGreaterEqual(len(hits), want["min_count"], want)

    def test_rules(self):
        results = self.m["rule_results"]
        for want in self.expect["rules"]:
            rows = [r for r in results if r["rule_id"] == want["rule_id"]]
            if "episode" in want:
                rows = [r for r in rows if r["scope"].get("episode") == want["episode"]]
            if "episode_event" in want:
                rows = [rows[want["episode_event"]]]
            if "count" in want:
                self.assertEqual(len([r for r in rows if r["state"] == want["state"]]), want["count"], want)
                continue
            self.assertTrue(rows, want)
            for row in rows:
                self.assertEqual(row["state"], want["state"], (want, row["detail"]))
                self.assertIn(want.get("detail_contains", ""), row["detail"])
                if row["state"] == "triggered":
                    self.assertTrue(row["evidence"], row["rule_id"])

    def test_hypotheses(self):
        self.assertEqual(len(self.m["incidents"]), 2)
        for incident in self.m["incidents"]:
            titles = [h["title"] for h in incident["hypotheses"]]
            self.assertEqual(len(titles), 4)
            self.assertTrue(all(h["probability"] is None for h in incident["hypotheses"]))
            power = next(h for h in incident["hypotheses"] if h["id"] == "power-degradation")
            self.assertEqual(power["label"], "unassessable")

    def test_missions(self):
        want = self.expect["missions"]
        tx = self.m["missions"]["transactions"]
        self.assertEqual(len([t for t in tx if t["kind"] == "upload" and t["status"] == "accepted_with_all_items_observed"]),
                         want["accepted_full_uploads"])
        self.assertEqual(len([t for t in tx if t["status"] in ("superseded_before_items", "restarted_before_completion")]),
                         want["restarted_uploads"])
        execution = self.m["missions"]["execution"]
        self.assertEqual([e["plan_version"] for e in execution], want["episode_plan_versions"])
        for key, episode in (("episode0_item2", 0), ("episode1_item2", 1)):
            item = next(i for i in execution[episode]["items"] if i["seq"] == 2)
            self.assertEqual(item["status"], want[key]["status"])
            low, high = want[key]["nearest_m"]
            self.assertTrue(low <= item["nearest_m"] <= high, item["nearest_m"])
        for episode in want["takeoff_reported_complete_episodes"]:
            item = next(i for i in execution[episode]["items"] if i["seq"] == 1)
            self.assertEqual(item["status"], "reported_complete")

    def test_parameters(self):
        want = self.expect["parameters"]
        snapshot = max(self.m["parameters"], key=lambda p: len(p["values"]))
        self.assertEqual(len(snapshot["values"]), want["observed_names"])
        for name, value in want["typed"].items():
            self.assertEqual(snapshot["values"][name]["value"], value, name)
            self.assertEqual(snapshot["values"][name]["encoding"], want["encoding"])


if __name__ == "__main__":
    unittest.main()
