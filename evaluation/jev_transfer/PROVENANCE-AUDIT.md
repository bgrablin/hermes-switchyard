# Provenance audit

This records an automated audit of the retained trial artifacts on 2026-10-01. It closes the two original-source verification questions raised during review and checks the published data against the original local outputs.

## Original run outputs

Every published row and freeze was compared with its original file using canonical JSON, preserving row order and all field values.

| Run | Provider observation rows | Freeze | Runtime inventories |
| --- | ---: | --- | ---: |
| Decision screen | 144 | Matched | Not applicable |
| Decision confirmation | 144 | Matched | Not applicable |
| Native routing | 96 | Matched | 2 matched |
| Native consolidation, complete repeat | 96 | Matched | 2 matched |
| Native consolidation, interrupted | 79 | Matched | 1 matched |
| Total | 559 | 5 matched | 5 matched |

For each runtime inventory, the original path/hash mapping reproduced the published canonical SHA-256 and tracked-file count. The interrupted run still has no after-inventory and remains excluded from the complete-trial gate.

The original output directories are results-screen-v1, results-confirmation-v1, results-native-v1 and results-native-v2 under evaluation/turn_consolidation, plus results-v1 under evaluation/model_routing. These local outputs remain ignored by Git. The published observations are bound by provenance.json.

## Historical source originals

The two originals were recovered from commit 8e848adb6ac3a0492d5f6d3efe64ce49c469c876 in this PR's history. Each recovered file matched its recorded original SHA-256 in source-snapshots.json. Comparing parsed string constants and then replacing the single differing runtime-root literal reproduced the public file byte for byte; no other source changes were present.

| Original source | Retained Git blob |
| --- | --- |
| evaluation/model_routing/compare.py | 99524cf061e8859c9776f4b438351035a1aa8127 |
| evaluation/turn_consolidation/native_compare.py | 62b7413f78436e19a339dc5d6cc4c3f6510b5a79 |

Verify the original byte hashes without printing the source:

```sh
# Fetch the PR history if these objects are absent from a shallow or main-only clone.
git fetch origin refs/pull/188/head
git cat-file blob 99524cf061e8859c9776f4b438351035a1aa8127 | sha256sum
git cat-file blob 62b7413f78436e19a339dc5d6cc4c3f6510b5a79 | sha256sum
```

The expected hashes are the recorded_sha256 fields in source-snapshots.json. The ordinary replay independently verifies the current portable copies, their export bindings, observation completeness, decision qualification, pinned models, actual wire dispatches, and derived results.

## Scope of the conclusion

The retained artifacts are internally consistent and support preserving these synthetic feasibility results and evaluation harnesses. This audit is automated; it is not a human review or independent provider attestation.

The historical runtime inventories establish unchanged hashes for the configured source tree. Binding that tree to loaded module origins is available only in the maintained runners and was verified separately with matching-root and mismatched-root native startup checks. The first decision screen's failed gate, reused fixtures, interrupted run, and consolidation's changed effort choices remain disclosed in README.md.

Production qualification remains separate. The pilot adapters are not registered by Switchyard, and this PR does not enable either feature.
