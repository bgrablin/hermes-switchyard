"""Boundary and provenance tests for the maintained experimental harness."""

import hashlib
import json
import math
from contextlib import nullcontext, redirect_stdout
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from hermes_switchyard.receipt_state import safe_usage

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


WORKERS = [
    load("evaluation/model_routing/native_worker.py", "transfer_routing_worker"),
    load("evaluation/turn_consolidation/native_worker.py", "transfer_consolidation_worker"),
]


class JevTransferHarnessTests(unittest.TestCase):
    def test_worker_runtime_paths_must_match_fingerprinted_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "expected"
            modules = {}
            for name in WORKERS[0].RUNTIME_MODULES:
                path = root / (name.replace(".", "/") + ".py")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                modules[name] = SimpleNamespace(__file__=str(path))
            unrelated = Path(temp) / "other-run-agent.py"
            unrelated.touch()
            for worker in WORKERS:
                paths = worker.runtime_identity(root, modules)
                self.assertEqual(set(paths), set(worker.RUNTIME_MODULES))
                changed = {**modules, "run_agent": SimpleNamespace(__file__=str(unrelated))}
                with self.assertRaisesRegex(ValueError, "does not match"):
                    worker.runtime_identity(root, changed)

    def test_launcher_passes_root_and_retains_worker_identity(self):
        bootstrap = "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
        ready = {"runtime_modules": {"run_agent": "/runtime/run_agent.py"}}
        for runner in [ROUTING, CONSOLIDATION]:
            proc = mock.Mock()
            proc.stdout = ["READY " + json.dumps(ready) + "\n"]
            with mock.patch.object(
                runner.driver.subprocess,
                "check_output",
                return_value=json.dumps(["python", "-c", bootstrap]),
            ), mock.patch.object(
                runner.driver.subprocess, "Popen", return_value=proc
            ) as popen:
                worker = runner.driver.Worker(
                    "off", Path("/output"), ROOT, hermes_root=Path("/runtime")
                )
            command = popen.call_args.args[0]
            self.assertEqual(
                command[-2:], ["--hermes-root", str(Path("/runtime").resolve())]
            )
            self.assertEqual(worker.ready, ready)

    def test_recorded_usage_is_allowlisted_without_mutating_response(self):
        usage = {
            "input_tokens": 12,
            "cost": None,
            "reasoning_tokens": math.nan,
            "output_tokens": True,
            "total_tokens": -1,
            "unknown": {"text": "provider metadata"},
        }
        result = {"model": "test-model", "usage": usage}
        for worker in WORKERS:
            row = worker.recorded_decision(result, safe_usage)
            self.assertEqual(row["usage"], {"input_tokens": 12.0, "cost": None})
            self.assertIs(result["usage"], usage)
            self.assertIn("unknown", result["usage"])
            self.assertIsNot(row["usage"], usage)

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

    def test_routing_deadline_and_receipt_use_one_elapsed_value(self):
        for elapsed in [0.3999999, 0.4000001]:
            with self.subTest(elapsed=elapsed):
                router = PILOT.PilotRouter()
                router.client = mock.Mock()
                router.client.decide.return_value = {
                    "request_id": "decision",
                    "answers": {"routine": {"noul": 1}, "stakes": {"noul": 0}},
                }
                router.turns[("s", "t", "u")] = {"text": "Sum 1 and 2", "model": None}
                with mock.patch.object(
                    PILOT, "request_budget_scope", return_value=nullcontext()
                ), mock.patch.object(
                    PILOT.time, "perf_counter", side_effect=[0, elapsed, 0.401]
                ) as clock:
                    result = router.apply(
                        {"model": PILOT.ORIGIN, "input": [
                            {"role": "user", "content": "Sum 1 and 2"}
                        ]},
                        session_id="s", task_id="t", turn_id="u",
                        provider="openai-codex", api_mode="codex_responses",
                        model=PILOT.ORIGIN,
                    )
                self.assertEqual(clock.call_count, 2)
                self.assertEqual(router.receipts[0]["wall_ms"], elapsed * 1000)
                self.assertEqual(router.receipts[0]["applied"], elapsed <= 0.4)
                self.assertEqual(
                    result["model"], PILOT.TARGET if elapsed <= 0.4 else PILOT.ORIGIN
                )

    def test_live_summaries_require_completed_turns(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for path in [
                "hermes_switchyard/placeholder.py",
                "evaluation/routing_value/catalogs/c25/skills/sample/SKILL.md",
            ]:
                content = b"sample"
                entry = tarfile.TarInfo(path)
                entry.size = len(content)
                tar.addfile(entry, io.BytesIO(content))

        class Worker:
            def __init__(self, arm, *_args, **_kwargs):
                self.arm = arm
                self.ready = {"runtime_modules": {}}

            def ask(self, request):
                return {
                    "arm": self.arm, "id": request["id"], "final": "OK",
                    "completed": request["id"].endswith("-1"), "wall_ms": 1,
                    "wire": [], "jev": [], "route": [],
                }

            def close(self):
                pass

        def git_output(command, **_kwargs):
            return archive.getvalue() if command[1] == "archive" else "frozen-revision"

        for runner in [ROUTING, CONSOLIDATION]:
            with self.subTest(runner=runner.__name__), tempfile.TemporaryDirectory() as temp:
                out = Path(temp) / "run"
                with mock.patch.object(
                    runner, "CASES", [{"id": "case", "prompt": "Reply OK", "expected": "OK"}]
                ), mock.patch.object(
                    runner.subprocess, "check_output", side_effect=git_output
                ), mock.patch.object(
                    runner, "runtime_hashes", return_value={}
                ), mock.patch.object(
                    runner.driver, "Worker", Worker
                ), mock.patch(
                    "sys.argv", ["compare", "--output", str(out), "--hermes-root", temp]
                ), redirect_stdout(io.StringIO()):
                    runner.main()
                summary = json.loads((out / "summary.json").read_text())
                for metrics in summary["arms"].values():
                    self.assertEqual((metrics["n"], metrics["correct"]), (2, 1))
                rows = [
                    json.loads(line) for line in (out / "raw.jsonl").read_text().splitlines()
                ]
                self.assertEqual(len(rows), 8)
                self.assertEqual(sum(row["correct"] for row in rows), 4)
                self.assertTrue(all(row["completed"] for row in rows if row["correct"]))

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
