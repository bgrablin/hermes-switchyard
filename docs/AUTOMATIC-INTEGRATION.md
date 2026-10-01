# Automatic skill routing: how it works

> **Advanced reference.** This page explains the full flow, every privacy gate, and every receipt field. To get started, read [Automatic skill routing: setup](AUTOMATIC-SETUP.md). For background, read the [Concepts primer](CONCEPTS.md).

## In short

On each turn, Switchyard's `pre_llm_call` hook picks an exact skill name for your request, or deliberately picks none. In the default `load` mode, it then loads that skill through Hermes' normal `skill_view` loader, once per turn.

It never changes your system prompt, toolsets, active model, provider, credentials, or fallback policy.

There are two ways to choose:

- **Local:** on-device word matching. It costs nothing and sends nothing.
- **Hosted:** asks Jev. This sends only a bounded, scrubbed copy of your current message and the candidate skill **names**.

The defaults are hosted (`hosted_sanitized`), `load`, and the standing acknowledgement `true`. Hosting requires `load` mode, the acknowledgement, and one of these:

- an "allow" policy from Hermes (`egress_authority: host_envelope`). Its `allowed_payload` is redacted but **not** re-scanned locally, because Hermes has already classified it; or
- no policy at all, plus a clean local per-turn scan (`egress_authority: standing_ack`).

The acknowledgement never overrides restricted content or any other control. `advisory` mode never hosts (`consumer_contract_unmet`). For more privacy, choose `local_only` or `advisory`.

**Where the code lives:**

- The hook: `hermes_switchyard/automatic.py`, registered through `pre_llm_call`, which `plugin.yaml` declares in `provides_hooks`.
- The per-turn policy contract: `hermes_switchyard/egress.py`.

## The flow, step by step

For each Hermes user turn, the hook reads only:

- `user_message`, for bounded local matching;
- your profile's skill registry (Hermes' `tools.skills_tool.skills_list()`), the default candidate source;
- `automatic_skill_candidates`, when you configured an explicit list;
- an optional `turn_egress_policy` "allow" envelope, if Hermes classified the turn. Without one, the acknowledgement plus a clean local scan authorize hosting. An explicit non-allow envelope always blocks.

It never uses earlier user or assistant messages, or the cached system prompt, as a source of candidates. The task is cut to 4,000 characters for local matching.

A hosted call sends only:

- the authorized bounded text: the envelope's `allowed_payload`, or the scanned and scrubbed message under standing acknowledgement;
- the exact candidate names.

By default, candidate descriptions stay local. Opting in with `automatic_skill_hosted_detail` sends bounded descriptions or excerpts for stage-two finalists (see [The two-stage selector](#the-two-stage-selector)). Conversation history and full skill bodies always stay local.

**What the main model sees:**

- In `load` mode (the default), the hook calls `skill_view` directly and returns the loaded skill as turn context. It never substitutes its own file reader.
- In `advisory` mode, the recommendation is added as temporary user-message context:

  ```text
  Advisory skill recommendation: consider the exact skill identifier "..." if it fits this request. The plugin did not load it. Mandatory skills, explicit instructions, safety controls, and the user's preferences take precedence.
  ```

  That sentence is context for the model, not a status message. The system prompt stays unchanged.

## Local matching

Local matching is deterministic and never calls OpenRouter or Jev:

1. Candidate names and descriptions are validated. Identifiers are never trimmed.
2. An explicit `automatic_skill_candidates` list has no size cap. Names are limited to 128 characters and descriptions to 1,000.
3. With no explicit list, the hook reads your full profile registry, drops disabled and platform-ineligible skills, and ranks the rest locally.
4. Matching scores word overlap, ignoring a small list of stopwords.
5. The top candidate must reach the threshold (`automatic_skill_local_threshold`) **and** beat the runner-up by the margin (`automatic_skill_local_margin`). Otherwise, it abstains.
6. Results are cached in memory for 30 seconds by default (maximum 300).

The threshold and margin are policy gates, not calibrated probabilities. A local pick means only that the matcher cleared those gates.

## Hosted Jev path

### Routing modes

| Mode | Local matching | Asks Jev? |
| --- | --- | --- |
| `off` | Disabled | Never |
| `local_only` | Enabled | Never |
| `hosted_sanitized` (default) | Enabled | Only with `load` mode, acknowledgement `true`, and either a host "allow" policy or a clean local scan |

Hosted calls go to the fixed Jev endpoint you configured, with OpenRouter's provider fallbacks disabled. The acknowledgement is your attestation, not Hermes-owned DLP.

### Time limits

Automatic routing has its own deadline: 20 s by default (`automatic_skill_deadline_seconds`). That keeps it under Hermes' typical ~30 s plugin-callback timeout. It is separate from:

- the 60 s deadline for explicit decisions and computer use;
- the ~25 s per-request provider I/O timeout.

Switchyard checks the remaining budget before every request. If Hermes can't cancel the callback, Switchyard stops issuing new requests and discards late answers. Receipts record `deadline_exceeded`, `host_cancelled`, and `late_result_discarded` separately from ordinary transport failures.

### When Jev fails or abstains

- **Jev validly answers "no skill":** that's final for the turn. There's no local fallback.
- **Any other hosted failure** (transport or client error, invalid or malformed answer, deadline, cancellation, or partial-accounting failure): recorded as `hosted_failure` if there's no local winner, or `hosted_failure_local_fallback` if a local pick is kept.

Hosted details live only in the typed routing receipt and callback state. They are not a claim that the task succeeded.

### The per-turn policy envelope

```json
{
  "version": 1,
  "decision": "allow",
  "data_class": "public",
  "reason_code": "host_policy_allowed",
  "allowed_payload": "bounded public or sanitized task text"
}
```

An envelope is accepted only when:

- `decision` is `allow`;
- `data_class` is `public` or `sanitized`;
- `allowed_payload` is non-empty, free of control characters, and at most 4,000 characters.

`deny`, `unknown`, malformed, or restricted envelopes block the turn before any client is created.

With no envelope and the acknowledgement `true`, Switchyard scans your whole message locally, up to 64,000 characters. A longer message stays local with `local_scan_oversized`. A clean scan authorizes hosting with `egress_authority: standing_ack` and `reason_code: standing_ack_allowed`.

### What the local scan blocks and what it scrubs

Since v0.5.5, a message is **scrubbed, not blocked**, unless it contains something scrubbing can't make safe.

**Kept local entirely:**

- restricted document markings: `proprietary`, a standalone `Confidential` banner, and `company/employer/client confidential`. This is the same rule adaptive effort uses (reason `local_scan_restricted_marking`).
- prompt-injection wording
- control characters
- opaque structured payloads

**Not blocked:**

- topic words such as `password`, `credential`, or `confidential` on their own
- email addresses and phone numbers, which aren't masked either

**Scrubbed before sending:** the whole message goes through Hermes' egress redactor (`agent.redact.redact_for_egress`), plus Switchyard's own masks for `password=VALUE` / `--password VALUE` forms and Luhn-valid card numbers. Only the redacted text, cut to 4,000 characters, is sent.

**If Hermes' redactor isn't available:** no task text is sent. The hosted call is skipped with `redaction_unavailable`, and local routing still applies.

Skill names and stage-two descriptions are your own catalog text. They still use the older, stricter blocklist. None of this is a DLP engine.

### Which turns ask Jev

When hosting is authorized, the platform policy allows the turn, and `automatic_skill_jev_mode` is `always`, Jev is called even if local matching is confident. These exceptions skip Jev:

- **Light turns** (`automatic_skill_light_turn_bypass`, on by default): closed-list acknowledgements, greeting-style instructions, and pure read-only listings of the current directory (for example, "List the files in the current directory. Do not modify anything.") make zero Jev requests. Listings of any other path still go to Jev. Open-ended explanations still go to Jev, so a catalog skill isn't missed because of a fixed word list.
- **No-overlap turns** (`automatic_skill_honor_no_skill_gate`, off by default): when enabled, skips Jev under `always` if the request shares almost no words with any skill.
- **`uncertain_only` mode:** an explicit latency-saving override. It already honors the no-skill gate (`local_no_skill_gate`).

### The two-stage selector

By default:

1. **Shortlist.** For large catalogs, a confidence-bounded `local_prefilter_shortlist` may trim the list when the score cutoff is clear. Otherwise, the whole catalog is split into bounded **stage-one** partitions.
2. **Recheck.** The top finalists (`automatic_skill_recheck_top_k`) are rechecked in **stage two**.

Only names are sent by default. Setting `automatic_skill_hosted_detail` to `descriptions` or `excerpt` sends screened, bounded detail for stage-two finalists. History and full skill bodies are never sent.

Turning two-stage off restores the older single fan-out. The provider's 255-option limit per question is not a catalog limit. Receipts record which shortlist policy ran.

Automatic results expose redacted `routing_status` and `routing_reason` metadata. They never expose:

- task text or the policy payload
- skill descriptions or bodies
- history
- provider error text
- credentials

### Host-provided per-turn envelope

A host can forward a typed allow envelope on the existing hook call:

```python
_invoke_hook(
    "pre_llm_call",
    # existing fields remain unchanged
    turn_egress_policy=typed_allow_envelope,
)
```

The host must forward only a typed envelope. Switchyard still enforces the acknowledgement, local policy, and redacted receipts.

- Envelope evaluation runs whenever hosted mode is selected, so its redacted metadata can be recorded.
- In `advisory` mode, client creation is still skipped (`consumer_contract_unmet`).
- In `load` mode with the acknowledgement `true` and no envelope, a clean local scan allows hosting (`standing_ack`).
- Denied, unknown, malformed, or restricted envelopes still block hosting.
- Local matching can still run, because it never leaves your machine.

Current Hermes may not pass the envelope on every path. Standing acknowledgement covers clean turns in that gap, without inventing a second policy language.

## Routing receipts and diagnostics

Every automatic recommendation ends with one typed receipt. Its terminal state is one of:

| State | Meaning |
| --- | --- |
| `local_selection` | Local matching picked a skill |
| `hosted_selection` | Jev picked a skill |
| `hosted_abstention` | Jev validly said "no skill" |
| `hosted_failure` | Jev failed and there was no local pick |
| `hosted_failure_local_fallback` | Jev failed and a local pick was kept |
| `hosted_skipped` | Hosting was skipped (see `hosted_skip_reason`) |
| `cache_hit` | Reused a recent result. It reports no new request, latency, usage, or request ID; the source is that of the cached result. |

### History file

Every finished turn also appends one record to `receipt-history.jsonl`, next to the latest-receipt file in the profile's plugin-data folder.

- **Contents:** the receipt plus `session_id`, `turn_id`, `platform`, and a UTC `recorded_at` timestamp. Task text, history, skill descriptions, and provider text are never stored.
- **Size:** at most 500 valid records and 1 MiB.
- **Writes:** ordinary turns append one line under a short-wait lock, without `fsync` on the hot path. Duplicates, a corrupt tail, or crossing a limit trigger an atomic compaction with private file permissions.

History is best-effort diagnostic data, not a durable audit log.

### Commands

```text
hermes switchyard status --json
hermes switchyard receipt --json
hermes switchyard receipt --session <id>
hermes switchyard receipt --last <n>
hermes switchyard stats [--since 24h]
```

**`status`** runs locally with no network. After the plugin registers in a fresh process, it reports:

- the effective `routing_mode`, `consumer_mode`, standing acknowledgement, `automatic_skill_jev_mode`, and `hosted_construction_allowed`;
- whether a key is present (as a separate readiness field);
- `plugin_version`;
- `tool_exposure`, which separates "registered" from "callable." See [Confirm what a session exposes](SETUP.md#confirm-what-a-session-exposes).

A process started before a config change may still have the old hook and values.

**`receipt`** prints the latest retained receipt. It covers source, selection, attempt, error or skip reason, model, request, latency, usage, candidate count, and shortlist policy.

- In `load` mode it adds `consumer_status`, `loaded_skill`, `loaded_source`, and `skill_load_verified`. `consumer_status` is one of `loaded`, `load_failed`, `explicit_override`, or `mandatory_conflict`.
- `verified` is always `false`. A receipt doesn't prove the recommendation was right, that a model changed, or that a GUI task finished.
- With no receipt yet, it prints a structured `no_receipt` diagnostic and exits non-zero.
- `--session <id>` shows that session's history and `--last <n>` the newest *n* records. They can be combined. An empty result prints `no_matching_receipts` or `no_receipt_history` and exits non-zero.

**`stats`** summarizes history:

- turns and sessions
- selection, no-selection, and abstention counts and rates
- hosted attempts, successes, and failures by sub-code
- hosted skip reasons
- skill-load rate
- p50/p95 turn latency
- requests per turn
- known cost per turn and in total

`--since` takes windows like `30m`, `24h`, `7d`, or `2w`. Cost totals include only turns with a known cost, and the number of unknown-cost turns is shown next to them, so a partial total is never presented as complete.

### Source identity

Receipts include the plugin version and an exact source SHA, resolved in this order:

1. A validated `SOURCE-MANIFEST.json`, as found in a release archive.
2. The checked-out Git commit, read directly from `.git/HEAD` and its loose or packed ref, with no subprocess. Linked worktrees are supported.
3. The explicit value `unavailable`.

Task text, skill descriptions, history, credentials, local paths, and provider error text are never serialized.

## Configuration

All settings live under `plugins.entries.hermes-switchyard.settings`. They are read when the plugin registers, so start a fresh Hermes process after changing them.

| Key | Default | Effect |
| --- | ---: | --- |
| `automatic_skill_recommendation` | `true` | Registers the automatic `pre_llm_call` hook. `false` disables the feature. |
| `automatic_skill_consumer_mode` | `load` | `load` calls Hermes' normal skill loader once per accepted turn, reports typed load results, and is required for hosted Jev. `advisory` only adds recommendation context and can't authorize hosting (`consumer_contract_unmet`). |
| `automatic_skill_candidates` | `[]` | Explicit list of strings or `{name, description}` objects. Empty uses your full profile registry. |
| `automatic_skill_local_threshold` | `0.20` | Minimum local word-overlap score, clamped to `[0, 1]`. |
| `automatic_skill_local_margin` | `0.05` | Minimum gap between the top two local candidates, clamped to `[0, 1]`. |
| `automatic_skill_cache_seconds` | `30.0` | In-process cache lifetime, clamped to `[0, 300]`. |
| `automatic_skill_deadline_seconds` | `20.0` | End-to-end deadline for hosted routing. Kept below Hermes' ~30 s callback timeout and separate from explicit tool deadlines. |
| `automatic_skill_routing_mode` | `hosted_sanitized` | `off`, `local_only`, or `hosted_sanitized`. Hosted mode needs `load`, the acknowledgement, and either a host allow envelope or a clean local scan. |
| `automatic_skill_jev` | `true` | Deprecated; kept for old configs. It never authorizes hosted egress. Use `automatic_skill_routing_mode`. |
| `automatic_skill_jev_mode` | `always` | Evaluate the full catalog on every allowed turn, subject to the light-turn bypass and the optional no-skill gate. `uncertain_only` is an explicit latency-saving override. |
| `automatic_skill_light_turn_bypass` | `true` | Skip hosted routing for acknowledgements, greeting-style instructions, and read-only listings of the current directory that state no changes may be made (other paths stay hosted). Open-ended explanations stay hosted. It runs after `routing_mode=off` and explicit-skill override, so those keep precedence. The optional early probe also refuses when a turn looks like an explicit skill use. `false` forces hosted evaluation on those turns. |
| `automatic_skill_early_light_bypass_before_discover` | `false` | Run the light-turn check before reading the catalog. Fail-open; consequential and explicit-override turns still read the catalog. |
| `automatic_skill_honor_no_skill_gate` | `false` | When `true`, skip hosted fan-out under `always` when local word overlap is near zero. The default keeps full fan-out for short, opaque tasks such as `fix ci`. |
| `automatic_skill_public_or_sanitized_data_ack` | `true` | Your attestation that hosted routing may send bounded task text and candidate names to Jev. Not DLP. Private, employer, regulated, credential, payment, and verification content stay prohibited. `local_only` never sends anything. |
| `automatic_skill_mandatory_skills` | `[]` | Exact skill IDs treated as mandatory. In `load` mode, a different pick isn't loaded and is recorded as `mandatory_conflict`. |
| `automatic_skill_two_stage` | `true` | Use the bounded two-stage selector. `false` keeps the older full-catalog path. |
| `automatic_skill_platforms` | `[]` | Empty uses the interactive policy, which skips API-server, batch, cron, webhook, and Kanban worker turns. `[all]` includes every platform. A list like `[cli, telegram]` routes only those; Kanban workers also need `kanban` in the list. An invalid non-empty list refuses routing rather than silently broadening it. |
| `automatic_skill_hosted_detail` | `names` | Stage-two detail. `names` sends names only, `descriptions` adds bounded descriptions, and `excerpt` also adds bounded `SKILL.md` excerpts for the top-K finalists. Screened locally, but not DLP. |
| `automatic_skill_recheck_top_k` | `3` | Maximum finalists rechecked in stage two, clamped to 1–8. |
| `automatic_skill_early_stop` | `true` | Lets a low "needs a skill" signal stop a no-fit turn. With single-round stage one it skips stage two; otherwise it stops before fan-out. |
| `automatic_skill_early_stop_threshold` | `0.30` | Uncalibrated early-stop threshold, clamped to `[0, 1]`. |
| `automatic_skill_stage1_min_probability` | `0.05` | Uncalibrated minimum stage-one probability for a finalist, clamped to `[0, 1]`. |
| `automatic_skill_parallel_requests` | `4` | Maximum parallel stage-one requests, clamped to 1–8 and bounded by the deadline and request cap. |
| `automatic_skill_stage1_single_round` | `true` | Send every stage-one partition in one parallel round. The first partition's "needs a skill" answer then only skips stage two. `false` probes partition 0 first, which saves requests on no-fit turns but adds a round to every other hosted turn. |

The bundled two-stage benchmark uses synthetic fixtures and an offline oracle. It compares request shape and what information crosses the boundary, not hosted Jev accuracy. Keep `names` unless you've separately approved sending descriptions or excerpts to your provider.

## Model routing

`jev_model_route` is Hermes' routing point for model *recommendations*. You call it with an approved candidate registry. Its `approved`, cost, data-class, tool, context, and `registry_generation` fields are owned by code, and descriptions never grant approval.

The explicit adapters for that registry are `hermes_switchyard.model_registry.route_model_from_registry` and `hermes_switchyard.model_route_adapter.recommend_model_route` (see [MODEL-ROUTING.md](MODEL-ROUTING.md)). The shipped registry is empty, so coordinators pass a real approved list.

A selected route is an auditable recommendation. It never changes the Hermes runtime model, provider, credentials, or fallback policy. Possible outcomes:

| Outcome | When |
| --- | --- |
| `empty_registry` | No candidates are configured. |
| `stale_registry` | Every candidate fails the required `registry_generation`. |
| `no_eligible_candidates` | Any other policy exclusion, such as unapproved or over-budget routes. |
| Transport or client error | The provider is unavailable. There is no automatic fallback. |

## Privacy and account boundary

**The local path** sends nothing to Jev. Your task still goes to your chosen Hermes model as part of the normal turn, so local matching isn't a DLP control.

**The hosted path** sends, by default, only the authorized bounded task text and exact candidate names to your Jev endpoint.

- `automatic_skill_hosted_detail=descriptions` adds bounded descriptions for stage-two finalists.
- `excerpt` also adds bounded `SKILL.md` excerpts.
- Both are screened locally, but this isn't DLP.

History and full skill bodies always stay local. A clean local classification says nothing about the provider's retention, data residency, or zero-data-retention terms.

**Your Jev key** is separate from any Codex or ChatGPT subscription. Save it only through the masked setup command, never in a URL, shell history, config value, repository file, or issue report:

```text
hermes switchyard setup --provider typesafe
# or: hermes switchyard setup --provider openrouter
```

`hermes plugins list --enabled` is a metadata and readiness check that never prints keys. Profiles don't share keys.

## Loading, overrides, and "no model switch"

- **`advisory`** mode never calls Hermes' skill loader and can't authorize hosting. Hosted work abstains with `consumer_contract_unmet`.
- **`load`** mode passes the accepted exact identifier to Hermes' supported `skill_view` loader.
- **Naming a skill yourself** is detected **before** any selection. It's recorded as a pre-routing `explicit_override` skip with zero provider requests, and it suppresses automatic loading.
- **Abstention and invalid output** do nothing.
- **A loader rejection** falls back to advisory context.
- **Repeated delivery of the same turn** reuses the first result rather than loading again.

Every automatic turn records `delivery_status`, `adoption_status`, and `outcome_status` separately. `outcome_status` is always `unverified`, so delivery alone is never claimed as improvement. A paired evaluation must show the "on" arm improving adoption or outcome over the "off" arm.

`jev_model_route` remains a separate advisory tool. Automatic recommendations never pick a new model, provider, account, credential, toolset, or fallback. A Jev fit signal isn't a calibrated quality or safety claim, and your active model may reflect cost, quota, capability, residency, or authorization policy.

## Decision primitives

`jev_assess` and the internal routing use three Jev question types:

- **Choice:** pick one from a closed set.
- **Score:** rate against an ordered rubric.
- **Noul:** yes or no on a statement.

Independent questions about the same state are packed into bounded requests and recombined. Code owns thresholds, weights, and side effects. Score answers are validated before use and are not treated as calibrated truth.

## Hands-on: observing the hook

You can run a local smoke test without a TypeSafe or OpenRouter account:

```text
hermes chat -q "Diagnose an exiting Docker Compose container"
```

If your profile has a matching skill:

- in `load` mode (the default), the skill body is loaded through Hermes' normal loader with an exact typed load result;
- in `advisory` mode, a recommendation for `docker-management` is added instead.

Abstention is normal when the registry is empty, nothing overlaps, or the top match is ambiguous. It's not a failed load.

**Make it deterministic** by setting an explicit candidate list before starting a fresh process:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_candidates '[{"name":"docker-management","description":"Manage Docker containers and Compose services."}]'
```

The config command parses list and mapping literals as YAML/JSON. Switchyard still validates the list, and the list is not a permission grant.

**To see the advisory gate,** configure an account, then set advisory mode while keeping hosted routing selected:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_routing_mode hosted_sanitized
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_jev_mode always
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_consumer_mode advisory
```

A `hermes chat` smoke then uses local matching only. The receipt shows `hosted_skip_reason=consumer_contract_unmet`, and no hosted client is created.

**To see the standing-acknowledgement path,** use the defaults:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_consumer_mode load
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_public_or_sanitized_data_ack true
```

Use only public or sanitized tasks and metadata, and start a fresh process. With no host-forwarded `turn_egress_policy`, a clean local scan allows hosting, and the redacted metadata shows `egress_authority=standing_ack` / `policy_reason=standing_ack_allowed`.

**To see host-envelope authorization,** have the host forward an allow envelope such as:

```json
{"version":1,"decision":"allow","data_class":"sanitized","reason_code":"host_policy_allowed","allowed_payload":"Diagnose an exiting Docker Compose container"}
```

Denied, unknown, malformed, or restricted envelopes still block. A hosted failure may keep a local pick, or report `hosted_failure` when there isn't one. A valid hosted abstention stays an abstention. No fallback provider or model is ever selected.

## Source anchors

**In this repository:**

- `hermes_switchyard/automatic.py`: registry discovery, local ranking, routing modes, policy gates, redacted metadata, cache, receipts, and the hook callback
- `hermes_switchyard/egress.py`: the versioned per-turn envelope contract and fail-closed evaluator
- `hermes_switchyard/receipt_state.py`: source identity, receipt validation, and diagnostic state
- `hermes_switchyard/receipt_history.py`: bounded per-turn history, routing statistics, and Git SHA resolution
- `hermes_switchyard/__init__.py`: plugin settings and `ctx.register_hook("pre_llm_call", ...)`
- `plugin.yaml`: the hook declaration and configuration defaults

**In Hermes:**

- `tools/skills_tool.py`: the profile-scoped `skills_list()` used for discovery
- `agent/turn_context.py`: `pre_llm_call` timing and user-message context injection
- `hermes_cli/plugins_dispatch.py`: callback and timeout behavior
- `hermes_cli/config.py`: scalar and YAML/JSON config parsing
