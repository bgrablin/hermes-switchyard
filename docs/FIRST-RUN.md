# First-run check (v0.5.5 development candidate)

**In short:** on a clean machine, installing, enabling, and checking Switchyard took about **9 seconds** of measured time, with no key and no billed calls. Typing in a key and getting a first live answer weren't timed. So "working in under two minutes" is plausible, but not proven end to end. The details follow.

This check used a clean, disposable Hermes home and the pinned Python 3.11 Hermes CLI. The source was the public GitHub repository at commit [`fffcf03`](https://github.com/bgrablin/hermes-switchyard/commit/fffcf03f03bcb9ab3b8bb512907b472c59831019). The manifest still says `0.5.4`; this is not a published v0.5.5 release. The installer completed its normal security scan without `--force`. No provider key was added and no billed request was made. The recorded times include process startup.

| Step | Time | Type | Result |
| --- | ---: | --- | --- |
| Install public Git source at the exact SHA, disabled | 5.951 s | Measured | Exit 0; security scan allowed it. |
| Enable without built-in tool overrides | 0.606 s | Measured | Exit 0; takes effect on the next session. |
| Local `switchyard status --json` | 1.563 s | Measured | Plugin loaded and callable; `credential_required` because neither key was present. `network: false`. |
| Plugin Doctor `--ci` | 0.812 s | Measured | Exit 0; seven tools and six hook registrations. It checks registration, not a live decision. |
| Installed-plugin trivial-turn probe | 0.008 s | Measured | Synthetic `hi`, cap `high` → `low`, `local_trivial`, no Jev client and no socket connection. This used the installed module, not a chat session. |
| Enter one provider key through the masked prompt | 20–60 s | Estimate, **not run** | Depends on the operator and provider. No key was used in this check. |
| Start a fresh Hermes session and send a first turn | 5–15 s | Estimate, **not run** | A real answer and any hosted Jev decision are not measured here. |

The **measured local steps total 8.940 s**. This is below the two-minute target for those steps, not proof of a complete under-two-minute first run. The estimated human steps and a real provider response are excluded. A separate real-profile check installed this development candidate from GitHub in 11.9 s and observed a receipt after `hi`; see the [recorded receipt rendering](assets/effort-receipts-recorded.png). That check is not a timing result for this disposable profile. A local `switchyard test` without `--live` refuses to run; its live mode needs an explicit acknowledgement and a billable key, so the offline proof used the installed plugin's local effort path instead.

For a normal installation, use the [quickstart](../README.md#first-run-quickstart). Keep the installer scan in place; review a blocked source instead of adding `--force`. A missing key is an expected status in a no-key check, not an install failure.
