# Feature and test matrix

This matrix separates synthetic checks from host integrations and comparative evaluation.

| Area | Coverage | Evidence in this repository | Release statement |
| --- | --- | --- | --- |
| Native manifest | Root `plugin.yaml`, root `register(ctx)`, declared tools, config schema | `hermes plugins validate` in the pinned Hermes lane and the local portability test when the Hermes parser is installed | Admission shape is checked; it does not certify runtime task quality. |
| Offline plugin behavior | Routing, policy gates, response validation, target freshness, error redaction | `python -m unittest discover -s tests -v` | Tests use synthetic transports and dispatch fixtures. They do not call a live service. |
| Synthetic evaluation | Fixture schema, policy behavior, abstention, source hashes | `python evaluation/evaluate.py --validate` | The evaluation is a deterministic policy and plumbing check, not a quality-calibration result. |
| Portability and public hygiene | Tracked-file paths, generic credential patterns, private host suffixes, stale branding, version consistency, fixture boundaries | `python scripts/check_portability.py` and `--history` | The scan is pattern-based and deliberately does not claim complete secret detection. |
| Release archive | Exact Git-blob payload allowlist, deterministic bytes, strict source SHA, embedded checksums, fresh extraction, optional external source reference | Resolve the reviewed commit with `git rev-parse --verify HEAD`, then pass that value to `python scripts/build_release.py --source-sha COMMIT_SHA`; `tests/test_release.py` checks the contract. | The ZIP excludes tests, evaluation data, CI files, caches, logs, and local evidence. `--verify` with `--source-root` and `--expected-source-sha` distinguishes source verification from internal integrity checks. |
| Linux and Windows Python matrix | Python 3.11, 3.12, and 3.13 on Linux and Windows | `.github/workflows/switchyard-compatibility.yml` | Six offline jobs are defined; a fresh CI run is required for a release claim. |
| Native Hermes integration | Exact upstream Hermes commit `8503ee4459316ce092b5d69b7d396c27aa03d0be`, isolated `HERMES_HOME`, real `hermes plugins validate`, and parser call | Manual release gate in [docs/RELEASE.md](RELEASE.md) | The gate has no plugin credentials and no model call. A dependency-install failure blocks publication. |
| Real GUI behavior | Windows desktop execution and coordinator-owned final verification | Not covered by offline CI | Real GUI tests are pending. The plugin returns `verified: false` for completion candidates. |
| Comparative benchmark | Independent comparison against other routing or GUI approaches | No benchmark result is shipped | Comparative benchmark results are pending. |
