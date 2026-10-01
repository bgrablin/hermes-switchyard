"""Independent workflow fixtures; labels frozen before any workflow provider calls."""

import json
from pathlib import Path

rows = [
    (
        "help-typo",
        "Help banner misspells Options",
        "cli",
        "On version 1.0 Linux, run project --help. Expected heading: Options. Actual heading: Optoins. All documented commands work; only that spelling is wrong.",
        "cosmetic",
    ),
    (
        "format-workaround",
        "Short JSON format option fails",
        "cli",
        "On version 1.0 Linux, create a.txt containing hello. Run project convert a.txt -f json. Expected JSON output; actual Unknown option -f. Running project convert a.txt --format json outputs the correct identical JSON, with no loss.",
        "minor",
    ),
    (
        "parser-blocked",
        "Valid empty JSON object cannot be parsed",
        "parser",
        "On version 1.0, save {} to input.json and run project parse input.json. Expected parsed empty object; actual parser exits 2 with Unsupported object. Both API parse('{}') and CLI fail. No supported parse path works. Source files are unchanged.",
        "major",
    ),
    (
        "overwrite",
        "Convert destroys unrelated source file",
        "cli",
        "On version 1.0, create a.txt containing alpha and b.txt containing beta. Run project convert a.txt --output c.txt. Expected c.txt only changes. Actual b.txt is overwritten with zeros, and the project's only backup of b.txt is overwritten too; original data cannot be recovered.",
        "critical",
    ),
    (
        "docs-layout",
        "Command example has uneven indentation",
        "docs",
        "Open the bundled help with project help examples in version 1.0. Expected examples align. Actual the second example is indented one extra space. Copying and running either example succeeds; all operations work.",
        "cosmetic",
    ),
    (
        "env-workaround",
        "CLI endpoint option is ignored",
        "cli",
        "Version 1.0: run project fetch --endpoint https://example.test. Expected supplied endpoint; actual default endpoint is used. Setting PROJECT_ENDPOINT=https://example.test and running project fetch uses the supplied endpoint correctly and retrieves the same data intact.",
        "minor",
    ),
    (
        "network-blocked",
        "Every local test server request fails",
        "network",
        "Version 1.0: start the documented test server on localhost:8123 and run project fetch http://localhost:8123/item. Expected item JSON; actual client fails before sending with invalid socket state. API fetch fails identically. No documented alternative works, and stored data is intact.",
        "major",
    ),
    (
        "access-control",
        "Unauthenticated read exposes another account",
        "network",
        "Version 1.0 test server: create user A and private item 42, then log out. Send GET /items/42 without an Authorization header. Expected 401; actual 200 containing A's private item. Reproduced from a fresh unauthenticated client.",
        "critical",
    ),
    (
        "version-label",
        "Version output repeats a harmless word",
        "cli",
        "On version 1.0 run project --version. Expected project 1.0; actual project project 1.0. Exit is zero and every functional command behaves correctly. No data is changed.",
        "cosmetic",
    ),
    (
        "stdin-workaround",
        "Standard-input parse rejects valid document",
        "parser",
        "Version 1.0: pipe '{\"x\":1}' to project parse -. Expected parsed object; actual unsupported input stream. Saving the identical JSON to doc.json and running project parse doc.json returns the correct object. All original data is retained.",
        "minor",
    ),
    (
        "export-blocked",
        "No interface can export the stored records",
        "cli",
        "Version 1.0: create two test records using project add, then run project export --all. Expected export of both; actual unsupported encoder. API export_all fails too. Every supported export format fails without a workaround. Existing records remain readable and intact.",
        "major",
    ),
    (
        "secret-log",
        "Ordinary users can download live secret tokens",
        "network",
        "Version 1.0 test server: log in as admin using a synthetic live access token, log out, then log in as an ordinary test user and GET /support.log. Expected log excludes credentials; actual response includes the complete usable admin token. That token grants admin access.",
        "critical",
    ),
    (
        "punctuation",
        "Help example has inconsistent punctuation",
        "docs",
        "Version 1.0: run project help import. Expected a final period after the explanatory sentence; actual punctuation is absent. The documented example executes correctly and no operation is impaired.",
        "cosmetic",
    ),
    (
        "rename-workaround",
        "Short rename command is broken",
        "cli",
        "Version 1.0: create item old, run project mv old new. Expected rename; actual unknown subcommand mv. The documented project rename old new succeeds, preserving the same content and completing the requested rename.",
        "minor",
    ),
    (
        "delete-blocked",
        "Delete cannot remove any selected record",
        "cli",
        "Version 1.0: create record r1 then run project delete r1 --yes. Expected r1 removed; actual error unsupported deletion. API delete('r1') also fails. No supported method can delete any record. No records are corrupted or lost.",
        "major",
    ),
    (
        "permission-escalation",
        "Viewer can grant itself administrator privileges",
        "network",
        "Version 1.0: create a viewer-only account. With that account's token send PATCH /accounts/self with role=admin. Expected 403 and no role change; actual 200, role becomes admin, and an admin-only endpoint is accessible. Reproduced twice on a clean test database.",
        "critical",
    ),
]
book = {
    "records": [
        {
            "id": r[0],
            "title": r[1],
            "component": r[2],
            "body": r[3],
            "data_class": "synthetic",
        }
        for r in rows
    ],
    "expected": {r[0]: {"disposition": "qualified", "severity": r[4]} for r in rows},
    "scope": "Synthetic project 1.0 reports with concrete reproduction and expected outcomes; not real user defects.",
}
Path(__file__).with_name("workflow-cases.json").write_text(
    json.dumps(book, indent=2) + "\n"
)
