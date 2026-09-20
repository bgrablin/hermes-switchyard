"""Behavior checks for the DOM browser loop."""

from __future__ import annotations

import json
import unittest
from contextlib import contextmanager
from unittest import mock

from hermes_switchyard.browser_use import infer_start_url, requested_web_start, run_browser_goal
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


if __name__ == "__main__":
    unittest.main()
