# Contributing

Hermes Switchyard is a standalone native Hermes plugin. Keep changes inside this repository and use the documented Hermes plugin context. Do not modify Hermes core to support this plugin.

## Scope and compatibility

Keep the manifest name `hermes-switchyard`, the root `__init__.py` entrypoint, and the `hermes_switchyard` package path stable. Version 0.4.0 used the legacy `jev-decision` plugin ID; users must remove that installation before installing `hermes-switchyard` so both copies cannot load together.

Preserve `bgrablin` attribution and any genuine third-party attribution. Do not add personal paths, private hostnames, lab details, credentials, raw UI captures, or operational handoffs to source, tests, docs, or issue reports.

## Local checks

Run these commands from the repository root:

```text
python -m unittest discover -s tests -v
python evaluation/evaluate.py --validate
python scripts/check_portability.py
python scripts/check_portability.py --history
python scripts/build_release.py --source-sha "$(git rev-parse --verify HEAD)" --output-dir dist
```

The evaluator uses public synthetic fixtures. It does not call the live Decisions service or drive a real GUI. The release builder reads its fixed allowlist from ordinary Git blobs at the requested commit, writes deterministic ZIP metadata, and verifies extraction, hashes, the manifest, the entrypoint, and the source reference before it returns.

Do not commit `dist/`, test results, caches, logs, or local evidence. Keep behavior tests under `tests/` and keep them focused on observable contracts rather than source-text snapshots.

## Pull requests

Describe the behavior changed, the checks run, and any test boundary that remains. State clearly when a result is synthetic, when a real GUI check is pending, or when comparative benchmark results are not available. Do not claim calibration, task completion, or broad security properties that the code does not establish.

All project-facing text and examples are English. Use concrete limits and configuration names. Do not include tokens in commands or URLs.
