# Issue 9 — Privacy-Safe Typed Routing Receipts (Evidence Handoff)

## Summary
Implemented `bgrablin/hermes-switchyard#9`: a supported, redacted, operator-visible routing-receipt
surface for the Jev automatic-skill-recommender. The receipt maps every automatic-routing terminal
state onto stable typed fields without exposing task text, candidate descriptions, conversation
history, credentials, or provider exception text. It is advisory (`verified=false`) and never implies
a skill was loaded or a GUI action completed.

## Exact SHAs
- Base commit: `c6d9b286566cf36696e746d64600c041cf40b838`
- Work branch: `issue-9-routing-receipt` (detached base `c6d9b28`)

## Changed Files
- `jev_decision/automatic.py` — receipt surface (additive; no behavior change to recommendation logic)
- `tests/test_routing_receipts.py` — new receipt test module (9 tests)
- `docs/receipts.md` (new) — schema + safety contract

## Receipt Schema (fields exposed)
- `terminal_state`: one of `local_selection`, `hosted_selection`, `hosted_abstention`,
  `hosted_failure_local_fallback`, `hosted_skipped`, `cache_hit`
- `source`: `local` | `jev` | `none`
- `selected`: skill identifier when a selection occurred (sanitized)
- `hosted_attempted`: bool
- `hosted_succeeded`: bool (attempted and no stable error)
- `hosted_error`: stable local code only — `transport_or_execution_failure`, `ack_required`,
  `validation_failure`, `typed_response_failure`, `request_budget_exhausted`, `plugin_error`
- `hosted_skip_reason`: stable skip reason (ack required, disabled, etc.)
- `abstention_reason`: stable reason
- `jev_model`: resolved Jev model slug (sanitized)
- `request_count`, `latency_ms`, `total_latency_ms`, `total_usage`
- `candidate_count`, `offered_count`, `excluded_count`, `shortlist_policy`
- `verified`: always `false`
- `advisory_only`: always `true`
- `plugin_identity`: `{"plugin": "jev-decision", "version": "0.4.0"}`

## Privacy Guarantees
- No `task`, `candidates_considered` detail, conversation history, or provider exception text
  reaches the receipt. Intermediate string fields go through `build_routing_receipt`'s forbidden
  marker strip; any value containing a task/candidate/credential/exception marker is emptied.
- Hosted failures reduce to a stable local error code; no arbitrary provider, executor, local path,
  username, or hostname text is retained.
- No telemetry transmission. Receipts are local operator evidence only.

## Retention & State Surface
- Exactly one terminal receipt is retained per recommendation attempt via
  `AutomaticSkillRecommender.last_receipt` (cached and non-cached return paths both set it).
- The `pre_llm_call` hook object also carries `last_receipt` (stable plugin state surface). Nothing
  is injected into the system prompt, preserving prompt-cache invariants.

## Regression Test Proof
1. New test module `tests/test_routing_receipts.py` (9 tests) was added first.
2. On pristine base, `python3 -m unittest tests.test_routing_receipts` FAILED with
   `ImportError: cannot import name 'build_routing_receipt'` — the receipt surface is absent.
3. After the fix, the same suite passed (9/9), and the full suite went from 80 → 89 tests.

## Required Verification Gates (real results)
- `python3 -m unittest discover -s tests -v` → Ran 89 tests (88 pass, 6 skipped).
  The single ERROR is pre-existing on the base and unrelated to this change:
  `test_cli_setup_uses_masked_prompt_and_profile_secret_writer` fails because the local
  standalone interpreter is Python 3.14, where the `hermes_cli` package is not importable.
- `python3 evaluation/evaluate.py --validate` → status `ok`, `policy_pass: True`,
  `semantic pass: True`, all validation probes pass.
- `python3 scripts/check_portability.py` → passed.
- `python3 scripts/check_portability.py --history` → passed.
- `python3 -m compileall -q jev_decision evaluation tests scripts` → OK.
- `hermes plugins doctor . --ci` (native, pinned `8503ee44...`): manifest parse, import, and
  registration passed; 4 tools + 1 hook.
- Native compatibility check against pinned Hermes SHA `8503ee4459316ce092b5d69b7d396c27aa03d0be`:
  `check_native_hermes.py` → `ok: True`, `registered_tools` = `{jev_assess, jev_computer_use,
  jev_model_route, jev_skill_select}`, `hermes_python: >=3.11,<3.14`, `python: 3.11.16`.
  No new core tool was added, so the native tool-set contract is preserved.
- `git diff --check` → OK (no trailing whitespace / tab errors).

## Signing
- Committed as `bgrablin <5216789+bgrablin@users.noreply.github.com>` (author + committer + signer).
- Local git signing key: `id_ed25519_github_signing`
- Expected signing-fingerprint for verification: `LZ2/Le7EloJXDpeMH6c3T7FhJVIXPApc9suWa/xX54E`

## Remaining Limitations
- Receipts are populated on `AutomaticSkillRecommender` and the `pre_llm_call` hook object; no
  separate CLI subcommand exposes them. Adding a read-only `hermes jev-decision receipt` command
  is a natural, low-footprint follow-up if operators want a surfaced entry point.
- Live Jev contract was not exercised (offline-only per task constraints).

## Not Done (intentionally)
- Did not push, open/close/modify a PR, comment on GitHub, or merge.
- Did not access secrets or make live Jev calls.
- Did not use any paid/fallback route.
