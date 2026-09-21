# Changelog

## Unreleased

- Automatic hosted routing uses a separate intervention deadline (default 20s, below the typical Hermes ~30s plugin callback) from explicit decision/computer-use deadlines (60s). Remaining budget is checked before each partition/reduction request; late results after the deadline are discarded; distinct receipt codes record `deadline_exceeded`, `host_cancelled`, and `late_result_discarded`; intervention timeout is reported separately from provider I/O timeout (issue #26).
- DOM browser receipts: preserve the action ledger on provider timeout/validation failures as `partial_failure` with `reconcile_before_retry`; distinguish `action_dispatched` / `effect_observed` / `goal_verified`; do not set `effect_confirmed` for same-document clicks with no observed URL/title/DOM delta (issue #28).
- Hermes 0.19 status and live collector: when `parse_config_string_list` is missing, fall back to list/CSV parsing so `hermes switchyard status` resolves platform selection and reports real `callable` values (parser errors still fail closed); tolerate missing `hydrate_profile_secret_sources` and fall back to a process `OPENROUTER_API_KEY` when the scoped value is missing or unusable.
- Host config fallback for Hermes 0.19 reads `plugins.entries.<id>.settings` (legacy `.config` accepted); outer entry fields such as `allow_tool_override` are no longer mistaken for plugin settings.
- Tolerates Hermes 0.19.0 PluginContext hosts that omit `get_config`: `register()` falls back to `plugins.entries.<plugin_id>` (or install defaults) so tools, hooks, and `hermes switchyard` still register when the plugin is enabled.
- Makes automatic skill routing `local_only` by default. Hosted automatic routing now requires an explicit `hosted_sanitized` profile setting; the legacy boolean remains compatibility-only and never authorizes hosted egress.
- Resolves Ubuntu's `chromium-browser` Snap wrapper to `/snap/bin/chromium` and places each temporary browser profile under the Snap-accessible `~/snap/chromium/common` directory with exact per-run cleanup. Snap confinement is detected from the resolved executable and from a bounded wrapper-script prefix (quoted, unquoted, or `--`-separated exec targets; compiled binaries are never inspected as scripts). Directory preparation and per-run profile creation both raise a typed `snap_profile_unavailable` diagnostic, and `jev_computer_use` returns that code instead of a generic execution failure.
- Stores routing receipts under Hermes' profile-scoped `plugin-data/hermes-switchyard/` directory and migrates one valid legacy receipt without overwriting unrelated state. Receipt files are atomically published with owner-private permissions: `0600` on POSIX; on Windows a protected DACL granting only the current user, SYSTEM, and Administrators, applied to the published file on both the normal replace and legacy migration paths and failing closed when the descriptor cannot be enforced.
- Adds `hermes_switchyard.record_triage`, a bounded second demonstration of the `jev_assess` primitive. It qualifies public or synthetic records in bounded `jev_assess`-shaped batches with code-defined alternatives, an aggregate deadline, and request accounting; decides some records locally without a provider request; and feeds only accepted decisions to a deterministic consumer that writes a local work-queue artifact. `verify_artifact` re-checks that artifact from disk. It is a library module, not a registered tool, so the tool surface and `plugin.yaml` are unchanged. See `docs/RECORD-TRIAGE.md`.

## 0.4.2

- Automatic hosted Jev is on after install. `automatic_skill_public_or_sanitized_data_ack` defaults true. Set it false to skip hosted automatic routing. Topic words such as "private" or "verification" no longer skip Jev; high-confidence secrets, payment values, and verification codes still do.
- OpenRouter Decisions requests send Hermes-Switchyard app headers (`HTTP-Referer`, `X-Title`, `X-OpenRouter-Title`) so usage shows as Hermes-Switchyard instead of Unknown. Direct TypeSafe requests do not send those headers.
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
