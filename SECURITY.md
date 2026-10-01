# Security

## Reporting a vulnerability

**Please don't open a public issue** for a security problem, and never include secrets, credentials, raw screen captures, or private logs anywhere.

- Use the repository's GitHub **private vulnerability reporting** channel when it's available.
- Otherwise, contact the maintainer through the repository profile before any public disclosure.

A good report includes the plugin version, a minimal reproduction, and sanitized output only. Never include secret values.

## Security boundaries to know about

Switchyard handles model-facing state and can drive a browser or Hermes `computer_use`. These are its main boundaries:

- **The data acknowledgement is a promise, not a filter.** `public_or_sanitized_data_ack` records the caller's attestation. It is not DLP or authorization. Don't send private, employer, regulated, credential, payment, or verification data.
- **Fixed endpoints only.** The client accepts only the fixed direct TypeSafe or OpenRouter Jev endpoint and the matching aliases. OpenRouter provider fallback is disabled, direct TypeSafe requests omit OpenRouter-only fields, and redirects are rejected.
- **Computer use stays inside Hermes' controls.**
  - Desktop runs go through Hermes' Cua Driver-backed tool. They keep Hermes' dispatch and approval, filter sensitive or destructive controls, split dense target lists without dropping targets, and recapture the screen before each action.
  - Completion candidates, browser and desktop alike, return `verified: false`.
  - Browser runs use a throwaway profile and allow public `https` destinations only. Details: [DOM-BROWSER-BACKEND.md](docs/DOM-BROWSER-BACKEND.md).
- **Nothing sensitive in the repository.** Don't commit API keys, auth files, config files, raw runs, screen captures, or logs. Use the repository `.gitignore` and Hermes' own secret and config flows.
