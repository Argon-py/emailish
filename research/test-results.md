# Test and Benchmark Results (actual, 2026-11-17)

> The later structured QA round (1,000 synthetic records, SMTP disabled, 7 issues fixed) is in
> [`QA_REPORT.md`](QA_REPORT.md). The test count is now 141.

Environment: cloud container, Python 3.13. **Outbound TCP/25 is blocked** (connection
refused to Google MX). The system DNS resolver answered only A queries, so emailish
switched itself to DNS-over-HTTPS after its MX self-test failed.

## 1. Automated test suite: `pytest`

`124 passed, 3 skipped` (the 3 skipped are the live tests, run separately below).

| file | tests | what it covers |
|---|---|---|
| `tests/test_engine.py` | 38 | full engine against **real SMTP conversations** with local mock servers (`tests/mocksmtp.py`) and a fake DNS table |
| `tests/test_units.py` | 75 | 23 real-world SMTP reply texts (Gmail, Microsoft 365, Outlook.com, Postfix, Exim, qmail), policy rejections never mapped to "mailbox missing", provider identification, syntax (16 cases), role/free/disposable/typo lists, DNS analysis |
| `tests/test_files_api.py` | 11 | CSV, semicolon-delimited Latin-1 CSV, header-less CSV, TXT with noise, XLSX, misleading headers; full HTTP flow upload → start → poll → download CSV and XLSX; column preservation, leading zeros, formula-injection safety, column-name collisions, history, error codes |

Scenario matrix (each is a test in `test_engine.py`; all pass):

| scenario | result |
|---|---|
| existing mailbox, non-catch-all | VALID, CONFIRMED, 90 |
| unknown user `550 5.1.1` | INVALID `mailbox_not_found`, CONFIRMED |
| role address that exists / does not exist | ROLE_BASED / INVALID (role flag kept) |
| catch-all server | CATCH_ALL, UNCERTAIN; one probe per domain for 25 addresses |
| Microsoft 365 `550 5.4.1 Access denied` | INVALID (DBEB), 88 |
| `5.2.1` disabled / `552 5.2.2` full | INVALID `mailbox_disabled` / RISKY `mailbox_quota_exceeded` |
| greylisted once, then accepted | VALID after the retry pass |
| greylisted persistently | UNKNOWN `greylisted`, confidence 0 |
| `554 5.7.1 … spamhaus` on RCPT / banner `554 … spamcop` / MAIL FROM rejected | UNKNOWN `antispam_system` (never INVALID) |
| `550 Administrative prohibition` | UNKNOWN `ambiguous_rejection` |
| banner timeout / RCPT timeout | TIMEOUT |
| garbage banner | UNKNOWN `unexpected_response` |
| connection refused / disconnect after MAIL FROM | UNKNOWN `failed_smtp_connection` / `forcible_disconnect` |
| accepted but catch-all probe rate-limited (4xx) | RISKY `catch_all_undetermined` |
| NXDOMAIN / Null MX / no MX and no A / dangling MX / private MX IP | INVALID |
| no MX but A record | verified through the implicit MX (VALID only after SMTP 250) |
| DNS SERVFAIL | UNKNOWN `dns_error` (never INVALID) |
| port 25 unavailable | UNKNOWN `smtp_unavailable` for every SMTP-dependent address; never VALID |
| `.example`, `.test`, `.invalid`, `.localhost` | INVALID |
| typo domain that accepts everything (`gmaill.com`) | RISKY `possible_typo`, did_you_mean `gmail.com` |
| disposable via list / via MX operator only | DISPOSABLE (SMTP skipped) |
| `DATA` command | never sent (asserted over the whole run) |
| duplicates with different case, spacing or brackets | probed once |
| rate limiter (0.4 s interval, 3 sessions on one MX) | ≥ 0.8 s observed |

### Bugs the tests found and that were fixed
1. **Catch-all probe and greylisting:** a new random probe address on each retry was
   greylisted every time, so the catch-all status never resolved. The probe address is
   now stable per domain.
2. **DoH client reused across event loops** produced "Event loop is closed". The engine
   correctly reported `dns_error` rather than a verdict; the client is now bound per loop.
3. **Typo false positive:** `yopmail.com` was suggested as `hotmail.com` (edit distance 2).
   Distance 2 is now allowed only for long provider names.
4. FastAPI request models were not resolvable (HTTP 422) and were moved to module level.
5. UI drop zone overlapped the heading.

## 2. Live DNS tests: `EMAILISH_LIVE=1 pytest tests/test_live.py -s`

`3 passed`. Real output:

```
user@example.org                          INVALID    does_not_accept_mail  mx=True  Null MX (RFC 7505)
nobody@zz-nonexistent-9f8e7d6c-domain.com INVALID    no_dns_entries        mx=False NXDOMAIN
a@gmail.con                               INVALID    no_dns_entries        mx=False NXDOMAIN. Did you mean a@gmail.com?
test@invalid-domain.example               INVALID    no_dns_entries        mx=False NXDOMAIN
user@example.com                          INVALID    does_not_accept_mail  mx=True  Null MX (RFC 7505)
x@mailinator.com                          DISPOSABLE disposable            mx=True
x@yopmail.com                             DISPOSABLE disposable            mx=True
someone@gmail.com                         UNKNOWN    smtp_unavailable      mx=True  (port 25 blocked here)
someone@httpbin.org                       mx_found=False, domain_valid=True (implicit MX via A record)
```

## 3. End-to-end UI run (headless Chromium against `./run.sh`)
`examples/sample_contacts.xlsx` (10 rows) was uploaded through the UI. The column
"Contact Email" was auto-detected, the job completed, and XLSX and CSV were downloaded
(`examples/sample_contacts_verified.*`). Result: INVALID 5, DISPOSABLE 1, UNKNOWN 4. The 4
UNKNOWN are Gmail, Outlook and Stripe addresses that **could not be SMTP-checked from this
container**. They are correctly not reported as VALID.

## 4. Vendor benchmark: NOT yet run

| step | status |
|---|---|
| Adapter self-test against MillionVerifier demo keys (5) and Hunter `test-api-key` | ✅ all mapped correctly (`python -m benchmark.run_benchmark --selftest`) |
| ZeroBounce sandbox check | ⏳ needs your API key (`--zerobounce-sandbox`, free) |
| Real comparison on controlled addresses | ❌ **not run.** It needs (a) authorized vendor API keys, which were not available, and (b) a host with port 25 egress, without which emailish's mailbox verdicts would all be `smtp_unavailable` and the comparison would only measure the DNS/list layers |

**No accuracy or parity claim is made.** The hypotheses about expected differences are in
`feature-comparison.md`. Run `benchmark/run_benchmark.py` with keys on a suitable host to
confirm or refute them, then tune `replies.py` from the disagreement table.

## 5. Not verified here
- Docker image build: the Docker daemon is not available in this container. `run.sh` was
  verified from a fresh virtualenv.
- Real SMTP behaviour of Google, Microsoft, Yahoo and SEGs against emailish: blocked by
  port 25. The reply handling for them is tested with their documented reply texts.
