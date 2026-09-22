# Adaptive reasoning effort

Codex-style per-turn reasoning effort for Hermes Switchyard 0.5.0.

## Behavior

1. On each Hermes LLM generation, Switchyard's `llm_request` middleware asks Jev
   for a typed Choice over Hermes-supported levels:
   `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`.
2. Context is bounded: task snippet, recent tool outcomes, prior effort.
3. Raise when tools fail / stuck; lower for routine work.
4. Apply by setting request-scoped `reasoning_effort` (and nested effort twins
   already present). **Messages are never rewritten** so Hermes prompt cache
   stays friendly.
5. Fail closed: on Jev failure, missing ack, or invalid choice, keep the previous
   effort (or the configured default).

## Default

**On after install** (`adaptive_reasoning_effort: true`), same standing Jev key
as other Switchyard features. Disable:

```text
hermes config set plugins.entries.hermes-switchyard.settings.adaptive_reasoning_effort false
```

## Hermes seam

Requires Hermes ≥ 0.21.4 `PluginContext.register_middleware("llm_request", ...)`
plus `hermes_cli.middleware.apply_llm_request_middleware`. Hosts without that
API record `noop_seam_unavailable` and do not mutate requests.

Model routing (`jev_model_route`) stays advisory (`applied: false`). Adaptive
effort is the apply-able win on current Hermes.

## Receipts

`hermes switchyard status --json` includes `reasoning_effort_adapter`. In-process
receipts expose `effort`, `reason_code`, and `applied`.
