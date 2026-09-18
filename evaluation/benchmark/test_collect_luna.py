from __future__ import annotations

import json
import unittest

from collect_luna import _luna_failure_error, _parse_success, _sanitize_luna_response_excerpt


USAGE = {
    "completed": True,
    "partial": False,
    "failed": False,
    "model": "gpt-5.6-luna-900k",
    "provider": "openai-codex",
    "api_calls": 1,
    "turn_exit_reason": "text_response(finish_reason=stop)",
}


class LunaFailureEvidenceTests(unittest.TestCase):
    def test_selected_response_without_selected_skills_keeps_exact_cause_and_safe_excerpt(self):
        raw = json.dumps({
            "status": "selected",
            "selected": "git-change-preparation",
            "abstention_reason": None,
            "secret_like_field": "must-not-be-retained",
        })
        with self.assertRaises(ValueError) as raised:
            _parse_success(raw, USAGE)

        error = _luna_failure_error(raised.exception, stdout=raw, usage=USAGE, exit_code=0)
        self.assertEqual(error["type"], "ValueError")
        self.assertEqual(error["cause"], "luna_response_schema_invalid")
        self.assertEqual(
            error["response_excerpt"],
            {
                "json_valid": True,
                "fields_present": ["abstention_reason", "selected", "status"],
                "status": "selected",
                "selected": "git-change-preparation",
                "abstention_reason": None,
            },
        )
        self.assertNotIn("secret_like_field", json.dumps(error, sort_keys=True))
        self.assertEqual(error["usage_receipt"]["model"], "gpt-5.6-luna-900k")
        self.assertEqual(error["usage_receipt"]["api_calls"], 1)

    def test_invalid_json_excerpt_does_not_copy_provider_text(self):
        excerpt = _sanitize_luna_response_excerpt("not-json provider text with a token")
        self.assertEqual(excerpt, {"json_valid": False})


if __name__ == "__main__":
    unittest.main()
