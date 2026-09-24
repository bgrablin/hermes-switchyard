# Automatic skill recommendations

Automatic skill recommendations are implemented in `hermes_switchyard/automatic.py` and registered through Hermes' `pre_llm_call` plugin hook. The standalone per-turn contract is implemented in `hermes_switchyard/egress.py`.

The feature defaults to hosted_sanitized + load with standing acknowledgement; opt down to advisory/local_only for privacy:

- it recommends an exact skill identifier;
- `advisory` mode does not load a skill;
- `load` mode invokes Hermes' normal `skill_view` loader once for an accepted exact identifier in a turn;
- it does not change the system prompt, toolset, active model, provider, credentials, or fallback policy;
- it uses local token matching for a local-only fallback;
- the product defaults are `hosted_sanitized`, `automatic_skill_consumer_mode=load`, and standing acknowledgement true; hosted construction requires load mode, standing acknowledgement, the plugin-owned strict local per-turn scan, and either an allowed host `turn_egress_policy` envelope (`egress_authority: host_envelope`) or a clean local scan under standing ack (`egress_authority: standing_ack`); advisory mode records `consumer_contract_unmet` and never hosts;
- persistent acknowledgement is required for standalone hosted mode, but local per-turn scanning remains mandatory and the acknowledgement never overrides restricted content or other controls.

The plugin manifest declares `pre_llm_call` in `provides_hooks`.

## Runtime flow

For each Hermes user turn, the plugin receives the normal `pre_llm_call` callback payload. The callback uses only:

- `user_message` for bounded local matching;
- Hermes' profile-scoped `tools.skills_tool.skills_list()` response as the default local candidate source;
- explicit configured candidates when `automatic_skill_candidates` is non-empty;
- an optional `turn_egress_policy` allow envelope when the host classifies the turn; when absent, standing acknowledgement plus a clean local scan authorize hosting; explicit non-allow envelopes still fail closed.

The plugin never uses prior user or assistant messages, or the cached system prompt, as a hosted candidate catalog. The original task is bounded to 4,000 characters for local matching. A hosted call uses only the authorized bounded payload (envelope `allowed_payload`, or standing-ack task text) and exact candidate identifiers. Candidate descriptions, conversation history, and full skill bodies stay local. In advisory mode, a recommendation is returned to Hermes as ephemeral user-message context:

```text
Advisory skill recommendation: consider the exact skill identifier "..." if it fits this request. The plugin did not load it. Mandatory skills, explicit instructions, safety controls, and the user's preferences take precedence.
```

The sentence is model-visible context, not a separate status message. Hermes keeps the system prompt unchanged. In `load` mode (install default), the hook calls the normal `skill_view` path directly and returns the loaded skill body as turn context. It never substitutes its own file reader.

## Local recommendation path

Local matching is deterministic and does not call OpenRouter or Jev.

1. Candidate names and descriptions are validated without trimming identifiers.
2. An explicit `automatic_skill_candidates` list has no catalog-size cap. Each name is limited to 128 characters and each description to 1,000 characters.
3. With no explicit list, the hook reads the full Hermes active profile `skills_list()` registry, filters disabled/platform-ineligible skills, and ranks it locally.
4. Matching uses bounded token overlap with a small local stopword list.
5. The top candidate must meet the local threshold and beat the next candidate by the local margin. Otherwise the hook abstains.
6. Results are cached per plugin process. The default cache lifetime is 30 seconds; the configured maximum is 300 seconds.

The local threshold and margin are policy gates, not calibrated probabilities. A selection means only that the local deterministic matcher crossed the configured gates.

## Hosted Jev path

The automatic path has three explicit routing modes:

| Mode | Local matching | Hosted client construction |
| --- | --- | --- |
| `off` | disabled | never |
| `local_only` | enabled | never |
| `hosted_sanitized` | install default | requires `load` consumer mode and acknowledgement true; host allow envelope or standing ack + clean local scan |

`hosted_sanitized` + `load` + standing acknowledgement are the product defaults after install. Ordinary turns may construct hosted Jev when a provider key is present and the local restricted-pattern scan is clean. Opt down to `local_only` / `advisory` / ack false for privacy; advisory consumer mode cannot authorize hosted work (`consumer_contract_unmet`). The acknowledgement is not Hermes-owned DLP.

The hosted request uses the selected fixed Jev endpoint with OpenRouter fallbacks disabled. Automatic hosted routing uses a separate intervention deadline (default 20 seconds via `automatic_skill_deadline_seconds`) that stays below the typical Hermes plugin callback timeout (~30 seconds) and is distinct from the 60-second explicit decision / computer-use deadline and from the per-request provider I/O timeout (~25 seconds). Remaining budget is checked before every partition request and final reduction. When the host cannot cancel the callback, further requests are prevented and late provider results are discarded. Receipts record `deadline_exceeded`, `host_cancelled`, and `late_result_discarded` distinctly from generic transport failures.
 A transport failure is reported as `hosted_failure` when there is no local winner, or `hosted_failure_local_fallback` when a local winner is preserved; a valid hosted abstention remains abstention and does not fall back locally. Hosted metadata is retained only in the typed routing receipt and callback state; it is not a user-facing completion claim.

The standalone envelope contract is:

```json
{
  "version": 1,
  "decision": "allow",
  "data_class": "public",
  "reason_code": "host_policy_allowed",
  "allowed_payload": "bounded public or sanitized task text"
}
```

The accepted envelope shape uses `allow` for `decision`, `public` or `sanitized` for `data_class`, and a non-empty, control-safe `allowed_payload` of at most 4,000 characters. Envelopes containing `deny`, `unknown`, malformed, or restricted values fail closed before `client_factory()` is called. With no envelope and standing acknowledgement true, the plugin scans the bounded task locally for restricted patterns; a clean scan authorizes hosting with `egress_authority: standing_ack` and `reason_code: standing_ack_allowed` (bounded task text is the hosted payload). Restricted scan hits still fail closed. The local scan flags explicit restricted markings such as `confidential`, `classified`, `export-controlled`, `hipaa`/`phi`, and the explicit `CUI` / `Controlled Unclassified Information` banners. The scan is heuristic, not a comprehensive DLP standard, so a clean scan never implies certified sanitized or authorized content beyond the operator's standing acknowledgement. The plugin owns its local classification and is not a DLP engine.

When hosting is authorized (host envelope or standing ack), the platform policy permits the turn, and `automatic_skill_jev_mode` is `always`, Jev is called even when local matching is confident. `uncertain_only` is an explicit latency-saving override. The default two-stage path sends only the allowed payload and candidate names; opting in to `descriptions` or `excerpt` sends screened, bounded detail for stage-two finalists. It never sends conversation history or full skill bodies. Before hosted work, a confidence-bounded `local_prefilter_shortlist` may reduce large catalogs when the score cutoff is clear. In `uncertain_only` mode, a cheap `local_no_skill_gate` may abstain when lexical overlap is near zero. Insufficient margin keeps the complete catalog in bounded stage-one partitions, followed by a top-K recheck; disabling two-stage routing restores the legacy fan-out path. The provider's 255-option Choice limit is not a catalog limit. Receipts record which shortlist policy ran.

A valid hosted abstention is terminal for that turn and does not fall back to the local winner. A transport or client failure is unavailable and may preserve a local recommendation. Automatic results expose redacted `routing_status` and `routing_reason` metadata; they never expose task text, policy payload, descriptions, history, skill bodies, provider exception text, or credentials.

### Host-provided per-turn envelope

Hosts may forward a typed allow envelope on the existing invocation:

```python
_invoke_hook(
    "pre_llm_call",
    # existing fields remain unchanged
    turn_egress_policy=typed_allow_envelope,
)
```

When present, the host must forward only a typed envelope; the plugin still enforces standing acknowledgement, local policy, and redacted receipts. Envelope evaluation (when hosted mode is selected) still runs so redacted policy metadata can be recorded, but advisory consumer mode skips hosted client construction as `consumer_contract_unmet`. In `load` mode with standing acknowledgement true and no envelope, a clean local scan allows hosting (`egress_authority: standing_ack`). Denied, unknown, malformed, or restricted envelopes still skip hosting. Local matching may still run because it does not cross the boundary. Current Hermes core may not yet propagate the envelope on every path; standing acknowledgement covers that gap for clean turns without inventing a second policy language.

## Routing receipts and diagnostics

Every automatic recommendation ends by creating one typed receipt. The terminal state is one of `local_selection`, `hosted_selection`, `hosted_abstention`, `hosted_failure`, `hosted_failure_local_fallback`, `hosted_skipped`, or `cache_hit`. A cache-hit receipt reports no hosted attempt, request, latency, usage, or request ID for that attempt; the selected source remains the origin of the cached result.

Every finished turn also appends one record to `receipt-history.jsonl`, the bounded per-turn history next to the latest-receipt file in the profile plugin-data directory. Each record holds the canonical receipt plus `session_id`, `turn_id`, `platform`, and a UTC `recorded_at` timestamp. Task text, conversation history, candidate descriptions, and provider text are never stored. The file is append-only JSONL bounded to the last 500 records and 1 MiB; each append rewrites the retained tail atomically under a bounded-wait lock with private permissions.

The supported operator diagnostic commands are:

```text
hermes switchyard status --json
hermes switchyard receipt --json
hermes switchyard receipt --session <id>
hermes switchyard receipt --last <n>
hermes switchyard stats [--since 24h]
```

`status` is local and network-free. After the plugin registers in a fresh process, it reports the effective `routing_mode`, `consumer_mode`, standing acknowledgement, `automatic_skill_jev_mode`, and `hosted_construction_allowed`. Credential presence remains a separate readiness field. It also reports `plugin_version` and a `tool_exposure` object that separates whether Hermes holds this plugin's registration for each tool from whether the tool is in the callable catalog for a session; see [Confirm what a session exposes](SETUP.md#confirm-what-a-session-exposes). A process started before a config change can retain the previous hook and values.

`receipt` prints the latest receipt retained by the plugin. The receipt contains stable source, selection, attempt, error/skip, model, request, latency, usage, candidate-count, and shortlist-policy fields. In load mode it also records `consumer_status`, `loaded_skill`, `loaded_source`, and `skill_load_verified`. `consumer_status` may be `loaded`, `load_failed`, `explicit_override`, or `mandatory_conflict`. `verified` remains `false`: the receipt does not prove recommendation correctness, a model change, or GUI completion. If no attempt has produced a receipt, the command prints a structured `no_receipt` diagnostic and exits non-zero. `receipt --session <id>` prints the retained history records for one session and `receipt --last <n>` the newest n records; the two combine, and an empty lookup prints a structured `no_matching_receipts` or `no_receipt_history` diagnostic and exits non-zero.

`stats` summarizes the retained history: turns, sessions, selection / no-selection / abstention counts and rates, hosted attempts, successes, and failures broken down by sub-code, hosted skip reasons, skill load rate, p50/p95 turn latency, request counts and requests per turn, and known cost per turn and in total. `--since` accepts a window such as `30m`, `24h`, `7d`, or `2w`. Cost totals include only turns whose cost is known; the unknown-cost turn count is reported alongside so a partial total is never presented as complete.

Receipts include the plugin version and an exact source SHA. A validated `SOURCE-MANIFEST.json`, such as in a release archive, takes precedence. Without a manifest, the checked-out git commit is read directly from `.git/HEAD` and its loose or packed ref with no subprocess (linked worktrees included), so git installs report an exact SHA. Sources that resolve neither way use the explicit `unavailable` value. Task text, candidate descriptions, conversation history, credentials, local paths, and provider exception text are not serialized.

## Configuration

All settings are profile-scoped under `plugins.entries.hermes-switchyard.settings`:

| Key | Default | Effect |
| --- | ---: | --- |
| `automatic_skill_recommendation` | `true` | Register the automatic `pre_llm_call` hook. Set `false` to disable the feature. |
| `automatic_skill_consumer_mode` | `load` | `advisory` injects recommendation context and cannot authorize hosted construction (`consumer_contract_unmet`). `load` invokes Hermes' normal skill loader once per accepted turn, exposes typed load readback, and is required for hosted Jev. |
| `automatic_skill_candidates` | `[]` | Explicit list of strings or `{name, description}` objects. Empty means use the full Hermes active profile `skills_list()` registry. |
| `automatic_skill_local_threshold` | `0.20` | Minimum local token-overlap score. Clamped to `[0, 1]`. |
| `automatic_skill_local_margin` | `0.05` | Minimum gap between the top two local candidates. Clamped to `[0, 1]`. |
| `automatic_skill_cache_seconds` | `30.0` | Per-process recommendation cache lifetime. Clamped to `[0, 300]`. |
| `automatic_skill_deadline_seconds` | `20.0` | End-to-end automatic hosted routing deadline. Kept below the typical Hermes ~30s callback timeout; separate from explicit tool deadlines. |
| `automatic_skill_routing_mode` | `hosted_sanitized` | `off`, `local_only`, or `hosted_sanitized`. Hosted mode requires `automatic_skill_consumer_mode=load`, standing acknowledgement, and either a host allow envelope or a clean local scan. |
| `automatic_skill_jev` | `true` | Deprecated compatibility switch retained for configuration compatibility. It never authorizes hosted egress; set `automatic_skill_routing_mode` to `hosted_sanitized` for explicit opt-in. |
| `automatic_skill_jev_mode` | `always` | Evaluate the full catalog on every allowed turn. `uncertain_only` is an explicit latency-saving override. |
| `automatic_skill_public_or_sanitized_data_ack` | `true` | Operator attestation required before hosted automatic routing sends bounded task text and candidate identifiers to Jev. The install default is `hosted_sanitized` in load mode; `local_only` never constructs a hosted request. The acknowledgement is not DLP, and private, employer, regulated, credential, payment, or verification content remains prohibited. |
| `automatic_skill_mandatory_skills` | `[]` | Exact skill identifiers treated as mandatory. In `load` mode, a different recommendation is not auto-loaded and is recorded as `mandatory_conflict`. |
| `automatic_skill_two_stage` | `true` | Use bounded two-stage hosted selection on allowed turns. `false` retains the previous full-catalog selection path. |
| `automatic_skill_platforms` | `[]` | Empty uses the interactive policy, which skips API-server, batch, cron, webhook, and Kanban worker turns. `[all]` opts in every platform. A list such as `[cli, telegram]` routes only those platforms; a Kanban worker also requires `kanban` in the list. |
| `automatic_skill_hosted_detail` | `names` | Stage-two detail: `names` sends names only, `descriptions` opts in bounded descriptions, and `excerpt` opts in descriptions plus bounded SKILL.md excerpts for top-K finalists. Detail is locally screened but this is not DLP. |
| `automatic_skill_recheck_top_k` | `3` | Maximum finalists rechecked in stage two; clamped to 1–8. |
| `automatic_skill_early_stop` | `true` | Permit a low first-request needs-skill signal to stop before partition fan-out. |
| `automatic_skill_early_stop_threshold` | `0.30` | Uncalibrated early-stop policy threshold, clamped to `[0, 1]`. |
| `automatic_skill_stage1_min_probability` | `0.05` | Uncalibrated minimum stage-one probability for a recheck finalist, clamped to `[0, 1]`. |
| `automatic_skill_parallel_requests` | `4` | Maximum parallel stage-one requests; clamped to 1–8 and bounded by the operation deadline and request cap. |

The bundled two-stage benchmark uses synthetic fixtures and an offline oracle. It compares request shape and information boundaries, not hosted Jev accuracy. Keep `names` unless the operator separately approves sending bounded descriptions or excerpts to the configured provider.

Configuration is read when the plugin registers. Start a fresh Hermes process after changing these settings; an existing process may retain the previous hook and values.

## Model routing

`jev_model_route` is the documented Hermes routing point for model recommendations. Call it with an approved candidate registry whose `approved`, cost, data-class, tool, context, and `registry_generation` fields are code-owned. Descriptions never confer approval.

`hermes_switchyard.model_registry.route_model_from_registry` and `hermes_switchyard.model_route_adapter.recommend_model_route` are the explicit adapters for that registry (see [MODEL-ROUTING.md](MODEL-ROUTING.md)). The shipped registry is empty. Coordinators pass a real approved list at the routing point. A selected route remains an auditable recommendation: the adapter does not change the Hermes runtime model, provider, credentials, or fallback policy. Distinct outcomes:

- `empty_registry` when no candidates are configured
- `stale_registry` when every candidate fails the required `registry_generation`
- `no_eligible_candidates` for other policy exclusions, including unapproved or over-budget routes
- provider unavailability is raised as a transport/client error with no automatic fallback

## Privacy and account boundary

The fallback local path sends no automatic recommendation request to Jev. The current task still enters the user's selected Hermes model through the normal turn, so local matching is not a DLP control.

By default, the automatic hosted path sends only the authorized bounded task payload and exact candidate names to the selected Jev endpoint. Opting in to `automatic_skill_hosted_detail=descriptions` adds bounded descriptions for stage-two finalists; `excerpt` also adds bounded SKILL.md excerpts. Both detail modes screen each field locally, but this is not DLP. Conversation history and full skill bodies remain local. A successful plugin-owned classification does not establish provider retention, residency, or zero-data-retention properties.

The selected credential is separate from a Codex or ChatGPT subscription. Save either provider key through Switchyard's masked setup command; never put a key in a URL, shell history, config value, repository file, or issue report.

```text
hermes switchyard setup --provider typesafe
# or: hermes switchyard setup --provider openrouter
```

`hermes plugins list --enabled` is a metadata/readiness check; it must not print credentials. Profiles do not share secrets automatically.

## Typed skill consumer and no model switch

Advisory mode does not call Hermes' skill loader and cannot authorize hosted automatic routing: hosted work abstains with `consumer_contract_unmet` unless consumer mode is `load`. Opt-in load mode passes the accepted exact identifier to Hermes' supported `skill_view` loader (the supported consumer seam). Explicit skill instructions are resolved **before** any local or hosted selection, recorded as a pre-routing `explicit_override` skip with zero provider requests, and suppress automatic loading. Abstention and invalid output do nothing; loader rejection fails closed to advisory context. Repeated delivery of the same identified turn reuses the first callback result rather than loading again.

Every automatic turn records `delivery_status`, `adoption_status`, and `outcome_status` separately. The plugin always leaves `outcome_status` as `unverified` so delivery alone is never claimed as improvement. Paired evaluation must show the on arm improving adoption or outcome over the off arm.

`jev_model_route` remains a separate advisory tool. Automatic recommendations do not select a new Hermes model, provider, account, credential, toolset, or fallback. A Jev fit signal is not a calibrated quality or safety claim, and the active model may encode cost, quota, capability, residency, or authorization policy.

## Decision primitives

`jev_assess` and the internal routing paths use Jev `Choice`, `Score`, and
`Noul` questions. Choice is for a closed set, Score is for an ordered rubric,
and Noul is for a yes/no proposition. Independent questions sharing one state are
packed into bounded requests and recombined; code owns thresholds, weights, and
side effects. Score answers are validated before returning and are not treated as
calibrated truth.

## Observable hook behavior

A successful local smoke can be run without a TypeSafe or OpenRouter account:

```text
hermes chat -q "Diagnose an exiting Docker Compose container"
```

If the current profile's `skills_list()` registry contains a matching skill, advisory mode adds a recommendation for `docker-management`. Load mode (install default) instead returns the body read through Hermes' normal loader and reports an exact typed load result. Automatic routing is `hosted_sanitized` after install. Opt down to `local_only` / `advisory` for privacy. Advisory mode alone records `consumer_contract_unmet`. In load mode without an envelope, a clean local scan allows hosting with `egress_authority: standing_ack`.

The local hook can abstain when the registry is empty, no candidate overlap exists, or the top match is ambiguous. Abstention is normal behavior, not a failed skill load.

To make the smoke deterministic, configure an explicit candidate list before starting a fresh process:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_candidates '[{"name":"docker-management","description":"Manage Docker containers and Compose services."}]'
```

The config command parses list and mapping literals as YAML/JSON values. The list is still validated by the plugin and is not a permission grant.

For hosted-path smokes, configure the account first. Install defaults already use load + hosted_sanitized; to observe the contract gate, temporarily set advisory:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_routing_mode hosted_sanitized
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_jev_mode always
```

A plain `hermes chat` smoke in that advisory configuration still exercises local matching only: the routing receipt reports `hosted_skip_reason=consumer_contract_unmet` and no hosted client is constructed.

Install defaults already enable load consumer and standing acknowledgement. To exercise the missing-envelope standing-ack path, keep those defaults:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_consumer_mode load
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_public_or_sanitized_data_ack true
```

Use only a task and configured metadata that are public or already sanitized. Start a fresh process. With load mode and acknowledgement true but no host-forwarded `turn_egress_policy`, a clean local scan allows hosted construction and redacted metadata reports `egress_authority=standing_ack` / `policy_reason=standing_ack_allowed`. To exercise host-envelope authorization, have the host forward an allowed envelope such as:

```json
{"version":1,"decision":"allow","data_class":"sanitized","reason_code":"host_policy_allowed","allowed_payload":"Diagnose an exiting Docker Compose container"}
```

Denied, unknown, malformed, or restricted envelopes still fail closed. A hosted failure may preserve a local selection or report `hosted_failure` when none exists; a valid hosted abstention remains abstention. No fallback provider or model is selected.

## Source anchors

- `hermes_switchyard/automatic.py` — profile-scoped registry discovery, local ranking, routing modes, policy gating, redacted metadata, cache, receipts, and hook callback
- `hermes_switchyard/egress.py` — standalone versioned per-turn envelope contract and fail-closed evaluator
- `hermes_switchyard/receipt_state.py` — source identity, receipt contract validation, and plugin-owned diagnostic state
- `hermes_switchyard/receipt_history.py` — bounded per-turn receipt history, routing statistics, and git source SHA resolution
- `hermes_switchyard/__init__.py` — plugin settings and `ctx.register_hook("pre_llm_call", ...)`
- `plugin.yaml` — manifest hook declaration and configuration defaults
- Hermes `tools/skills_tool.py` — public profile-scoped `skills_list()` response used for catalog discovery
- Hermes `agent/turn_context.py` — `pre_llm_call` timing and user-message context injection
- Hermes `hermes_cli/plugins_dispatch.py` — callback and timeout behavior
- Hermes `hermes_cli/config.py` — scalar and YAML/JSON config value parsing
