# Hermes Switchyard

Version: 0.5.4

Switchyard is a [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin for bounded [Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev) decisions. It can choose and load a matching skill for one turn, lower reasoning effort within your selected cap, and guide a public DOM browser or desktop task. Local receipts show what the plugin selected, skipped, or could not verify. Model routing remains advice: it does not switch your model.

![Rail-yard map of Hermes Switchyard: bounded decisions pass local policy before Hermes acts](docs/assets/hermes-switchyard-overview.png)

The [Switchyard branding image](docs/assets/hermes-switchyard-branding.png) is also packaged.

**Release state:** `plugin.yaml` in this branch still declares version 0.5.4. The 0.5.5 work is not released. Two opt-in features are proposed for 0.5.5:

- **Research Navigator (F1, pending):** an explicit tool will compare up to four claims with six caller-supplied public source windows. It will show support, contradiction, missing quotes, and unassessed windows. It will not fetch pages or verify that a supplied excerpt matches a live URL.
- **DOM Progress & Recovery (F2, pending):** an opt-in mode will assess repeated, unproductive steps in Switchyard's public DOM browser loop. It can stop that loop as incomplete and suggest a permitted next observation. It will not control desktop apps, repeat a mutation, or certify task completion. Its default will be `off`.

Neither pending feature is available in a current install. The [changelog](CHANGELOG.md) distinguishes work on the branch from work still pending. The [benchmark report](docs/BENCHMARKS.md) identifies the source and limits of its measurements: the selector value report uses 0.5.4 source `7dc77c8`; older feature-battery rows use 0.5.0 code. It does not prove that Jev improves every task.

## 60-second quickstart (after the install gate passes)

You need a working Hermes installation, one TypeSafe or OpenRouter account key, and approval to send **public or sanitized** task data to that provider. Jev requests can incur charges beyond a ChatGPT or Codex subscription. The commands below use the active Hermes profile.

1. Install the public Git repository and enable it:

   ```text
   hermes plugins install bgrablin/hermes-switchyard --enable
   ```

2. Save one provider key in the masked prompt. Do not put it in a command, chat, URL, or repository:

   ```text
   hermes switchyard setup --provider typesafe
   # or: hermes switchyard setup --provider openrouter
   ```

3. Start a fresh Hermes session. Read the local status:

   ```text
   hermes switchyard status --json
   ```

4. If you accept a billed **public synthetic** test, make one explicit decision request:

   ```text
   hermes switchyard test --live --public-or-sanitized-data-ack
   ```

The live test is optional. A status of `ready` checks key presence and tool exposure; it is not a successful Jev decision. The live test reports `passed` only after a provider response. Plugin Doctor checks registration, not account readiness or model quality. The installer can block a community Git source after a security scan. Read the findings before you approve any override; do not treat a blocked install as success. A fresh session or gateway restart is necessary before an already-running session uses a new plugin.

## What you will see

Automatic skill routing uses `hosted_sanitized` and `load` by default. An eligible turn can send a bounded task and candidate names to Jev, then load one accepted skill through Hermes. `/reasoning` remains the cap for adaptive effort. You can inspect the current session with `/switchyard effort status` and the stored routing decision with `hermes switchyard receipt --json` or `hermes switchyard stats --since 24h`.

**Example only, not observed output from this branch:**

```text
Switchyard adaptive reasoning effort
  mode: auto
  your level (cap): high
  last sent: low (jev_selected)
  requests: 1, Jev calls: 1
```

The current status command shows the last decision, not the last five. A per-turn line such as `switchyard: effort high→low (Jev 240 ms)` and five-decision history are **pending** in the effort lane. No receipt proves that an answer was correct or that a whole browser goal succeeded. For the exact current receipt contract, see [automatic routing](docs/AUTOMATIC-INTEGRATION.md) and [adaptive effort](docs/ADAPTIVE-REASONING-EFFORT.md).

## Privacy and egress

The two default-on automatic paths have different data rules:

- **Adaptive effort:** the current branch uses Hermes `agent.redact.redact_for_egress` through `redact_for_jev` on the whole current text message before it sends a bounded 1,200-character excerpt. It does not send conversation history, memory, tool bodies, or private files. If the Hermes redactor is absent, it sends no message text and keeps the requested effort. Explicit proprietary and confidential markings stay local. A clean scan does not prove that other private text is safe to send.
- **Skill routing:** the current branch scans the whole task locally for restricted markings, injection shapes, control characters, and structured payloads. It then passes clean text through Hermes `redact_for_egress` plus bounded masks before sending up to 4,000 characters and exact candidate names. Without the Hermes redactor, it skips hosted routing and uses local matching. Ordinary words such as `password` and `confidential`, email addresses, and phone numbers are not restricted shapes. They do not authorize private content. Bounded descriptions or skill excerpts require a separate opt-in.
- **Explicit tools and DOM browser:** only submit public or deliberately sanitized inputs. The browser cannot log in to your existing session or upload a private file. F1 and F2 will require a whole-payload eligibility gate and explicit public-data acknowledgement. A public URL alone will not make a private goal safe.

TypeSafe and OpenRouter are external providers. The acknowledgement flags are not data loss prevention. Switchyard does not authorize private, employer, regulated, credential, payment, or verification content for hosted decisions. To stop automatic hosted skill routing, use `local_only`. To stop adaptive message-text egress, set adaptive effort to `false`:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_routing_mode local_only
hermes config set plugins.entries.hermes-switchyard.settings.adaptive_reasoning_effort false
```

You can also set `public_or_sanitized_data_ack` and `automatic_skill_public_or_sanitized_data_ack` to `false` independently. Start a fresh session after a setting change. Read [setup](docs/SETUP.md) and [automatic routing](docs/AUTOMATIC-INTEGRATION.md) for the full boundary.

## Supported features

| Surface | Available now | Default |
| --- | --- | --- |
| Tools (7) | `jev_assess`, `jev_skill_select`, `jev_skill_select_many`, `jev_model_route`, `jev_model_route_approved`, `jev_session_search_rerank`, `jev_computer_use` | Callable when their toolsets are selected |
| Hooks (3) | `pre_llm_call`, `post_llm_call`, `post_tool_call` | Registered after enablement |
| Middleware (1) | `llm_request` for request-scoped effort | On when supported by Hermes |

`jev_skill_select_many` recommends several skills but does not load them. The automatic hook can load one accepted skill per identified turn. `jev_model_route` and `jev_model_route_approved` do not apply a model switch. Jev `DONE` alone does not verify a browser goal; Switchyard also needs its local completion condition. An early local stop is a candidate, not verified success. [Browser receipts](docs/DOM-BROWSER-BACKEND.md) show the separate action, effect, and goal fields.

## Automatic skill recommendations

Switchyard reads the active profile's skills at the beginning of an eligible turn. Its default hosted path uses one or more bounded Jev requests, then can load one accepted skill. Explicit skill instructions take precedence. Invalid results, policy refusal, missing candidates, or a loader error can leave the turn without a loaded skill. The local matcher can also abstain. Set `automatic_skill_routing_mode=local_only` to keep this plugin's automatic skill selection local, and `automatic_skill_consumer_mode=advisory` to stop automatic loads. Neither change prevents normal Hermes model calls or explicit Jev tools. [Routing details](docs/AUTOMATIC-INTEGRATION.md) give the current gates and receipt fields.

## Computer use

`jev_computer_use` uses a fresh, ephemeral Chromium profile for eligible public DOM goals. It does not attach to a signed-in browser, authenticate, or upload files. The caller supplies ordinary field values in `text_inputs`; Jev chooses a safe target but does not receive those values. Desktop goals use Hermes Cua Driver on supported hosts. A proposed finish is not a verified task. [Browser and desktop limits](docs/DOM-BROWSER-BACKEND.md) include destination checks, receipts, and recovery conditions. F2 will apply **only** to the plugin-owned public DOM loop, after its implementation and evaluation gates pass.

## Toolsets and session exposure

Hermes exposes a plugin tool only when its toolset is selected. Switchyard registers its seven tools under two toolsets:

| Toolset | Tools |
| --- | --- |
| `computer_use` | `jev_computer_use` |
| `hermes_switchyard` | `jev_assess`, `jev_skill_select`, `jev_skill_select_many`, `jev_model_route`, `jev_model_route_approved`, `jev_session_search_rerank` |

An explicit `-t` pin replaces the default selection. Pinning only `computer_use` leaves out the six decision tools; pinning only `hermes_switchyard` leaves out computer use. A pin naming neither exposes none of the seven tools. To expose all seven in one CLI session:

```text
hermes -t computer_use,hermes_switchyard chat
```

`hermes switchyard status --json` evaluates the toolsets for a **fresh** session, not a running one. It reports registration and callability separately. See [session exposure](docs/SETUP.md#confirm-what-a-session-exposes).

## Configuration

All current settings live under `plugins.entries.hermes-switchyard.settings`. The defaults below come from this branch's `plugin.yaml`. An empty string or list means that no value is configured.

| Key | Default | Meaning |
| --- | --- | --- |
| `jev_provider` | `auto` | Prefer TypeSafe when its profile key exists; otherwise use OpenRouter. |
| `api_endpoint` | `https://openrouter.ai/api/alpha/decisions` | Fixed route. TypeSafe also uses its fixed endpoint; an arbitrary endpoint is refused. |
| `jev_model` | `""` | Use the selected provider's default Jev alias. |
| `browser_executable` | `""` | Discover a Chromium-family browser unless you set its absolute path. |
| `computer_max_steps` | `100` | Maximum computer-use actions before a smaller per-call limit. |
| `approved_model_registry` | `[]` | Profile-approved model records for advisory routing. An empty registry supplies no approved candidate. |
| `approved_model_registry_version` | `""` | Required operator version for that registry. |
| `approved_model_registry_valid_until` | `""` | Registry expiry as a timezone-aware ISO-8601 value. |
| `automatic_skill_recommendation` | `true` | Enable automatic local skill matching in `pre_llm_call`. |
| `automatic_skill_consumer_mode` | `load` | Load one accepted skill; `advisory` does not load and skips hosted automatic routing. |
| `automatic_skill_candidates` | `[]` | Empty uses the active profile's full skill registry; otherwise supply explicit candidates. |
| `automatic_skill_local_threshold` | `0.2` | Minimum local token-overlap score; uncalibrated. |
| `automatic_skill_local_margin` | `0.05` | Minimum gap between two local candidate scores. |
| `automatic_skill_cache_seconds` | `30.0` | Lifetime of the in-process recommendation cache. |
| `automatic_skill_deadline_seconds` | `20.0` | End-to-end deadline for hosted automatic routing. |
| `automatic_skill_routing_mode` | `hosted_sanitized` | Use `off`, `local_only`, or eligible hosted routing. |
| `automatic_skill_jev` | `true` | Deprecated compatibility setting; does not authorize hosted egress alone. |
| `automatic_skill_jev_mode` | `always` | Try hosted routing on eligible turns; `uncertain_only` opts in to a local-first latency policy. |
| `automatic_skill_public_or_sanitized_data_ack` | `true` | Standing acknowledgement for hosted automatic skill routing; not a data classifier. |
| `automatic_skill_mandatory_skills` | `[]` | Exact skill IDs that must not be displaced by an automatic load. |
| `automatic_skill_two_stage` | `true` | Use the bounded two-stage hosted selector. |
| `automatic_skill_platforms` | `[]` | Empty permits eligible interactive turns; an explicit list changes platform scope. |
| `automatic_skill_hosted_detail` | `names` | Send names by default; `descriptions` or `excerpt` opts in more text for finalists. |
| `automatic_skill_recheck_top_k` | `3` | Maximum finalists for the second-stage check. |
| `automatic_skill_early_stop` | `true` | Stop the hosted catalog search after a low needs-skill signal. |
| `automatic_skill_early_stop_threshold` | `0.3` | Uncalibrated needs-skill threshold. |
| `automatic_skill_stage1_single_round` | `true` | Run stage-one catalog partitions in one parallel round; `false` probes the first partition before fan-out. |
| `automatic_skill_stage1_min_probability` | `0.05` | Minimum stage-one candidate probability for recheck. |
| `automatic_skill_parallel_requests` | `4` | Maximum parallel stage-one requests within the shared budget. |
| `adaptive_reasoning_effort` | `true` | Allow Jev to lower effort; set `false` to stop this message-text path. |
| `adaptive_reasoning_effort_mode` | `auto` | `auto` can lower effort; `pinned` keeps your level. |
| `adaptive_reasoning_effort_exclude_models` | `[]` | Model patterns that the effort adapter does not touch. |
| `adaptive_reasoning_effort_allow_raise` | `false` | Permit one level above your cap after a failed tool call only when enabled. |
| `adaptive_reasoning_effort_default` | `medium` | Deprecated and unused; the request's own effort is the fallback. |
| `adaptive_reasoning_effort_deadline_seconds` | `1.5` | Wall-clock budget for one adaptive Jev decision; timeout keeps your level. |
| `session_search_rerank_choice_confidence_threshold` | `0.8` | Minimum Jev Choice confidence to change FTS order. |
| `session_search_rerank_winning_probability_threshold` | `0.8` | Minimum winning probability to change FTS order. |
| `session_search_rerank_max_card_chars` | `360` | Maximum text in a redacted FTS candidate card. |
| `public_or_sanitized_data_ack` | `true` | Standing acknowledgement for explicit Jev tools and adaptive effort; callers can refuse one call. |

**Not current settings:** F1 proposes `research_navigator_enabled=false`, `research_navigator_deadline_seconds=6.0`, `research_support_threshold=0.85`, and `research_contradiction_threshold=0.85`. F2 proposes `browser_progress_mode=off`, `browser_progress_stall_count=2`, `browser_progress_confidence_threshold=0.85`, and `browser_progress_new_evidence_no_threshold=0.15`. Do not set these keys until the corresponding implementation and manifest changes land. Both features must stay off if their predeclared evaluation gates fail.

## Troubleshooting

These are code-owned status or receipt reasons. They identify a gate, not necessarily a defect:

| Reason | What it means | Next action |
| --- | --- | --- |
| `tools_not_registered` / `not_registered` | Hermes did not register this plugin's tool. | Enable the plugin and start a fresh session. |
| `tools_not_callable` / `toolset_not_selected` | Registration worked, but the session did not select the toolset. | Select `computer_use` and `hermes_switchyard`; inspect `agent.disabled_toolsets`. |
| `credential_required` | No key exists for the effective TypeSafe or OpenRouter route. | Use the masked `switchyard setup` prompt for that provider. |
| `ack_required` | The required public/sanitized acknowledgement is off. | Classify the input first; only then set the relevant acknowledgement. |
| `consumer_contract_unmet` | Automatic routing is advisory, so it will not pay for a hosted decision. | Use `load` only if automatic skill loading is intended. |
| `local_scan_restricted_data` / `local_scan_restricted_marking` | A local scan kept a marked or restricted turn off the hosted path. | Keep this input local. Do not override the gate with a different spelling. |
| `per_turn_policy_denied` / `per_turn_policy_invalid` | A host envelope denied hosting or did not match the typed contract. | Inspect the host policy. Keep this turn local. |
| `client_unavailable` / `provider_request_failed` | No usable Jev client exists or the provider request failed. | Inspect key, route, allowance, and typed receipt; do not assume fallback. |

The installer can also report `Security scan blocked plugin install`. This occurs before any plugin status command is available. Review the exact scanner findings and source; do not pass `--force` merely to make the quickstart appear complete. [Setup](docs/SETUP.md) covers browser startup, explicit pins, and further status reasons.

## Uninstall and rollback

Disable the plugin first if you need to stop its hooks without deleting the installed source:

```text
hermes plugins disable hermes-switchyard
```

Start a fresh session. For a Git install, `hermes plugins update hermes-switchyard` changes the installed source and is **not** a rollback. To return to a previously recorded version, reinstall that reviewed immutable Git SHA under your normal security approval path. Remove the plugin only after you no longer need its local plugin data:

```text
hermes plugins remove hermes-switchyard
```

Keep or export receipts you need before removal; check the active profile's `plugin-data/hermes-switchyard` separately. Do not remove a shared profile or gateway to uninstall this plugin. Read [setup](docs/SETUP.md), [release process](docs/RELEASE.md), [changelog](CHANGELOG.md), [security reporting](SECURITY.md), and [third-party references](THIRD_PARTY.md).
