"""Offline evidence replay rejects incomplete or modified trial records."""

import copy
import json
import math
from pathlib import Path
import shutil
import tempfile
import unittest

from evaluation.jev_transfer.summarize import (
    LEGACY_RECEIPT,
    complete_rows,
    decision_summary,
    native_summary,
    normalize_answer,
    summarize,
)

ROOT = Path(__file__).resolve().parents[1] / "evaluation/jev_transfer"


class JevTransferEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evidence = json.loads((ROOT / "observations.json").read_text())

    def test_retained_trials_replay(self):
        result = summarize()
        self.assertEqual(result, json.loads((ROOT / "summary.json").read_text()))
        self.assertEqual(result["interrupted_run"]["rows"], 79)
        self.assertFalse(result["interrupted_run"]["qualified"])
        for name in ["native_routing", "native_consolidation"]:
            self.assertFalse(result[name]["release_qualified"])
            for metrics in result[name]["arms"].values():
                self.assertEqual(metrics["n"], 24)

    def test_decision_screens_bind_derived_fields_to_call_answers(self):
        for name in ["decision_screen", "decision_confirmation"]:
            for arm in ["split", "merged", "cap_matrix"]:
                for field in ["skill", "effort"]:
                    with self.subTest(name=name, arm=arm, field=field):
                        run = copy.deepcopy(self.evidence["runs"][name])
                        row = next(r for r in run["rows"] if r["arm"] == arm)
                        if field == "skill":
                            row["skill"]["selected"] = "not-the-recorded-choice"
                        else:
                            row["effort"]["effort"] = (
                                "high" if row["effort"]["effort"] == "low" else "low"
                            )
                        with self.assertRaisesRegex(ValueError, "derived decision fields"):
                            decision_summary(run)

    def test_decision_screens_validate_physical_call_contracts(self):
        for arm in ["split", "merged", "cap_matrix"]:
            for changed in ["model", "questions", "answers", "choice", "missing", "extra", "timing", "error"]:
                with self.subTest(arm=arm, changed=changed):
                    run = copy.deepcopy(self.evidence["runs"]["decision_screen"])
                    row = next(r for r in run["rows"] if r["arm"] == arm)
                    call = row["calls"][0]
                    if changed == "model":
                        call["result"]["model"] = "another-model"
                    elif changed == "questions":
                        call["questions"].remove("skill")
                    elif changed == "answers":
                        del call["result"]["answers"]["needs_skill"]
                    elif changed == "choice":
                        call["result"]["answers"]["skill"]["confidence"] = True
                    elif changed == "missing":
                        row["calls"].pop()
                    elif changed == "extra":
                        row["calls"].append(copy.deepcopy(call))
                    elif changed == "timing":
                        call["wall_ms"] = row["wall_ms"] + (arm != "split")
                    else:
                        row["error_type"] = "invented-error"
                    with self.assertRaises(ValueError):
                        decision_summary(run)

    def test_missing_duplicate_and_nonfinite_rows_are_rejected(self):
        run = self.evidence["runs"]["native_routing"]
        for rows in [run["rows"][:-1], run["rows"] + run["rows"][:1]]:
            with self.assertRaises(ValueError):
                complete_rows(run["freeze"], rows, True)
        rows = copy.deepcopy(run["rows"])
        rows[0]["wall_ms"] = math.nan
        with self.assertRaises(ValueError):
            complete_rows(run["freeze"], rows, True)

    def test_modified_export_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in [
                "provenance.json",
                "observations.json",
                "source-snapshots.json",
            ]:
                shutil.copy2(ROOT / name, root / name)
            with (root / "observations.json").open("a") as handle:
                handle.write(" ")
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                summarize(root)

    def test_modified_archived_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in [
                "provenance.json",
                "observations.json",
                "source-snapshots.json",
            ]:
                shutil.copy2(ROOT / name, root / name)
            shutil.copytree(ROOT / "frozen", root / "frozen")
            archive = next((root / "frozen").rglob("*.py"))
            archive.write_bytes(archive.read_bytes() + b"# changed\n")
            with self.assertRaisesRegex(ValueError, "archived source digest mismatch"):
                summarize(root)

    def test_unbound_shared_receipts_are_rejected(self):
        run = copy.deepcopy(self.evidence["runs"]["native_consolidation"])
        for row in run["rows"]:
            if row["arm"] == "candidate":
                row["route"] = [{}]
        with self.assertRaisesRegex(ValueError, "unbound shared"):
            native_summary(run)

    def test_switched_wire_without_receipt_is_rejected(self):
        run = copy.deepcopy(self.evidence["runs"]["native_routing"])
        for row in run["rows"]:
            if row["arm"] == "candidate":
                row["route"] = []
        with self.assertRaisesRegex(ValueError, "switched wire is missing"):
            native_summary(run)

    def test_route_without_matching_wire_is_rejected(self):
        run = copy.deepcopy(self.evidence["runs"]["native_routing"])
        for row in run["rows"]:
            for wire in row["wire"]:
                wire["model"] = run["freeze"]["source_model"]
        with self.assertRaisesRegex(ValueError, "route is not bound"):
            native_summary(run)

    def test_unbound_kept_routes_do_not_count_as_consumed(self):
        run = copy.deepcopy(self.evidence["runs"]["native_routing"])
        baseline = native_summary(run)["arms"]["candidate"]
        self.assertEqual(baseline["pilot_consumed"], 24)
        unbound = 0
        for row in run["rows"]:
            if row["arm"] == "candidate" and row["route"] and not any(
                receipt["applied"] for receipt in row["route"]
            ):
                for receipt in row["route"]:
                    receipt["decision"]["request_id"] = "not-an-observed-call"
                unbound += 1
        self.assertEqual(unbound, 2)
        metrics = native_summary(run)["arms"]["candidate"]
        self.assertEqual(metrics["pilot_consumed"], 22)
        self.assertEqual(metrics["model_switched"], baseline["model_switched"])

    def test_routes_cannot_bind_to_skill_or_effort_calls(self):
        for questions in [("skill", "needs_skill"), ("reasoning_effort", "stakes")]:
            for applied in [False, True]:
                with self.subTest(questions=questions, applied=applied):
                    run = copy.deepcopy(self.evidence["runs"]["native_routing"])
                    row = next(
                        row
                        for row in run["rows"]
                        if row["arm"] == "candidate"
                        and any(r["applied"] is applied for r in row["route"])
                        and any(tuple(c["questions"]) == questions for c in row["jev"])
                    )
                    receipt = next(r for r in row["route"] if r["applied"] is applied)
                    other = next(c for c in row["jev"] if tuple(c["questions"]) == questions)
                    receipt["decision"]["request_id"] = other["request_id"]
                    if applied:
                        with self.assertRaisesRegex(ValueError, "route is not bound"):
                            native_summary(run)
                    else:
                        metrics = native_summary(run)["arms"]["candidate"]
                        self.assertEqual(metrics["pilot_consumed"], 23)

    def test_routing_decision_must_match_recorded_model_and_answers(self):
        for field in ["model", "answers"]:
            with self.subTest(field=field):
                run = copy.deepcopy(self.evidence["runs"]["native_routing"])
                row = next(
                    row
                    for row in run["rows"]
                    if row["arm"] == "candidate"
                    and any(r["applied"] for r in row["route"])
                )
                receipt = next(r for r in row["route"] if r["applied"])
                if field == "model":
                    receipt["decision"]["model"] = "different-decision-model"
                else:
                    receipt["decision"]["answers"]["routine"]["noul"] = 0
                with self.assertRaisesRegex(ValueError, "route is not bound"):
                    native_summary(run)

    def test_native_calls_must_use_frozen_jev_model(self):
        for name in ["native_routing", "native_consolidation"]:
            with self.subTest(name=name):
                run = copy.deepcopy(self.evidence["runs"][name])
                row = next(
                    r for r in run["rows"] if r["arm"] == "candidate" and r["route"]
                )
                for call in row["jev"]:
                    call["model"] = "different-but-matching-model"
                for receipt in row["route"]:
                    if "decision" in receipt:
                        receipt["decision"]["model"] = "different-but-matching-model"
                with self.assertRaisesRegex(ValueError, "differs from frozen model"):
                    native_summary(run)

    def test_applied_routing_requires_qualifying_answers_and_deadline(self):
        for changed in ["routine", "stakes", "deadline"]:
            with self.subTest(changed=changed):
                run = copy.deepcopy(self.evidence["runs"]["native_routing"])
                row = next(
                    r for r in run["rows"]
                    if r["arm"] == "candidate" and any(x["applied"] for x in r["route"])
                )
                receipt = next(x for x in row["route"] if x["applied"])
                call = next(
                    c for c in row["jev"]
                    if c["request_id"] == receipt["decision"]["request_id"]
                )
                if changed == "deadline":
                    receipt["wall_ms"] = run["freeze"]["routing_deadline_ms"] + 1
                else:
                    score = 0 if changed == "routine" else 1
                    call["answers"][changed]["noul"] = score
                    receipt["decision"]["answers"][changed]["noul"] = score
                with self.assertRaisesRegex(ValueError, "did not qualify"):
                    native_summary(run)

    def test_shared_effort_requires_combined_answers_and_deadline(self):
        for changed in ["questions", "effort", "stakes", "deadline"]:
            with self.subTest(changed=changed):
                run = copy.deepcopy(self.evidence["runs"]["native_consolidation"])
                row = next(
                    r for r in run["rows"]
                    if r["arm"] == "candidate"
                    and any(x["effort"] == "low" for x in r["route"])
                )
                receipt = next(x for x in row["route"] if x["effort"] == "low")
                call = next(
                    c for c in row["jev"]
                    if c["request_id"] == receipt["shared_request_id"]
                )
                if changed == "questions":
                    call["questions"].remove("reasoning_effort_high")
                elif changed == "effort":
                    choice = call["answers"]["reasoning_effort_" + receipt["cap"]]
                    choice["choice"] = receipt["cap"]
                elif changed == "stakes":
                    call["answers"]["stakes"]["noul"] = 1
                else:
                    receipt["shared_latency_ms"] = run["freeze"]["routing_deadline_ms"] + 1
                with self.assertRaisesRegex(ValueError, "shared decision is not bound"):
                    native_summary(run)

    def test_qualifying_routing_decision_must_be_applied(self):
        run = copy.deepcopy(self.evidence["runs"]["native_routing"])
        row = next(
            r for r in run["rows"]
            if r["arm"] == "candidate" and any(x["applied"] for x in r["route"])
        )
        receipt = next(x for x in row["route"] if x["applied"])
        receipt["applied"] = False
        receipt["to"] = run["freeze"]["source_model"]
        for wire in row["wire"]:
            wire["model"] = run["freeze"]["source_model"]
        with self.assertRaisesRegex(ValueError, "qualifying route was not applied"):
            native_summary(run)

    def test_decision_receipt_must_enclose_physical_call_latency(self):
        for name in ["native_routing", "native_consolidation"]:
            for value in ["exceeds_receipt", 500, math.nan, math.inf, -1, True, None, "20"]:
                with self.subTest(name=name, value=value):
                    run = copy.deepcopy(self.evidence["runs"][name])
                    row = next(
                        r for r in run["rows"]
                        if r["arm"] == "candidate" and r["route"]
                        and (name == "native_consolidation" or r["route"][0]["applied"])
                    )
                    receipt = row["route"][0]
                    request_id = (
                        receipt["decision"]["request_id"] if name == "native_routing"
                        else receipt["shared_request_id"]
                    )
                    call = next(c for c in row["jev"] if c["request_id"] == request_id)
                    outer = receipt.get("shared_latency_ms", receipt.get("wall_ms"))
                    call["wall_ms"] = outer + 0.001 if value == "exceeds_receipt" else value
                    with self.assertRaisesRegex(ValueError, "not bound"):
                        native_summary(run)

    def test_optional_raw_type_tags_preserve_native_metrics(self):
        for name in ["native_routing", "native_consolidation"]:
            with self.subTest(name=name):
                run = copy.deepcopy(self.evidence["runs"][name])
                expected = native_summary(run)
                for row in run["rows"]:
                    for call in row["jev"]:
                        for answer in (call.get("answers") or {}).values():
                            answer.pop("type", None)
                self.assertEqual(native_summary(run), expected)

    def shared_fixture(self):
        run = copy.deepcopy(self.evidence["runs"]["native_consolidation"])
        row = next(r for r in run["rows"] if r["arm"] == "candidate" and r["route"])
        receipt = row["route"][0]
        call = next(
            c for c in row["jev"] if c["request_id"] == receipt["shared_request_id"]
        )
        return run, call["answers"]

    def test_shared_answers_require_exact_question_keys(self):
        _, answers = self.shared_fixture()
        for name in [*answers, "extra"]:
            with self.subTest(name=name):
                run, changed = self.shared_fixture()
                if name == "extra":
                    changed[name] = {"noul": 0}
                else:
                    del changed[name]
                with self.assertRaisesRegex(ValueError, "shared decision is not bound"):
                    native_summary(run)

    def test_every_shared_answer_rejects_wrong_type_or_extra_fields(self):
        _, answers = self.shared_fixture()
        for name in answers:
            for field, value in [("type", "wrong"), ("type", None), ("extra", 0)]:
                with self.subTest(name=name, field=field, value=value):
                    run, changed = self.shared_fixture()
                    changed[name][field] = value
                    with self.assertRaisesRegex(ValueError, "shared decision is not bound"):
                        native_summary(run)

    def test_all_shared_choices_must_win_with_offered_criteria(self):
        for name in ["skill", "reasoning_effort_medium", "reasoning_effort_high"]:
            for change in ["nonwinning", "missing", "extra"]:
                with self.subTest(name=name, change=change):
                    run, answers = self.shared_fixture()
                    answer = answers[name]
                    other = next(k for k in answer["probabilities"] if k != answer["choice"])
                    if change == "nonwinning":
                        answer["probabilities"] = {
                            key: float(key == other) for key in answer["probabilities"]
                        }
                    elif change == "missing":
                        del answer["probabilities"][other]
                    else:
                        answer["probabilities"]["not-offered"] = 0
                    with self.assertRaisesRegex(ValueError, "shared decision is not bound"):
                        native_summary(run)

    def test_noul_shape_and_numeric_contract(self):
        for value in [True, None, math.nan, math.inf, -0.01, 1.01]:
            with self.subTest(value=value):
                self.assertIsNone(normalize_answer({"noul": value}, "noul"))
        for answer in [
            {"type": None, "noul": 0},
            {"type": "choice", "noul": 0},
            {"noul": 0, "extra": 0},
        ]:
            self.assertIsNone(normalize_answer(answer, "noul"))
        for value in [0, 0.5, 1]:
            self.assertEqual(normalize_answer({"noul": value}, "noul"), {"noul": value})

    def test_choice_numeric_contract_and_tolerances(self):
        criteria = {"low", "high"}
        for probabilities, accepted in [
            ({"low": 0.51, "high": 0.5}, True),
            ({"low": 0.5, "high": 0.4999995}, True),
            ({"low": 0.4999995, "high": 0.5}, True),
            ({"low": 0.49, "high": 0.5}, False),
            ({"low": 0.53, "high": 0.5}, False),
            ({"low": True, "high": 0}, False),
            ({"low": math.nan, "high": 0}, False),
        ]:
            with self.subTest(probabilities=probabilities):
                answer = {"choice": "low", "probabilities": probabilities, "confidence": 0.8}
                self.assertEqual(normalize_answer(answer, "choice", criteria) is not None, accepted)
        for confidence in [True, None, math.inf, -0.1, 1.1]:
            answer = {
                "choice": "low", "probabilities": {"low": 1, "high": 0},
                "confidence": confidence,
            }
            self.assertIsNone(normalize_answer(answer, "choice", criteria))

    def test_receipt_normalization_is_terminal_and_exact(self):
        receipt = "\n\nswitchyard: effort high→low · Jev 180 ms"
        self.assertEqual(LEGACY_RECEIPT.sub("", "Rome" + receipt), "Rome")
        for text in ["Rome switchyard: effort high", "Rome" + receipt + "\nOther text"]:
            self.assertEqual(LEGACY_RECEIPT.sub("", text), text)


if __name__ == "__main__":
    unittest.main()
