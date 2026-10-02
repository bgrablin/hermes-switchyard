# Skill description lint (`lint-skills`)

**In short:** find missing task descriptions and ambiguous pairs without a wall of wording warnings. The default report puts errors and warnings first, explains the evidence and next action, and shows at most 20 findings. Wording and length preferences are opt-in. It runs offline, changes nothing, and is a hint, not a verdict. *New in the upcoming 0.6.0.*

`hermes switchyard lint-skills` checks whether the active Hermes skill descriptions are easy to distinguish. It uses the names and descriptions returned by Hermes' `skills_list()` registry. It does not open skill bodies or supporting files, call Jev or a network endpoint, edit skills, or change automatic routing. It is an advisory routability check, **not** Hermes' SKILL.md standards lint.

Run it after confirming that the active profile and project are the catalog you intend to inspect:

```text
hermes switchyard lint-skills
hermes switchyard lint-skills --limit 5
hermes switchyard lint-skills --all
hermes switchyard lint-skills --style
hermes switchyard lint-skills --json
hermes switchyard lint-skills --json --fail-on error
```

The report summarizes coverage and severity before listing findings. Each pair appears once in text, not once per peer plus a repeated cluster. `--limit N` requires a positive integer and limits text only; `--all` removes that display limit. Analysis, JSON, and exit-code decisions always use the complete bounded catalog. The footer gives the exact omitted finding count.

The default exit code is **0**, even when findings exist. For automation, use `--fail-on error` or `--fail-on warning`: a finding at or above the selected severity returns **2**. Optional style hints and lower-overlap suggestions never fail these gates. An unavailable or refused catalog returns **1** with a closed-set reason. Invalid CLI arguments return **2** before discovery. These thresholds do not turn lexical hints into a skill-validation standard.

## What it checks

| Finding | Severity | Rule and action |
| --- | --- | --- |
| `invalid_rows` | Error | Some rows cannot be checked. Read the aggregate reason counts and repair the registry input. |
| `empty_description` | Error | Empty or whitespace-only description. Add a concrete task and selection condition. |
| `low_information_description` | Warning | Fewer than two specific ASCII tokens after stopwords and a small generic-word list are removed. Add the task or target if unclear. This is a heuristic, not a semantic assessment. |
| `near_duplicate` | Warning | Jaccard overlap at least 0.75, with at least three shared tokens. Review scope and triggers; do not merge skills from this evidence alone. |
| `confusable` | Suggestion (`info`) | Overlap at least 0.50 but below 0.75, with at least three shared tokens. Shared vocabulary can be legitimate, especially for different tools. |
| `short_description` | Style suggestion | **Only with `--style`:** fewer than 40 characters. Concise, specific descriptions are acceptable. |
| `long_description` | Style suggestion | **Only with `--style`:** more than 200 characters. Prefer trigger-first wording. |
| `missing_use_when` | Style suggestion | **Only with `--style`:** no leading `Use when` (ASCII case-insensitive). Hermes does not require this literal prefix. |

Pair evidence includes numeric similarity, shared/union token counts, each side's unique-token count, and whether the full descriptions are equal after case folding and whitespace normalization. Similarity is **not confidence or probability**. Different word orders and negations can produce the same token set. No description text or extracted words are exported.

## JSON v2 and migration

JSON uses `schema: switchyard.lint_skills.v2`. Consumers of the initial v1 draft must update their schema check. Existing `counts`, name-ordered `pairs`, `clusters`, and per-skill `findings` remain, but pairs now carry severity and numeric evidence. Prefer `diagnostics` for presentation and automation:

- Each diagnostic has `code`, `severity`, `category`, `names`, `message`, `suggestion`, and `evidence`. Messages and suggestions are fixed application text.
- Diagnostics sort by severity, category, descending similarity where applicable, names, and code. Errors precede warnings; overlap suggestions precede optional style hints.
- `severity_counts` counts diagnostics, not skills. One pair is one diagnostic. Invalid rows produce one aggregate error, regardless of the number of skipped rows.
- `status` is `partial` when invalid rows were omitted; otherwise `ok`. `coverage_complete` means every registry row passed input validation, **not** that every skill body or routing behavior was checked.
- `comparison_complete` is false for invalid rows or descriptions with non-ASCII letters or combining marks omitted from lexical comparison. Empty and weak descriptions receive individual diagnostics instead of pair comparisons. `compared_skills` is the number eligible for pair comparisons, not the number of pairs.
- `invalid_reasons` counts each omitted row once, using the first applicable reason: `not_mapping`, `invalid_name`, `duplicate_name`, `non_string_description`, `description_too_large`, `too_many_tokens`.
- `style_enabled` records whether wording/length rules ran. Style counts are zero when disabled, not evidence of conformance.

Validated identifiers are the only skill-sourced strings exported. Descriptions, bodies, paths, extracted tokens, and registry error text are not exported. Numeric evidence contains lengths and overlap counts. A failed lookup or global size refusal returns only `schema`, `status: unavailable`, and `reason`, not a partial success.

## Limits

Token matching folds case, extracts ASCII letters and numbers, and removes the small stopword set frozen in `evaluation/lint-skills/PLAN.md`. Descriptions containing non-ASCII letters or Unicode combining marks are counted separately and omitted from comparison and the weak-description rule, rather than compared from fragments or mislabeled empty. This includes mixed-language and decomposed accented descriptions; non-ASCII punctuation alone does not prevent comparison. Empty and weak descriptions are excluded from pair comparisons to avoid cascades of unhelpful collisions. The generic-word list for the weak-description check is explicit in `skill_lint.py` and does not alter the overlap tokenizer. These are lexical hints, not proof of semantic equivalence or completeness. Nothing is auto-edited.

The command refuses catalogs above 1024 rows, serialized responses above 8 MiB, or reports that would contain more than 4096 pairs. It counts malformed or repeated identifiers, non-string descriptions, descriptions above 4096 characters, and descriptions with more than 128 distinct comparison tokens as `invalid_rows` rather than printing or guessing from them. No collision result is asserted for an omitted row. Names must be 1–128 ASCII letters, digits, hyphens, underscores, or colons, starting with a letter or digit. This safe output grammar may omit a valid Hermes identifier that uses other punctuation; the invalid count makes that omission visible.

The frozen offline synthetic check is under `evaluation/lint-skills/`; its plan, fixture, and historical results remain byte-for-byte unchanged. That v1 plan describes the historical baseline, not the current report. `tests/test_skill_lint.py` protects the recorded plan/fixture hashes, preserves all planted pair labels, and checks the original style oracle with `include_style=True`. V2's reporting and noise controls have separate regression tests, including a 414-entry synthetic catalog. Passing these checks does not establish real-catalog precision, improved agent routing, or a release-harness SHIP verdict. An active-catalog pass needs a separate data go-ahead; it should report only names and aggregate counts.
