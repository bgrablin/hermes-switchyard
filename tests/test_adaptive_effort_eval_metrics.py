"""Offline tests for the adaptive-effort evaluation harness (issue #121).

These tests do not need Hermes. They check the frozen fixture book, the
metric functions, and that the Jev recorder keeps the exact provider wire
fields from an isolated recorded client.
"""
from __future__ import annotations

import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from evaluation import adaptive_effort_eval as harness
from hermes_switchyard import client as jev_client

ROOT = Path(__file__).resolve().parent.parent
BOOK_PATH = ROOT / "evaluation" / "adaptive_effort_fixtures.json"

STATE = {"task_present": True, "requested_effort": "high", "turn_phase": "new_turn"}
QUESTIONS = {"reasoning_effort": {"type": "choice", "instructions": "Pick the reasoning effort.", "criteria": {
    "low": "Routine work.", "medium": "Some reasoning.", "high": "Consequential work."}}}
WIRE_RESPONSE = {
    "id": "gen-synthetic-0001",
    "model": jev_client.EXPECTED_MODEL,
    "answers": {"reasoning_effort": {"choice": "medium", "confidence": 0.61,
                                     "probabilities": {"low": 0.2, "medium": 0.61, "high": 0.19}}},
    "usage": {"prompt_tokens": 211, "completion_tokens": 4, "total_tokens": 215, "cost": 0.00012},
    "latency_ms": 432.5,
}


def _recorded_client(recordings, cap=4):
    spec = {"responder": "recorded", "max_jev_calls": cap, "recordings": recordings}
    recorder = harness._JevRecorder(spec, jev_client, original_factory=None)
    return recorder, recorder.factory()


class FixtureBookTests(unittest.TestCase):
    def setUp(self):
        self.book = json.loads(BOOK_PATH.read_text(encoding="utf-8"))

    def test_committed_book_is_valid_frozen_and_split(self):
        harness.validate_fixture_book(self.book)
        splits = {item["split"] for item in self.book["fixtures"]}
        self.assertEqual(splits, {"dev", "holdout"})
        groups = {item["contrast_group"] for item in self.book["fixtures"] if item.get("contrast_group")}
        self.assertGreaterEqual(len(groups), 6)

    def test_changing_a_label_or_split_breaks_the_freeze(self):
        for mutate in (
            lambda book: book["fixtures"][0]["labels"].update({"eval_worker_rubric_v1": "keep_cap"}),
            lambda book: book["fixtures"][-1].update({"split": "dev"}),
            lambda book: book["fixtures"][0].update({"user_message": book["fixtures"][0]["user_message"] + "!"}),
        ):
            book = copy.deepcopy(self.book)
            mutate(book)
            with self.assertRaises(harness.FixtureError):
                harness.validate_fixture_book(book)

    def test_contrast_pairs_have_identical_length_and_opposite_labels(self):
        by_group = {}
        for item in self.book["fixtures"]:
            if item.get("contrast_group"):
                by_group.setdefault(item["contrast_group"], []).append(item)
        for name, members in by_group.items():
            self.assertEqual(len({len(m["user_message"]) for m in members}), 1, name)
            labels = {harness.consensus_label(m["labels"]) for m in members}
            self.assertEqual(labels, {"lower_ok", "keep_cap"}, name)

    def test_book_is_synthetic_and_sentinel_free(self):
        text = BOOK_PATH.read_text(encoding="utf-8")
        for sentinel in harness.LEAK_SENTINELS.values():
            self.assertNotIn(sentinel, text)
        self.assertTrue(all(item["public_synthetic"] is True for item in self.book["fixtures"]))

    def test_harness_does_not_use_adapter_policy_as_oracle(self):
        source = (ROOT / "evaluation" / "adaptive_effort_eval.py").read_text(encoding="utf-8")
        self.assertNotIn("reasoning_effort_adapter", source)
        self.assertNotIn("choose_reasoning_effort", source)


class JevWireFieldTests(unittest.TestCase):
    def test_recorder_keeps_exact_provider_wire_fields(self):
        payload = {"model": jev_client.EXPECTED_MODEL, "state": STATE, "questions": QUESTIONS}
        key = harness.request_key(payload)
        recorder, client = _recorded_client({key: {"source": "recorded_public_fixture",
                                                   "response": copy.deepcopy(WIRE_RESPONSE)}})
        client.decide(STATE, QUESTIONS)
        self.assertEqual(len(recorder.calls), 1)
        call = recorder.calls[0]
        self.assertTrue(call["ok"])
        self.assertEqual(call["request_key"], key)
        self.assertEqual(call["state"], STATE)
        self.assertEqual(call["question_types"], {"reasoning_effort": "choice"})
        self.assertEqual(call["model"], WIRE_RESPONSE["model"])
        self.assertEqual(call["request_id"], WIRE_RESPONSE["id"])
        self.assertEqual(call["usage"], WIRE_RESPONSE["usage"])
        self.assertEqual(call["wire_latency_ms"], WIRE_RESPONSE["latency_ms"])
        self.assertEqual(call["latency_ms"], WIRE_RESPONSE["latency_ms"])
        self.assertEqual(call["answers"], WIRE_RESPONSE["answers"])
        self.assertEqual(call["response_source"], "recorded_public_fixture")
        if jev_client.DecisionClient(api_key="x").endpoint == jev_client.DEFAULT_ENDPOINT:
            self.assertEqual(call["provider_routing"], {"allow_fallbacks": False})

    def test_missing_recording_fails_closed_and_is_recorded(self):
        recorder, client = _recorded_client({})
        with self.assertRaises(Exception):
            client.decide(STATE, QUESTIONS)
        self.assertEqual(len(recorder.calls), 1)
        self.assertFalse(recorder.calls[0]["ok"])

    def test_call_cap_is_enforced(self):
        payload = {"model": jev_client.EXPECTED_MODEL, "state": STATE, "questions": QUESTIONS}
        recordings = {harness.request_key(payload): {"response": copy.deepcopy(WIRE_RESPONSE)}}
        recorder, client = _recorded_client(recordings, cap=1)
        client.decide(STATE, QUESTIONS)
        with self.assertRaises(Exception):
            client.decide(STATE, QUESTIONS)
        self.assertTrue(recorder.cap_reached)
        self.assertEqual(recorder.total, 1)

    def test_missing_usage_and_latency_are_flagged_not_filled(self):
        response = {key: value for key, value in WIRE_RESPONSE.items() if key not in {"usage", "latency_ms", "id"}}
        payload = {"model": jev_client.EXPECTED_MODEL, "state": STATE, "questions": QUESTIONS}
        recorder, client = _recorded_client({harness.request_key(payload): {"response": response}})
        client.decide(STATE, QUESTIONS)
        fixture = {"id": "x1", "slice": "short_routine", "split": "dev", "user_message": "list files",
                   "requested_effort": "high", "phase": "new_turn", "tool_status": None,
                   "labels": {"a": "lower_ok"}, "public_synthetic": True}
        request = {"phase": "new_turn", "requested_wire": "high", "sent_wire": "low", "middleware_changed": True,
                   "prompt_bytes_identical": True, "receipt": {"status": "selected", "reason_code": "jev_selected",
                                                               "cap": "high"}}
        child = {"records": [{"id": "x1", "route": "r", "requests": [request], "jev_calls": recorder.calls}]}
        record = harness.build_records({}, [fixture], child, "recorded")[0]
        for flag in ("jev_usage_missing", "jev_wire_latency_missing", "jev_request_id_missing",
                     "responder_not_live_no_accuracy_claim", "single_labeler"):
            self.assertIn(flag, record["missing_evidence"])
        self.assertIsNone(record["jev"]["calls"][0]["wire_latency_ms"])
        self.assertEqual(record["outcome"], "lowered")
        self.assertTrue(record["score"]["successful_lower"])


class MetricTests(unittest.TestCase):
    def test_outcome_uses_wire_levels_only(self):
        cases = [
            (("low", "high", "jev_selected", 1), "lowered"),
            (("high", "high", "jev_selected", 1), "kept"),
            (("high", "high", "kept_requested_on_jev_failure", 1), "abstained"),
            (("high", "high", None, 0), "kept_no_jev_call"),
            (("xhigh", "high", "jev_selected", 1), "raised"),
            ((None, "high", "jev_selected", 1), "unmeasured"),
        ]
        for args, expected in cases:
            self.assertEqual(harness.classify_outcome(*args), expected, args)

    def test_scores_compare_outcome_with_independent_label(self):
        self.assertTrue(harness.score_record({"label": "keep_cap", "outcome": "lowered"})["false_lower"])
        self.assertTrue(harness.score_record({"label": "lower_ok", "outcome": "lowered"})["successful_lower"])
        self.assertTrue(harness.score_record({"label": "lower_ok", "outcome": "abstained"})["missed_lower"])
        ambiguous = harness.score_record({"label": "ambiguous", "outcome": "lowered"})
        self.assertFalse(ambiguous["false_lower"] or ambiguous["successful_lower"])

    def test_accuracy_claim_only_for_live_holdout(self):
        for responder, split, allowed in (("live", "holdout", True), ("live", "dev", False),
                                          ("recorded", "holdout", False), ("synthetic_lowest", "holdout", False)):
            self.assertIs(harness.summarize([], responder=responder, split=split)["accuracy_claim_allowed"], allowed)

    def test_contrast_report_detects_identical_jev_states(self):
        def rec(fid, digest):
            return {"id": fid, "route": "r", "contrast_group": "g", "jev": {"first_state_sha256": digest}}
        same = harness.contrast_report([rec("a", "d1"), rec("b", "d1")])
        self.assertFalse(same["all_distinguishable"])
        different = harness.contrast_report([rec("a", "d1"), rec("b", "d2")])
        self.assertTrue(different["all_distinguishable"])
        unmeasured = harness.contrast_report([rec("a", None), rec("b", "d2")])
        self.assertEqual(unmeasured["measurable"], 0)

    def test_wilson_interval_bounds(self):
        self.assertEqual(harness.wilson_interval(0, 0), (None, None))
        low, high = harness.wilson_interval(0, 40)
        self.assertEqual(low, 0.0)
        self.assertLess(high, 0.1)


class CommandLineTests(unittest.TestCase):
    def _run(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = harness.main(argv)
        return code, json.loads(buffer.getvalue().splitlines()[-1])

    def test_live_mode_requires_explicit_network_flag(self):
        code, report = self._run(["--responder", "live"])
        self.assertEqual((code, report["error"]["code"]), (2, "live_requires_allow_network"))

    def test_live_mode_requires_bounded_cap(self):
        code, report = self._run(["--responder", "live", "--allow-network", "--max-jev-calls", "0"])
        self.assertEqual((code, report["error"]["code"]), (2, "live_requires_bounded_call_cap"))

    def test_recorded_mode_requires_recordings(self):
        code, report = self._run(["--responder", "recorded"])
        self.assertEqual((code, report["error"]["code"]), (2, "recorded_requires_recordings_file"))


if __name__ == "__main__":
    unittest.main()
