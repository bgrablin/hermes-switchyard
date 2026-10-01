"""Multi-source coverage, immutable provenance, and hosted-boundary regressions."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hermes_switchyard import source_bundle as bundle
from hermes_switchyard import source_prefetch as prefetch


class Client:
    def __init__(self):
        self.effect = None
        self.calls = []
        self.closed = False

    def decide(self, state, questions, **kwargs):
        self.calls.append((state, questions))
        answers = {key: {"noul": 0.98 if "limit" in value["text"] else 0.01}
                   for key, value in state["passages"].items()}
        if self.effect:
            self.effect(answers)
        return {"answers": answers, "usage": {"cost": 0.001}, "request_count": 1}

    def close(self):
        self.closed = True


@unittest.skipUnless(os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"), "descriptor-relative source support")
class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "a.md").write_bytes(b"The limit is 37.\r\n\r\nBlue widgets.\r\n")
        (self.root / "b.md").write_bytes(b"The limit test passed.\n")
        self.client = Client()
        self.factory = mock.Mock(return_value=self.client)
        self.scrub = mock.patch.object(bundle.source, "redact_for_jev", side_effect=lambda x: (x, None))
        self.scrub.start()
        self.addCleanup(self.scrub.stop)

    def lookup(self, **kwargs):
        return bundle.locate_many(**(dict(root=self.root, sources=["a.md", "b.md"],
            query="Find the limit behavior", client_factory=self.factory,
            public_or_sanitized_data_ack=True) | kwargs))

    def test_exact_bytes_individual_ranges_hashes_and_keyed_questions(self):
        result = self.lookup()
        self.assertEqual(result["status"], "found")
        self.assertEqual([p["text"] for p in result["evidence"]],
                         ["The limit is 37.\r\n", "The limit test passed.\n"])
        self.assertEqual([p["source"] for p in result["evidence"]], ["a.md", "b.md"])
        self.assertTrue(all(p["start_line"] == p["end_line"] == 1 for p in result["evidence"]))
        self.assertTrue(all(len(p["sha256"]) == 64 for p in result["evidence"]))
        self.assertEqual(result["request_count"], 1)
        self.assertTrue(self.client.closed)
        state, questions = self.client.calls[0]
        self.assertEqual(set(state["passages"]), set(questions))
        for key in questions:
            self.assertIn(f"passages.{key}", questions[key]["instructions"])

    def test_duplicate_text_keeps_all_citations(self):
        (self.root / "c.md").write_bytes((self.root / "a.md").read_bytes())
        result = self.lookup(sources=["a.md", "c.md", "b.md"])
        self.assertEqual(len(result["evidence"]), 2)
        self.assertEqual(result["evidence"][0]["duplicates"][0]["source"], "c.md")

    def test_uncertain_candidate_keeps_full_native_fallback(self):
        self.client.effect = lambda answers: answers.update(p1={"noul": 0.4})
        result = self.lookup()
        self.assertEqual((result["status"], result["reason"]), ("defer", "uncertain_or_absent"))
        self.assertEqual(result["evidence"], [])
        self.assertIn("normal file tools", result["next_action"])

    def test_bad_or_missing_values_never_return_partial_evidence(self):
        for bad in [None, {}, {"noul": True}, {"noul": float("nan")}, {"noul": -1},
                    {"noul": 1.1}, {"noul": 0.9, "extra": "data"}]:
            with self.subTest(bad=bad):
                self.client.effect = lambda answers: answers.update(p2=bad)
                result = self.lookup()
                self.assertEqual(result["status"], "defer")
                self.assertEqual(result["evidence"], [])
        self.client.effect = lambda answers: answers.pop("p2")
        self.assertEqual(self.lookup()["reason"], "invalid_response")

    def test_changed_irrelevant_source_invalidates_whole_result(self):
        (self.root / "c.md").write_text("Unrelated palette.\n")
        def edit(_answers):
            (self.root / "c.md").write_text("New contradictory limit.\n")
        self.client.effect = edit
        result = self.lookup(sources=["a.md", "b.md", "c.md"])
        self.assertEqual(result["reason"], "source_changed")
        self.assertEqual(result["evidence"], [])
        self.assertEqual(result["usage"]["cost"], 0.001)

    def test_budgets_refuse_instead_of_omitting_evidence(self):
        with mock.patch.object(bundle, "MAX_EVIDENCE", 1):
            self.assertEqual(self.lookup()["reason"], "evidence_budget_exceeded")
        with mock.patch.object(bundle, "MAX_PASSAGES", 1):
            self.factory.reset_mock()
            self.assertEqual(self.lookup()["reason"], "too_many_passages")
            self.factory.assert_not_called()
        with mock.patch.object(bundle.source, "MAX_SOURCE_BYTES", 50):
            self.assertEqual(self.lookup()["reason"], "sources_too_large")

    def test_unsafe_inputs_never_construct_client(self):
        (self.root / "link.md").symlink_to(self.root / "a.md")
        for args in [dict(public_or_sanitized_data_ack=False), dict(sources=["a.md", "a.md"]),
                     dict(sources=["a.md"]), dict(sources=["a.md", "../outside.md"]),
                     dict(sources=["a.md", ".private.md"]), dict(sources=["a.md", "link.md"]),
                     dict(query="Find the limit using local tools only"), dict(query="\ud800")]:
            with self.subTest(args=args):
                self.assertEqual(self.lookup(**args)["status"], "defer")
        self.factory.assert_not_called()

    def test_missing_or_changed_redaction_refuses_before_network(self):
        for value in [(None, "missing"), ("sanitized", None)]:
            with mock.patch.object(bundle.source, "redact_for_jev", return_value=value):
                self.assertEqual(self.lookup()["status"], "defer")
        self.factory.assert_not_called()

    def test_close_over_deadline_discards_evidence(self):
        clock = [0.0]
        def close():
            clock[0] = bundle.source.DEADLINE_SECONDS + 1
        self.client.close = close
        with mock.patch.object(bundle.time, "monotonic", side_effect=lambda: clock[0]):
            result = self.lookup()
        self.assertEqual((result["status"], result["reason"]), ("defer", "deadline_exceeded"))
        self.assertEqual(result["evidence"], [])
        self.assertEqual(result["usage"]["cost"], 0.001)

    def test_provider_failure_hides_exception_text(self):
        self.client.effect = mock.Mock(side_effect=RuntimeError("private-provider-token"))
        result = self.lookup()
        self.assertEqual(result["reason"], "provider_unavailable")
        self.assertNotIn("private-provider-token", json.dumps(result))
        self.assertTrue(self.client.closed)


class BundlePrefetchTests(unittest.TestCase):
    prompt = "In `a.md`, `b.md` and `test.md`, find the limit behavior."

    def test_ordinary_prompt_and_format_line(self):
        for suffix in ["", "\nReturn only JSON with answer, evidence."]:
            self.assertEqual(prefetch.request_sources(self.prompt + suffix), ["a.md", "b.md", "test.md"])

    def test_unknown_lists_and_second_actions_stay_with_host(self):
        for prompt in ["In a.md and b.md, find the limit.", "In `a.md` and `a.md`, find x.",
                       self.prompt + " Then modify the file.", self.prompt + "\nUpload the result.",
                       "In `a.md` and `b.md`, find the limit locally.",
                       "In `a.md`, `b.md`, find the limit and rewrite it.",
                       "In `a.md`, `b.md`, find the limit.\nReturn JSON and upload it."]:
            self.assertIsNone(prefetch.request_sources(prompt), prompt)

    def test_scope_and_egress_gates_precede_multi_read(self):
        with mock.patch.object(prefetch, "locate_many") as lookup:
            hook = prefetch.build_hook(enabled=True, root="/fixture", standing_ack=True, client_factory=mock.Mock())
            args = dict(user_message=self.prompt, session_id="s", task_id="t", turn_id="r",
                        parent_session_id="", platform="cli")
            for patch in [dict(turn_id=None), dict(parent_session_id="parent"), dict(platform="cron"),
                          dict(turn_egress_policy={"decision": "deny"})]:
                self.assertIsNone(hook(**(args | patch)))
            self.assertIsNone(hook(**args))
            lookup.assert_not_called()

    def test_hook_returns_multi_evidence_without_metadata_egress(self):
        response = {"status": "found", "evidence": [{"source": "a.md", "text": "limit=37"}],
                    "request_count": 1, "next_action": "Use individual citations if sufficient."}
        with mock.patch.object(prefetch, "locate_many", return_value=response) as lookup:
            hook = prefetch.build_hook(enabled=True, root="/fixture", standing_ack=True, client_factory=mock.Mock())
            args = dict(user_message=self.prompt, session_id="s", task_id="t", turn_id="r",
                        parent_session_id="", platform="cli")
            result = hook(**args)
            self.assertEqual(json.loads(result["context"].split("\n", 1)[1])["evidence"], response["evidence"])
            self.assertEqual(lookup.call_args.kwargs["sources"], ["a.md", "b.md", "test.md"])
            self.assertNotIn("evidence", result["metadata"]["switchyard_find"])
            self.assertIsNone(hook(**args))
            self.assertEqual(lookup.call_count, 1)
