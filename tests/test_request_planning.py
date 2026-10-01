"""Compare incremental sizing with full serialization, including byte boundaries."""
import json
import random
import unittest
from unittest.mock import patch

from hermes_switchyard import client, routing


def reference_chunks(candidates, task, *, multi=False):
    """Independent, deliberately slow oracle using complete trial requests."""
    chunks, current = [], []
    offset = 0
    for candidate in candidates:
        while True:
            trial = current + [candidate]
            if multi:
                questions = routing._multi_skill_batch_questions(trial, offset=offset, task=task)
                state = {"task": task, "skills": trial}
                criteria_over = False
                limit = client.MAX_QUESTIONS_PER_REQUEST
            else:
                state, questions = routing._skill_request_parts(
                    trial, task=task, total_count=len(candidates), partition=len(chunks),
                    include_needs_skill=not chunks,
                )
                criteria = questions[f"skill_chunk_{len(chunks)}"]["criteria"]
                criteria_over = len(json.dumps(criteria, ensure_ascii=False, allow_nan=False).encode()) > routing._PARTITION_CRITERIA_BYTES
                limit = routing._SKILL_PARTITION_SIZE
            oversized = routing._request_size(state, questions) > routing.MAX_REQUEST_BYTES
            if not current and oversized:
                raise ValueError("oversized singleton")
            if current and (len(trial) > limit or criteria_over or oversized):
                chunks.append(current)
                offset += len(current)
                current = []
                continue
            current = trial
            break
    if current:
        chunks.append(current)
    return chunks


def reference_batches(decider, state, questions):
    batches, current = [], {}
    for name, question in decider._validate_questions(questions).items():
        trial = {**current, name: question}
        try:
            decider._payload(state, trial)
        except ValueError:
            if not current:
                raise
            batches.append(current)
            current = {name: question}
            decider._payload(state, current)
        else:
            if len(trial) > client.MAX_QUESTIONS_PER_REQUEST:
                batches.append(current)
                current = {name: question}
            else:
                current = trial
    if current:
        batches.append(current)
    return batches


def transport(payload):
    return {"model": payload["model"], "answers": {
        name: {"noul": 0.5} for name in payload["questions"]
    }, "usage": {"input_tokens": len(payload["questions"])}}


class RequestPlanningTests(unittest.TestCase):
    def test_chunk_plans_match_complete_serialization(self):
        rng = random.Random(73108)
        for count in (1, 25, 199, 200, 201, 255, 256, 600, 2201):
            candidates = [{"name": f'skill-{i}-"é😀', "description": rng.choice([
                "", "line\nback\\slash\t", "日本語" * 12, "x" * 700,
            ]), "extra": {"values": [i, None, True]}} for i in range(count)]
            for multi in (False, True):
                planner = routing._multi_skill_batches if multi else routing._skill_chunks
                with self.subTest(count=count, multi=multi):
                    self.assertEqual(planner(candidates, task='task "é😀'), reference_chunks(candidates, 'task "é😀', multi=multi))

    def test_exact_request_boundaries_and_partition_digits(self):
        candidates = [{"name": f"s{i}", "description": "é" * 80} for i in range(35)]
        for multi in (False, True):
            if multi:
                state = {"task": "task", "skills": candidates[:2]}
                questions = routing._multi_skill_batch_questions(candidates[:2], offset=0, task="task")
            else:
                state, questions = routing._skill_request_parts(candidates[:2], task="task", total_count=35, partition=0, include_needs_skill=True)
            boundary = routing._request_size(state, questions)
            planner = routing._multi_skill_batches if multi else routing._skill_chunks
            for delta in (-1, 0, 1):
                with self.subTest(multi=multi, delta=delta), patch.object(routing, "MAX_REQUEST_BYTES", boundary + delta):
                    actual = planner(candidates, task="task")
                    self.assertGreater(len(actual), 10)
                    self.assertEqual(actual, reference_chunks(candidates, "task", multi=multi))
                    self.assertEqual(len(actual[0]), 1 if delta < 0 else 2)

    def test_criteria_boundary_matches_complete_serialization(self):
        candidates = [{"name": f"s{i}", "description": "é" * 50} for i in range(35)]
        _, questions = routing._skill_request_parts(candidates[:2], task="", total_count=35, partition=0, include_needs_skill=True)
        boundary = len(json.dumps(questions["skill_chunk_0"]["criteria"], ensure_ascii=False).encode())
        for delta in (-1, 0, 1):
            with patch.object(routing, "_PARTITION_CRITERIA_BYTES", boundary + delta):
                self.assertEqual(routing._skill_chunks(candidates), reference_chunks(candidates, ""))

    def test_batches_match_wire_payloads_for_both_endpoints(self):
        questions = {f'q{i}"é😀': {"type": "noul", "instructions": "read\n" + "é" * (i % 9)} for i in range(600)}
        for endpoint, model in ((client.DEFAULT_ENDPOINT, client.EXPECTED_MODEL), (client.TYPESAFE_ENDPOINT, "jev-1.13.0")):
            calls = []
            decider = client.DecisionClient(api_key="synthetic", endpoint=endpoint, model=model, transport=lambda payload: (calls.append(payload), transport(payload))[1])
            for state in ({"text": "small"}, {"text": "é" * 35000}):
                with self.subTest(endpoint=endpoint, large=len(state["text"]) > 10):
                    calls.clear()
                    expected = reference_batches(decider, state, questions)
                    result = decider.decide(state, questions)
                    expected_payloads = [decider._payload(state, batch) for batch in expected]
                    self.assertEqual(calls, expected_payloads)
                    self.assertEqual(
                        json.dumps(calls, ensure_ascii=False).encode(),
                        json.dumps(expected_payloads, ensure_ascii=False).encode(),
                    )
                    self.assertEqual(list(result["answers"]), list(questions))
                    self.assertEqual(result["request_count"], len(expected))
                    self.assertEqual(result["total_usage"]["input_tokens"], len(questions))

    def test_client_exact_byte_boundary(self):
        calls = []
        decider = client.DecisionClient(api_key="synthetic", transport=lambda payload: (calls.append(payload), transport(payload))[1])
        questions = {f"q{i}": {"type": "noul", "instructions": "é" * 10} for i in range(3)}
        boundary = len(json.dumps(decider._payload("task", dict(list(questions.items())[:2])), ensure_ascii=False).encode())
        for delta in (-1, 0, 1):
            calls.clear()
            with patch.object(client, "MAX_REQUEST_BYTES", boundary + delta):
                expected = reference_batches(decider, "task", questions)
                decider.decide("task", questions)
                self.assertEqual([p["questions"] for p in calls], expected)
                self.assertEqual(len(calls[0]["questions"]), 1 if delta < 0 else 2)

    def test_invalid_tail_and_budget_fail_before_transport(self):
        calls = []
        decider = client.DecisionClient(api_key="synthetic", transport=lambda payload: (calls.append(payload), transport(payload))[1])
        question = {"type": "noul", "instructions": "public synthetic input"}
        for state, questions in ((float("nan"), {"q": question}), ("task", {"q": question, "tail": {"type": "noul", "instructions": "x" * 96000}})):
            with self.assertRaises(ValueError):
                decider.decide(state, questions)
        with decider.request_budget(1), self.assertRaisesRegex(ValueError, "budget exceeded"):
            decider.decide("task", {f"q{i}": question for i in range(256)})
        self.assertFalse(calls)

    def test_bad_skill_payloads_fail_before_transport(self):
        for bad in ({"name": "big", "description": "x" * 96000}, {"name": "nan", "extra": float("nan")}, {"name": "object", "extra": object()}):
            for planner in (routing._skill_chunks, routing._multi_skill_batches):
                with self.subTest(bad=bad["name"], planner=planner.__name__), self.assertRaises(ValueError):
                    planner([{"name": "good"}, bad], task="task")

    def test_duplicate_and_reserved_candidates_still_rejected(self):
        for candidates in ([{"name": "same"}, {"name": "same"}], [{"name": routing._SKILL_NONE}]):
            with self.assertRaises(ValueError):
                routing._skill_chunks(candidates)


if __name__ == "__main__":
    unittest.main()
