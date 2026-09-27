"""Regression tests for the Copilot review findings on PR #125 (review 5332095377).

Synthetic values only. Secret-shaped values are built at run time from inert fragments.
No test reaches the network.
"""
from __future__ import annotations

import json
import time
import unittest

from hermes_switchyard.automatic import AutomaticSkillRecommender
from hermes_switchyard.egress_redaction import mask_personal_data
from hermes_switchyard.reasoning_effort_adapter import (
    ReasoningEffortController,
    _effort_scan_reason,
    last_receipt,
)
from test_reasoning_effort_adapter import OracleClient, capture_turn, opus
from test_routing_redact_bypass import CATALOG, Transport, forbidden_factory, recommender
from test_support import HermesHomeTestCase

_EQ = "="
_TYPED_TASK = "deploy the docker stack"


def _effort_send(client, text):
    factory_calls: list[bool] = []

    def factory():
        factory_calls.append(True)
        return client

    controller = ReasoningEffortController(client_factory=factory)
    capture_turn(controller, text, session_id="s", turn_id="t1")
    request = opus("high")
    controller.on_llm_request(
        request, session_id="s", task_id="s", turn_id="t1",
        provider="anthropic", model="claude-opus-5-5", api_mode="anthropic_messages",
    )
    return factory_calls


class TypedTextPartsRoutingTests(HermesHomeTestCase):
    """[4116938617] Only parts typed text/input_text become the routing allowed_payload."""

    def test_untyped_and_non_text_parts_never_reach_the_hosted_selector(self):
        task = [
            {"type": "document", "text": "DOCUMENTBODYWORD"},
            {"type": "tool_result", "text": "TOOLRESULTWORD"},
            {"type": "image_url", "text": "IMAGECAPTIONWORD", "image_url": {"url": "data:image/png;base64,QQ"}},
            {"text": "UNTYPEDPARTWORD"},
            "BARESTRINGWORD",
            {"type": "text", "text": _TYPED_TASK},
        ]
        transport = Transport()
        result = recommender(transport).recommend(task)
        self.assertTrue(result["hosted_attempted"], result.get("hosted_skipped"))
        wire = transport.wire()
        for word in ("DOCUMENTBODYWORD", "TOOLRESULTWORD", "IMAGECAPTIONWORD", "UNTYPEDPARTWORD", "BARESTRINGWORD"):
            self.assertNotIn(word, wire)
        self.assertIn(_TYPED_TASK, wire)

    def test_input_text_parts_and_plain_strings_still_reach_the_hosted_selector(self):
        for task in ([{"type": "input_text", "text": _TYPED_TASK}], _TYPED_TASK):
            with self.subTest(kind=type(task).__name__):
                transport = Transport()
                result = recommender(transport).recommend(task)
                self.assertTrue(result["hosted_attempted"], result.get("hosted_skipped"))
                self.assertIn(_TYPED_TASK, transport.wire())


class TypedTextPartsEffortTests(HermesHomeTestCase):
    """[4116938718] Bare string elements of a part list never enter current_request."""

    def test_bare_string_list_elements_are_rejected(self):
        client = OracleClient()
        _effort_send(client, ["BARESTRINGWORD", {"type": "text", "text": "describe this"}])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0].get("current_request"), "describe this")
        self.assertNotIn("BARESTRINGWORD", json.dumps(client.calls, default=str))

    def test_list_of_only_bare_strings_sends_no_text(self):
        client = OracleClient()
        _effort_send(client, ["BARESTRINGWORD only"])
        self.assertNotIn("BARESTRINGWORD", json.dumps(client.calls, default=str))

    def test_plain_string_content_still_works(self):
        client = OracleClient()
        _effort_send(client, "describe this")
        self.assertEqual(client.calls[0][0].get("current_request"), "describe this")


class ShortSecretMaskTests(unittest.TestCase):
    """[4116938639] Assigned secret values of 1-256 characters are masked."""

    def test_short_assigned_values_are_masked(self):
        cases = {
            "pwd" + _EQ + "abc": "pwd" + _EQ + "***",
            "password" + _EQ + "x": "password" + _EQ + "***",
            "token: ab": "token: ***",
            "login with --password ab now": "login with --password *** now",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(mask_personal_data(text), expected)

    def test_ordinary_text_and_email_addresses_are_unchanged(self):
        for text in (
            "email ops" + "@" + "example.com about the docker outage",
            "rotate the password on the staging registry",
            "set TOKEN_TTL" + _EQ + "3600 and retry",
            "keep the secret santa list private",
        ):
            with self.subTest(text=text):
                self.assertEqual(mask_personal_data(text), text)

    def test_adversarial_input_stays_linear(self):
        for text in (
            "password" + _EQ * 50_000,
            ("pwd" + _EQ + "a ") * 20_000,
            "--password " * 20_000,
            "token:" + " " * 3 + "x" * 60_000,
        ):
            started = time.perf_counter()
            mask_personal_data(text)
            self.assertLess(time.perf_counter() - started, 1.0)


class ConfidentialBannerTests(HermesHomeTestCase):
    """[4116938663, 4116938679] A standalone Confidential banner is a restricted marking."""

    BANNERS = (
        "Confidential: Review the technical design.",
        "CONFIDENTIAL\nReview the technical design.",
        "\n  confidential\nReview the technical design.",
        "CONFIDENTIAL//NOFORN\nReview the technical design.",
        "Confidential//Internal: review the design",
    )
    ORDINARY = (
        "please keep this confidential and review the design",
        "review the confidentiality agreement template",
        "Confidentiality agreements: what should they cover?",
        "review the confidential rollout notes for the docker upgrade",
    )

    def test_banner_is_a_restricted_marking(self):
        for text in self.BANNERS:
            with self.subTest(text=text):
                self.assertEqual(_effort_scan_reason(text), "local_scan_restricted_marking")

    def test_mid_sentence_words_stay_ordinary(self):
        for text in self.ORDINARY:
            with self.subTest(text=text):
                self.assertIsNone(_effort_scan_reason(text))

    def test_banner_makes_no_hosted_call_on_either_path(self):
        for text in self.BANNERS:
            with self.subTest(text=text[:20]):
                calls: list[bool] = []
                routed = AutomaticSkillRecommender(
                    configured_candidates=CATALOG, routing_mode="hosted_sanitized", hosted_mode="always",
                    public_or_sanitized_data_ack=True, client_factory=forbidden_factory(calls), cache_seconds=0.0,
                ).recommend(text)
                self.assertEqual(calls, [])
                self.assertFalse(routed["hosted_attempted"])
                client = OracleClient()
                _effort_send(client, text)
                self.assertNotIn("Review the technical design", json.dumps(client.calls, default=str))
                self.assertNotIn("review the design", json.dumps(client.calls, default=str))
                self.assertEqual(last_receipt()["reason_code"], "kept_requested_restricted_text")


# [4116938730] Metadata-only fallback (no Hermes redactor): covered by
# test_reasoning_effort_visible_value.MetadataOnlyFallbackTests
# .test_no_user_text_substring_leaves_the_process, which asserts that Jev is asked
# and that no substring of the user message reaches the Jev request.


if __name__ == "__main__":
    unittest.main()
