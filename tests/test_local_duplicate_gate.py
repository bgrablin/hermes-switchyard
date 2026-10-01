"""C2 boundary regressions: missing evidence must perform a fresh read."""
import hashlib
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from hermes_switchyard import local_duplicate_gate as g


def evidence(body="complete", source="server/account/workspace/resource", revision="v1"):
    return g.ReadEvidence(source, revision, hashlib.sha256(body.encode()).hexdigest(), True, True)


class SafeguardTests(unittest.TestCase):
    def setUp(self):
        g.reset_store_for_tests()
        self.args = {"snapshot_id": "v1"}
        self.scope = {"session_id": "s1", "task_id": "t1"}
        self.ev = evidence()
        self.mid = g.build_tool_execution_middleware(enabled=True)
        self.calls = []

    def run_read(self, *, body="complete", name="browser_snapshot", args=None, scope=None, provider=None):
        def fresh(a):
            self.calls.append(a)
            return body
        return self.mid(tool_name=name, args=self.args if args is None else args,
                        **(self.scope if scope is None else scope), next_call=fresh,
                        reuse_evidence_provider=provider or (lambda n, a: self.ev))

    def test_same_scope_exact_reuses_complete(self):
        self.assertEqual(self.run_read(), "complete")
        self.assertEqual(self.run_read(body="must not dispatch"), "complete")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(g.gate_counters()["reused"], 1)

    def test_sibling_tasks_and_sessions_are_isolated(self):
        self.run_read()
        for scope in ({"session_id": "s1", "task_id": "t2"},
                      {"session_id": "s2", "task_id": "t1"}):
            self.run_read(scope=scope)
        self.assertEqual(len(self.calls), 3)

    def test_both_scope_dimensions_are_mandatory_and_exact(self):
        self.run_read()
        for scope in ({}, {"session_id": "s1"}, {"task_id": "t1"},
                      {"session_id": "s1", "task_id": ""},
                      {"session_id": "s1", "task_id": " t1"}):
            self.run_read(scope=scope)
        self.assertEqual(len(self.calls), 6)

    def test_full_server_and_source_identity_are_mandatory(self):
        self.run_read(name="mcp__one__read_file")
        self.run_read(name="mcp__two__read_file")
        self.ev = replace(self.ev, source_identity="other/account/workspace/resource")
        self.run_read(name="mcp__one__read_file")
        self.assertEqual(len(self.calls), 3)
        self.assertIsNone(g.fingerprint_for("read_file", self.args,
                                           evidence=replace(self.ev, source_identity="")))

    def test_argument_ids_and_catalog_names_are_not_freshness(self):
        for name, args in (("browser_snapshot", {"page_id": "p"}),
                           ("browser_snapshot", {"snapshot_id": "v1"}),
                           ("skill_view", {"name": "demo"}),
                           ("skills_list", {})):
            for _ in range(2):
                self.run_read(name=name, args=args, provider=lambda n, a: None)
            self.assertIsNone(g.fingerprint_for(name, args))
        self.assertEqual(len(self.calls), 8)
        self.assertFalse(g.ARGS_STABLE_READ_TOOLS)

    def test_changed_revision_and_content_always_dispatch(self):
        self.run_read()
        self.ev = replace(self.ev, revision="v2")
        self.run_read()
        self.ev = evidence("changed", revision="v2")
        self.assertEqual(self.run_read(body="changed"), "changed")
        self.assertEqual(len(self.calls), 3)

    def test_unknown_incomplete_or_untrusted_evidence_dispatches(self):
        self.run_read()
        invalid = (None, {}, {"complete": True}, replace(self.ev, complete=False),
                   replace(self.ev, read_only=False), replace(self.ev, revision=""),
                   replace(self.ev, result_sha256="invalid"))
        for ev in invalid:
            self.run_read(provider=lambda n, a, ev=ev: ev)
        self.assertEqual(len(self.calls), 1 + len(invalid))

    def test_oversized_or_wrong_digest_results_never_truncate_or_cache(self):
        for body in ("x" * (g.DEFAULT_MAX_RESULT_CHARS + 1), "partial"):
            self.ev = evidence(body if body != "partial" else "partial plus tail")
            self.assertEqual(self.run_read(body=body), body)
            self.assertEqual(self.run_read(body=body), body)
        self.assertEqual(len(self.calls), 4)
        self.ev = evidence("x" * g.DEFAULT_MAX_RESULT_CHARS)
        self.run_read(body="x" * g.DEFAULT_MAX_RESULT_CHARS)
        self.assertEqual(len(self.run_read()), g.DEFAULT_MAX_RESULT_CHARS)
        self.assertEqual(len(self.calls), 5)

    def test_nonstring_results_do_not_cache(self):
        for body in (None, {"ok": True}, b"complete"):
            self.run_read(body=body)
            self.run_read(body=body)
        self.assertEqual(len(self.calls), 6)

    def test_exception_and_change_during_read_dispatch_once(self):
        def broken(*args):
            raise RuntimeError("verification unavailable")
        self.run_read(provider=broken)
        self.assertEqual(len(self.calls), 1)
        seq = iter([self.ev, evidence("new", revision="v2")])
        self.run_read(provider=lambda n, a: next(seq))
        self.run_read()
        self.assertEqual(len(self.calls), 3)

    def test_change_during_hit_revalidation_dispatches(self):
        self.run_read()
        seq = iter([self.ev, evidence("new", revision="v2"), evidence("new", revision="v2")])
        self.assertEqual(self.run_read(body="new", provider=lambda n, a: next(seq)), "new")
        self.assertEqual(len(self.calls), 2)

    def test_mutation_invalidates_all_sibling_tasks(self):
        self.run_read()
        self.run_read(scope={"session_id": "s1", "task_id": "t2"})
        self.run_read(name="write_file", scope={"session_id": "s1", "task_id": "t3"})
        self.run_read()
        self.run_read(scope={"session_id": "s1", "task_id": "t2"})
        self.assertEqual(len(self.calls), 5)

    def test_missing_scope_mutation_clears_all(self):
        self.run_read()
        self.run_read(name="unknown_tool", scope={})
        self.run_read()
        self.assertEqual(len(self.calls), 3)

    def test_error_results_are_never_cached(self):
        self.ev = evidence('{"error":"failed"}')
        self.run_read(body='{"error":"failed"}')
        self.run_read(body='{"error":"failed"}')
        self.assertEqual(len(self.calls), 2)

    def test_flag_off_or_missing_native_provider_dispatches(self):
        for enabled in (False, True):
            mid = g.build_tool_execution_middleware(enabled=enabled)
            for _ in range(2):
                self.assertEqual(mid(tool_name="browser_snapshot", args=self.args,
                                     **self.scope, next_call=lambda a: "fresh"), "fresh")
        self.assertEqual(g.gate_counters()["reused"], 0)

    def test_uncanonicalizable_arguments_fail_open(self):
        cyclic = {}; cyclic["self"] = cyclic
        for args in (None, cyclic, {1: "bad"}, {"opaque": object()},
                     {"nan": float("nan")}, {"tuple": (1,)}, [1]):
            self.assertIsNone(g.fingerprint_for("read_file", args, evidence=self.ev))

    def test_bounded_cache_and_exact_empty_string(self):
        for session in range(g.DEFAULT_MAX_SESSIONS + 2):
            for key in range(g.DEFAULT_MAX_KEYS_PER_SESSION + 2):
                g.record_tool_outcome(enabled=True, tool_name="read_file", args={"key": key},
                     session_id=str(session), task_id="t", result="complete", evidence=self.ev,
                     max_sessions=1000, max_keys_per_session=1000)
        self.assertEqual(len(g._SESSION_STORE), g.DEFAULT_MAX_SESSIONS)
        self.assertTrue(all(len(v) == g.DEFAULT_MAX_KEYS_PER_SESSION for v in g._SESSION_STORE.values()))
        self.ev = evidence("")
        self.assertEqual(self.run_read(body=""), "")
        self.assertEqual(self.run_read(), "")

    def test_registration_and_native_chain(self):
        registered = []
        ctx = SimpleNamespace(register_middleware=lambda k, v: registered.append((k, v)),
                              register_hook=lambda k, v: registered.append((k, v)))
        self.assertTrue(g.register_local_duplicate_gate(ctx, enabled=True)["tool_execution_registered"])
        try:
            import hermes_cli.plugins as plugins
            from hermes_cli.middleware import run_tool_execution_middleware
        except ImportError:
            self.skipTest("requires native Hermes")
        manager = SimpleNamespace(_middleware={"tool_execution": [self.mid]},
                                  _report_hook_failure=lambda *a, **k: self.fail("middleware exception"))
        calls = []
        with patch.object(plugins, "_delivery_manager", return_value=manager):
            for _ in range(2):
                result = run_tool_execution_middleware("browser_snapshot", self.args,
                    lambda a: calls.append(1) or "complete", **self.scope,
                    reuse_evidence_provider=lambda n, a: self.ev)
                self.assertEqual(result, "complete")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
