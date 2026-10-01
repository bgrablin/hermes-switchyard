"""Boundary and provenance tests for the maintained experimental harness."""

import hashlib
from contextlib import nullcontext
import importlib.util
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PILOT = load(
    "evaluation/model_routing/pilot.py", "hermes_switchyard._transfer_router_test"
)
ROUTING = load("evaluation/model_routing/compare.py", "transfer_routing_driver")
CONSOLIDATION = load(
    "evaluation/turn_consolidation/native_compare.py", "transfer_consolidation_driver"
)


CONSOLIDATION_PILOT = load(
    "evaluation/turn_consolidation/pilot.py",
    "hermes_switchyard._transfer_consolidation_test",
)


class JevTransferHarnessTests(unittest.TestCase):
    def test_capture_rejects_nonstring_and_long_inputs(self):
        router = PILOT.PilotRouter()
        context = dict(
            session_id="s",
            task_id="t",
            turn_id="u",
            is_first_turn=True,
            model=PILOT.ORIGIN,
            platform="cli",
        )
        with mock.patch.object(PILOT, "_task_scan") as scan:
            for message in [None, {}, [{"type": "text", "text": "x" * 2000}]]:
                router.capture(user_message=message, **context)
            scan.assert_not_called()
            scan.return_value = ("x" * 1201, None)
            router.capture(user_message="x" * 1201, **context)
        self.assertEqual(router.turns, {})

    def test_wire_rejects_multimodal_or_unknown_parts_without_deciding(self):
        router = PILOT.PilotRouter()
        router.client = mock.Mock()
        router.client.decide.side_effect = AssertionError("no decision allowed")
        router.turns[("s", "t", "u")] = {"text": "Describe this", "model": None}
        for parts in [
            [
                {"type": "input_text", "text": "Describe this"},
                {"type": "input_image", "image_url": "x"},
            ],
            [{"type": "unknown", "text": "partial"}],
            [{"type": "input_text", "text": 12}],
        ]:
            request = {
                "model": PILOT.ORIGIN,
                "input": [{"role": "user", "content": parts}],
            }
            result = router.apply(
                request,
                session_id="s",
                task_id="t",
                turn_id="u",
                provider="openai-codex",
                api_mode="codex_responses",
                model=PILOT.ORIGIN,
            )
            self.assertIs(result, request)
        router.client.decide.assert_not_called()

    def test_new_freezes_bind_shared_launcher_and_task_definitions(self):
        helper = "evaluation/decision_quality/native_compare.py"
        expected = hashlib.sha256((ROOT / helper).read_bytes()).hexdigest()
        for runner in [ROUTING, CONSOLIDATION]:
            hashes = runner.source_hashes()
            self.assertEqual(hashes[helper], expected)
            folder = runner.ROOT.relative_to(ROOT).as_posix()
            required = {
                Path(runner.__file__).relative_to(ROOT).as_posix(),
                folder + "/pilot.py",
                folder + "/native_worker.py",
                helper,
            }
            if runner is CONSOLIDATION:
                required.add("evaluation/routing_value/tasks.json")
            self.assertEqual(set(hashes), required)
        tasks = "evaluation/routing_value/tasks.json"
        self.assertEqual(
            CONSOLIDATION.source_hashes()[tasks],
            hashlib.sha256((ROOT / tasks).read_bytes()).hexdigest(),
        )

    def test_changed_wire_text_cannot_decide_or_reuse_a_route(self):
        router = PILOT.PilotRouter()
        router.client = mock.Mock()
        for cached in [None, PILOT.TARGET]:
            router.turns[("s", "t", "u")] = {
                "text": "Original public task",
                "model": cached,
            }
            request = {
                "model": PILOT.ORIGIN,
                "input": [{"role": "user", "content": "Different public task"}],
            }
            result = router.apply(
                request,
                session_id="s",
                task_id="t",
                turn_id="u",
                provider="openai-codex",
                api_mode="codex_responses",
                model=PILOT.ORIGIN,
            )
            self.assertIs(result, request)
        router.client.decide.assert_not_called()
        request["input"][0]["content"] = [
            {"type": "input_text", "text": "Original public task"}
        ]
        result = router.apply(
            request,
            session_id="s",
            task_id="t",
            turn_id="u",
            provider="openai-codex",
            api_mode="codex_responses",
            model=PILOT.ORIGIN,
        )
        self.assertEqual(result["model"], PILOT.TARGET)

    def test_model_route_without_identity_keeps_original_model(self):
        for request_id in [None, "", "  ", 12, "valid-decision-id"]:
            with self.subTest(request_id=request_id):
                router = PILOT.PilotRouter()
                router.client = mock.Mock()
                router.client.decide.return_value = {
                    "request_id": request_id,
                    "answers": {
                        "routine": {"noul": 1},
                        "stakes": {"noul": 0},
                    },
                }
                router.turns[("s", "t", "u")] = {
                    "text": "Sum 1 and 2",
                    "model": None,
                }
                request = {
                    "model": PILOT.ORIGIN,
                    "input": [{"role": "user", "content": "Sum 1 and 2"}],
                }
                with mock.patch.object(
                    PILOT, "request_budget_scope", return_value=nullcontext()
                ), mock.patch.object(PILOT.time, "perf_counter", return_value=0):
                    result = router.apply(
                        request,
                        session_id="s",
                        task_id="t",
                        turn_id="u",
                        provider="openai-codex",
                        api_mode="codex_responses",
                        model=PILOT.ORIGIN,
                    )
                valid = request_id == "valid-decision-id"
                self.assertEqual(
                    result["model"], PILOT.TARGET if valid else PILOT.ORIGIN
                )
                self.assertEqual(router.receipts[0]["applied"], valid)
                self.assertEqual(
                    router.receipts[0]["reason"],
                    "routine_qualified" if valid else "decision_failed",
                )
                router.client.decide.assert_called_once()

    def test_shared_decision_without_usable_id_falls_back(self):
        broker = CONSOLIDATION_PILOT.Broker()
        controller = SimpleNamespace(
            public_or_sanitized_data_ack=True, deadline_seconds=0.4
        )
        key = ("session", "task", "turn")
        for request_id in [None, "", "  ", 12]:
            broker.store(
                key,
                {
                    "task": "public sample",
                    "wall_ms": 1,
                    "at": time.monotonic(),
                    "result": {"request_id": request_id},
                },
            )
            result = broker.consume(
                key,
                {"excerpt": "public sample"},
                "high",
                ["low", "medium", "high"],
                controller,
            )
            self.assertIsNone(result)
        self.assertEqual(broker.receipts, [])

    def test_fixture_hashes_cover_actual_arm_copies(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = {}
            for arm in ["release", "main", "candidate"]:
                path = (
                    root
                    / arm
                    / "evaluation/routing_value/catalogs/c25/skills/sample/SKILL.md"
                )
                path.parent.mkdir(parents=True)
                path.write_text("first")
                files[arm] = path
            before = CONSOLIDATION.fixture_hashes(root)
            self.assertEqual(before["off"], before["main"])
            files["release"].write_text("changed")
            after = CONSOLIDATION.fixture_hashes(root)
            self.assertNotEqual(before["release"], after["release"])
            self.assertEqual(before["main"], after["main"])
            files["candidate"].unlink()
            with self.assertRaisesRegex(ValueError, "fixture tree missing"):
                CONSOLIDATION.fixture_hashes(root)


if __name__ == "__main__":
    unittest.main()
