"""Invariant tests for the CI source gate, the upstream pin, and live usage receipts."""
from __future__ import annotations

import io
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.ci import check_native_hermes
from scripts.ci.live_jev_contract import LiveContractError, _usage_receipt
from scripts.ci.validate_live_source import (
    TrustedSourceError,
    main,
    validate_trusted_source,
)


class CiContractTests(unittest.TestCase):
    @staticmethod
    def _workflow_text() -> str:
        return (
            Path(__file__).resolve().parent.parent
            / ".github"
            / "workflows"
            / "switchyard-compatibility.yml"
        ).read_text(encoding="utf-8")

    def test_compatibility_workflow_avoids_duplicate_feature_push_runs(self):
        workflow = self._workflow_text()

        push_stanza = re.search(
            r"(?m)^  push:\n(?: {4,}.*\n)*",
            workflow,
        )
        if push_stanza is None:
            self.fail("compatibility workflow is missing the push trigger")
        self.assertEqual(
            push_stanza.group(0),
            "  push:\n    branches:\n      - main\n",
        )
        self.assertIn("  pull_request:\n", workflow)
        self.assertIn("  workflow_dispatch:\n", workflow)
        schedule_stanza = re.search(r"(?m)^  schedule:\n(?: {4,}.*\n)*", workflow)
        if schedule_stanza is None:
            self.fail("compatibility workflow is missing the weekly schedule trigger")
        self.assertRegex(
            schedule_stanza.group(0),
            r"- cron: '\d{1,2} \d{1,2} \* \* [0-6]'",
        )
        self.assertIn("  cancel-in-progress: true\n", workflow)

    def test_pull_request_event_plans_exactly_one_matrix_cell(self):
        """Pin event-dependent matrix behavior without duplicating the job steps.

        Pull requests use one fast combo, pushes to main use the full
        six-cell matrix, and weekly/manual runs add one non-required
        pre-qualification cell. Reads the exact JSON the `plan` job emits for
        each branch of its event_name guard.
        """
        workflow = self._workflow_text()
        guard = re.search(
            r'if \[ "\$\{\{ github\.event_name \}\}" = "pull_request" \]; then\n'
            r"(?P<pr_branch>(?:.*\n)*?)"
            r"          elif .*schedule.*workflow_dispatch.*\n"
            r"(?P<prequal_branch>(?:.*\n)*?)"
            r"          else\n"
            r"(?P<default_branch>(?:.*\n)*?)"
            r"          fi\n",
            workflow,
        )
        if guard is None:
            self.fail(
                "compatibility workflow's plan job is missing the "
                "event-dependent matrix guard; the fast PR gate or the "
                "pre-qualification lane may have regressed"
            )

        def matrix_cells(branch: str) -> list[tuple[str, str]]:
            matrix = "".join(
                re.findall(
                    r'\{"os":"[^"]+","python-version":"[^"]+"\}',
                    branch,
                )
            )
            return re.findall(
                r'\{"os":"([^"]+)","python-version":"([^"]+)"\}',
                matrix,
            )

        pr_cells = matrix_cells(guard.group("pr_branch"))
        prequal_cells = matrix_cells(guard.group("prequal_branch"))
        default_cells = matrix_cells(guard.group("default_branch"))
        required_cells = [
            ("ubuntu-latest", "3.11"),
            ("ubuntu-latest", "3.12"),
            ("ubuntu-latest", "3.13"),
            ("windows-latest", "3.11"),
            ("windows-latest", "3.12"),
            ("windows-latest", "3.13"),
        ]

        self.assertEqual(
            pr_cells,
            [("ubuntu-latest", "3.11")],
            "pull_request events must plan exactly one fast combo",
        )
        self.assertEqual(
            sorted(default_cells),
            sorted(required_cells),
            "push events must plan the full 2 OS x 3 Python matrix",
        )
        self.assertEqual(
            sorted(prequal_cells),
            sorted(required_cells + [("ubuntu-latest", "3.14")]),
            "schedule and workflow_dispatch events must add only the "
            "Ubuntu/Python 3.14 pre-qualification cell",
        )
        self.assertNotIn(
            '"python-version":"3.14"',
            guard.group("pr_branch"),
            "Python 3.14 must stay out of the required pull-request matrix",
        )
        self.assertIn(
            "continue-on-error: ${{ matrix.python-version == '3.14' }}",
            workflow,
            "the Python 3.14 pre-qualification cell must be non-required",
        )

    def test_ruff_pin_matches_the_lint_configuration(self):
        """The lint runner version lives in two files; they must not drift.

        `ruff.toml` pins the version a developer's local runner must match,
        and the compatibility workflow pins the version CI installs. A
        mismatch means CI and local runs check different rule sets.
        """
        workflow = self._workflow_text()
        config = (Path(__file__).resolve().parent.parent / "ruff.toml").read_text(encoding="utf-8")
        required = re.search(r'required-version = "==([0-9][0-9.]*)"', config)
        runner = re.search(r"uvx --from ruff==([0-9][0-9.]*) ruff check", workflow)
        if required is None or runner is None:
            self.fail(
                "ruff pin missing: ruff.toml or the compatibility workflow "
                "does not pin a ruff version"
            )
        self.assertEqual(
            runner.group(1),
            required.group(1),
            "ruff pin drift between ruff.toml and the compatibility workflow",
        )

    SETUP_UV_USE = re.compile(
        r"""(?m)^[ \t]*(?:-[ \t]+)?(?:uses|'uses'|"uses"):[ \t]*
        (?P<quote>['"]?)(?P<ref>astral-sh/setup-uv@[^ \t'"#\r\n]+)
        (?P=quote)[ \t]*(?:\#[ \t]*(?P<note>[^\r\n]*))?[ \t]*$""",
        re.VERBOSE,
    )

    def _assert_setup_uv_pins(self, workflows: Path) -> None:
        expected_pin = (
            "astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7 # v10.2.0"
        )
        expected = {
            "live-jev.yml": [expected_pin],
            "release-candidate.yml": [expected_pin, expected_pin],
            "switchyard-compatibility.yml": [expected_pin, expected_pin],
        }
        actual = {}
        for path in sorted(workflows.iterdir()):
            if path.suffix not in {".yml", ".yaml"}:
                continue
            uses = []
            for match in self.SETUP_UV_USE.finditer(path.read_text(encoding="utf-8")):
                use = match.group("ref")
                if match.group("note") is not None:
                    use += f" # {match.group('note').strip()}"
                uses.append(use)
            if uses:
                actual[path.name] = uses
        self.assertEqual(actual, expected)

    def test_setup_uv_is_pinned_consistently_in_all_workflows(self):
        """Keep each setup-uv use on the reviewed SHA and matching version label."""
        workflows = Path(__file__).resolve().parent.parent / ".github" / "workflows"
        self._assert_setup_uv_pins(workflows)

    @staticmethod
    def _unpinned_workflow_dependencies(workflows: Path) -> list[str]:
        """Inspect YAML uses fields, including reusable workflows, without network access."""
        from ruamel.yaml import YAML

        failures = []
        for path in sorted(workflows.iterdir()):
            if path.suffix not in {".yml", ".yaml"}:
                continue
            workflow = YAML(typ="safe", pure=True).load(path.read_text(encoding="utf-8"))
            for job_name, job in workflow["jobs"].items():
                locations = [(f"jobs.{job_name}", job)]
                locations.extend(
                    (f"jobs.{job_name}.steps[{index}]", step)
                    for index, step in enumerate(job.get("steps", []))
                )
                for location, entry in locations:
                    if "uses" not in entry:
                        continue
                    ref = entry["uses"]
                    if isinstance(ref, str) and (
                        ref.startswith("./")
                        or re.fullmatch(r"docker://[^\s@]+@sha256:[0-9a-f]{64}", ref)
                        or re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref)
                    ):
                        continue
                    failures.append(f"{path.name}: {location}.uses must be immutable: {ref!r}")
        return failures

    def test_all_workflow_dependencies_use_immutable_references(self):
        """Reject mutable dependency references in every current workflow."""
        workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        self.assertEqual(self._unpinned_workflow_dependencies(workflows), [])

    def test_dependency_guard_rejects_mutable_actions_and_reusable_workflows(self):
        """A future tag/branch/short-SHA regression must fail the existing native CI gate."""
        refs = (
            "actions/checkout@v7",
            "actions/checkout@main",
            "actions/checkout@3d3c42e",
            "owner/repo/subdir/action@v1.2.3",
            "owner/repo/.github/workflows/build.yml@main",
            "docker://alpine:latest",
            "docker://alpine@sha256:1234",
        )
        with tempfile.TemporaryDirectory() as temp:
            workflows = Path(temp)
            for suffix in ("yml", "yaml"):
                path = workflows / f"candidate.{suffix}"
                for ref in refs:
                    for placement in ("step", "reusable"):
                        with self.subTest(ref=ref, placement=placement, suffix=suffix):
                            body = (
                                f"    steps:\n      - 'uses': '{ref}'\n"
                                if placement == "step"
                                else f"    'uses': '{ref}'\n"
                            )
                            path.write_text("jobs:\n  candidate:\n" + body, encoding="utf-8")
                            failures = self._unpinned_workflow_dependencies(workflows)
                            self.assertEqual(len(failures), 1)
                            self.assertIn(ref, failures[0])
                path.unlink()

    def test_dependency_guard_accepts_pins_local_uses_and_ignores_run_text(self):
        """Accept supported immutable references without treating shell text as YAML uses."""
        pin = "3d3c42e5aac5ba805825da76410c181273ba90b1"
        digest = "a" * 64
        with tempfile.TemporaryDirectory() as temp:
            workflows = Path(temp)
            (workflows / "accepted.yaml").write_text(
                "jobs:\n"
                f"  reusable: {{uses: 'owner/repo/.github/workflows/build.yml@{pin}'}}\n"
                "  local: {uses: './.github/workflows/local.yml'}\n"
                "  candidate:\n"
                "    steps:\n"
                f"      - &checkout {{uses: 'actions/checkout@{pin}'}}\n"
                "      - *checkout\n"
                f"      - uses: owner/repo/subdir/action@{pin}\n"
                "      - uses: ./local-action\n"
                f"      - uses: docker://alpine@sha256:{digest}\n"
                "      # - uses: actions/checkout@main\n"
                "      - run: |\n"
                "          echo 'uses: actions/checkout@main'\n",
                encoding="utf-8",
            )
            self.assertEqual(self._unpinned_workflow_dependencies(workflows), [])

    def test_upstream_head_report_runs_only_weekly_and_manually(self):
        """Keep the optional upstream report out of PR and main-push usage."""
        from ruamel.yaml import YAML

        path = Path(__file__).resolve().parents[1] / ".github/workflows/switchyard-compatibility.yml"
        workflow = YAML(typ="safe", pure=True).load(path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(workflow["on"]),
            {"push", "pull_request", "schedule", "workflow_dispatch"},
        )
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(set(workflow["jobs"]), {"plan", "compatibility", "upstream-head"})
        job = workflow["jobs"]["upstream-head"]
        self.assertEqual(
            job["if"],
            "${{ github.event_name == 'schedule' || github.event_name == 'workflow_dispatch' }}",
        )
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(job["strategy"]["matrix"]["python-version"], ["3.14"])
        self.assertIs(job["strategy"]["fail-fast"], False)
        self.assertIs(job["continue-on-error"], True)
        self.assertNotIn("needs", job)
        uses = [step["uses"] for step in job["steps"] if "uses" in step]
        self.assertTrue(uses)
        self.assertTrue(all(re.fullmatch(r"[\w-]+/[\w-]+@[0-9a-f]{40}", use) for use in uses))
        self.assertTrue(any(use.startswith("actions/upload-artifact@") for use in uses))
        self.assertEqual(
            [step["if"] for step in job["steps"] if "if" in step],
            ["always()", "always()"],
        )
        gate = next(step for step in job["steps"] if step["name"].startswith("Run isolated"))
        for check in (
            "check_native_hermes.py",
            "check_native_tool_invocation.py",
            "-m unittest discover -s tests -v",
            '"$receipts/steps.tsv"',
        ):
            self.assertIn(check, gate["run"])
        self.assertIn("$GITHUB_STEP_SUMMARY", job["steps"][-2]["run"])
        self.assertNotIn("secrets.", path.read_text(encoding="utf-8"))

    def test_setup_uv_guard_covers_quoted_new_jobs_and_yaml_files(self):
        source = Path(__file__).resolve().parent.parent / ".github" / "workflows"
        current = "astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7"
        old = "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9"
        with tempfile.TemporaryDirectory() as temp:
            workflows = Path(temp)
            for path in source.iterdir():
                if path.suffix in {".yml", ".yaml"}:
                    (workflows / path.name).write_text(
                        path.read_text(encoding="utf-8"), encoding="utf-8"
                    )

            extra = workflows / "extra.yaml"
            extra.write_text(
                "name: comments-only\non: push\njobs:\n  benign:\n"
                "    runs-on: ubuntu-latest\n    steps:\n"
                f"      # - uses: '{old}' # v9.0.0\n"
                f"      - run: echo ok # uses: '{old}' # v9.0.0\n",
                encoding="utf-8",
            )
            self._assert_setup_uv_pins(workflows)
            extra.unlink()

            live = workflows / "live-jev.yml"
            original = live.read_text(encoding="utf-8")
            self.assertIn(f"uses: {current} # v10.2.0", original)
            for quote in ("'", '"'):
                live.write_text(
                    original.replace(
                        f"uses: {current} # v10.2.0",
                        f"uses: {quote}{current}{quote} # v10.2.0",
                        1,
                    ),
                    encoding="utf-8",
                )
                self._assert_setup_uv_pins(workflows)
            live.write_text(original, encoding="utf-8")

            for suffix, quote in (("yml", "'"), ("yaml", '"')):
                extra = workflows / f"extra.{suffix}"
                extra.write_text(
                    "name: new-old-pin\non: push\njobs:\n  new-job:\n"
                    "    runs-on: ubuntu-latest\n    steps:\n"
                    f"      - uses: {quote}{old}{quote} # v9.0.0\n",
                    encoding="utf-8",
                )
                with self.assertRaises(AssertionError):
                    self._assert_setup_uv_pins(workflows)
                extra.unlink()

    def test_compatibility_step_sequence_is_not_duplicated_across_lanes(self):
        """Guard against a second copy of the compatibility steps (the

        duplicate-lane drift risk Copilot flagged on PR #65): the fast PR
        gate and the full matrix must be one job whose size varies by event,
        not two jobs that can be edited independently and drift apart.
        """
        workflow = self._workflow_text()
        self.assertEqual(
            workflow.count("- name: Run offline plugin checks"),
            1,
            "the offline-checks step must appear exactly once; a second "
            "occurrence means the compatibility steps were duplicated into "
            "a separate job instead of driven by one event-dependent matrix",
        )
        jobs_section = workflow.split("\njobs:\n", 1)[1]
        job_names = re.findall(r"(?m)^  ([a-zA-Z0-9_-]+):\n", jobs_section)
        self.assertEqual(
            job_names,
            ["plan", "compatibility", "upstream-head"],
            "the pinned compatibility sequence must remain one job; the "
            "upstream report is a separate non-gating job",
        )

    def test_release_candidate_windows_job_consumes_the_ubuntu_artifact(self):
        """The Windows release job installs the Ubuntu-built archive."""
        workflow = (
            Path(__file__).resolve().parent.parent / ".github/workflows/release-candidate.yml"
        ).read_text(encoding="utf-8")
        ubuntu, windows = workflow.split("\n  windows-installed-archive:\n", 1)
        for job in (ubuntu, windows):
            self.assertIn('-e "${hermes_root}[all,dev,anthropic]"', job)
        self.assertIn("--source-sha \"$SWITCHYARD_SOURCE_SHA\"", workflow)
        self.assertIn("--version 0.6.0", workflow)
        self.assertIn("archive-sha256.json", workflow)
        self.assertIn("\n  windows-installed-archive:\n", workflow)
        windows = workflow.split("\n  windows-installed-archive:\n", 1)[1]
        self.assertIn("needs: build-verify-upload", windows)
        self.assertIn("runs-on: windows-latest", windows)
        self.assertIn("timeout-minutes: 30", windows)
        self.assertIn("python-version: '3.11'", windows)
        self.assertIn(
            "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c", windows
        )
        self.assertIn("hermes-switchyard-candidate-${{ github.sha }}", windows)
        self.assertIn('test "$(git rev-parse --verify HEAD)" = "$GITHUB_SHA"', windows)
        self.assertIn('test "$(git -C "$hermes_root" rev-parse --verify HEAD)" = "$HERMES_UPSTREAM_SHA"', windows)
        self.assertIn("check_windows_installed_archive.py", windows)
        self.assertIn("--artifact-dir", windows)
        self.assertIn("--source-sha \"$GITHUB_SHA\"", windows)
        self.assertIn("--report", windows)
        self.assertIn("if: always()", windows)
        self.assertIn("if-no-files-found: error", windows)
        self.assertNotIn("continue-on-error", windows)

    PINNED_WORKFLOWS = (
        "switchyard-compatibility.yml",
        "live-jev.yml",
        "release-candidate.yml",
        "upstream-pin-drift.yml",
    )
    PINNED_DOCS = ("docs/CI.md", "docs/TEST-MATRIX.md", "THIRD_PARTY.md")
    HERMES_UPSTREAM_PIN = re.compile(r"(?m)^\s*HERMES_UPSTREAM_SHA:\s*([0-9a-f]{40})\s*$")

    def _workflow_pins(self) -> dict[str, str]:
        root = Path(__file__).resolve().parent.parent
        pins: dict[str, str] = {}
        for name in self.PINNED_WORKFLOWS:
            text = (root / ".github" / "workflows" / name).read_text(encoding="utf-8")
            match = self.HERMES_UPSTREAM_PIN.search(text)
            if match is None:
                self.fail(f"{name} is missing the HERMES_UPSTREAM_SHA environment pin")
            pins[name] = match.group(1)
        return pins

    def test_pinned_workflows_agree_on_one_hermes_upstream_sha(self):
        """The pin is duplicated by design, so equality is the only guard.

        Workflows stay self-contained, which means the SHA is copied into
        each file; this test is what catches a silent divergence.
        """
        pins = self._workflow_pins()
        unique = sorted(set(pins.values()))
        self.assertEqual(len(unique), 1, f"pinned workflows disagree: {pins}")

    def test_native_checker_pin_and_range_match_the_pinned_contract(self):
        pin = self._workflow_pins()[self.PINNED_WORKFLOWS[0]]
        self.assertEqual(
            check_native_hermes.PINNED_HERMES_SHA,
            pin,
            "the native checker must recognize the workflow pin to enforce requires-python",
        )
        ci_docs = (Path(__file__).resolve().parents[1] / "docs/CI.md").read_text(
            encoding="utf-8"
        )
        documented_range = re.search(r"pinned Hermes range `([^`]+)`", ci_docs)
        if documented_range is None:
            self.fail("docs/CI.md must state the pinned Hermes Python range")
        self.assertEqual(
            check_native_hermes.PINNED_HERMES_PYTHON,
            documented_range.group(1),
            "the native checker's requires-python range must match the documented pin",
        )

    def test_documented_hermes_upstream_references_match_the_pin(self):
        """Documented references must name the pinned commit and nothing else."""
        pins = self._workflow_pins()
        pin = pins[self.PINNED_WORKFLOWS[0]]
        root = Path(__file__).resolve().parent.parent
        for relative in self.PINNED_DOCS:
            text = (root / relative).read_text(encoding="utf-8")
            found = sorted(set(re.findall(r"\b[0-9a-f]{40}\b", text)))
            self.assertEqual(
                found,
                [pin],
                f"{relative} must reference only the pinned commit; found {found}",
            )

    def test_live_source_requires_canonical_repo_allowlisted_ref_and_exact_head(self):
        source_sha = "a" * 40
        result = validate_trusted_source(
            repository="bgrablin/hermes-switchyard",
            ref="bgrablin/release-packaging",
            requested_sha=source_sha,
            checked_out_sha=source_sha,
        )
        self.assertEqual(result["source_sha"], source_sha)

        with self.assertRaises(TrustedSourceError):
            validate_trusted_source(
                repository="someone/fork",
                ref="main",
                requested_sha=source_sha,
                checked_out_sha=source_sha,
            )
        with self.assertRaises(TrustedSourceError):
            validate_trusted_source(
                repository="bgrablin/hermes-switchyard",
                ref="feature/unreviewed",
                requested_sha=source_sha,
                checked_out_sha=source_sha,
            )
        with self.assertRaises(TrustedSourceError):
            validate_trusted_source(
                repository="bgrablin/hermes-switchyard",
                ref="main",
                requested_sha=source_sha,
                checked_out_sha="b" * 40,
            )

    def test_live_source_cli_does_not_echo_source_sha(self):
        source_sha = "a" * 40
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "--repository",
                        "bgrablin/hermes-switchyard",
                        "--ref",
                        "bgrablin/release-packaging",
                        "--source-sha",
                        source_sha,
                        "--selector-only",
                    ]
                ),
                0,
            )
        self.assertEqual(output.getvalue(), "trusted live-contract source validated\n")
        self.assertNotIn(source_sha, output.getvalue())

    def test_live_receipt_requires_numeric_provider_usage(self):
        receipt = _usage_receipt({"cost": 0.01, "total_tokens": 12, "ignored": "not emitted"})
        self.assertEqual(receipt, {"cost": 0.01, "total_tokens": 12})
        with self.assertRaises(LiveContractError):
            _usage_receipt({})
        with self.assertRaises(LiveContractError):
            _usage_receipt({"cost": -1})


if __name__ == "__main__":
    unittest.main()
