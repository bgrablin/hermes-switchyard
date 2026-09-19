---
name: jev-decision-operations
description: Use when evaluating bounded Jev decisions or integrating this plugin safely.
version: 0.4.1
author: bgrablin
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Jev, Computer-Use, Cua-Driver, Routing, TypeSafe, OpenRouter]
---

# Jev Decision Operations

Use Jev only as a bounded, advisory decision coprocessor. Jev returns typed
outputs, but a valid type does not establish truth. Choice confidence is
distribution concentration. Noul/fit values are intended yes/no probabilities;
calibration for correctness is not independently established. Do not make a
calibrated-quality claim or treat a 0.8 threshold as 80% correctness.

This plugin never loads a skill, edits a prompt, changes the Hermes runtime
model, or performs an automatic fallback. The coordinator owns those decisions.

## Data boundary

Every model-facing entrypoint requires `public_or_sanitized_data_ack: true`.
This is a caller attestation, not DLP or authorization. Send no private,
employer, regulated, credential, payment, or verification UI/data. Regex
redaction is not authorization. If the acknowledgement is absent or false, the
entrypoint must reject before network access or desktop capture.

## Fixed client contract

- Supported endpoints are the direct TypeSafe System One endpoint and OpenRouter's Decisions endpoint.
- `jev_provider: auto` prefers direct TypeSafe when its profile secret exists; explicit `typesafe` and `openrouter` routes are also supported.
- Model aliases are endpoint-specific and validated exactly. A direct `jev-latest` alias may resolve to a supported concrete release; pinned aliases must resolve exactly.
- OpenRouter provider fallback is explicitly disabled and HTTP redirects are rejected. Direct TypeSafe requests omit OpenRouter-only fields.
- The client keeps a pooled HTTPS connection for repeated decisions in one process.
- API keys never appear in model-facing or tool error text.
- `jev_assess` exposes Choice, Score, and Noul. Score answers are validated for ordered levels, legend, probability distribution, and confidence before returning.

## Skill selection

`jev_skill_select` is advisory only. Candidate identifiers are exact: leading or
trailing whitespace is rejected and no identifier normalization is performed.
The provider's per-Choice limit is handled internally: catalogs larger than 255
entries are evaluated through partition fan-out and recursive reduction. Every
candidate is considered; the tail is not silently discarded.

Selection abstains unless all three bounded local policy gates pass:

1. Choice confidence meets `choice_confidence_threshold`.
2. The intended yes/no `needs_skill` probability meets
   `needs_skill_threshold`.
3. The selected candidate's winning Choice distribution value meets
   `winning_probability_threshold`.

The defaults are conservative local policy thresholds. They do not establish
calibration or correctness probability. A returned `selected` value does not
load a skill; the caller must decide whether and how to load it.

## Model routing

The caller supplies explicit metadata. Code filters candidates before asking
Jev and never infers policy from `description`:

- `approved`: required boolean and must be true.
- `data_classes_allowed`: required when `requirements.data_classes` is used.
- `tool_capabilities`: required when `requirements.tool_capabilities` is used.
- `context_limit`: required when `requirements.context_limit` is used and must
  meet that minimum.
- `cost`: required for eligibility because code must prove the cheapest choice.
- `requirements.budget`: excludes candidates whose explicit cost is above it.

Jev supplies an intended yes/no capability-fit probability for each
deterministic eligible candidate. The local threshold is an uncalibrated policy
gate; code keeps candidates meeting it, then chooses the cheapest qualified
candidate with a stable input-order tie break. If no candidate is eligible or
qualified, the result is an explicit abstention and no alternate route is tried.
The result is advisory and does not change the runtime model.

## Computer-use loop

`jev_computer_use` requires a non-empty `app` and an explicit public/sanitized
acknowledgement. It delegates to Hermes' existing Cua Driver-backed
`computer_use` tool on Windows, macOS, and Linux. Switchyard does not vendor a
second desktop driver.

- Jev chooses from application-owned `CLICK`, `DOUBLE_CLICK`, `RIGHT_CLICK`,
  `MIDDLE_CLICK`, `DRAG`, four scroll directions, `TYPE_TEXT`, `SET_VALUE`,
  explicit navigation hotkeys, `WAIT`, `DONE`, and `BLOCKED` operations.
- Native roles include buttons, checkboxes, radio/toggle controls, links, tabs,
  menus, trees, lists, comboboxes, edits, sliders, spinners, calendars, and
  date controls. Sensitive, destructive, payment, credential, logout, and
  close-like labels remain excluded.
- Dense target sets are partitioned into bounded Choices with an explicit
  no-target option. The plugin can search beyond 255 controls without dropping
  the tail; the state summary is compacted separately so duplicated labels do
  not overflow Jev's context budget.
- Before every action or wait, the plugin takes a fresh AX capture and compares
  exact exposed app/window/control identity. Drag operations verify both source
  and destination. TYPE_TEXT/SET_VALUE takes another fresh capture after text
  generation and before mutation.
- Hotkeys default to an empty allowlist. A caller must explicitly list semantic
  names; raw model-generated key strings are not accepted.
- Jev `DONE` returns `status: completion_candidate` and `verified: false`.
  Independent completion verification remains coordinator-owned.

## Offline verification

Run from the repository root:

```text
python -m unittest discover -s tests -v
python evaluation/evaluate.py --validate
```

Tests use synthetic transports and dispatch fixtures only. They must not call a
live API, drive a real GUI, or treat source-text assertions as behavioral proof.

After installation, use a fresh Hermes process and run Plugin Doctor before
any live smoke test. Offline checks do not establish real GUI task completion;
verify the exact target outcome independently.
