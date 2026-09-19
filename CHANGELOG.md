# Changelog

## 0.4.2

- Adds an opt-in typed automatic skill consumer that invokes Hermes' normal `skill_view` loader once per accepted turn while retaining advisory mode as the default.

- Uses standalone `hosted_sanitized` automatic routing with persistent acknowledgement and strict local per-turn scanning.
- Resolves provider, model, endpoint, and profile-scoped secret settings at each invocation.
- Adds local `status` and `guide` commands plus an explicit `test --live` billed-request gate.
- Adds the separate typed `jev_skill_select_many` catalog-selection contract without loading or mutating skills.
- Renames the package, command, bundled skill, auxiliary task, and prompt-section surfaces from `jev-decision` to `hermes-switchyard`; Jev tool names remain stable.
- Adds migration guidance: remove the legacy `jev-decision` installation before installing `hermes-switchyard` so duplicate registrations cannot load together.

## 0.4.1

- Corrected two security-scanner false positives in operator documentation so the repository passes Hermes' plugin security gate without `--force`.

## 0.4.0

- Adds `jev_assess` with validated Choice, Score, and Noul support.
- Adds direct TypeSafe routing with automatic provider selection and pooled HTTPS connections; OpenRouter remains supported with fallbacks disabled.
- Searches skill catalogs larger than Jev's per-Choice limit through partition fan-out and recursive reduction instead of truncating the tail.
- Prepares explicit automatic routing modes (`off`, `local_only`, and `hosted_sanitized`) and a fail-closed per-turn egress contract. The plugin keeps candidate descriptions and history local, but current Hermes core does not yet propagate the envelope or consume routing metadata; production hosted-routing integration remains a separate core change.
- Expands Cua Driver-backed computer use to Windows, macOS, and Linux, broader native roles/actions, dense target partitioning, and 100-step bounded runs while preserving fresh identity checks and coordinator-owned verification.
- Stages CUA operation and target decisions, compares dense-partition finalists, uses macOS Command shortcuts, batches oversized assessment/model-routing requests, validates typed questions before transport, and aggregates multi-request receipts.
- Enforces aggregate provider-request budgets across nested fan-out and CUA runs, with identical preflight and wire serialization.

## 0.3.2

- Ships as a root-layout native Hermes plugin with `plugin.yaml` and a root `register(ctx)` entrypoint.
- Provides `jev_skill_select`, `jev_model_route`, and the Windows-only `jev_computer_use` pilot.
- Requires `public_or_sanitized_data_ack: true` before model-facing operations; this remains a caller attestation, not DLP or authorization.
- Keeps endpoint, model aliases, provider fallback behavior, action budgets, hotkey allowlists, and target re-capture checks closed in code.
- Returns advisory selections and `completion_candidate` results without loading skills, changing the runtime model, or certifying GUI completion.
- Verifies behavior with offline synthetic transports, dispatch fixtures, portability checks, and a source-bounded release archive.
