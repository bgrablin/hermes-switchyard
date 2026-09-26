"""Public-page navigation fixtures and DOM target recall."""
from __future__ import annotations

import json
import os
import re
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
    def test_observation_document_identity_is_browser_owned(self):
        session = object.__new__(browser_use.ChromiumSession)
        session._main_contexts = {"frame": "browser-context-a"}
        supplied = {"url": "https://example.org/form", "document_id": "page-controlled",
                    "text": "Form", "elements": []}
        with (mock.patch.object(session, "_evaluate", return_value=supplied),
              mock.patch.object(session, "_cdp", return_value={"frameTree": {"frame": {
                  "id": "frame", "loaderId": "browser-loader-a"}}})):
            first = session.observe()
        self.assertNotEqual(first["document_id"], "page-controlled")
        self.assertIn("browser-loader-a", first["document_id"])

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
                with (mock.patch.object(browser_use.ChromiumSession, "_evaluate", return_value=snapshot),
                      mock.patch.object(browser_use.ChromiumSession, "_browser_document", return_value=("frame:loader", "context"))):
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
        session._document_id, session._document_context = "frame:loader", "context"
        with (
            mock.patch.object(session, "_evaluate", side_effect=[{"ok": True, "changed": True}, True]) as evaluate,
            mock.patch.object(session, "_browser_document", return_value=("frame:loader", "context")),
            mock.patch.object(session, "wait"),
            mock.patch.object(session, "_wait_ready"),
        ):
            result = session.type_text("1", "Ada Lovelace", label="search")
        self.assertEqual(result, {"accepted": True, "changed": True})
        script = evaluate.call_args_list[0].args[0]
        self.assertIn('liveLabel !== "search"', script)
        self.assertIn('el.value = "Ada Lovelace"', script)

    def test_rejected_field_does_not_return_confirmed_typing(self):
        session = object.__new__(browser_use.ChromiumSession)
        session._document_id, session._document_context = "frame:loader", "context"
        with (
            mock.patch.object(session, "_evaluate", side_effect=[{"ok": True, "changed": True}, False]) as evaluate,
            mock.patch.object(session, "_browser_document", return_value=("frame:loader", "context")),
            mock.patch.object(session, "wait"),
            mock.patch.object(session, "_wait_ready"),
        ):
            result = session.type_text("1", "Ada", label="Search")
        self.assertEqual(result, {"accepted": False, "changed": False})
        self.assertEqual(evaluate.call_count, 2, "a post-settle readback is required")

    @unittest.skipUnless(os.environ.get("SWITCHYARD_LIVE_BROWSER_TESTS") == "1", "requires opt-in Chromium")
    def test_real_chromium_synthetic_form_retention_and_document_replacement(self):
        """Use a public HTTPS origin, but only synthetic form content and no hosted decisions."""
        with browser_use.ChromiumSession("https://example.org/") as session:
            # Fixed synthetic markup only; no page-supplied HTML is interpolated.
            form = ('<main><label for="author">Author</label><input id="author">'
                    '<label for="title">Article title</label><input id="title">'
                    '<label for="other">Search</label><input id="other">'
                    '<label for="selected">Search</label><input id="selected">'
                    '<label for="rejected">Reject</label><input id="rejected"></main>')
            session._evaluate(f"document.body.innerHTML = {json.dumps(form)}")
            session._evaluate("document.querySelector('#rejected').addEventListener('change', e => { e.target.value = ''; })")
            session._evaluate("window.__hermesSwitchyardDocumentId = 'spoofed-same-token'")
            first = session.observe()
            fields = {item["label"]: item for item in first["elements"] if item["kind"] == "type" and item["label"] != "Search"}
            search = [item for item in first["elements"] if item["label"] == "Search"]
            self.assertEqual(len(search), 2)
            for label, value in (("Author", "Ada"), ("Article title", "Computing")):
                self.assertEqual(session.type_text(fields[label]["id"], value, label=label),
                                 {"accepted": True, "changed": True})
                self.assertTrue(session.text_retained(fields[label]["id"], value, first["document_id"]))
            # Reordering must not change the selected stable ID.
            selected_id = next(item["id"] for item in search if session._evaluate(
                f'(window.__hermesSwitchyardClickNodes || new Map()).get({json.dumps(item["id"])})?.id'
            ) == "selected")
            session._evaluate("document.querySelector('main').prepend(document.querySelector('#selected'))")
            session.observe()
            self.assertTrue(session.type_text(selected_id, "Chosen", label="Search")["accepted"])
            self.assertEqual(session._evaluate("document.querySelector('#other').value"), "")
            self.assertEqual(session.type_text(fields["Reject"]["id"], "Denied", label="Reject"),
                             {"accepted": False, "changed": False})
            self.assertFalse(session.text_retained(fields["Reject"]["id"], "Denied", first["document_id"]))
            editable = '<main><label for="editable">Notes</label><div id="editable" contenteditable="true"></div><p>Public context remains.</p></main>'
            session._evaluate(f"document.body.innerHTML = {json.dumps(editable)}")
            editable_before = session.observe()
            editable_id = next(item["id"] for item in editable_before["elements"]
                               if item["label"] == "Notes" and item["kind"] == "type")
            self.assertEqual(session.type_text(editable_id, "CallerValueNeverToProvider", label="Notes"),
                             {"accepted": True, "changed": True})
            editable_page = session.observe()
            self.assertNotIn("CallerValueNeverToProvider", editable_page["text"])
            self.assertIn("Public context remains", editable_page["text"])
            self.assertTrue(any(item["label"] == "Notes" and item["kind"] == "type"
                                for item in editable_page["elements"]))
            session._cdp("Page.reload", ignoreCache=True)
            session._wait_ready()
            replacement = '<main><label for="fresh">Author</label><input id="fresh"></main>'
            session._evaluate(f"document.body.innerHTML = {json.dumps(replacement)}")
            session._evaluate("window.__hermesSwitchyardDocumentId = 'spoofed-same-token'")
            second = session.observe()
            self.assertEqual(first["url"], second["url"])
            self.assertNotEqual(first["document_id"], second["document_id"])
            with self.assertRaisesRegex(RuntimeError, "stale browser document"):
                session._document_id = first["document_id"]
                session.type_text("1", "Wrong target", label="Author")
            session.observe()
            fresh = next(item for item in second["elements"] if item["kind"] == "type")
            self.assertEqual(session.type_text(fresh["id"], "Ada", label="Author"),
                             {"accepted": True, "changed": True})

    @unittest.skipUnless(os.environ.get("SWITCHYARD_LIVE_BROWSER_TESTS") == "1", "requires opt-in Chromium")
    def test_real_chromium_loop_redacts_free_text_and_keeps_structured_fields(self):
        """Run the real snapshot and loop on synthetic markup with a local scripted client; no Jev call."""
        marker = "[editable text]"
        form = ('<main><h1>Example form</h1><label for="author">Author</label><input id="author">'
                '<label for="notes">Notes</label><div id="notes" contenteditable="true"></div>'
                '<button type="button">Search</button>'
                '<p><a href="https://example.org/example?item=1">Example item 1</a> public context.</p></main>')

        def outside_markers(text, value):
            spans = [(item.start(), item.end()) for item in re.finditer(re.escape(marker), text)]
            start = text.find(value)
            while start >= 0:
                if not any(left <= start and start + len(value) <= right for left, right in spans):
                    return True
                start = text.find(value, start + 1)
            return False

        class LocalClient:
            def __init__(self, steps):
                self.steps, self.calls = steps, []

            def decide(self, state, questions, **kwargs):
                self.calls.append({"state": json.loads(json.dumps(state)),
                                   "questions": json.loads(json.dumps(questions))})
                operation, target = self.steps[len(self.calls) - 1]
                answers = {"operation": choice(operation, questions["operation"]["criteria"])}
                for key in ("click_target", "type_target"):
                    if key in questions:
                        options = questions[key]["criteria"]
                        wanted = "type_target" if operation == "TYPE_TEXT" else "click_target"
                        answers[key] = choice(target if key == wanted else next(iter(options)), options)
                return {"answers": answers, "latency_ms": 1, "model": "local-fixture", "usage": {}}

        with browser_use.ChromiumSession("https://example.org/") as session:
            for author, notes in (("1", "e"), ("Example", "example")):
                with self.subTest(author=author, notes=notes):
                    # Fixed synthetic markup only; no page-supplied HTML is interpolated.
                    session._evaluate("delete window.__hermesSwitchyardTargets; "
                                      "delete window.__hermesSwitchyardClickNodes; "
                                      f"document.title = 'Example form'; document.body.innerHTML = {json.dumps(form)}")
                    ids = {item["label"]: item["id"] for item in session.observe()["elements"]}
                    url = session.observe()["url"]
                    client = LocalClient([("TYPE_TEXT", ids["Author"]), ("TYPE_TEXT", ids["Notes"]),
                                          ("CLICK", ids["Search"]), ("DONE", None)])
                    goal = "Fill Author and Notes, then click Search on the example form"
                    result = browser_use.run_browser_goal(
                        goal=goal, session=session, client=client, max_steps=4,
                        text_inputs=[{"field_label": "Author", "value": author},
                                     {"field_label": "Notes", "value": notes}])
                    self.assertEqual(len(client.calls), 4)
                    # A short value must collide with a real offered element ID.
                    self.assertIn("1", ids.values())
                    self.assertEqual(session._evaluate("document.querySelector('#author').value"), author)
                    self.assertEqual(session._evaluate("document.querySelector('#notes').textContent"), notes)
                    self.assertEqual(result["status"], "completion_candidate")
                    self.assertEqual(result["completion_source"], "provider_decision")
                    self.assertEqual(result["goal"], goal)
                    self.assertEqual(result["url"], url)
                    self.assertEqual([item["element"] for item in result["actions"]],
                                     [ids["Author"], ids["Notes"], ids["Search"]])
                    self.assertEqual([item["effect_status"] for item in result["actions"]],
                                     ["text_entered", "text_entered", "unchanged"])
                    for call in client.calls:
                        state, questions = call["state"], call["questions"]
                        self.assertEqual(state["goal"], goal)
                        self.assertEqual(state["page"]["url"], url)
                        self.assertEqual({item["id"] for item in state["elements"]}, set(ids.values()))
                        link = next(item for item in state["elements"] if item["id"] == ids["Example item 1"])
                        # Origin stays; a path or query part that carries a caller value is masked.
                        self.assertTrue(link["href"].startswith("https://example.org/"), link["href"])
                        for value in (author, notes):
                            self.assertFalse(outside_markers(link["href"][len("https://example.org/"):], value))
                        roles = {item["id"]: (item["role"], item["label"]) for item in state["elements"]}
                        for key in ("click_target", "type_target"):
                            for element_id, text in questions.get(key, {}).get("criteria", {}).items():
                                self.assertEqual(text, f"[{element_id}] {roles[element_id][0]} {roles[element_id][1]}")
                        free_text = [state["page"]["text"], state["page"]["title"],
                                     *(label for _role, label in roles.values()),
                                     *(item["label"] for item in state["recent_actions"])]
                        for text in free_text:
                            self.assertEqual(text.count("["), text.count(marker), text)
                            for value in (author, notes):
                                self.assertFalse(outside_markers(text, value), (value, text))
                    # Page prose that does not contain a caller value remains visible.
                    self.assertIn("public", client.calls[-1]["state"]["page"]["text"])
                    for text in [result["title"], *(item["label"] for item in result["actions"]),
                                 *(item["title"] for item in result["actions"])]:
                        for value in (author, notes):
                            self.assertFalse(outside_markers(text, value), (value, text))

    @unittest.skipUnless(os.environ.get("SWITCHYARD_LIVE_BROWSER_TESTS") == "1", "requires opt-in Chromium")
    def test_real_chromium_reflected_url_stays_local_and_exact(self):
        """A real page copies typed text into an href and its own URL; no network navigation and no Jev call."""
        marker = "[editable text]"
        origin = "https://example.org"
        # Fixed synthetic markup. Typing rewrites the Results href; Search puts the
        # value in the page URL with history.replaceState, which stays same-document.
        form = ('<main><h1>Example search</h1><label for="terms">Search terms</label><input id="terms">'
                '<a id="results" href="/results">Results</a> <a href="/about">About</a>'
                '<button type="button" id="go">Search</button></main>')
        script = ("document.querySelector('#terms').addEventListener('input', event => {"
                  " document.querySelector('#results').setAttribute('href', '/results?q=' +"
                  " encodeURIComponent(event.target.value) + '&page=2'); });"
                  "document.querySelector('#go').addEventListener('click', () => {"
                  " history.replaceState(null, '', '/search/' +"
                  " encodeURIComponent(document.querySelector('#terms').value) + '/page-2'); });")

        def private(url, value):
            if not url.startswith(origin + "/"):
                return False
            rest = url[len(origin):]
            spans = [(item.start(), item.end()) for item in re.finditer(re.escape(marker), rest)]
            for needle in {value, value.replace(" ", "%20"), value.replace(" ", "+")}:
                start = rest.find(needle)
                while start >= 0:
                    if not any(left <= start and start + len(needle) <= right for left, right in spans):
                        return False
                    start = rest.find(needle, start + 1)
            return True

        class LocalClient:
            def __init__(self, steps):
                self.steps, self.calls = steps, []

            def decide(self, state, questions, **kwargs):
                self.calls.append({"state": json.loads(json.dumps(state)),
                                   "questions": json.loads(json.dumps(questions))})
                operation, target = self.steps[len(self.calls) - 1]
                answers = {"operation": choice(operation, questions["operation"]["criteria"])}
                for key in ("click_target", "type_target"):
                    if key in questions:
                        options = questions[key]["criteria"]
                        wanted = "type_target" if operation == "TYPE_TEXT" else "click_target"
                        answers[key] = choice(target if key == wanted else next(iter(options)), options)
                return {"answers": answers, "latency_ms": 1, "model": "local-fixture", "usage": {}}

        with browser_use.ChromiumSession(origin + "/") as session:
            for value in ("Ada", "Ada Lovelace", "e", "1"):
                with self.subTest(value=value):
                    session._evaluate("delete window.__hermesSwitchyardTargets; "
                                      "delete window.__hermesSwitchyardClickNodes; "
                                      "history.replaceState(null, '', '/'); "
                                      f"document.title = 'Example search'; document.body.innerHTML = {json.dumps(form)}; "
                                      f"{script}")
                    ids = {item["label"]: item["id"] for item in session.observe()["elements"]}
                    client = LocalClient([("TYPE_TEXT", ids["Search terms"]), ("CLICK", ids["Search"]), ("DONE", None)])
                    result = browser_use.run_browser_goal(
                        goal="Search the example form", session=session, client=client, max_steps=3,
                        text_inputs=[{"field_label": "Search terms", "value": value}])
                    encoded = value.replace(" ", "%20")
                    # The exact reflected values stay local and drive the real page.
                    self.assertEqual(session._evaluate("document.querySelector('#terms').value"), value)
                    self.assertEqual(session._evaluate("location.pathname"), f"/search/{encoded}/page-2")
                    self.assertEqual(session._evaluate("document.querySelector('#results').href"),
                                     f"{origin}/results?q={encoded}&page=2")
                    self.assertEqual(len(client.calls), 3)
                    self.assertEqual(result["status"], "completion_candidate")
                    self.assertEqual([item["element"] for item in result["actions"]],
                                     [ids["Search terms"], ids["Search"]])
                    self.assertEqual([item["effect_status"] for item in result["actions"]],
                                     ["text_entered", "url_changed"])
                    # Call 2 sees the reflected href; call 3 sees the reflected page URL.
                    # Look up by ID: a short value such as ``e`` also masks the label.
                    hrefs = {item["id"]: item["href"] for item in client.calls[1]["state"]["elements"]}
                    self.assertEqual(hrefs[ids["About"]], origin + "/about")
                    # The origin stays exact. A short value can also mask a fixed segment such as ``results``.
                    self.assertTrue(hrefs[ids["Results"]].startswith(origin + "/"), hrefs[ids["Results"]])
                    self.assertIn("?", hrefs[ids["Results"]])
                    self.assertTrue(client.calls[2]["state"]["page"]["url"].startswith(origin + "/"))
                    self.assertNotEqual(client.calls[2]["state"]["page"]["url"], client.calls[1]["state"]["page"]["url"])
                    for call in client.calls:
                        state = call["state"]
                        urls = [state["page"]["url"], *(item["href"] for item in state["elements"] if item["href"]),
                                *(item["url"] for item in state["recent_actions"])]
                        for url in urls:
                            self.assertTrue(private(url, value), (value, url))
                    for url in [result["url"], *(item["url"] for item in result["actions"])]:
                        self.assertTrue(private(url, value), (value, url))

    @unittest.skipUnless(os.environ.get("SWITCHYARD_LIVE_BROWSER_TESTS") == "1", "requires opt-in Chromium")
    def test_real_chromium_case_variant_and_long_reflection_is_redacted(self):
        """A real page shows the typed value in upper case and past field bounds; no network and no Jev call."""
        marker = "[editable text]"
        # Fixed synthetic markup. Typing mirrors the value into CSS upper-case text,
        # a link label, and an upper-case title. innerText returns the upper case.
        form = ('<main><h1>Example search</h1><label for="terms">Search terms</label><input id="terms">'
                '<p>Results for <span id="echo" style="text-transform: uppercase"></span>. Public context.</p>'
                '<a id="more" href="/about">About</a><button type="button" id="go">Search</button></main>')
        script = ("document.querySelector('#terms').addEventListener('input', event => {"
                  " const text = event.target.value;"
                  " document.querySelector('#echo').textContent = text;"
                  " document.querySelector('#more').textContent = 'More ' + text.toUpperCase();"
                  " document.title = 'Search ' + text.toUpperCase(); });")

        def outside_markers(text, value):
            spans = [(item.start(), item.end()) for item in re.finditer(re.escape(marker), text)]
            folded, needle = text.casefold(), value.casefold()
            start = folded.find(needle)
            while start >= 0:
                if not any(left <= start and start + len(needle) <= right for left, right in spans):
                    return True
                start = folded.find(needle, start + 1)
            return False

        def fragments(text, value):
            """Return value fragments of 8 or more characters found outside markers."""
            rest = "".join(text.split(marker)).casefold()
            folded = value.casefold()
            return sorted({folded[i:i + 8] for i in range(len(folded) - 7) if folded[i:i + 8] in rest})

        class LocalClient:
            def __init__(self, steps):
                self.steps, self.calls = steps, []

            def decide(self, state, questions, **kwargs):
                self.calls.append({"state": json.loads(json.dumps(state)),
                                   "questions": json.loads(json.dumps(questions))})
                operation, target = self.steps[len(self.calls) - 1]
                answers = {"operation": choice(operation, questions["operation"]["criteria"])}
                for key in ("click_target", "type_target"):
                    if key in questions:
                        options = questions[key]["criteria"]
                        wanted = "type_target" if operation == "TYPE_TEXT" else "click_target"
                        answers[key] = choice(target if key == wanted else next(iter(options)), options)
                return {"answers": answers, "latency_ms": 1, "model": "local-fixture", "usage": {}}

        # 136 characters: the real snapshot cuts the link label to 120 inside the value.
        long_value = "Synthetic query " + "abcdefghij" * 12
        with browser_use.ChromiumSession("https://example.org/") as session:
            # "e" is a short value whose letters also occur in the marker.
            for value in ("Ada", "ada lovelace", "e", long_value):
                with self.subTest(value=value):
                    session._evaluate("delete window.__hermesSwitchyardTargets; "
                                      "delete window.__hermesSwitchyardClickNodes; "
                                      f"document.title = 'Example search'; document.body.innerHTML = {json.dumps(form)}; "
                                      f"{script}")
                    ids = {item["label"]: item["id"] for item in session.observe()["elements"]}
                    url = session.observe()["url"]
                    client = LocalClient([("TYPE_TEXT", ids["Search terms"]), ("CLICK", ids["Search"]), ("DONE", None)])
                    goal = "Search the example form"
                    result = browser_use.run_browser_goal(
                        goal=goal, session=session, client=client, max_steps=3,
                        text_inputs=[{"field_label": "Search terms", "value": value}])
                    # The real page reflects the value in another case; the exact value stays local.
                    self.assertEqual(session._evaluate("document.querySelector('#terms').value"), value)
                    self.assertIn(value.upper(), session._evaluate("document.body.innerText"))
                    self.assertEqual(len(client.calls), 3)
                    self.assertEqual(result["status"], "completion_candidate")
                    self.assertEqual(result["goal"], goal)
                    self.assertEqual(result["url"], url)
                    self.assertEqual([item["element"] for item in result["actions"]],
                                     [ids["Search terms"], ids["Search"]])
                    for call in client.calls[1:]:
                        state = call["state"]
                        self.assertEqual(state["goal"], goal)
                        self.assertEqual(state["page"]["url"], url)
                        self.assertEqual({item["id"] for item in state["elements"]}, set(ids.values()))
                        for text in [state["page"]["text"], state["page"]["title"],
                                     *(item["label"] for item in state["elements"]),
                                     *(item["label"] for item in state["recent_actions"])]:
                            self.assertEqual(text.count("["), text.count(marker), text)
                            self.assertFalse(outside_markers(text, value), (value, text))
                            self.assertEqual(fragments(text, value), [], text[-160:])
                    self.assertIn(marker, client.calls[1]["state"]["page"]["text"])
                    for text in [result["title"], *(item["label"] for item in result["actions"]),
                                 *(item["title"] for item in result["actions"])]:
                        self.assertFalse(outside_markers(text, value), (value, text))
                        self.assertEqual(fragments(text, value), [], text[-160:])

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
