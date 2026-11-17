# Competitor Analysis: MillionVerifier, ZeroBounce, Hunter

Research date: 2026-11-17. Method: the `reverse-eng-site-skill` workflow
(instrument → replay → prove fidelity), applied to the **publicly reachable**
surface only. No accounts were used, no authentication was bypassed, and no
captcha or anti-abuse control was circumvented.

Raw evidence (sanitized, no cookies or tokens) is in [`evidence/`](evidence/).

## Evidence labels used in this document

| Label | Meaning |
|---|---|
| **VERIFIED (doc)** | Stated in the vendor's own published documentation; source quoted/linked. |
| **VERIFIED (replay)** | Observed directly by replaying a documented request in this research session. |
| **VENDOR CLAIM** | Marketing statement by the vendor; not independently checked. |
| **THIRD-PARTY** | Stated by a competitor or reviewer; treat with caution. |
| **INFERENCE** | Our reasoning from the evidence; not confirmed by the vendor. |

## What the skill-driven capture found

The skill's Step 2 asks for a captured request from an authenticated session.
We had no authorized accounts, so we captured unauthenticated page loads with
headless Chromium and ranked requests with the skill's `har_candidates.py`
(results: `evidence/har_candidates_*.tsv`).

| Site | Page captured | Verification request seen? | Gate found |
|---|---|---|---|
| millionverifier.com | home page | No: only analytics, Ghost CMS, cookie consent | Turnstile/reCAPTCHA scripts present |
| zerobounce.net | /free-email-verifier/ | No: only promotions + Cloudflare challenge | Cloudflare challenge platform, Turnstile/reCAPTCHA |
| hunter.io | /email-verifier | No (the bundle references `/api/email-verifier`) | Turnstile/reCAPTCHA; the in-app verifier requires sign-in |

**Conclusion (VERIFIED (replay)):** every web widget is captcha- or login-gated.
Per the skill's permission boundary ("Discovery does not authorize bypassing a
... challenge"), UI capture stopped there. All three vendors publish documented
APIs, so the replay stage used those instead. A direct unauthenticated call to
ZeroBounce's API host returned Cloudflare `error code: 1020` (HTTP 403),
which is an access rule that we did not attempt to get around.

---

## 1. MillionVerifier

### API surface: VERIFIED (doc + replay)

The developer page embeds a full OpenAPI spec. It is extracted in
`evidence/millionverifier_openapi_extract.json`.

| Endpoint | Purpose |
|---|---|
| `GET api.millionverifier.com/api/v3?api=KEY&email=X&timeout=2..60` | Real-time single verification (default timeout 20 s) |
| `GET /api/v3/credits` | Credit balance |
| `POST bulkapi.millionverifier.com/bulkapi/v2/upload` | Upload a file (multipart `file_contents`) |
| `GET /bulkapi/v2/fileinfo`, `/filelist` | Progress: `percent`, `verified`, `unverified`, per-class counts, `estimated_time_sec` |
| `GET /bulkapi/v2/download?filter=ok\|ok_and_catch_all\|unknown\|invalid\|all\|custom` | Results, filterable by status, `free`, `role` |
| `/bulkapi/stop`, `/bulkapi/v2/delete` | Job control |

**Single response fields:** `email, quality, result, resultcode, subresult, free,
role, didyoumean, credits, executiontime, error, livemode`.

- `result` ∈ `ok(1) | catch_all(2) | unknown(3) | error(4) | disposable(5) | invalid(6)`
- `quality` ∈ `good | bad | risky | ""`
- `subresult` (33 values, verbatim from the spec):
  `unknown, ok, internal_error, invalid_syntax, no_local_ip_available,
  dns_server_failed, dns_no_mx, dns_no_a, could_not_connect, no_code_in_banner,
  invalid_banner_code, no_code_in_ehlo_response, no_code_in_helo_response,
  no_code_in_mail_from_response, no_code_in_rcpt_to_response, ip_blocked,
  no_mailbox, mailbox_disabled, mailbox_full, greylisted, connection_lost,
  connection_timeout, connection_refused, connection_reset_by_peer,
  connection_no_route_to_host, host_not_accept_incoming_mail,
  mail_service_unavailable, bad_domain, dns_error, anti_spam_system,
  dns_no_domain, dns_refused, timeout_error`

### What the subresult enum reveals: INFERENCE, strongly supported

The enum mirrors an SMTP client state machine one step at a time:
`dns_*` → `could_not_connect / connection_*` → `no_code_in_banner /
invalid_banner_code` → `no_code_in_ehlo_response / no_code_in_helo_response`
(EHLO with HELO fallback) → `no_code_in_mail_from_response` →
`no_code_in_rcpt_to_response` → mailbox verdicts (`no_mailbox`,
`mailbox_disabled`, `mailbox_full`) or blocking verdicts (`ip_blocked`,
`anti_spam_system`, `greylisted`). `no_local_ip_available` suggests a pool of
outbound IPs is used for SMTP. `dns_no_a` alongside `dns_no_mx` suggests an
RFC 5321 implicit-MX (A-record) fallback.

### Replay of documented demo keys: VERIFIED (replay)

`API_KEY_FOR_OK|CATCH_ALL|INVALID|UNKOWN|DISPOSABLE` return fixed results with
`livemode:false` (`evidence/millionverifier_demo_key_replays.txt`). Observed
`result → quality` mapping: `ok→good`, `catch_all→risky`, `unknown→risky`,
`invalid→bad` (subresult `no_mailbox`), `disposable→bad`.
Demo keys do **not** compute `free`, `role` or `didyoumean`: `info@…`,
`john@gmial.com` and `support@yahoo.com` all returned `false`/empty. **Demo
keys therefore cannot be used for benchmarking.**

### Methodology as published: VERIFIED (doc), detail limited
From [How Email Verification Works](https://www.millionverifier.com/blog/how-email-verification-works-the-millionverifier-process/)
and the [results help page](https://help.millionverifier.com/email-verification/email-verification-results):
1. Normalization and syntax ("IETF-compliant formatting"), typo catching.
2. Domain/MX DNS check: "If the domain is dead, misconfigured, or missing valid MX records, the email is immediately marked Invalid."
3. Disposable detection against "a continuously updated database"; role flagging (admin@, support@ …), which are "may still be valid".
4. SMTP handshake: "The connection is then dropped before any actual message content is transmitted."
5. Greylisting: the engine "pauses, waits, and retries in the background."
6. "Smart Verify" for ambiguous cases uses "real-life sending data", "historical server behavior" and similar signals (**VENDOR CLAIM**, no method disclosed).
7. Catch-all resolution (paid add-on): "deep-level ping", "30% to 40% recovery rate" (**VENDOR CLAIM**). Unresolved → "Risky"; no credits charged for unknown/unresolved catch-all.

---

## 2. ZeroBounce

### API surface: VERIFIED (doc)
- `GET|POST api.zerobounce.net/v2/validate?api_key&email&ip_address&timeout(3–60, default 30)&activity_data&verify_plus` (also `api-us`, `api-eu`)
- `POST /v2/validatebatch`: up to 200 emails, may take up to 70 s
- File API: `sendfile` (`email_address_column`, `has_header_row`, `remove_duplicate`, `return_url`), `filestatus`, `getfile`, `deletefile`
- **Greylist API processing:** `greylist_processing=true` + polling after ~30 minutes, or a `callback_url` webhook
- Rate limit: 80,000 requests / 10 s; 200 bad-key requests/hour → 1 h block
- "On average, 96-98% of all domains will return in 1 to 5 seconds … Postfix/Dovecot … 20-second connection time"
- "If we encounter a slow mail server or a mail server with a greylisting algorithm, you will get an unknown result."

### Response fields: VERIFIED (doc)
`address, status, sub_status, account, domain, did_you_mean, domain_age_days,
active_in_days, active_first_seen, free_email, mx_found, mx_record,
smtp_provider, catchall_domain, firstname, lastname, gender, city, region,
zipcode, country, processed_at`.

### Status taxonomy: VERIFIED (doc), full text in `evidence/zerobounce_v2_status_codes_page.txt`

| status | sub_status values |
|---|---|
| valid | `alias_address, leading_period_removed, alternate, gold, role_based_accept_all, accept_all, ai_agent_mailbox` |
| invalid | `does_not_accept_mail, failed_syntax_check, possible_typo, mailbox_not_found, no_dns_entries, mailbox_quota_exceeded, unroutable_ip_address` |
| catch-all | (none), `ai_agent_mailbox` |
| spamtrap | (none) |
| abuse | (none) |
| do_not_mail | `role_based, disposable, role_based_catch_all, mx_forward, global_suppression, possible_trap, toxic` |
| unknown | `antispam_system, exception_occurred, failed_smtp_connection, forcible_disconnect, greylisted, mail_server_did_not_respond, mail_server_temporary_error, timeout_exceeded` |

Notable documented policies:
- `accept_all` (a **valid** sub-status) is distinct from `catch-all`: "Domain vetted by ZeroBounce, historically delivers". This is an allow-list built from their own history; it is not an SMTP signal.
- "All emails that we flag as 'Role-Based' are valid emails." This means role-based is only assigned after the mailbox has been validated.
- `mx_forward`: "Domain forwards its MX records to one domain. Similar to a disposable domain." This is MX-infrastructure-based disposable detection.
- `possible_trap`: keyword-based (`spam@`, `@spamtrap.com`).
- unknown: "In most cases, 80% of unknowns are invalid" (**VENDOR CLAIM**); unknown results are not charged.
- Catch-all FAQ: ZeroBounce "can validate many catch-all email addresses … across … Google Workspace, Microsoft 365, and major Secure Email Gateway (SEG) providers"; the domain-level catch-all flag is preserved separately from the address verdict. **Verify+** adds a further catch-all validation phase (method undisclosed).
- Sandbox: `valid@example.com`, `invalid@example.com`, `catch_all@example.com`, … return fixed results without using credits, but **still require an API key**.

---

## 3. Hunter

### API surface: VERIFIED (doc + replay)
- `GET api.hunter.io/v2/email-verifier?email&api_key`; runs up to 20 s; **HTTP 202** if still running (poll again, counted once); **222** for an unexpected remote SMTP response; **451** when the data subject has opted out; 10 req/s, 300 req/min.
- Results may be reused from **up to one month** earlier (`verification.date`).
- Documented `test-api-key` returns a dummy response. Replayed: `evidence/hunter_test_key_replay.json`. Unauthenticated → `401 authentication_failed`.

### Response fields: VERIFIED (doc + replay)
`status, score, email, regexp, gibberish, disposable, webmail, mx_records,
smtp_server, smtp_check, accept_all, block, verification{date,status},
sources[], result (deprecated: deliverable|undeliverable|risky)`.

- `status` ∈ `valid | invalid | accept_all | webmail | disposable | unknown`
- "Webmail and disposable addresses get a fixed score of 50." So for
  webmail (Gmail etc.), Hunter does **not** claim SMTP-level confirmation.
- `accept_all`: "SMTP checks can give false positives."
- `block`: "Whether the SMTP server blocked the check."
- `gibberish`: auto-generated-looking local part.
- `sources`: web pages where the address was found. This is Hunter's
  distinguishing signal (crawl data), which is unavailable to an independent tool.

---

## Cross-vendor conclusions

1. **Common core (VERIFIED (doc) for all three):** syntax → domain/DNS/MX →
   disposable list → SMTP RCPT probe without sending → catch-all detection →
   class + reason code. None claims to send a real message during standard
   verification.
2. **The SMTP layer is reported in fine detail** (MV's 33 subresults, ZB's
   unknown sub-statuses, Hunter's `smtp_server/smtp_check/block`). All three
   treat blocked, greylisted or timed-out probes as **unknown, not invalid**.
3. **Catch-all is never reported as valid on SMTP evidence alone.** Each vendor
   resolves catch-alls (if at all) using proprietary data: sending history,
   allow-lists, crawl sources or AI scoring.
4. **Their real moat is data, not protocol:** IP reputation pools, historical
   bounce data, spam-trap/abuse feeds, web-crawl sources. These cannot be
   reproduced independently; see `feature-comparison.md`.
5. **Accuracy claims (99%+, 99.6%) are vendor claims.** Third-party benchmarks
   found in search results were run by competitors and are not independent.

## Sources
- MillionVerifier API docs: https://developer.millionverifier.com/
- MillionVerifier process: https://www.millionverifier.com/blog/how-email-verification-works-the-millionverifier-process/
- MillionVerifier results: https://help.millionverifier.com/email-verification/email-verification-results
- MillionVerifier catch-all: https://www.millionverifier.com/catch-all-email-verifier/
- ZeroBounce validate: https://www.zerobounce.net/docs/email-validation-api-quickstart/v2-validate-emails
- ZeroBounce status codes: https://www.zerobounce.net/docs/email-validation-api-quickstart/v2-status-codes
- ZeroBounce greylist: https://www.zerobounce.net/docs/email-validation-api-quickstart/greylist-api-processing
- ZeroBounce sandbox: https://www.zerobounce.net/docs/email-validation-api-quickstart/v2-sandbox-mode
- ZeroBounce catch-all FAQ: https://www.zerobounce.net/docs/frequently-asked-questions/questions-about-our-services/what-are-catch-all-domains
- Hunter API: https://hunter.io/api-documentation/v2#email-verifier
- Third-party (caution): https://leadmagic.io/blog/what-does-risky-mean-millionverifier , https://www.salesforge.ai/blog/millionverifier-review
