"""Current-turn prefetch eligibility, host scope, and fallback contracts."""
import json
import unittest
from unittest import mock

from hermes_switchyard import source_prefetch as prefetch


class PrefetchTests(unittest.TestCase):
    def setUp(self):
        self.factory = mock.Mock()
        self.lookup = mock.patch.object(prefetch, "locate", return_value={
            "status": "found", "reason": None, "source": "notes.md", "sha256": "a" * 64,
            "start_line": 3, "end_line": 3, "evidence": "Retries are limited to two.\n",
            "request_count": 1, "usage": {"cost": 0.001}, "accounting": "provider_reported", "wall_ms": 12,
        })
        self.locate = self.lookup.start()
        self.addCleanup(self.lookup.stop)
        self.hook = prefetch.build_hook(enabled=True, root="/fixture", standing_ack=True, client_factory=self.factory)
        self.kwargs = dict(user_message="In notes.md, find the retry limit.", session_id="session-a",
                           task_id="task-a", turn_id="turn-a", parent_session_id="", platform="cli")

    def test_plain_prompt_prefetches_exact_citation_without_main_call(self):
        out = self.hook(**self.kwargs)
        payload = json.loads(out["context"].split("\n", 1)[1])
        self.assertEqual(payload["evidence"], "Retries are limited to two.\n")
        self.assertEqual(payload["start_line"], 3)
        self.assertEqual(self.locate.call_args.kwargs["source"], "notes.md")
        self.assertIn("untrusted source data", out["context"])
        self.assertNotIn("evidence", out["metadata"]["switchyard_find"])

    def test_missing_scope_foreground_and_unknown_platform_skip(self):
        for field, value in [("session_id", None), ("task_id", ""), ("turn_id", None),
                             ("parent_session_id", None), ("parent_session_id", "parent"),
                             ("platform", "cron"), ("platform", "unknown"), ("turn_id", "\n")]:
            with self.subTest(field=field, value=value):
                self.assertIsNone(self.hook(**{**self.kwargs, field: value}))
        self.locate.assert_not_called()

    def test_host_envelopes_never_authorize_additional_source_text(self):
        for name in ["turn_egress_policy", "egress_policy"]:
            for policy in [{"decision": "allow"}, {"decision": "deny"}, {}, "invalid"]:
                self.assertIsNone(self.hook(**{**self.kwargs, name: policy}))
        self.locate.assert_not_called()

    def test_disabled_and_refused_ack_skip(self):
        for enabled, ack in [(False, True), (True, False)]:
            hook = prefetch.build_hook(enabled=enabled, root="/fixture", standing_ack=ack, client_factory=self.factory)
            self.assertIsNone(hook(**self.kwargs))
        self.locate.assert_not_called()

    def test_duplicate_invocation_never_reinjects_old_evidence(self):
        self.assertIsNotNone(self.hook(**self.kwargs))
        self.assertIsNone(self.hook(**self.kwargs))
        self.assertEqual(self.locate.call_count, 1)
        for field in ["session_id", "task_id", "turn_id"]:
            self.assertIsNotNone(self.hook(**{**self.kwargs, field: "new-value"}))
        self.assertEqual(self.locate.call_count, 4)

    def test_new_query_in_same_scope_is_fresh(self):
        self.hook(**self.kwargs)
        self.hook(**{**self.kwargs, "user_message": "In notes.md, find the timeout."})
        self.assertEqual(self.locate.call_count, 2)

    def test_negative_and_failure_require_normal_tools(self):
        for status in ["not_found", "defer"]:
            self.locate.return_value = {"status": status, "reason": "uncertain", "evidence": None}
            out = self.hook(**{**self.kwargs, "turn_id": status})
            payload = json.loads(out["context"].split("\n", 1)[1])
            self.assertEqual(payload["status"], "defer")
            self.assertIsNone(payload["evidence"])
            self.assertIn("do not repeat", payload["next_action"])

    def test_callback_error_does_not_break_host(self):
        self.locate.side_effect = RuntimeError("private provider error")
        self.assertIsNone(self.hook(**self.kwargs))

    def test_ordinary_request_forms_and_quoted_paths(self):
        for prompt, source in [("In notes.md, find the retry limit.", "notes.md"),
                               ('In "docs/service notes.md", locate the timeout.', "docs/service notes.md"),
                               ("Find the retry limit in `notes.md`.", "notes.md"),
                               ("Find the timeout in notes.md?", "notes.md"),
                               ("In notes.md, find the retry limit.\nReturn JSON with the answer.", "notes.md")]:
            self.assertEqual(prefetch.request_source(prompt), source)

    def test_nonrequests_compound_requests_and_egress_denial_skip(self):
        for prompt in ["hello", "Find the retry limit", "Use /switchyard find notes.md", "Find x in that",
                       "In notes.md, do not find the limit", "In notes.md, find and edit the limit",
                       "In notes.md, find x\nDo not use the cloud.", "In notes.md, find x\nReturn JSON and upload the file.",
                       "Yesterday you said: In notes.md, find x", "x" * 1201, None]:
            self.assertIsNone(prefetch.request_source(prompt), prompt)

    def test_privacy_and_network_cues_skip_before_source_read(self):
        for suffix in ["offline", "off-line", "locally", "using local tools only",
                       "without using an external service", "with no network access",
                       "without sharing it", "on my computer", "inside this device",
                       "don't send it anywhere", "don’t use the cloud", "with no third-party access"]:
            prompt = "In notes.md, find the retry limit " + suffix
            with self.subTest(suffix=suffix):
                self.assertIsNone(self.hook(**{**self.kwargs, "user_message": prompt}))
        self.assertIsNone(self.hook(**{**self.kwargs, "user_message":
            "In notes.md, find the retry limit.\nReturn JSON using local tools only."}))
        self.locate.assert_not_called()

    def test_format_suffix_must_be_a_complete_supported_request(self):
        for suffix in ["Return JSON and compare another file", "Return JSON with answer and then compare other.md",
                       "Return JSON with answer (read another file)", "Return JSON; summarize other.md"]:
            self.assertIsNone(self.hook(**{**self.kwargs, "user_message":
                "In notes.md, find the retry limit.\n" + suffix}))
        self.locate.assert_not_called()
        for suffix in ["Return JSON", "Return only a JSON object with status (found or not_found), source (relative path), and evidence (an exact source quotation, or null when absent).",
                       "Return JSON with answer, confidence (number), and source (relative path)."]:
            self.assertEqual(prefetch.request_source("In notes.md, find the retry limit.\n" + suffix), "notes.md")
