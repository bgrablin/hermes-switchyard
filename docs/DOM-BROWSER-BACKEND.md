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

The loop offers `CLICK`, `TYPE_TEXT`, `SCROLL_DOWN`, `SCROLL_UP`, `WAIT`, `DONE`, and `BLOCKED`.
`TYPE_TEXT` fills an ordinary text field with a bounded caller-supplied value from
`text_inputs`, matched locally by exact field label and never sent to Jev. Password,
file, payment, and other denied fields stay filtered out. The backend does not upload
files, authenticate, use hotkeys, or reach an existing signed-in session. A goal that
requires one of those unsupported capabilities, or that needs typing without
`text_inputs`, returns `status: unsupported_capability` with a request-free reason
code before the first provider request:

| Code | Trigger |
| --- | --- |
| `dom_text_input_value_required` | the goal states typing into a field but no `text_inputs` were supplied |
| `dom_sensitive_text_input_unsupported` | a `text_inputs` field label names a password, payment, or other denied field |
| `dom_file_upload_unsupported` | the goal states an upload or attachment requirement |
| `dom_authentication_unsupported` | the goal states a sign-in, sign-up, registration, authentication, password, or verification-code requirement |
| `dom_existing_session_unsupported` | the goal asks for an existing, already open, or signed-in browser session |
| `dom_hotkey_unsupported` | `allowed_hotkeys` supplied for a web goal |

`unsupported_capability` is not a fallback and not a partial success. It reports a
capability boundary and names the caller's next option. The capability set is also
reported on every receipt as `capabilities`, so a caller can see the whole set and
not just the mismatch.

After filling an offered field, that same field is not offered for a second
`TYPE_TEXT` decision on the same page. Forms are submitted only by clicking an
offered visible control; the backend does not synthesize Enter or bypass the
destination policy. A public Wikipedia Special:Search form is supported when its
search field and Search button are visible.

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

Narrow quoted derivation is allowed only for these explicit goal phrases:

- `title contains|equals|is "…"` → `source: derived_goal_title`, `title_contains`
- `url equals|is "https://…"` → `source: derived_goal_url`, `url_equals` (public https only)
- `url contains "…"` → `source: derived_goal_url`, `url_contains` (unsafe schemes refused; matching is case-insensitive like title/text contains)

Unquoted URLs and free-form wording are never mined. When no safe predicate is
supplied or derived, the loop falls back to a provider `DONE` decision.

A predicate stop and a provider `DONE` both return `status: completion_candidate`
with `verified: false`. The receipt records `completion_source` as
`local_predicate` or `provider_decision` and reports each predicate check, so the
difference between "the caller's condition matched" and "the model believed it was
done" stays visible. Independent verification remains coordinator-owned.

## Target offering and progress

Snapshots offer up to 48 targets. Elements with zero rendered width or height
are excluded before ranking, so hidden article links cannot crowd out a nearby
visible link. Candidate scanning is windowed around the current viewport: only
targets within a bounded window (three viewport heights)
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
bounded number of consecutive actions produce no progress **and** at least `min_actions_before_done` actions have already been dispatched, the loop stops with
`failure_phase: no_progress` and `reconcile_before_retry: true` instead of paying for
another provider decision over unchanged state. Early stalls below that floor keep
running so scenic multi-hop races are not aborted prematurely.

## Destination boundary

The approved destination class is a public https origin without credentials. The
policy is code (`hermes_switchyard/destination_policy.py`), not model judgment, and
it runs before provider work and at every request boundary the browser exposes.

| Layer | What it decides | When |
| --- | --- | --- |
| Start URL | scheme, credentials, host spelling, and host resolution | before a browser process exists and before any provider request |
| Offered targets | the same lexical policy over every `href` | when a snapshot is filtered |
| Request interception | every request from the page, its frames, and its workers: navigations, redirect hops, subresources | before the request is sent, through request-stage `Fetch` interception |
| Connection | the address every tunnel is dialled to | the validating proxy resolves once, requires every answer to be public, and dials the validated address literal |
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
hop is refused at the hop that drifts. One narrow exception upgrades a
same-host HTTP **document redirect** from an already approved HTTPS request:
the guard validates the exact HTTPS target with the ordinary host/resolution
policy, then fulfills the pending HTTP request locally with a 307 pointing to
HTTPS. It never dispatches HTTP to the network. Initial HTTP URLs, cross-host
downgrades, credentialed/private targets, subresources, and redirects beyond
the existing hop limit remain blocked. The upgraded target must still answer
over HTTPS; this is not an HTTP fallback.

The browser starts on `about:blank` and loads the start URL only after interception
is installed, so the first load and its redirects are covered. Interception that
cannot be proven (the protocol call fails, the connection ends, a frame cannot be
attached, a handler cannot answer) is recorded as `interception_unavailable` and
stops the run. New windows are blocked, so the session stays a single tab.

A refused **navigation** (including a frame navigation), an address refused at connect
or seen after the fact, and any integrity failure stop the run:

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

### Connection pinning

The request check resolves a host, but Chrome resolves it again to connect, so a
rebinding DNS server could answer with a public address to the check and a private
one to the connect. The browser is therefore launched with `--proxy-server` pointing
at a loopback validating proxy (`ValidatingProxy`), and every connection it makes
goes through that proxy:

- The proxy accepts only `CONNECT host:port`. Plain HTTP and malformed requests are
  refused, and the head is size- and time-bounded.
- It applies the lexical policy to the target, resolves the host once, requires
  every returned address to be public, and dials one of those address literals. The
  connector refuses a hostname, so nothing is resolved a second time, and each
  tunnel is validated on its own answer.
- `--proxy-bypass-list=<-loopback>` removes Chrome's implicit bypass, so a loopback
  target reaches the proxy and is refused there.
- A tunnel refused for an address is recorded as a fatal `proxy_connect` violation
  (the request check had allowed the name and the connect saw something else). A
  resolution failure or a plain-HTTP request is evidence only: no connection was
  made, or it was browser housekeeping.
- Because the proxy dials the connection, the response address Chrome reports is
  the local proxy's, so the after-the-fact address check is off when pinning is on
  and the receipt says so (`connection_pinning: true`,
  `post_response_address_check: false`).
- The profile disables non-proxied WebRTC UDP (a page-created connection with a STUN
  server on loopback otherwise sent datagrams there even under the proxy), QUIC is
  disabled, and Chrome's background time query is turned off so it does not hit the
  proxy as plain HTTP. This also puts a WebSocket connection behind the same pin.

Each control was checked natively against a loopback listener with a negative
control that leaks without it, and against a mutation that removes it:
`tests/test_destination_proxy.py`. Live pinning tests need
`SWITCHYARD_LIVE_BROWSER_TESTS=1`.

### What this boundary does not claim

- **The proxy dials directly.** An environment that requires an upstream proxy to
  reach the internet is not supported; connections through it fail rather than
  bypassing policy.
- **Any local process can use the loopback proxy** to reach public https hosts. It
  can reach nothing the policy refuses, and it cannot make the session fatal
  through a malformed request.
- **The browser settings are version-dependent.** Network prediction, WebRTC, QUIC,
  and the background time query were verified against Chromium 152.0.7977.64
  (Snap). The `--force-webrtc-ip-handling-policy` switch did not stop WebRTC UDP
  there; the profile preferences did. A different version may behave differently,
  and the real-browser tests are the check.
- **A session without pinning** (only reachable through a private test seam) keeps
  the earlier limits: the DNS answer may change between check and connect, and the
  address check detects a private connection only after it was made.
- **Interception covers the page target, its frames, and dedicated workers.** A
  dedicated worker rejects `Fetch.enable`, and its requests are paused on the parent
  session, which was verified against the installed Chromium. Other worker types
  that reject interception fail closed.
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

On POSIX hosts the browser keeps renderer shared memory in `/dev/shm` when that
mount is writable and at least 512 MiB. `--disable-dev-shm-usage` is added only
for a smaller or unusable `/dev/shm`, such as a default container. With that flag
always on, Snap Chromium's Wikipedia renderer crashed on load; without it the same
page loaded cleanly.

A page renderer crash (`Inspector.targetCrashed` on the page target) fails every
pending and later protocol command at once with the bounded code
`renderer_crashed`, instead of a generic 15-second command timeout. A crash
before the first page is ready, including interception setup, reports
`failure_phase: browser_startup` and spends no provider request. A crash in
the first snapshot reports `failure_phase: capture` with `failure_reason:
renderer_crashed`, also before any provider request. A crashed page is never
retried automatically.

## Action evidence

Each action record separates three claims that are not interchangeable:

| Field | Meaning |
| --- | --- |
| `action_dispatched` | the click, scroll, or wait was sent to the browser |
| `effect_observed` | a URL, title, document, or focus change was observed afterwards |
| `goal_verified` | always false inside the loop; receipt-level verification is separate |

Action records do not carry a top-level `verified` field. Receipt-level dual-gate verification sets `goal_verified` / `verified` true only when Hermes agreed `DONE` (`completion_source: provider_decision`) **and** a local completion condition is satisfied; `verification_owner` is then `hermes_and_url`. A `local_predicate` early-stop (caller-supplied or derived) may still be `completion_candidate` but keeps both flags false. Provider `DONE` without a satisfied condition stays unverified (`verification_owner: coordinator`).

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
benchmark against other browser automation. The plugin still returns `verified: false`
for every completion candidate.

Issue #25 unit/fixture proof (identical Ada Lovelace → Analytical Engine public
fixture, scripted provider): without a predicate the loop spends two Jev decisions
(`CLICK` then `DONE`); with `url_equals` it spends one (`CLICK` only), keeps
`completion_candidate` / `verified: false`, and records lower `jev_request_count` and
`jev_total_latency_ms`. A paired live-provider host benchmark is still optional and is
not claimed here.