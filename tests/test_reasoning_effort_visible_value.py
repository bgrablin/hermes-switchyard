"""v0.5.5 visible value: default-on per-turn receipt, session summary, metadata-only fallback.

All values are synthetic. Fake Jev clients record the exact outbound state and questions, so
the tests can prove what would leave the process. Nothing here opens a network connection.
"""
from __future__ import annotations

import copy
import json
import socket
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_switchyard import egress_redaction
from hermes_switchyard.reasoning_effort_adapter import (
    ReasoningEffortController,
    clamp_effort_for_provider,
    last_receipt,
    parse_receipt_mode,
    persist_plugin_receipt_mode,
    register_reasoning_effort_adapter,
)

from tests.test_reasoning_effort_user_cap import OPUS, ORDER, Env, opus_request, sent_effort
from tests.test_reasoning_effort_v055 import SESSION, begin, make, send, tool

ROOT = Path(__file__).resolve().parent.parent

def setUpModule():
    egress_redaction._reset_for_tests(lambda text: text, loaded=True)

def tearDownModule():
    egress_redaction._reset_for_tests()



def finish(controller, text="Synthetic answer.", turn="t1", session=SESSION):
    hook = controller.build_transform_llm_output_hook()
    return hook(response_text=text, session_id=session, model=OPUS["model"], platform="cli", turn_id=turn)


def receipt_line(controller, turn="t1"):
    text = finish(controller, turn=turn)
    return None if text is None else text.splitlines()[-1]


def send_with_id(controller, effort, *, turn, request_id, session=SESSION):
    request = opus_request(effort)
    result = controller.on_llm_request(
        request, session_id=session, task_id=session, turn_id=turn, api_request_id=request_id, **OPUS
    )
    return sent_effort(request, result)


def usage(controller, request_id, *, reasoning=0, output=0, turn="t1", session=SESSION):
    controller.build_post_api_request_hook()(
        task_id=session, turn_id=turn, api_request_id=request_id, session_id=session,
        model=OPUS["model"], provider=OPUS["provider"], api_mode=OPUS["api_mode"],
        usage={"input_tokens": 10, "output_tokens": output, "reasoning_tokens": reasoning,
               "cache_read_tokens": 0, "cache_write_tokens": 0, "request_count": 1},
    )


class ShapeJev:
    """Fake Jev for metadata-only asks: decides from the closed-set request shape alone.

    A short single-line request without code, URL, or path is routine; anything else keeps the
    highest candidate. It records every outbound payload.
    """

    def __init__(self, *, pick_highest: bool = False) -> None:
        self.calls: list[dict] = []
        self.pick_highest = pick_highest

    def decide(self, state, questions, *, public_or_sanitized_data_ack=True):
        self.calls.append({"state": json.loads(json.dumps(state)), "questions": json.loads(json.dumps(questions))})
        levels = list(questions["reasoning_effort"]["criteria"])
        shape = state.get("request_shape") or {}
        routine = (
            shape.get("chars") in {"1-16", "17-64"}
            and shape.get("lines") == "1"
            and not (shape.get("has_code_fence") or shape.get("has_url") or shape.get("has_file_path"))
        )
        streak = int(state.get("routine_success_streak") or 0)
        pick = levels[-1] if self.pick_highest or not (routine or streak >= 2) else levels[0]
        return {"answers": {
            "reasoning_effort": {"choice": pick, "confidence": 0.9,
                                 "probabilities": {level: 1.0 if level == pick else 0.0 for level in levels}},
            "stakes": {"noul": 0.0},
        }}


CHAR_BUCKETS = ("1-16", "17-64", "65-256", "257-1024", "1025+")
LINE_BUCKETS = ("1", "2-3", "4-10", "11+")
STATE_KEYS = {"request_shape", "turn_index", "turn_phase", "recent_tool_statuses", "latest_tool_failed"}


def _substrings(text: str, size: int = 4):
    return {text[index:index + size] for index in range(len(text) - size + 1)}


# -- 1. per-turn receipt, visible by default ------------------------------------------------


class DefaultReceiptTests(unittest.TestCase):
    def test_receipt_line_is_on_by_default_everywhere(self):
        controller = ReasoningEffortController()
        self.assertTrue(controller.receipt_line)
        self.assertEqual(controller.receipt_mode, "auto")
        manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        self.assertRegex(manifest, r'adaptive_reasoning_effort_receipt_mode: \{type: str, default: "auto",')
        source = (ROOT / "hermes_switchyard" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('"adaptive_reasoning_effort_receipt_mode", default=None', source)

        class Ctx:
            def register_middleware(self, *_a, **_k):
                pass

        receipt = register_reasoning_effort_adapter(Ctx(), client_factory=None)
        self.assertTrue(receipt["settings"]["receipt_line"])
        self.assertEqual(receipt["settings"]["receipt_mode"], "auto")

    def test_lowered_turn_names_the_change_and_latency_without_a_baseline(self):
        controller, _, _ = make()
        begin(controller, "status ping")
        self.assertEqual(send(controller, "high"), "low")
        line = receipt_line(controller)
        self.assertRegex(line, r"^Reasoning: high→low · \d+ ms$")
        self.assertNotIn("saved", line, "no measured baseline yet: no saved figure")

    def test_kept_consequential_turn_says_kept(self):
        controller, _, _ = make()
        begin(controller, "delete the prod database backups")
        self.assertEqual(send(controller, "high"), "high")
        self.assertRegex(receipt_line(controller), r"^Reasoning: kept at high — consequential request · \d+ ms$")

    def test_kept_routine_level_turn_says_kept(self):
        controller, _, _ = make()
        begin(controller, "refactor the parser module")
        self.assertEqual(send(controller, "high"), "high")
        self.assertRegex(receipt_line(controller), r"^Reasoning: kept at high — cloud decision · \d+ ms$")

    def test_pinned_turn_did_no_work_and_stays_quiet(self):
        controller, jev, _ = make()
        controller.set_mode("pinned", session_id=SESSION)
        begin(controller, "hi")
        send(controller, "high")
        self.assertEqual(jev.calls, [])
        self.assertIsNone(finish(controller))

    def test_cached_reuse_in_a_later_request_is_named(self):
        controller, jev, _ = make()
        begin(controller, "status ping")
        send(controller, "high")
        tool(controller, "terminal")  # a non-routine round: no step ask, the turn choice is reused
        send(controller, "high")
        self.assertEqual(len(jev.calls), 1)
        self.assertRegex(receipt_line(controller), r"^Reasoning: high→low · \d+ ms · 1 cached$")

    def test_off_switches_still_work(self):
        controller, _, _ = make(receipt_line=False)
        begin(controller, "hi")
        send(controller, "high")
        self.assertIsNone(finish(controller))
        controller, _, _ = make()
        self.assertIn("off", controller.handle_command("effort receipt off"))
        begin(controller, "hi")
        send(controller, "high")
        self.assertIsNone(finish(controller))

    def test_delegated_child_turn_gets_no_line_by_default(self):
        controller, _, _ = make()
        begin(controller, "hi", task="subagent-0-synthetic", turn="c1", parent=SESSION)
        send(controller, "high", task="subagent-0-synthetic", turn="c1")
        self.assertIsNone(finish(controller, turn="c1"))

    def test_transform_hook_fails_open(self):
        controller, _, _ = make()
        begin(controller, "hi")
        send(controller, "high")
        with patch.object(controller, "turn_receipt_line", side_effect=RuntimeError("synthetic")):
            self.assertIsNone(finish(controller))
        self.assertIsNone(controller.build_transform_llm_output_hook()(response_text=None, turn_id="t1"))


class ReceiptReplayGuardTests(unittest.TestCase):
    """Hermes stores the transformed reply; the model must never get the receipt line back."""

    LINE = "Reasoning: high→low · local decision"

    def _replay_request(self, earlier: object, *, key: str = "messages") -> dict:
        request = opus_request("high")
        request[key] = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": earlier},
            {"role": "user", "content": "refactor the parser module"},
        ]
        return request

    def _assistant(self, request: dict, result: dict | None, key: str = "messages"):
        final = result["request"] if result else request
        return [item["content"] for item in final[key] if item.get("role") == "assistant"]

    def test_every_receipt_shape_the_hook_makes_is_removed(self):
        for line in (
            self.LINE,
            "Reasoning: high→low · 180 ms · ~1.2k reasoning tokens saved (est.)",
            "Reasoning: kept at high — consequential request · 210 ms",
            "Reasoning: kept at high — cloud over 400 ms budget",
            "Reasoning: high→low→medium · 2 decisions, 390 ms · 1 cached · shape only (message text not sent)",
        ):
            with self.subTest(line=line):
                controller, _, _ = make()
                begin(controller, "refactor the parser module", turn="t2")
                request = self._replay_request(f"Synthetic answer.\n\n{line}")
                result = controller.on_llm_request(request, session_id=SESSION, task_id=SESSION, turn_id="t2", **OPUS)
                self.assertEqual(self._assistant(request, result), ["Synthetic answer."])
                # The caller's request object is not changed in place.
                self.assertIn(line, request["messages"][1]["content"])

    def test_the_hook_output_round_trips_to_the_clean_answer(self):
        controller, _, _ = make()
        begin(controller, "hi")
        send(controller, "high")
        shown = finish(controller)
        self.assertTrue(shown.endswith(self.LINE), shown)
        begin(controller, "refactor the parser module", turn="t2")
        request = self._replay_request(shown)
        result = controller.on_llm_request(request, session_id=SESSION, task_id=SESSION, turn_id="t2", **OPUS)
        self.assertEqual(self._assistant(request, result), ["Synthetic answer."])

    def test_list_content_and_responses_input_are_cleaned(self):
        controller, _, _ = make()
        begin(controller, "refactor the parser module", turn="t2")
        parts = [{"type": "output_text", "text": f"Synthetic answer.\n\n{self.LINE}"}]
        request = self._replay_request(parts, key="input")
        request.pop("messages", None)
        result = controller.on_llm_request(request, session_id=SESSION, task_id=SESSION, turn_id="t2", **OPUS)
        self.assertEqual(self._assistant(request, result, key="input"),
                         [[{"type": "output_text", "text": "Synthetic answer."}]])

    def test_other_text_is_never_changed(self):
        for text in (
            "Reasoning: high→low · local decision",  # no blank line before it
            f"Quote:\n\n{self.LINE}\n\nThen more text.",  # not at the end
            "The Reasoning: high setting is described here.",
            "Synthetic answer.\n\nReasoning: HIGH",  # not the receipt shape
            # Closed-set strip must not delete ordinary assistant prose that happens to
            # start with Reasoning: after a blank line.
            "Synthetic answer.\n\nReasoning: high→low · therefore use the lower-cost implementation",
            "Synthetic answer.\n\nReasoning: kept at high — use a cheaper plan next",
        ):
            with self.subTest(text=text):
                controller, _, _ = make()
                begin(controller, "refactor the parser module", turn="t2")
                request = self._replay_request(text)
                result = controller.on_llm_request(request, session_id=SESSION, task_id=SESSION, turn_id="t2", **OPUS)
                self.assertEqual(self._assistant(request, result), [text])

    def test_user_turns_are_never_changed(self):
        controller, _, _ = make()
        begin(controller, "refactor the parser module", turn="t2")
        request = self._replay_request("Synthetic answer.")
        request["messages"][0]["content"] = f"hi\n\n{self.LINE}"
        result = controller.on_llm_request(request, session_id=SESSION, task_id=SESSION, turn_id="t2", **OPUS)
        final = result["request"] if result else request
        self.assertEqual(final["messages"][0]["content"], f"hi\n\n{self.LINE}")

    def test_cleaning_also_runs_when_effort_is_off_or_pinned(self):
        for kwargs in ({"enabled": False}, {}):
            with self.subTest(kwargs=kwargs):
                controller, _, env = make(**kwargs)
                if not kwargs:
                    controller.handle_command("effort pin")
                begin(controller, "refactor the parser module", turn="t2")
                request = self._replay_request(f"Synthetic answer.\n\n{self.LINE}")
                result = controller.on_llm_request(request, session_id=SESSION, task_id=SESSION, turn_id="t2", **OPUS)
                self.assertIsNotNone(result)
                self.assertEqual(result["reason"], "receipt_line_removed")
                self.assertEqual(self._assistant(request, result), ["Synthetic answer."])
                self.assertEqual(sent_effort(request, result), "high")

    def test_a_guard_error_sends_the_request_unchanged(self):
        controller, _, _ = make()
        begin(controller, "refactor the parser module", turn="t2")
        request = self._replay_request(f"Synthetic answer.\n\n{self.LINE}")
        with patch("hermes_switchyard.reasoning_effort_adapter.strip_receipt_lines", side_effect=RuntimeError("x")):
            result = controller.on_llm_request(request, session_id=SESSION, task_id=SESSION, turn_id="t2", **OPUS)
        self.assertEqual(sent_effort(request, result), "high")



class ReceiptModeAndPlainLanguageTests(unittest.TestCase):
    def test_legacy_switchyard_receipt_shape_is_still_stripped(self):
        legacy = "switchyard: effort high→low · local (no Jev call)"
        controller, _, _ = make()
        begin(controller, "refactor the parser module", turn="t2")
        request = opus_request("high")
        request["messages"] = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": f"Synthetic answer.\n\n{legacy}"},
            {"role": "user", "content": "refactor the parser module"},
        ]
        result = controller.on_llm_request(request, session_id=SESSION, task_id=SESSION, turn_id="t2", **OPUS)
        final = result["request"] if result else request
        assistant = [item["content"] for item in final["messages"] if item.get("role") == "assistant"]
        self.assertEqual(assistant, ["Synthetic answer."])

    def test_always_mode_shows_pinned_pass_through(self):
        controller, jev, _ = make()
        mode, _saved = controller.set_receipt_mode("always", persist=False)
        self.assertEqual(mode, "always")
        controller.set_mode("pinned", session_id=SESSION)
        begin(controller, "hi")
        send(controller, "high")
        self.assertEqual(jev.calls, [])
        line = finish(controller)
        self.assertEqual(line.splitlines()[-1], "Reasoning: high · pinned")

    def test_legacy_work_alias_maps_to_auto(self):
        controller, _, _ = make()
        mode, _ = controller.set_receipt_mode("work", persist=False)
        self.assertEqual(mode, "auto")
        reply = controller.handle_command("effort receipt work")
        self.assertIn("Reasoning receipt: auto", reply)
        self.assertIn("alias of auto", reply)
        self.assertEqual(controller.receipt_mode, "auto")

    def test_auto_receipt_mode_stays_quiet_when_pinned(self):
        controller, _, _ = make()
        controller.set_receipt_mode("auto", persist=False)
        controller.set_mode("pinned", session_id=SESSION)
        begin(controller, "hi")
        send(controller, "high")
        self.assertIsNone(finish(controller))

    def test_command_persists_receipt_mode_when_config_available(self):
        controller, _, _ = make()
        with patch(
            "hermes_switchyard.reasoning_effort_adapter.persist_plugin_receipt_mode",
            return_value=True,
        ) as persist:
            reply = controller.handle_command("effort receipt always")
        persist.assert_called_once_with("always")
        self.assertIn("always", reply)
        self.assertIn("Saved in plugin settings", reply)
        self.assertEqual(controller.receipt_mode, "always")

    def test_status_leading_line_is_plain_language(self):
        controller, _, _ = make()
        begin(controller, "status ping")
        send(controller, "high")
        text = controller.handle_command("effort status")
        lead = "Cap high · last sent low · auto · why: cloud decision"
        self.assertIn(lead, text)
        self.assertEqual(text.count(lead), 1, "status must not duplicate the Cap lead via summary")
        self.assertNotIn("jev_selected", text)
        summary = controller.handle_command("effort summary")
        self.assertIn(lead, summary)
        self.assertEqual(summary.count(lead), 1)

    def test_always_mode_stays_quiet_without_a_known_wire_level(self):
        """always covers pinned/pass-through with a level; not no_host_effort / unsupported_route."""
        controller, _, _ = make()
        controller.set_receipt_mode("always", persist=False)
        begin(controller, "hi")
        request = {"model": OPUS["model"], "messages": [{"role": "user", "content": "hi"}]}
        result = controller.on_llm_request(
            request, session_id=SESSION, task_id=SESSION, turn_id="t1", **OPUS
        )
        self.assertIsNone(result)  # no effort field → no_host_effort, request unchanged
        self.assertEqual(last_receipt()["reason_code"], "no_host_effort")
        self.assertIsNone(finish(controller))
        begin(controller, "hi", turn="t2")
        result = controller.on_llm_request(
            {"modelId": "model", "messages": []},
            session_id=SESSION,
            task_id=SESSION,
            turn_id="t2",
            provider="bedrock",
            api_mode="bedrock_converse",
            model="model",
        )
        self.assertIsNone(result)
        self.assertEqual(last_receipt()["reason_code"], "unsupported_route")
        self.assertIsNone(finish(controller, turn="t2"))


class PersistReceiptModeTests(unittest.TestCase):
    """Direct coverage for persist_plugin_receipt_mode (settings / legacy config / failures)."""

    def _install(self, holder):
        def load_config():
            return copy.deepcopy(holder["config"])

        def save_config(config):
            holder["config"] = copy.deepcopy(config)
            holder["saves"].append(copy.deepcopy(config))

        fake = SimpleNamespace(load_config=load_config, save_config=save_config)
        return patch.dict(sys.modules, {"hermes_cli.config": fake, "hermes_cli": SimpleNamespace(config=fake)})

    def test_writes_settings_and_syncs_legacy_bool(self):
        holder = {
            "config": {"plugins": {"entries": {"hermes-switchyard": {"settings": {}}}}},
            "saves": [],
        }
        with self._install(holder):
            self.assertTrue(persist_plugin_receipt_mode("always"))
        settings = holder["config"]["plugins"]["entries"]["hermes-switchyard"]["settings"]
        self.assertEqual(settings["adaptive_reasoning_effort_receipt_mode"], "always")
        self.assertIs(settings["adaptive_reasoning_effort_receipt_line"], True)
        with self._install(holder):
            self.assertTrue(persist_plugin_receipt_mode("off"))
        settings = holder["config"]["plugins"]["entries"]["hermes-switchyard"]["settings"]
        self.assertEqual(settings["adaptive_reasoning_effort_receipt_mode"], "off")
        self.assertIs(settings["adaptive_reasoning_effort_receipt_line"], False)

    def test_seeds_settings_from_legacy_config_key(self):
        holder = {
            "config": {
                "plugins": {
                    "entries": {
                        "hermes_switchyard": {
                            "config": {"adaptive_reasoning_effort_receipt_line": True},
                        }
                    }
                }
            },
            "saves": [],
        }
        with self._install(holder):
            self.assertTrue(persist_plugin_receipt_mode("auto"))
        entry = holder["config"]["plugins"]["entries"]["hermes_switchyard"]
        self.assertIn("settings", entry)
        self.assertEqual(entry["settings"]["adaptive_reasoning_effort_receipt_mode"], "auto")
        self.assertIs(entry["settings"]["adaptive_reasoning_effort_receipt_line"], True)

    def test_creates_missing_plugin_entry(self):
        holder = {"config": {}, "saves": []}
        with self._install(holder):
            self.assertTrue(persist_plugin_receipt_mode("always"))
        entry = holder["config"]["plugins"]["entries"]["hermes-switchyard"]
        self.assertEqual(entry["settings"]["adaptive_reasoning_effort_receipt_mode"], "always")

    def test_malformed_config_or_entry_returns_false(self):
        for bad in (None, [], {"plugins": "nope"}, {"plugins": {"entries": "nope"}},
                    {"plugins": {"entries": {"hermes-switchyard": "nope"}}}):
            with self.subTest(bad=bad):
                holder = {"config": bad, "saves": []}
                with self._install(holder):
                    self.assertFalse(persist_plugin_receipt_mode("always"))

    def test_save_failure_returns_false(self):
        def load_config():
            return {"plugins": {"entries": {"hermes-switchyard": {"settings": {}}}}}

        def save_config(_config):
            raise OSError("disk full")

        fake = SimpleNamespace(load_config=load_config, save_config=save_config)
        with patch.dict(sys.modules, {"hermes_cli.config": fake, "hermes_cli": SimpleNamespace(config=fake)}):
            self.assertFalse(persist_plugin_receipt_mode("always"))

    def test_reload_mismatch_returns_false(self):
        calls = {"n": 0}

        def load_config():
            calls["n"] += 1
            if calls["n"] == 1:
                return {"plugins": {"entries": {"hermes-switchyard": {"settings": {}}}}}
            # Reload returns a stale mode so verification fails.
            return {
                "plugins": {
                    "entries": {
                        "hermes-switchyard": {
                            "settings": {"adaptive_reasoning_effort_receipt_mode": "off"}
                        }
                    }
                }
            }

        def save_config(_config):
            return None

        fake = SimpleNamespace(load_config=load_config, save_config=save_config)
        with patch.dict(sys.modules, {"hermes_cli.config": fake, "hermes_cli": SimpleNamespace(config=fake)}):
            self.assertFalse(persist_plugin_receipt_mode("always"))

    def test_missing_hermes_cli_returns_false(self):
        # None in sys.modules makes "import hermes_cli.config" raise ImportError.
        with patch.dict(sys.modules, {"hermes_cli": None, "hermes_cli.config": None}):
            self.assertFalse(persist_plugin_receipt_mode("always"))

    def test_parse_receipt_mode_aliases(self):
        self.assertEqual(parse_receipt_mode("work"), "auto")
        self.assertEqual(parse_receipt_mode("on"), "auto")
        self.assertEqual(parse_receipt_mode("changes"), "auto")
        self.assertEqual(parse_receipt_mode(True), "auto")
        self.assertEqual(parse_receipt_mode(False), "off")
        self.assertEqual(parse_receipt_mode(None), "auto")
        self.assertEqual(parse_receipt_mode("ALWAYS"), "always")


class SavedEstimateTests(unittest.TestCase):

    """The saved figure uses only measured usage: baseline requests at the user's level."""

    def _baseline(self, controller, values, *, metric="reasoning"):
        for index, value in enumerate(values):
            turn = f"b{index}"
            begin(controller, "refactor the parser module", turn=turn)
            self.assertEqual(send_with_id(controller, "high", turn=turn, request_id=f"{turn}:api:1"), "high")
            if metric == "reasoning":
                usage(controller, f"{turn}:api:1", reasoning=value, output=50, turn=turn)
            else:
                usage(controller, f"{turn}:api:1", reasoning=0, output=value, turn=turn)
            finish(controller, turn=turn)

    def test_saved_reasoning_tokens_use_the_measured_median_baseline(self):
        controller, _, _ = make()
        self._baseline(controller, [1500, 1300, 1400])
        begin(controller, "status ping", turn="t9")
        self.assertEqual(send_with_id(controller, "high", turn="t9", request_id="t9:api:1"), "low")
        usage(controller, "t9:api:1", reasoning=200, output=20, turn="t9")
        self.assertRegex(
            receipt_line(controller, turn="t9"),
            r"^Reasoning: high→low · \d+ ms · ~1\.2k reasoning tokens saved \(est\.\)$",
        )

    def test_output_tokens_basis_when_the_provider_reports_no_reasoning_tokens(self):
        controller, _, _ = make()
        self._baseline(controller, [900, 700, 800], metric="output")
        begin(controller, "status ping", turn="t9")
        send_with_id(controller, "high", turn="t9", request_id="t9:api:1")
        usage(controller, "t9:api:1", reasoning=0, output=300, turn="t9")
        self.assertRegex(receipt_line(controller, turn="t9"), r" · ~500 output tokens saved \(est\.\)$")

    def test_too_few_baseline_samples_or_no_usage_omits_the_figure(self):
        controller, _, _ = make()
        self._baseline(controller, [1500, 1300])  # 2 samples: below the minimum
        begin(controller, "status ping", turn="t9")
        send_with_id(controller, "high", turn="t9", request_id="t9:api:1")
        usage(controller, "t9:api:1", reasoning=200, turn="t9")
        self.assertNotIn("saved", receipt_line(controller, turn="t9"))
        controller, _, _ = make()
        self._baseline(controller, [1500, 1300, 1400])
        begin(controller, "status ping", turn="t9")
        send_with_id(controller, "high", turn="t9", request_id="t9:api:1")  # no usage arrives
        self.assertNotIn("saved", receipt_line(controller, turn="t9"))

    def test_no_saving_is_reported_as_none_not_hidden(self):
        controller, _, _ = make()
        self._baseline(controller, [300, 300, 300])
        begin(controller, "status ping", turn="t9")
        send_with_id(controller, "high", turn="t9", request_id="t9:api:1")
        usage(controller, "t9:api:1", reasoning=400, turn="t9")
        self.assertRegex(receipt_line(controller, turn="t9"), r" · no reasoning tokens saved \(est\.\)$")


# -- 2. session summary ---------------------------------------------------------------------


class SessionSummaryTests(unittest.TestCase):
    def _session(self):
        controller, jev, env = make()
        begin(controller, "status ping", turn="t1")
        send(controller, "high", turn="t1")
        tool(controller, "terminal")
        send(controller, "high", turn="t1")  # cached reuse at low
        begin(controller, "delete the prod database backups", turn="t2")
        send(controller, "high", turn="t2")
        begin(controller, "refactor the parser module", turn="t3")
        send(controller, "high", turn="t3")
        return controller, jev, env

    def test_summary_counts_turns_requests_jev_and_latency(self):
        controller, jev, _ = self._session()
        summary = controller.session_status()["summary"]
        self.assertEqual(summary["turns"], 3)
        self.assertEqual((summary["lowered"], summary["kept"], summary["raised"]), (2, 2, 0))
        self.assertEqual(summary["jev_calls"], len(jev.calls))
        self.assertEqual(summary["cached_reuses"], 1)
        self.assertIsInstance(summary["jev_p50_ms"], (int, float))
        self.assertIsInstance(summary["jev_p95_ms"], (int, float))
        self.assertIsNone(summary["tokens_saved_est"])
        text = controller.handle_command("effort summary")
        self.assertIn("Switchyard effort summary (this session)", text)
        self.assertIn("turns: 3", text)
        self.assertIn("requests: lowered 2, kept 2, raised 0", text)
        self.assertRegex(text, r"cloud decisions: 3, p50 \d+ ms, p95 \d+ ms")
        self.assertIn("cached reuses: 1", text)
        self.assertIn("estimated tokens saved: unknown", text)
        self.assertIn("Switchyard effort summary (this session)", controller.handle_command("effort status"))

    def test_summary_before_any_request_is_empty_not_an_error(self):
        controller, _, _ = make()
        text = controller.handle_command("effort summary")
        self.assertIn("no model request yet", text)
        self.assertTrue(controller.handle_command("effort summary extra").startswith("Usage:"))

    def test_status_and_summary_make_no_network_call(self):
        controller, _, _ = self._session()
        factory_calls: list[int] = []
        controller.client_factory = lambda: factory_calls.append(1)

        def refuse(*_args, **_kwargs):
            raise AssertionError("network attempted while producing status or summary")

        with patch.object(socket, "socket", side_effect=refuse), \
                patch.object(socket, "create_connection", side_effect=refuse), \
                patch.object(socket, "getaddrinfo", side_effect=refuse), \
                patch("hermes_switchyard.client.DecisionClient", side_effect=refuse):
            status = controller.session_status()
            status_text = controller.handle_command("effort status")
            summary_text = controller.handle_command("effort summary")
        self.assertEqual(factory_calls, [])
        self.assertTrue(status["known"])
        self.assertIn("turns: 3", status_text)
        self.assertIn("turns: 3", summary_text)


# -- 3. metadata-only fallback on older Hermes ---------------------------------------------


class MetadataOnlyTests(unittest.TestCase):
    MESSAGES = (
        "hello there",
        "use zq9Xk2Lm8Pw3Rt7Yv4Hs4Nc6Bd for the lookup",
        "ghp_" + "Zx81Qw7Er6Ty5Ui4Op3As2Df1Gh0Jk9Lz8Xc7",
        "What did Wendy Albright say about the quarterly figures?",
        "why is\n```\nSYNTHETIC_TRACE line 12\n```\nslow at https://example.invalid/a/b and /srv/app/main.py",
        "Can you explain\nthe difference between\nthese two approaches\nin detail\nwith examples\nplease",
    )

    def setUp(self):
        egress_redaction._reset_for_tests(None, loaded=True)
        self.addCleanup(lambda: egress_redaction._reset_for_tests(lambda text: text, loaded=True))

    def _make(self, jev=None, **kwargs):
        jev = jev or ShapeJev()
        controller = ReasoningEffortController(
            client_factory=lambda: jev, session_env=Env(HERMES_SESSION_ID=SESSION), **kwargs
        )
        return controller, jev

    def _outbound(self, text, turn="t1"):
        controller, jev = self._make()
        begin(controller, text, turn=turn)
        send(controller, "high", turn=turn)
        return jev

    def test_no_user_text_substring_leaves_the_process(self):
        # Control payload from a 1-character message: it carries the fixed vocabulary (keys,
        # instructions, closed-set labels) and cannot contain any 4-character user substring.
        control = self._outbound("q")
        vocabulary = json.dumps(control.calls, ensure_ascii=False) + " ".join(
            (*CHAR_BUCKETS, *LINE_BUCKETS)
        )
        for index, text in enumerate(self.MESSAGES):
            with self.subTest(text=text[:20]):
                jev = self._outbound(text, turn=f"t{index}")
                self.assertEqual(len(jev.calls), 1, "metadata-only must still ask Jev")
                outbound = json.dumps(jev.calls, ensure_ascii=False)
                leaked = sorted(
                    part for part in _substrings(text)
                    if part.strip() and part in outbound and part not in vocabulary
                )
                self.assertEqual(leaked, [], f"user text reached Jev: {leaked[:5]}")
                # The questions do not depend on the message at all.
                self.assertEqual(jev.calls[0]["questions"], control.calls[0]["questions"])
                state = jev.calls[0]["state"]
                self.assertNotIn("current_request", state)
                self.assertEqual(set(state), STATE_KEYS)
                for key, value in state["request_shape"].items():
                    if key == "chars":
                        self.assertIn(value, CHAR_BUCKETS)
                    elif key == "lines":
                        self.assertIn(value, LINE_BUCKETS)
                    else:
                        self.assertIsInstance(value, bool)
                self.assertIsInstance(state["turn_index"], int)
                self.assertEqual(last_receipt()["scan_reason"], "metadata_only")

    def test_short_greeting_can_lower_and_long_multiline_keeps_the_cap(self):
        controller, jev = self._make()
        begin(controller, "hello there", turn="t1")
        self.assertEqual(send(controller, "high", turn="t1"), "low")
        begin(controller, self.MESSAGES[-1], turn="t2")
        self.assertEqual(send(controller, "high", turn="t2"), "high")
        shapes = [call["state"]["request_shape"] for call in jev.calls]
        self.assertEqual((shapes[0]["chars"], shapes[0]["lines"]), ("1-16", "1"))
        self.assertEqual(shapes[1]["lines"], "4-10")
        self.assertEqual([call["state"]["turn_index"] for call in jev.calls], [1, 2])

    def test_metadata_only_shape_fields_are_closed_set(self):
        controller, jev = self._make()
        begin(controller, self.MESSAGES[4])
        send(controller, "high")
        shape = jev.calls[0]["state"]["request_shape"]
        self.assertEqual(
            set(shape), {"chars", "lines", "has_code_fence", "has_url", "has_file_path", "has_question_mark"}
        )
        self.assertTrue(shape["has_code_fence"] and shape["has_url"] and shape["has_file_path"])
        self.assertFalse(shape["has_question_mark"])
        self.assertIn(shape["chars"], CHAR_BUCKETS)
        self.assertIn(shape["lines"], LINE_BUCKETS)

    def test_change_request_keeps_the_cap_without_a_jev_call(self):
        controller, jev = self._make()
        begin(controller, "fix it")
        self.assertEqual(send(controller, "high"), "high")
        self.assertEqual(jev.calls, [])
        self.assertEqual(last_receipt()["reason_code"], "kept_requested_metadata_change_request")

    def test_redactor_error_and_marker_paths_also_go_metadata_only(self):
        def boom(_text):
            raise RuntimeError("synthetic")

        for redactor in (boom, lambda _t: "[redaction-unavailable]", lambda _t: None):
            with self.subTest(redactor=redactor):
                egress_redaction._reset_for_tests(redactor, loaded=True)
                controller, jev = self._make()
                begin(controller, "hello there")
                self.assertEqual(send(controller, "high"), "low")
                self.assertEqual(len(jev.calls), 1)
                self.assertNotIn("current_request", jev.calls[0]["state"])
                self.assertNotIn("hello", json.dumps(jev.calls))
                self.assertEqual(last_receipt()["scan_reason"], "metadata_only")

    def test_restricted_marking_still_stays_local(self):
        controller, jev = self._make()
        begin(controller, "Company confidential summary of the program")
        self.assertEqual(send(controller, "high"), "high")
        self.assertEqual(jev.calls, [])

    def test_cap_is_never_exceeded(self):
        # The cap is the user's level on the provider wire (Anthropic has no xhigh: it sends max).
        for level in ("low", "medium", "high", "xhigh", "max"):
            with self.subTest(level=level):
                cap = clamp_effort_for_provider(level, provider=OPUS["provider"], model=OPUS["model"],
                                                api_mode=OPUS["api_mode"])
                controller, jev = self._make(ShapeJev(pick_highest=True))
                begin(controller, "hello there")
                sent = send(controller, level)
                self.assertLessEqual(ORDER.index(sent), ORDER.index(cap))
                for call in jev.calls:
                    self.assertLessEqual(
                        max(ORDER.index(candidate) for candidate in call["questions"]["reasoning_effort"]["criteria"]),
                        ORDER.index(cap),
                    )

    def test_allow_raise_is_the_only_way_above_the_cap(self):
        controller, jev = self._make(ShapeJev(pick_highest=True), allow_raise=True)
        begin(controller, "hello there")
        send(controller, "medium")
        tool(controller, "terminal", ok=False)
        sent = send(controller, "medium")
        self.assertLessEqual(ORDER.index(sent), ORDER.index("medium") + 1)
        controller, jev = self._make(ShapeJev(pick_highest=True), allow_raise=False)
        begin(controller, "hello there")
        send(controller, "medium")
        tool(controller, "terminal", ok=False)
        self.assertEqual(send(controller, "medium"), "medium")

    def test_step_ask_uses_tool_metadata_without_text(self):
        controller, jev = self._make()
        begin(controller, self.MESSAGES[-1].replace("explain", "walk through"))
        self.assertEqual(send(controller, "high"), "high")
        for _ in range(3):
            tool(controller, "read_file")
            sent = send(controller, "high")
        self.assertEqual(sent, "medium")
        self.assertEqual(len(jev.calls), 2)
        step = jev.calls[-1]["state"]
        self.assertEqual((step["recent_tool_kinds"], step["routine_success_streak"]), (["read"], 3))
        self.assertNotIn("current_request", step)
        self.assertEqual(list(jev.calls[-1]["questions"]["reasoning_effort"]["criteria"]), ["medium", "high"])

    def test_local_high_stakes_words_keep_the_cap_without_a_jev_call(self):
        controller, jev = self._make()
        begin(controller, "is prod ok?")
        self.assertEqual(send(controller, "high"), "high")
        self.assertEqual(jev.calls, [])
        self.assertEqual(last_receipt()["reason_code"], "kept_requested_metadata_high_stakes")

    def test_metadata_only_receipt_line_is_labeled(self):
        controller, _ = self._make()
        begin(controller, "hello there")
        send(controller, "high")
        self.assertRegex(receipt_line(controller), r"^Reasoning: high→low · \d+ ms · shape only \(message text not sent\)$")


class TextPathUnchangedTests(unittest.TestCase):
    """With a redactor, the text path and its state keys are unchanged."""

    def test_text_path_still_sends_current_request(self):
        controller, jev, _ = make()
        begin(controller, "status ping")
        send(controller, "high")
        self.assertEqual(jev.calls[0]["state"]["current_request"], "status ping")
        self.assertNotIn("request_shape", jev.calls[0]["state"])
        self.assertIsNone(last_receipt().get("scan_reason"))


if __name__ == "__main__":
    unittest.main()
