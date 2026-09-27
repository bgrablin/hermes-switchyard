"""Offline tests for the F2 end-to-end harness (A' judge arm, live pairs, acceptance).

No test calls a model, a browser, or the network. The main-model runner and
the Jev exchange are stubs.
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "evaluation" / "browser_progress"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import evaluate  # noqa: E402
import progress_e2e as e2e  # noqa: E402

OPTIONAL = {
    "trajectory": {"type": "choice", "instructions": "x", "criteria": {
        "progress": "a", "stagnant": "b", "regression": "c", "unclear": "d"}},
    "new_goal_evidence": {"type": "noul", "instructions": "y"},
}
OPTIONAL_WITH_RECOVERY = dict(OPTIONAL, next_observation={
    "type": "choice", "instructions": "z", "criteria": {"SCROLL_DOWN": "a", "RETURN_INCOMPLETE": "b"}})
STATE = {"goal": "Find the fact.", "page": {"title": "t", "text": "now"}, "previous_page": {"title": "t", "text": "before"}}
USAGE = {"input_tokens": 1000, "cache_read_tokens": 2000, "cache_write_tokens": 0, "output_tokens": 300,
         "reasoning_tokens": 200, "api_calls": 1, "model": "gpt-6-sol", "provider": "openai-codex",
         "completed": True, "failed": False}


class StubRunner:
    def __init__(self, replies, usage=USAGE, exit_code=0):
        self.replies = list(replies)
        self.usage = usage
        self.exit_code = exit_code
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return {"stdout": reply, "usage": copy.deepcopy(self.usage), "exit_code": self.exit_code, "wall_ms": 12.5}


class PricingTests(unittest.TestCase):
    def test_list_price_uses_the_cited_gpt_6_sol_rates(self):
        cost = e2e.list_price_usd(USAGE)
        # 1000 * 2.00 + 2000 * 0.20 + 0 * 2.50 + 300 * 10.00 per 1M tokens.
        self.assertAlmostEqual(cost, (1000 * 2.0 + 2000 * 0.2 + 300 * 10.0) / 1_000_000)
        self.assertEqual(e2e.PRICING["model"], "gpt-6-sol")
        self.assertTrue(e2e.PRICING["source_url"].startswith("https://developers.openai.com/"))
        self.assertRegex(e2e.PRICING["retrieved"], r"^\d{4}-\d{2}-\d{2}$")

    def test_long_prompts_use_the_whole_request_tier(self):
        usage = dict(USAGE, input_tokens=300_000, cache_read_tokens=0, output_tokens=0)
        self.assertAlmostEqual(e2e.list_price_usd(usage), 300_000 * 4.0 / 1_000_000)

    def test_harness_auxiliary_tokens_are_recorded_but_not_priced(self):
        # One-shot title generation is a harness artifact: a judgment inside a
        # real session makes no such call. Excluding it lowers A' cost only.
        usage = dict(USAGE, auxiliary={"input_tokens": 500, "output_tokens": 10, "cache_read_tokens": 0,
                                       "cache_write_tokens": 0, "api_calls": 1})
        self.assertAlmostEqual(e2e.list_price_usd(usage), e2e.list_price_usd(USAGE))
        runner = StubRunner(['{"trajectory": "progress", "confidence": 0.95, "new_goal_evidence": 0.8}'], usage=usage)
        judge = e2e.MainModelJudge(runner, max_calls=1)
        judge(STATE, OPTIONAL)
        self.assertEqual(judge.calls[0]["harness_auxiliary_tokens"], 510)

    def test_missing_token_counts_give_a_null_cost(self):
        for key in ("input_tokens", "output_tokens"):
            usage = dict(USAGE)
            usage.pop(key)
            self.assertIsNone(e2e.list_price_usd(usage))
        self.assertIsNone(e2e.list_price_usd(dict(USAGE, output_tokens=True)))
        self.assertIsNone(e2e.list_price_usd({}))


class JudgePromptTests(unittest.TestCase):
    def test_prompt_is_frozen_and_uses_the_same_observation_fields(self):
        template = e2e.load_judge_template()
        prompt = e2e.render_judge_prompt(template, STATE, OPTIONAL)
        self.assertIn(json.dumps(STATE, ensure_ascii=False, sort_keys=True), prompt)
        self.assertIn('"stagnant"', prompt)
        self.assertNotIn("<<", prompt)
        self.assertNotIn("next_observation:", prompt)
        with_recovery = e2e.render_judge_prompt(template, STATE, OPTIONAL_WITH_RECOVERY)
        self.assertIn("next_observation", with_recovery)
        self.assertIn('"RETURN_INCOMPLETE"', with_recovery)

    def test_valid_reply_maps_to_jev_answer_shapes(self):
        reply = 'text {"trajectory": "stagnant", "confidence": 0.9, "new_goal_evidence": 0.1, "next_observation": "SCROLL_DOWN"}'
        answers = e2e.parse_judge_reply(reply, OPTIONAL_WITH_RECOVERY)
        self.assertEqual(answers["trajectory"]["choice"], "stagnant")
        self.assertEqual(answers["trajectory"]["confidence"], 0.9)
        self.assertAlmostEqual(answers["trajectory"]["probabilities"]["stagnant"], 0.9, places=5)
        self.assertAlmostEqual(sum(answers["trajectory"]["probabilities"].values()), 1.0)
        self.assertEqual(answers["new_goal_evidence"], {"noul": 0.1})
        self.assertEqual(answers["next_observation"]["choice"], "SCROLL_DOWN")

    def test_invalid_replies_are_rejected(self):
        bad = [
            "no json",
            '{"trajectory": "stuck", "confidence": 0.9, "new_goal_evidence": 0.1}',
            '{"trajectory": "stagnant", "confidence": 1.5, "new_goal_evidence": 0.1}',
            '{"trajectory": "stagnant", "confidence": 0.9}',
            '{"trajectory": "stagnant", "confidence": true, "new_goal_evidence": 0.1}',
        ]
        for reply in bad:
            self.assertIsNone(e2e.parse_judge_reply(reply, OPTIONAL), reply)
        reply = '{"trajectory": "stagnant", "confidence": 0.9, "new_goal_evidence": 0.1, "next_observation": "CLICK"}'
        self.assertIsNone(e2e.parse_judge_reply(reply, OPTIONAL_WITH_RECOVERY))

    def test_abstention_answers_cannot_stop_a_run(self):
        answers = e2e.abstain_answers(OPTIONAL_WITH_RECOVERY)
        self.assertEqual(answers["trajectory"]["choice"], "unclear")
        self.assertEqual(answers["trajectory"]["confidence"], 0.0)
        # Every asked question needs a valid answer for the client; an
        # abstention caps the run, so this choice can never be reported.
        self.assertIn(answers["next_observation"]["choice"], OPTIONAL_WITH_RECOVERY["next_observation"]["criteria"])
        self.assertEqual(answers["next_observation"]["confidence"], 0.0)
        self.assertEqual(set(answers), set(OPTIONAL_WITH_RECOVERY))


class MainModelJudgeTests(unittest.TestCase):
    def test_judge_records_tokens_wall_time_and_list_price(self):
        runner = StubRunner(['{"trajectory": "progress", "confidence": 0.95, "new_goal_evidence": 0.8}'])
        judge = e2e.MainModelJudge(runner, max_calls=2)
        answers = judge(STATE, OPTIONAL)
        self.assertEqual(answers["trajectory"]["choice"], "progress")
        call = judge.calls[0]
        self.assertEqual(call["status"], "ok")
        self.assertEqual(call["wall_ms"], 12.5)
        self.assertEqual(call["input_tokens"], 1000)
        self.assertEqual(call["cache_read_tokens"], 2000)
        self.assertEqual(call["output_tokens"], 300)
        self.assertEqual(call["reasoning_tokens"], 200)
        self.assertAlmostEqual(call["list_price_usd"], e2e.list_price_usd(USAGE))
        self.assertNotIn("stdout", call)

    def test_failures_abstain_and_keep_the_measured_usage(self):
        runner = StubRunner(["not json"])
        judge = e2e.MainModelJudge(runner, max_calls=3)
        answers = judge(STATE, OPTIONAL)
        self.assertEqual(answers["trajectory"]["choice"], "unclear")
        self.assertEqual(judge.calls[0]["status"], "invalid_reply")
        self.assertIsNotNone(judge.calls[0]["list_price_usd"])
        failing = e2e.MainModelJudge(StubRunner(["x"], exit_code=2), max_calls=3)
        failing(STATE, OPTIONAL)
        self.assertEqual(failing.calls[0]["status"], "hermes_failed")

    def test_wrong_route_is_a_failure(self):
        runner = StubRunner(['{"trajectory": "progress", "confidence": 0.95, "new_goal_evidence": 0.8}'],
                            usage=dict(USAGE, model="gpt-6-luna"))
        judge = e2e.MainModelJudge(runner, max_calls=1)
        self.assertEqual(judge(STATE, OPTIONAL)["trajectory"]["choice"], "unclear")
        self.assertEqual(judge.calls[0]["status"], "wrong_route")

    def test_call_cap_is_hard(self):
        runner = StubRunner(['{"trajectory": "progress", "confidence": 0.95, "new_goal_evidence": 0.8}'])
        judge = e2e.MainModelJudge(runner, max_calls=1)
        judge(STATE, OPTIONAL)
        with self.assertRaises(RuntimeError):
            judge(STATE, OPTIONAL)
        self.assertEqual(len(runner.prompts), 1)

    def test_oneshot_command_uses_the_main_route_without_plugins(self):
        command = e2e.oneshot_command("hermes", "PROMPT", "/tmp/u.json")
        self.assertEqual(command[0], "hermes")
        for flag in ("--safe-mode", "--ignore-rules"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("-m") + 1], "gpt-6-sol")
        self.assertEqual(command[command.index("--provider") + 1], "openai-codex")
        self.assertEqual(command[command.index("--reasoning") + 1], e2e.PLAN_REASONING)
        self.assertEqual(command[command.index("--usage-file") + 1], "/tmp/u.json")
        self.assertEqual(command[-2:], ["-z", "PROMPT"])
        self.assertNotIn("HERMES_HOME", " ".join(command))

    def test_a_scratch_hermes_home_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "HERMES_HOME"):
            e2e.check_real_hermes_home({"HERMES_HOME": "/tmp/scratch-home"}, home=Path("/home/u"))
        e2e.check_real_hermes_home({}, home=Path("/home/u"))
        e2e.check_real_hermes_home({"HERMES_HOME": "/home/u/.hermes"}, home=Path("/home/u"))


class JudgeExchangeTests(unittest.TestCase):
    def test_optional_questions_go_to_the_judge_and_not_to_jev(self):
        seen = []

        def inner(path, body, headers, *, allow_stale_retry):
            payload = json.loads(body)
            seen.append(payload)
            return json.dumps({"model": "m", "answers": {"operation": {"choice": "CLICK"}}}).encode(), None, None, False

        judge = e2e.MainModelJudge(
            StubRunner(['{"trajectory": "stagnant", "confidence": 0.9, "new_goal_evidence": 0.05}']), max_calls=4)
        exchange = e2e.JudgeExchange(inner, judge)
        body = json.dumps({"state": dict(STATE, recent_actions=[]),
                           "questions": dict(OPTIONAL, operation={"type": "choice", "criteria": {"CLICK": "c"}})}).encode()
        raw, status, _retry, _stale = exchange("/v1", body, {}, allow_stale_retry=True)
        self.assertIsNone(status)
        self.assertEqual(set(seen[0]["questions"]), {"operation"})
        self.assertNotIn("previous_page", seen[0]["state"])
        response = json.loads(raw)
        self.assertEqual(response["answers"]["trajectory"]["choice"], "stagnant")
        self.assertEqual(response["answers"]["operation"], {"choice": "CLICK"})
        self.assertEqual(len(judge.calls), 1)
        self.assertEqual(exchange.judge_steps, [len(exchange.bodies) - 1])

    def test_base_only_requests_pass_through(self):
        calls = []

        def inner(path, body, headers, *, allow_stale_retry):
            calls.append(body)
            return b"{}", None, None, False

        judge = e2e.MainModelJudge(StubRunner(["x"]), max_calls=1)
        exchange = e2e.JudgeExchange(inner, judge)
        body = json.dumps({"state": {"goal": "g"}, "questions": {"operation": {}}}).encode()
        exchange("/v1", body, {}, allow_stale_retry=False)
        self.assertEqual(calls, [body])
        self.assertEqual(judge.calls, [])

    def test_a_failed_jev_exchange_does_not_call_the_judge(self):
        def inner(path, body, headers, *, allow_stale_retry):
            return None, 429, "0", False

        judge = e2e.MainModelJudge(StubRunner(["x"]), max_calls=1)
        exchange = e2e.JudgeExchange(inner, judge)
        body = json.dumps({"state": STATE, "questions": dict(OPTIONAL, operation={})}).encode()
        self.assertEqual(exchange("/v1", body, {}, allow_stale_retry=False)[1], 429)
        self.assertEqual(judge.calls, [])


class APrimeArmTests(unittest.TestCase):
    def setUp(self):
        self.book, _ = evaluate.load_book()

    def case(self, kind, split="heldout"):
        return next(c for c in self.book["cases"] if c["kind"] == kind and c["split"] == split)

    def test_a_prime_stops_a_stall_on_confident_main_model_stalls(self):
        runner = StubRunner(['{"trajectory": "stagnant", "confidence": 0.95, "new_goal_evidence": 0.02, '
                             '"next_observation": "RETURN_INCOMPLETE"}'])
        judge = e2e.MainModelJudge(runner, max_calls=20)
        row = evaluate.run_jev_arm("A_prime", self.case("semantic_stall"), judge=judge)
        self.assertTrue(row["semantic_stop"])
        self.assertFalse(row["feature_questions_sent"])
        self.assertEqual(row["main_model_calls"], len(runner.prompts))
        self.assertGreater(row["main_model_calls"], 0)
        self.assertAlmostEqual(row["main_model_cost"], len(runner.prompts) * e2e.list_price_usd(USAGE))
        self.assertAlmostEqual(row["known_cost"], round(row["jev_cost"] + row["main_model_cost"], 10))
        self.assertEqual(row["recovery_suggestion"]["selected"], "RETURN_INCOMPLETE")
        self.assertEqual(row["task_latency_ms"], round(sum(step["latency_ms"] for step in row["steps"]), 3))

    def test_a_prime_does_not_stop_when_the_main_model_sees_progress(self):
        runner = StubRunner(['{"trajectory": "progress", "confidence": 0.95, "new_goal_evidence": 0.9}'])
        judge = e2e.MainModelJudge(runner, max_calls=20)
        row = evaluate.run_jev_arm("A_prime", self.case("semantic_stall"), judge=judge)
        self.assertFalse(row["semantic_stop"])

    def test_null_main_model_cost_is_counted_as_unknown(self):
        runner = StubRunner(['{"trajectory": "progress", "confidence": 0.95, "new_goal_evidence": 0.9}'],
                            usage=dict(USAGE, output_tokens=None))
        judge = e2e.MainModelJudge(runner, max_calls=20)
        row = evaluate.run_jev_arm("A_prime", self.case("semantic_stall"), judge=judge)
        self.assertEqual(row["main_model_unknown_cost_count"], len(runner.prompts))
        self.assertGreaterEqual(row["unknown_cost_count"], len(runner.prompts))

    def test_plan_cap_covers_every_possible_a_prime_judge_call(self):
        plan = e2e.load_plan()
        self.assertEqual(plan["a_prime"]["max_main_model_calls"], evaluate.a_prime_call_bound(self.book))


class LivePlanTests(unittest.TestCase):
    def test_live_order_is_interleaved_and_alternates_the_first_arm(self):
        plan = e2e.load_plan()
        order = plan["live"]["order"]
        self.assertEqual(len(order), 8)
        goals = [item["goal"] for item in order]
        self.assertEqual(goals[0::2], goals[1::2])
        self.assertEqual(sorted(set(goals)), ["live-1", "live-2", "live-3", "live-4"])
        firsts = [order[i]["arm"] for i in range(0, 8, 2)]
        self.assertEqual(firsts, ["B", "C", "B", "C"])
        self.assertEqual({(item["arm"]) for item in order}, {"B", "C"})
        self.assertEqual(plan["live"]["max_physical_requests"], 24)

    def test_pairs_are_run_whole_or_skipped_whole(self):
        order = e2e.load_plan()["live"]["order"]
        runs = []

        def run_one(item, cap):
            runs.append((item, cap))
            return {"goal": item["goal"], "arm": item["arm"], "physical_requests": cap}

        rows, used = e2e.run_live_pairs(order, run_one, total_cap=24, per_run_cap=4)
        self.assertEqual(used, 24)
        self.assertEqual(len(runs), 6)
        skipped = [row for row in rows if row.get("skipped")]
        self.assertEqual(len(skipped), 2)
        self.assertEqual(skipped[0]["goal"], skipped[1]["goal"])
        self.assertTrue(all(cap == 4 for _item, cap in runs))

    def test_live_pairs_use_actual_consumption(self):
        order = e2e.load_plan()["live"]["order"]
        rows, used = e2e.run_live_pairs(
            order, lambda item, cap: {"goal": item["goal"], "arm": item["arm"], "physical_requests": 2},
            total_cap=24, per_run_cap=4)
        self.assertEqual(used, 16)
        self.assertFalse(any(row.get("skipped") for row in rows))

    def test_response_costs_are_read_without_headers(self):
        recorder = e2e.ResponseCostRecorder(cap=2)

        def post(path, body, headers, *, allow_stale_retry):
            return json.dumps({"usage": {"cost": 0.0002}, "model": "m"}).encode(), None, None, False

        wrapped = recorder.wrap(post)
        wrapped("/v1", b"{}", {"Authorization": "secret"}, allow_stale_retry=False)
        self.assertEqual(recorder.records, [{"bytes": 2, "status": None, "cost": 0.0002}])
        wrapped("/v1", b"{}", {}, allow_stale_retry=False)
        with self.assertRaises(e2e.LiveCapReached):
            wrapped("/v1", b"{}", {}, allow_stale_retry=False)
        self.assertNotIn("secret", json.dumps(recorder.records))

    def test_missing_provider_cost_is_null(self):
        recorder = e2e.ResponseCostRecorder(cap=2)
        wrapped = recorder.wrap(lambda *a, **k: (b'{"usage": {}}', None, None, False))
        wrapped("/v1", b"{}", {}, allow_stale_retry=False)
        wrapped2 = recorder.wrap(lambda *a, **k: (None, 529, "1", False))
        wrapped2("/v1", b"{}", {}, allow_stale_retry=False)
        self.assertEqual([r["cost"] for r in recorder.records], [None, None])

    def test_jev_key_env_keeps_only_jev_key_names(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "helper.py"
            helper.write_text(
                "import sys\nprint('TYPESAFE_API_KEY=fixture-a')\nprint('OTHER_SECRET=nope')\nprint('OPENROUTER_API_KEY=')\n"
            )
            env = e2e.jev_key_env({}, helper=helper, scope="default")
            self.assertEqual(env, {"TYPESAFE_API_KEY": "fixture-a"})
            # A key already in the environment wins; the helper is not run.
            self.assertEqual(e2e.jev_key_env({"OPENROUTER_API_KEY": "set"}, helper=Path(tmp) / "absent.py",
                                             scope="default"), {})
            empty = Path(tmp) / "empty.py"
            empty.write_text("print('OTHER=1')\n")
            with self.assertRaisesRegex(RuntimeError, "jev_key_missing"):
                e2e.jev_key_env({}, helper=empty, scope="default")


class AcceptanceTests(unittest.TestCase):
    def row(self, case, *, completed=True, stop=False, suggestion="RETURN_INCOMPLETE", actions=3,
            latency=100.0, cost=0.001, unknown=0, label="productive"):
        return {"case": case, "label": label, "completed": completed, "semantic_stop": stop,
                "recovery_suggestion": {"selected": suggestion} if stop else None,
                "dispatched_actions": actions, "task_latency_ms": latency, "known_cost": cost,
                "unknown_cost_count": unknown, "steps": []}

    def arms(self, **overrides):
        c = [self.row("s1", completed=False, stop=True, label="semantic_stall", actions=2),
             self.row("p1")]
        b = [self.row("s1", completed=False, label="semantic_stall", actions=5), self.row("p1")]
        a = [self.row("s1", completed=False, label="semantic_stall", actions=5, latency=900, cost=0.01),
             self.row("p1", latency=900, cost=0.01)]
        arms = {"C": c, "B": b, "A_prime": a}
        arms.update(overrides)
        return arms

    def test_summary_reports_the_required_fields(self):
        summary = e2e.summarize_arm(self.arms()["C"])
        for key in ("cases", "completed", "labelled_stalls", "early_stops_with_suggestion", "premature_stops",
                    "dispatched_actions", "task_latency_p50_ms", "task_latency_total_ms",
                    "known_cost_usd", "null_cost_count"):
            self.assertIn(key, summary)
        self.assertEqual(summary["early_stops_with_suggestion"], ["s1"])

    def test_all_rules_pass_when_c_beats_both_baselines(self):
        result = e2e.acceptance(self.arms(), baselines=("A_prime", "B"))
        self.assertTrue(result["passed"], result)
        self.assertEqual(set(result["rules"]), {"outcome", "latency_p50", "latency_total", "cost"})

    def test_each_rule_fails_on_its_own(self):
        arms = self.arms()
        arms["C"][1]["completed"] = False
        self.assertFalse(e2e.acceptance(arms, baselines=("A_prime", "B"))["rules"]["outcome"]["passed"])
        arms = self.arms()
        arms["C"][1]["semantic_stop"] = True
        self.assertFalse(e2e.acceptance(arms, baselines=("A_prime", "B"))["rules"]["outcome"]["passed"])
        arms = self.arms()
        arms["C"][0]["recovery_suggestion"] = {"selected": None}
        self.assertFalse(e2e.acceptance(arms, baselines=("A_prime", "B"))["rules"]["outcome"]["passed"])
        arms = self.arms()
        arms["C"][1]["task_latency_ms"] = 1000
        rules = e2e.acceptance(arms, baselines=("A_prime", "B"))["rules"]
        self.assertFalse(rules["latency_total"]["passed"])
        arms = self.arms()
        arms["C"][1]["known_cost"] = 1.0
        self.assertFalse(e2e.acceptance(arms, baselines=("A_prime", "B"))["rules"]["cost"]["passed"])
        arms = self.arms()
        arms["C"][1]["unknown_cost_count"] = 1
        self.assertFalse(e2e.acceptance(arms, baselines=("A_prime", "B"))["rules"]["cost"]["passed"])

    def test_a_null_baseline_cost_also_fails_the_cost_rule(self):
        arms = self.arms()
        arms["B"][0]["unknown_cost_count"] = 1
        self.assertFalse(e2e.acceptance(arms, baselines=("A_prime", "B"))["rules"]["cost"]["passed"])

    def test_injected_fault_nulls_are_shown_and_still_fail_the_cost_rule(self):
        arms = self.arms()
        arms["C"][1]["unknown_cost_count"] = 1
        arms["C"][1]["steps"] = [{"faults": ["http_429"]}, {"faults": ["malformed_optional"]}]
        result = e2e.acceptance(arms, baselines=("A_prime", "B"))
        self.assertEqual(result["summaries"]["C"]["null_cost_from_injected_faults"], 1)
        self.assertFalse(result["rules"]["cost"]["passed"])


class FreezeTests(unittest.TestCase):
    def test_plan_pins_the_prompt_pricing_and_thresholds(self):
        plan = e2e.load_plan()
        self.assertEqual(plan["judge_prompt_sha256"], evaluate.sha256_bytes(e2e.JUDGE_PROMPT.read_bytes()))
        self.assertEqual(plan["pricing"], e2e.PRICING)
        self.assertEqual(plan["a_prime"]["reasoning"], e2e.PLAN_REASONING)
        self.assertEqual(plan["a_prime"]["model"], "gpt-6-sol")
        self.assertEqual(plan["a_prime"]["provider"], "openai-codex")
        self.assertEqual(plan["live"]["jev_provider"], "auto")
        self.assertIn("outcome", plan["acceptance"])

    def test_calls_refuse_when_the_frozen_files_are_not_committed(self):
        def dirty(_paths):
            return ["evaluation/browser_progress/e2e_plan.json"]

        with self.assertRaisesRegex(RuntimeError, "not committed"):
            e2e.require_frozen(dirty_paths=dirty)

    def test_calls_refuse_when_the_prompt_hash_changed(self):
        plan = dict(e2e.load_plan(), judge_prompt_sha256="0" * 64)
        with self.assertRaisesRegex(RuntimeError, "prompt"):
            e2e.require_frozen(plan=plan, dirty_paths=lambda _paths: [])

    def test_frozen_files_pass_when_clean(self):
        e2e.require_frozen(dirty_paths=lambda _paths: [])


if __name__ == "__main__":
    unittest.main()
