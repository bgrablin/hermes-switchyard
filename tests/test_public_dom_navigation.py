"""Public-page navigation fixtures and DOM target recall."""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from contextlib import nullcontext
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from hermes_switchyard import browser_use

PAGES = json.loads((Path(__file__).parent / "fixtures/public_dom_navigation.json").read_text(encoding="utf-8"))["pages"]


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.current = {"id": str(len(self.links) + 1), "role": "link", "href": dict(attrs)["href"], "label": ""}

    def handle_data(self, data):
        if self.current is not None:
            self.current["label"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.links.append(self.current)
            self.current = None


class ChoiceClient:
    def __init__(self, destination):
        self.destination = destination
        self.calls = []

    def request_budget(self, *args, **kwargs):
        return nullcontext()

    def decide(self, state, questions, **kwargs):
        self.calls.append({"state": state, "questions": questions})
        criteria = questions["operation"]["criteria"]
        operation = "CLICK" if any(item["href"] == self.destination for item in state["elements"]) else "BLOCKED"
        answers = {"operation": choice(operation, criteria)}
        if "click_target" in questions:
            target = next((item["id"] for item in state["elements"] if item["href"] == self.destination), next(iter(questions["click_target"]["criteria"])))
            answers["click_target"] = choice(target, questions["click_target"]["criteria"])
        return {"answers": answers, "latency_ms": 1, "model": "fixture", "usage": {}}


def choice(selected, criteria):
    return {"choice": selected, "confidence": 0.95, "probabilities": {key: (1.0 if key == selected else 0.0) for key in criteria}}


def _execute_snapshot(runner, html, url):
    try:
        return subprocess.run(
            ["node", str(runner)],
            input=json.dumps({"html": html, "url": url, "script": browser_use._SNAPSHOT_JS}),
            text=True, capture_output=True, timeout=30, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # TimeoutExpired may retain bytes even with text=True. Keep diagnostics
        # to runner stage markers, not the public markup or JS payload.
        stderr = exc.stderr or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        stage = "no stage received (launch/startup)"
        for line in stderr.splitlines():
            if line.startswith("fixture-stage="):
                stage = line.removeprefix("fixture-stage=")
        raise AssertionError(
            f"Node snapshot fixture timed out after {exc.timeout}s at {url}; last runner stage={stage}"
        ) from exc


class PublicFixtureTests(unittest.TestCase):
    def test_snapshot_timeout_identifies_last_runner_stage_and_keeps_outer_bound(self):
        runner = Path(__file__).parent / "fixtures/public_dom_snapshot_runner.cjs"
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(
            ["node", str(runner)], 30, stderr=b"fixture-stage=vm-start\n",
        )) as run:
            with self.assertRaisesRegex(AssertionError, r"30s.*vm-start"):
                _execute_snapshot(runner, PAGES[0]["html"], PAGES[0]["start"])
        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_snapshot_runner_reports_vm_progress_and_stops_an_infinite_script(self):
        runner = Path(__file__).parent / "fixtures/public_dom_snapshot_runner.cjs"
        executed = subprocess.run(
            ["node", str(runner)],
            input=json.dumps({"html": PAGES[0]["html"], "url": PAGES[0]["start"],
                              "script": "while (true) {}"}),
            text=True, capture_output=True, timeout=30, check=False,
        )
        self.assertNotEqual(executed.returncode, 0)
        self.assertIn("fixture-stage=vm-start", executed.stderr)
        self.assertNotIn("fixture-stage=vm-complete", executed.stderr)
        self.assertIn("ERR_SCRIPT_EXECUTION_TIMEOUT", executed.stderr)

    def test_production_snapshot_extracts_saved_public_links_offline(self):
        """Execute the actual snapshot JS, then ChromiumSession's safety filter."""
        runner = Path(__file__).parent / "fixtures/public_dom_snapshot_runner.cjs"
        for fixture in PAGES:
            with self.subTest(fixture=fixture["start"]):
                html = fixture["html"]
                if fixture["target_label"] == "Zeus":
                    # Zero-size links ahead of Zeus used to exhaust the offer bound.
                    zero_size = "".join(
                        f'<a data-zero-size="1" href="https://en.wikipedia.org/wiki/Apollo">'
                        f'Hidden public link {index}</a>' for index in range(60)
                    )
                    html = html.replace('<div class="mw-parser-output">',
                                        '<div class="mw-parser-output">' + zero_size)
                executed = _execute_snapshot(runner, html, fixture["start"])
                self.assertEqual(executed.returncode, 0, executed.stderr)
                snapshot = json.loads(executed.stdout)
                with mock.patch.object(browser_use.ChromiumSession, "_evaluate", return_value=snapshot):
                    session = object.__new__(browser_use.ChromiumSession)
                    page = session.observe()
                targets = [item for item in page["elements"]
                           if item["href"] == fixture["destination"]]
                self.assertTrue(targets, f"snapshot omitted {fixture['target_label']}")
                self.assertEqual(targets[0]["label"], fixture["target_label"])
                self.assertFalse(any(item["label"].startswith("Hidden public link")
                                     for item in page["elements"]))

    def test_chromium_session_types_into_a_matched_public_field(self):
        session = object.__new__(browser_use.ChromiumSession)
        with (
            mock.patch.object(session, "_evaluate", return_value={"ok": True}) as evaluate,
            mock.patch.object(session, "wait"),
            mock.patch.object(session, "_wait_ready"),
        ):
            session.type_text("1", "Ada Lovelace", label="search")
        script = evaluate.call_args.args[0]
        self.assertIn('liveLabel !== "search"', script)
        self.assertIn('el.value = "Ada Lovelace"', script)

    @unittest.skipUnless(os.environ.get("SWITCHYARD_LIVE_BROWSER_TESTS") == "1", "requires opt-in Chromium")
    def test_real_public_wikipedia_search_field_accepts_caller_text(self):
        with browser_use.ChromiumSession("https://en.wikipedia.org/wiki/Special:Search") as session:
            field = next(item for item in session.observe()["elements"]
                         if item.get("kind") == "type" and item["label"] == "search")
            session.type_text(field["id"], "Ada Lovelace", label=field["label"])
            selected_value = session._evaluate(
                f'(window.__hermesSwitchyardClickNodes || new Map()).get({json.dumps(field["id"])})?.value'
            )
            self.assertEqual(selected_value, "Ada Lovelace")
            search_button = next(item for item in session.observe()["elements"]
                                 if item.get("kind") == "click" and item["label"] == "Search")
            session.click(search_button["id"], label=search_button["label"])
            self.assertNotEqual(session.observe()["url"], "https://en.wikipedia.org/wiki/Special:Search")

    def test_public_links_survive_safety_filter_and_reach_jev(self):
        for fixture in PAGES:
            with self.subTest(fixture=fixture["start"]):
                parser = Links()
                parser.feed(fixture["html"])
                offered = browser_use._safe_elements(parser.links)
                self.assertIn(fixture["destination"], [item["href"] for item in offered])
                client = ChoiceClient(fixture["destination"])
                class Session:
                    url = fixture["start"]
                    def observe(self):
                        return {"url": self.url, "title": "Public fixture", "text": fixture["html"], "elements": parser.links}
                    def click(self, element_id, label="", href=""):
                        self.url = href
                    def type_text(self, element_id, value, label=""):
                        raise AssertionError("not offered")
                    def scroll(self, direction):
                        raise AssertionError("not selected")
                    def wait(self, seconds=0.2):
                        pass
                    def close(self):
                        pass
                result = browser_use.run_browser_goal(goal="Follow the public link", session=Session(), client=client, max_steps=1)
                self.assertEqual(client.calls[0]["state"]["elements"], offered)
                self.assertIn(fixture["target_label"], client.calls[0]["questions"]["click_target"]["criteria"][offered[0]["id"]])
                self.assertEqual(result["actions"][0]["effect_status"], "url_changed")

    @unittest.skipUnless(os.environ.get("SWITCHYARD_LIVE_BROWSER_TESTS") == "1", "requires opt-in Chromium")
    def test_snapshot_extraction_from_saved_public_markup(self):
        for fixture in PAGES:
            with self.subTest(fixture=fixture["start"]):
                with browser_use.ChromiumSession(fixture["start"]) as session:
                    # This checked-in public-only fixture contains no scripts or event handlers.
                    session._evaluate("document.body.innerHTML = " + json.dumps(fixture["html"]))
                    page = session.observe()
                    self.assertIn(fixture["destination"], [item["href"] for item in page["elements"]])

    @unittest.skipUnless(os.environ.get("SWITCHYARD_LIVE_BROWSER_TESTS") == "1", "requires opt-in Chromium")
    def test_zero_size_links_do_not_evict_public_wikipedia_target(self):
        fixture = PAGES[1]
        with browser_use.ChromiumSession(fixture["start"]) as session:
            # The checked-in fixture is public-only and script-free.
            session._evaluate("document.body.innerHTML = " + json.dumps(fixture["html"]))
            session._evaluate("""(() => {
                const main = document.querySelector('main');
                const hidden = document.createElement('div');
                hidden.style.display = 'none';
                for (let n = 0; n < 60; n++) {
                    const p = document.createElement('p');
                    const a = document.createElement('a');
                    a.href = 'https://en.wikipedia.org/wiki/Apollo';
                    a.textContent = 'Hidden public link ' + n;
                    p.appendChild(a);
                    hidden.appendChild(p);
                }
                const spacer = document.createElement('div');
                spacer.style.height = '900px';
                main.prepend(hidden, spacer);
                return true;
            })()""")
            page = session.observe()
            self.assertIn(fixture["destination"], [item["href"] for item in page["elements"]])
            self.assertFalse(any(item["label"].startswith("Hidden public link") for item in page["elements"]))

    @unittest.skipUnless(os.environ.get("SWITCHYARD_LIVE_BROWSER_TESTS") == "1", "requires opt-in Chromium")
    def test_public_example_link_reaches_iana_over_https(self):
        with browser_use.ChromiumSession(PAGES[0]["start"]) as session:
            page = session.observe()
            target = next(item for item in page["elements"] if item["href"] == PAGES[0]["destination"])
            session.click(target["id"], label=target["label"], href=target["href"])
            self.assertEqual(session.observe()["url"], "https://www.iana.org/help/example-domains")
            self.assertEqual(session.destination_report()["navigation_blocks"], 0)


if __name__ == "__main__":
    unittest.main()
