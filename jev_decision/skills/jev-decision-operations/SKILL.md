---
name: jev-decision-operations
description: Use when evaluating bounded Jev decisions or integrating this plugin safely.
version: 0.3.2
author: bgrablin
license: MIT
platforms: [linux, windows]
metadata:
  hermes:
    tags: [Jev, Computer-Use, Routing, OpenRouter]
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

- Decisions endpoint: `https://openrouter.ai/api/alpha/decisions` only.
- Requested model aliases are exactly `typesafe/jev-1.13` and
  `typesafe/jev-1.13-20260917`; no regex family authorization is used.
- A response outside those two aliases is rejected. The base alias may resolve
  to the one evidence-backed concrete alias; a concrete request must resolve
  exactly to itself.
- Provider fallback is explicitly disabled and HTTP redirects are rejected so
  the bearer token cannot be forwarded to another host.
- API keys never appear in model-facing or tool error text.
- No guardrail-management or arbitrary credential-bearing endpoint is part of
  this plugin's contract.

## Skill selection

`jev_skill_select` is advisory only. Candidate identifiers are exact: leading or
trailing whitespace is rejected and no identifier normalization is performed.
The candidate set is an explicit closed set of at most 255 entries. That limit
is an API constraint, not a claim that Jev can safely select from a whole
catalog.

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
acknowledgement. It preserves `ctx.dispatch_tool("computer_use", args)` and the
existing Hermes approval/action surface.

- Hotkeys default to an empty allowlist. A caller must explicitly list semantic
  names such as `SAVE` or `SELECT_ALL`; raw model-generated key strings are not
  accepted.
- Before every action or wait, the plugin takes a fresh AX capture. It compares
  exact exposed app/window identity and selected-control identity. The local
  comparison retains the full exposed label, role, index, app, and bounds; that
  raw identity is never sent to Jev. A changed exposed target is refused before
  action dispatch. Hermes core capture JSON does not expose pid/window_id, so
  this pilot does not claim unique native-window identity or prevent every race.
- For TYPE_TEXT/SET_VALUE, a second fresh capture occurs after text generation
  and immediately before the side effect.
- A failed key dispatch is not retried in foreground and is not replaced by an
  automatic menu fallback.
- Jev `DONE` returns `status: completion_candidate` and `verified: false`.
  Independent completion verification remains coordinator-owned. No result from
  this plugin is independent proof of task completion.

The safe-control filter still excludes credential, payment, verification,
destructive, permission, logout, close, and secret-like controls. It is a
candidate filter, not a DLP boundary.

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
