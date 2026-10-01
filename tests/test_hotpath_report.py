"""Reject changed live-provider claims independently of archive checksums."""
import copy
import importlib.util
import json
import tarfile
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
        verify.main()

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
