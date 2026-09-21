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
                    with self.assertRaises(BrowserStartupError) as caught:
                        _browser_profile_dir(Path("/snap/bin/chromium"))
            finally:
                common.chmod(0o755)
        self.assertEqual(caught.exception.code, "snap_profile_unavailable")

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
            f"    marker = Path({str(marker)!r})\n"
            "    marker.write_text(session._tmpdir.name, encoding='utf-8')\n"
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
            profile_dir = Path(marker.read_text(encoding="utf-8").strip())
            self.assertEqual(
                Path(os.path.realpath(profile_dir)).parent,
                Path(os.path.realpath(Path.home() / "snap" / "chromium" / "common")),
            )
            self.assertFalse(profile_dir.exists(), "the temporary profile was not cleaned up")


if __name__ == "__main__":
    unittest.main()
