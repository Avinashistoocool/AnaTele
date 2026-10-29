import hashlib
import io
import json
import os
import struct
import tempfile
import unittest
import uuid
from pathlib import Path

os.environ["TELEMETRY_DISABLE_WORKER"] = "1"

from fastapi.testclient import TestClient
from pymavlink.dialects.v20 import ardupilotmega as mav

from make_samples import BASE, generate, tlog_bytes
from telemetry_platform import store
from telemetry_platform.analysis import run
from telemetry_platform.api import app
from telemetry_platform.parsers import ParseError, read_tlog, records


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data, self.old_db = store.DATA, store.DB
        store.DATA = Path(self.temp.name)
        store.DB = store.DATA / "platform.db"
        store.initialize()
        generate(store.DATA / "samples")
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.client_context.__exit__(None, None, None)
        store.DATA, store.DB = self.old_data, self.old_db
        self.temp.cleanup()

    def upload(self, name="demo.tlog", profile_id=None):
        path = store.DATA / "samples" / name
        data = {"profile_id": profile_id} if profile_id else {}
        response = self.client.post("/api/logs", files={"file": (name, path.read_bytes())}, data=data)
        self.assertEqual(response.status_code, 202, response.text)
        job_id = response.json()["id"]
        self.assertEqual(store.claim(), job_id)
        run(job_id)
        return job_id

    def test_tlog_end_to_end_and_roles(self):
        job_id = self.upload()
        job = store.get_job(job_id)
        self.assertEqual(job["status"], "complete")
        m = job["manifest"]
        roles = {source["key"]: source["role"] for source in m["sources"]}
        self.assertEqual(roles, {"1:1": "vehicle", "255:190": "ground_station", "51:68": "radio_status_source"})
        self.assertEqual(len(m["armed_episodes"]), 1)
        self.assertEqual(m["armed_episodes"][0]["source"], "1:1")
        self.assertIn("reported", m["armed_episodes"][0]["airborne"])
        titles = [finding["title"] for finding in m["findings"]]
        self.assertIn("Crash-check disarm reported", titles)
        self.assertIn("Compass-selection event preceded disarm", titles)
        self.assertIn("Radio failsafe blocked arming", titles)
        self.assertIn("ARMING_SKIPCHK configuration review", titles)
        self.assertTrue(all(f["calibrated_probability"] is None for f in m["findings"]))
        snapshot = m["parameters"][0]
        self.assertEqual(snapshot["expected_count"], 6)
        self.assertEqual(snapshot["values"]["FS_THR_VALUE"]["value"], 975)
        clips = [e for e in m["events"] if e["kind"] == "CLIPPING_INCREMENT"]
        self.assertEqual(len(clips), 9)
        self.assertTrue(all("+2 clips" in e["text"] for e in clips))
        tx = m["missions"]["transactions"]
        self.assertEqual([x["count"] for x in tx], [4, 3])
        self.assertTrue(all(x["status"] == "accepted_with_all_items_observed" for x in tx))
        self.assertEqual(tx[1]["items"]["1"]["command"], mav.MAV_CMD_NAV_TAKEOFF)
        self.assertNotIn("3", tx[1]["items"])

    def test_evidence_api_and_series(self):
        job_id = self.upload()
        base = f"/api/logs/{job_id}"
        detail = self.client.get(base).json()
        self.assertNotIn("events", detail["manifest"])
        record = self.client.get(base + "/records/1").json()
        self.assertEqual(record["source"], "255:190")
        self.assertEqual(record["offset"], 0)
        self.assertEqual(record["artifact_sha256"], hashlib.sha256(tlog_bytes()).hexdigest())
        self.assertTrue(record["raw_hex_preview"])
        series = self.client.get(base + "/series", params={"source": "1:1", "signal": "imu.0.vibration.z", "epoch": 0}).json()
        self.assertEqual(max(p["value"] for p in series["points"]), 66)
        points = self.client.get(base + "/positions", params={"source": "1:1", "epoch": 0}).json()["points"]
        self.assertEqual(len(points), 10)
        self.assertEqual(points[0]["lat"], 10)
        events = self.client.get(base + "/events", params={"source": "1:1", "search": "Radio failsafe"}).json()
        self.assertEqual(events["total"], 1)
        self.assertEqual(events["items"][0]["count"], 3)
        self.assertEqual(self.client.get(base + "/report").status_code, 200)
        self.assertEqual(self.client.get(base + "/original").content, tlog_bytes())
        self.assertEqual(self.client.get(base + "/records/999999").status_code, 404)

    def test_dataflash_binary_and_text(self):
        for name in ("demo.bin", "demo.log"):
            with self.subTest(name=name):
                job_id = self.upload(name)
                m = store.get_job(job_id)["manifest"]
                self.assertGreater(m["record_count"], 3)
                self.assertIn("dataflash", {source["key"] for source in m["sources"]})
                self.assertTrue(any("Elevated vibration" in f["title"] for f in m["findings"]))
                clips = [e for e in m["events"] if e["kind"] == "CLIPPING_INCREMENT"]
                self.assertEqual(len(clips), 1)
                self.assertIn("+5 clips", clips[0]["text"])
                if name.endswith(".bin"):
                    raw = self.client.get(f"/api/logs/{job_id}/records/1").json()
                    self.assertTrue(raw["raw_hex_preview"].startswith("a3 95"))

    def test_csv_and_empty_signal_dataset(self):
        job_id = self.upload("demo.csv")
        self.assertEqual(store.get_job(job_id)["manifest"]["record_count"], 2)
        self.assertEqual(len(store.get_job(job_id)["manifest"]["armed_episodes"]), 0)
        encoder = mav.MAVLink(None, srcSystem=1, srcComponent=1)
        only_heartbeat = struct.pack(">Q", BASE) + encoder.heartbeat_encode(2, 3, 0, 0, 3).pack(encoder)
        (store.DATA / "samples" / "heartbeat.tlog").write_bytes(only_heartbeat)
        job_id = self.upload("heartbeat.tlog")
        self.assertEqual(store.get_job(job_id)["manifest"]["signals"], [])

    def test_truncated_tail_marks_partial(self):
        (store.DATA / "samples" / "partial.tlog").write_bytes(tlog_bytes() + b"\x00\x01\x02")
        job_id = self.upload("partial.tlog")
        self.assertEqual(store.get_job(job_id)["status"], "partial")
        self.assertIn("truncated", store.get_job(job_id)["manifest"]["diagnostic_counts"])

    def test_chunked_utf8_text_is_reassembled(self):
        encoder = mav.MAVLink(None, srcSystem=1, srcComponent=1)
        raw = ("A" * 49 + "\u03bb" + "B" * 10).encode()
        frames = []
        for i, part in enumerate((raw[:50], raw[50:])):
            message = encoder.statustext_encode(6, part, id=7, chunk_seq=i)
            frames.append(struct.pack(">Q", BASE + i * 100000) + message.pack(encoder))
        (store.DATA / "samples" / "chunks.tlog").write_bytes(b"".join(frames))
        job_id = self.upload("chunks.tlog")
        events = store.get_job(job_id)["manifest"]["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["text"], raw.decode())
        self.assertEqual(events[0]["evidence"], [1, 2])

    def test_parameter_encoding_unknown_stays_unknown(self):
        encoder = mav.MAVLink(None, srcSystem=1, srcComponent=1)
        msg = encoder.param_value_encode(b"ARMING_SKIPCHK", 128, mav.MAV_PARAM_TYPE_INT32, 2, 0)
        (store.DATA / "samples" / "param.tlog").write_bytes(struct.pack(">Q", BASE) + msg.pack(encoder))
        job_id = self.upload("param.tlog")
        snapshot = store.get_job(job_id)["manifest"]["parameters"][0]
        self.assertIsNone(snapshot["values"]["ARMING_SKIPCHK"]["value"])
        self.assertEqual(snapshot["completeness"], "partial or unknown")

    def test_profile_versions_are_immutable(self):
        first = self.client.post("/api/profiles", json={"drone_key": "quad", "name": "Quad", "vibration_warning": 30}).json()
        second = self.client.post("/api/profiles", json={"drone_key": "quad", "name": "Quad", "vibration_warning": 100}).json()
        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertEqual(store.get_profile(first["id"])["body"]["vibration_warning"], 30)
        job_id = self.upload(profile_id=second["id"])
        self.assertFalse(any("Elevated vibration" in f["title"] for f in store.get_job(job_id)["manifest"]["findings"]))

    def test_http_validation_and_unready_queries(self):
        self.assertEqual(self.client.post("/api/logs", files={"file": ("sample.exe", b"no")}).status_code, 415)
        self.assertEqual(self.client.post("/api/logs", files={"file": ("empty.tlog", b"")}).status_code, 400)
        self.assertEqual(self.client.post("/api/profiles", json={"drone_key": "quad", "name": "Quad", "vibration_warning": -1}).status_code, 422)
        self.assertEqual(self.client.post("/api/profiles", json={"drone_key": "quad", "name": "Quad"},
                                         headers={"Origin": "https://evil.example"}).status_code, 403)
        response = self.client.post("/api/logs", files={"file": ("demo.tlog", tlog_bytes())})
        job_id = response.json()["id"]
        self.assertEqual(self.client.get(f"/api/logs/{job_id}/events").status_code, 409)
        self.assertEqual(self.client.get("/api/logs/no-such-log").status_code, 404)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_bad_crc_is_not_a_valid_observation(self):
        blob = bytearray(tlog_bytes())
        # Flip a payload byte in the first heartbeat; later records remain available.
        blob[20] ^= 0x01
        path = store.DATA / "samples" / "corrupt.tlog"
        path.write_bytes(blob)
        diagnostics = []
        decoded = list(read_tlog(path, lambda *args: diagnostics.append(args)))
        self.assertTrue(any(d[0] == "invalid_frame" for d in diagnostics))
        self.assertGreater(len(decoded), 10)
        self.assertFalse(any(r["offset"] == 0 for r in decoded))


if __name__ == "__main__":
    unittest.main()
