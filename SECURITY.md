# Security

This plugin handles model-facing state and can dispatch Hermes `computer_use` actions. Do not open a public issue with secrets, credentials, raw UI captures, or private logs. Use the repository's GitHub private vulnerability reporting channel when it is available; otherwise contact the maintainer through the repository profile before public disclosure.

Operational boundaries:

- `public_or_sanitized_data_ack` is a caller attestation, not DLP or authorization. Do not send private, employer, regulated, credential, payment, or verification data.
- The client accepts only the fixed OpenRouter Decisions endpoint and two evidence-backed Jev aliases. Provider fallback is disabled and HTTP redirects are rejected.
- Computer use is a Windows-only capability. It preserves Hermes dispatch and approval, filters sensitive or destructive controls, re-captures before actions, and returns `verified: false` for completion candidates.
- Do not commit API keys, auth files, config files, raw runs, UI captures, or logs. Use the repository `.gitignore` and Hermes' native secret/config flows.

Security reports should include the plugin version, a minimal reproduction, and sanitized output only. Never include secret values.
