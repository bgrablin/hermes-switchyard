"""Tests for the Research Navigator end-to-end harness (arms A', B, C).

These tests use fake main-model and fake Jev clients only. They make no
network call and need no credential.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from evaluation.research_navigator import e2e

ROOT = Path(__file__).resolve().parents[1]


def _case(case_id: str) -> dict:
    book, _ = e2e.load_book()
    return copy.deepcopy(next(c for c in book["cases"] if c["id"] == case_id))


def _usage(prompt: int, completion: int) -> SimpleNamespace:
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion)


def _reply(text=None, tool_calls=None, usage=(1000, 100)):
    message = SimpleNamespace(role="assistant", content=text, tool_calls=tool_calls)
    choice = SimpleNamespace(index=0, message=message, finish_reason="tool_calls" if tool_calls else "stop")
    return SimpleNamespace(choices=[choice], model="gpt-6-sol", usage=_usage(*usage) if usage else None)


def _call(name: str, arguments: dict | str, call_id: str = "call_1") -> SimpleNamespace:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(name=name, arguments=raw))


class ScriptedMain:
    """Main-model fake that returns scripted replies and records requests."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def create(self, *, messages, tools=None):
        self.requests.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class RecordingJev:
    """Jev fake: every pair answers (support, contradiction) from a map."""

    def __init__(self, answers=None, cost=0.00002):
        self.answers = answers or {}
        self.cost = cost
        self.calls = []

    def decide(self, state, questions, **_kwargs):
        self.calls.append((copy.deepcopy(state), copy.deepcopy(questions)))
        out = {}
        for name in questions:
            kind, claim_id, window_id = name.split("_", 2)
            support, contradiction = self.answers.get(f"{claim_id}/{window_id}", (0.05, 0.05))
            out[name] = {"noul": support if kind == "support" else contradiction}
        return {"model": "typesafe/jev-test", "answers": out, "request_count": 1, "usage": {"cost": self.cost}, "total_usage": {"cost": self.cost}}

    def close(self):
        pass


def _final(classes: dict, evidence: dict | None = None) -> str:
    return json.dumps(
        {"claims": [{"id": cid, "class": klass, "evidence_window_ids": (evidence or {}).get(cid, [])} for cid, klass in classes.items()]}
    )


class ParseFinalTests(unittest.TestCase):
    def test_valid_answer_maps_windows_to_original_text_and_url(self):
        case = _case("ho-01")
        cards, error = e2e.parse_final(_final({"c1": "supported"}, {"c1": ["w1"]}), case)
        self.assertIsNone(error)
        self.assertEqual(cards["c1"]["class"], "supported")
        window = case["windows"][0]
        self.assertEqual(cards["c1"]["evidence"], [{"window_id": "w1", "text": window["text"], "url": window["url"]}])

    def test_fenced_json_is_accepted(self):
        case = _case("ho-02")
        text = "```json\n" + _final({"c1": "supported"}, {"c1": ["w1"]}) + "\n```"
        cards, error = e2e.parse_final(text, case)
        self.assertIsNone(error)
        self.assertEqual(cards["c1"]["class"], "supported")

    def test_unparseable_answer_is_unresolved_with_an_error(self):
        case = _case("ho-02")
        cards, error = e2e.parse_final("Release B supports Linux, I think.", case)
        self.assertEqual(error, "final_not_json")
        self.assertEqual(cards, {"c1": {"class": "unresolved", "evidence": []}})

    def test_unknown_class_is_unresolved_with_an_error(self):
        cards, error = e2e.parse_final(_final({"c1": "probably"}), _case("ho-02"))
        self.assertEqual(error, "final_bad_class")
        self.assertEqual(cards["c1"]["class"], "unresolved")

    def test_unknown_window_id_is_kept_so_scoring_flags_a_wrong_mapping(self):
        case = _case("ho-02")
        cards, error = e2e.parse_final(_final({"c1": "supported"}, {"c1": ["w9"]}), case)
        self.assertIsNone(error)
        self.assertEqual(cards["c1"]["evidence"], [{"window_id": "w9", "text": None, "url": None}])
        self.assertTrue(e2e.score(case, {"cards": cards})["wrong_mapping"])


class PromptTests(unittest.TestCase):
    def test_every_arm_gets_the_same_case_input_and_the_same_answer_rules(self):
        case = _case("ho-14")
        inputs = {arm: e2e.user_message(arm, case) for arm in e2e.ARMS}
        self.assertEqual(len(set(inputs.values())), 1)
        payload = json.loads(inputs["A_prime"].split("\n", 1)[1])
        self.assertEqual(payload["claims"], case["claims"])
        self.assertEqual(payload["windows"], case["windows"])
        for arm in e2e.ARMS:
            self.assertIn(e2e.ANSWER_RULES, e2e.system_prompt(arm))

    def test_only_the_plugin_arms_describe_a_tool(self):
        self.assertNotIn("jev_", e2e.system_prompt("A_prime"))
        self.assertIn("jev_assess", e2e.system_prompt("B"))
        self.assertIn("jev_research_navigator", e2e.system_prompt("C"))

    def test_prompt_hash_changes_when_a_prompt_changes(self):
        before = e2e.prompt_sha256()
        original = e2e.ANSWER_RULES
        try:
            e2e.ANSWER_RULES = original + " "
            self.assertNotEqual(e2e.prompt_sha256(), before)
        finally:
            e2e.ANSWER_RULES = original


class RunCaseTests(unittest.TestCase):
    def test_a_prime_is_one_main_call_with_no_tools_and_no_jev(self):
        case = _case("ho-05")
        main = ScriptedMain([_reply(_final({"c1": "contradicted"}, {"c1": ["w1"]}), usage=(1200, 80))])
        row = e2e.run_case("A_prime", case, main=main, tool=None, jev_ledger=None)
        self.assertEqual(len(main.requests), 1)
        self.assertIsNone(main.requests[0]["tools"])
        self.assertEqual(row["main_calls"], 1)
        self.assertEqual(row["jev_calls"], 0)
        self.assertEqual((row["main_input_tokens"], row["main_output_tokens"]), (1200, 80))
        self.assertAlmostEqual(row["main_cost"], 1200 * 2.00 / 1e6 + 80 * 10.00 / 1e6)
        self.assertAlmostEqual(row["cost"], row["main_cost"])
        self.assertEqual(row["jev_cost"], 0.0)
        self.assertEqual(row["cards"]["c1"]["class"], "contradicted")
        self.assertTrue(row["class_correct"])
        self.assertGreaterEqual(row["wall_ms"], 0.0)

    def test_c_runs_the_tool_call_then_reads_the_result_in_a_second_turn(self):
        case = _case("ho-05")
        args = {"goal": case["goal"], "claims": case["claims"], "windows": case["windows"]}
        main = ScriptedMain(
            [
                _reply(None, [_call("jev_research_navigator", args, "call_7")], usage=(900, 300)),
                _reply(_final({"c1": "contradicted"}, {"c1": ["w1"]}), usage=(1500, 60)),
            ]
        )
        seen = []

        def tool(name, arguments):
            seen.append((name, arguments))
            return json.dumps({"status": "ok", "claims": []})

        ledger = e2e.JevLedger()
        ledger.record(cost=0.00002)
        row = e2e.run_case("C", case, main=main, tool=tool, jev_ledger=ledger)
        self.assertEqual(seen, [("jev_research_navigator", args)])
        self.assertEqual(len(main.requests), 2)
        self.assertEqual(main.requests[0]["tools"][0]["function"]["name"], "jev_research_navigator")
        second = main.requests[1]["messages"]
        self.assertEqual(second[-2]["role"], "assistant")
        self.assertEqual(second[-2]["tool_calls"][0]["id"], "call_7")
        self.assertEqual(second[-1], {"role": "tool", "tool_call_id": "call_7", "content": json.dumps({"status": "ok", "claims": []})})
        self.assertEqual((row["main_calls"], row["jev_calls"]), (2, 1))
        self.assertEqual((row["main_input_tokens"], row["main_output_tokens"]), (2400, 360))
        main_cost = 2400 * 2.00 / 1e6 + 360 * 10.00 / 1e6
        self.assertAlmostEqual(row["cost"], main_cost + 0.00002)
        self.assertEqual(row["tool_calls"], 1)
        self.assertFalse(row["flags"])
        self.assertIn("tool_ms", row)

    def test_b_offers_only_jev_assess(self):
        case = _case("ho-05")
        main = ScriptedMain([_reply(_final({"c1": "contradicted"}, {"c1": ["w1"]}))])
        row = e2e.run_case("B", case, main=main, tool=lambda *_: "{}", jev_ledger=e2e.JevLedger())
        self.assertEqual([t["function"]["name"] for t in main.requests[0]["tools"]], ["jev_assess"])
        self.assertEqual(row["flags"], ["no_tool_call"])
        self.assertEqual(row["main_calls"], 1)

    def test_a_second_tool_request_is_a_protocol_violation(self):
        case = _case("ho-05")
        call = _call("jev_research_navigator", {"goal": "g", "claims": [], "windows": []})
        main = ScriptedMain([_reply(None, [call]), _reply(None, [call])])
        row = e2e.run_case("C", case, main=main, tool=lambda *_: "{}", jev_ledger=e2e.JevLedger())
        self.assertIn("protocol_violation", row["flags"])
        self.assertEqual(row["cards"]["c1"]["class"], "unresolved")
        self.assertEqual(row["main_calls"], 2)

    def test_only_the_first_of_several_calls_runs(self):
        case = _case("ho-05")
        calls = [
            _call("jev_research_navigator", {"goal": "g", "claims": [], "windows": []}, "a"),
            _call("jev_research_navigator", {"goal": "g", "claims": [], "windows": []}, "b"),
        ]
        ran = []
        main = ScriptedMain([_reply(None, calls), _reply(_final({"c1": "unresolved"}))])
        row = e2e.run_case("C", case, main=main, tool=lambda n, a: ran.append(n) or "{}", jev_ledger=e2e.JevLedger())
        self.assertEqual(len(ran), 1)
        outputs = [m for m in main.requests[1]["messages"] if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in outputs], ["a", "b"])
        self.assertEqual(json.loads(outputs[1]["content"]), {"status": "error", "error": "one_tool_call_per_case"})
        self.assertIn("extra_tool_calls", row["flags"])

    def test_wrong_tool_name_and_bad_json_are_not_executed(self):
        case = _case("ho-05")
        for call, error in (
            (_call("jev_assess", {}), "tool_not_offered"),
            (_call("jev_research_navigator", "{not json"), "invalid_tool_arguments"),
        ):
            ran = []
            main = ScriptedMain([_reply(None, [call]), _reply(_final({"c1": "unresolved"}))])
            e2e.run_case("C", case, main=main, tool=lambda n, a: ran.append(n) or "{}", jev_ledger=e2e.JevLedger())
            self.assertEqual(ran, [])
            self.assertEqual(json.loads(main.requests[1]["messages"][-1]["content"])["error"], error)

    def test_main_model_error_gives_unresolved_and_null_cost(self):
        case = _case("ho-05")
        main = ScriptedMain([RuntimeError("HTTP 500")])
        row = e2e.run_case("A_prime", case, main=main, tool=None, jev_ledger=None)
        self.assertEqual(row["status"], "main_error")
        self.assertIsNone(row["cost"])
        self.assertEqual(row["cards"]["c1"]["class"], "unresolved")
        self.assertEqual(row["error"], "RuntimeError")

    def test_missing_usage_gives_null_cost(self):
        case = _case("ho-05")
        main = ScriptedMain([_reply(_final({"c1": "contradicted"}, {"c1": ["w1"]}), usage=None)])
        row = e2e.run_case("A_prime", case, main=main, tool=None, jev_ledger=None)
        self.assertIsNone(row["main_cost"])
        self.assertIsNone(row["cost"])

    def test_unknown_jev_cost_gives_null_total_cost(self):
        case = _case("ho-05")
        main = ScriptedMain([_reply(None, [_call("jev_research_navigator", {})]), _reply(_final({"c1": "unresolved"}))])
        ledger = e2e.JevLedger()
        ledger.record(cost=None)
        row = e2e.run_case("C", case, main=main, tool=lambda *_: "{}", jev_ledger=ledger)
        self.assertIsNone(row["jev_cost"])
        self.assertIsNone(row["cost"])


class FaultInjectionTests(unittest.TestCase):
    def test_timeout_and_outage_raise_without_calling_the_real_client(self):
        for mode, error in (("timeout", "DeadlineExceeded"), ("outage", "RuntimeError")):
            real = RecordingJev()
            ledger = e2e.JevLedger()
            client = e2e.FaultJev(real, mode, ledger)
            with self.assertRaises(Exception) as caught:
                client.decide({"s": 1}, {"support_c1_w1": {"type": "noul", "instructions": "x"}})
            self.assertEqual(type(caught.exception).__name__, error)
            self.assertEqual(real.calls, [])
            self.assertEqual((ledger.calls, ledger.cost), (0, 0.0))

    def test_malformed_drops_one_real_answer_and_extra_adds_one(self):
        questions = {"support_c1_w1": {"type": "noul", "instructions": "x"}, "contradict_c1_w1": {"type": "noul", "instructions": "y"}}
        real = RecordingJev({"c1/w1": (0.9, 0.1)})
        ledger = e2e.JevLedger()
        out = e2e.FaultJev(real, "malformed", ledger).decide({}, questions)
        self.assertEqual(len(real.calls), 1)
        self.assertEqual(set(out["answers"]), {"contradict_c1_w1"})
        out = e2e.FaultJev(real, "extra_answer", ledger).decide({}, questions)
        self.assertEqual(set(out["answers"]), set(questions) | {"support_zz_zz"})
        self.assertEqual(ledger.calls, 2)
        self.assertAlmostEqual(ledger.cost, 0.00004)

    def test_scripted_passes_through_and_records_cost(self):
        real = RecordingJev(cost=0.00003)
        ledger = e2e.JevLedger()
        e2e.FaultJev(real, "scripted", ledger).decide({}, {"support_c1_w1": {"type": "noul", "instructions": "x"}})
        self.assertEqual((ledger.calls, ledger.cost), (1, 0.00003))

    def test_a_failed_real_call_has_unknown_cost(self):
        class Broken(RecordingJev):
            def decide(self, *a, **k):
                raise ConnectionError("reset")

        ledger = e2e.JevLedger()
        with self.assertRaises(ConnectionError):
            e2e.FaultJev(Broken(), "scripted", ledger).decide({}, {"support_c1_w1": {}})
        self.assertEqual(ledger.calls, 1)
        self.assertIsNone(ledger.cost)


def _row(arm, case_id, split="heldout", correct=True, wall=100.0, cost: float | None = 0.001, false_assertion=False, jev_calls=0):
    return {
        "arm": arm, "case": case_id, "split": split, "class_correct": correct, "false_assertion": false_assertion,
        "wrong_mapping": False, "wall_ms": wall, "cost": cost, "main_cost": cost, "jev_cost": 0.0,
        "main_input_tokens": 10, "main_output_tokens": 1, "main_calls": 1, "jev_calls": jev_calls, "flags": [],
        "status": "answered",
    }


class AcceptanceTests(unittest.TestCase):
    def _rows(self, c_correct=3, c_wall=50.0, c_cost=0.0005, a_cost: float | None = 0.001, c_null=False):
        ids = ["ho-01", "ho-02", "ho-03", "ho-04"]
        rows = []
        for i, cid in enumerate(ids):
            rows.append(_row("A_prime", cid, correct=i < 2, wall=100.0, cost=a_cost))
            rows.append(_row("B", cid, correct=i < 2, wall=100.0, cost=0.001, jev_calls=1))
            rows.append(_row("C", cid, correct=i < c_correct, wall=c_wall, cost=None if c_null and i == 0 else c_cost, jev_calls=1))
        return rows

    def test_passes_only_when_c_is_strictly_better_and_no_worse(self):
        result = e2e.acceptance(self._rows(), primary_ids=["ho-01", "ho-02", "ho-03", "ho-04"])
        self.assertTrue(result["passed"], result)
        self.assertEqual(set(result["rules"]), {"outcome", "latency_p50", "latency_total", "cost_total", "floor_false_assertions", "floor_wrong_mappings", "floor_jev_requests", "floor_fault_cases"})

    def test_outcome_tie_fails(self):
        result = e2e.acceptance(self._rows(c_correct=2), primary_ids=["ho-01", "ho-02", "ho-03", "ho-04"])
        self.assertFalse(result["rules"]["outcome"]["passed"])
        self.assertFalse(result["passed"])

    def test_slower_or_costlier_c_fails_with_margin(self):
        result = e2e.acceptance(self._rows(c_wall=150.0, c_cost=0.002), primary_ids=["ho-01", "ho-02", "ho-03", "ho-04"])
        self.assertFalse(result["rules"]["latency_p50"]["passed"])
        self.assertFalse(result["rules"]["latency_total"]["passed"])
        self.assertFalse(result["rules"]["cost_total"]["passed"])
        self.assertAlmostEqual(result["rules"]["cost_total"]["c_over_best_baseline"], 2.0)

    def test_null_cost_fails(self):
        result = e2e.acceptance(self._rows(c_null=True), primary_ids=["ho-01", "ho-02", "ho-03", "ho-04"])
        self.assertFalse(result["rules"]["cost_total"]["passed"])
        self.assertEqual(result["rules"]["cost_total"]["null_cost_rows"]["C"], 1)

    def test_null_baseline_cost_also_fails_because_no_worse_cannot_be_shown(self):
        result = e2e.acceptance(self._rows(a_cost=None), primary_ids=["ho-01", "ho-02", "ho-03", "ho-04"])
        self.assertFalse(result["rules"]["cost_total"]["passed"])

    def test_primary_uses_only_the_frozen_subset(self):
        rows = self._rows()
        rows.append(_row("C", "ho-29", correct=False, cost=None))
        result = e2e.acceptance(rows, primary_ids=["ho-01", "ho-02", "ho-03", "ho-04"])
        self.assertTrue(result["rules"]["cost_total"]["passed"])

    def test_fault_case_gate_fails_on_any_b_or_c_assertion(self):
        rows = self._rows()
        rows.append(_row("C", "ho-29"))
        rows.append(_row("B", "ho-29"))
        self.assertTrue(e2e.acceptance(rows, primary_ids=["ho-01", "ho-02", "ho-03", "ho-04"])["rules"]["floor_fault_cases"]["passed"])
        for arm in ("B", "C"):
            bad = rows + [_row(arm, "ho-30", false_assertion=True)]
            rule = e2e.acceptance(bad, primary_ids=["ho-01", "ho-02", "ho-03", "ho-04"])["rules"]["floor_fault_cases"]
            self.assertFalse(rule["passed"])
            self.assertEqual(rule["false_assertion_cases"][arm], ["ho-30"])

    def test_false_assertion_floor_counts_every_case(self):
        rows = self._rows()
        rows.append(_row("C", "dev-02", split="dev", false_assertion=True))
        result = e2e.acceptance(rows, primary_ids=["ho-01", "ho-02", "ho-03", "ho-04"])
        self.assertFalse(result["rules"]["floor_false_assertions"]["passed"])


class PlanTests(unittest.TestCase):
    def test_plan_covers_all_forty_cases_with_balanced_interleaving(self):
        plan = e2e.build_plan(primary_heldout_ids=e2e.DEFAULT_PRIMARY_HELDOUT)
        self.assertEqual(len(plan["order"]), 40)
        self.assertEqual({o["case"] for o in plan["order"]}, {c["id"] for c in e2e.load_book()[0]["cases"]})
        orders = [tuple(o["arms"]) for o in plan["order"]]
        self.assertTrue(all(sorted(o) == sorted(e2e.ARMS) for o in orders))
        first = [o[0] for o in orders]
        for arm in e2e.ARMS:
            self.assertGreaterEqual(first.count(arm), 13)

    def test_plan_freezes_hashes_prices_and_thresholds(self):
        plan = e2e.build_plan(primary_heldout_ids=e2e.DEFAULT_PRIMARY_HELDOUT)
        self.assertEqual(plan["fixtures_sha256"], e2e.load_book()[1])
        self.assertEqual(plan["prompts_sha256"], e2e.prompt_sha256())
        self.assertEqual(plan["harness_sha256"], e2e.file_sha256(Path(e2e.__file__)))
        self.assertEqual(plan["pricing"]["input_per_million_usd"], 2.00)
        self.assertEqual(plan["pricing"]["output_per_million_usd"], 10.00)
        self.assertIn("developers.openai.com/api/docs/pricing", plan["pricing"]["source"])
        self.assertEqual(plan["main_route"], {"provider": "openai-codex", "model": "gpt-6-sol", "reasoning_effort": "high"})
        self.assertEqual(plan["jev_route"], {"jev_provider": "auto", "fallback": "none"})
        self.assertEqual(plan["excluded_from_primary"], ["ho-27", "ho-28", "ho-29", "ho-30", "ho-31", "ho-32"])
        self.assertEqual(len(plan["primary_heldout_ids"]), 26)
        self.assertNotIn("codex_credential_label", plan)
        self.assertIn("fill_first", plan["codex_credential_selection"])

    def test_verify_plan_rejects_any_frozen_input_change(self):
        plan = e2e.build_plan(primary_heldout_ids=e2e.DEFAULT_PRIMARY_HELDOUT)
        e2e.verify_plan(plan)
        for key in ("fixtures_sha256", "prompts_sha256", "harness_sha256", "candidate_sha256"):
            bad = dict(plan, **{key: "0" * 64})
            with self.assertRaises(SystemExit) as caught:
                e2e.verify_plan(bad)
            self.assertIn(key, str(caught.exception))


class IsolationTests(unittest.TestCase):
    def test_a_prime_child_environment_has_no_plugin_path_and_no_jev_key(self):
        env = e2e.child_env(
            "A_prime",
            base={"PATH": "/usr/bin", "PYTHONPATH": "/x/hermes-switchyard", "OPENROUTER_API_KEY": "k", "TYPESAFE_API_KEY": "t", "HOME": "/h"},
            package_root=Path("/pkg"),
            hermes_root=Path("/hermes"),
            eval_home=Path("/eval-home"),
        )
        self.assertNotIn("OPENROUTER_API_KEY", env)
        self.assertNotIn("TYPESAFE_API_KEY", env)
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep), ["/hermes", str(Path(e2e.__file__).resolve().parent)])
        self.assertEqual(env["HERMES_HOME"], "/eval-home")

    def test_plugin_child_environment_puts_the_arm_package_first(self):
        env = e2e.child_env("B", base={"PATH": "/usr/bin", "OPENROUTER_API_KEY": "k"}, package_root=Path("/pkg"), hermes_root=Path("/hermes"), eval_home=Path("/e"))
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep)[:2], ["/pkg", "/hermes"])
        self.assertEqual(env["OPENROUTER_API_KEY"], "k")

    def test_assert_switchyard_absent_fails_when_loaded(self):
        e2e.assert_switchyard_absent(modules={"json": json})
        with self.assertRaises(SystemExit):
            e2e.assert_switchyard_absent(modules={"hermes_switchyard": object()})

    def test_history_snapshot_detects_new_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "plugin-data" / "hermes-switchyard"
            data.mkdir(parents=True)
            before = e2e.history_snapshot(Path(tmp))
            (data / "receipt-history.jsonl").write_text("{}\n")
            after = e2e.history_snapshot(Path(tmp))
            self.assertEqual(e2e.history_delta(before, after), {"receipt-history.jsonl": 3})


def _jwt(exp: float) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": int(exp)}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


try:
    import agent.credential_pool  # noqa: F401  (pinned Hermes tree)

    HAVE_HERMES = True
except ImportError:  # pragma: no cover - CI without Hermes
    HAVE_HERMES = False


def _entry(label, priority, *, exp_in=86400.0, **extra):
    return {
        "id": label[:6], "label": label, "auth_type": "oauth", "priority": priority, "source": "manual:device_code",
        "access_token": _jwt(time.time() + exp_in), "refresh_token": "r", "base_url": "https://chatgpt.com/backend-api/codex",
        "request_count": 0, **extra,
    }


@unittest.skipUnless(HAVE_HERMES, "pinned Hermes tree not importable")
class CredentialTests(unittest.TestCase):
    """The eval uses the real Hermes openai-codex pool selection, read-only."""

    def _write(self, tmp, entries, strategy=None):
        auth = Path(tmp) / "auth.json"
        auth.write_text(json.dumps({"version": 1, "credential_pool": {"openai-codex": entries}}))
        config = Path(tmp) / "config.yaml"
        config.write_text("" if strategy is None else f"credential_pool_strategies:\n  openai-codex: {strategy}\n")
        return auth, config

    def _select(self, tmp, entries, strategy=None):
        auth, config = self._write(tmp, entries, strategy)
        before = (auth.read_bytes(), config.read_bytes())
        try:
            return e2e.codex_credential(auth, config)
        finally:
            self.assertEqual((auth.read_bytes(), config.read_bytes()), before, "credential files changed")

    def test_fill_first_picks_the_lowest_priority_available_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            got = self._select(tmp, [_entry("second", 1), _entry("first", 0)])
            self.assertEqual(got["label"], "first")
            self.assertEqual(got["base_url"], "https://chatgpt.com/backend-api/codex")
            self.assertTrue(got["access_token"].startswith("h."))

    def test_exhausted_entry_in_cooldown_is_skipped_like_the_agent_does(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = _entry("first", 0, last_status="exhausted", last_status_at=time.time(), last_error_reset_at=time.time() + 3600)
            got = self._select(tmp, [first, _entry("second", 1)])
            self.assertEqual(got["label"], "second")

    def test_an_entry_that_needs_refresh_stops_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as caught:
                self._select(tmp, [_entry("first", 0, exp_in=30.0), _entry("second", 1)])
            self.assertIn("codex_token_needs_refresh", str(caught.exception))
            self.assertNotIn("h.", str(caught.exception))

    def test_a_token_too_close_to_expiry_for_the_run_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as caught:
                self._select(tmp, [_entry("first", 0, exp_in=900.0)])
            self.assertIn("codex_token_expiring", str(caught.exception))

    def test_no_available_entry_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            dead = _entry("first", 0, last_status="dead")
            with self.assertRaises(SystemExit) as caught:
                self._select(tmp, [dead])
            self.assertIn("codex_no_available_entry", str(caught.exception))

    def test_a_non_fill_first_strategy_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as caught:
                self._select(tmp, [_entry("first", 0), _entry("second", 1)], strategy="round_robin")
            self.assertIn("codex_strategy_not_fill_first", str(caught.exception))


class DryRunTests(unittest.TestCase):
    def test_dry_run_drives_all_three_arms_through_child_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "dry.json"
            proc = subprocess.run(
                [
                    sys.executable, str(ROOT / "evaluation" / "research_navigator" / "e2e.py"), "--dry-run",
                    "--cases", "dev-01,ho-05,ho-29", "--baseline-root", str(ROOT), "--output", str(out),
                ],
                capture_output=True, text=True, timeout=240, cwd=tmp,
                env={k: v for k, v in os.environ.items() if k not in {"OPENROUTER_API_KEY", "TYPESAFE_API_KEY"}},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
            report = json.loads(out.read_text())
            self.assertEqual(report["mode"], "dry_run")
            self.assertEqual(len(report["rows"]), 9)
            by = {(r["case"], r["arm"]): r for r in report["rows"]}
            self.assertEqual((by[("ho-05", "C")]["main_calls"], by[("ho-05", "C")]["jev_calls"]), (2, 1))
            self.assertEqual((by[("ho-05", "B")]["main_calls"], by[("ho-05", "B")]["jev_calls"]), (2, 1))
            self.assertEqual((by[("ho-05", "A_prime")]["main_calls"], by[("ho-05", "A_prime")]["jev_calls"]), (1, 0))
            self.assertEqual(by[("ho-29", "C")]["jev_calls"], 0)
            self.assertFalse(by[("ho-05", "A_prime")]["switchyard_loaded"])
            self.assertEqual(by[("ho-05", "A_prime")]["history_delta"], {})
            self.assertIn("acceptance", report)
            self.assertNotIn("access_token", out.read_text())


if __name__ == "__main__":
    unittest.main()
