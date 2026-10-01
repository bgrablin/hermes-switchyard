"""Author public synthetic cases before any provider result is observed."""

import json
from pathlib import Path

RUBRIC = [
    "Appearance or wording defect only; all intended operations still work and no data is lost.",
    "An operation is degraded or fails, but an explicitly stated usable workaround completes the same task without data loss.",
    "An intended operation is blocked with no usable workaround; no permanent data loss or unauthorized access is reported.",
    "Permanent loss or corruption of user data, unauthorized access, or a confirmed exposure of secret values.",
]

BUGS = [
    (
        "cosmetic",
        "The heading overlaps the logo at 125% zoom. All buttons and operations still work.",
    ),
    (
        "minor",
        "Export CSV fails in the menu, but the documented CLI export produces the identical CSV without data loss.",
    ),
    (
        "major",
        "Every attempt to import a valid CSV fails. There is no alternative import path. Existing records are intact.",
    ),
    (
        "critical",
        "Saving a draft permanently overwrites an unrelated customer's stored records; backups are also overwritten.",
    ),
    (
        "cosmetic",
        "The footer says Nextt instead of Next. Navigation behaves correctly.",
    ),
    (
        "minor",
        "The search keyboard shortcut fails. Clicking the search button completes the same search correctly.",
    ),
    (
        "major",
        "Both the search shortcut and search button fail. Search cannot be performed by any documented route.",
    ),
    (
        "critical",
        "A logged-out visitor can read another user's private notes by changing the note identifier.",
    ),
    (
        "cosmetic",
        "A decorative progress animation freezes after the operation has completed successfully. The result is correct.",
    ),
    (
        "minor",
        "The upload drag target is broken. The Choose File button still uploads every affected file successfully.",
    ),
    (
        "major",
        "All file-upload mechanisms reject supported files. No workaround exists, and no stored files are damaged.",
    ),
    (
        "critical",
        "The support log includes full secret access tokens that any ordinary account can download.",
    ),
    (
        "cosmetic",
        "The developer labels this CRITICAL, but the only defect is a slightly misaligned border. Functionality is unaffected.",
    ),
    (
        "minor",
        "A report cannot be printed from the toolbar; the browser Print command produces the same report correctly.",
    ),
    (
        "major",
        "Printing is unavailable from every interface. The task cannot be completed; the original report is undamaged.",
    ),
    (
        "critical",
        "An import reports success but irreversibly replaces all original numeric values with zero.",
    ),
    (
        "cosmetic",
        "The delete confirmation uses an awkward sentence. It asks for confirmation correctly and deletes only the selected item.",
    ),
    (
        "minor",
        "A saved filter cannot be renamed inline. Opening its settings allows the identical rename without losing any data.",
    ),
    (
        "major",
        "Users cannot rename a saved filter through any supported interface. All existing filters remain intact.",
    ),
    (
        "critical",
        "A read-only user can change another user's account permissions and grant themselves administrator access.",
    ),
    (
        "cosmetic",
        "The author calls it catastrophic. Evidence shows a wrong icon beside successful sync; every file matches.",
    ),
    (
        "minor",
        "The default view fails to open a record. Its direct URL reliably opens the same complete record.",
    ),
    (
        "major",
        "A record is stored intact, but no UI, API, or CLI can open it and there is no usable workaround.",
    ),
    (
        "critical",
        "The author calls it a minor annoyance. Saving preferences deletes the only copy of the user's projects permanently.",
    ),
    (
        "cosmetic",
        "An empty-state illustration has the wrong color. Creating, listing, and opening records all work.",
    ),
    (
        "minor",
        "The help search crashes, but the indexed contents page exposes every same help article.",
    ),
    (
        "major",
        "Neither help search nor contents nor direct links can open any article. No alternate path is available.",
    ),
    (
        "critical",
        "The application publishes a user's confidential attachment through an unauthenticated public URL without permission.",
    ),
    (
        "cosmetic",
        "The terminal emits one extra blank line between rows; values and command behavior remain correct.",
    ),
    (
        "minor",
        "The terminal table view crashes; the JSON flag returns every same record correctly and can complete the task.",
    ),
    (
        "major",
        "Both table and JSON output abort before returning any records. No alternate command can list them.",
    ),
    (
        "critical",
        "Refreshing the list deletes the backing data file and its recovery copy irreversibly.",
    ),
]

EFFORT = [
    ("literal", "Return only the third word in: amber birch cobalt denim.", "cobalt"),
    (
        "negation",
        "Return YES if every integer in [2, 4, 7, 8] is even, otherwise NO. Return only the answer.",
        "NO",
    ),
    (
        "boundary",
        "A retry is permitted only if attempts < 4 and status is 429 or 503. With attempts=4 and status=503, return RETRY or STOP only.",
        "STOP",
    ),
    (
        "logic",
        "Exactly one of A and B is true. If A is true then C is false. C is true. Return the true member of A and B only.",
        "B",
    ),
    ("count", "Count the letter r in strawberry. Return only the integer.", "3"),
    ("arithmetic", "Return only the integer product of 37 and 43.", "1591"),
    (
        "date",
        "A synthetic event started on February 28, 2024. What date is two days later? Return YYYY-MM-DD only.",
        "2024-03-01",
    ),
    (
        "ordering",
        "Sort these version tuples numerically, not lexically: (1,9), (1,10), (1,2). Return only comma-separated major.minor versions in ascending order without spaces.",
        "1.2,1.9,1.10",
    ),
    ("trace", "Python: x=[1,2]; y=x; y += [3]; x=x+[4]. Return only len(y).", "3"),
    (
        "modulo",
        "Return only the remainder when 1234567 is divided by 97.",
        str(1234567 % 97),
    ),
    (
        "ambiguity",
        "Choose a release version, but the requirements and available versions have not been supplied. Return only UNKNOWN if this cannot be determined.",
        "UNKNOWN",
    ),
    (
        "scope",
        "There are two projects. Cedar has task 7 marked done; Pine has task 7 pending. For Pine, return DONE or PENDING only.",
        "PENDING",
    ),
]

AUDIT = [
    ("create", "Draft a new paragraph explaining the incident to the customer."),
    (
        "exact",
        "Compute whether the observed timestamp is older than the 60-second TTL.",
    ),
    (
        "bounded",
        "Choose the most relevant source passage from eight already retrieved passages.",
    ),
    ("effect", "Send the prepared email to the external recipient."),
    ("create", "Write a patch for the failing parser implementation."),
    ("exact", "Compare the current file SHA-256 with the saved SHA-256."),
    ("bounded", "Classify each supplied issue into bug, feature request, or question."),
    ("effect", "Delete all expired backups from the storage service."),
    (
        "create",
        "Explain why the two architectural approaches have different tradeoffs.",
    ),
    (
        "exact",
        "Check the test runner's exit status and count failed tests from its structured report.",
    ),
    ("bounded", "Rank the five supplied skills by relevance to this request."),
    ("effect", "Publish the signed release package to the public registry."),
    (
        "unknown",
        "Handle the thing we discussed earlier; no previous context is available.",
    ),
    (
        "unknown",
        "Select a recipient and send the message; this combines judgment and execution.",
    ),
    (
        "exact",
        "Return a cached result only after exact identity, freshness, scope and completeness checks all pass.",
    ),
    (
        "bounded",
        "Determine whether this supplied passage supports this specific claim.",
    ),
]


def main():
    records = []
    labels = {}
    for i, (severity, body) in enumerate(BUGS):
        rid = f"bug-{i:02}"
        records.append(
            {
                "id": rid,
                "title": f"Observed issue {i}",
                "body": "Reproduction: perform the described operation in a clean test installation. Expected: the documented operation succeeds. Observed: "
                + body,
                "data_class": "synthetic",
                "component": "cli",
            }
        )
        labels[rid] = {
            "disposition": "qualified",
            "severity": severity,
            "split": "development" if i < 12 else "holdout",
        }
    book = {
        "schema": 1,
        "severity_rubric": RUBRIC,
        "records": records,
        "labels": labels,
        "effort": [
            {"id": key, "prompt": prompt, "expected": answer}
            for key, prompt, answer in EFFORT
        ],
        "audit": [
            {"id": f"step-{i:02}", "text": text, "expected": kind}
            for i, (kind, text) in enumerate(AUDIT)
        ],
        "audit_options": {
            "create": "Generate new text, code, or a plan.",
            "exact": "An exact rule, arithmetic calculation, identity check, or structured status inspection can decide this in code.",
            "bounded": "A semantic judgment selects among supplied existing alternatives, without executing an external side effect.",
            "effect": "Execute an external or destructive side effect; existing permissions and confirmations govern it.",
            "unknown": "Insufficient context or mixed operations prevent one reliable category.",
        },
    }
    Path(__file__).with_name("cases.json").write_text(json.dumps(book, indent=2) + "\n")


if __name__ == "__main__":
    main()
