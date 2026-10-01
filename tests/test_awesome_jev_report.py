"""The native pilot report must derive integrity from the recorded fingerprints."""
import json
import tempfile
import unittest
from pathlib import Path

from evaluation.awesome_jev.summarize_native import summarize


class NativeReportIntegrityTests(unittest.TestCase):
    def test_forged_unchanged_flag_does_not_override_changed_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "freeze.json").write_text(json.dumps({
                "order": [[0, "example", "candidate"]],
                "runtime_file_hashes": {"run_agent.py": "a" * 64},
            }))
            (root / "raw.jsonl").write_text(json.dumps({
                "id": "example-r0", "arm": "candidate",
            }) + "\n")
            (root / "runtime-after.json").write_text(json.dumps({
                "unchanged": True,
                "hashes": {"run_agent.py": "b" * 64},
            }))
            output = root / "report.json"
            with self.assertRaisesRegex(AssertionError, "runtime changed"):
                summarize(root, {"example-r0:candidate": {}}, output)
            self.assertFalse(output.exists())
