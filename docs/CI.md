# CI and release verification

This repository has three separate CI boundaries. Offline checks never call OpenRouter. Native compatibility uses the pinned Hermes checkout and the real loader. Live Jev checks are manual, source-allowlisted, and require a protected GitHub environment.

## Offline compatibility

`.github/workflows/switchyard-compatibility.yml` runs the plugin's Python test and hygiene surface on Ubuntu and Windows with Python 3.11, 3.12, and 3.13. The matrix uses the supported Hermes range `>=3.11,<3.14`; Python 3.14 is not a supported Hermes runtime for this gate.

The six-entry matrix runs once for each pull-request revision and once after a change reaches `main`. Feature-branch pushes do not also start a duplicate matrix run. Superseded runs for the same pull request or branch are cancelled. Maintainers can still use `workflow_dispatch` for an explicit rerun.

Each matrix job:

1. Fetches Hermes Agent at `8503ee4459316ce092b5d69b7d396c27aa03d0be` into the runner's temporary directory outside the candidate workspace.
2. Creates a venv outside the Hermes checkout with `uv venv`.
3. Installs the pinned checkout with the upstream contributor setup, `uv pip install -e ".[all,dev]"`.
4. Runs `hermes plugins validate --json` and `hermes plugins doctor --ci` against this candidate.
5. Runs `scripts/ci/check_native_hermes.py`, which uses Hermes' real manifest parser, directory loader, registration path, registry entries, and tool schemas.
6. Runs the plugin's offline tests with that native Hermes Python, so the native parser test cannot become a green import skip.

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

## Action and credential boundary

Workflow actions are pinned to full commit SHAs and jobs use `contents: read`. No workflow writes GitHub secrets, environments, tags, releases, or comments. Keep local evidence outside the repository; only the declared CI receipts and candidate archive are uploaded by their respective workflows.
