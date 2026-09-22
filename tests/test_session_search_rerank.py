"""Offline tests for Jev session_search re-rank (stubbed client, no network)."""
from __future__ import annotations

import unittest

from hermes_switchyard.session_search_rerank import (
    DEFAULT_MAX_CARD_CHARS,
    redact_card_text,
    rerank_session_search,
)


class FakeDecisionClient:
    def __init__(self, response_factory=None, *, error=None):
        self.response_factory = response_factory
        self.error = error
        self.calls = []

    def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
        if public_or_sanitized_data_ack is not True:
            raise AssertionError("test client requires the acknowledgement")
        self.calls.append((state, questions))
        if self.error is not None:
            raise self.error
        return self.response_factory(state, questions)


def _choice(criteria, selected, confidence=0.95, winning=0.9):
    keys = list(criteria)
    if selected not in criteria:
        selected = keys[0]
    if len(keys) == 1:
        probabilities = {keys[0]: 1.0}
    else:
        remainder = 1.0 - winning
        probabilities = {key: remainder / (len(keys) - 1) for key in keys}
        probabilities[selected] = winning
    return {"choice": selected, "confidence": confidence, "probabilities": probabilities}


def _session_response(selected, *, confidence=0.95, winning=0.9, model="typesafe/jev-1.13"):
    def response(_state, questions):
        answers = {}
        if "session" in questions:
            criteria = questions["session"]["criteria"]
            answers["session"] = _choice(criteria, selected, confidence, winning)
        if "match_message" in questions:
            criteria = questions["match_message"]["criteria"]
            answers["match_message"] = _choice(criteria, next(iter(criteria)), 0.92, 0.88)
        return {
            "model": model,
            "request_id": "req-test-1",
            "answers": answers,
            "usage": {"total_tokens": 12},
            "latency_ms": 15.0,
            "request_count": 1,
            "total_latency_ms": 15.0,
            "total_usage": {"total_tokens": 12},
        }

    return response


def _candidates():
    return [
        {
            "session_id": "sess-a",
            "title": "Grocery list",
            "snippet": "milk eggs bread contact alice@example.com",
            "match_message_ids": ["msg-a1", "msg-a2"],
        },
        {
            "session_id": "sess-b",
            "title": "Deploy plan",
            "snippet": "rollback window and canary for payments phone +1 555 0100",
            "match_message_ids": ["msg-b1"],
        },
        {
            "session_id": "sess-c",
            "title": "Unrelated",
            "snippet": "weather forecast",
        },
    ]


class RedactionTests(unittest.TestCase):
    def test_redacts_email_phone_and_token(self):
        text = (
            "email me at alice@example.com or +1 (555) 010-9988 "
            "api_key=sk-abcdefghijklmnopqrstuvwxyz"
        )
        redacted = redact_card_text(text)
        self.assertNotIn("alice@example.com", redacted)
        self.assertIn("[email]", redacted)
        self.assertIn("[phone]", redacted)
        self.assertIn("[secret]", redacted)


class RerankTests(unittest.TestCase):
    def test_empty_shortlist_skips_provider(self):
        client = FakeDecisionClient(_session_response("sess-a"))
        result = rerank_session_search(
            query="where did we talk about canary deploys?",
            candidates=[],
            client=client,
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "empty")
        self.assertIsNone(result["selected_session_id"])
        self.assertEqual(result["shortlist_size"], 0)
        self.assertEqual(client.calls, [])
        self.assertFalse(result["redaction"]["full_transcripts_sent"])

    def test_winner_pick_and_optional_message(self):
        client = FakeDecisionClient(_session_response("sess-b"))
        result = rerank_session_search(
            query="which session covered the canary deploy rollback?",
            candidates=_candidates(),
            client=client,
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["selected_session_id"], "sess-b")
        self.assertEqual(result["match_message_id"], "msg-b1")
        self.assertIsNone(result["fail_open_reason"])
        self.assertEqual(result["shortlist_size"], 3)
        self.assertEqual(result["model"], "typesafe/jev-1.13")
        self.assertEqual(result["request_id"], "req-test-1")
        self.assertGreaterEqual(result["confidence"], 0.9)
        self.assertFalse(result["redaction"]["full_transcripts_sent"])
        self.assertEqual(result["redaction"]["max_card_chars"], DEFAULT_MAX_CARD_CHARS)
        # First call is session Choice; single-anchor winner skips second call.
        self.assertEqual(len(client.calls), 1)
        state, questions = client.calls[0]
        self.assertIn("recall_question", state)
        self.assertEqual(set(questions), {"session"})
        # Redaction applied before egress.
        preview = questions["session"]["criteria"]["sess-a"]
        self.assertNotIn("alice@example.com", preview)
        self.assertIn("[email]", preview)

    def test_fail_open_on_provider_error(self):
        client = FakeDecisionClient(error=RuntimeError("provider down"))
        result = rerank_session_search(
            query="recall the deploy thread",
            candidates=_candidates(),
            client=client,
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "fail_open")
        self.assertEqual(result["selected_session_id"], "sess-a")
        self.assertEqual(result["fail_open_reason"], "provider_failed")
        self.assertTrue(result["fts_order_preserved"])
        self.assertIsNone(result["match_message_id"])

    def test_fail_open_on_low_confidence(self):
        client = FakeDecisionClient(
            _session_response("sess-c", confidence=0.4, winning=0.55)
        )
        result = rerank_session_search(
            query="recall the deploy thread",
            candidates=_candidates(),
            client=client,
            choice_confidence_threshold=0.8,
            winning_probability_threshold=0.8,
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "fail_open")
        self.assertEqual(result["selected_session_id"], "sess-a")
        self.assertEqual(result["fail_open_reason"], "choice_confidence_below_threshold")

    def test_fail_open_on_low_winning_probability(self):
        client = FakeDecisionClient(
            _session_response("sess-c", confidence=0.95, winning=0.5)
        )
        result = rerank_session_search(
            query="recall the deploy thread",
            candidates=_candidates(),
            client=client,
            choice_confidence_threshold=0.8,
            winning_probability_threshold=0.8,
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "fail_open")
        self.assertEqual(result["selected_session_id"], "sess-a")
        self.assertEqual(result["fail_open_reason"], "winning_probability_below_threshold")

    def test_ack_false_refuses(self):
        client = FakeDecisionClient(_session_response("sess-b"))
        with self.assertRaises(PermissionError):
            rerank_session_search(
                query="x",
                candidates=_candidates(),
                client=client,
                public_or_sanitized_data_ack=False,
            )
        self.assertEqual(client.calls, [])

    def test_second_choice_among_message_anchors(self):
        def factory(state, questions):
            if "session" in questions:
                criteria = questions["session"]["criteria"]
                return {
                    "model": "typesafe/jev-1.13",
                    "request_id": "req-sess",
                    "answers": {
                        "session": _choice(criteria, "sess-a", 0.95, 0.9),
                    },
                    "usage": {},
                    "latency_ms": 10.0,
                    "request_count": 1,
                    "total_latency_ms": 10.0,
                    "total_usage": {},
                }
            criteria = questions["match_message"]["criteria"]
            return {
                "model": "typesafe/jev-1.13",
                "request_id": "req-msg",
                "answers": {
                    "match_message": _choice(criteria, "msg-a2", 0.93, 0.91),
                },
                "usage": {},
                "latency_ms": 8.0,
                "request_count": 1,
                "total_latency_ms": 8.0,
                "total_usage": {},
            }

        client = FakeDecisionClient(factory)
        result = rerank_session_search(
            query="where was the grocery list message?",
            candidates=_candidates(),
            client=client,
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["selected_session_id"], "sess-a")
        self.assertEqual(result["match_message_id"], "msg-a2")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[1][1].keys(), {"match_message"})


if __name__ == "__main__":
    unittest.main()
