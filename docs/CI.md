# CI and release verification

This repository has three separate CI boundaries. Offline checks never call OpenRouter. Native compatibility uses the pinned Hermes checkout and the real loader. Live Jev checks are manual, source-allowlisted, and require a protected GitHub environment. A separate report-only workflow, `Upstream pin drift`, watches the pinned upstream commit and never gates a change.

## Offline compatibility

`.github/workflows/switchyard-compatibility.yml` runs the plugin's Python test and hygiene surface with the supported Hermes range `>=3.11,<3.14`; Python 3.14 is not a supported Hermes runtime for this gate. One `plan` job computes an event-dependent matrix; one `compatibility` job runs the exact same step sequence for every planned cell, so a future dependency, security, or test-command change updates one lane instead of two that could drift apart (`tests/test_ci_contracts.py` pins this).

- **Pull requests** run one fast combo: Ubuntu, Python 3.11. All 26 compatibility failures observed before this design broke identically across every matrix cell, so the other five cells bought redundant runs against a limited Actions minutes budget, not extra signal. This fast check is the required branch-protection status check on `main`.
- **Push to `main`, a weekly schedule (Monday 06:17 UTC), and manual `workflow_dispatch`** run the full six-entry matrix (Ubuntu and Windows, Python 3.11/3.12/3.13), to still catch real OS/version-specific drift without paying the 6x multiplier on every PR iteration.

Superseded runs for the same pull request or branch are cancelled. Maintainers can still use `workflow_dispatch` for an explicit full-matrix rerun on any branch.

Each matrix job:

1. Fetches Hermes Agent at `8503ee4459316ce092b5d69b7d396c27aa03d0be` into the runner's temporary directory outside the candidate workspace.
2. Creates a venv outside the Hermes checkout with `uv venv`.
3. Installs the pinned checkout with the upstream contributor setup, `uv pip install -e ".[all,dev]"`.
4. Runs `hermes plugins validate --json` and `hermes plugins doctor --ci` against this candidate.
5. Runs `scripts/ci/check_native_hermes.py`, which uses Hermes' real manifest parser, directory loader, registration path, registry entries, and tool schemas. This proves every declared tool is registered with a well-formed schema; it does not call a handler.
6. Runs `scripts/ci/check_native_tool_invocation.py`, which loads the same real registry entries and actually calls each Jev-backed tool's handler (`jev_skill_select`, `jev_skill_select_many`, `jev_model_route`, `jev_assess`) with a synthetic HTTPS transport standing in for the Jev provider. No network call, no credential, no cost; it exercises the exact code path a live turn would use (schema → handler closure → `DecisionClient` → `http.client.HTTPSConnection` → response validation → JSON envelope) so a PR that breaks a handler at runtime (bad import, signature mismatch, argument-handling regression) fails this required PR gate instead of only the manual, paid `Live Jev contract` workflow. A synthetic transport does not prove a live Jev response would satisfy the plugin; that remains the live contract's job.
7. Runs the plugin's offline tests with that native Hermes Python, so the native parser test cannot become a green import skip.

The native receipt is uploaded as a workflow artifact. It contains the candidate source SHA, pinned Hermes SHA, manifest identity, registered tool names, schema fields, and interpreter version. It never contains credentials or model requests.

Local equivalent from the Switchyard checkout with the pinned Hermes source at an explicit path:

```text
SWITCHYARD_ROOT="$(pwd)"
HERMES_SOURCE_ROOT="/path/to/hermes-agent"
HERMES_VENV="$(mktemp -d)/hermes-switchyard-ci-venv"
uv venv "$HERMES_VENV" --python 3.11
uv pip install --python "$HERMES_VENV/bin/python" -e "$HERMES_SOURCE_ROOT[all,dev]"
HERMES_HOME="$(mktemp -d)" "$HERMES_VENV/bin/hermes" plugins validate --json "$SWITCHYARD_ROOT"
HERMES_HOME="$(mktemp -d)" "$HERMES_VENV/bin/hermes" plugins doctor --ci "$SWITCHYARD_ROOT"
REPORT="$(mktemp)"
HERMES_HOME="$(mktemp -d)" "$HERMES_VENV/bin/python" "$SWITCHYARD_ROOT/scripts/ci/check_native_hermes.py" \
  --plugin-root "$SWITCHYARD_ROOT" \
  --upstream-root "$HERMES_SOURCE_ROOT" \
  --upstream-sha 8503ee4459316ce092b5d69b7d396c27aa03d0be \
  --source-sha "$(git -C "$SWITCHYARD_ROOT" rev-parse --verify HEAD)" \
  --report "$REPORT"
```

The upstream setup deliberately keeps the venv outside the source checkout. This prevents a relative cleanup command from deleting the active interpreter and matches the pinned Hermes contributor guidance.

## Live Jev contract

`.github/workflows/live-jev.yml` is `workflow_dispatch` only and must be dispatched from the finalized `main` workflow source. It does not use `pull_request_target`, does not run for forks, and does not read a repository secret. The live job declares the protected environment `jev-live`; `OPENROUTER_API_KEY` must be defined only in that environment, with the environment's required reviewers and branch restrictions configured by the repository owner. If the secret is absent, the job fails with an error. It never reports a skipped live test as success.

The manual inputs are both required:

- `trusted_ref`: exactly `main` or `bgrablin/release-packaging`.
- `trusted_sha`: the exact 40-character lowercase commit checked out from that ref.

The source gate checks the canonical repository, the explicit ref allowlist, and the exact checked-out SHA before a live request. Update the allowlist only through a reviewed workflow change; do not turn it into a free-form branch selector.

The live job registers the candidate through the real Hermes plugin loader and calls the real plugin tools with public fixed cases:

- one selected skill,
- one selected model route,
- one explicit skill abstention.

The script requires the client-validated response schema and an allowed resolved Jev model. Every case emits a latency and numeric provider-usage receipt bound to the exact candidate source SHA. The plugin's fixed Decisions endpoint and `allow_fallbacks: false` policy remain in force; the workflow does not add a provider or paid fallback. The receipt is uploaded only as the artifact of that manually authorized run and contains no API key.

Local native scoped hydration uses Hermes' profile secret scope. It does not write a key, change a profile, or print a key:

```text
SWITCHYARD_ROOT="$(pwd)"
HERMES_SOURCE_ROOT="/path/to/hermes-agent"
HERMES_HOME="$(mktemp -d)" \
  /path/to/hermes-python "$SWITCHYARD_ROOT/scripts/ci/live_jev_contract.py" \
  --plugin-root "$SWITCHYARD_ROOT" \
  --upstream-root "$HERMES_SOURCE_ROOT" \
  --source-sha "$(git -C "$SWITCHYARD_ROOT" rev-parse --verify HEAD)" \
  --upstream-sha 8503ee4459316ce092b5d69b7d396c27aa03d0be \
  --secret-home "$HOME/.hermes" \
  --report "$(mktemp)"
```

Use an authorized local Hermes profile only. Do not place a key in a command argument, fixture, repository file, artifact, or report.

## Release candidate

`.github/workflows/release-candidate.yml` remains `workflow_dispatch` only. It checks the dispatched SHA, runs the offline checks, builds the source-bound archive, and invokes `scripts/ci/verify_release_candidate.py` under the pinned Hermes environment. That verifier checks the exact Git blobs, extracts the archive, confirms the README setup and branding links resolve to packaged files, and runs the actual Hermes loader and tool-schema check on the extracted plugin.

The workflow uploads the candidate ZIP and verification receipt as short-lived artifacts. It never creates tags, publishes releases, changes repository settings, or enables a plugin in a user profile. A successful archive build without source verification or native loader evidence is not a release claim.

## Hermes upstream pin

One full commit SHA pins the Hermes upstream source for every workflow that uses it: `switchyard-compatibility.yml`, `live-jev.yml`, `release-candidate.yml`, and `upstream-pin-drift.yml`. The value is the `HERMES_UPSTREAM_SHA` environment entry in each file, and the documented references in `docs/CI.md`, `docs/TEST-MATRIX.md`, and `THIRD_PARTY.md` must name the same commit. `tests/test_ci_contracts.py` fails when any copy diverges.

To re-pin:

1. Select the reviewed upstream commit.
2. Update `HERMES_UPSTREAM_SHA` in every pinned workflow in one change.
3. Update the SHA text in `docs/CI.md`, `docs/TEST-MATRIX.md`, and `THIRD_PARTY.md`.
4. Run `python -m unittest tests.test_ci_contracts -v`.

`upstream-pin-drift.yml` runs monthly and on manual dispatch. It compares the pin with the current `NousResearch/hermes-agent` default-branch head through `git ls-remote`, records the status and a compare link in the run summary, and never writes to the repository. Read a drift report as a prompt to re-pin deliberately; it is not a release gate.

## Action and credential boundary

Workflow actions are pinned to full commit SHAs and jobs use `contents: read`. No workflow writes GitHub secrets, environments, tags, releases, or comments. Keep local evidence outside the repository; only the declared CI receipts and candidate archive are uploaded by their respective workflows.
