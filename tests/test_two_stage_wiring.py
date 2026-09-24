"""Integration tests for the issue #94 wiring patch (automatic.py hook path).

Synthetic DecisionClient transports only. No network or private text.
"""
from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from unittest import mock

import yaml

from hermes_switchyard import receipt_history, receipt_state
from hermes_switchyard.automatic import build_pre_llm_call_hook
from test_support import HermesHomeTestCase
from hermes_switchyard.client import EXPECTED_MODEL, DecisionClient
from hermes_switchyard.two_stage_routing import (
    HOSTED_DETAIL_DESCRIPTIONS,
    SHORTLIST_POLICY_TWO_STAGE,
    TwoStageConfig,
)

NONE = "__jev_none_of_these__"
MARKER = "SYNTHETIC_CANDIDATE_DESCRIPTION_MARKER"


def catalog():
    rows = [{"name": f"filler-skill-{i:03d}", "description": f"{MARKER} filler {i}"} for i in range(403)]
    rows[350] = {"name": "songsee", "description": f"{MARKER} audio spectrogram"}
    return rows


class Transport:
    def __init__(self):
        self.payloads = []
        self.lock = threading.Lock()

    def __call__(self, payload):
        with self.lock:
            self.payloads.append(json.loads(json.dumps(payload)))
        answers = {}
        for name, question in payload["questions"].items():
            if question["type"] == "noul":
                answers[name] = {"noul": 0.95}
                continue
            keys = list(question["criteria"])
            pick = "songsee" if "songsee" in keys else NONE
            rest = [key for key in keys if key != pick]
            probabilities = {key: 0.02 / len(rest) for key in rest} if rest else {}
            probabilities[pick] = 0.98 if rest else 1.0
            answers[name] = {"choice": pick, "probabilities": probabilities, "confidence": 0.98}
        return {"model": EXPECTED_MODEL, "answers": answers, "usage": {}, "latency_ms": 5.0}


def hook(transport, *, two_stage=None, environ=None):
    return build_pre_llm_call_hook(
        configured_candidates=catalog(),
        routing_mode="hosted_sanitized",
        hosted_mode="always",
        client_factory=lambda: DecisionClient(api_key="test-key", transport=transport),
        consumer_mode="load",
        skill_loader=lambda name, task_id=None: f"SYNTHETIC SKILL BODY {name}",
        cache_seconds=0.0,
        two_stage=two_stage,
        environ=environ if environ is not None else {},
    )


class TwoStageWiringTests(HermesHomeTestCase):
    def test_noninteractive_platform_skips_with_zero_requests(self):
        transport = Transport()
        callback = hook(transport, two_stage=TwoStageConfig())
        for platform in ("api_server", "cron"):
            response = callback(user_message="Render a mel spectrogram", platform=platform)
            self.assertNotIn("context", response)
            self.assertEqual(response["metadata"]["skill_recommendation"]["status"], "platform_skipped")
        self.assertEqual(transport.payloads, [])
        self.assertEqual(callback.last_receipt["hosted_skip_reason"], "noninteractive_platform")

    def test_platform_skip_records_history_and_replays_one_turn(self):
        transport = Transport()
        callback = hook(transport, two_stage=TwoStageConfig())
        kwargs = {
            "user_message": "A synthetic machine-generated poll",
            "platform": "cron",
            "session_id": "synthetic-session",
            "turn_id": "synthetic-turn",
        }
        first = callback(**kwargs)
        records = receipt_history.read_history(session_id="synthetic-session")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["turn_id"], "synthetic-turn")
        self.assertEqual(records[0]["receipt"]["hosted_skip_reason"], "noninteractive_platform")
        with mock.patch.object(receipt_history, "record_turn_receipt", side_effect=AssertionError("replayed turn")):
            self.assertEqual(callback(**kwargs), first)
        self.assertEqual(len(receipt_history.read_history(session_id="synthetic-session")), 1)
        self.assertEqual(transport.payloads, [])

    def test_platform_skip_reports_persistence_failure(self):
        callback = hook(Transport(), two_stage=TwoStageConfig())
        with mock.patch.object(receipt_state, "store_latest_receipt", return_value=False):
            result = callback(
                user_message="A synthetic machine-generated poll", platform="cron",
                session_id="synthetic-session", turn_id="failed-save",
            )
        self.assertIs(result["metadata"].get("receipt_persist_failed"), True)
        self.assertIs(callback.last_metadata.get("receipt_persist_failed"), True)
        self.assertIs(callback.last_routing_metadata.get("receipt_persist_failed"), True)
        self.assertEqual(len(receipt_history.read_history(session_id="synthetic-session")), 1)

    def test_kanban_worker_skips_and_platform_override_routes(self):
        transport = Transport()
        callback = hook(transport, two_stage=TwoStageConfig(), environ={"HERMES_KANBAN_TASK": "t_1"})
        callback(user_message="Render a mel spectrogram", platform="cli")
        self.assertEqual(transport.payloads, [])
        forced = hook(
            transport,
            two_stage=TwoStageConfig.from_mapping({"automatic_skill_platforms": "all"}),
            environ={"HERMES_KANBAN_TASK": "t_1"},
        )
        response = forced(user_message="Render a mel spectrogram", platform="api_server")
        self.assertEqual(response["metadata"]["skill_recommendation"]["selected"], "songsee")
        self.assertGreater(len(transport.payloads), 0)

    def test_full_catalog_turn_uses_two_stage_names_only_by_default(self):
        # No lexical overlap: the #20 prefilter keeps the full catalog.
        transport = Transport()
        callback = hook(transport, two_stage=TwoStageConfig())
        response = callback(user_message="Picture how this recording sounds", platform="cli")
        self.assertEqual(response["metadata"]["skill_recommendation"]["status"], "loaded")
        self.assertEqual(response["metadata"]["skill_recommendation"]["source"], "jev")
        self.assertNotIn(MARKER, json.dumps(transport.payloads))
        self.assertEqual(callback.last_receipt["shortlist_policy"], SHORTLIST_POLICY_TWO_STAGE)
        self.assertEqual(callback.last_receipt["request_count"], len(transport.payloads))
        self.assertTrue(any(p["state"].get("stage") == 2 for p in transport.payloads))

    def test_prefilter_shortlist_then_single_two_stage_request(self):
        transport = Transport()
        callback = hook(transport, two_stage=TwoStageConfig())
        response = callback(user_message="Render a mel spectrogram", platform="cli")
        self.assertEqual(response["metadata"]["skill_recommendation"]["status"], "loaded")
        self.assertEqual(len(transport.payloads), 1)
        self.assertEqual(callback.last_receipt["shortlist_policy"], "local_prefilter_shortlist")
        self.assertNotIn(MARKER, json.dumps(transport.payloads))

    def test_descriptions_opt_in_reaches_stage2_only(self):
        transport = Transport()
        callback = hook(transport, two_stage=TwoStageConfig(hosted_detail=HOSTED_DETAIL_DESCRIPTIONS))
        callback(user_message="Picture how this recording sounds", platform="cli")
        stage1 = [p for p in transport.payloads if p["state"].get("stage") != 2]
        stage2 = [p for p in transport.payloads if p["state"].get("stage") == 2]
        self.assertNotIn(MARKER, json.dumps(stage1))
        self.assertIn(MARKER, json.dumps(stage2))

    def test_disabled_two_stage_keeps_legacy_fan_out(self):
        for two_stage in (None, TwoStageConfig(enabled=False)):
            transport = Transport()
            callback = hook(transport, two_stage=two_stage)
            callback(user_message="Picture how this recording sounds", platform="cli")
            self.assertGreater(len(transport.payloads), 0)
            self.assertTrue(all(p["state"].get("stage") != 2 for p in transport.payloads))
            self.assertNotEqual(callback.last_receipt["shortlist_policy"], SHORTLIST_POLICY_TWO_STAGE)
            self.assertNotIn(MARKER, json.dumps(transport.payloads))

    def test_register_reads_two_stage_config_keys(self):
        from hermes_switchyard import two_stage_routing

        self.assertIn("automatic_skill_platforms", two_stage_routing.TWO_STAGE_CONFIG_KEYS)
        self.assertIn("automatic_skill_hosted_detail", two_stage_routing.TWO_STAGE_CONFIG_KEYS)
        parsed = TwoStageConfig.from_mapping({key: None for key in two_stage_routing.TWO_STAGE_CONFIG_KEYS})
        self.assertEqual(parsed, TwoStageConfig())

    def test_manifest_exposes_every_runtime_two_stage_setting(self):
        from hermes_switchyard.two_stage_routing import TWO_STAGE_CONFIG_KEYS

        manifest = yaml.safe_load((Path(__file__).resolve().parents[1] / "plugin.yaml").read_text(encoding="utf-8"))
        schema = manifest["config_schema"]
        expected_types = {
            "automatic_skill_two_stage": "bool",
            "automatic_skill_platforms": "list",
            "automatic_skill_hosted_detail": "str",
            "automatic_skill_recheck_top_k": "int",
            "automatic_skill_early_stop": "bool",
            "automatic_skill_early_stop_threshold": "float",
            "automatic_skill_stage1_min_probability": "float",
            "automatic_skill_parallel_requests": "int",
        }
        self.assertEqual(set(TWO_STAGE_CONFIG_KEYS), set(expected_types))
        for key, expected_type in expected_types.items():
            with self.subTest(key=key):
                self.assertIn(key, schema)
                self.assertEqual(schema[key]["type"], expected_type)
        defaults = {key: schema[key]["default"] for key in TWO_STAGE_CONFIG_KEYS}
        self.assertEqual(TwoStageConfig.from_mapping(defaults), TwoStageConfig())


if __name__ == "__main__":
    unittest.main()
