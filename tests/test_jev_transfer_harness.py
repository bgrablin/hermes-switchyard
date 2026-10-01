"""Boundary and provenance tests for the maintained experimental harness."""

import hashlib
import importlib.util
from pathlib import Path
import tempfile
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
