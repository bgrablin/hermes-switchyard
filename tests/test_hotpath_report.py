"""Reject changed live-provider claims independently of archive checksums."""
import copy
import hashlib
import importlib.util
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "evaluation" / "hotpath"
spec = importlib.util.spec_from_file_location("hotpath_verify", ROOT / "verify.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class HotpathReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tarfile.open(ROOT / "frozen" / "measurements.tar.gz") as archive:
            cls.freeze = json.load(archive.extractfile("live-v2/freeze.json"))
            cls.rows = [json.loads(line) for line in archive.extractfile("live-v2/observations.jsonl")]

    def test_retained_accounting_and_summaries(self):
        # Historical measurements bind their exact measured source, not every
        # future feature branch. The default CLI still rejects current drift.
        with tempfile.TemporaryDirectory() as tmp:
            with tarfile.open(ROOT / "frozen" / "measured-source-v2.tar.gz") as archive:
                archive.extractall(tmp, filter="data")
            verify.main(source_root=Path(tmp))

    def test_source_and_driver_drift_are_rejected(self):
        files = {"hermes_switchyard/client.py": b"client source",
                 "hermes_switchyard/routing.py": b"routing source",
                 "evaluation/hotpath/planning.py": b"benchmark source"}
        freeze = {"sources": {"candidate": {name: hashlib.sha256(data).hexdigest()
                                            for name, data in files.items()
                                            if name.startswith("hermes_switchyard/")}},
                  "driver_sha256": hashlib.sha256(files["evaluation/hotpath/planning.py"]).hexdigest()}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, data in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            verify.verify_sources(freeze, root)
            for name, data in files.items():
                with self.subTest(changed=name):
                    (root / name).write_bytes(data + b" changed")
                    with self.assertRaises(AssertionError):
                        verify.verify_sources(freeze, root)
                    (root / name).write_bytes(data)
            (root / "hermes_switchyard/extra.py").write_bytes(b"extra source")
            with self.assertRaises(AssertionError):
                verify.verify_sources(freeze, root)
            (root / "hermes_switchyard/extra.py").unlink()
            (root / "hermes_switchyard/client.py").unlink()
            with self.assertRaises(AssertionError):
                verify.verify_sources(freeze, root)

    def test_altered_provider_fields_are_rejected(self):
        for field, value in (("resolved_model", "unrecorded-model"), ("request_count", 99),
                             ("usage", {}), ("usage", {"cost": -1}),
                             ("usage", {"cost": 1}), ("usage", {"cost": float("nan")}),
                             ("plan_or_payload_sha256", "0" * 64), ("answers", {})):
            with self.subTest(field=field, value=value):
                rows = copy.deepcopy(self.rows)
                rows[0][field] = value
                with self.assertRaises(AssertionError):
                    verify.verify_live("live-v2", self.freeze, rows)

    def test_missing_model_and_changed_frozen_model_are_rejected(self):
        rows = copy.deepcopy(self.rows)
        rows[0].pop("resolved_model")
        with self.assertRaises(AssertionError):
            verify.verify_live("live-v2", self.freeze, rows)
        freeze = copy.deepcopy(self.freeze)
        freeze["model"] = "unrecorded-model"
        with self.assertRaises(AssertionError):
            verify.verify_live("live-v2", freeze, self.rows)


if __name__ == "__main__":
    unittest.main()
