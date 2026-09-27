# Research Navigator evaluation (F1, policy `research-v1`)

## Files

| File | Role |
| --- | --- |
| `fixtures.json` | 8 development and 32 heldout cases. Author-written synthetic and public-fact windows. Labels were written before any provider-backed run. |
| `fixtures.lock.json` | SHA-256 of `fixtures.json`, frozen before any provider-backed run. `evaluate.py` refuses to run if the hash changed. |
| `evaluate.py` | Runs arms A, B, and C through the real registered handlers. |

Window text is not a fetched copy of the cited URL. The scripted Jev answers in each case are labelled correct answers for the plumbing gate. They are not observed Jev output.

## Arms

- **A, disabled:** the real `jev_research_navigator` handler with `research_navigator_enabled=false`. It returns the original windows and makes no claim.
- **B, v0.5.4 behavior:** the real `jev_assess` handler from the `v0.5.4` tag source (loaded with `git archive`). It sends every claim/window pair as raw support and contradiction Noul questions, with no quote check, stale check, or local egress gate. The evaluator classifies its answers with the same `research-v1` thresholds. This is the raw agent workflow, not an existing Navigator.
- **C, candidate:** the real `jev_research_navigator` handler from this tree.

Each arm runs in its own Python process.

## Run

```text
python evaluation/research_navigator/evaluate.py --validate --output report.json
```

Offline mode uses a fake `DecisionClient` and makes no network call. It needs the pinned Hermes tree on `PYTHONPATH` so the Hermes egress redactor loads.

Live mode runs only the 6 cases marked `live`, one Jev request each, on the plugin's configured route:

```text
python evaluation/research_navigator/evaluate.py --live --live-arms C --output live.json
```

## Predeclared pass thresholds (from the design)

Offline plumbing gate, with labelled correct Jev answers:

- zero false `supported` or `contradicted` assertions on missing quotes, stale sources, denied inputs, and model failure;
- zero wrong original-text or window mappings;
- at least 28 of 32 heldout cases with the correct claim class;
- at most one physical Jev request per case.

Live set: no false supported claim, at least 5 of 6 class matches, and no source-coverage regression against B. Latency p50/p95 at most B + 15% and B + 300 ms. Known cost at most 1.15 × B. A null cost cannot pass a cost claim.

## Offline result

Fixture SHA-256 `56f5362dfede6c92511bbfa270c563fdf3464e2ce0f1a0de2f894a679933eddf`. Baseline `v0.5.4` at `5e2b878`.

| Arm | Split | Class correct | False assertions | Wrong mappings | Max requests per case |
| --- | --- | --- | --- | --- | --- |
| A | dev | 5 / 8 | 0 | 0 | 0 |
| A | heldout | 20 / 32 | 0 | 0 | 0 |
| B | dev | 6 / 8 | 2 (`dev-05`, `dev-08`) | 0 | 1 |
| B | heldout | 25 / 32 | 7 (`ho-13`, `ho-14`, `ho-25`, `ho-26`, `ho-28`, `ho-31`, `ho-32`) | 0 | 1 |
| C | dev | 8 / 8 | 0 | 0 | 1 |
| C | heldout | 31 / 32 | 0 | 0 | 1 |

**Offline gate: PASS** (all four checks).

The one C miss is `ho-24`. The window says "Tool Q is proprietary software". The shared restricted-marking rule treats "proprietary" as a document marking, so C refused the case locally (`restricted_marking`) and returned `unresolved` instead of the labelled `contradicted`. This is a safe miss (no false assertion). The label was not changed after the run.

B false assertions come from missing local checks: B accepts support when the exact quote is absent (`dev-05`, `ho-13`, `ho-14`), assesses stale windows (`ho-25`, `ho-26`), accepts an extra answer key (`ho-28`; the fake client bypasses `DecisionClient` response validation in both arms, so this B result may not occur with the real client), and sends marked or private-URL text (`dev-08`, `ho-31`, `ho-32`).

Offline latency is local handler time with a fake client (C p50 about 0.4 ms). It is not a provider latency claim.
