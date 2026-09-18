# Release instructions

This repository publishes a release candidate artifact for human review. The workflow does not create a tag, publish a GitHub Release, or change repository settings.

## Prepare a candidate

1. Start from the exact source commit intended for review and keep the checkout clean.
2. Run the offline tests, synthetic evaluator, current-tree hygiene check, and history check.
3. Build the archive with the exact source commit:

```text
set -euo pipefail
SOURCE_ROOT="$(git rev-parse --show-toplevel)"
SOURCE_SHA="$(git rev-parse --verify HEAD)"
test -z "$(git status --porcelain --untracked-files=all)"
git cat-file -e "$SOURCE_SHA^{commit}"
python scripts/build_release.py --source-sha "$SOURCE_SHA" --output-dir dist
```

The builder reads every release file from an ordinary Git blob in the exact source commit. It does not use dirty worktree bytes, and it rejects a missing commit, missing allowlisted file, symlink, or submodule. It writes `SOURCE-MANIFEST.json` with the plugin ID, manifest version, exact source SHA, and SHA-256 hash for each payload file. `SHA256SUMS` is embedded in the ZIP; it is not a detached checksum file. The builder extracts the archive into a fresh directory and verifies the manifest, entrypoint, Python parsing, file sizes, hashes, and the external Git source reference.

To verify an existing archive against the actual checkout, use the same exact source identity:

```text
set -- dist/hermes-switchyard-*.zip
test "$#" -eq 1
ARCHIVE="$1"
python scripts/build_release.py --verify "$ARCHIVE" --source-root "$SOURCE_ROOT" --expected-source-sha "$SOURCE_SHA"
```

This prints `source-verified` only after each archive payload member matches the corresponding blob in the expected commit. Running `--verify` without `--source-root` and `--expected-source-sha` checks only internal archive integrity; it does not prove the archive came from a source checkout.

## Candidate workflow

Run the `Release candidate` workflow with `workflow_dispatch` on the exact reviewed commit. The workflow checks out that commit, confirms the checkout is clean, runs the repository checks, builds the archive, verifies it against that checkout's Git blobs, and uploads the ZIP as a short-lived workflow artifact. The embedded source manifest and `SHA256SUMS` identify and check the source SHA inside the ZIP.

A workflow artifact is not a public release. Inspect the ZIP contents and checksum, then retain the exact workflow run and source SHA as review evidence.

## Manual native Hermes gate

Before publication, run the native Hermes gate against the exact upstream commit named in [docs/TEST-MATRIX.md](TEST-MATRIX.md). The gate must use a fresh `HERMES_HOME`, no plugin credentials, the real `hermes plugins validate --json` command, and the actual Hermes manifest parser. Use the dependency setup documented by that exact upstream checkout and keep the source checkout pinned to the full SHA. A dependency-install failure is a release blocker, not a passing substitute.

The gate is separate from offline synthetic tests. It must not call OpenRouter, enable the plugin in a user's profile, or drive a real GUI.

## Human publication boundary

The repository owner decides whether a candidate is ready for publication. That decision includes an exact source SHA, a reviewed archive, completed required CI, a completed native Hermes gate, and a review of unresolved test boundaries. Creating an exact-SHA tag, publishing a GitHub Release, submitting a catalog entry, or changing repository settings is a separate human action. Nothing in this repository performs those writes automatically.

## Installation choices

The normal install command is short and works while the repository is private when GitHub authentication is already available to Git:

```text
hermes plugins install bgrablin/hermes-switchyard --enable
```

The same command remains valid after a future public release. Do not put a token in a URL, command, or issue report. The `--enable` form installs and enables in one step. To inspect first, use `--no-enable`, then run `hermes plugins list` and `hermes plugins enable jev-decision`.

For a reproducible install, first verify that the current checkout is the reviewed release commit, then use its exact SHA:

```text
RELEASE_SHA="$(git rev-parse --verify HEAD)"
git cat-file -e "$RELEASE_SHA^{commit}"
hermes plugins install bgrablin/hermes-switchyard --ref "$RELEASE_SHA" --enable
```

This uses a commit SHA, not a moving branch or tag. The plugin ID remains `jev-decision` for enable, disable, list, and configuration commands.

The catalog name form is for a future catalog admission. Until admission, use the repository form above.

## OpenRouter and Jev access

Live Jev decisions require an OpenRouter account, an OpenRouter API key stored through Hermes' secret flow, enough OpenRouter credit or available provider quota for the request, and access to the exact approved model alias requested by the plugin. The approved aliases are `typesafe/jev-1.13` and `typesafe/jev-1.13-20260917`; access to another model does not satisfy this requirement.

A Codex subscription pays for Codex usage. It does not pay Jev fees or OpenRouter requests. Direct TypeSafe account or API access is not currently supported; the plugin uses the fixed OpenRouter Decisions endpoint and must not be pointed at a TypeSafe endpoint or given a TypeSafe key. Do not put any API key in a command, URL, repository file, or issue report.

## Hermes guidance comparison

The layout follows the Hermes native plugin contract: `plugin.yaml`, root `__init__.py`, `register(ctx)`, explicit capability declarations, and host-owned context APIs. The plugin does not modify Hermes core or use a private host integration. The catalog trust model is separate: catalog admission requires a human-reviewed exact SHA and is not implied by a repository release.
