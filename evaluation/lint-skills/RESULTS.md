# Offline skill-routability lint: synthetic evaluation result

The frozen fixture is `fixture.json` at SHA-256 `1594daa52d22cc188d8f100ed0a1ff21dc0c3060a30122b39f64974469083d57`. The evaluation contract is `PLAN.md` at SHA-256 `99db874094409c53a4d94a2c7bac742c5c406077e20560aa1e75d98ca580ee7a`. Both were committed before implementation at `06f957f440459e56408a4f826c0755dfd77216f3`.

## Pair-level result

| Measure | Observed | Gate |
| --- | ---: | ---: |
| True positives | 3 | 3 planted pairs |
| False positives | 0 | 0 on distinct set |
| False negatives | 0 | 0 |
| Precision | 3 / (3 + 0) = 100% | ≥90% |
| Recall | 3 / (3 + 0) = 100% | ≥80% |
| Distinct-set false positives | 0 | 0 |
| Exact-description baseline true positives | 1 / 3 | Candidate must exceed it |

The three reported pairs comprise two `near_duplicate` pairs and one `confusable` pair. The candidate found two planted collisions beyond exact-description matching without a planted distinct false positive. The fixture also produced one short-description hint, one long-description hint, one missing-trigger hint, and one invalid-row count. JSON parsed and round-tripped, output ordering was stable when input order reversed, and no description/body/supporting-file canary appeared in either CLI format. The generated 1025-entry catalog returned `catalog_too_large` without a partial report; an 8 MiB serialized response was also refused. A dense 100-entry catalog returned `catalog_too_confusable` instead of exporting more than 4096 pairs.

**Gate:** synthetic fixture PASS. The active private catalog was not scanned for this evaluation, and the lint path made no provider call. The frozen fixture is small; it does not establish general precision, an agent-behavior improvement, or production routing benefit. A release-harness SHIP verdict is **not available** unless the separate harness merges and passes its own checks.

Commands used for the local synthetic and scoped checks: `python3 -m unittest discover -s tests -p test_skill_lint.py -v`, `python3 -m unittest discover -s tests -p test_release.py -q`, and `uvx --offline --from ruff==0.11.13 ruff check hermes_switchyard/skill_lint.py hermes_switchyard/__init__.py tests/test_skill_lint.py`.
