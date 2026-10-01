"""Unit tests for optional local exact-duplicate tool-round gate (C2)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from hermes_switchyard.local_duplicate_gate import (
    ARGS_STABLE_READ_TOOLS,
    build_post_tool_call_hook,
    build_tool_execution_middleware,
    canonical_args,
    decide_local_duplicate,
    fingerprint_for,
    gate_counters,
    observation_identity,
    record_tool_outcome,
    register_local_duplicate_gate,
    reset_store_for_tests,
)


class FingerprintHelpersTests(unittest.TestCase):
    def test_canonical_args_sorts_keys(self):
        self.assertEqual(
            canonical_args({"b": 1, "a": 2}),
            '{"a":2,"b":1}',
        )

    def test_args_stable_skill_view_identity(self):
        obs = observation_identity("skill_view", {"name": "demo-skill"})
        self.assertIsNotNone(obs)
        assert obs is not None
        self.assertTrue(obs.startswith("args_stable:"))

    def test_missing_obs_on_ordinary_read_fail_opens(self):
        self.assertIsNone(observation_identity("read_file", {"path": "README.md"}))
        self.assertIsNone(fingerprint_for("read_file", {"path": "README.md"}))

    def test_uncanonicalizable_arguments_fail_open_without_shared_keys(self):
        cyclic = {}
        cyclic["self"] = cyclic
        for args in (cyclic, {1: "integer key"}, {"nested": {1: "integer key"}},
                     {"opaque": object()}, {"tuple": (1,)}, {"nan": float("nan")},
                     [1], "[1]"):
            with self.subTest(kind=type(args).__name__):
                self.assertIsNone(canonical_args(args))
                self.assertIsNone(fingerprint_for("skill_view", args))
                self.assertIsNone(fingerprint_for("read_file", args, observation_id="known"))
                self.assertFalse(record_tool_outcome(enabled=True, tool_name="skill_view",
                                 args=args, result="must not cache", status="ok", session_id="bad")["recorded"])

    def test_explicit_observation_id_used(self):
        key = fingerprint_for(
            "browser_snapshot",
            {"observation_id": "obs-abc", "url": "https://example.com"},
        )
        self.assertIsNotNone(key)
        assert key is not None
        self.assertTrue(key.startswith("read:"))

    def test_non_read_fingerprint_none(self):
        self.assertIsNone(fingerprint_for("write_file", {"path": "x", "observation_id": "o"}))
        self.assertIsNone(fingerprint_for("terminal", {"command": "ls", "observation_id": "o"}))

    def test_args_stable_set_includes_skill_view(self):
        self.assertIn("skill_view", ARGS_STABLE_READ_TOOLS)

    def test_live_state_integrations_not_args_stable(self):
        # HA / Kanban reads can change without a write tool Hermes classifies as write.
        for name in ("ha_list_entities", "ha_list_services", "kanban_list", "kanban_show"):
            self.assertNotIn(name, ARGS_STABLE_READ_TOOLS)
            self.assertIsNone(observation_identity(name, {}))
            self.assertIsNone(fingerprint_for(name, {}))


class DecideAndRecordTests(unittest.TestCase):
    def setUp(self):
        reset_store_for_tests()

    def tearDown(self):
        reset_store_for_tests()

    def test_flag_off_never_reuses(self):
        record_tool_outcome(
            enabled=True,
            tool_name="skill_view",
            args={"name": "demo"},
            result='{"ok": true, "body": "skill text"}',
            status="ok",
            session_id="s1",
        )
        decision = decide_local_duplicate(
            enabled=False,
            tool_name="skill_view",
            args={"name": "demo"},
            session_id="s1",
        )
        self.assertEqual(decision["action"], "dispatch")
        self.assertEqual(decision["reason"], "flag_off")

    def test_exact_duplicate_skill_view_reuses(self):
        body = '{"name":"demo","content":"hello skill"}'
        record_tool_outcome(
            enabled=True,
            tool_name="skill_view",
            args={"name": "demo"},
            result=body,
            status="ok",
            session_id="s1",
        )
        decision = decide_local_duplicate(
            enabled=True,
            tool_name="skill_view",
            args={"name": "demo"},
            session_id="s1",
        )
        self.assertEqual(decision["action"], "reuse")
        self.assertEqual(decision["reason"], "local_duplicate")
        self.assertEqual(decision["cached_result"], body)

    def test_different_args_dispatch(self):
        record_tool_outcome(
            enabled=True,
            tool_name="skill_view",
            args={"name": "a"},
            result="A",
            status="ok",
            session_id="s1",
        )
        decision = decide_local_duplicate(
            enabled=True,
            tool_name="skill_view",
            args={"name": "b"},
            session_id="s1",
        )
        self.assertEqual(decision["action"], "dispatch")
        self.assertEqual(decision["reason"], "no_prior_success")

    def test_sessions_isolated(self):
        record_tool_outcome(
            enabled=True,
            tool_name="skill_view",
            args={"name": "demo"},
            result="from-s1",
            status="ok",
            session_id="s1",
        )
        decision = decide_local_duplicate(
            enabled=True,
            tool_name="skill_view",
            args={"name": "demo"},
            session_id="s2",
        )
        self.assertEqual(decision["action"], "dispatch")

    def test_mutation_clears_session(self):
        record_tool_outcome(
            enabled=True,
            tool_name="skill_view",
            args={"name": "demo"},
            result="body",
            status="ok",
            session_id="s1",
        )
        record_tool_outcome(
            enabled=True,
            tool_name="write_file",
            args={"path": "x"},
            result="ok",
            status="ok",
            session_id="s1",
        )
        decision = decide_local_duplicate(
            enabled=True,
            tool_name="skill_view",
            args={"name": "demo"},
            session_id="s1",
        )
        self.assertEqual(decision["action"], "dispatch")

    def test_failed_read_invalidates_and_never_skips(self):
        args = {"name": "demo"}
        record_tool_outcome(
            enabled=True,
            tool_name="skill_view",
            args=args,
            result="good",
            status="ok",
            session_id="s1",
        )
        # A later failure for the same key drops the cache.
        record_tool_outcome(
            enabled=True,
            tool_name="skill_view",
            args=args,
            result='{"error":"not found"}',
            status="error",
            error_message="not found",
            session_id="s1",
        )
        decision = decide_local_duplicate(
            enabled=True,
            tool_name="skill_view",
            args=args,
            session_id="s1",
        )
        self.assertEqual(decision["action"], "dispatch")

        # Failed outcome itself must never be treated as reusable.
        mid = build_tool_execution_middleware(enabled=True)
        calls = []

        def next_call(a):
            calls.append(a)
            return "fresh"

        out = mid(tool_name="skill_view", args=args, next_call=next_call, session_id="s1")
        self.assertEqual(out, "fresh")
        self.assertEqual(len(calls), 1)

    def test_non_read_never_reuses(self):
        decision = decide_local_duplicate(
            enabled=True,
            tool_name="terminal",
            args={"command": "ls", "observation_id": "x"},
            session_id="s1",
        )
        self.assertEqual(decision["action"], "dispatch")
        self.assertEqual(decision["reason"], "non_read")

    def test_browser_snapshot_requires_obs_id(self):
        # Without observation identity, fail-open (do not args-only skip).
        record_tool_outcome(
            enabled=True,
            tool_name="browser_snapshot",
            args={"url": "https://example.com"},
            result='{"text":"Example Domain"}',
            status="ok",
            session_id="s1",
        )
        decision = decide_local_duplicate(
            enabled=True,
            tool_name="browser_snapshot",
            args={"url": "https://example.com"},
            session_id="s1",
        )
        self.assertEqual(decision["action"], "dispatch")
        self.assertEqual(decision["reason"], "missing_observation_identity")

    def test_browser_snapshot_exact_obs_reuses(self):
        args = {"url": "https://example.com", "observation_id": "doc-1"}
        body = '{"url":"https://example.com","text":"Example Domain"}'
        record_tool_outcome(
            enabled=True,
            tool_name="browser_snapshot",
            args=args,
            result=body,
            status="ok",
            session_id="s1",
        )
        decision = decide_local_duplicate(
            enabled=True,
            tool_name="browser_snapshot",
            args=args,
            session_id="s1",
        )
        self.assertEqual(decision["action"], "reuse")
        self.assertEqual(decision["cached_result"], body)


class MiddlewareHookTests(unittest.TestCase):
    def setUp(self):
        reset_store_for_tests()

    def tearDown(self):
        reset_store_for_tests()

    def test_middleware_reuses_without_next_call(self):
        record_tool_outcome(
            enabled=True,
            tool_name="skill_view",
            args={"name": "demo"},
            result="cached-body",
            status="ok",
            session_id="s1",
        )
        mid = build_tool_execution_middleware(enabled=True)
        calls = []

        def next_call(a):
            calls.append(a)
            return "should-not-run"

        out = mid(
            tool_name="skill_view",
            args={"name": "demo"},
            next_call=next_call,
            session_id="s1",
        )
        self.assertEqual(out, "cached-body")
        self.assertEqual(calls, [])
        self.assertGreaterEqual(gate_counters()["reused"], 1)

    def test_middleware_flag_off_always_dispatches(self):
        record_tool_outcome(
            enabled=True,
            tool_name="skill_view",
            args={"name": "demo"},
            result="cached-body",
            status="ok",
            session_id="s1",
        )
        mid = build_tool_execution_middleware(enabled=False)
        out = mid(
            tool_name="skill_view",
            args={"name": "demo"},
            next_call=lambda a: "live",
            session_id="s1",
        )
        self.assertEqual(out, "live")

    def test_post_tool_hook_records(self):
        hook = build_post_tool_call_hook(enabled=True)
        hook(
            tool_name="skill_view",
            args={"name": "demo"},
            result="from-hook",
            session_id="s1",
            status="ok",
        )
        decision = decide_local_duplicate(
            enabled=True,
            tool_name="skill_view",
            args={"name": "demo"},
            session_id="s1",
        )
        self.assertEqual(decision["action"], "reuse")
        self.assertEqual(decision["cached_result"], "from-hook")

    def test_register_wires_middleware_and_hook(self):
        registered = []

        def register_middleware(kind, cb):
            registered.append(("mw", kind, cb))

        def register_hook(name, cb):
            registered.append(("hook", name, cb))

        ctx = SimpleNamespace(
            register_middleware=register_middleware,
            register_hook=register_hook,
        )
        status = register_local_duplicate_gate(ctx, enabled=False)
        self.assertTrue(status["tool_execution_registered"])
        self.assertTrue(status["post_tool_call_registered"])
        self.assertFalse(status["enabled"])
        self.assertEqual(status["flag"], "local_duplicate_tool_gate")
        kinds = {(t, k) for t, k, _ in registered}
        self.assertIn(("mw", "tool_execution"), kinds)
        self.assertIn(("hook", "post_tool_call"), kinds)

    def test_register_missing_seams_fail_open(self):
        status = register_local_duplicate_gate(SimpleNamespace(), enabled=True)
        self.assertFalse(status["tool_execution_registered"])
        self.assertFalse(status["post_tool_call_registered"])
        self.assertIn("unavailable", status.get("reason", ""))


class OfflineScorerShapeTests(unittest.TestCase):
    """Mirror Smoke 2 fingerprint shape: exact skip count on a toy trace."""

    def setUp(self):
        reset_store_for_tests()

    def tearDown(self):
        reset_store_for_tests()

    def test_toy_trace_local_duplicate_count(self):
        # Synthetic public trace — no host paths.
        trace = [
            {"tool": "skill_view", "args": {"name": "alpha"}, "ok": True, "result": "A1"},
            {"tool": "skill_view", "args": {"name": "alpha"}, "ok": True, "result": "A1"},
            {"tool": "skill_view", "args": {"name": "beta"}, "ok": True, "result": "B1"},
            {"tool": "write_file", "args": {"path": "out.txt"}, "ok": True, "result": "ok"},
            {"tool": "skill_view", "args": {"name": "alpha"}, "ok": True, "result": "A2"},
            {
                "tool": "browser_snapshot",
                "args": {"url": "https://example.com", "observation_id": "o1"},
                "ok": True,
                "result": "page1",
            },
            {
                "tool": "browser_snapshot",
                "args": {"url": "https://example.com", "observation_id": "o1"},
                "ok": True,
                "result": "page1",
            },
            {
                "tool": "browser_snapshot",
                "args": {"url": "https://example.com", "observation_id": "o2"},
                "ok": True,
                "result": "page2",
            },
        ]
        skipped = 0
        must_dispatch = 0
        session = "toy"
        for step in trace:
            decision = decide_local_duplicate(
                enabled=True,
                tool_name=step["tool"],
                args=step["args"],
                session_id=session,
            )
            if decision["action"] == "reuse":
                skipped += 1
            else:
                must_dispatch += 1
            record_tool_outcome(
                enabled=True,
                tool_name=step["tool"],
                args=step["args"],
                result=step["result"],
                status="ok" if step["ok"] else "error",
                ok=step["ok"],
                session_id=session,
            )
        # skill_view alpha exact dupe (1) + browser_snapshot o1 exact dupe (1) = 2
        # write clears session so later alpha redispatches; o2 is unique.
        self.assertEqual(skipped, 2)
        self.assertEqual(must_dispatch, len(trace) - skipped)


class CacheBoundaryTests(unittest.TestCase):
    def setUp(self):
        reset_store_for_tests()

    def tearDown(self):
        reset_store_for_tests()

    def record(self, **overrides):
        values = dict(enabled=True, tool_name="skill_view", args={"name": "demo"},
                      result="complete result", status="ok", session_id="s1")
        values.update(overrides)
        return record_tool_outcome(**values)

    def decide(self, **overrides):
        values = dict(enabled=True, tool_name="skill_view", args={"name": "demo"}, session_id="s1")
        values.update(overrides)
        return decide_local_duplicate(**values)

    def test_missing_scope_never_uses_shared_default_and_task_scope_is_distinct(self):
        self.assertFalse(self.record(session_id=None)["recorded"])
        self.assertEqual(self.decide(session_id=None)["action"], "dispatch")
        self.record()
        self.assertEqual(self.decide(session_id=None, task_id="s1")["action"], "dispatch")

    def test_oversized_or_nonstring_results_invalidate_without_truncation(self):
        from hermes_switchyard.local_duplicate_gate import DEFAULT_MAX_RESULT_CHARS
        for result in ("x" * (DEFAULT_MAX_RESULT_CHARS + 1), {"ok": True}, None):
            self.record()
            self.assertFalse(self.record(result=result)["recorded"])
            self.assertEqual(self.decide()["action"], "dispatch")
        body = "x" * DEFAULT_MAX_RESULT_CHARS
        self.assertTrue(self.record(result=body)["recorded"])
        self.assertEqual(self.decide()["cached_result"], body)

    def test_combined_hard_bounds_even_with_larger_caller_limits(self):
        from hermes_switchyard import local_duplicate_gate as gate
        payload = "x" * gate.DEFAULT_MAX_RESULT_CHARS
        for session in range(gate.DEFAULT_MAX_SESSIONS + 2):
            for key in range(gate.DEFAULT_MAX_KEYS_PER_SESSION + 2):
                self.record(session_id=str(session), args={"name": str(key)}, result=payload,
                            max_sessions=1000, max_keys_per_session=1000, max_result_chars=10**9)
        total = sum(len(value) for session in gate._SESSION_STORE.values() for value in session.values())
        self.assertEqual(total, 4 * 1024 * 1024)
        self.assertEqual(self.decide(session_id="0", args={"name": "0"})["action"], "dispatch")
        self.assertFalse(self.record(result=payload + "x", max_result_chars=10**9)["recorded"])

    def test_cache_hits_refresh_entry_and_session_recency(self):
        self.record(args={"name": "a"}, max_keys_per_session=2, max_sessions=2)
        self.record(args={"name": "b"}, max_keys_per_session=2, max_sessions=2)
        self.decide(args={"name": "a"})
        self.record(args={"name": "c"}, max_keys_per_session=2, max_sessions=2)
        self.assertEqual(self.decide(args={"name": "b"})["action"], "dispatch")
        self.record(session_id="s2", max_sessions=2)
        self.decide(args={"name": "a"})
        self.record(session_id="s3", max_sessions=2)
        self.assertEqual(self.decide(session_id="s2")["action"], "dispatch")

    def test_full_tool_identity_and_resource_ids_do_not_alias_observations(self):
        args = {"observation_id": "snapshot-1"}
        self.record(tool_name="mcp__one__read_file", args=args)
        self.assertEqual(self.decide(tool_name="mcp__two__read_file", args=args)["action"], "dispatch")
        for field in ("page_id", "document_id"):
            self.assertIsNone(fingerprint_for("read_file", {field: "resource-1"}))
        self.assertIsNone(fingerprint_for("mcp__other__skill_view", {"name": "demo"}))

    def test_unknown_mutations_clear_and_hook_failure_flags_invalidate(self):
        self.record()
        self.record(tool_name="ha_call_service")
        self.assertEqual(self.decide()["action"], "dispatch")
        self.record()
        hook = build_post_tool_call_hook(enabled=True)
        hook(tool_name="skill_view", args={"name": "demo"}, result="failure",
             session_id="s1", ok=False)
        self.assertEqual(self.decide()["action"], "dispatch")


class NativeExecutionChainTests(unittest.TestCase):
    def test_real_hermes_chain_reuses_read_but_dispatches_after_mutation(self):
        from unittest.mock import patch
        import importlib
        try:
            from hermes_cli.middleware import run_tool_execution_middleware
            plugins = importlib.import_module("hermes_cli.plugins")
        except ImportError:
            self.skipTest("native Hermes is exercised by compatibility CI")
        reset_store_for_tests()
        self.addCleanup(reset_store_for_tests)
        callback = build_tool_execution_middleware(enabled=True)
        hook = build_post_tool_call_hook(enabled=True)
        manager = SimpleNamespace(
            _middleware={"tool_execution": [callback]},
            _report_hook_failure=lambda *args, **kwargs: self.fail("middleware failed"),
        )
        calls = []
        def dispatch(args):
            calls.append(args)
            return "complete live result"
        def run(tool, args):
            result = run_tool_execution_middleware(tool, args, dispatch, session_id="native-test", task_id="task")
            hook(tool_name=tool, args=args, result=result, session_id="native-test", task_id="task", status="ok")
            return result
        with patch.object(plugins, "get_plugin_manager", return_value=manager), \
                patch.object(plugins, "_delivery_manager", return_value=manager, create=True):
            self.assertEqual(run("skill_view", {"name": "demo"}), "complete live result")
            self.assertEqual(run("skill_view", {"name": "demo"}), "complete live result")
            self.assertEqual(len(calls), 1)
            run("write_file", {"path": "demo"})
            run("skill_view", {"name": "demo"})
            self.assertEqual(len(calls), 3)
            run("browser_snapshot", {"url": "https://example.com"})
            run("browser_snapshot", {"url": "https://example.com"})
            self.assertEqual(len(calls), 5)


if __name__ == "__main__":
    unittest.main()
