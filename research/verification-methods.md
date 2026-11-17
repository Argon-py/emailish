# Verification Methods: How Each Check Works

This document breaks the professional verification pipeline into its checks.
For each one it records what the vendors do (with evidence labels from
`competitor-analysis.md`), the governing standard, and how `emailish`
implements it. Labels: **VERIFIED (doc)**, **VERIFIED (replay)**,
**STANDARD** (an RFC or a mail provider's own published documentation),
**INDUSTRY** (widely reported practice that we did not confirm), **INFERENCE**.

---

## 1. Syntax validation

- **Vendors:** MV `invalid_syntax`; ZB `invalid / failed_syntax_check`;
  Hunter `regexp`. **VERIFIED (doc)**
- **Standard:** RFC 5322 (addr-spec), RFC 5321 §4.5.3.1 (local part ≤ 64
  octets, path ≤ 256), RFC 6531/IDNA for internationalized domains.
- **emailish:** `email-validator` in syntax-only mode (no network), plus
  explicit length limits. Quoted local parts and IP-literal domains are
  rejected: no consumer mailbox uses them, and every vendor treats them as bad
  for marketing lists. **Result:** INVALID, `failed_syntax_check`,
  certainty **CONFIRMED**.

## 2. Normalization and typo detection

- **Vendors:** MV `didyoumean`; ZB `did_you_mean` and
  `invalid/possible_typo`; ZB `leading_period_removed`. **VERIFIED (doc)**
- **emailish:** trims whitespace, lowercases the domain, removes the mailto
  prefix and surrounding brackets. Typo detection compares the domain against
  a list of high-volume mailbox domains using Damerau-Levenshtein distance and
  common TLD swaps (`gmail.con`, `hotmial.com`, `yahoo.co`).
- **Policy difference from ZeroBounce:** ZB marks possible typos as invalid.
  We do not label an address invalid because of spelling alone. Typo domains
  that do not resolve become INVALID on DNS evidence. Typo domains that do
  resolve (typosquat domains often accept everything) become **RISKY**, with
  `did_you_mean` set and SMTP evidence recorded.

## 3. Domain existence and DNS

- **Vendors:** MV `dns_no_domain, dns_no_mx, dns_no_a, dns_server_failed,
  dns_refused, dns_error, bad_domain`; ZB `no_dns_entries,
  unroutable_ip_address, does_not_accept_mail`; Hunter `mx_records`.
  **VERIFIED (doc)**
- **Standards:**
  - RFC 5321 §5.1: if there is **no MX**, the domain's A/AAAA record acts as an
    *implicit MX*. Treating "no MX" as invalid is therefore wrong when an A
    record exists. MV's separate `dns_no_mx` / `dns_no_a` codes are consistent
    with this rule (**INFERENCE**).
  - RFC 7505 **Null MX** (`MX 0 .`): the domain explicitly accepts no mail.
    This maps to ZB's `does_not_accept_mail`. Example: `example.com` publishes
    a Null MX (observed in this session with DNS-over-HTTPS).
  - NXDOMAIN is an authoritative "domain does not exist".
  - SERVFAIL, REFUSED and timeouts are **resolver failures**, not
    "domain missing". They must yield UNKNOWN.
- **emailish decision table:**

| DNS outcome | email_status | sub_status | certainty |
|---|---|---|---|
| NXDOMAIN | INVALID | `no_dns_entries` | CONFIRMED |
| Null MX (RFC 7505) | INVALID | `does_not_accept_mail` | CONFIRMED |
| No MX and no A/AAAA | INVALID | `no_mail_host` | CONFIRMED |
| No MX, A present | continue: implicit MX, `mx_found=No` | | |
| All MX hosts resolve to private/loopback/reserved IPs | INVALID | `unroutable_ip_address` | CONFIRMED |
| SERVFAIL / REFUSED / timeout | UNKNOWN | `dns_error` | UNCERTAIN |

- **Environment finding (VERIFIED, this session):** the container's system
  resolver answered only A queries and returned empty answers for MX, TXT and
  NS (including for `gmail.com`). A verifier that trusts such a resolver would
  wrongly report "no MX" for every domain. emailish therefore runs a
  **resolver self-test** at startup: it queries the MX for a reference domain,
  and if the result is wrong it switches to **DNS-over-HTTPS**
  (Cloudflare/Google JSON API). The backend in use is recorded with each job.

## 4. SMTP mailbox diagnostics

- **Vendors:** all three. MV's subresult enum exposes each protocol stage
  (`no_code_in_banner` … `no_code_in_rcpt_to_response`). MV says "The
  connection is then dropped before any actual message content is
  transmitted." Hunter reports `smtp_server`, `smtp_check` and `block`.
  **VERIFIED (doc)**
- **Protocol (STANDARD, RFC 5321):**
  ```
  connect MX:25 → 220 banner
  EHLO <helo-name>   (falls back to HELO if EHLO gets a 5xx)
  MAIL FROM:<probe-sender>
  RCPT TO:<target>                 ← the verdict
  RCPT TO:<random-nonexistent>     ← catch-all probe, same transaction
  RSET / QUIT                      ← no DATA, so no message is ever sent
  ```
- **Reply interpretation (RFC 5321 + RFC 3463 enhanced codes):**

| Reply | Meaning | emailish result |
|---|---|---|
| 250 / 251 | recipient accepted | accepted (VALID only if the catch-all probe is rejected) |
| 550/551/553 + `5.1.1`, `5.1.10`, "user unknown", "no such user", "does not exist" | mailbox does not exist | INVALID `mailbox_not_found` |
| `5.2.1`, "disabled", "inactive" | mailbox disabled | INVALID `mailbox_disabled` |
| 552 / `x.2.2` / "quota", "full" | mailbox full | RISKY `mailbox_quota_exceeded` |
| 5xx + `5.7.x`, "blocked", "spamhaus", "blacklist", "reputation", "policy", "client host" | **our probe was blocked**, so this says nothing about the mailbox | UNKNOWN `antispam_system` |
| 450/451/452 + "greylist", "try again", `4.7.1` | greylisting | UNKNOWN `greylisted` (queued for a retry pass) |
| other 4xx | temporary failure | UNKNOWN `mail_server_temporary_error` |
| 421 on connect / disconnect mid-session | forcible disconnect | UNKNOWN `forcible_disconnect` |
| TCP timeout / no banner | | TIMEOUT `timeout_exceeded` |
| connection refused / no route | | UNKNOWN `failed_smtp_connection` |
| non-numeric or garbage reply | protocol error | UNKNOWN `unexpected_response` |
| 5xx without a recognizable mailbox code or text | ambiguous permanent failure | UNKNOWN `ambiguous_rejection`, never guessed as INVALID |

  A **generic 5xx is not treated as "mailbox missing"**. A 5xx is classified
  INVALID only when the code or text identifies the recipient. A 5xx caused
  by policy or blocking would otherwise produce false INVALID results that
  delete real customers. This is the most common accuracy failure of naive
  checkers.

- **Conservative rate limiting (INDUSTRY practice; MV's `ip_blocked` and
  ZB's `antispam_system` show that the vendors hit these defenses):**
  - At most **one concurrent connection per MX host** (configurable). There
    is also a minimum interval between connections to the same MX and a
    global concurrency cap.
  - Several recipients of the **same domain share one session**, up to a
    per-session cap (default 10). This cuts the number of connections, which
    is gentler on servers.
  - **Greylist retry pass:** after the main pass, greylisted addresses are
    retried once after a delay (default 90 s; ZB documents a 30-minute
    window, MV says it "waits and retries"). This is configurable.
- **Pre-flight egress check:** before a job, emailish tests whether outbound
  TCP/25 is possible at all. **If port 25 is blocked (true in this research
  container and on most cloud and residential networks), SMTP checks are
  marked "not performed"** and the result is UNKNOWN `smtp_unavailable`, with
  an explicit reason. It is never VALID and never per-email TIMEOUT. Port 25
  is blocked on AWS, GCP and Azure by default and by most consumer ISPs.
- **Sender identity:** the HELO name and MAIL FROM are configurable. For
  reliable results they should be a real domain you control, with matching
  PTR/forward DNS. Probes from unidentified hosts are blocked more often,
  which raises UNKNOWN rates.

## 5. Catch-all (accept-all) detection

- **Vendors:** MV `catch_all` (risky); ZB `catch-all` plus the
  `catchall_domain` flag; Hunter `accept_all` "can give false positives".
  **VERIFIED (doc)**. The probe method is not disclosed by any vendor. The
  random-recipient probe is **INDUSTRY** standard practice.
- **emailish:** in the same SMTP transaction, after the target RCPT, it sends
  `RCPT TO:<random 20-char local part>@domain`.
  - random **rejected** and target accepted → VALID (CONFIRMED, high confidence)
  - random **accepted** → domain is catch-all → CATCH_ALL (domain behaviour
    CONFIRMED; whether this mailbox exists is unknown)
  - random **inconclusive** (4xx or blocked) and target accepted → RISKY
    `catch_all_undetermined`. We do not upgrade it to VALID without evidence.
  - Results are **cached per domain** (memory and SQLite, 24 h TTL), so one
    probe serves the whole list for that domain.
- **Resolving individual catch-all mailboxes** (MV "deep-level ping", ZB
  Verify+) depends on proprietary sending and history data. It is **not
  implemented**, and catch-all addresses stay CATCH_ALL.

## 6. Provider-specific response handling

The receiving platform is identified from the MX hostnames.

| Provider (MX pattern) | Known behaviour | Handling | Evidence |
|---|---|---|---|
| Google (`aspmx.l.google.com`, `gmail-smtp-in.l.google.com`, `*.googlemail.com`) | `550-5.1.1 … account … does not exist`; `452-4.2.2` over quota; `421-4.7.0` / `550-5.7.1` blocks unauthenticated or low-reputation senders | standard table; 4.7.x → UNKNOWN | STANDARD (Google SMTP error reference) |
| Microsoft 365 (`*.mail.protection.outlook.com`) | With Directory-Based Edge Blocking, an unknown recipient gets `550 5.4.1 Recipient address rejected: Access denied` | **5.4.1 from M365 → INVALID `mailbox_not_found`** (it would otherwise be misread as a policy block) | STANDARD (Microsoft Exchange Online NDR documentation) |
| Outlook.com consumer (`*.olc.protection.outlook.com`) | `550 5.5.0 Requested action not taken: mailbox unavailable` for unknown users | mailbox-unavailable text → INVALID | INDUSTRY |
| Yahoo / AOL (`*.yahoodns.net`) | widely reported to accept RCPT for any local part and bounce later | catch-all probe detects this; the provider is flagged as an `smtp_unreliable` profile, and acceptance on its own is never VALID | INDUSTRY (third-party reviews cite Yahoo catch-all accuracy problems for MV) |
| Secure Email Gateways: Mimecast (`*.mimecast.com`), Proofpoint (`*.pphosted.com`, `*.ppe-hosted.com`), Barracuda (`*.barracudanetworks.com`), Cisco IronPort (`*.iphmx.com`) | often accept all RCPTs at the edge or block probes | provider recorded; catch-all probe decides; blocks → UNKNOWN | INDUSTRY; ZB mentions SEGs as a hard case |

## 7. Disposable email intelligence

- **Vendors:** MV "continuously updated database"; ZB `do_not_mail/disposable`
  **and `mx_forward`** (MX-infrastructure detection); Hunter `disposable`.
  **VERIFIED (doc)**
- **emailish, two layers:**
  1. **Domain list:** the community-maintained `disposable-email-domains`
     blocklist (CC0), vendored with an allow-list override.
  2. **MX infrastructure:** a domain whose MX hosts belong to a known
     disposable mail service is disposable even if the domain is new and not
     yet listed. We built this list from data: we resolved the MX records of
     a sample of listed disposable domains and kept only the MX operators
     that are themselves disposable services. This is the same idea as ZB's
     `mx_forward`. Shared infrastructure (Cloudflare Email Routing, Google,
     Microsoft and so on) was explicitly excluded to avoid false positives.
- Disposable addresses skip SMTP, as Hunter does (`verification` is null for
  disposable addresses).

## 8. Free / webmail provider detection
- Vendors: MV `free`, ZB `free_email`, Hunter `webmail`. **VERIFIED (doc)**
- emailish: vendored list of free mailbox providers (Kikobeats
  `free-email-domains`, MIT, derived from HubSpot's list), minus anything on
  the disposable list. This is an informational flag and does not change the
  status.

## 9. Role-based detection
- Vendors: MV `role`; ZB `do_not_mail/role_based` and
  `role_based_catch_all`, with "All emails that we flag as Role-Based are
  valid emails". **VERIFIED (doc)**
- emailish follows the ZB policy. The `role_based` flag is always reported,
  but **status ROLE_BASED is assigned only when the mailbox was accepted**
  (VALID → ROLE_BASED) or the domain is catch-all (sub-status
  `role_based_catch_all`). A role address that fails SMTP stays INVALID, and
  one with an unknown result stays UNKNOWN.

## 10. Risk classification and confidence

Vendors return a status plus a sub-status. MV adds a three-way `quality`
(good/risky/bad) and Hunter adds a 0–100 `score`. Score formulas are
undisclosed.

emailish returns:
- `email_status`: one of VALID, INVALID, CATCH_ALL, DISPOSABLE, ROLE_BASED,
  RISKY, UNKNOWN, TIMEOUT
- `sub_status`: the machine reason code (vocabulary close to ZB/MV)
- `certainty`: **CONFIRMED** (direct, conclusive evidence: a DNS fact, an
  explicit SMTP verdict, or a list match) or **UNCERTAIN** (no conclusive
  evidence, or the evidence is ambiguous)
- `confidence` 0–100: how strongly the gathered evidence supports
  `email_status`. Every value is a documented rule (see
  `technical-architecture.md` §5), not a trained model. The weights are
  priors chosen by us. **They are not calibrated against ground truth**, and
  calibrating them is one purpose of the benchmark harness.

## 11. Unknown and inconclusive results
- All vendors keep an explicit unknown class, do not charge for it, and
  recommend re-verification. **VERIFIED (doc)**
- emailish never converts "no answer" into a verdict. Every UNKNOWN or
  TIMEOUT carries the concrete stage that failed (DNS, connect, banner,
  EHLO, MAIL FROM, RCPT, greylist, block, egress unavailable).

## 12. Bulk processing
- Vendors: file upload, de-duplication (ZB `remove_duplicate`, MV
  `unique_emails`), progress reporting (MV `percent`, `estimated_time_sec`),
  filtered downloads, and asynchronous greylist re-checks. **VERIFIED (doc)**
- emailish: upload → column detection → de-duplication → group by domain →
  domain-batched SMTP sessions with per-MX throttling → greylist retry pass →
  results merged back into **every original row** → CSV and XLSX export.
