"""Shareable .ant analysis files: round trip, tamper detection and untrusted-archive handling."""
import io
import json
import os
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

os.environ["TELEMETRY_DISABLE_WORKER"] = "1"

from fastapi.testclient import TestClient

from telemetry_platform import antfile, simulator, store
from telemetry_platform.analysis import run
from telemetry_platform.api import app, evidence, report
from telemetry_platform.desktop_import import import_file


class AntFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.old = store.DATA, store.DB
        store.DATA = Path(cls.temp.name)
        store.DB = store.DATA / "platform.db"
        store.initialize()
        log = store.DATA / "sim_motor_failure.tlog"
        log.write_bytes(simulator.simulate("motor_failure", duration=60))
        cls.job_id = import_file(log)
        store.claim()
        run(cls.job_id)
        cls.job = store.get_job(cls.job_id)
        cls.lean = store.DATA / "lean.ant"
        cls.full = store.DATA / "full.ant"
        antfile.export_ant(cls.job_id, cls.lean)
        antfile.export_ant(cls.job_id, cls.full, include_original=True)

    @classmethod
    def tearDownClass(cls):
        store.DATA, store.DB = cls.old
        cls.temp.cleanup()

    def rebuild(self, name, change):
        """Copy lean.ant applying change(member_name, bytes) -> bytes | None (drop)."""
        target = store.DATA / name
        with zipfile.ZipFile(self.lean) as source, zipfile.ZipFile(target, "w") as out:
            for info in source.infolist():
                data = change(info.filename, source.read(info))
                if data is not None:
                    out.writestr(info.filename, data)
        return target

    def test_lean_round_trip_preserves_analysis(self):
        with zipfile.ZipFile(self.lean) as archive:
            self.assertEqual(archive.infolist()[0].filename, "mimetype")
            self.assertEqual(archive.read("mimetype").decode(), antfile.MIMETYPE)
            self.assertFalse(any(n.startswith("original.") for n in archive.namelist()))
            self.assertIn("report.html", archive.namelist())
        imported = store.get_job(antfile.import_ant(self.lean))
        original, copy = self.job["manifest"], imported["manifest"]
        self.assertEqual(imported["status"], self.job["status"])
        self.assertEqual(imported["sha256"], self.job["sha256"])
        for key in ("findings", "incidents", "rule_results", "phases", "missions", "flight_metrics", "signals"):
            self.assertEqual(copy[key], original[key], key)
        self.assertFalse(copy["shared_from"]["original_included"])
        self.assertFalse(store.has_original(imported))
        record = evidence(imported["id"], original["findings"][0]["evidence"][0])
        self.assertIsNone(record["raw_hex_preview"])
        self.assertIn("not included", record["raw_note"])
        self.assertIn("shared analysis", report(imported["id"]))

    def test_full_round_trip_keeps_raw_bytes(self):
        imported = store.get_job(antfile.import_ant(self.full))
        self.assertTrue(store.has_original(imported))
        self.assertEqual(antfile.digest(imported["path"]), self.job["sha256"])
        record_id = self.job["manifest"]["findings"][0]["evidence"][0]
        self.assertEqual(evidence(imported["id"], record_id)["raw_hex_preview"],
                         evidence(self.job_id, record_id)["raw_hex_preview"])

    def test_tampered_members_are_rejected(self):
        def edit_manifest(name, data):
            if name != "manifest.json":
                return data
            m = json.loads(data)
            m["findings"] = []
            return json.dumps(m).encode()
        with self.assertRaisesRegex(antfile.AntError, "SHA-256"):
            antfile.import_ant(self.rebuild("t1.ant", edit_manifest))
        flip = lambda name, data: data[:-20] + bytes(20) if name == "signals.parquet" else data
        with self.assertRaisesRegex(antfile.AntError, "SHA-256"):
            antfile.import_ant(self.rebuild("t2.ant", flip))
        self.assertFalse(list((store.DATA / "datasets").glob(".import-*")))

    def test_untrusted_archives_are_rejected(self):
        evil = self.rebuild("t3.ant", lambda n, d: d)
        with zipfile.ZipFile(evil, "a") as archive:
            archive.writestr("../../escape.txt", "x")
        with self.assertRaisesRegex(antfile.AntError, "Unexpected entries"):
            antfile.import_ant(evil)
        with self.assertRaisesRegex(antfile.AntError, "missing"):
            antfile.import_ant(self.rebuild("t4.ant", lambda n, d: None if n == "signals.parquet" else d))
        bad_version = lambda n, d: json.dumps({**json.loads(d), "format_version": 99}).encode() if n == "meta.json" else d
        with self.assertRaisesRegex(antfile.AntError, "format version"):
            antfile.import_ant(self.rebuild("t5.ant", bad_version))
        plain = store.DATA / "plain.ant"
        plain.write_text("not a zip")
        with self.assertRaisesRegex(antfile.AntError, "ZIP"):
            antfile.import_ant(plain)
        bomb = store.DATA / "bomb.ant"
        with zipfile.ZipFile(self.lean) as source, zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as out:
            for info in source.infolist():
                out.writestr(info.filename, source.read(info.filename) if info.filename != "meta.json"
                             else b" " * (antfile.LIMITS["meta.json"] + 10))
        with self.assertRaisesRegex(antfile.AntError, "size limit"):
            antfile.import_ant(bomb)

    def test_api_export_and_import(self):
        client = TestClient(app)
        response = client.get(f"/api/logs/{self.job_id}/export.ant")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-disposition"].endswith('.ant"'))
        uploaded = client.post("/api/analyses", files={"file": ("shared.ant", response.content)})
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        detail = client.get(f"/api/logs/{uploaded.json()['id']}").json()
        self.assertFalse(detail["has_original"])
        self.assertEqual(client.get(f"/api/logs/{uploaded.json()['id']}/original").status_code, 404)
        self.assertEqual(client.post("/api/analyses", files={"file": ("x.ant", b"junk")}).status_code, 400)
        self.assertFalse(list((store.DATA / "exports").glob("*.ant")))

    def test_cli_reads_ant(self):
        from telemetry_platform.cli import main
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(main(["analyse", str(self.lean)]), 0)
        self.assertIn("Shared analysis", buffer.getvalue())
        self.assertIn("Findings:", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
