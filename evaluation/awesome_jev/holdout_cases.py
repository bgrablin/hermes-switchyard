"""New synthetic confirmation fixtures, independent of the development topic bank."""

from __future__ import annotations


def cases():
    rows = [
        (
            "batch",
            "batch flush behavior",
            [
                "The buffer commits its accumulated events when its size reaches 37.",
                "The configured flush limit is 37 events. The timer fallback is disabled.",
                "The boundary test appended 36 events without a write, then observed one write on event 37.",
            ],
            ["37", "36", "disabled"],
        ),
        (
            "lease",
            "lease renewal behavior",
            [
                "An active lease is renewed 19 seconds before its deadline; a stopped owner cannot renew it.",
                "The deployment sets the lease lifetime to 73 seconds.",
                "The stopped-owner test observed expiration after 73 seconds with zero renewals.",
            ],
            ["19", "73", "zero"],
        ),
        (
            "queue",
            "queue overload behavior",
            [
                "The queue rejects the newest arrival when there are already 41 pending jobs.",
                "The operator handbook says that the oldest job is removed when the queue is full.",
                "The overload test retained all 41 existing jobs and rejected job 42.",
            ],
            ["41", "42", "oldest", "newest"],
        ),
        (
            "paging",
            "page continuation behavior",
            [
                "The next page starts strictly after the last returned item key; the boundary item is excluded.",
                "The default page size is 23 items.",
                "The continuation test concatenated two pages and found 46 unique item keys.",
            ],
            ["23", "46", "excluded"],
        ),
        (
            "encoding",
            "invalid text handling",
            [
                "The decoder rejects invalid UTF-8 input and leaves the destination file untouched.",
                "Replacement-character recovery is disabled in the shipped settings.",
                "The invalid-byte test received a decode error and retained the previous file contents.",
            ],
            ["utf-8", "disabled", "previous"],
        ),
        (
            "shutdown",
            "shutdown draining behavior",
            [
                "Shutdown stops accepting work, waits up to 29 seconds, then cancels pending jobs.",
                "The service configuration gives in-flight work a 29-second grace interval.",
                "The shutdown test submitted a stuck job and observed cancellation after the grace interval.",
            ],
            ["29", "cancel", "accept"],
        ),
    ]
    out = []
    for index, (cid, query, facts, expected) in enumerate(rows):
        files = {}
        for i, fact in enumerate(facts):
            name = ["implementation.md", "policy.md", "check.md"][i]
            blocks = [
                f"Display palette entry {n}: foreground shade {n % 9}, font family Mono, widget spacer {n + 11}. This only configures appearance."
                for n in range(12)
            ]
            blocks.insert(3 + i * 3, fact)
            if index == 0 and i == 0:
                blocks.insert(
                    11, fact
                )  # exact duplicate must not crowd out other files
            files[name] = "\n\n".join(blocks) + "\n"
        prompt = (
            "In `implementation.md`, `policy.md` and `check.md`, find the "
            + query
            + ".\nReturn only JSON with answer, evidence."
        )
        out.append(
            {
                "id": cid,
                "prompt": prompt,
                "files": files,
                "gold": facts,
                "expected_terms": expected,
            }
        )
    out.append(
        {
            "id": "absent",
            "prompt": "In `implementation.md` and `policy.md`, find the payment refund behavior.\nReturn only JSON with answer, evidence.",
            "files": {
                "implementation.md": "The project only chooses terminal color palettes.\n",
                "policy.md": "Refunds are not implemented in this color picker.\n",
            },
            "gold": ["Refunds are not implemented in this color picker."],
            "expected_terms": ["not"],
        }
    )
    out.append(
        {
            "id": "unrelated",
            "prompt": "In `implementation.md` and `policy.md`, find the account migration behavior.\nReturn only JSON with answer, evidence.",
            "files": {
                "implementation.md": "The project only chooses terminal color palettes.\n",
                "policy.md": "Blue is the default background shade.\n",
            },
            "gold": [],
            "expected_terms": [],
        }
    )
    return out
