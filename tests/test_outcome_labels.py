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

    def test_documented_comma_and_unicode_meaningful_suffix(self):
        from hermes_switchyard.outcome_labels import generate

        records, evidence, _ = frozen_case("explicit-correction-with-same-arm-adjacent-reply")
        for text, expected in (
            ("No, I asked for 日本語.", True),
            ("No, I asked you to 翻訳.", True),
            ("That's not what I asked for: Ελληνικά.", True),
            ("No I asked for three items.", "UNKNOWN"),
            ("No! I asked for three items.", "UNKNOWN"),
            ("No? I asked for three items.", "UNKNOWN"),
            ("No. I asked for three items.", "UNKNOWN"),
            ("No, I asked for ...?!", "UNKNOWN"),
        ):
            with self.subTest(text=text):
                following = {**evidence[1], "user_message": {
                    "reply_to_message_id": "m-positive", "text": text,
                }}
                report = generate(records, [evidence[0], following])
                self.assertEqual(report["labels"][0]["next_turn_user_correction"], expected)
                self.assertNotIn(text, json.dumps(report, ensure_ascii=False))

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

    def test_atomic_report_protection_cleanup_and_no_clobber(self):
        from hermes_switchyard import outcome_labels
        payload = '{"schema":"test","labels":[]}\n'
        for failure in (None, "protect", "flush", "race"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                output = Path(root) / "labels.json"
                real_link = os.link
                real_protect = receipt_state._apply_private_permissions
                events = []
                def protect(path):
                    self.assertFalse(output.exists())
                    self.assertEqual(path.read_bytes(), b"")
                    events.append("protect")
                    if failure == "protect":
                        raise OSError("protection failed")
                    real_protect(path)
                def sync(descriptor):
                    self.assertFalse(output.exists())
                    self.assertEqual(events, ["protect"])
                    self.assertGreater(os.fstat(descriptor).st_size, 0)
                    events.append("sync")
                    if failure == "flush":
                        raise OSError("flush failed")
                def publish(source, destination):
                    self.assertEqual(events, ["protect", "sync"])
                    self.assertEqual(Path(source).read_text(), payload)
                    self.assertFalse(output.exists())
                    if failure == "race":
                        output.write_text("concurrent writer")
                    return real_link(source, destination)
                with mock.patch.object(receipt_state, "_apply_private_permissions", side_effect=protect), \
                        mock.patch.object(outcome_labels.os, "fsync", side_effect=sync), \
                        mock.patch.object(outcome_labels.os, "link", side_effect=publish):
                    if failure:
                        with self.assertRaises(OSError):
                            outcome_labels._publish_report(output, payload)
                    else:
                        outcome_labels._publish_report(output, payload)
                self.assertEqual(list(Path(root).glob(".outcome-labels-*.tmp")), [])
                if failure == "race":
                    self.assertEqual(output.read_text(), "concurrent writer")
                elif failure:
                    self.assertFalse(output.exists())
                else:
                    self.assertEqual(output.read_text(), payload)
                    if os.name != "nt":
                        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                    with self.assertRaises(FileExistsError):
                        outcome_labels._publish_report(output, "replacement")
                    self.assertEqual(output.read_text(), payload)

    def test_strict_history_refuses_unreadable_or_linked_source_but_accepts_empty(self):
        from hermes_switchyard import outcome_labels
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            history = root / receipt_history.HISTORY_FILE_NAME
            history.write_text("")
            self.assertEqual(outcome_labels._read_retained_history(history), [])
            with mock.patch.object(outcome_labels.os, "open", side_effect=PermissionError("denied")):
                with self.assertRaises(PermissionError):
                    outcome_labels._read_retained_history(history)
            evidence = root / "evidence.json"
            evidence.write_text("[]")
            output = root / "labels.json"
            with mock.patch.object(outcome_labels, "_read_retained_history", side_effect=OSError("read failed")):
                with self.assertRaises(OSError):
                    outcome_labels.main(["--history-dir", str(root), "--evidence", str(evidence), "--output", str(output)])
            self.assertFalse(output.exists())
            linked = root / "linked.jsonl"
            os.link(history, linked)
            with self.assertRaises(OSError):
                outcome_labels._read_retained_history(history)
            linked.unlink()
            if os.name != "nt":
                linked.symlink_to(history)
                with self.assertRaises(OSError):
                    outcome_labels._read_retained_history(linked)

    def test_history_revalidates_link_count_and_path_after_read(self):
        from types import SimpleNamespace
        from hermes_switchyard import outcome_labels
        with tempfile.TemporaryDirectory() as root:
            history = Path(root) / "history.jsonl"
            history.write_text("")
            original = history.stat()
            values = {name: getattr(original, name) for name in (
                "st_mode", "st_nlink", "st_dev", "st_ino", "st_size", "st_mtime_ns",
            )}
            for boundary in ("new_link", "path_replaced", "path_unlinked"):
                changed = SimpleNamespace(**{**values, "st_nlink": 2})
                replaced = SimpleNamespace(**{**values, "st_ino": original.st_ino + 1})
                stats = [original, changed if boundary == "new_link" else original]
                paths = [original, original, FileNotFoundError() if boundary == "path_unlinked" else replaced]
                if boundary == "new_link":
                    paths[-1] = original
                with self.subTest(boundary=boundary), \
                        mock.patch.object(outcome_labels.os, "fstat", side_effect=stats), \
                        mock.patch.object(Path, "lstat", side_effect=paths):
                    with self.assertRaises(OSError):
                        outcome_labels._read_retained_history(history)

    def test_cleanup_failure_preserves_publication_success_or_original_error(self):
        from hermes_switchyard import outcome_labels
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "labels.json"
            with mock.patch.object(Path, "unlink", side_effect=OSError("cleanup failed")):
                outcome_labels._publish_report(output, "complete\n")
            self.assertEqual(output.read_text(), "complete\n")
            with mock.patch.object(Path, "unlink", side_effect=OSError("cleanup failed")), \
                    mock.patch.object(receipt_state, "_apply_private_permissions", side_effect=PermissionError("protect failed")):
                with self.assertRaisesRegex(PermissionError, "protect failed"):
                    outcome_labels._publish_report(Path(root) / "other.json", "unpublished")
            self.assertFalse((Path(root) / "other.json").exists())

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
