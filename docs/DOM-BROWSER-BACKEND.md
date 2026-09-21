# DOM browser backend

`jev_computer_use` runs a DOM browser loop when the caller supplies a public
`start_url` or when the goal contains a public https URL. One bounded Jev request
per step chooses the operation and the click target together, and the plugin then
acts on the page. Hermes `computer_use` is never inserted between those clicks.

Desktop applications without a public URL still use Cua Driver.

## Backend and session semantics

Every DOM result reports what actually ran:

| Field | Meaning |
| --- | --- |
| `backend` | `chromium_dom`, the only DOM implementation |
| `session_mode` | `headless_ephemeral`, a per-run profile removed on close |
| `capabilities` | the explicit capability set for this backend and mode |
| `session_identity` | `mode`, `profile` (`fresh_ephemeral`), `context_generation` (always `1` here), `tab` (`single_tab`) |
| `browser` | `chromium`, `chrome`, or `edge` |
| `browser_confinement` | `none` or `snap` |
| `session_setup_ms` | measured launch and page-ready time for this call |
| `jev_total_latency_ms` | summed provider decision latency, separate from setup |

The backend never attaches to a user browser profile. Each call launches a fresh
headless profile with restrictive permissions and removes only the directory it
created. The session-mode vocabulary is `headless_ephemeral`,
`managed_persistent`, and `attached_existing_user_browser`; this backend
implements only `headless_ephemeral`. A caller that needs an authenticated or
already open session must use the native `computer_use` path instead.

## Supported operations

The loop offers `CLICK`, `SCROLL_DOWN`, `SCROLL_UP`, `WAIT`, `DONE`, and `BLOCKED`.
It does not type into fields, upload files, authenticate, or reach an existing
signed-in session. A goal that requires one of those capabilities returns
`status: unsupported_capability` with a request-free reason code before the first
provider request, so no Jev requests are spent discovering the mismatch:

| Code | Trigger |
| --- | --- |
| `dom_text_input_unsupported` | `text_inputs` supplied for a web goal, or the goal states typing into a field |
| `dom_file_upload_unsupported` | the goal states an upload or attachment requirement |
| `dom_authentication_unsupported` | the goal states a sign-in, sign-up, registration, authentication, password, or verification-code requirement |
| `dom_existing_session_unsupported` | the goal asks for an existing, already open, or signed-in browser session |
| `dom_hotkey_unsupported` | `allowed_hotkeys` supplied for a web goal |

`unsupported_capability` is not a fallback and not a partial success. It reports a
capability boundary and names the caller's next option. The capability set is also
reported on every receipt as `capabilities`, so a caller can see the whole set and
not just the mismatch.

## Completion predicates

A caller may supply `completion_condition`, a bounded predicate evaluated locally
against every observation:

```text
{"url_equals": "https://example.org/target"}
{"title_contains": "Analytical Engine"}
{"text_contains": "Order confirmed", "element_label": "Order confirmed"}
```

The predicate is fixed before execution begins, is never sent to Jev, and cannot be
relaxed mid-loop. When it is satisfied, the loop stops without another provider
decision. `min_actions_before_done` still applies.

One narrow derivation exists: a goal that states a quoted expectation, such as
`stop when title contains "Analytical Engine"`, produces a predicate with
`source: derived_goal_title`. No other goal text is interpreted.

A predicate stop and a provider `DONE` both return `status: completion_candidate`
with `verified: false`. The receipt records `completion_source` as
`local_predicate` or `provider_decision` and reports each predicate check, so the
difference between "the caller's condition matched" and "the model believed it was
done" stays visible. Independent verification remains coordinator-owned.

## Target offering and progress

Snapshots offer up to 48 targets. Candidate scanning is windowed around the
current viewport: only targets within a bounded window (three viewport heights)
above or below the viewport are considered, so on a long page the scan follows
the viewport instead of stopping at a fixed document-order prefix. Offering is
scroll-relative: targets in the viewport come first, then targets within one
viewport of it, then the remaining considered targets ordered by distance from
the current viewport. Scrolling therefore advances the offered window instead of
re-offering the top of the document, and a target that was not offered in the
first snapshot becomes reachable by scrolling toward it.

Targets keep a stable identity across scrolls and recaptures, because the snapshot
assigns each element one identifier from a per-document registry instead of
renumbering by position.

Progress is measured locally by an observation signature over URL, title, text, and
the offered targets. Scroll offset and focus are excluded, because they describe the
view rather than the content. When a scroll changes nothing, the loop retries the
scroll locally up to the configured bound before spending another decision. When a
bounded number of consecutive actions produce no progress, the loop stops with
`failure_phase: no_progress` and `reconcile_before_retry: true` instead of paying for
another provider decision over unchanged state.

## Destination boundary

The approved destination class is a public https origin without credentials. The
policy is code (`hermes_switchyard/destination_policy.py`), not model judgment, and
it runs before provider work and at every request boundary the browser exposes.

| Layer | What it decides | When |
| --- | --- | --- |
| Start URL | scheme, credentials, host spelling, and host resolution | before a browser process exists and before any provider request |
| Offered targets | the same lexical policy over every `href` | when a snapshot is filtered |
| Request interception | every request from the page, its frames, and its workers: navigations, redirect hops, subresources | before the request is sent, through request-stage `Fetch` interception |
| Response address | the address a response actually came from | after the response arrives; a private address is recorded as a fatal violation |
| Landing URL | the URL observed after each action | after every action |

Refused by the policy: any scheme other than https (`file`, `data`, `javascript`,
`about`, `blob`, `ftp`, `chrome`, `http`, `ws`) for a navigation; credentialed URLs;
loopback, private, link-local, shared, multicast, and other non-global addresses,
including IPv6 forms that embed a private IPv4 address; numeric host spellings
(`0x7f.0.0.1`, octal, decimal, short forms); percent-encoded or non-ASCII hosts that
normalize to an address; local-only names (`localhost`, `.local`, `.internal`,
`.lan`, `.home.arpa`, single-label hosts); control characters and backslashes; and
any host whose resolution fails, returns nothing, or includes one non-public
address. A non-document subresource may also be `data` or `blob`, which carry no
network destination. A redirect chain is bounded at 10 hops and each hop is decided
on its own, so a chain that starts public and drifts to a private or credentialed
hop is refused at the hop that drifts.

The browser starts on `about:blank` and loads the start URL only after interception
is installed, so the first load and its redirects are covered. Interception that
cannot be proven (the protocol call fails, the connection ends, a frame cannot be
attached, a handler cannot answer) is recorded as `interception_unavailable` and
stops the run. New windows are blocked, so the session stays a single tab.

A refused **navigation** (including a frame navigation), a refused address seen
after the fact, and any integrity failure stop the run:

```text
status: blocked
failure_phase: destination_blocked
failure_reason: non_public_address    # bounded code, never provider or page text
reconcile_before_retry: true          # the click was dispatched; its effect is uncertain
```

A refused **subresource** is blocked at the request and recorded in
`destination_policy`, and the run continues, because a public page that references a
private image or script has not moved the session there. A caller who wants a
stricter posture can read `destination_policy.subresource_blocks` from the receipt.

Every receipt carries `destination_policy`: policy name and version, whether
interception was active, requests checked and blocked, redirect hops and
cross-origin redirects, the refusal records, and the named residual risks. A refused
URL appears in receipts only as scheme and host; credentials, path, query, and port
are dropped everywhere, including the recent-action history sent to the provider.

### What this boundary does not claim

- **DNS answers can change between the check and the connect.** The policy resolves
  a host before the request is released, and Chrome resolves it again to connect. A
  rebinding server can answer differently the second time. The response-address
  check detects that after the request was sent; it does not prevent it. Closing it
  needs a validating proxy that connects to the address it validated.
- **WebSocket handshakes are detected, not intercepted.** `Fetch` does not pause
  them, so a private WebSocket target is recorded as fatal after the attempt starts.
- **Interception covers the page target, its frames, and dedicated workers.** A
  dedicated worker rejects `Fetch.enable`, and its requests are paused on the parent
  session, which was verified against the installed Chromium. Other worker types
  that reject interception fail closed.
- **The preconnect fix is a browser setting.** It was verified against the Chromium
  the tests ran on (a listener saw six connections without it and none with it). A
  different browser version may behave differently.
- **A resolver that answers non-public addresses for public names is refused.** A
  fake-IP VPN mode, a split-horizon resolver, or a transparent proxy on a private
  address makes public hosts look private. The backend fails closed there rather
  than trusting the answer.
- The policy decides where a request may go. It does not decide whether the page
  content is trustworthy, and it does not verify the goal.

## Sandboxed browsers

Some Linux distributions ship Chromium only as a Snap. The distribution wrapper
`/usr/bin/chromium-browser` resolves to a confined Snap, so the wrapper path alone
cannot decide whether the install is confined. The plugin resolves the target and
reads the wrapper before choosing a profile location.

A confined Chromium gets its per-run profile inside the Snap's own writable area,
under the user's `snap/chromium/common` directory, because the runtime directory and
the cache directory are not writable under confinement. A non-confined install is
always preferred when both are present, and a startup failure returns a bounded
local reason code such as `browser_profile_not_writable` or
`snap_profile_unavailable`.

## Action evidence

Each action record separates three claims that are not interchangeable:

| Field | Meaning |
| --- | --- |
| `action_dispatched` | the click, scroll, or wait was sent to the browser |
| `effect_observed` | a URL, title, document, or focus change was observed afterwards |
| `goal_verified` | always false inside the loop; the coordinator owns verification |

`effect_confirmed` repeats `effect_observed` for compatibility and is never true
without an observed delta, so a click that changes nothing reports
`effect_status: no_observed_effect` instead of a confirmed effect.

Every terminal path returns a structured receipt that keeps the actions already
attempted. A provider timeout, malformed response, validation failure, deadline
exit, startup failure, or unexpected error reports `failure_phase`,
`failure_reason`, `attempted_request_count`, `last_state_hash`, and
`reconcile_before_retry` rather than collapsing into a generic plugin error.

## Decision gating

A provider choice carries its own confidence and a probability distribution over
the alternatives. Before any action is dispatched, the loop checks both: a choice
whose confidence is below the configured floor, or whose margin over the
runner-up is too small to be decisive, abstains with `failure_phase:
low_confidence` or `ambiguous_decision` and dispatches nothing. The floors are
conservative for the public-navigation risk class and are single-sourced module
constants, not values copied from another task.

## Verification status

Offline behavior is covered by `tests/test_browser_use.py`, which uses scripted
providers and fake sessions. Those tests do not call a live service.

The backend was additionally exercised on a Linux host whose only browser is a
confined Snap Chromium, using a real headless browser over CDP:

- A 60-target fixture recorded the pre-fix behavior (last offered target
  `Article item 48`, identical offered set after scrolling to the bottom, target 60
  never offered) and the post-fix behavior (target 60 offered after scrolling,
  offered set changed, identities stable).
- A live public article page recorded a scroll-relative offered window (47 targets,
  one target shared with the first snapshot), no identifier ever reassigned to a
  different element, and a confined profile under `snap/chromium/common`.
- Destination-boundary behavior is covered by `tests/test_browser_destination.py`:
  policy and resolution checks with a stub resolver, the interception guard driven
  with synthetic protocol events, the loop with scripted providers, and a real
  headless browser against a local listener that must record zero connections. The
  listener is test-local and nothing private is browsed. The real-browser tests
  need a Chromium-family browser and DNS for `example.com` and `example.org`, and
  skip when either is missing.
- A live public goal with a caller-supplied predicate clicked one target, observed
  the URL change, satisfied the predicate on the live page, and stopped with one
  provider decision instead of the two decisions the pre-fix loop required.

These are local host checks. They are not part of CI and they are not a comparative
benchmark against other browser automation. A paired live benchmark with a real
provider remains pending, and the plugin still returns `verified: false` for every
completion candidate.