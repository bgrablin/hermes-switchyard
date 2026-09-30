"""Offline outcome labels against the frozen, independent synthetic oracle."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from hermes_switchyard import receipt_history, receipt_state

FIXTURES = Path(__file__).resolve().parents[1] / "evaluation" / "outcome-labels" / "fixtures.json"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def frozen_case(name):
    fixture = json.loads(FIXTURES.read_text(encoding="utf-8"))
    case = next(case for case in fixture["cases"] if case["name"] == name)
    records = []
    for item in case["records"]:
        receipt = dict(fixture["receipt_defaults"])
        receipt.update(fixture.get(item.get("receipt_override", "") + "_override", {}))
        receipt.update(item.get("receipt_patch", {}))
        assert receipt_state.canonicalize_receipt(receipt) == receipt
        record = receipt_history.build_history_record(
            receipt, session_id=item["session_id"], turn_id=item["turn_id"],
            platform="cli", now=NOW,
        )
        assert record is not None and receipt_history.validate_history_record(record)
        records.append(record)
    return records, case["evidence"], case["expected"]


class OutcomeLabelTests(unittest.TestCase):
    def test_explicit_reply_labels_load_trace_completion_and_same_arm_correction(self):
        from hermes_switchyard.outcome_labels import generate

        records, evidence, expected = frozen_case("explicit-correction-with-same-arm-adjacent-reply")
        result = generate(records, evidence)
        self.assertEqual(result["schema"], "switchyard-outcome-label/1")
        self.assertEqual(result["labels"], expected)
        self.assertEqual(result["coverage"]["skill_loaded_after_selection"], {"denominator": 2, "labeled": 1, "unknown": 1})
        self.assertEqual(result["splits"], {"train": 0, "holdout": 2, "unknown": 0})
        wire = json.dumps(result, sort_keys=True)
        self.assertNotIn("PRIVATE_SYNTHETIC_TEXT", wire)
        self.assertNotIn("s-positive", wire)
        self.assertNotIn("m-positive", wire)

    def test_every_frozen_case_agrees_with_independently_planted_truth(self):
        from hermes_switchyard.outcome_labels import generate

        fixture = json.loads(FIXTURES.read_text(encoding="utf-8"))
        disagreements = false_positive_corrections = known = 0
        for case in fixture["cases"]:
            with self.subTest(case=case["name"]):
                records, evidence, expected = frozen_case(case["name"])
                actual = generate(records, evidence)
                self.assertEqual(actual["labels"], expected)
                self.assertEqual(actual, generate(records, evidence))
                self.assertEqual(len(actual["labels"]), len(records))
                for got, truth in zip(actual["labels"], expected):
                    self.assertEqual(set(got), {
                        "split", "skill_loaded_after_selection", "tool_error_in_turn",
                        "next_turn_user_correction", "turn_completed",
                    })
                    for field in ("skill_loaded_after_selection", "tool_error_in_turn",
                                  "next_turn_user_correction", "turn_completed"):
                        self.assertIn(got[field], (True, False, "UNKNOWN"))
                        if type(truth[field]) is bool:
                            known += 1
                            disagreements += got[field] != truth[field]
                            if field == "next_turn_user_correction" and got[field] is True and truth[field] is False:
                                false_positive_corrections += 1
                self.assertEqual(
                    {arm: sum(label["split"] == arm for label in expected)
                     for arm in ("train", "holdout", "UNKNOWN")},
                    {"train": actual["splits"]["train"], "holdout": actual["splits"]["holdout"],
                     "UNKNOWN": actual["splits"]["unknown"]},
                )
                for field in ("skill_loaded_after_selection", "tool_error_in_turn",
                              "next_turn_user_correction", "turn_completed"):
                    self.assertEqual(actual["coverage"][field], {
                        "denominator": len(records),
                        "labeled": sum(type(label[field]) is bool for label in expected),
                        "unknown": sum(label[field] == "UNKNOWN" for label in expected),
                    })
        self.assertGreater(known, 0)
        self.assertEqual(false_positive_corrections, 0)
        self.assertLessEqual(disagreements / known, 0.05)

    def test_frozen_split_examples_and_empty_report(self):
        from hermes_switchyard.outcome_labels import generate, split_for_turn

        self.assertEqual(split_for_turn("a-correction"), "holdout")
        self.assertEqual(split_for_turn("b-no-correction"), "train")
        self.assertEqual(split_for_turn("a-correction"), split_for_turn("a-correction"))
        report = generate([], [])
        self.assertEqual(report["labels"], [])
        self.assertEqual(report["splits"], {"train": 0, "holdout": 0, "unknown": 0})
        self.assertEqual(report["coverage"]["next_turn_user_correction"], {
            "denominator": 0, "labeled": 0, "unknown": 0,
        })

    def test_combined_frozen_denominators_and_cross_session_split(self):
        from hermes_switchyard.outcome_labels import generate

        fixture = json.loads(FIXTURES.read_text(encoding="utf-8"))
        cases = [frozen_case(case["name"]) for case in fixture["cases"]]
        records = [record for group, _, _ in cases for record in group]
        evidence = [item for _, group, _ in cases for item in group]
        expected = [label for _, _, group in cases for label in group]
        result = generate(records, evidence)
        self.assertEqual(result["labels"], expected)
        self.assertEqual(result["splits"], {"train": 13, "holdout": 6, "unknown": 3})
        self.assertEqual({field: result["coverage"][field]["labeled"] for field in (
            "skill_loaded_after_selection", "tool_error_in_turn", "next_turn_user_correction", "turn_completed",
        )}, {"skill_loaded_after_selection": 2, "tool_error_in_turn": 3,
             "next_turn_user_correction": 2, "turn_completed": 10})
        for counts in result["coverage"].values():
            self.assertEqual(counts["denominator"], 22)
            self.assertEqual(counts["labeled"] + counts["unknown"], 22)
        self.assertEqual(result, generate(records, evidence))
        self.assertNotIn("PRIVATE_SYNTHETIC_TEXT", json.dumps(result))

    def test_raw_fields_and_ambiguous_evidence_fail_closed(self):
        from hermes_switchyard.outcome_labels import generate

        records, evidence, _ = frozen_case("explicit-correction-with-same-arm-adjacent-reply")
        for bad in (
            {**evidence[0], "chat_id": "PRIVATE_CHAT_ID"},
            {**evidence[0], "raw_error": "PRIVATE_ERROR"},
            {**evidence[0], "tool_events": [{**evidence[0]["tool_events"][0], "error": "PRIVATE_ERROR"}]},
            {**evidence[0], "reactions": [{"kind": "raw emoji", "target_message_id": "m-positive"}]},
            {**evidence[0], "turn_completed": "true"},
            {**evidence[0], "session_id": "other"},
        ):
            with self.subTest(bad=tuple(bad)):
                with self.assertRaises(ValueError):
                    generate(records, [bad, evidence[1]])
        with self.assertRaises(ValueError):
            generate(records, [evidence[0], evidence[0], evidence[1]])
        invalid = dict(records[0], prompt="PRIVATE_PROMPT")
        with self.assertRaises(ValueError):
            generate([invalid], [])

    def test_ambiguous_text_and_incomplete_trace_remain_unknown(self):
        from hermes_switchyard.outcome_labels import generate

        records, evidence, _ = frozen_case("explicit-correction-with-same-arm-adjacent-reply")
        next_user = dict(evidence[1]["user_message"], text="I meant the other report. Try again.")
        result = generate(records, [evidence[0], {**evidence[1], "user_message": next_user}])
        self.assertEqual(result["labels"][0]["next_turn_user_correction"], "UNKNOWN")
        incomplete = {**evidence[0], "tool_trace_complete": False, "tool_events": []}
        self.assertEqual(generate(records, [incomplete, evidence[1]])["labels"][0]["tool_error_in_turn"], "UNKNOWN")

    def test_unmatched_correction_cues_are_unknown_not_false(self):
        from hermes_switchyard.outcome_labels import generate

        records, evidence, _ = frozen_case("explicit-correction-with-same-arm-adjacent-reply")
        for text in ("No, that's wrong.", "I asked for three items.", "Please undo this.",
                     "Could you fix the mistake?", "No, I asked for three items, actually.",
                     "No, I asked for...", "No, I asked for", "No, I asked you to!",
                     "That's not what I asked for", "That's not what I asked you to...",
                     "You misread my request, actually.",
                     "You misunderstood my request. Try again."):
            with self.subTest(text=text):
                next_user = {**evidence[1], "user_message": {
                    "reply_to_message_id": "m-positive", "text": text,
                }}
                result = generate(records, [evidence[0], next_user])
                self.assertEqual(result["labels"][0]["next_turn_user_correction"], "UNKNOWN")

    def test_complete_correction_markers_need_no_suffix(self):
        from hermes_switchyard.outcome_labels import generate

        records, evidence, _ = frozen_case("explicit-correction-with-same-arm-adjacent-reply")
        for marker in ("You misread my request", "You misunderstood my request"):
            for suffix in ("", ".", "?!", "...", ": list three items."):
                text = f"  {marker.upper()}{suffix}  "
                with self.subTest(text=text):
                    following = {**evidence[1], "user_message": {
                        "reply_to_message_id": "m-positive", "text": text,
                    }}
                    result = generate(records, [evidence[0], following])
                    self.assertIs(result["labels"][0]["next_turn_user_correction"], True)
                    self.assertNotIn(text.strip(), json.dumps(result))

    def test_complete_correction_markers_still_require_attribution(self):
        from hermes_switchyard.outcome_labels import generate

        for boundary in ("reply_target", "retried", "undone", "completion", "session", "split", "next_turn"):
            with self.subTest(boundary=boundary):
                records, evidence, _ = frozen_case("explicit-correction-with-same-arm-adjacent-reply")
                evidence[1]["user_message"]["text"] = "You misunderstood my request."
                if boundary == "reply_target":
                    evidence[1]["user_message"]["reply_to_message_id"] = "other-message"
                elif boundary in ("retried", "undone"):
                    evidence[1][boundary] = True
                elif boundary == "completion":
                    evidence[0]["turn_completed"] = False
                elif boundary == "session":
                    records[1]["session_id"] = evidence[1]["session_id"] = "other-session"
                elif boundary == "split":
                    records[1]["turn_id"] = evidence[1]["turn_id"] = "b-no-correction"
                else:
                    records.insert(1, {**records[1], "turn_id": "j-reaction"})
                result = generate(records, evidence)
                self.assertEqual(result["labels"][0]["next_turn_user_correction"], "UNKNOWN")

    def test_cli_protects_before_writing_and_closes_before_failed_protection_cleanup(self):
        from hermes_switchyard import outcome_labels

        for denied in (False, True):
            with self.subTest(denied=denied), tempfile.TemporaryDirectory(prefix="outcome-label-test-") as temporary:
                root = Path(temporary)
                data = root / "retained"
                data.mkdir()
                records, evidence, expected = frozen_case("explicit-correction-with-same-arm-adjacent-reply")
                history = receipt_history.history_path(data)
                assert history is not None
                history.write_text(
                    "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8",
                )
                input_path = root / "evidence.json"
                input_path.write_text(json.dumps(evidence), encoding="utf-8")
                output = root / "labels.json"
                args = ["--history-dir", str(data), "--evidence", str(input_path), "--output", str(output)]
                events = []
                descriptors = []
                real_open, real_fdopen, real_unlink = os.open, os.fdopen, Path.unlink

                def capture_open(path, flags, mode):
                    descriptor = real_open(path, flags, mode)
                    descriptors.append(descriptor)
                    return descriptor

                def protect(path):
                    self.assertEqual(path, output)
                    self.assertEqual(path.read_bytes(), b"")
                    events.append("protect")
                    if denied:
                        raise OSError("synthetic protection failure")

                class TrackedOutput:
                    def __init__(self, descriptor, *args, **kwargs):
                        self.handle = real_fdopen(descriptor, *args, **kwargs)

                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        return self.handle.__exit__(*args)

                    def write(self, payload):
                        if events != ["protect"] or denied:
                            raise AssertionError("payload written before successful protection")
                        events.append("write")
                        return self.handle.write(payload)

                def remove(path, *args, **kwargs):
                    self.assertEqual(path, output)
                    self.assertEqual(events, ["protect"])
                    self.assertEqual(path.read_bytes(), b"")
                    with self.assertRaises(OSError):
                        os.fstat(descriptors[0])
                    events.append("remove")
                    return real_unlink(path, *args, **kwargs)

                with mock.patch.object(outcome_labels.os, "open", side_effect=capture_open), \
                        mock.patch.object(outcome_labels.os, "fdopen", side_effect=TrackedOutput), \
                        mock.patch.object(receipt_state, "_apply_private_permissions", side_effect=protect), \
                        mock.patch.object(Path, "unlink", autospec=True, side_effect=remove):
                    if denied:
                        with self.assertRaisesRegex(OSError, "synthetic protection failure"):
                            outcome_labels.main(args)
                    else:
                        self.assertEqual(outcome_labels.main(args), 0)
                self.assertEqual(events, ["protect", "remove"] if denied else ["protect", "write"])
                if denied:
                    self.assertFalse(output.exists())
                else:
                    self.assertEqual(json.loads(output.read_text(encoding="ascii"))["labels"], expected)
                    if os.name != "nt":
                        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                    original = output.read_bytes()
                    with mock.patch.object(receipt_state, "_apply_private_permissions") as protection:
                        with self.assertRaises(FileExistsError):
                            outcome_labels.main(args)
                        protection.assert_not_called()
                    self.assertEqual(output.read_bytes(), original)

    def test_offline_cli_reads_only_explicit_synthetic_history_and_writes_private_output(self):
        with tempfile.TemporaryDirectory(prefix="outcome-label-test-") as temporary:
            root = Path(temporary)
            data = root / "retained"
            records, evidence, expected = frozen_case("explicit-correction-with-same-arm-adjacent-reply")
            for record in records:
                self.assertTrue(receipt_history.append_receipt_history(
                    record["receipt"], session_id=record["session_id"], turn_id=record["turn_id"],
                    platform="cli", now=NOW, data_dir=data,
                ))
            input_path = root / "ephemeral-evidence.json"
            input_path.write_text(json.dumps(evidence), encoding="utf-8")
            output = root / "labels.json"
            command = [sys.executable, "-m", "hermes_switchyard.outcome_labels",
                       "--history-dir", str(data), "--evidence", str(input_path), "--output", str(output)]
            run = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                 capture_output=True, text=True, check=False, timeout=15)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, "")
            self.assertEqual(json.loads(output.read_text(encoding="ascii"))["labels"], expected)
            self.assertNotIn("PRIVATE_SYNTHETIC_TEXT", output.read_text(encoding="ascii"))
            if os.name != "nt":
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            original = output.read_bytes()
            self.assertNotEqual(subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                               capture_output=True, check=False, timeout=15).returncode, 0)
            self.assertEqual(output.read_bytes(), original)
