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

    def test_leading_blank_lines_use_the_recognized_request_line(self):
        for prefix in ["\n", "\r\n\r\n", " \n\t "]:
            with self.subTest(prefix=prefix):
                prompt = prefix + "In notes.md, find the retry limit.  \nReturn JSON."
                self.assertIsNotNone(self.hook(**{**self.kwargs, "user_message": prompt}))
                self.assertEqual(self.locate.call_args.kwargs["query"],
                                 "In notes.md, find the retry limit.")

    def test_unpaired_surrogates_skip_without_consuming_the_turn(self):
        for prompt in ["In notes.md, find \ud800.", "In bad\udfff.md, find retries."]:
            self.assertIsNone(self.hook(**{**self.kwargs, "user_message": prompt}))
        self.locate.assert_not_called()
        self.assertIsNotNone(self.hook(**self.kwargs))
        self.assertEqual(self.locate.call_count, 1)

    def test_missing_scope_foreground_and_unknown_platform_skip(self):
        for field, value in [("session_id", None), ("task_id", ""), ("turn_id", None),
                             ("parent_session_id", None), ("parent_session_id", "parent"),
                             ("platform", "cron"), ("platform", "unknown"), ("turn_id", "\n")]:
            with self.subTest(field=field, value=value):
                self.assertIsNone(self.hook(**{**self.kwargs, field: value}))
        self.locate.assert_not_called()

    def test_blank_or_malformed_identities_skip_without_io(self):
        for field in ("session_id", "task_id", "turn_id"):
            for value in (None, "", " ", " " * 256, "\t", "\u00a0", "\u3000", "x" * 257, 1):
                with self.subTest(field=field, value=value):
                    self.assertIsNone(self.hook(**{**self.kwargs, field: value}))
        self.locate.assert_not_called()
        self.factory.assert_not_called()

    def test_host_envelopes_never_authorize_additional_source_text(self):
        for name in ["turn_egress_policy", "egress_policy"]:
            for policy in [{"decision": "allow"}, {"decision": "deny"}, {}, "invalid"]:
                self.assertIsNone(self.hook(**{**self.kwargs, name: policy}))
        self.locate.assert_not_called()

    def test_envelope_denial_survives_duplicate_callback_without_envelope(self):
        for field in ("turn_egress_policy", "egress_policy"):
            for envelope in ({"decision": "allow"}, {"decision": "deny"}, {}, "invalid"):
                with self.subTest(field=field, envelope=envelope):
                    hook = prefetch.build_hook(enabled=True, root="/fixture", standing_ack=True,
                                               client_factory=self.factory)
                    self.assertIsNone(hook(**{**self.kwargs, field: envelope}))
                    self.assertIsNone(hook(**self.kwargs))
        self.locate.assert_not_called()
        self.factory.assert_not_called()

    def test_envelope_refusals_survive_capacity_and_replay(self):
        hook = prefetch.build_hook(enabled=True, root="/fixture", standing_ack=True,
                                   client_factory=self.factory)
        for n in range(257):
            self.assertIsNone(hook(**{**self.kwargs, "turn_id": str(n),
                                      "turn_egress_policy": {"decision": "deny"}}))
        self.assertIsNone(hook(**{**self.kwargs, "turn_id": "0"}))
        self.assertIsNone(hook(**{**self.kwargs, "turn_id": "256"}))
        self.assertIsNone(hook(**{**self.kwargs, "turn_id": "new"}))
        self.locate.assert_not_called()
        self.factory.assert_not_called()

    def test_successful_lookup_eviction_does_not_evict_refusal(self):
        hook = prefetch.build_hook(enabled=True, root="/fixture", standing_ack=True,
                                   client_factory=self.factory)
        self.assertIsNone(hook(**{**self.kwargs, "egress_policy": {"decision": "deny"}}))
        for n in range(257):
            self.assertIsNotNone(hook(**{**self.kwargs, "turn_id": str(n)}))
        before = self.locate.call_count
        self.assertIsNone(hook(**self.kwargs))
        self.assertEqual(self.locate.call_count, before)

    def test_compound_rewrite_copy_rename_inflections_skip(self):
        for action in ("rewrite", "rewrites", "rewriting", "rewritten", "rewrote", "rename",
                       "renames", "renamed", "renaming", "copy", "copies", "copied", "copying",
                       "translate", "translates", "translated", "translating", "simplify",
                       "simplifies", "simplified", "simplifying", "paraphrase", "rephrase"):
            self.assertIsNone(self.hook(**{**self.kwargs, "user_message":
                "In notes.md, find the retry limit " + action + " it in simpler language."}))
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
            self.assertIn("normal file tools", payload["next_action"])

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
                       "on this air-gapped system", "on this airgapped system", "on this air gapped system",
                       "on this air‐gapped system", "on this air‑gapped system", "on this air–gapped system",
                       "across the air gap", "while disconnected", "all data must remain here",
                       "—must not contact third parties", "must not contact third-parties",
                       "must not contact thirdparties", "cannot call services", "can't call services",
                       "mustn't call services", "shouldn’t call services",
                       "on-device only", "on device only", "ondevice", "on-box", "on premises",
                       "using this laptop", "inside our perimeter", "strictly in-house",
                       "keeping everything here", "using internal resources exclusively",
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

    def test_additional_action_or_file_clauses_stay_with_host(self):
        for prompt in ["In notes.md, find the retry limit translate it to French.",
                       "In notes.md, find the retry limit, translate it to French.",
                       "In notes.md, find the retry limit: translate it to French.",
                       "In notes.md, find the retry limit — translate it to French.",
                       "In notes.md, find the retry limit - translate it to French.",
                       "In notes.md, find the retry limit and compare it with config.md.",
                       "In notes.md, find the retry limit; summarize config.md.",
                       "In notes.md, find the retry limit. Summarize config.md.",
                       "Find the retry limit and calculate the delay in notes.md.",
                       "In notes.md, find the retry limit … summarize config.md."]:
            self.assertIsNone(self.hook(**{**self.kwargs, "user_message": prompt}))
        self.locate.assert_not_called()



if __name__ == "__main__":
    unittest.main()
