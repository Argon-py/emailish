# Technical Architecture: emailish

Goal: **upload a list → verify each address with the professional
methodology → download CSV/XLSX with the original columns plus results.**
This is a local tool, not a SaaS: it runs as one process with SQLite and has
no queue broker, no accounts and no billing.

## 1. Components

```
emailish/
├── app/
│   ├── main.py            FastAPI routes + static UI
│   ├── config.py          env-driven settings (HELO, MAIL FROM, limits, DNS mode)
│   ├── db.py              SQLite: jobs, domain cache
│   ├── files.py           CSV/TXT/XLSX reading, email-column detection, export
│   ├── jobs.py            background job runner (asyncio task in the server loop)
│   ├── cli.py             command-line verification (same engine)
│   ├── engine/
│   │   ├── models.py      Result dataclass, status/sub-status vocabulary
│   │   ├── syntax.py      normalization + RFC syntax
│   │   ├── lists.py       disposable / free / role / typo intelligence
│   │   ├── dns.py         resolver backends (system, DoH), self-test, MX analysis
│   │   ├── smtp.py        minimal async SMTP client (banner→EHLO→MAIL→RCPT×n→RSET/QUIT)
│   │   ├── replies.py     reply-code interpretation + provider profiles
│   │   ├── throttle.py    per-MX and global rate limiting
│   │   └── verifier.py    orchestration, catch-all cache, classification, confidence
│   ├── data/              vendored lists (disposable, free, disposable MX operators)
│   └── static/index.html  single-page UI (vanilla JS)
├── tests/                 pytest, local mock SMTP server, fake DNS
├── benchmark/             vendor comparison harness (needs your API keys)
├── Dockerfile, run.sh
```

## 2. Per-address pipeline

```
raw cell
 └─ normalize ─ syntax ──✗──▶ INVALID failed_syntax_check
      │
      ├─ lists: role? free? typo→did_you_mean? disposable-domain?
      │
      ├─ DNS (cached per domain)
      │    NXDOMAIN ─────────────▶ INVALID no_dns_entries
      │    Null MX ──────────────▶ INVALID does_not_accept_mail
      │    no MX & no A/AAAA ────▶ INVALID no_mail_host
      │    MX IPs all unroutable ▶ INVALID unroutable_ip_address
      │    SERVFAIL/timeout ─────▶ UNKNOWN dns_error (DISPOSABLE if listed)
      │    MX hosts → provider profile, disposable-MX check
      │
      ├─ disposable (domain list or MX operator) ─▶ DISPOSABLE (SMTP skipped)
      │
      ├─ SMTP (domain-batched session, throttled)
      │    egress unavailable ──▶ UNKNOWN smtp_unavailable
      │    RCPT target → reply class (replies.py)
      │    RCPT random → catch-all class (cached per domain)
      │
      └─ classify() → status, sub_status, certainty, confidence, reason
```

## 3. Bulk job flow
1. **Upload:** the file is stored under `data/uploads/<job_id>/`. It is parsed
   with every cell as a **string** (`dtype=str`, no NA coercion), so ZIP codes,
   phone numbers and IDs are not altered.
2. **Column detection:** each column gets
   `header_score + 3 × fraction_of_cells_matching_email_pattern`. The best
   column is preselected, and the user can override it.
3. **De-duplication:** normalized addresses are verified once and mapped back to
   every row.
4. **Batching:** unique addresses are grouped by domain. DNS runs once per
   domain. SMTP sessions carry up to `SMTP_RCPT_PER_SESSION` (10) recipients,
   with the catch-all probe sent once per domain.
5. **Throttling:** a global concurrency cap (`SMTP_GLOBAL_CONCURRENCY`, 8),
   one connection per MX host at a time, and a `SMTP_MX_MIN_INTERVAL`
   (2 s) gap between connections to the same MX.
6. **Greylist retry pass:** addresses that got a greylist or temporary reply
   are retried once after `GREYLIST_RETRY_DELAY` (90 s; 0 disables).
7. **Progress** (`done / total` unique addresses, current phase) is written to
   SQLite and polled by the UI.
8. **Export:** original columns in the original order, followed by the result
   columns, as CSV (UTF-8 with BOM for Excel) and XLSX. In XLSX, strings that
   start with `=`, `+`, `-` or `@` are written as text, so original data is
   never turned into live formulas.

## 4. Output columns
Required: `email_status, syntax_valid, domain_valid, mx_found, disposable,
free_email, role_based, smtp_status, catch_all, verification_reason,
confidence, verified_at`.
Added for transparency: `sub_status, certainty, did_you_mean, mx_host,
mail_provider, smtp_code`.

- `smtp_status`: `accepted | rejected | catch_all | greylisted | blocked |
  temp_error | timeout | connection_failed | protocol_error | not_checked |
  unavailable`
- `catch_all`: `Yes | No | Unknown`. `Unknown` is used whenever the probe was
  not conclusive; it is never guessed.

## 5. Classification and confidence rules

`certainty` describes the **deliverability verdict**. It is CONFIRMED only when
direct evidence decides it. `confidence` (0–100) is the strength of the
evidence behind `email_status`. **0 means no verdict could be reached.** These
are hand-set priors, not calibrated probabilities.

| Condition | email_status | sub_status | certainty | confidence |
|---|---|---|---|---|
| empty cell | INVALID | `empty` | CONFIRMED | 99 |
| syntax failure | INVALID | `failed_syntax_check` | CONFIRMED | 99 |
| NXDOMAIN | INVALID | `no_dns_entries` | CONFIRMED | 97 |
| Null MX | INVALID | `does_not_accept_mail` | CONFIRMED | 97 |
| no MX, no A/AAAA | INVALID | `no_mail_host` | CONFIRMED | 95 |
| all MX IPs unroutable | INVALID | `unroutable_ip_address` | CONFIRMED | 92 |
| disposable (domain list) | DISPOSABLE | `disposable` | CONFIRMED | 95 |
| disposable (MX operator) | DISPOSABLE | `disposable_mx` | CONFIRMED | 85 |
| RCPT 5xx, mailbox unknown | INVALID | `mailbox_not_found` | CONFIRMED | 92 |
| RCPT 5.4.1 on Microsoft 365 (DBEB) | INVALID | `mailbox_not_found` | CONFIRMED | 88 |
| RCPT 5.2.1 / disabled | INVALID | `mailbox_disabled` | CONFIRMED | 90 |
| accepted, random rejected | VALID | `accepted` | CONFIRMED | 90 |
| …and role local part | ROLE_BASED | `role_based` | CONFIRMED | 90 |
| accepted, random accepted | CATCH_ALL | `catch_all` | UNCERTAIN | 80 |
| …and role local part | ROLE_BASED | `role_based_catch_all` | UNCERTAIN | 60 |
| accepted, random inconclusive | RISKY | `catch_all_undetermined` | UNCERTAIN | 50 |
| possible typo domain (resolves), not rejected | RISKY | `possible_typo` | UNCERTAIN | 60 |
| mailbox full | RISKY | `mailbox_quota_exceeded` | UNCERTAIN | 70 |
| spam-trap keyword local part, not rejected | RISKY | `possible_trap` | UNCERTAIN | 55 |
| greylisted (after retry) | UNKNOWN | `greylisted` | UNCERTAIN | 0 |
| blocked / anti-spam | UNKNOWN | `antispam_system` | UNCERTAIN | 0 |
| temporary error / ambiguous 5xx / garbage | UNKNOWN | `mail_server_temporary_error` / `ambiguous_rejection` / `unexpected_response` | UNCERTAIN | 0 |
| connection refused / reset | UNKNOWN | `failed_smtp_connection` / `forcible_disconnect` | UNCERTAIN | 0 |
| connect or command timeout | TIMEOUT | `timeout_exceeded` | UNCERTAIN | 0 |
| outbound port 25 unavailable | UNKNOWN | `smtp_unavailable` | UNCERTAIN | 0 |
| DNS failure | UNKNOWN | `dns_error` | UNCERTAIN | 0 |

**Hard invariants (enforced by tests):**
- VALID and ROLE_BASED (non-catch-all) require an SMTP 250/251 for the target
  **and** a rejected random probe. MX records alone never produce VALID.
- INVALID from SMTP requires a recipient-specific rejection. A policy or block
  5xx is UNKNOWN.
- Every result has a non-empty `verification_reason`, including the raw SMTP
  code and text when available.

## 6. DNS backend selection
`DNS_MODE=auto|system|doh`. In `auto`, a startup self-test resolves the MX for
`gmail.com` with the system resolver. If it fails or returns no MX, the engine
uses DoH (`cloudflare-dns.com`, falling back to `dns.google`). This was needed
in the research container, where the system resolver silently dropped MX
answers.

## 7. SMTP egress pre-flight
At job start: a TCP connection to port 25 of a well-known MX (5 s). If it
fails, SMTP is marked unavailable for the job. Every address that needs SMTP
gets UNKNOWN `smtp_unavailable`, with the reason "outbound port 25 blocked
from this host". The UI shows a banner. This avoids reporting a false
per-address TIMEOUT.

## 8. Data store (SQLite)
- `jobs(id, filename, created_at, status, phase, total_rows, unique_emails,
  done, email_column, summary_json, error, smtp_available, dns_backend)`
- `domain_cache(domain, kind, value_json, expires_at)`: DNS results and
  catch-all verdicts (24 h).

## 9. HTTP API (used by the UI)
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/upload` | upload file → job id, columns, detected column, preview |
| POST | `/api/jobs/{id}/start` | start with the chosen column |
| GET | `/api/jobs/{id}` | progress and summary |
| GET | `/api/jobs` | history |
| GET | `/api/jobs/{id}/download?format=csv\|xlsx` | results |
| POST | `/api/verify` | verify a single address (JSON), for debugging |
| GET | `/api/health` | DNS backend, SMTP egress, configuration |

## 10. Deployment requirement for meaningful SMTP results
Run on a host with outbound TCP/25 and a static IP. Set the reverse DNS (PTR)
to the hostname in `EMAILISH_HELO_HOST`, and set `EMAILISH_MAIL_FROM` to an
address on a domain you control. Keep the default conservative limits. Without
this, the tool still produces correct syntax, DNS, disposable, role, free and
typo results, plus honest UNKNOWN results for mailbox existence.
