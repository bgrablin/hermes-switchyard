"""Opt-in disposable tool-output filter (default off; exec soft-cap slice)."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from hermes_switchyard.tool_output_filter import (
    DEFAULT_SOFT_CAP_CHARS,
    FILTERABLE_KINDS,
    build_pre_llm_call_capture_hook,
    build_transform_tool_result_hook,
    clear_user_text_for_tests,
    filter_tool_result_text,
    looks_security_or_failure_relevant,
    note_user_text,
    register_tool_output_filter,
    should_filter_tool_result,
    soft_cap_text,
    user_asks_full_dump,
)


def _noisy_stdout(n: int = DEFAULT_SOFT_CAP_CHARS + 2_000) -> str:
    # Synthetic build log — no host paths, no credentials.
    line = "npm notice built package size: 1.2 MB\n"
    repeats = (n // len(line)) + 1
    return (line * repeats)[:n]


class SoftCapHelpersTests(unittest.TestCase):
    def test_soft_cap_text_returns_none_when_small(self):
        self.assertIsNone(soft_cap_text("short ok"))

    def test_soft_cap_text_keeps_head_and_tail(self):
        body = _noisy_stdout(10_000)
        capped = soft_cap_text(body, soft_cap=6_000, head_chars=2_500, tail_chars=2_000)
        self.assertIsNotNone(capped)
        assert capped is not None
        self.assertTrue(capped.startswith(body[:2_500]))
        self.assertTrue(capped.endswith(body[-2_000:]))
        self.assertIn("switchyard: omitted", capped)
        self.assertLess(len(capped), len(body))
        self.assertIn("filter_disposable_tool_output", capped)

    def test_security_marker_detected(self):
        self.assertTrue(looks_security_or_failure_relevant("Error: Permission denied reading vault"))
        self.assertFalse(looks_security_or_failure_relevant(_noisy_stdout(200)))

    def test_full_dump_ask_from_command(self):
        self.assertTrue(user_asks_full_dump(args={"command": "npm test -- full dump please"}))
        self.assertTrue(user_asks_full_dump(args={"command": "cat log.txt  # do not truncate"}))
        self.assertFalse(user_asks_full_dump(args={"command": "npm install"}))


class PredicateTests(unittest.TestCase):
    def test_flag_off_never_filters(self):
        self.assertFalse(
            should_filter_tool_result(
                enabled=False,
                tool_name="terminal",
                result=_noisy_stdout(),
                status="ok",
            )
        )
        self.assertIsNone(
            filter_tool_result_text(
                enabled=False,
                tool_name="terminal",
                result=_noisy_stdout(),
                status="ok",
            )
        )

    def test_non_exec_kinds_not_filtered(self):
        big = "x" * (DEFAULT_SOFT_CAP_CHARS + 500)
        for name in ("read_file", "write_file", "web_search", "jev_assess"):
            self.assertFalse(
                should_filter_tool_result(
                    enabled=True,
                    tool_name=name,
                    result=big,
                    status="ok",
                ),
                msg=name,
            )

    def test_exec_success_large_is_filtered(self):
        body = _noisy_stdout()
        self.assertTrue(
            should_filter_tool_result(
                enabled=True,
                tool_name="terminal",
                result=body,
                status="ok",
            )
        )
        capped = filter_tool_result_text(
            enabled=True,
            tool_name="terminal",
            result=body,
            status="ok",
        )
        self.assertIsNotNone(capped)
        assert capped is not None
        self.assertLess(len(capped), len(body))
        self.assertIn("switchyard: omitted", capped)

    def test_preserves_error_status(self):
        body = _noisy_stdout()
        self.assertFalse(
            should_filter_tool_result(
                enabled=True,
                tool_name="terminal",
                result=body,
                status="error",
                error_message="command failed",
            )
        )

    def test_preserves_nonzero_returncode_json(self):
        payload = {
            "output": _noisy_stdout(),
            "returncode": 1,
        }
        raw = json.dumps(payload)
        self.assertFalse(
            should_filter_tool_result(
                enabled=True,
                tool_name="terminal",
                result=raw,
                status="ok",
            )
        )

    def test_preserves_security_relevant_stdout(self):
        # Marker must remain in the scanned window (head/mid/tail), not only
        # past a truncated fixtureslice.
        marker = "FATAL ERROR: Permission denied for deploy key\n"
        pad = "npm notice ok\n"
        body = marker + (pad * 900) + marker
        self.assertGreater(len(body), DEFAULT_SOFT_CAP_CHARS)
        self.assertTrue(looks_security_or_failure_relevant(body))
        self.assertFalse(
            should_filter_tool_result(
                enabled=True,
                tool_name="bash",
                result=body,
                status="ok",
            )
        )

    def test_preserves_user_full_dump_ask(self):
        self.assertFalse(
            should_filter_tool_result(
                enabled=True,
                tool_name="terminal",
                result=_noisy_stdout(),
                args={"command": "npm test  # keep full output"},
                status="ok",
            )
        )

    def test_preserves_user_full_dump_from_turn_message(self):
        clear_user_text_for_tests()
        # Ordinary ask lives in the user turn, not in the shell command.
        note_user_text(
            "Run npm test and show the full output please.",
            session_id="sess-1",
            task_id="task-1",
        )
        self.assertTrue(
            user_asks_full_dump(
                args={"command": "npm test"},
                session_id="sess-1",
                task_id="task-1",
            )
        )
        self.assertFalse(
            should_filter_tool_result(
                enabled=True,
                tool_name="terminal",
                result=_noisy_stdout(),
                args={"command": "npm test"},
                status="ok",
                session_id="sess-1",
                task_id="task-1",
            )
        )
        clear_user_text_for_tests()

    def test_preserves_small_output(self):
        self.assertFalse(
            should_filter_tool_result(
                enabled=True,
                tool_name="shell",
                result="ok\n",
                status="ok",
            )
        )

    def test_structured_output_field_is_soft_capped(self):
        payload = {"output": _noisy_stdout(), "returncode": 0}
        raw = json.dumps(payload)
        capped = filter_tool_result_text(
            enabled=True,
            tool_name="terminal",
            result=raw,
            status="ok",
        )
        self.assertIsNotNone(capped)
        assert capped is not None
        parsed = json.loads(capped)
        self.assertEqual(parsed["returncode"], 0)
        self.assertTrue(parsed.get("switchyard_output_filtered"))
        self.assertIn("switchyard: omitted", parsed["output"])
        self.assertLess(len(parsed["output"]), len(payload["output"]))

    def test_filterable_kinds_closed_set_exec_only(self):
        self.assertEqual(FILTERABLE_KINDS, frozenset({"exec"}))


class HookRegistrationTests(unittest.TestCase):
    def test_hook_noop_when_flag_off(self):
        hook = build_transform_tool_result_hook(enabled=False)
        self.assertIsNone(
            hook(tool_name="terminal", result=_noisy_stdout(), status="ok")
        )

    def test_hook_soft_caps_when_flag_on(self):
        hook = build_transform_tool_result_hook(enabled=True)
        body = _noisy_stdout()
        out = hook(tool_name="terminal", args={"command": "npm install"}, result=body, status="ok")
        self.assertIsInstance(out, str)
        assert isinstance(out, str)
        self.assertLess(len(out), len(body))

    def test_hook_swallows_internal_errors(self):
        hook = build_transform_tool_result_hook(enabled=True)

        class Boom:
            def __str__(self) -> str:
                raise RuntimeError("boom")

        # Should fail open (None), never raise.
        self.assertIsNone(hook(tool_name="terminal", result=Boom(), status="ok"))

    def test_pre_llm_call_capture_feeds_transform(self):
        clear_user_text_for_tests()
        capture = build_pre_llm_call_capture_hook()
        capture(
            user_message="please keep the full dump of this build",
            session_id="s2",
            task_id="t2",
        )
        hook = build_transform_tool_result_hook(enabled=True)
        self.assertIsNone(
            hook(
                tool_name="terminal",
                args={"command": "npm install"},
                result=_noisy_stdout(),
                status="ok",
                session_id="s2",
                task_id="t2",
            )
        )
        clear_user_text_for_tests()

    def test_register_flag_on_registers_capture(self):
        seen: list[str] = []

        def register_hook(name, callback):
            seen.append(name)

        receipt = register_tool_output_filter(
            SimpleNamespace(register_hook=register_hook),
            enabled=True,
        )
        self.assertTrue(receipt["registered"])
        self.assertTrue(receipt["enabled"])
        self.assertTrue(receipt["pre_llm_call_capture"])
        self.assertEqual(seen, ["transform_tool_result", "pre_llm_call"])

    def test_register_flag_off_adds_no_listener(self):
        seen: list[tuple[str, object]] = []

        def register_hook(name, callback):
            seen.append((name, callback))

        receipt = register_tool_output_filter(
            SimpleNamespace(register_hook=register_hook),
            enabled=False,
        )
        self.assertFalse(receipt["registered"])
        self.assertFalse(receipt["enabled"])
        self.assertEqual(receipt["reason"], "disabled")
        self.assertEqual(receipt["scope"], "exec_soft_cap")
        self.assertEqual(seen, [])

    def test_register_without_seam(self):
        receipt = register_tool_output_filter(SimpleNamespace(), enabled=True)
        self.assertFalse(receipt["registered"])
        self.assertEqual(receipt["reason"], "hermes_transform_tool_result_unavailable")


class CompositionSmokeTests(unittest.TestCase):
    """Hermes first-string-wins: Switchyard must compose, not stack listeners."""

    def test_composed_with_compaction_still_soft_caps(self):
        from hermes_switchyard.output_pruning import prune_terminal_result
        from hermes_switchyard.tool_output_filter import build_transform_tool_result_hook

        # Unique lines: compaction cannot collapse them, so soft-cap must still run
        # after the shared transform_tool_result composition (Hermes first-string-wins).
        lines = [f"build step {i}: compiled object {i}.o ok" for i in range(400)]
        noisy = "\n".join(lines)
        self.assertGreater(len(noisy), DEFAULT_SOFT_CAP_CHARS)
        payload = json.dumps({"exit_code": 0, "stdout": noisy, "stderr": ""})
        filter_hook = build_transform_tool_result_hook(enabled=True)

        def composed(**kwargs):
            working = dict(kwargs)
            override = None
            pruned = prune_terminal_result(**working)
            if isinstance(pruned, str):
                override = pruned
                working["result"] = pruned
            filtered = filter_hook(**working)
            if isinstance(filtered, str):
                override = filtered
            return override

        out = composed(tool_name="terminal", result=payload, status="ok", args={"command": "make -j4"})
        self.assertIsInstance(out, str)
        self.assertIn("switchyard: omitted", out)
        self.assertLess(len(out), len(payload))


if __name__ == "__main__":
    unittest.main()
