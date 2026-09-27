"""Issue #122: automatic skill routing must keep secret-bearing turns local.

Every hosted call here uses a synthetic in-memory DecisionClient transport. No
test calls a real endpoint. All secret-shaped values are inert synthetic markers;
none of them is a real credential. A regular-expression scan is a conservative
blocklist, not data-loss prevention. These tests pin the observable contract:
common actual-value shapes never construct a hosted client, while public
technical words, placeholders, versions, and dates still route through Jev.
"""
from __future__ import annotations

import json
import unittest

from hermes_switchyard.automatic import (
    MAX_TASK_CHARS,
    AutomaticSkillRecommender,
    _local_scan_reason,
    build_pre_llm_call_hook,
)
from hermes_switchyard import automatic
from hermes_switchyard.client import EXPECTED_MODEL, DecisionClient
from hermes_switchyard.two_stage_routing import (
    HOSTED_DETAIL_DESCRIPTIONS,
    TwoStageConfig,
    build_stage2_skills,
)
from test_support import HermesHomeTestCase

INERT = "SYNTHETIC_INERT_VALUE"
# Assembled at runtime so no literal token shape appears in the source tree.
_ALNUM = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"

SECRET_TASKS = {
    "env_password": f"fix the deploy: DB_PASSWORD={INERT}",
    "env_prefixed_password": f"fix the deploy: PGPASSWORD={INERT}",
    "env_api_key": f"fix the deploy: OPENAI_API_KEY={INERT}",
    "env_token": f"fix the deploy: GITHUB_TOKEN={INERT}",
    "env_access_key": f"fix the deploy: AWS_SECRET_ACCESS_KEY={INERT}",
    "env_short_pass": f"fix the deploy: MYSQL_PASS={INERT}",
    "env_plain_word_password": "fix the deploy: DB_PASSWORD=hunter",
    "django_secret_key": f"fix the deploy: SECRET_KEY = '{INERT}'",
    "export_quoted": f'fix the deploy: export SLACK_BOT_TOKEN="{INERT}"',
    "code_client_secret": f"fix the deploy: client_secret = '{INERT}'",
    "yaml_key": f"fix the deploy: db_password: {INERT}",
    "json_key": f'fix the deploy: {{"refresh_token": "{INERT}"}} is rejected',
    "prose_token": f"fix the deploy: my token: {INERT}",
    "header_key": f"curl -H 'X-Api-Key: {INERT}' against the staging host",
    "query_token": f"open https://api.example.invalid/v1?access_token={INERT}",
    "authorization_basic": f"curl -H 'Authorization: Basic {INERT}' against the staging host",
    "authorization_bearer": f"curl -H 'Authorization: Bearer {INERT}' against the staging host",
    "url_userinfo_tld": f"connect to postgres://admin:{INERT}@db.example.invalid/app",
    "url_userinfo_local": f"connect to postgres://admin:{INERT}@localhost:5432/app",
    "github_fine_grained": "authenticate with github_pat_" + _ALNUM,
    "github_classic_after_underscore": "authenticate with value_ghp_" + _ALNUM,
    "openai_style": "authenticate with sk-proj-" + _ALNUM,
    "slack": "authenticate with xoxb-" + _ALNUM[:24],
    "aws_access_key_id": "authenticate with AKIA" + "ABCDEFGHIJKLMNOP",
    "google_api_key": "authenticate with AIza" + _ALNUM[:35],
    "gitlab": "authenticate with glpat-" + _ALNUM[:20],
    "stripe_live": "authenticate with sk_live_" + _ALNUM[:24],
    "jwt": "authenticate with eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ." + _ALNUM[:24],
    "private_key_block": "load this -----BEGIN OPENSSH PRIVATE " + "KEY-----\n" + _ALNUM,
}

PUBLIC_TASKS = {
    "hash_passwords": "how should I hash passwords with argon2 in a web app?",
    "rotate_token": "rotate the GitHub token secret in the Actions settings page",
    "config_name_only": "set api_key in config.yaml and restart the Docker service",
    "env_name_only": "why is OPENAI_API_KEY unset inside the Docker container?",
    "env_reference": "export GITHUB_TOKEN=$GH_TOKEN before running the Docker build",
    "env_template": "GITHUB_TOKEN=${{ secrets.GITHUB_TOKEN }} in the Docker workflow",
    "placeholder": "put DB_PASSWORD=<your-value> in the Docker compose env file",
    "masked": "the Docker log shows GITHUB_TOKEN=******** after masking",
    "code_expression": "why does token = get_token() return None in my Docker script?",
    "name_suffix_path": "mount DB_PASSWORD_FILE=/run/secrets/db for the Docker service",
    "token_count": "max_tokens=4096 is too small for the Docker log summary",
    "bearer_topic": "explain OAuth bearer tokens and secret management for Docker",
    "prose_token_word": "Token: expired, so the Docker registry login fails",
    "version": "upgrade Docker token helper from v2.3.1 to v2.4.0-rc.1",
    "url_with_port": "pull from https://registry.example.invalid:5000/v2/app for Docker",
    "password_topic_basic": "basic authentication versus token authentication for Docker",
    "prose_pass": "first pass: done, second pass: pending for the Docker build",
    "prose_label": "Credentials: required and Authorization: missing in the Docker log",
    "json_boolean": 'the Docker report has "policy_pass": true and "is_secret": false',
    "suffix_plain_word": "the Docker test_pass=skipped and first_pwd=unset flags",
}

DATE_AND_VERSION_VALUES = (
    "token: 2026-09-26",
    "token: 2026-09-26T10:00:00Z",
    "secret rotation date 2026-09-26",
    "release token v2.3.1 on 2026-09-26",
    "token: v10.20.30",
)


def _jev_transport(calls):
    """Answer every offered question with a valid, confident synthetic result."""

    def transport(payload):
        calls.append(json.loads(json.dumps(payload)))
        answers = {}
        for name, question in payload["questions"].items():
            if question["type"] == "noul":
                answers[name] = {"noul": 0.95}
                continue
            keys = list(question["criteria"])
            pick = "docker-management" if "docker-management" in keys else keys[-1]
            rest = [key for key in keys if key != pick]
            probabilities = {key: 0.02 / len(rest) for key in rest} if rest else {}
            probabilities[pick] = 0.98 if rest else 1.0
            answers[name] = {"choice": pick, "probabilities": probabilities, "confidence": 0.98}
        return {"model": EXPECTED_MODEL, "answers": answers, "usage": {}, "latency_ms": 5.0}

    return transport


class AutomaticSecretEgressTests(HermesHomeTestCase):
    candidates = [
        {"name": "docker-management", "description": "Docker containers and Compose"},
        {"name": "songsee", "description": "audio spectrograms"},
    ]

    def _forbidden_recommender(self, constructed, **kwargs):
        def forbidden_client():
            constructed.append(True)
            raise AssertionError("restricted input constructed a hosted client")

        options = {
            "configured_candidates": self.candidates,
            "routing_mode": "hosted_sanitized",
            "hosted_mode": "always",
            "public_or_sanitized_data_ack": True,
            "client_factory": forbidden_client,
            "adoption_capable": True,
            "cache_seconds": 0.0,
        }
        options.update(kwargs)
        return AutomaticSkillRecommender(**options)

    def _hosted_recommender(self, calls, constructed=None, **kwargs):
        def factory():
            if constructed is not None:
                constructed.append(True)
            return DecisionClient(api_key="fixture-key", transport=_jev_transport(calls))

        options = {
            "configured_candidates": self.candidates,
            "routing_mode": "hosted_sanitized",
            "hosted_mode": "always",
            "public_or_sanitized_data_ack": True,
            "client_factory": factory,
            "adoption_capable": True,
            "cache_seconds": 0.0,
        }
        options.update(kwargs)
        return AutomaticSkillRecommender(**options)

    def test_secret_values_never_construct_hosted_client_under_standing_ack(self):
        for label, task in SECRET_TASKS.items():
            with self.subTest(case=label):
                constructed = []
                result = self._forbidden_recommender(constructed).recommend(task)
                self.assertEqual(constructed, [])
                self.assertFalse(result["hosted_attempted"])
                self.assertEqual(result["hosted_skipped"], result["routing_reason"])
                self.assertTrue(result["hosted_skipped"].startswith("local_scan_"), result["hosted_skipped"])
                self.assertNotIn(INERT, json.dumps(result))

    def test_secret_values_classify_as_secret_like(self):
        for label, task in SECRET_TASKS.items():
            with self.subTest(case=label):
                self.assertIsNotNone(_local_scan_reason(task, task))

    def test_env_assignment_classifies_as_secret_like_value(self):
        for key in ("DB_PASSWORD", "OPENAI_API_KEY", "GITHUB_TOKEN"):
            with self.subTest(key=key):
                task = f"{key}={INERT}"
                self.assertEqual(_local_scan_reason(task, task), "local_scan_secret_like_value")

    def test_public_technical_text_still_routes_through_jev(self):
        for label, task in PUBLIC_TASKS.items():
            with self.subTest(case=label):
                self.assertIsNone(_local_scan_reason(task, task))
                calls = []
                result = self._hosted_recommender(calls).recommend(task)
                self.assertTrue(result["hosted_attempted"])
                self.assertNotIn("hosted_skipped", result)
                self.assertGreaterEqual(len(calls), 1)
                self.assertEqual(calls[0]["state"]["task"], task)

    def test_dates_and_versions_are_not_secret_values(self):
        # The contact scan separately treats some ISO dates as phone-like;
        # that pre-existing behavior is outside this secret-value contract.
        for text in DATE_AND_VERSION_VALUES:
            with self.subTest(text=text):
                self.assertFalse(automatic._contains_secret_value(text))
                self.assertNotEqual(_local_scan_reason(text, text), "local_scan_secret_like_value")

    def test_secret_after_task_bound_blocks_the_whole_turn(self):
        filler = "Docker compose container health check notes. " * 120
        self.assertGreater(len(filler), MAX_TASK_CHARS)
        task = f"{filler}DB_PASSWORD={INERT} trailing note"
        constructed = []
        result = self._forbidden_recommender(constructed).recommend(task)
        self.assertEqual(constructed, [])
        self.assertFalse(result["hosted_attempted"])
        self.assertEqual(result["hosted_skipped"], "local_scan_secret_like_value")

    def test_secret_cut_at_task_bound_does_not_leak_a_prefix(self):
        token = "sk-proj-" + _ALNUM
        head = ("Docker compose container notes " * 200)[: MAX_TASK_CHARS - 7] + " "
        self.assertEqual(len(head), MAX_TASK_CHARS - 6)
        task = f"{head}{token} more text"
        # The hosted projection keeps only ``sk-pro``: too short to classify.
        self.assertIsNone(_local_scan_reason(task[:MAX_TASK_CHARS], task[:MAX_TASK_CHARS]))
        constructed = []
        result = self._forbidden_recommender(constructed).recommend(task)
        self.assertEqual(constructed, [])
        self.assertFalse(result["hosted_attempted"])

    def test_secret_in_later_content_block_blocks_the_turn(self):
        blocks = [
            {"type": "text", "text": "Docker compose container notes " * 140},
            {"type": "image", "source": "synthetic-image"},
            {"type": "text", "text": f"GITHUB_TOKEN={INERT}"},
        ]
        constructed = []
        result = self._forbidden_recommender(constructed).recommend(blocks)
        self.assertEqual(constructed, [])
        self.assertFalse(result["hosted_attempted"])
        self.assertEqual(result["hosted_skipped"], "local_scan_secret_like_value")

    def test_oversized_task_fails_closed_before_client_construction(self):
        task = "Docker compose notes " * (automatic.MAX_SCAN_CHARS // 10)
        self.assertGreater(len(task), automatic.MAX_SCAN_CHARS)
        constructed = []
        result = self._forbidden_recommender(constructed).recommend(task)
        self.assertEqual(constructed, [])
        self.assertFalse(result["hosted_attempted"])
        self.assertEqual(result["hosted_skipped"], "local_scan_unclassifiable")

    def test_long_clean_task_still_sends_only_the_bounded_prefix(self):
        filler = "Docker compose container health check notes. " * 120
        task = filler + "TAIL_ONLY_MARKER"
        calls = []
        result = self._hosted_recommender(calls).recommend(task)
        self.assertTrue(result["hosted_attempted"])
        wire = json.dumps(calls)
        self.assertNotIn("TAIL_ONLY_MARKER", wire)
        self.assertIn(filler.strip()[:200], wire)

    def test_cached_clean_prefix_does_not_authorize_secret_tail(self):
        filler = "Docker compose container health check notes. " * 120
        calls = []
        constructed = []
        recommender = self._hosted_recommender(calls, constructed, cache_seconds=30.0)
        clean = recommender.recommend(filler + "clean tail")
        self.assertEqual(clean["source"], "jev")
        request_count = len(calls)
        secret = recommender.recommend(filler + f"clean tail GITHUB_TOKEN={INERT}")
        self.assertEqual(len(calls), request_count)
        self.assertEqual(len(constructed), 1)
        self.assertFalse(secret["cache_hit"])
        self.assertEqual(secret["hosted_skipped"], "local_scan_secret_like_value")
        self.assertNotEqual(secret["source"], "jev")

    def test_allow_envelope_payload_with_secret_never_constructs_client(self):
        for key in ("DB_PASSWORD", "OPENAI_API_KEY", "GITHUB_TOKEN"):
            with self.subTest(key=key):
                constructed = []
                policy = {
                    "version": 1,
                    "decision": "allow",
                    "data_class": "sanitized",
                    "reason_code": "synthetic_fixture_allowed",
                    "allowed_payload": f"{key}={INERT}",
                }
                result = self._forbidden_recommender(constructed).recommend(
                    "Docker maintenance", turn_egress_policy=policy
                )
                self.assertEqual(constructed, [])
                self.assertFalse(result["hosted_attempted"])
                self.assertEqual(result["hosted_skipped"], "local_scan_secret_like_value")

    def test_off_and_local_only_modes_never_construct_client(self):
        task = SECRET_TASKS["env_token"]
        for mode in ("off", "local_only"):
            with self.subTest(mode=mode):
                constructed = []
                self._forbidden_recommender(constructed, routing_mode=mode).recommend(task)
                self.assertEqual(constructed, [])

    def test_stage2_detail_with_secret_value_is_withheld(self):
        rows, withheld = build_stage2_skills(
            ["docker-management"],
            {"docker-management": {"description": f"Docker notes GITHUB_TOKEN={INERT}"}},
            HOSTED_DETAIL_DESCRIPTIONS,
        )
        self.assertEqual(withheld, 1)
        self.assertNotIn(INERT, json.dumps(rows))

    def test_stage2_description_cut_never_sends_a_partial_token(self):
        from hermes_switchyard.two_stage_routing import MAX_DETAIL_DESCRIPTION_CHARS

        # Place ``ghp_`` so the stage-2 description cut keeps only 8 value
        # characters: too short for the token pattern to classify.
        limit = MAX_DETAIL_DESCRIPTION_CHARS
        head = ("DETAIL_KEEP_MARKER " + "Docker compose words " * 40)[: limit - 13].rstrip()
        head = head + " " * (limit - 12 - len(head))
        self.assertEqual(len(head), limit - 12)
        description = f"{head}ghp_{_ALNUM} description tail"
        calls = []
        catalog = [
            {"name": f"filler-skill-{i:03d}", "description": f"filler {i}"} for i in range(40)
        ]
        catalog.append({"name": "docker-management", "description": description})
        catalog.append({"name": "clean-skill", "description": "DETAIL_KEEP_MARKER clean detail"})
        callback = build_pre_llm_call_hook(
            configured_candidates=catalog,
            routing_mode="hosted_sanitized",
            hosted_mode="always",
            client_factory=lambda: DecisionClient(api_key="fixture-key", transport=_jev_transport(calls)),
            consumer_mode="load",
            skill_loader=lambda name, task_id=None: f"SYNTHETIC SKILL BODY {name}",
            cache_seconds=0.0,
            two_stage=TwoStageConfig(hosted_detail=HOSTED_DETAIL_DESCRIPTIONS, recheck_top_k=8),
            environ={},
        )
        assert callback is not None
        callback(user_message="Picture how this recording sounds", platform="cli")
        stage2 = [p for p in calls if p["state"].get("stage") == 2]
        self.assertTrue(stage2, "the fixture must reach stage 2")
        stage2_names = {row["name"] for p in stage2 for row in p["state"]["skills"]}
        self.assertIn("docker-management", stage2_names)
        self.assertNotIn("ghp_", json.dumps(calls))

    def test_two_stage_hook_keeps_secret_turn_local(self):
        constructed = []

        def forbidden_client():
            constructed.append(True)
            raise AssertionError("restricted hook turn constructed a hosted client")

        callback = build_pre_llm_call_hook(
            configured_candidates=self.candidates,
            routing_mode="hosted_sanitized",
            hosted_mode="always",
            client_factory=forbidden_client,
            consumer_mode="load",
            skill_loader=lambda name, task_id=None: f"SYNTHETIC SKILL BODY {name}",
            cache_seconds=0.0,
            two_stage=TwoStageConfig(),
            environ={},
        )
        assert callback is not None
        for label in ("env_password", "env_api_key", "env_token", "url_userinfo_local"):
            with self.subTest(case=label):
                response = callback(user_message=SECRET_TASKS[label], platform="cli")
                self.assertEqual(constructed, [])
                self.assertFalse(callback.last_result["hosted_attempted"])
                self.assertTrue(callback.last_receipt["hosted_skip_reason"].startswith("local_scan_"))
                self.assertNotIn(INERT, json.dumps(response))
                self.assertNotIn(INERT, json.dumps(callback.last_receipt))


if __name__ == "__main__":
    unittest.main()
