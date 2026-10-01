"""Observable source, failure, and opt-in contracts; no provider traffic."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hermes_switchyard import source_find as finder


class Client:
    def __init__(self, effect=None):
        self.calls = []
        self.effect = effect
        self.closed = False

    def decide(self, state, questions, **kwargs):
        self.calls.append((state, questions))
        ids = list(questions["where"]["criteria"])
        response = {"model": "typesafe/jev-1.13", "request_id": "fixture",
                    "usage": {"cost": 0.001}, "latency_ms": 1,
                    "answers": {"where": {"choice": ids[0], "confidence": 0.95,
                    "probabilities": {k: (0.95 if k == ids[0] else 0.05 / (len(ids) - 1)) for k in ids}},
                    "exists": {"noul": 0.95}}}
        if self.effect:
            return self.effect(response)
        return response

    def close(self):
        self.closed = True


@unittest.skipUnless(os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"), "descriptor-relative source support")
class EvidenceFindTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "source.md"
        self.raw = b"Retry twice.\r\n\r\nTimeout after five seconds.\r\n"
        self.path.write_bytes(self.raw)
        self.scrub = mock.patch.object(finder, "redact_for_jev", side_effect=lambda text: (text, None))
        self.scrub.start()
        self.addCleanup(self.scrub.stop)
        self.client = Client()
        self.factory = mock.Mock(return_value=self.client)

    def run_find(self, **kwargs):
        args = dict(root=self.root, source="source.md", query="How many retries?",
                    client_factory=self.factory, public_or_sanitized_data_ack=True)
        args.update(kwargs)
        return finder.locate(**args)

    def test_exact_bytes_and_line_citation(self):
        out = self.run_find()
        self.assertEqual(out["status"], "found")
        self.assertEqual(out["evidence"], "Retry twice.\r\n")
        self.assertEqual((out["start_line"], out["end_line"]), (1, 1))
        self.assertEqual(out["accounting"], "provider_reported")
        self.assertEqual(out["request_count"], 1)
        self.assertTrue(self.client.closed)

    def test_late_verification_defers_and_retains_provider_accounting(self):
        clock = [0.0]
        read = finder.read_source
        calls = []
        def verification(*args):
            value = read(*args)
            calls.append(True)
            if len(calls) == 2:
                clock[0] = finder.DEADLINE_SECONDS + 0.1
            return value
        with mock.patch.object(finder.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(finder, "read_source", side_effect=verification):
            out = self.run_find()
        self.assertEqual((out["status"], out["reason"]), ("defer", "deadline_exceeded"))
        self.assertIsNone(out["evidence"])
        self.assertEqual(out["usage"]["cost"], 0.001)
        self.assertEqual(out["request_count"], 1)

    def test_expired_budget_before_dispatch_does_not_count_a_request(self):
        clock = [0.0]
        def slow_factory():
            clock[0] = finder.DEADLINE_SECONDS + 0.1
            return self.client
        self.factory.side_effect = slow_factory
        with mock.patch.object(finder.time, "monotonic", side_effect=lambda: clock[0]):
            out = self.run_find()
        self.assertEqual(out["reason"], "deadline_exceeded")
        self.assertEqual(out["request_count"], 0)
        self.assertEqual(out["accounting"], "no_request")
        self.assertEqual(self.client.calls, [])

    def test_late_cleanup_cannot_publish_accepted_evidence(self):
        clock = [0.0]
        def close():
            clock[0] = finder.DEADLINE_SECONDS + 0.1
        self.client.close = close
        with mock.patch.object(finder.time, "monotonic", side_effect=lambda: clock[0]):
            out = self.run_find()
        self.assertEqual((out["status"], out["reason"]), ("defer", "deadline_exceeded"))
        self.assertIsNone(out["evidence"])
        self.assertEqual(out["usage"]["cost"], 0.001)

    def test_source_changed_during_inference_defers_with_accounting(self):
        def mutate(response):
            self.path.write_text("Different instructions.")
            return response
        self.client.effect = mutate
        out = self.run_find()
        self.assertEqual((out["status"], out["reason"]), ("defer", "source_changed"))
        self.assertIsNone(out["evidence"])
        self.assertEqual(out["usage"]["cost"], 0.001)

    def test_same_content_replacement_is_detected(self):
        def mutate(response):
            replacement = self.root / "other.md"
            replacement.write_bytes(self.raw)
            replacement.replace(self.path)
            return response
        self.client.effect = mutate
        self.assertEqual(self.run_find()["reason"], "source_changed")

    def test_post_inference_symlink_rejected(self):
        def mutate(response):
            self.path.unlink()
            self.path.symlink_to(self.root / "other.md")
            return response
        self.client.effect = mutate
        self.assertEqual(self.run_find()["reason"], "source_unavailable")

    def test_scope_and_path_failures_make_no_request(self):
        for source in ["../source.md", "/source.md", ".env", "./source.md", "dir/../source.md", "dir//source.md", "dir\\source.md", "missing", "bad\nname"]:
            with self.subTest(source=source):
                self.assertEqual(self.run_find(source=source)["status"], "defer")
        self.factory.assert_not_called()

    def test_symlink_parent_and_leaf_rejected(self):
        (self.root / "link.md").symlink_to(self.path)
        (self.root / "dir").symlink_to(self.root, target_is_directory=True)
        for source in ["link.md", "dir/source.md"]:
            self.assertEqual(self.run_find(source=source)["reason"], "source_unavailable")
        self.factory.assert_not_called()

    def test_nonregular_file_does_not_block(self):
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertEqual(self.run_find()["reason"], "not_regular_file")
        self.factory.assert_not_called()

    def test_missing_ack_and_invalid_query_do_not_read(self):
        with mock.patch.object(finder, "read_source") as read:
            for kwargs in [{"public_or_sanitized_data_ack": False}, {"query": ""}, {"query": "x" * 1201}, {"query": None}]:
                self.assertEqual(self.run_find(**kwargs)["status"], "defer")
            read.assert_not_called()
        self.factory.assert_not_called()

    def test_local_only_query_defers_before_read_or_provider(self):
        with mock.patch.object(finder, "read_source") as read:
            for query in ["Find retries offline", "Find retries without using an external service",
                          "Find retries using local tools only", "Find retries on my computer",
                          "Find retries on this air-gapped system", "Find retries while disconnected",
                          "Find retries on-device only", "Find retries on device only",
                          "Find retries ondevice", "Find retries on-box", "Find retries on premises",
                          "Find retries using this laptop", "Find retries inside our perimeter",
                          "Find retries strictly in-house", "Find retries using internal resources exclusively"]:
                self.assertEqual(self.run_find(query=query)["reason"], "local_handling_required")
            read.assert_not_called()
        self.factory.assert_not_called()

    def test_size_encoding_and_candidate_limits_do_not_call_provider(self):
        for raw, reason in [(b"x" * 80001, "source_too_large"), (b"\xff", "invalid_encoding"),
                            (b"\x00", "binary_source"), (b"x" * 2401, "line_too_large"),
                            (b"one\n\n" * 241, "too_many_passages"), (b"\n\n", "empty_source")]:
            with self.subTest(reason=reason):
                self.path.write_bytes(raw)
                self.assertEqual(self.run_find()["reason"], reason)
        self.factory.assert_not_called()

    def test_scrubber_failure_or_modified_source_defers(self):
        for value, reason in [((None, "unavailable"), "redaction_unavailable"), (("masked", None), "source_requires_sanitization")]:
            with mock.patch.object(finder, "redact_for_jev", return_value=value):
                self.assertEqual(self.run_find()["reason"], reason)
        self.factory.assert_not_called()

    def test_invalid_and_uncertain_responses_never_render(self):
        def edits(response):
            options = []
            for key, value in [("choice", "unoffered"), ("choice", []), ("confidence", True), ("confidence", float("nan")), ("probabilities", {})]:
                item = copy.deepcopy(response)
                item["answers"]["where"][key] = value
                options.append(item)
            item = copy.deepcopy(response)
            item["answers"]["exists"]["noul"] = False
            options.append(item)
            return options
        base = Client().decide({},{"where": {"criteria": {"L1-1": "", "L3-3": "", "NONE": ""}}})
        for response in edits(base) + [{}, None]:
            self.client.effect = lambda _, response=response: response
            out = self.run_find()
            self.assertEqual(out["status"], "defer")
            self.assertIsNone(out["evidence"])
        self.client.effect = lambda response: {**response, "answers": {**response["answers"], "exists": {"noul": 0.5}}}
        self.assertEqual(self.run_find()["reason"], "uncertain_or_conflicting")

    def test_provider_error_is_redacted_and_cost_unknown(self):
        self.client.effect = mock.Mock(side_effect=TimeoutError("provider private diagnostic"))
        out = self.run_find()
        self.assertEqual(out["accounting"], "unknown")
        self.assertNotIn("private diagnostic", json.dumps(out))
        self.assertTrue(self.client.closed)

    def test_negative_requires_consistent_confident_response(self):
        def negative(response):
            where = response["answers"]["where"]
            where.update(choice="NONE", probabilities={k: float(k == "NONE") for k in where["probabilities"]})
            response["answers"]["exists"]["noul"] = 0.02
            return response
        self.client.effect = negative
        self.assertEqual(self.run_find()["status"], "not_found")
        def uncertain(response):
            response = negative(response)
            response["answers"]["where"]["confidence"] = 0.1
            return response
        self.client.effect = uncertain
        self.assertEqual(self.run_find()["status"], "defer")

    def test_repeated_reads_never_reuse_old_result(self):
        self.run_find()
        self.path.write_text("Retry three times.\n")
        out = self.run_find()
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(out["evidence"], "Retry three times.\n")

    def test_all_nonblank_lines_covered_and_unicode_preserved(self):
        raw = ("αβ\n" * 101).encode()
        for source in ["source.py", "source.md"]:
            chunks = finder.build_passages(raw, source)
            covered = set()
            for passage in chunks:
                covered.update(range(passage["start_line"], passage["end_line"] + 1))
                self.assertEqual(passage["text"], "".join(raw.decode().splitlines(keepends=True)[passage["start_line"] - 1:passage["end_line"]]))
            self.assertEqual(covered, set(range(1, 102)))
            self.assertTrue(all(p["end_line"] - p["start_line"] < finder.MAX_PASSAGE_LINES for p in chunks))

    def test_short_definition_crossing_window_is_whole_in_a_passage(self):
        body = "def retry(attempts):\n    if attempts > 3:\n        return False\n    return True\n"
        raw = ("# filler\n" * 22 + body).encode()
        self.assertTrue(any(body in p["text"] for p in finder.build_passages(raw, "retry.py")))


class WiringTests(unittest.TestCase):
    def context(self, settings):
        from hermes_switchyard import register
        class Context:
            def __init__(self):
                self.tools = {}
                self.sections = {}
            def get_config(self, key, default=None):
                return settings.get(key, default)
            def register_tool(self, **kwargs):
                self.tools[kwargs["name"]] = kwargs
            def register_system_prompt_section(self, name, text, **kwargs):
                self.sections[name] = text
        ctx = Context()
        with mock.patch("hermes_switchyard._secret", side_effect=AssertionError("no credential access")):
            register(ctx)
        return ctx

    def test_default_has_no_exposure_or_routing_prompt(self):
        ctx = self.context({})
        tool = ctx.tools["switchyard_find"]
        self.assertFalse(tool["check_fn"]())
        self.assertNotIn("hermes-switchyard.find", ctx.sections)
        self.assertEqual(json.loads(tool["handler"]({}))["reason"], "feature_disabled")

    def test_enabled_natural_language_guidance_and_runtime_guard(self):
        with tempfile.TemporaryDirectory() as root:
            ctx = self.context({"evidence_finder_enabled": True, "evidence_finder_root": root})
            self.assertTrue(ctx.tools["switchyard_find"]["check_fn"]())
            self.assertIn("natural-language", ctx.sections["hermes-switchyard.find"])
            self.assertNotIn(root, ctx.sections["hermes-switchyard.find"])
            self.assertIn("local-only", ctx.sections["hermes-switchyard.find"])
            out = json.loads(ctx.tools["switchyard_find"]["handler"]({"source": "x", "query": "y", "public_or_sanitized_data_ack": False}))
            self.assertEqual(out["reason"], "ack_required")

    def test_relative_root_is_not_advertised(self):
        ctx = self.context({"evidence_finder_enabled": True, "evidence_finder_root": "relative/source"})
        self.assertFalse(ctx.tools["switchyard_find"]["check_fn"]())
        self.assertNotIn("hermes-switchyard.find", ctx.sections)

    def test_invalid_provider_has_no_routing_prompt(self):
        ctx = self.context({"evidence_finder_enabled": True, "evidence_finder_root": "/fixture",
                            "jev_provider": "not-a-provider"})
        self.assertFalse(ctx.tools["switchyard_find"]["check_fn"]())
        self.assertNotIn("hermes-switchyard.find", ctx.sections)

    def test_standing_denial_cannot_be_overridden_by_model(self):
        ctx = self.context({"evidence_finder_enabled": True, "evidence_finder_root": "/fixture", "public_or_sanitized_data_ack": False})
        out = json.loads(ctx.tools["switchyard_find"]["handler"]({"source": "x", "query": "y", "public_or_sanitized_data_ack": True}))
        self.assertEqual(out["reason"], "ack_required")

    def test_unsupported_filesystem_defers(self):
        with mock.patch.object(finder.os, "supports_dir_fd", set()):
            with self.assertRaisesRegex(finder.SourceError, "unsupported_filesystem"):
                finder.read_source("/fixture", "source.md")
