"""Behavior checks for the DOM browser loop."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from hermes_switchyard import browser_use
from hermes_switchyard.browser_use import (
    BrowserStartupError,
    _browser_profile_dir,
    _is_snap_chromium,
    _resolve_browser_binary,
    infer_start_url,
    requested_web_start,
    run_browser_goal,
)
import hermes_switchyard


class FakeSession:
    def __init__(self, pages: dict[str, dict]):
        self.pages = pages
        self.url = next(iter(pages))
        self.clicks: list[str] = []
        self.scrolls: list[str] = []

    def observe(self) -> dict:
        page = dict(self.pages[self.url])
        page["url"] = self.url
        return page

    def click(self, element_id: str, label: str = "", href: str = "") -> None:
        self.clicks.append(element_id)
        page = self.pages[self.url]
        target = next(item for item in page["elements"] if item["id"] == element_id)
        if label and target["label"] != label:
            raise RuntimeError("stale label")
        if href and target["href"] != href:
            raise RuntimeError("stale href")
        dest = target["href"]
        if dest in self.pages:
            self.url = dest

    def scroll(self, direction: str) -> None:
        self.scrolls.append(direction)

    def wait(self, seconds: float = 0.2) -> None:
        return None

    def close(self) -> None:
        return None


class FakeClient:
    def __init__(self, script: list[dict]):
        self.script = script
        self.calls: list[dict] = []

    @contextmanager
    def request_budget(self, max_requests: int = 256, *, deadline_seconds=None):
        yield

    def decide(self, state, questions, **kwargs):
        self.calls.append({"state": state, "questions": dict(questions)})
        answers = self.script[len(self.calls) - 1]
        return {
            "answers": answers,
            "latency_ms": 12,
            "model": "jev-latest",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }


def _choice(choice: str, criteria: dict) -> dict:
    rest = max(0.0, 1.0 - 0.91)
    others = [key for key in criteria if key != choice]
    probabilities = {choice: 0.91}
    if others:
        share = rest / len(others)
        probabilities.update({key: share for key in others})
    else:
        probabilities[choice] = 1.0
    return {"choice": choice, "probabilities": probabilities, "confidence": 0.9}


class BrowserUseTests(unittest.TestCase):
    def test_snap_wrapper_resolves_to_confined_binary(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            wrapper = root_path / "chromium-browser"
            snap = root_path / "chromium"
            wrapper.write_text('#!/bin/sh\nexec /snap/bin/chromium "$@"\n', encoding="utf-8")
            snap.write_text("binary", encoding="utf-8")
            self.assertEqual(_resolve_browser_binary(wrapper, snap_binary=snap), snap)
            self.assertTrue(_is_snap_chromium(wrapper))

    def test_snap_profile_is_created_under_confined_common_directory(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            with mock.patch("pathlib.Path.home", return_value=home):
                temporary = _browser_profile_dir(Path("/snap/bin/chromium"))
            profile = Path(temporary.name)
            self.assertEqual(profile.parent, home / "snap" / "chromium" / "common")
            self.assertTrue(profile.is_dir())
            temporary.cleanup()
            self.assertFalse(profile.exists())

    def test_infers_wikipedia_start_url_from_goal(self):
        url = infer_start_url(None, "Public Wikipedia race starting on Cat. Reach Lion.")
        self.assertEqual(url, "https://en.wikipedia.org/wiki/Cat")

    def test_explicit_https_url_wins(self):
        url = infer_start_url(
            "https://en.wikipedia.org/wiki/Felidae",
            "starting on Cat",
        )
        self.assertEqual(url, "https://en.wikipedia.org/wiki/Felidae")

    def test_rejects_file_urls(self):
        self.assertIsNone(infer_start_url("file:///etc/passwd", "open this"))

    def test_rejects_localhost_and_private_urls(self):
        self.assertIsNone(infer_start_url("http://localhost:8000/", "open this"))
        self.assertIsNone(infer_start_url("https://127.0.0.1/", "open this"))
        self.assertIsNone(infer_start_url("https://192.168.0.1/", "open this"))
        self.assertIsNone(infer_start_url("https://10.0.0.1/", "open this"))
        self.assertIsNone(infer_start_url("https://127.1/", "open this"))
        with self.assertRaises(ValueError):
            requested_web_start("https://127.0.0.1/", "click Start")
        with self.assertRaises(ValueError):
            requested_web_start(None, "open https://192.168.0.1/ now")
        with self.assertRaises(ValueError):
            requested_web_start(None, "open file:///etc/passwd")
        with self.assertRaises(ValueError):
            requested_web_start(None, "run javascript:alert(1)")
        with self.assertRaises(ValueError):
            requested_web_start("about:blank", "click Start")

    def test_rejects_unsafe_uri_variants(self):
        cases = [
            (None, "open file:///example.txt"),
            (None, "run javascript:void(0)"),
            (None, "open data:text/plain,example"),
            (None, "open about:blank"),
            (None, "open vbscript:MsgBox(1)"),
            (None, "open blob:https://example.org/example"),
            (None, "run javascript:(void(0))"),
            (None, "run javascript:%76oid(0)"),
            (None, "run javascript: void(0)"),
            ("https://example.org/", "run javascript:void(0)"),
        ]
        for explicit, goal in cases:
            with self.subTest(explicit=explicit, goal=goal):
                with self.assertRaises(ValueError):
                    requested_web_start(explicit, goal)

    def test_false_ack_does_not_observe(self):
        session = FakeSession(
            {
                "https://en.wikipedia.org/wiki/Cat": {
                    "title": "Cat",
                    "text": "Cat",
                    "elements": [{"id": "1", "role": "link", "label": "Felidae", "href": "https://en.wikipedia.org/wiki/Felidae"}],
                }
            }
        )
        observed = {"count": 0}
        original = session.observe

        def counting():
            observed["count"] += 1
            return original()

        session.observe = counting  # type: ignore[method-assign]
        with self.assertRaises(PermissionError):
            run_browser_goal(
                goal="stuck",
                session=session,
                client=FakeClient([]),
                max_steps=3,
                public_or_sanitized_data_ack=False,
            )
        self.assertEqual(observed["count"], 0)

    def test_loop_clicks_without_computer_use_and_uses_one_jev_call_per_step(self):
        cat = "https://en.wikipedia.org/wiki/Cat"
        felidae = "https://en.wikipedia.org/wiki/Felidae"
        carnivora = "https://en.wikipedia.org/wiki/Carnivora"
        session = FakeSession(
            {
                cat: {
                    "title": "Cat",
                    "text": "The cat is a domestic species.",
                    "elements": [
                        {"id": "1", "role": "link", "label": "Felidae", "href": felidae},
                        {"id": "2", "role": "link", "label": "Donate", "href": "https://donate.wikimedia.org/"},
                    ],
                },
                felidae: {
                    "title": "Felidae",
                    "text": "The cat family.",
                    "elements": [
                        {"id": "1", "role": "link", "label": "Carnivora", "href": carnivora},
                    ],
                },
                carnivora: {
                    "title": "Carnivora",
                    "text": "An order of mammals.",
                    "elements": [
                        {"id": "1", "role": "link", "label": "Mammal", "href": "https://en.wikipedia.org/wiki/Mammal"},
                    ],
                },
            }
        )
        client = FakeClient(
            [
                {
                    "operation": _choice("CLICK", {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b"}),
                    "click_target": _choice("1", {"1": "Felidae"}),
                },
                {
                    "operation": _choice("CLICK", {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b", "DONE": "d"}),
                    "click_target": _choice("1", {"1": "Carnivora"}),
                },
                {
                    "operation": _choice("DONE", {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b", "DONE": "d"}),
                    "click_target": _choice("1", {"1": "Mammal"}),
                },
            ]
        )
        result = run_browser_goal(
            goal="Wikipedia race from Cat toward Carnivora",
            session=session,
            client=client,
            max_steps=10,
        )
        self.assertEqual(result["status"], "completion_candidate")
        self.assertEqual(result["executor"], "browser_dom")
        self.assertEqual(result["computer_use_dispatches"], 0)
        self.assertEqual(result["click_count"], 2)
        self.assertEqual(session.clicks, ["1", "1"])
        self.assertEqual(result["url"], carnivora)
        self.assertEqual(len(client.calls), 3)
        first_questions = client.calls[0]["questions"]
        self.assertEqual(set(first_questions), {"operation", "click_target"})
        self.assertNotIn("Donate", json.dumps(client.calls[0]["state"]["elements"]))

    def test_blocked_does_not_click(self):
        session = FakeSession(
            {
                "https://en.wikipedia.org/wiki/Cat": {
                    "title": "Cat",
                    "text": "Cat",
                    "elements": [{"id": "1", "role": "link", "label": "Felidae", "href": "https://en.wikipedia.org/wiki/Felidae"}],
                }
            }
        )
        client = FakeClient(
            [
                {
                    "operation": _choice("BLOCKED", {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b"}),
                    "click_target": _choice("1", {"1": "Felidae"}),
                }
            ]
        )
        result = run_browser_goal(goal="stuck", session=session, client=client, max_steps=3)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(session.clicks, [])
        self.assertEqual(result["click_count"], 0)

    def test_unsafe_url_after_click_records_action(self):
        session = FakeSession(
            {
                "https://en.wikipedia.org/wiki/Cat": {
                    "title": "Cat",
                    "text": "Cat",
                    "elements": [
                        {
                            "id": "1",
                            "role": "link",
                            "label": "Felidae",
                            "href": "https://en.wikipedia.org/wiki/Felidae",
                        }
                    ],
                },
                "http://localhost/": {"title": "private", "text": "no", "elements": []},
            }
        )
        original = session.click

        def hijack(element_id: str, label: str = "", href: str = "") -> None:
            original(element_id, label=label, href=href)
            session.url = "http://localhost/"

        session.click = hijack  # type: ignore[method-assign]
        result = run_browser_goal(
            goal="stuck",
            session=session,
            client=FakeClient(
                [
                    {
                        "operation": _choice(
                            "CLICK",
                            {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b"},
                        ),
                        "click_target": _choice("1", {"1": "Felidae"}),
                    }
                ]
            ),
            max_steps=3,
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failure_phase"], "unsafe_url")
        self.assertEqual(result["click_count"], 1)
        self.assertEqual(result["attempted_action_count"], 1)
        self.assertEqual(result["actions"][0]["effect_status"], "left_public_https")
        self.assertTrue(result["reconcile_before_retry"])

    def test_handler_uses_dom_loop_for_wikipedia_and_skips_computer_use(self):
        session = FakeSession(
            {
                "https://en.wikipedia.org/wiki/Cat": {
                    "title": "Cat",
                    "text": "Cat",
                    "elements": [{"id": "1", "role": "link", "label": "Felidae", "href": "https://en.wikipedia.org/wiki/Felidae"}],
                }
            }
        )

        class Context:
            def __init__(self):
                self.tools = {}
                self.dispatch_calls = []

            def get_config(self, _key, default=None):
                return default

            def register_auxiliary_task(self, *_args, **_kwargs):
                pass

            def register_tool(self, *, name, handler, **_kwargs):
                self.tools[name] = handler

            def register_skill(self, *_args, **_kwargs):
                pass

            def register_hook(self, *_args, **_kwargs):
                pass

            def dispatch_tool(self, *args, **kwargs):
                self.dispatch_calls.append((args, kwargs))
                raise AssertionError("computer_use must not run for a web goal")

        class Client:
            def close(self):
                return None

            @contextmanager
            def request_budget(self, max_requests=256, *, deadline_seconds=None):
                yield

            def decide(self, state, questions, **kwargs):
                operation_criteria = questions["operation"]["criteria"]
                answers = {"operation": _choice("DONE", operation_criteria)}
                if "click_target" in questions:
                    answers["click_target"] = _choice("1", questions["click_target"]["criteria"])
                return {"answers": answers, "latency_ms": 4, "model": "jev-latest", "usage": {}}

        context = Context()
        with mock.patch.object(hermes_switchyard, "_secret", return_value="fixture-key"):
            with mock.patch.object(hermes_switchyard, "DecisionClient", return_value=Client()):
                with mock.patch.object(hermes_switchyard.browser_use, "open_browser_session") as opener:
                    opener.return_value.__enter__.return_value = session
                    opener.return_value.__exit__.return_value = None
                    hermes_switchyard.register(context)
                    result = json.loads(
                        context.tools["jev_computer_use"](
                            {
                                "goal": "Public Wikipedia race starting on Cat",
                                "app": "Chrome",
                            }
                        )
                    )
        self.assertEqual(result["executor"], "browser_dom")
        self.assertEqual(result["computer_use_dispatches"], 0)
        self.assertEqual(context.dispatch_calls, [])
        opener.assert_called_once()
        self.assertEqual(opener.call_args.args[0], "https://en.wikipedia.org/wiki/Cat")

    def test_handler_refuses_private_start_url_without_native_dispatch(self):
        class Context:
            def __init__(self):
                self.tools = {}
                self.dispatch_calls = []

            def get_config(self, _key, default=None):
                return default

            def register_auxiliary_task(self, *_args, **_kwargs):
                pass

            def register_tool(self, *, name, handler, **_kwargs):
                self.tools[name] = handler

            def register_skill(self, *_args, **_kwargs):
                pass

            def register_hook(self, *_args, **_kwargs):
                pass

            def dispatch_tool(self, *args, **kwargs):
                self.dispatch_calls.append((args, kwargs))
                raise AssertionError("computer_use must not run for a rejected URL")

        context = Context()
        with mock.patch.object(hermes_switchyard, "_secret", return_value="fixture-key"):
            hermes_switchyard.register(context)
            result = json.loads(
                context.tools["jev_computer_use"](
                    {
                        "goal": "click Start",
                        "app": "Chrome",
                        "start_url": "https://127.0.0.1/",
                    }
                )
            )
        self.assertEqual(result["status"], "error")
        self.assertEqual(context.dispatch_calls, [])

        context = Context()
        with mock.patch.object(hermes_switchyard, "_secret", return_value="fixture-key"):
            hermes_switchyard.register(context)
            result = json.loads(
                context.tools["jev_computer_use"](
                    {
                        "goal": "open file:///etc/passwd in Chrome",
                        "app": "Chrome",
                    }
                )
            )
        self.assertEqual(result["status"], "error")
        self.assertEqual(context.dispatch_calls, [])

    def test_handler_refuses_unsafe_uri_variants_without_client_or_native(self):
        class Context:
            def __init__(self):
                self.tools = {}
                self.dispatch_calls = []

            def get_config(self, _key, default=None):
                return default

            def register_auxiliary_task(self, *_args, **_kwargs):
                pass

            def register_tool(self, *, name, handler, **_kwargs):
                self.tools[name] = handler

            def register_skill(self, *_args, **_kwargs):
                pass

            def register_hook(self, *_args, **_kwargs):
                pass

            def dispatch_tool(self, *args, **kwargs):
                self.dispatch_calls.append((args, kwargs))
                raise AssertionError("computer_use must not run for a rejected URI")

        payloads = [
            {"goal": "open file:///example.txt", "app": "Chrome"},
            {"goal": "run javascript:void(0)", "app": "Chrome"},
            {"goal": "open data:text/plain,example", "app": "Chrome"},
            {"goal": "open about:blank", "app": "Chrome"},
            {"goal": "open vbscript:MsgBox(1)", "app": "Chrome"},
            {"goal": "open blob:https://example.org/example", "app": "Chrome"},
            {"goal": "run javascript:(void(0))", "app": "Chrome"},
            {"goal": "run javascript:%76oid(0)", "app": "Chrome"},
            {"goal": "run javascript: void(0)", "app": "Chrome"},
            {
                "goal": "run javascript:void(0)",
                "app": "Chrome",
                "start_url": "https://example.org/",
            },
        ]
        for args in payloads:
            with self.subTest(args=args):
                context = Context()
                with mock.patch.object(hermes_switchyard, "_secret", return_value="fixture-key"):
                    hermes_switchyard.register(context)
                    with mock.patch.object(
                        hermes_switchyard,
                        "DecisionClient",
                        side_effect=AssertionError("client must not be constructed"),
                    ):
                        result = json.loads(context.tools["jev_computer_use"](args))
                self.assertEqual(result["status"], "error")
                self.assertEqual(context.dispatch_calls, [])

    def test_standing_false_refuses_explicit_true(self):
        self.assertFalse(
            hermes_switchyard._resolved_public_data_ack(
                {"public_or_sanitized_data_ack": True},
                standing=False,
            )
        )

    def test_noop_same_document_click_is_not_effect_confirmed(self):
        """Issue #28: a dispatched click with no observed delta must not claim effect_confirmed."""
        start = "https://example.com/article"
        session = FakeSession(
            {
                start: {
                    "title": "Article",
                    "text": "Public article body with enough text.",
                    "elements": [
                        {
                            "id": "1",
                            "role": "link",
                            "label": "Same page anchor",
                            "href": start,
                        },
                        {
                            "id": "2",
                            "role": "link",
                            "label": "Elsewhere",
                            "href": "https://example.com/other",
                        },
                    ],
                }
            }
        )
        client = FakeClient(
            [
                {
                    "operation": _choice(
                        "CLICK",
                        {
                            "CLICK": "c",
                            "SCROLL_DOWN": "s",
                            "SCROLL_UP": "u",
                            "WAIT": "w",
                            "BLOCKED": "b",
                        },
                    ),
                    "click_target": _choice("1", {"1": "Same page anchor", "2": "Elsewhere"}),
                },
                {
                    "operation": _choice(
                        "DONE",
                        {
                            "CLICK": "c",
                            "SCROLL_DOWN": "s",
                            "SCROLL_UP": "u",
                            "WAIT": "w",
                            "BLOCKED": "b",
                            "DONE": "d",
                        },
                    ),
                    "click_target": _choice("1", {"1": "Same page anchor", "2": "Elsewhere"}),
                },
            ]
        )
        result = run_browser_goal(
            goal="Click the same-page link then finish",
            session=session,
            client=client,
            max_steps=5,
            min_actions_before_done=1,
        )
        self.assertEqual(result["status"], "completion_candidate")
        self.assertFalse(result["goal_verified"])
        self.assertEqual(session.clicks, ["1"])
        action = result["actions"][0]
        self.assertEqual(action["operation"], "CLICK")
        self.assertTrue(action["action_dispatched"])
        self.assertFalse(action["effect_observed"])
        self.assertFalse(action["effect_confirmed"])
        self.assertEqual(action["effect_status"], "unchanged")
        self.assertFalse(action["goal_verified"])
        self.assertIsInstance(result["last_state_hash"], str)
        self.assertEqual(len(result["last_state_hash"]), 64)

    def test_provider_timeout_after_click_returns_partial_receipt(self):
        """Issue #28: a later provider timeout must preserve the completed click ledger."""
        start = "https://example.com/start"
        dest = "https://example.com/dest"
        session = FakeSession(
            {
                start: {
                    "title": "Start",
                    "text": "Start page with enough text for the snapshot.",
                    "elements": [
                        {"id": "1", "role": "link", "label": "Continue", "href": dest},
                    ],
                },
                dest: {
                    "title": "Dest",
                    "text": "Destination page with enough text for the snapshot.",
                    "elements": [
                        {"id": "1", "role": "link", "label": "Home", "href": start},
                    ],
                },
            }
        )

        class TimeoutAfterClickClient(FakeClient):
            def decide(self, state, questions, **kwargs):
                self.calls.append({"state": state, "questions": dict(questions)})
                if len(self.calls) >= 2:
                    raise TimeoutError("provider timed out")
                answers = self.script[0]
                return {
                    "answers": answers,
                    "latency_ms": 12,
                    "model": "jev-latest",
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                }

        client = TimeoutAfterClickClient(
            [
                {
                    "operation": _choice(
                        "CLICK",
                        {
                            "CLICK": "c",
                            "SCROLL_DOWN": "s",
                            "SCROLL_UP": "u",
                            "WAIT": "w",
                            "BLOCKED": "b",
                        },
                    ),
                    "click_target": _choice("1", {"1": "Continue"}),
                }
            ]
        )
        result = run_browser_goal(
            goal="Click Continue then the provider dies",
            session=session,
            client=client,
            max_steps=5,
            min_actions_before_done=1,
        )
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["failure_phase"], "operation_deadline")
        self.assertTrue(result["reconcile_before_retry"])
        self.assertEqual(result["attempted_action_count"], 1)
        self.assertEqual(result["jev_request_count"], 2)
        self.assertTrue(any(item.get("failed") is True for item in result["decisions"]))
        self.assertEqual(session.clicks, ["1"])
        self.assertEqual(result["url"], dest)
        action = result["actions"][0]
        self.assertEqual(action["operation"], "CLICK")
        self.assertTrue(action["action_dispatched"])
        self.assertTrue(action["effect_observed"])
        self.assertTrue(action["effect_confirmed"])
        self.assertEqual(action["effect_status"], "url_changed")
        self.assertFalse(result["goal_verified"])
        self.assertFalse(result["verified"])





class SnapConfinementDetectionTests(unittest.TestCase):
    """A Snap-confined browser is recognised from its real identity or its content."""

    def test_variant_named_snap_wrapper_is_detected_by_content(self):
        # A Snap-confined wrapper can be found under any file name (a snap alias
        # or a PATH shim), so the rule must read the entry point, not the name.
        with tempfile.TemporaryDirectory() as root:
            wrapper = Path(root) / "chromium-snap-alias"
            wrapper.write_text('#!/bin/sh\nexec /snap/bin/chromium "$@"\n', encoding="utf-8")
            wrapper.chmod(0o755)
            self.assertTrue(_is_snap_chromium(wrapper))

    def test_variant_named_wrapper_resolves_to_the_confined_binary(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            wrapper = root_path / "chromium-snap-alias"
            snap = root_path / "chromium"
            wrapper.write_text('#!/bin/sh\nexec /snap/bin/chromium "$@"\n', encoding="utf-8")
            wrapper.chmod(0o755)
            snap.write_text("binary", encoding="utf-8")
            self.assertEqual(_resolve_browser_binary(wrapper, snap_binary=snap), snap)

    def test_quoted_snap_exec_is_detected(self):
        with tempfile.TemporaryDirectory() as root:
            wrapper = Path(root) / "chromium"
            wrapper.write_text('#!/bin/sh\nexec "/snap/bin/chromium" "$@"\n', encoding="utf-8")
            wrapper.chmod(0o755)
            self.assertTrue(_is_snap_chromium(wrapper))

    def test_unquoted_snap_exec_is_detected(self):
        with tempfile.TemporaryDirectory() as root:
            wrapper = Path(root) / "chromium"
            wrapper.write_text('#!/bin/sh\nexec /snap/bin/chromium -- "$@"\n', encoding="utf-8")
            wrapper.chmod(0o755)
            self.assertTrue(_is_snap_chromium(wrapper))

    def test_variant_named_snap_wrapper_uses_the_confined_profile_directory(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            wrapper = home / "bin" / "chromium-snap-alias"
            wrapper.parent.mkdir(parents=True)
            wrapper.write_text('#!/bin/sh\nexec /snap/bin/chromium "$@"\n', encoding="utf-8")
            wrapper.chmod(0o755)
            with mock.patch("pathlib.Path.home", return_value=home):
                temporary = _browser_profile_dir(wrapper)
                try:
                    profile = Path(temporary.name)
                    expected_root = home / "snap" / "chromium" / "common"
                    self.assertEqual(profile.parent, expected_root)
                    self.assertTrue(profile.is_dir())
                    self.assertEqual(
                        Path(os.path.realpath(profile.parent)).parent,
                        Path(os.path.realpath(expected_root)).parent,
                    )
                finally:
                    temporary.cleanup()

    def test_ordinary_browser_file_is_not_snap_confined(self):
        with tempfile.TemporaryDirectory() as root:
            wrapper = Path(root) / "chromium"
            wrapper.write_text('#!/bin/sh\nexec /opt/google/chrome/chrome "$@"\n', encoding="utf-8")
            wrapper.chmod(0o755)
            self.assertFalse(_is_snap_chromium(wrapper))

    def test_missing_path_and_none_are_not_snap_confined(self):
        self.assertFalse(_is_snap_chromium(Path("/nonexistent/switchyard/chromium")))
        self.assertFalse(_is_snap_chromium(None))

    def test_directory_is_never_read_as_a_wrapper(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(_is_snap_chromium(Path(root)))

    def test_symlink_to_snap_wrapper_is_detected(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            real = root_path / "real-wrapper"
            real.write_text('#!/bin/sh\nexec /snap/bin/chromium "$@"\n', encoding="utf-8")
            real.chmod(0o755)
            link = root_path / "chromium-browser"
            try:
                link.symlink_to(real)
            except OSError:
                self.skipTest("symlinks are unavailable on this platform")
            self.assertTrue(_is_snap_chromium(link))

    def test_native_binary_control_is_not_snap_confined(self):
        with tempfile.TemporaryDirectory() as root:
            binary = Path(root) / "chrome"
            binary.write_bytes(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 64)
            binary.chmod(0o755)
            self.assertFalse(_is_snap_chromium(binary))
            self.assertEqual(_resolve_browser_binary(binary), binary)

    def test_non_snap_profile_dir_never_falls_back_to_the_confined_root(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            ordinary = home / "bin" / "chromium"
            ordinary.parent.mkdir(parents=True)
            ordinary.write_text("", encoding="utf-8")
            ordinary.chmod(0o755)
            with mock.patch("pathlib.Path.home", return_value=home):
                with mock.patch.dict(
                    os.environ, {"XDG_RUNTIME_DIR": str(home / "run")}, clear=False
                ):
                    (home / "run").mkdir(parents=True, exist_ok=True)
                    temporary = _browser_profile_dir(ordinary)
                    try:
                        profile = Path(temporary.name)
                        self.assertNotEqual(
                            profile.parent, home / "snap" / "chromium" / "common"
                        )
                    finally:
                        temporary.cleanup()

    def test_unusable_snap_common_directory_returns_typed_error(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            (home / "snap").write_text("not a directory", encoding="utf-8")
            with mock.patch("pathlib.Path.home", return_value=home):
                with self.assertRaises(BrowserStartupError) as caught:
                    _browser_profile_dir(Path("/snap/bin/chromium"))
        self.assertEqual(caught.exception.code, "snap_profile_unavailable")
        self.assertIn("snap/chromium/common", str(caught.exception))

    @unittest.skipUnless(os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() != 0, "POSIX non-root permissions")
    def test_unwritable_snap_common_directory_returns_typed_error(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            common = home / "snap" / "chromium" / "common"
            common.mkdir(parents=True)
            common.chmod(0o555)
            try:
                with mock.patch("pathlib.Path.home", return_value=home):
                    try:
                        _browser_profile_dir(Path("/snap/bin/chromium"))
                    except BrowserStartupError as exc:
                        caught = exc
                    except OSError as exc:
                        self.fail(f"unconverted {type(exc).__name__}")
                    else:
                        self.fail("expected BrowserStartupError")
            finally:
                common.chmod(0o755)
        self.assertEqual(caught.code, "snap_profile_unavailable")

    def test_handler_preserves_snap_profile_unavailable(self):
        class Context:
            def __init__(self):
                self.tools = {}

            def get_config(self, _key, default=None):
                return default

            def register_auxiliary_task(self, *_args, **_kwargs):
                pass

            def register_tool(self, *, name, handler, **_kwargs):
                self.tools[name] = handler

            def register_skill(self, *_args, **_kwargs):
                pass

            def register_hook(self, *_args, **_kwargs):
                pass

        context = Context()
        with mock.patch.object(hermes_switchyard, "_secret", return_value="fixture-key"):
            hermes_switchyard.register(context)
            with mock.patch.object(
                hermes_switchyard.browser_use,
                "run_browser_goal",
                side_effect=BrowserStartupError(
                    "snap_profile_unavailable",
                    "Snap Chromium requires an accessible ~/snap/chromium/common directory",
                ),
            ):
                result = json.loads(
                    context.tools["jev_computer_use"](
                        {
                            "goal": "Public Wikipedia race starting on Cat",
                            "app": "Chrome",
                        }
                    )
                )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "snap_profile_unavailable")


class SnapBrowserLaunchIntegrationTests(unittest.TestCase):
    """The confined profile directory is proven by launching the real browser."""

    def _real_snap_binary(self):
        candidate = Path("/snap/bin/chromium")
        if os.name == "nt" or not candidate.is_file():
            self.skipTest("no Snap Chromium entry point on this host")
        return candidate

    def _real_python_launcher(self, module_dir: Path, executable: Path, marker: Path):
        launcher = module_dir / "launcher.py"
        launcher.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "from unittest import mock\n"
            "from hermes_switchyard import browser_use\n"
            f"wrapper = Path({str(executable)!r})\n"
            "with mock.patch.object(browser_use, '_browser_binary', return_value=wrapper):\n"
            "    session = browser_use.ChromiumSession('https://example.org/', headed=False)\n"
            "    launched = session._proc.args[0] if session._proc is not None else ''\n"
            f"    marker = Path({str(marker)!r})\n"
            "    marker.write_text(chr(10).join([launched, session._tmpdir.name]), encoding='utf-8')\n"
            "    session.close()\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        return launcher

    def test_real_snap_browser_launch_uses_the_confined_profile(self):
        self._real_snap_binary()
        with tempfile.TemporaryDirectory() as node_root:
            node = Path(node_root)
            module_dir = node / "src"
            module_dir.mkdir()
            source_package = Path(hermes_switchyard.__file__).parent
            shutil.copytree(
                source_package,
                module_dir / "hermes_switchyard",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            executable = node / "snap-alias-chromium"
            executable.write_text(
                '#!/bin/sh\nexec /snap/bin/chromium "$@"\n', encoding="utf-8"
            )
            executable.chmod(0o755)
            marker = node / "profile.txt"
            launcher = self._real_python_launcher(module_dir, executable, marker)
            env = dict(os.environ)
            env["PYTHONPATH"] = str(module_dir)
            env.pop("PYTHONDONTWRITEBYTECODE", None)
            try:
                completed = subprocess.run(
                    ["python3", str(launcher)],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=90,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                self.skipTest(f"the real browser launch could not run here: {exc}")
            if completed.returncode != 0:
                self.skipTest(
                    "the confined browser could not launch in this environment: "
                    f"rc={completed.returncode} {completed.stderr[-400:]}"
                )
            self.assertTrue(marker.is_file(), "the launch did not record a profile directory")
            launched, profile_text = marker.read_text(encoding="utf-8").split("\n", 1)
            self.assertEqual(
                Path(os.path.realpath(launched)),
                Path(os.path.realpath(executable)),
                "the launch did not use the supplied Snap wrapper",
            )
            profile_dir = Path(profile_text.strip())
            self.assertEqual(
                Path(os.path.realpath(profile_dir)).parent,
                Path(os.path.realpath(Path.home() / "snap" / "chromium" / "common")),
            )
            self.assertFalse(profile_dir.exists(), "the temporary profile was not cleaned up")
class ScriptedClient:
    """A client whose script entries are either answers or a raised failure."""

    def __init__(self, script: list):
        self.script = script
        self.calls: list[dict] = []

    @contextmanager
    def request_budget(self, max_requests: int = 256, *, deadline_seconds=None):
        yield

    def decide(self, state, questions, **kwargs):
        self.calls.append({"state": state, "questions": dict(questions)})
        entry = self.script[len(self.calls) - 1]
        if isinstance(entry, BaseException):
            raise entry
        return {
            "answers": entry,
            "latency_ms": 11,
            "model": "jev-latest",
            "usage": {"input_tokens": 8, "output_tokens": 2},
        }


class StaticSession:
    """One page that never changes: models actions with no observable effect."""

    def __init__(self, page: dict):
        self.page = page
        self.clicks: list[str] = []
        self.scrolls: list[str] = []

    def observe(self) -> dict:
        return dict(self.page)

    def click(self, element_id: str, label: str = "", href: str = "") -> None:
        self.clicks.append(element_id)

    def scroll(self, direction: str) -> None:
        self.scrolls.append(direction)

    def wait(self, seconds: float = 0.2) -> None:
        return None

    def close(self) -> None:
        return None


class WindowedSession:
    """A long page whose snapshot offers one viewport-sized window of targets.

    The windowed offering models the shipped snapshot contract: only targets
    near the current viewport are offered, and scrolling advances the window.
    """

    def __init__(self, total: int = 60, window: int = 48):
        self.total = total
        self.window = window
        self.offset = 0
        self.clicks: list[str] = []
        self.scrolls: list[str] = []
        self.url = "https://example.org/list"

    def _element(self, index: int) -> dict:
        return {
            "id": str(index),
            "role": "link",
            "label": f"Article item {index}",
            "href": f"https://example.org/item/{index}",
            "in_viewport": index - self.offset <= 10,
        }

    def observe(self) -> dict:
        if self.url != "https://example.org/list":
            return {"url": self.url, "title": self.url.rsplit("/", 1)[-1], "text": "Article", "elements": []}
        indexes = range(self.offset + 1, min(self.offset + self.window, self.total) + 1)
        return {
            "url": self.url,
            "title": "Article list",
            "text": f"Listing {self.total} articles",
            "elements": [self._element(index) for index in indexes],
        }

    def click(self, element_id: str, label: str = "", href: str = "") -> None:
        self.clicks.append(element_id)
        self.url = f"https://example.org/item/{element_id}"

    def scroll(self, direction: str) -> None:
        self.scrolls.append(direction)
        if direction == "down":
            self.offset = min(self.offset + 12, self.total - self.window)
        else:
            self.offset = max(self.offset - 12, 0)

    def wait(self, seconds: float = 0.2) -> None:
        return None

    def close(self) -> None:
        return None


class BrowserReliabilityTests(unittest.TestCase):
    """Regression coverage for the DOM observation, evidence, and startup defects."""

    def test_completion_predicate_stops_without_another_decision(self):
        cat = "https://en.wikipedia.org/wiki/Cat"
        felidae = "https://en.wikipedia.org/wiki/Felidae"
        session = FakeSession(
            {
                cat: {
                    "title": "Cat",
                    "text": "The cat is a domestic species.",
                    "elements": [{"id": "1", "role": "link", "label": "Felidae", "href": felidae}],
                },
                felidae: {"title": "Felidae", "text": "The cat family.", "elements": []},
            }
        )
        client = ScriptedClient(
            [
                {
                    "operation": _choice("CLICK", {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b"}),
                    "click_target": _choice("1", {"1": "Felidae"}),
                }
            ]
        )
        result = run_browser_goal(
            goal="Open the Felidae article",
            session=session,
            client=client,
            max_steps=5,
            completion_condition={"title_contains": "Felidae"},
        )
        self.assertEqual(result["status"], "completion_candidate")
        self.assertEqual(result["completion_source"], "local_predicate")
        self.assertTrue(result["completion"]["satisfied"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["jev_request_count"], 1)
        self.assertEqual(result["verified"], False)
        self.assertEqual(result["verification_owner"], "coordinator")

    def test_completion_predicate_is_never_sent_to_the_provider(self):
        session = StaticSession(
            {
                "url": "https://en.wikipedia.org/wiki/Ada_Lovelace",
                "title": "Ada Lovelace",
                "text": "Ada Lovelace was an English mathematician.",
                "elements": [{"id": "1", "role": "link", "label": "Analytical Engine", "href": "https://en.wikipedia.org/wiki/Analytical_Engine"}],
            }
        )
        client = ScriptedClient(
            [
                {
                    "operation": _choice("DONE", {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b", "DONE": "d"}),
                    "click_target": _choice("1", {"1": "Analytical Engine"}),
                }
            ]
        )
        result = run_browser_goal(
            goal="Read the Ada Lovelace article",
            session=session,
            client=client,
            max_steps=3,
            completion_condition={"title_contains": "Never Mentioned Target"},
        )
        # A provider DONE cannot satisfy, relax, or even see the local predicate.
        self.assertEqual(result["status"], "completion_candidate")
        self.assertEqual(result["completion_source"], "provider_decision")
        self.assertFalse(result["completion"]["satisfied"])
        payload = json.dumps(client.calls[0]["state"]) + json.dumps(client.calls[0]["questions"])
        self.assertNotIn("Never Mentioned Target", payload)
        self.assertNotIn("completion", payload)

    def test_derived_quoted_title_predicate_stops_before_any_request(self):
        session = StaticSession(
            {
                "url": "https://en.wikipedia.org/wiki/Analytical_Engine",
                "title": "Analytical Engine",
                "text": "The Analytical Engine was a proposed mechanical computer.",
                "elements": [],
            }
        )
        client = ScriptedClient([])
        result = run_browser_goal(
            goal='Open the article and stop when title contains "Analytical Engine"',
            session=session,
            client=client,
            max_steps=3,
        )
        self.assertEqual(result["status"], "completion_candidate")
        self.assertEqual(result["completion_predicate"], {"source": "derived_goal_title", "title_contains": "Analytical Engine"})
        self.assertEqual(result["attempted_request_count"], 0)
        self.assertEqual(client.calls, [])

    def test_invalid_completion_condition_is_refused_locally(self):
        session = StaticSession({"url": "https://example.org/", "title": "Home", "text": "Home", "elements": []})
        for condition in ({"url_equals": "http://localhost/"}, {"unknown_field": "x"}, {"title_contains": ""}, {}):
            with self.subTest(condition=condition):
                with self.assertRaises(ValueError):
                    run_browser_goal(
                        goal="Read the page",
                        session=session,
                        client=ScriptedClient([]),
                        max_steps=2,
                        completion_condition=condition,
                    )

    def test_provider_timeout_after_a_click_keeps_partial_evidence(self):
        cat = "https://en.wikipedia.org/wiki/Cat"
        felidae = "https://en.wikipedia.org/wiki/Felidae"
        session = FakeSession(
            {
                cat: {
                    "title": "Cat",
                    "text": "The cat is a domestic species.",
                    "elements": [{"id": "1", "role": "link", "label": "Felidae", "href": felidae}],
                },
                felidae: {"title": "Felidae", "text": "The cat family.", "elements": []},
            }
        )
        client = ScriptedClient(
            [
                {
                    "operation": _choice("CLICK", {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b"}),
                    "click_target": _choice("1", {"1": "Felidae"}),
                },
                TimeoutError("provider request timed out"),
            ]
        )
        result = run_browser_goal(goal="Open Felidae", session=session, client=client, max_steps=5)
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["failure_phase"], "operation_deadline")
        self.assertTrue(result["reconcile_before_retry"])
        self.assertEqual(result["attempted_action_count"], 1)
        self.assertEqual(result["jev_request_count"], 2)
        self.assertTrue(any(item.get("failed") is True for item in result["decisions"]))
        self.assertEqual(result["actions"][0]["action_dispatched"], True)
        self.assertEqual(len(result["last_state_hash"]), 64)
        self.assertTrue(result["reconcile_before_retry"])

    def test_malformed_response_keeps_partial_evidence(self):
        cat = "https://en.wikipedia.org/wiki/Cat"
        felidae = "https://en.wikipedia.org/wiki/Felidae"
        session = FakeSession(
            {
                cat: {
                    "title": "Cat",
                    "text": "The cat is a domestic species.",
                    "elements": [{"id": "1", "role": "link", "label": "Felidae", "href": felidae}],
                },
                felidae: {"title": "Felidae", "text": "The cat family.", "elements": []},
            }
        )
        client = ScriptedClient(
            [
                {
                    "operation": _choice("CLICK", {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b"}),
                    "click_target": _choice("1", {"1": "Felidae"}),
                },
                {"operation": {"choice": "CLICK"}, "unexpected": {"choice": "1"}},
            ]
        )
        result = run_browser_goal(goal="Open Felidae", session=session, client=client, max_steps=5)
        self.assertEqual(result["status"], "provider_failure")
        self.assertEqual(result["failure_reason"], "validation_failure")
        self.assertEqual(result["click_count"], 1)
        self.assertEqual(result["attempted_request_count"], 2)

    def test_noop_click_does_not_claim_a_confirmed_effect(self):
        session = StaticSession(
            {
                "url": "https://example.org/",
                "title": "Home",
                "text": "Home page body",
                "elements": [{"id": "1", "role": "link", "label": "Next page", "href": "https://example.org/next"}],
            }
        )
        client = ScriptedClient(
            [
                {
                    "operation": _choice("CLICK", {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b"}),
                    "click_target": _choice("1", {"1": "Next page"}),
                },
                {
                    "operation": _choice("DONE", {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b", "DONE": "d"}),
                    "click_target": _choice("1", {"1": "Next page"}),
                },
            ]
        )
        result = run_browser_goal(goal="Reach the next page", session=session, client=client, max_steps=5)
        action = result["actions"][0]
        self.assertEqual(action["action_dispatched"], True)
        self.assertIs(action["effect_observed"], False)
        self.assertIs(action["effect_confirmed"], False)
        self.assertEqual(action["effect_status"], "unchanged")
        self.assertIs(action["goal_verified"], False)
        self.assertEqual(result["effect_observed_count"], 0)

    def test_repeated_unchanged_state_stops_without_another_decision(self):
        session = StaticSession(
            {
                "url": "https://example.org/",
                "title": "Home",
                "text": "Home page body",
                "elements": [{"id": "1", "role": "link", "label": "Next page", "href": "https://example.org/next"}],
            }
        )
        click = {
            "operation": _choice("CLICK", {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b"}),
            "click_target": _choice("1", {"1": "Next page"}),
        }
        client = ScriptedClient([click, click, click])
        result = run_browser_goal(goal="Reach the next page", session=session, client=client, max_steps=10)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failure_phase"], "no_progress")
        self.assertEqual(result["stalled_observations"], 2)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(result["attempted_action_count"], 2)
        self.assertTrue(result["reconcile_before_retry"])

    def test_ineffective_scroll_recovers_locally_then_stops(self):
        session = StaticSession(
            {
                "url": "https://example.org/",
                "title": "Home",
                "text": "Home page body",
                "elements": [{"id": "1", "role": "link", "label": "Next page", "href": "https://example.org/next"}],
            }
        )
        scroll = {
            "operation": _choice("SCROLL_DOWN", {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b"}),
            "click_target": _choice("1", {"1": "Next page"}),
        }
        client = ScriptedClient([scroll, scroll, scroll, scroll])
        result = run_browser_goal(goal="Find a later target", session=session, client=client, max_steps=10)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failure_phase"], "no_progress")
        # Two paid decisions; recovery scrolls stay local inside each step.
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(session.scrolls.count("down"), 2 + 2 * browser_use.LOCAL_SCROLL_RECOVERY_LIMIT)
        self.assertNotIn("local_scroll_recovery", result["actions"][0])
        recovery = [item for item in result["actions"] if str(item.get("label") or "").startswith("local_scroll_recovery_")]
        self.assertEqual(len(recovery), 2 * browser_use.LOCAL_SCROLL_RECOVERY_LIMIT)
        self.assertTrue(all(item["action_dispatched"] for item in recovery))

    def test_targets_beyond_the_first_snapshot_become_reachable(self):
        session = WindowedSession(total=60, window=48)
        client = ScriptedClient(
            [
                {
                    "operation": _choice("SCROLL_DOWN", {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b"}),
                    "click_target": _choice("1", {"1": "Article item 1"}),
                },
                {
                    "operation": _choice("CLICK", {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b"}),
                    "click_target": _choice("58", {"58": "Article item 58"}),
                },
                {
                    "operation": _choice("DONE", {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b", "DONE": "d"}),
                },
            ]
        )
        result = run_browser_goal(goal="Open article 58", session=session, client=client, max_steps=6)
        first_offered = json.dumps(client.calls[0]["state"]["elements"])
        second_offered = json.dumps(client.calls[1]["state"]["elements"])
        self.assertNotIn("Article item 58", first_offered)
        self.assertIn("Article item 58", second_offered)
        self.assertEqual(result["click_count"], 1)
        self.assertEqual(session.clicks, ["58"])
        self.assertEqual(result["url"], "https://example.org/item/58")
        self.assertEqual(result["status"], "completion_candidate")

    def test_unsupported_dom_capabilities_refuse_before_any_request(self):
        cases = [
            ({"goal": "type my account name into the search box", "text_inputs": [{"field_label": "Search", "value": "x"}]}, "dom_text_input_unsupported"),
            ({"goal": "log in and open the settings page"}, "dom_authentication_unsupported"),
            ({"goal": "upload the report as an attachment"}, "dom_file_upload_unsupported"),
            ({"goal": "use the browser I have open to check the cart"}, "dom_existing_session_unsupported"),
            ({"goal": "open the article", "allowed_hotkeys": ["SUBMIT"]}, "dom_hotkey_unsupported"),
            ({"goal": "authenticate at https://example.org/"}, "dom_authentication_unsupported"),
            ({"goal": "sign up for an account"}, "dom_authentication_unsupported"),
            ({"goal": "register for the site"}, "dom_authentication_unsupported"),
            ({"goal": "log into the site"}, "dom_authentication_unsupported"),
            ({"goal": "typing into the search field"}, "dom_text_input_unsupported"),
            ({"goal": "uploading the report as an attachment"}, "dom_file_upload_unsupported"),
            ({"goal": "download the attachments"}, "dom_file_upload_unsupported"),
            ({"goal": "use my session to check the cart"}, "dom_existing_session_unsupported"),
            ({"goal": "already authenticated, open the page"}, "dom_existing_session_unsupported"),
            ]
        for args, expected in cases:
            with self.subTest(args=args):
                client = ScriptedClient([])
                with mock.patch.object(
                    browser_use,
                    "open_browser_session",
                    side_effect=AssertionError("no browser may start for an unsupported capability"),
                ):
                    result = run_browser_goal(
                        goal=args["goal"],
                        start_url="https://example.org/",
                        client=client,
                        max_steps=4,
                        text_inputs=args.get("text_inputs"),
                        allowed_hotkeys=args.get("allowed_hotkeys"),
                    )
                self.assertEqual(result["status"], "unsupported_capability")
                self.assertEqual(result["failure_phase"], "capability")
                self.assertIn(expected, result["unsupported_capabilities"])
                self.assertEqual(result["attempted_request_count"], 0)
                self.assertEqual(client.calls, [])

    def test_backend_and_session_identity_are_reported(self):
        class IdentifiedSession(StaticSession):
            def backend_info(self):
                return {
                "backend": "chromium_dom",
                "session_mode": "headless_ephemeral",
                "browser": "chromium",
                "confinement": "snap",
                "setup_ms": 412.5,
                }

        session = IdentifiedSession(
            {
                "url": "https://example.org/",
                "title": "Home",
                "text": "Home page body",
                "elements": [],
            }
        )
        client = ScriptedClient(
            [
                {
                    "operation": _choice("DONE", {"SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b", "DONE": "d"}),
                }
            ]
        )
        result = run_browser_goal(goal="Confirm the page", session=session, client=client, max_steps=2)
        self.assertEqual(result["backend"], "chromium_dom")
        self.assertEqual(result["session_mode"], "headless_ephemeral")
        self.assertEqual(result["browser"], "chromium")
        self.assertEqual(result["browser_confinement"], "snap")
        self.assertEqual(result["session_setup_ms"], 412.5)

    def test_browser_startup_failure_returns_a_bounded_local_diagnostic(self):
        client = ScriptedClient([])
        with mock.patch.object(
            browser_use,
            "open_browser_session",
            side_effect=RuntimeError("browser exited before debugger listen 1 SingletonLock: Permission denied"),
        ):
            result = run_browser_goal(
                goal="Read the article",
                start_url="https://example.org/",
                client=client,
                max_steps=3,
            )
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["failure_phase"], "browser_startup")
            self.assertEqual(result["failure_reason"], "browser_profile_not_writable")
            self.assertEqual(result["attempted_request_count"], 0)
            self.assertEqual(client.calls, [])

    def test_low_confidence_or_ambiguous_decision_abstains_before_dispatch(self):
        page = {
            "url": "https://example.org/",
            "title": "Start",
            "text": "one link",
            "elements": [{"id": "1", "label": "Next page", "href": "https://example.org/next", "role": "link"}],
        }
        low_confidence = {
            "operation": {"choice": "CLICK", "probabilities": {"CLICK": 0.2, "DONE": 0.2, "SCROLL_DOWN": 0.2, "SCROLL_UP": 0.2, "WAIT": 0.2}, "confidence": 0.01},
            "click_target": {"choice": "1", "probabilities": {"1": 1.0}, "confidence": 0.9},
        }
        ambiguous = {
            "operation": {"choice": "CLICK", "probabilities": {"CLICK": 0.51, "DONE": 0.49}, "confidence": 0.9},
            "click_target": {"choice": "1", "probabilities": {"1": 1.0}, "confidence": 0.9},
        }
        for answers, expected_phase in ((low_confidence, "low_confidence"), (ambiguous, "ambiguous_decision")):
            with self.subTest(expected_phase=expected_phase):
                session = StaticSession(page)
                client = ScriptedClient([answers])
                result = run_browser_goal(goal="Open the next page", session=session, client=client, max_steps=4)
                self.assertEqual(result["status"], "abstained")
                self.assertEqual(result["failure_phase"], expected_phase)
                self.assertEqual(session.clicks, [])
                self.assertEqual(result["action_dispatched_count"], 0)
                self.assertEqual(result["attempted_request_count"], 1)

    def test_disconnect_after_dispatch_preserves_action_evidence(self):
        class DisconnectingSession(StaticSession):
            def __init__(self, page):
                super().__init__(page)
                self.disconnected = False

            def observe(self):
                if self.disconnected:
                    raise OSError("browser transport closed")
                return dict(self.page)

            def click(self, element_id, label="", href=""):
                self.clicks.append(element_id)
                self.disconnected = True

        session = DisconnectingSession({
            "url": "https://example.org/",
            "title": "Start",
            "text": "one link",
            "elements": [{"id": "1", "label": "Next page", "href": "https://example.org/next", "role": "link"}],
        })
        client = ScriptedClient([{
            "operation": _choice("CLICK", {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "DONE": "d", "BLOCKED": "b"}),
            "click_target": _choice("1", {"1": "Next page"}),
        }])
        result = run_browser_goal(goal="Open the next page", session=session, client=client, max_steps=4)
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["failure_phase"], "action")
        self.assertEqual(result["failure_reason"], "transport_failure")
        self.assertEqual(result["action_dispatched_count"], 1)
        self.assertTrue(result["reconcile_before_retry"])
        action = result["actions"][0]
        self.assertIs(action["action_dispatched"], True)
        self.assertIsNone(action["effect_observed"])

    def test_receipt_state_hash_matches_the_reported_page_after_an_action(self):
        session = FakeSession({
            "https://en.wikipedia.org/wiki/Cat": {
                "title": "Cat",
                "text": "Cat article body",
                "elements": [{"id": "1", "label": "Felidae", "href": "https://en.wikipedia.org/wiki/Felidae", "role": "link"}],
            },
            "https://en.wikipedia.org/wiki/Felidae": {
                "title": "Felidae",
                "text": "Felidae article body",
                "elements": [],
            },
        })
        client = ScriptedClient([{
            "operation": _choice("CLICK", {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "DONE": "d", "BLOCKED": "b"}),
            "click_target": _choice("1", {"1": "Felidae"}),
        }])
        result = run_browser_goal(goal="Reach Felidae", session=session, client=client, max_steps=1)
        self.assertEqual(result["status"], "budget_exhausted")
        final_page = session.observe()
        self.assertEqual(result["last_state_hash"], browser_use._observation_signature(final_page))

    def test_snap_profile_dir_unwritable_raises_structured_error(self):
        if os.name == "nt":
            raise unittest.SkipTest("snap confinement profiles are a Unix path")
        with tempfile.TemporaryDirectory() as raw:
            blocker = Path(raw) / "not-a-directory"
            blocker.write_text("x", encoding="utf-8")
            with mock.patch.object(Path, "home", classmethod(lambda cls: blocker)):
                with self.assertRaises(browser_use.BrowserStartupError) as ctx:
                    browser_use._browser_profile_dir("snap")
                self.assertEqual(ctx.exception.code, "snap_profile_unavailable")

    def test_wrapper_script_is_detected_as_snap_confinement(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            wrapper = base / "chromium-browser"
            wrapper.write_text("#!/bin/sh\nexec snap run chromium \"$@\"\n", encoding="utf-8")
            self.assertTrue(browser_use._is_snap_confined(wrapper))
            plain = base / "chromium"
            plain.write_bytes(b"\x7fELF\x02\x01\x01\x00binary")
            self.assertFalse(browser_use._is_snap_confined(plain))
            direct = base / "snap" / "bin" / "chromium"
            direct.parent.mkdir(parents=True, exist_ok=True)
            direct.write_text("", encoding="utf-8")
            self.assertTrue(browser_use._is_snap_confined(direct))

    def test_unsandboxed_browser_is_preferred_over_a_snap_wrapper(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            wrapper = base / "chromium-browser"
            wrapper.write_text("#!/bin/sh\nexec snap run chromium \"$@\"\n", encoding="utf-8")
            native = base / "chromium"
            native.write_bytes(b"\x7fELF\x02\x01\x01\x00binary")

            def which(name):
                return {"chromium-browser": str(wrapper), "chromium": str(native)}.get(name)

            hidden = {"PROGRAMFILES": "", "PROGRAMFILES(X86)": "", "LOCALAPPDATA": ""}
            with mock.patch.dict(os.environ, hidden, clear=False), mock.patch.object(browser_use.shutil, "which", side_effect=which):
                path, family, confinement = browser_use._browser_binary_details()
            self.assertEqual(path, native)
            self.assertEqual(family, "chromium")
            self.assertEqual(confinement, "none")

            only_wrapper = lambda name: str(wrapper) if name == "chromium-browser" else None
            with mock.patch.dict(os.environ, hidden, clear=False), mock.patch.object(browser_use.shutil, "which", side_effect=only_wrapper):
                path, family, confinement = browser_use._browser_binary_details()
            self.assertEqual(path, wrapper)
            self.assertEqual(confinement, "snap")

    def test_snap_confined_profile_lives_in_the_snap_area_and_cleans_up_exactly(self):
        if os.name == "nt":
            raise unittest.SkipTest("snap confinement profiles are a Unix path")
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            with mock.patch.object(Path, "home", classmethod(lambda cls: home)):
                with browser_use._browser_profile_dir("snap") as directory:
                    created = Path(directory)
                    self.assertEqual(created.parent, home / "snap" / "chromium" / "common")
                    (created / "marker.txt").write_text("x", encoding="utf-8")
                self.assertFalse(created.exists())
            base = home / "snap" / "chromium" / "common"
            self.assertTrue(base.is_dir())
            self.assertEqual(list(base.iterdir()), [])

    def test_ephemeral_profile_is_used_by_default(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            runtime = home / "runtime"
            runtime.mkdir()
            if os.name == "nt":
                with mock.patch.dict(os.environ, {"TEMP": str(home), "LOCALAPPDATA": str(home)}, clear=False):
                    with browser_use._browser_profile_dir("none") as directory:
                        self.assertEqual(Path(directory).parent, home / "hermes-switchyard")
                return
            with mock.patch.object(Path, "home", classmethod(lambda cls: home)):
                with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}, clear=False):
                    with browser_use._browser_profile_dir("none") as directory:
                        self.assertEqual(Path(directory).parent, runtime)


    def test_inconsistent_choice_probability_abstains_instead_of_using_top_gap(self):
        # choice=CLICK is only 0.1 while DONE is 0.9; the old top-two gap was 0.8
        # and would have dispatched. Margin must be P(choice)-max(other).
        page = {
            "url": "https://example.org/",
            "title": "Start",
            "text": "one link",
            "elements": [{"id": "1", "label": "Next page", "href": "https://example.org/next", "role": "link"}],
        }
        answers = {
            "operation": {
                "choice": "CLICK",
                "probabilities": {"DONE": 0.9, "CLICK": 0.1},
                "confidence": 0.9,
            },
            "click_target": {"choice": "1", "probabilities": {"1": 1.0}, "confidence": 0.9},
        }
        session = StaticSession(page)
        client = ScriptedClient([answers])
        result = run_browser_goal(goal="Open the next page", session=session, client=client, max_steps=4)
        self.assertEqual(result["status"], "abstained")
        self.assertEqual(result["failure_phase"], "ambiguous_decision")
        self.assertEqual(session.clicks, [])
        confidence, margin = browser_use._decision_quality(answers["operation"])
        self.assertEqual(confidence, 0.9)
        self.assertAlmostEqual(margin, -0.8)

    def test_deadline_before_any_action_does_not_require_reconciliation(self):
        session = StaticSession(
            {
                "url": "https://example.org/",
                "title": "Home",
                "text": "Home page body",
                "elements": [{"id": "1", "role": "link", "label": "Next page", "href": "https://example.org/next"}],
            }
        )

        class ImmediateDeadlineClient(ScriptedClient):
            @contextmanager
            def request_budget(self, max_requests: int = 256, *, deadline_seconds=None):
                raise TimeoutError("deadline before any action")
                yield  # pragma: no cover

        client = ImmediateDeadlineClient([])
        result = run_browser_goal(goal="Reach the next page", session=session, client=client, max_steps=3)
        self.assertEqual(result["status"], "deadline_exceeded")
        self.assertEqual(result["failure_phase"], "deadline")
        self.assertFalse(result["reconcile_before_retry"])
        self.assertEqual(result["attempted_action_count"], 0)

    def test_nonconsecutive_observation_cycle_stops_before_another_decision(self):
        class AlternatingSession(StaticSession):
            def __init__(self):
                self.phase = 0
                self.clicks = []
                self.scrolls = []
                self.pages = [
                    {
                        "url": "https://example.org/a",
                        "title": "A",
                        "text": "page A",
                        "elements": [{"id": "1", "role": "link", "label": "Go", "href": "https://example.org/b"}],
                    },
                    {
                        "url": "https://example.org/b",
                        "title": "B",
                        "text": "page B",
                        "elements": [{"id": "1", "role": "link", "label": "Go", "href": "https://example.org/a"}],
                    },
                ]

            def observe(self):
                return dict(self.pages[self.phase % 2])

            def click(self, element_id, label="", href=""):
                self.clicks.append(element_id)
                self.phase += 1

        session = AlternatingSession()
        click = {
            "operation": _choice("CLICK", {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b"}),
            "click_target": _choice("1", {"1": "Go"}),
        }
        client = ScriptedClient([click, click, click, click])
        result = run_browser_goal(goal="Keep moving", session=session, client=client, max_steps=10)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failure_phase"], "no_progress")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(session.clicks), 2)

    def test_scroll_recovery_records_dispatches_and_keeps_unsafe_observation(self):
        class RecoveringUnsafeSession(StaticSession):
            def __init__(self):
                super().__init__(
                    {
                        "url": "https://example.org/",
                        "title": "Home",
                        "text": "Home page body",
                        "elements": [{"id": "1", "role": "link", "label": "Next page", "href": "https://example.org/next"}],
                    }
                )
                self.scrolls = []

            def scroll(self, direction):
                self.scrolls.append(direction)
                if len(self.scrolls) >= 2:
                    self.page = {
                        "url": "http://127.0.0.1/private",
                        "title": "Private",
                        "text": "should not complete",
                        "elements": [],
                    }

            def observe(self):
                return dict(self.page)

        session = RecoveringUnsafeSession()
        scroll = {
            "operation": _choice("SCROLL_DOWN", {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b"}),
            "click_target": _choice("1", {"1": "Next page"}),
        }
        client = ScriptedClient([scroll])
        result = run_browser_goal(goal="Find a later target", session=session, client=client, max_steps=4)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failure_phase"], "unsafe_url")
        self.assertGreaterEqual(result["action_dispatched_count"], 2)
        self.assertTrue(any(item.get("label", "").startswith("local_scroll_recovery_") for item in result["actions"]))
        self.assertNotEqual(result["status"], "completion_candidate")

    def test_successful_scroll_recovery_updates_parent_effect_and_counts(self):
        class RecoveringSession(StaticSession):
            def __init__(self):
                super().__init__(
                    {
                        "url": "https://example.org/",
                        "title": "Home",
                        "text": "Home page body",
                        "elements": [{"id": "1", "role": "link", "label": "Next page", "href": "https://example.org/next"}],
                    }
                )
                self.scrolls = []

            def scroll(self, direction):
                self.scrolls.append(direction)
                if len(self.scrolls) >= 2:
                    self.page = {
                        "url": "https://example.org/",
                        "title": "Home",
                        "text": "Home page body with more content revealed",
                        "elements": [{"id": "1", "role": "link", "label": "Later", "href": "https://example.org/later"}],
                    }

            def observe(self):
                return dict(self.page)

        session = RecoveringSession()
        scroll = {
            "operation": _choice("SCROLL_DOWN", {"CLICK": "c", "SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b"}),
            "click_target": _choice("1", {"1": "Next page"}),
        }
        done = {
            "operation": _choice("DONE", {"SCROLL_DOWN": "s", "SCROLL_UP": "u", "WAIT": "w", "BLOCKED": "b", "DONE": "d"}),
        }
        client = ScriptedClient([scroll, done])
        result = run_browser_goal(goal="Reveal more", session=session, client=client, max_steps=4)
        self.assertTrue(result["actions"][0].get("local_scroll_recovery"))
        self.assertTrue(result["actions"][0]["effect_observed"])
        recovery = [item for item in result["actions"] if str(item.get("label") or "").startswith("local_scroll_recovery_")]
        self.assertGreaterEqual(len(recovery), 1)
        self.assertTrue(all(item["action_dispatched"] for item in recovery))
        self.assertGreaterEqual(result["action_dispatched_count"], 2)



class SnapshotRecallTests(unittest.TestCase):
    """Real-browser checks that the snapshot never drops the candidate tail.

    The article-body selector must *extend* the candidate set, not replace it. A
    page whose body has many paragraph links and a large number of other
    interactive targets must still offer those others as the viewport moves.
    """

    BODY_LINKS = 10
    OTHER_LINKS = 60

    @classmethod
    def setUpClass(cls):
        if os.environ.get("SWITCHYARD_LIVE_BROWSER_TESTS") != "1":
            raise unittest.SkipTest(
                "set SWITCHYARD_LIVE_BROWSER_TESTS=1 to run live Chromium snapshot recall tests"
            )
        binary, _, _ = browser_use._browser_binary_details()
        if binary is None:
            raise unittest.SkipTest("no Chromium-family browser is available")

    def _fixture_spec(self) -> dict:
        return {
            "body": [
                {"href": f"https://example.com/body-{n}", "label": f"Body link {n}"}
                for n in range(1, self.BODY_LINKS + 1)
            ],
            "other": [
                {"href": f"https://example.com/other-{n}", "label": f"Other link {n}"}
                for n in range(1, self.OTHER_LINKS + 1)
            ],
        }

    def _load_fixture(self, session) -> None:
        # The fixture is built with explicit DOM calls against a test-local
        # structure; no markup is injected and nothing leaves the browser instance.
        # about:blank is ready synchronously, so the public-URL readiness gate does
        # not apply here.
        session._cdp("Page.navigate", url="about:blank")
        session.wait(0.2)
        session._evaluate(
            """(() => {
              const spec = %s;
              const anchor = item => {
                const a = document.createElement("a");
                a.setAttribute("href", item.href);
                a.textContent = item.label;
                return a;
              };
              const main = document.createElement("main");
              const parser = document.createElement("div");
              parser.className = "mw-parser-output";
              for (const item of spec.body) {
                const p = document.createElement("p");
                p.appendChild(anchor(item));
                parser.appendChild(p);
              }
              main.appendChild(parser);
              const other = document.createElement("div");
              other.id = "other";
              for (const item of spec.other) {
                const row = document.createElement("div");
                row.style.height = "120px";
                row.appendChild(anchor(item));
                other.appendChild(row);
              }
              main.appendChild(other);
              document.body.replaceChildren(main);
              return true;
            })()"""
            % json.dumps(self._fixture_spec())
        )
        session.wait(0.2)

    def test_candidate_tail_survives_a_populated_article_body(self):
        with browser_use.ChromiumSession("https://example.com") as session:
            self._load_fixture(session)
            first = session.observe()
            total = int(first.get("candidates_total") or 0)
            offered = first.get("elements") or []
            labels = [item["label"] for item in offered]

            expected = self.BODY_LINKS + self.OTHER_LINKS
            # The scan is viewport-windowed, so one snapshot considers only a
            # bounded prefix of the page, while the article-body selector
            # extends the candidate set instead of replacing it.
            self.assertLess(total, expected)
            self.assertLessEqual(len(offered), browser_use.MAX_PAGE_ELEMENTS)
            self.assertTrue(any(label.startswith("Body link") for label in labels))
            self.assertTrue(any(label.startswith("Other link") for label in labels))

            seen = set(labels)
            for _ in range(14):
                session.scroll("down")
                observed = session.observe()
                seen.update(item["label"] for item in observed.get("elements") or [])
            tail_labels = {label for label in seen if label.startswith("Other link")}
            self.assertTrue(
                any(int(label.rsplit(" ", 1)[1]) > 48 for label in tail_labels),
                "no target beyond the first 48 ever became reachable after scrolling",
            )
            self.assertIn(
                f"Other link {self.OTHER_LINKS}",
                tail_labels,
                "the candidate tail never became reachable",
            )

            second = session.observe()
            second_ids = [item["id"] for item in second.get("elements") or []]
            third = session.observe()
            third_ids = [item["id"] for item in third.get("elements") or []]
            self.assertEqual(set(third_ids), set(second_ids))

if __name__ == "__main__":
    unittest.main()
