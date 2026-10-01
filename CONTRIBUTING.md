# Contributing

Thanks for helping out. Hermes Switchyard is a standalone native Hermes plugin. Keep every change inside this repository, and use only the documented Hermes plugin context. Don't modify Hermes core to support this plugin.

New to the project? The [Concepts primer](docs/CONCEPTS.md) and [documentation map](docs/README.md) are the quickest way in.

## Keep these stable

- the manifest name `hermes-switchyard`
- the root `__init__.py` entrypoint
- the `hermes_switchyard` package path

Preserve `bgrablin` attribution and any genuine third-party attribution.

Never add any of the following to source, tests, docs, or issue reports:

- personal paths or private hostnames
- lab details or operational handoffs
- credentials
- raw UI captures

## Run the local checks

From the repository root:

```text
python -m unittest discover -s tests -v
python evaluation/evaluate.py --validate
python scripts/check_portability.py
python scripts/check_portability.py --history
python scripts/build_release.py --source-sha "$(git rev-parse --verify HEAD)" --output-dir dist
```

What these do:

- **Unit tests** run offline with synthetic fixtures. Some tests skip, with a stated reason, when no Hermes runtime can be imported.
- **The evaluator** uses public synthetic fixtures. It never calls the live Decisions service or drives a real GUI.
- **The portability check** scans for private paths, credentials, and stale branding. `--history` scans the Git history as well.
- **The release builder** reads its fixed file allowlist from Git at the given commit and writes a deterministic ZIP. It then verifies extraction, hashes, the manifest, the entrypoint, and the source reference. If you add a doc that the README links to, add it to `RELEASE_FILES` in `scripts/build_release.py`.

Don't commit `dist/`, test results, caches, logs, or local evidence. Keep behavior tests under `tests/`, and focus them on observable contracts rather than source-text snapshots.

## Pull requests

In the description:

- describe the behavior you changed and the checks you ran;
- state any test boundary that remains.

Be explicit when:

- a result is synthetic;
- a real GUI check is still pending;
- comparative benchmark results aren't available.

Don't claim calibration, task completion, or security properties the code doesn't establish.

## Writing style

All project-facing text and examples are in English.

- **Lead with what the reader can do or will see,** then the details. For a new feature guide, open with a short "In short" paragraph, and put exhaustive rules in a clearly marked reference section.
- **Use concrete limits and real configuration names.**
- **Never include tokens** in commands or URLs.
