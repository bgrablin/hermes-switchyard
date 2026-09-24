"""Browser startup diagnostics and ordered fallback, with synthetic launchers only.

No test here starts a real browser, opens a window, or makes a network
request. Launchers are small scripts: one crashes with a signal, one exits
with a code, and one answers the DevTools discovery endpoint on loopback.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import hermes_switchyard
from hermes_switchyard import browser_use, destination_policy
from hermes_switchyard.browser_use import _Candidate, run_browser_goal

PUBLIC = "93.184.216.34"
# Text a crashing browser might print. None of it may reach a diagnostic.
PRIVATE_MARKERS = (
    "https://private.example/secret?q=user-data",
    "user-typed-search-phrase",
    "operator-token-value",
)

_CRASH_TRAP = """#!/bin/sh
echo "[1:1:FATAL:zygote_host_impl_linux.cc] No usable sandbox! __PRIVATE_PATH__" >&2
echo "loading https://private.example/secret?q=user-data user-typed-search-phrase" >&2
echo "operator-token-value" >&2
echo "Trace/breakpoint trap (core dumped)" >&2
kill -TRAP $$
"""

_EXIT_CODE = """#!/bin/sh
echo "error while loading shared libraries: libnss3.so: cannot open shared object file" >&2
echo "https://private.example/secret?q=user-data" >&2
exit 127
"""

_UNCLASSIFIED_EXIT = """#!/bin/sh
echo "user-typed-search-phrase at __PRIVATE_PATH__" >&2
exit 3
"""

# Answers DevTools discovery on the requested port, like a browser that started.
_DEVTOOLS = """#!{python}
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port = int(next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--remote-debugging-port=")))
with open({argv_log!r}, "w", encoding="utf-8") as handle:
    json.dump(sys.argv[1:], handle)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps([{{"type": "page", "url": "about:blank",
            "webSocketDebuggerUrl": "ws://127.0.0.1:%d/devtools/page/T" % port}}]).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
HTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""


@unittest.skipIf(os.name == "nt", "synthetic launchers are POSIX shell scripts")
class BrowserStartupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.private_path = self.root / "private-profile"
        runtime = self.root / "runtime"
        runtime.mkdir(mode=0o700)
        # Per-run profiles land in this disposable directory, never the operator home.
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.runtime = runtime

    def _script(self, name: str, body: str) -> Path:
        path = self.root / "bin" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(body.replace("__PRIVATE_PATH__", str(self.private_path)), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def _devtools(self, name: str = "google-chrome") -> tuple[Path, Path]:
        argv_log = self.root / f"{name}.argv.json"
        body = _DEVTOOLS.format(python=sys.executable, argv_log=str(argv_log))
        return self._script(name, body), argv_log

    def _discovery(self, *candidates: _Candidate):
        return mock.patch.object(browser_use, "_discovered_candidates", return_value=list(candidates))

    def _assert_redacted(self, diagnostic: dict) -> None:
        text = json.dumps(diagnostic, sort_keys=True)
        for marker in PRIVATE_MARKERS:
            self.assertNotIn(marker, text)
        self.assertNotIn(str(self.private_path), text)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn("/", text.replace("\\/", ""))
        for attempt in diagnostic["attempts"]:
            self.assertIn(attempt["executable_class"], browser_use.BROWSER_EXECUTABLE_CLASSES)
            self.assertIn(attempt["browser_family"], browser_use.BROWSER_FAMILIES)
            self.assertIn(attempt["outcome"], browser_use.STARTUP_ATTEMPT_OUTCOMES)
            for reason in attempt["stderr_reasons"]:
                self.assertIn(reason, browser_use.STDERR_REASONS)

    def _assert_profiles_removed(self) -> None:
        self.assertEqual(list(self.runtime.iterdir()), [])

    def test_crash_reports_signal_and_allowlisted_reasons_then_falls_back(self):
        crash = self._script("chromium", _CRASH_TRAP)
        good, argv_log = self._devtools()
        with self._discovery(
            _Candidate(crash, "chromium", "none", "discovered"),
            _Candidate(good, "chrome", "none", "discovered"),
        ):
            diagnostic = browser_use.probe_browser_startup()
        self.assertEqual(diagnostic["outcome"], "started")
        self.assertIsNone(diagnostic["reason"])
        self.assertTrue(diagnostic["fallback_used"])
        first, second = diagnostic["attempts"]
        self.assertEqual(first["outcome"], "exited")
        self.assertEqual(first["exit_signal"], "SIGTRAP")
        self.assertIsNone(first["exit_code"])
        self.assertIsInstance(first["startup_ms"], float)
        self.assertIn("sandbox_unavailable", first["stderr_reasons"])
        self.assertIn("trap_signal", first["stderr_reasons"])
        self.assertEqual(first["executable_class"], "system")
        self.assertEqual(second["outcome"], "started")
        self.assertEqual(second["browser_family"], "chrome")
        self._assert_redacted(diagnostic)
        # The probe stays headless on about:blank, and all browser traffic goes
        # to a loopback proxy port; nothing is loaded or reached.
        argv = json.loads(argv_log.read_text(encoding="utf-8"))
        self.assertIn("--headless=new", argv)
        self.assertEqual(argv[-1], "about:blank")
        self.assertTrue(any(a.startswith("--proxy-server=http://127.0.0.1:") for a in argv))
        self.assertIn("--proxy-bypass-list=<-loopback>", argv)
        self._assert_profiles_removed()

    def test_every_candidate_crashing_reports_browser_crashed_with_exit_code(self):
        trap = self._script("chromium", _CRASH_TRAP)
        missing_lib = self._script("msedge", _EXIT_CODE)
        with self._discovery(
            _Candidate(trap, "chromium", "none", "discovered"),
            _Candidate(missing_lib, "edge", "none", "discovered"),
        ):
            diagnostic = browser_use.probe_browser_startup()
        self.assertEqual(diagnostic["outcome"], "failed")
        self.assertEqual(diagnostic["reason"], "browser_crashed")
        self.assertFalse(diagnostic["fallback_used"])
        self.assertEqual([a["outcome"] for a in diagnostic["attempts"]], ["exited", "exited"])
        last = diagnostic["attempts"][-1]
        self.assertEqual(last["exit_code"], 127)
        self.assertIsNone(last["exit_signal"])
        self.assertEqual(last["stderr_reasons"], ["shared_library_missing"])
        self._assert_redacted(diagnostic)
        self._assert_profiles_removed()

    def test_unclassified_stderr_is_dropped_not_echoed(self):
        opaque = self._script("chromium", _UNCLASSIFIED_EXIT)
        with self._discovery(_Candidate(opaque, "chromium", "none", "discovered")):
            diagnostic = browser_use.probe_browser_startup()
        [attempt] = diagnostic["attempts"]
        self.assertEqual(attempt["exit_code"], 3)
        self.assertEqual(attempt["stderr_reasons"], [])
        self._assert_redacted(diagnostic)

    def test_configured_executable_is_tried_first_and_crash_falls_back_to_discovery(self):
        configured = self._script("custom-chromium", _CRASH_TRAP)
        good, _argv = self._devtools()
        with self._discovery(_Candidate(good, "chrome", "none", "discovered")):
            diagnostic = browser_use.probe_browser_startup(browser_executable=str(configured))
        self.assertEqual([a["source"] for a in diagnostic["attempts"]], ["configured", "discovered"])
        self.assertEqual(diagnostic["attempts"][0]["exit_signal"], "SIGTRAP")
        self.assertEqual(diagnostic["outcome"], "started")
        self._assert_redacted(diagnostic)

    def test_configured_executable_validation(self):
        good, _argv = self._devtools()
        with self._discovery(_Candidate(good, "chrome", "none", "discovered")):
            relative = browser_use.probe_browser_startup(browser_executable="bin/chrome")
            non_string = browser_use.probe_browser_startup(browser_executable=["/usr/bin/chrome"])
            missing = browser_use.probe_browser_startup(browser_executable=str(self.root / "no-such-browser"))
            unset = browser_use.probe_browser_startup(browser_executable="  ")
        for diagnostic in (relative, non_string):
            self.assertEqual(diagnostic["outcome"], "failed")
            self.assertEqual(diagnostic["reason"], "browser_executable_invalid")
            self.assertEqual(diagnostic["attempts"], [])
        self.assertEqual([a["outcome"] for a in missing["attempts"]], ["unavailable", "started"])
        self.assertEqual(missing["attempts"][0]["source"], "configured")
        self.assertEqual(missing["outcome"], "started")
        self.assertEqual([a["source"] for a in unset["attempts"]], ["discovered"])
        self._assert_redacted(missing)

    def test_no_browser_reports_not_installed(self):
        with self._discovery():
            diagnostic = browser_use.probe_browser_startup()
        self.assertEqual(diagnostic["outcome"], "failed")
        self.assertEqual(diagnostic["reason"], "browser_not_installed")
        self.assertEqual(diagnostic["attempt_count"], 0)

    def test_debugger_timeout_ends_the_plan_without_trying_more_browsers(self):
        slow = self._script("chromium", "#!/bin/sh\nexec sleep 30\n")
        never = self._script("google-chrome", "#!/bin/sh\necho unreachable >&2\nexit 9\n")
        with (
            self._discovery(
                _Candidate(slow, "chromium", "none", "discovered"),
                _Candidate(never, "chrome", "none", "discovered"),
            ),
            mock.patch.object(browser_use, "DEBUGGER_START_SECONDS", 0.5),
        ):
            diagnostic = browser_use.probe_browser_startup()
        self.assertEqual(diagnostic["reason"], "browser_start_timeout")
        self.assertEqual([a["outcome"] for a in diagnostic["attempts"]], ["timeout"])
        self._assert_profiles_removed()

    def test_plan_is_bounded(self):
        crashes = [
            _Candidate(self._script(f"chromium-{i}", _EXIT_CODE), "chromium", "none", "discovered")
            for i in range(browser_use.MAX_STARTUP_ATTEMPTS + 3)
        ]
        with self._discovery(*crashes):
            diagnostic = browser_use.probe_browser_startup()
        self.assertEqual(diagnostic["attempt_count"], browser_use.MAX_STARTUP_ATTEMPTS)

    def test_run_receipt_carries_the_diagnostic_and_makes_no_provider_request(self):
        trap = self._script("chromium", _CRASH_TRAP)
        client = mock.Mock()
        client.decide.side_effect = AssertionError("no provider request after a startup crash")
        with (
            self._discovery(_Candidate(trap, "chromium", "none", "discovered")),
            mock.patch.object(destination_policy, "default_resolver", return_value=[PUBLIC]),
            mock.patch.object(browser_use, "request_budget_scope") as scope,
        ):
            scope.return_value.__enter__.return_value = None
            scope.return_value.__exit__.return_value = None
            result = run_browser_goal(
                goal="Read the article",
                start_url="https://example.org/",
                client=client,
                max_steps=2,
            )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failure_phase"], "browser_startup")
        self.assertEqual(result["failure_reason"], "browser_crashed")
        startup = result["browser_startup"]
        self.assertEqual(startup["attempts"][0]["exit_signal"], "SIGTRAP")
        self._assert_redacted(startup)
        client.decide.assert_not_called()
        self._assert_profiles_removed()

    def test_destination_refusal_still_happens_before_any_launch(self):
        with (
            mock.patch.object(browser_use, "_startup_plan", side_effect=AssertionError("no plan before policy")),
            mock.patch.object(browser_use.subprocess, "Popen", side_effect=AssertionError("no launch")),
            mock.patch.object(destination_policy, "default_resolver", return_value=["127.0.0.1"]),
        ):
            with self.assertRaises(browser_use.DestinationPolicyError):
                browser_use.ChromiumSession("https://rebind.example/")

    def test_session_crash_fallback_keeps_the_pinning_proxy_args(self):
        crash = self._script("chromium", _CRASH_TRAP)
        good, argv_log = self._devtools()
        with (
            self._discovery(
                _Candidate(crash, "chromium", "none", "discovered"),
                _Candidate(good, "chrome", "none", "discovered"),
            ),
            mock.patch.object(destination_policy, "default_resolver", return_value=[PUBLIC]),
            mock.patch.object(browser_use, "_ChromeWebSocket", side_effect=OSError("stop after launch")),
        ):
            with self.assertRaises(OSError):
                browser_use.ChromiumSession("https://example.org/")
        argv = json.loads(argv_log.read_text(encoding="utf-8"))
        self.assertIn("--headless=new", argv)
        self.assertEqual(argv[-1], "about:blank")
        self.assertIn("--proxy-bypass-list=<-loopback>", argv)
        self.assertIn("--disable-quic", argv)
        self._assert_profiles_removed()

    def test_port_allocation_failure_closes_pinning_proxy_and_profile(self):
        candidate = self._script("chromium", _EXIT_CODE)
        proxy = mock.Mock()
        proxy.start.return_value = 31234
        with (
            self._discovery(_Candidate(candidate, "chromium", "none", "discovered")),
            mock.patch.object(destination_policy, "default_resolver", return_value=[PUBLIC]),
            mock.patch.object(browser_use, "ValidatingProxy", return_value=proxy),
            mock.patch.object(browser_use, "_free_localhost_port", side_effect=OSError("port unavailable")),
        ):
            with self.assertRaisesRegex(OSError, "port unavailable"):
                browser_use.ChromiumSession("https://example.org/")
        proxy.stop.assert_called_once()
        self._assert_profiles_removed()

    def test_log_open_failure_closes_pinning_proxy_and_profile(self):
        candidate = self._script("chromium", _EXIT_CODE)
        proxy = mock.Mock()
        proxy.start.return_value = 31234
        with (
            self._discovery(_Candidate(candidate, "chromium", "none", "discovered")),
            mock.patch.object(destination_policy, "default_resolver", return_value=[PUBLIC]),
            mock.patch.object(browser_use, "ValidatingProxy", return_value=proxy),
            mock.patch.object(browser_use, "open", create=True, side_effect=OSError("log unavailable")),
        ):
            with self.assertRaisesRegex(OSError, "log unavailable"):
                browser_use.ChromiumSession("https://example.org/")
        proxy.stop.assert_called_once()
        self._assert_profiles_removed()

    def test_profile_creation_failure_closes_pinning_proxy(self):
        candidate = self._script("chromium", _EXIT_CODE)
        proxy = mock.Mock()
        proxy.start.return_value = 31234
        with (
            self._discovery(_Candidate(candidate, "chromium", "none", "discovered")),
            mock.patch.object(destination_policy, "default_resolver", return_value=[PUBLIC]),
            mock.patch.object(browser_use, "ValidatingProxy", return_value=proxy),
            mock.patch.object(browser_use, "_browser_profile_dir", side_effect=OSError("profile unavailable")),
        ):
            with self.assertRaisesRegex(OSError, "profile unavailable"):
                browser_use.ChromiumSession("https://example.org/")
        proxy.stop.assert_called_once()
        self._assert_profiles_removed()


class DiscoveryOrderTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "creating symlinks requires elevated Windows privileges")
    def test_executable_aliases_share_one_fallback_slot(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chrome = root / "google-chrome-stable"
            chrome.write_bytes(b"\x7fELF\x00binary")
            chrome.chmod(0o755)
            alias = root / "google-chrome"
            alias.symlink_to(chrome)
            edge = root / "msedge"
            edge.write_bytes(b"\x7fELF\x00binary")
            edge.chmod(0o755)
            located = {"google-chrome-stable": chrome, "google-chrome": alias, "msedge": edge}
            with (
                mock.patch.object(browser_use.shutil, "which", side_effect=lambda n: str(located[n]) if n in located else None),
                mock.patch.object(browser_use, "_playwright_candidates", return_value=[]),
                mock.patch.object(browser_use, "_browser_binary", return_value=chrome),
                mock.patch.object(browser_use, "_browser_binary_details", return_value=(chrome, "chrome", "none")),
            ):
                discovered = browser_use._discovered_candidates()
                plan = browser_use._startup_plan(str(alias))
        self.assertEqual([candidate.path for candidate in discovered], [chrome, edge])
        self.assertEqual([candidate.path for candidate in plan], [alias, edge])

    def test_order_is_chrome_chromium_edge_playwright_then_snap(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            files = {}
            for name in ("google-chrome", "chromium", "msedge"):
                path = base / name
                path.write_bytes(b"\x7fELF\x00binary")
                path.chmod(0o755)
                files[name] = path
            snap_wrapper = base / "chromium-browser"
            snap_wrapper.write_text('#!/bin/sh\nexec snap run chromium "$@"\n', encoding="utf-8")
            snap_wrapper.chmod(0o755)
            playwright = base / "pw" / "chromium-1200" / "chrome-linux64" / "chrome"
            playwright.parent.mkdir(parents=True)
            playwright.write_bytes(b"\x7fELF\x00binary")
            playwright.chmod(0o755)
            located = {
                "google-chrome": files["google-chrome"],
                "chromium": files["chromium"],
                "chromium-browser": snap_wrapper,
                "msedge": files["msedge"],
            }
            hidden = {
                "PROGRAMFILES": "",
                "PROGRAMFILES(X86)": "",
                "LOCALAPPDATA": "",
                "PLAYWRIGHT_BROWSERS_PATH": str(base / "pw"),
            }
            with (
                mock.patch.dict(os.environ, hidden),
                mock.patch.object(browser_use.shutil, "which", side_effect=lambda n: located.get(n) and str(located[n])),
            ):
                ordered = browser_use._discovered_candidates()
                path, family, confinement = browser_use._browser_binary_details()
        summary = [(c.family, c.executable_class) for c in ordered]
        self.assertEqual(
            summary,
            [("chrome", "system"), ("chromium", "system"), ("edge", "system"), ("chromium", "bundled"), ("chromium", "snap")],
        )
        self.assertEqual((path, family, confinement), (files["google-chrome"], "chrome", "none"))


class StatusBrowserCliTests(unittest.TestCase):
    _DIAGNOSTIC = {
        "outcome": "failed",
        "reason": "browser_crashed",
        "fallback_used": False,
        "attempt_count": 1,
        "attempts": [
            {
                "executable_class": "snap",
                "browser_family": "chromium",
                "source": "discovered",
                "outcome": "exited",
                "exit_code": None,
                "exit_signal": "SIGTRAP",
                "startup_ms": 812.4,
                "stderr_reasons": ["trap_signal"],
            }
        ],
        "elapsed_ms": 830.0,
    }

    def _run(self, **flags):
        args = SimpleNamespace(switchyard_command="status", **flags)
        out = io.StringIO()
        with (
            mock.patch.object(hermes_switchyard, "_secret", return_value=""),
            redirect_stdout(out),
        ):
            code = hermes_switchyard._cli_handler(args)
        return code, out.getvalue()

    def test_browser_flag_is_parsed(self):
        import argparse

        parser = argparse.ArgumentParser()
        hermes_switchyard._setup_cli(parser)
        parsed = parser.parse_args(["status", "--browser", "--json"])
        self.assertTrue(parsed.browser)
        self.assertFalse(parser.parse_args(["status"]).browser)

    def test_status_without_flag_never_launches(self):
        with mock.patch.object(browser_use, "probe_browser_startup", side_effect=AssertionError("no launch")):
            code, text = self._run(json_output=True)
        self.assertEqual(code, 0)
        self.assertNotIn("browser_startup", json.loads(text))

    def test_status_browser_json_reports_the_diagnostic(self):
        with mock.patch.object(browser_use, "probe_browser_startup", return_value=self._DIAGNOSTIC) as probe:
            code, text = self._run(json_output=True, browser=True)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["browser_startup"], self._DIAGNOSTIC)
        probe.assert_called_once()

    def test_status_browser_text_reports_the_diagnostic(self):
        with mock.patch.object(browser_use, "probe_browser_startup", return_value=self._DIAGNOSTIC):
            code, text = self._run(json_output=False, browser=True)
        self.assertEqual(code, 0)
        self.assertIn("Browser startup: failed (browser_crashed)", text)
        self.assertIn("chromium (snap, discovered): exited, signal SIGTRAP, 812.4 ms, stderr: trap_signal", text)

    def test_status_browser_passes_the_configured_executable(self):
        with (
            mock.patch.dict(hermes_switchyard._ROUTE_STATUS, {"browser_executable": lambda: "/opt/chrome/chrome"}),
            mock.patch.object(browser_use, "probe_browser_startup", return_value=self._DIAGNOSTIC) as probe,
        ):
            self._run(json_output=True, browser=True)
        probe.assert_called_once_with(browser_executable="/opt/chrome/chrome")

    def test_probe_fault_does_not_hide_status(self):
        private_path = str(Path(tempfile.gettempdir()) / "synthetic-private-value")
        with mock.patch.object(browser_use, "probe_browser_startup", side_effect=RuntimeError(private_path)):
            code, text = self._run(json_output=True, browser=True)
        self.assertEqual(code, 0)
        payload = json.loads(text)
        self.assertEqual(payload["browser_startup"]["reason"], "browser_probe_failed")
        self.assertNotIn(private_path, text)


if __name__ == "__main__":
    unittest.main()
