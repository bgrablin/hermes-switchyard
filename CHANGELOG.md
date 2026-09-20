# Changelog

## Unreleased (PR #18 corrective pass)

- Automatic hosted Jev is on after install. `automatic_skill_public_or_sanitized_data_ack` defaults true. Set it false to skip hosted automatic routing. Topic words such as "private" or "verification" no longer skip Jev; high-confidence secrets, payment values, and verification codes still do.
- OpenRouter Decisions requests send Hermes Agent app headers (`HTTP-Referer`, `X-Title`, `X-OpenRouter-Title`) so usage shows as Hermes Agent instead of Unknown. Direct TypeSafe requests do not send those headers.
- Adds an operation-level finalization boundary to computer use: an expected failure after execution began returns the partial ledger (known actions, uncertain-effect actions, provider decisions, last observation) instead of a generic error, and marks reconciliation before retry.
- Normalizes OpenRouter's documented Decisions response `id` to the canonical internal `request_id`; contradictory dual identifiers are rejected and no identifier is invented.
- Receipt validation now checks the record exactly as supplied: unknown fields are rejected with a set-difference test, persistence and readback round-trip the canonical record, a successful load requires the complete evidence group, and `cost: null` is the single unknown-cost representation.
- Public tool errors preserve `PartialAccountingError` evidence: a known request subtotal with explicit `total_usage_incomplete`, redacted identifiers, and no doubled accounting. Multi-skill and model-routing outer batch failures merge accounting from earlier successful batches.
- Documents the credential-ownership boundary: keys come from Hermes' profile secret scope; host-managed native Decisions transport awaits a Hermes core capability and is an explicit host-change dependency.
- `hermes switchyard status --json` reports the registered automatic routing mode, consumer mode, standing acknowledgement, and whether hosted construction is allowed. A fresh process must register the plugin before those fields are populated.
- Load mode skips automatic `skill_view` when the selected identifier conflicts with configured mandatory skills, records `mandatory_conflict`, and does not load.
- `jev_model_route` remains the documented Hermes routing point. `route_model_from_registry` uses a code-owned approved candidate registry; stale `registry_generation` values abstain as `stale_registry` with no egress, and an empty registry abstains as `empty_registry`. A selected route does not change the Hermes runtime model.
- Registers `jev_computer_use` in the `computer_use` toolset by default on Windows, macOS, and Linux. Catalog visibility no longer requires Jev credentials. Standing `public_or_sanitized_data_ack` is on after install; callers may omit it. A live Jev route is still required. Other Switchyard tools remain on `hermes_switchyard`.
- Web goals on `jev_computer_use` use a DOM browser loop: one Jev request per step chooses operation and click target together, then a Chromium-family browser clicks the page. Hermes `computer_use` is not between those clicks. Desktop apps without a URL still use Cua Driver.
- Decision tools stay visible without a saved key so operators are not left hunting for a missing catalog entry. Install prints `after-install.md`; `hermes switchyard guide` reprints the same next steps.

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
