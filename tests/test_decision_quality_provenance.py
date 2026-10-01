"""Published evaluation replay must refuse tampered or incomplete evidence."""

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from evaluation.decision_quality.validate_observations import ARCHIVE_HEADER, validate
from evaluation.decision_quality.summarize import summarize

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "evaluation/decision_quality"


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.root = self.repo / "evaluation/decision_quality"
        self.root.mkdir(parents=True)
        for name in [
            "observations.json",
            "provenance.json",
            "cases.json",
            "workflow-cases.json",
        ]:
            shutil.copy2(SOURCE / name, self.root / name)
        shutil.copytree(SOURCE / "frozen", self.root / "frozen")
        (self.repo / "hermes_switchyard").mkdir()
        shutil.copy2(
            ROOT / "hermes_switchyard/record_triage.py",
            self.repo / "hermes_switchyard/record_triage.py",
        )
        original = json.loads((self.root / "provenance.json").read_text())
        trees = {
            s["revision"]: s["tree"]
            for name, s in original["arm_sources"].items()
            if name != "candidate"
        }
        baseline = (self.root / "frozen/record_triage.py").read_bytes()[
            len(ARCHIVE_HEADER.encode()) :
        ]

        # CI may have a shallow checkout. The command contract is mocked here;
        # the standalone summary replay separately checks the real Git objects.
        def git(command, **kwargs):
            if command[:2] == ["git", "rev-parse"]:
                return trees[command[2].removesuffix("^{tree}")] + "\n"
            if command[:2] == ["git", "show"]:
                return baseline
            raise AssertionError(command)

        self.patch = mock.patch(
            "evaluation.decision_quality.validate_observations.subprocess.check_output",
            side_effect=git,
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def alter_observations(self, mutate, *, rebind=False):
        path = self.root / "observations.json"
        data = json.loads(path.read_text())
        mutate(data)
        path.write_text(json.dumps(data))
        if rebind:
            manifest = self.root / "provenance.json"
            obj = json.loads(manifest.read_text())
            obj["observations_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest.write_text(json.dumps(obj))

    def test_reviewed_observations_validate(self):
        self.assertEqual(len(validate(self.root)["native"]), 96)

    def test_edited_observation_is_refused(self):
        self.alter_observations(lambda b: b["native"][0].update(final="WRONG"))
        with self.assertRaisesRegex(ValueError, "observations hash mismatch"):
            validate(self.root)

    def test_missing_and_duplicate_rows_are_refused_even_with_rebound_digest(self):
        for family in ["screen", "workflow", "native"]:
            with self.subTest(family=family):
                shutil.copy2(
                    SOURCE / "observations.json", self.root / "observations.json"
                )
                self.alter_observations(
                    lambda b: b[family].__setitem__(1, b[family][0]), rebind=True
                )
                with self.assertRaisesRegex(ValueError, "missing, duplicate, or extra"):
                    validate(self.root)

    def test_native_label_drift_is_refused(self):
        self.alter_observations(
            lambda b: b["native"][0].update(expected="WRONG"), rebind=True
        )
        with self.assertRaisesRegex(ValueError, "native label drift"):
            validate(self.root)

    def test_frozen_hash_drift_is_refused(self):
        self.alter_observations(
            lambda b: b["freezes"]["screen-run"]["files"].update(
                {"screen.py": "0" * 64}
            ),
            rebind=True,
        )
        with self.assertRaisesRegex(ValueError, "pre-call source mismatch"):
            validate(self.root)

    def test_frozen_source_edit_is_refused(self):
        path = self.root / "frozen/screen.py"
        path.write_text(path.read_text() + "\n# altered\n")
        with self.assertRaisesRegex(ValueError, "frozen source mismatch"):
            validate(self.root)

    def test_fixture_drift_is_refused(self):
        path = self.root / "cases.json"
        data = json.loads(path.read_text())
        data["effort"][0]["expected"] = "WRONG"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "fixture drift"):
            validate(self.root)

    def test_source_revision_drift_is_refused(self):
        self.alter_observations(
            lambda b: b["freezes"]["native-run"]["revisions"].update(main="0" * 40),
            rebind=True,
        )
        with self.assertRaisesRegex(ValueError, "wrong main revision"):
            validate(self.root)

    def test_source_tree_drift_is_refused(self):
        path = self.root / "provenance.json"
        data = json.loads(path.read_text())
        data["arm_sources"]["main"]["tree"] = "0" * 40
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "source tree mismatch"):
            validate(self.root)

    def test_production_rubric_drift_is_refused(self):
        path = self.repo / "hermes_switchyard/record_triage.py"
        path.write_text(
            path.read_text().replace(
                "Appearance or wording defect only;", "Unrelated criterion;"
            )
        )
        with self.assertRaisesRegex(ValueError, "production rubric differs"):
            validate(self.root)

    def test_missing_revision_arm_is_refused(self):
        self.alter_observations(
            lambda b: b["freezes"]["native-run"]["revisions"].pop("release"),
            rebind=True,
        )
        with self.assertRaisesRegex(ValueError, "incomplete revision set"):
            validate(self.root)

    def test_missing_frozen_file_binding_is_refused(self):
        self.alter_observations(
            lambda b: b["freezes"]["screen-run"]["files"].clear(), rebind=True
        )
        with self.assertRaisesRegex(ValueError, "incomplete frozen file set"):
            validate(self.root)

    def test_missing_manifest_arm_is_refused(self):
        path = self.root / "provenance.json"
        data = json.loads(path.read_text())
        data["arm_sources"].pop("release")
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "incomplete source arm set"):
            validate(self.root)

    def test_native_correctness_is_recomputed_from_final_text(self):
        book = validate(self.root)
        original = summarize(
            **{key: book[key] for key in ["screen", "workflow", "native"]}
        )
        for row in book["native"]:
            row["correct"] = not row["correct"]
        altered = summarize(
            **{key: book[key] for key in ["screen", "workflow", "native"]}
        )
        self.assertEqual(original["native"], altered["native"])
        row = next(row for row in book["native"] if row["arm"] == "main")
        row["final"] = "WRONG"
        changed = summarize(
            **{key: book[key] for key in ["screen", "workflow", "native"]}
        )
        self.assertEqual(changed["native"]["main"]["strict_format_correct"], 23)
