# Security

This plugin handles model-facing state and can dispatch Hermes `computer_use` actions. Do not open a public issue with secrets, credentials, raw UI captures, or private logs. Use the repository's GitHub private vulnerability reporting channel when it is available; otherwise contact the maintainer through the repository profile before public disclosure.

Operational boundaries:

- `public_or_sanitized_data_ack` is a caller attestation, not DLP or authorization. Do not send private, employer, regulated, credential, payment, or verification data.
- The client accepts only the fixed direct TypeSafe or OpenRouter Jev endpoint and endpoint-specific aliases. OpenRouter provider fallback is disabled, direct TypeSafe requests omit OpenRouter-only fields, and redirects are rejected.
- Computer use is cross-platform when Hermes' Cua Driver-backed tool is available. It preserves Hermes dispatch and approval, filters sensitive or destructive controls, partitions dense targets without dropping them, re-captures before actions, and returns `verified: false` for completion candidates.
- Do not commit API keys, auth files, config files, raw runs, UI captures, or logs. Use the repository `.gitignore` and Hermes' native secret/config flows.

Security reports should include the plugin version, a minimal reproduction, and sanitized output only. Never include secret values.
