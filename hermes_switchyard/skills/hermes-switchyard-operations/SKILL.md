---
name: hermes-switchyard-operations
description: Use when operating or diagnosing Switchyard.
version: 0.5.6
author: bgrablin
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Jev, Computer-Use, Cua-Driver, Routing, TypeSafe, OpenRouter]
---

# Hermes Switchyard Operations

Switchyard adds automatic skill loading and adaptive reasoning effort to Hermes,
plus explicit Jev decision tools. Use this guide to operate those features, not
to grant execution, data-sharing, model-switching, or spending authority.

## When to use

- Diagnose skill selection, effort changes, receipts, or missing Switchyard tools.
- Choose between automatic behavior, a session-only pin, and a privacy opt-out.
- Review a Switchyard approval prompt or use its bounded decision/computer tools.
- Do not use a Jev score as proof of safety, factual correctness, or task completion.

## Start with local evidence

Use a fresh Hermes process after installation or persistent settings changes.
Find this bundled skill with `skills_list`, then pass the exact returned identifier
to `skill_view`; do not substitute an old user-local copy. Plugin Doctor establishes
discovery/import/registration, not tool availability in a particular session.

```python
terminal(command="hermes switchyard status --json")
terminal(command="hermes switchyard status --toolsets computer_use,hermes_switchyard --json")
terminal(command="hermes switchyard guide")
```

Compare `plugin_version`, `plugin_loaded`, `effective_provider`, credential-presence
booleans, and `tool_exposure`. The decision tools require `hermes_switchyard`;
`jev_computer_use` requires `computer_use`. A registered tool can still be hidden
by session toolset selection or disabled toolsets. Do not silently change them.
These commands make no Jev request. `status --browser` is different: it launches
a diagnostic browser, so do not add it to a read-only check without that scope.

## Automatic versus explicit behavior

| Surface | Default and boundary |
| --- | --- |
| Automatic skill routing | Enabled, `hosted_sanitized`, `load`. On eligible turns, may load one exact identifier through Hermes' normal loader. Explicit skill instructions, mandatory-skill conflicts, abstention, and loader rejection prevent an inappropriate load. |
| Adaptive effort | Enabled, `auto`, when the host exposes the required request middleware. May lower the host-requested effort; it does not select another model/provider. |
| Decision and computer tools | Run only when called. A selection or route recommendation does not apply itself. Computer-use calls can perform actions. |
| Extra screening, approval, compaction, and cache features | Opt-in; do not assume they are active because their code is installed. |

Automatic routing does not rewrite the cached system prompt or replace mandatory
skills. Light turns can skip Jev. The two-stage selector can shortlist a large
catalog; inspect its receipt rather than assuming every candidate reached the
provider. A valid hosted abstention is final for that turn. A hosted failure may
retain a local match, which is **local fallback**, not another provider call.

## Reasoning: a cap is not a pin

In `auto`, `/reasoning` sets the ceiling after Hermes' per-model clamp. Changing
it does not turn auto mode off. `/reasoning max` alone therefore does **not**
guarantee max on every request. For fixed effort in the current session, use the
native session commands in this order and verify each acknowledgment:

```text
/switchyard effort pin
/reasoning max
/switchyard effort status
```

Choose a level the current host/model supports; `max` is the example, not a new
provider entitlement. Pin prevents Switchyard lowering the host's selected level;
it does not bypass host clamps, create a missing effort field, or prove backend
acceptance. To resume adaptation, use `/switchyard effort auto`.

| Native session command | Effect |
| --- | --- |
| `/switchyard effort status` | Current session cap, last sent level, mode, reason, recent decisions, and summary. |
| `/switchyard effort summary` | In-memory session counters, with no Jev request. |
| `/switchyard effort pin` | Session-only pin, including before the first message. |
| `/switchyard effort auto` | Session-only auto mode using the current level as cap. |
| `/switchyard effort receipt auto` | Show decision/route-side receipts; the default. **Saves a persistent plugin setting.** |
| `/switchyard effort receipt always` | Also show pinned/pass-through receipts when a sent level is known; persistent. |
| `/switchyard effort receipt off` | Hide the line; persistent, and not a privacy opt-out. |

These are slash commands, not `hermes switchyard effort` CLI subcommands. Type the
full command; do not promise nested argument autocomplete. Read native `/status`
for the session's model/provider and `/reasoning` for its chosen level. The TUI
status chip is not proof of the level Switchyard sent.

`Reasoning: high→low` reports an adjustment. `local decision` means no Jev call;
`shape only` means metadata, not message text, went to Jev. `not adapted` means
pass-through, not a successful downgrade. If the host already supplied `low`,
do not blame Switchyard without a cap-to-sent receipt. Missing evidence stays
unknown. Token savings are estimates from measured baselines, not guaranteed
savings or evidence of equal answer quality.

On timeout, invalid output, missing acknowledgment, or an unsupported route,
effort stays at the host-requested value. The default never raises it. The
explicit `adaptive_reasoning_effort_allow_raise` opt-in permits one level above
the cap after a failed tool call. See the [effort guide](../../../docs/ADAPTIVE-REASONING-EFFORT.md).

## Privacy, providers, and failure handling

Jev requests can be billed separately from the conversation model. `jev_provider`
`auto` selects TypeSafe when its profile key exists, otherwise OpenRouter;
explicit `typesafe` or `openrouter` selects that route. This is initial selection,
not failover permission. The client accepts only the fixed provider endpoints,
rejects redirects, and disables OpenRouter provider fallback. Never change
provider, model, keys, or billing route to hide a failure.

`public_or_sanitized_data_ack` defaults true for explicit tools and effort.
Omitting it uses the standing setting; passing `false` refuses that tool call.
It is not blanket egress authority. Send no secrets, payment/verification data,
or unapproved private/employer material. Redaction is not DLP and cannot detect
all unmarked private text.

- **Effort:** may send a bounded, redacted excerpt of the current user message,
  plus status flags. History, memory, skill bodies, and tool arguments/results
  are not its input. Without a usable redactor it may send shape-only metadata.
- **Automatic skills:** separate acknowledgment and routing settings. By default
  sends bounded approved text and exact candidate names, not descriptions.
  An explicit host allow envelope supplies the approved text; otherwise standing
  acknowledgment requires a clean local scan. Deny/unknown/malformed policies
  block hosting. Optional hosted descriptions/excerpts need their own review.
- **Explicit tools:** the caller supplies their state/cards/candidates. Minimize
  and sanitize these inputs; do not assume the automatic-routing scan covers them.

For an **authorized persistent privacy change**, settings are under
`plugins.entries.hermes-switchyard.settings`. These examples keep skill matching
local and stop effort adaptation, respectively:

```python
terminal(command="hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_routing_mode local_only")
terminal(command="hermes config set plugins.entries.hermes-switchyard.settings.adaptive_reasoning_effort false")
```

Use `automatic_skill_consumer_mode: advisory` for suggestions without auto-loading;
it also suppresses hosted skill routing. `automatic_skill_recommendation: false`
disables that feature. `public_or_sanitized_data_ack: false` stops explicit Jev
tools and adaptive effort, **not** automatic skills; those use
`automatic_skill_public_or_sanitized_data_ack`. Read back the setting and use a
fresh session. Do not restart an active gateway without authorization. Full
options: [configuration](../../../docs/CONFIGURATION.md).

## Read routing receipts before retrying

```python
terminal(command="hermes switchyard receipt --human")
terminal(command="hermes switchyard receipt --last 5 --human")
terminal(command="hermes switchyard receipt --last 5 --json")
terminal(command="hermes switchyard stats --since 24h --json")
terminal(command="hermes switchyard wow --days 7 --json")
```

Routing receipts and effort receipts describe different operations. Use
`receipt --session SESSION_ID --last 5 --json` with the actual session identifier
when correlating routing history. Distinguish cache reuse, local selection,
hosted selection, hosted abstention, hosted failure/local fallback, and hosting
skipped. `selected` does not alone prove that the skill loaded or helped.
`wow` summarizes retained local observations, not a benchmark or task-success
certificate. A missing receipt is unavailable evidence, not success or failure.

On a refusal or failure, inspect its bounded reason, input scope, route, and
tool exposure. Correct only the proven issue within authority. Do not loop on
live requests or turn a local check into `test --live`; that command requires
explicit billed-test and public/sanitized-data consent.

## Explicit tools

Load the current schema with `tool_describe` when deferred. Use exact identifiers
from discovery; do not trim or invent candidate IDs. These are the callable tools:

| Tool | Use and limitation |
| --- | --- |
| `jev_assess` | Typed Choice, Score, or Noul questions over caller-provided `state` and `questions`. Valid types are not factual verification. |
| `jev_skill_select` | Choose one of `candidates` for `task`, or abstain. The caller decides whether to `skill_view` the exact selected name. |
| `jev_skill_select_many` | Recommend multiple skills using the threshold and optional `max_selections`; it does not load them. |
| `jev_model_route` | Filter explicit approved candidates by required data classes, capabilities, context, budget, and cost; choose the cheapest qualified candidate with input-order ties. Advisory only. |
| `jev_model_route_approved` | Use the profile's approved, versioned, unexpired registry. Missing/stale/ineligible registry abstains; no automatic model switch. |
| `jev_session_search_rerank` | First call native `session_search`, then send compact ordered cards plus `query`. Unavailable/low-confidence Jev keeps the FTS fallback; optional screening can withhold cards. Do not send full transcripts. |
| `jev_computer_use` | Bounded action loop with required `goal` and nonempty `app`; verify the outcome independently. |

For example, after confirming an exact discovered skill, call
`jev_skill_select` with `task: "Diagnose a Docker container"` and
`candidates: [{"name": "docker-management", "description": "Manage Docker containers"}]`.
This is a hosted decision, not a local diagnostic; use it only if that skill exists
and the task/data/cost are authorized. Choice confidence is concentration, and
Noul/fit values are intended probabilities with no independent correctness
calibration. A threshold of 0.8 does not mean 80% correct or safe.

For authorized multi-step GUI work, prefer `jev_computer_use`; respect a user's
explicit native-tool preference. Public HTTPS goals use the DOM browser loop,
not Hermes `computer_use` between clicks. Desktop apps without a URL use Cua
Driver. The desktop loop rechecks exposed app/window/control identity before
actions; hotkeys require an explicit semantic allowlist. Sensitive targets stay
excluded. In the DOM browser loop, `DONE` returns `completion_candidate`; the
receipt has `verified: true` only when the provider decided `DONE` and the fixed
local completion condition is satisfied. Desktop `DONE` always returns
`verified: false`. A blocked, stale, or incomplete run needs inspection, not
blind continuation. Read back the actual page/app target yourself.

## Approval prompts and opt-in features

Keep native approval controls in charge. `consequential_tool_gate` adds a request
for human approval on code-defined indicators; its `approve` hook directive means
**ask the human**, not permission to execute. `smart_approval_provider` is separate:
it must also be selected as native `auxiliary.approval` provider
`switchyard-approvals`. It remains experimental and can escalate safe commands.

For a suspected false positive, record the exact bounded command, intended
effects, plugin/host versions, enabled gate/provider, and returned reason without
secrets. Inspect any referenced script locally. Distinguish a lexical indicator,
missing context, unavailable reviewer, and an actual dangerous effect. A displayed
flag or score is review evidence, not proof the command is malicious or safe.
The current gate can match words inside quoted data; do not claim a parser fix
from an unmerged change. Seek native human disposition when required. Do not
rewrite, encode, or split the same action to evade the prompt, auto-approve it,
or disable approvals as a workaround.

All of these settings default **off** and need deliberate enablement:

| Setting | Limit |
| --- | --- |
| `retrieved_screen_enabled` | Bounded instruction/role-spoof indicators; search is shadow-only by default. Not a trust verdict or full injection defense. |
| `consequential_tool_gate` / `smart_approval_provider` | Separate opt-ins; neither replaces native authorization. Failed host hooks can be ignored by Hermes. |
| `repeated_output_compaction` | Consecutive duplicate lines in eligible successful terminal output only. Recognized full-output/verbatim requests preserve output. Leave off for byte-exact evidence. |
| `cross_tool_stuck_detection` | Bounded advisory check across tool failures; may send redacted error excerpts to Jev. Does not force retries or block tools. |
| `browser_plan_cache` | Memory-only reuse of matching public link decisions; fresh action checks remain. Never caches DONE or certifies success. |
| `evidence_finder_enabled` | Source-prefetch pilot requiring an approved root; not a callable search tool. |
| `defer_switchyard_tool_schemas` | Experimental reduction of decision-tool schemas; does not turn off automatic features. |

Local review commands are explicit, not auto-install or approval actions:

```python
terminal(command="hermes switchyard lint-skills --json")
terminal(command="hermes switchyard lint-skills --style --limit 10")
terminal(command="hermes switchyard scan-catalog ./candidate-package")
```

Lint is advisory by default; `--fail-on warning` or `--fail-on error` requests a
failure exit. Catalog scanning reports hashes, indicators, and coverage gaps,
never admission. Unsupported safe file primitives can prevent scanning. Details:
[feature boundaries](../../../docs/FEATURE-EXPANSION.md) and
[skill lint](../../../docs/LINT-SKILLS.md).

## Verification

Confirm the requested local setting/session mode or exact tool result, then verify
the actual outcome separately. Keep discovery, callable exposure, provider
response, skill loading, and completed user work distinct. Never infer release,
installation, safety, or quality from an offline test alone. Developer test and
benchmark procedures belong in [CONTRIBUTING](../../../CONTRIBUTING.md), not in
the normal user workflow.
