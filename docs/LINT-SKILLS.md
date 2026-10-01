# Skill description lint (`lint-skills`)

**In short:** automatic skill routing works best when every skill's description is clearly different from the others. This command reads your skill names and descriptions and flags pairs that look too alike, plus descriptions that are too short, too long, or don't start with "Use when". It runs entirely offline, changes nothing, and is a hint, not a verdict. *New in the upcoming 0.6.0.*

`hermes switchyard lint-skills` checks whether the active Hermes skill descriptions are easy to distinguish. It uses the names and descriptions returned by Hermes' `skills_list()` registry. It does not open skill bodies or supporting files, call Jev or a network endpoint, edit skills, or change automatic routing. It is an advisory routability check, **not** Hermes' SKILL.md standards lint.

Run it after confirming that the active profile and project are the catalog you intend to inspect:

```text
hermes switchyard lint-skills
hermes switchyard lint-skills --json
```

The text report lists counts and skill names with issue labels and named peers. JSON uses `schema: switchyard.lint_skills.v1`, `status: ok`, and stable name-ordered `counts`, `pairs`, `clusters`, and `findings`. Validated identifiers are the only skill-sourced strings exported. Descriptions, bodies, paths, and registry error text are not exported. A failed registry lookup exits nonzero with a closed-set reason instead of printing a partial report.

## What it checks

| Finding | Rule |
| --- | --- |
| `near_duplicate` | Jaccard overlap of normalized description token sets is at least 0.75. |
| `confusable` | Overlap is at least 0.50 but below 0.75. Named peers and connected groups are reported. |
| `short_description` | Fewer than 40 characters. |
| `long_description` | More than 200 characters. |
| `missing_use_when` | Description does not start with `Use when` (case-insensitive). |

## Limits

Token matching folds case, extracts ASCII letters and numbers, and removes the small stopword set frozen in `evaluation/lint-skills/PLAN.md`. It is a lexical hint, not proof that two skills have the same purpose. Review named peers yourself before changing a description. Nothing is auto-edited.

The command refuses catalogs above 1024 rows, serialized responses above 8 MiB, or reports that would contain more than 4096 pairs. It counts malformed or repeated identifiers, non-string descriptions, descriptions above 4096 characters, and descriptions with more than 128 distinct comparison tokens as `invalid_rows` rather than printing or guessing from them. No collision result is asserted for an omitted row. Names must be 1–128 ASCII letters, digits, hyphens, underscores, or colons, starting with a letter or digit. This safe output grammar may omit a valid Hermes identifier that uses other punctuation; the invalid count makes that omission visible.

The frozen offline synthetic check is under `evaluation/lint-skills/`. Passing that fixture does not establish real-catalog precision, improved agent routing, or a release-harness SHIP verdict. An active-catalog pass needs a separate data go-ahead; it should report only names and aggregate counts.
