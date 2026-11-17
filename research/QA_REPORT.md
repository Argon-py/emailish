# QA Report: emailish (testing round 1, 2026-11-17)

**Scope.** Structured QA of the existing application, with **SMTP disabled**. Syntax,
DNS and dataset-based classification were tested end-to-end through the real app and
also separately, layer by layer. No SMTP connection was made to any third-party mail
server.

**Environment.** Cloud container, Python 3.13. The system resolver drops MX answers, so the
app's `auto` DNS mode correctly switched to DNS-over-HTTPS. Outbound port 25 is blocked,
which is irrelevant for this round because SMTP was disabled on purpose.

## 1. Headline results

| Metric | Round 1 (before fixes) | **Final** |
|---|---:|---:|
| Records tested | 1,000 | **1,000** |
| **Pass** (all 9 compared fields match) | 978 | **1,000** |
| **Fail** | 22 | **0** |
| **Incorrect classification** (wrong `email_status`) | 6 | **0** |
| **Missing result** (row without `email_status`) | 0 | **0** |
| **UNKNOWN results** | 359 | **362**, all expected¹ |
| Application workflow checks (`run_qa.py`) | 47 / 47 | **47 / 47** |
| Browser UI checks (`qa_ui.js`) | 16 / 16 | **16 / 16** |
| Layer tests (`run_layers.py`) | 7 test failures (test bug F5) | **all pass** |
| Automated unit/integration suite (`pytest`) | 124 | **141 passed** (+17 regression tests), 3 live tests skipped by default |

¹ 357 are `smtp_disabled`: mailbox existence cannot be known without SMTP, so UNKNOWN is
the only correct answer. The other 5 are `dns_error` on `icloud.co`, whose DNS delegation is
genuinely broken (SERVFAIL from both Cloudflare and Google). **No UNKNOWN was turned into
VALID**, and the run contains zero VALID, ROLE_BASED or CATCH_ALL results, as required
without SMTP evidence.

The failures were 4 root causes: 2 genuine engine bugs and 2 answer-key errors. Two more
issues were found by the layer test and the UI review, plus one config-dependent engine
bug from a targeted probe. Full investigation: [`FAILED_TESTS.md`](FAILED_TESTS.md).

### Final status distribution (expected = actual)

| email_status / sub_status | count |
|---|---:|
| UNKNOWN / smtp_disabled | 357 |
| INVALID / failed_syntax_check | 180 |
| INVALID / does_not_accept_mail (Null MX) | 167 |
| DISPOSABLE / disposable | 123 |
| INVALID / no_dns_entries (NXDOMAIN) | 69 |
| RISKY / possible_typo | 45 |
| INVALID / empty | 40 |
| INVALID / no_mail_host | 14 |
| UNKNOWN / dns_error | 5 |

### By category (final)

| category | records | pass | | category | records | pass |
|---|---:|---:|---|---|---:|---:|
| valid_syntax_free_provider | 200 | 200 | | typo_domain | 90 | 90 |
| valid_syntax_other | 37 | 37 | | disposable | 120 | 120 |
| nonexistent_domain | 3 | 3 | | whitespace | 60 | 60 |
| reserved_domain_null_mx | 60 | 60 | | empty | 40 | 40 |
| reserved_tld | 80 | 80 | | duplicate | 100 | 100 |
| invalid_syntax | 120 | 120 | | role_based | 90 | 90 |

By expectation type: DETERMINISTIC_OFFLINE 220/220, DETERMINISTIC_DNS 423/423,
REQUIRES_SMTP_EVIDENCE 357/357 (all correctly UNKNOWN).

## 2. Layers tested separately (`run_layers.py`)

| Layer | Tested | Pass | Fail |
|---|---:|---:|---:|
| Syntax (`check_syntax`) | 1,000 | 1,000 | 0 |
| Dataset: disposable list | 780 | 780 | 0 |
| Dataset: free provider | 780 | 780 | 0 |
| Dataset: role-based | 780 | 780 | 0 |
| Dataset: typo suggestion | 780 | 780 | 0 |
| DNS: domain status vs independent oracle | 177 domains | 177 | 0 |
| DNS: top-3 MX hosts vs oracle | 113 domains | 113 | 0 |

## 3. Application workflow (`run_qa.py`: real server process, HTTP only)

| Requirement | Result | Evidence |
|---|---|---|
| Uploading CSV | ✅ | 1,000 rows accepted |
| Uploading XLSX | ✅ | 1,000 rows accepted; **results identical to the CSV run** (0 rows differ) |
| Automatic email column detection | ✅ | `Email Address` detected in CSV, XLSX and through the UI |
| Manual email column selection | ✅ | auto proposed `Email`; choosing `Backup Email` verified that column (30/30 INVALID Null MX). An unknown column is rejected with HTTP 400. In the UI the dropdown highlights the chosen column. |
| Verification progress | ✅ | 186 polls; counter never decreases; final 748/748; phases seen: reading file → syntax → verifying → writing results → done; UI progress bar is monotonic (108 samples) |
| Duplicate handling | ✅ | 1,000 rows → **748 unique verifications**, matching an independent count; all 100 duplicate variants (case, spaces, `<>`, `mailto:`) got exactly the original's result |
| Result classifications | ✅ | 1,000 / 1,000 match the answer key |
| Excel export | ✅ | header and 1,000 rows identical to CSV; **all 23,679 non-empty cells stored as text** (no formulas or numbers, so `=SUM(1,2)` stays text) |
| CSV export | ✅ | UTF-8 with BOM; original 9 columns + 18 result columns, in order |
| Preservation of original columns | ✅ | **0 differing cells** out of 9,000 original cells, in both exports and for both inputs |
| Preservation of leading zeros | ✅ | `Zip`: 539/539, `Customer No`: 1,000/1,000 kept in CSV and XLSX |
| Verification history | ✅ | all jobs listed as done; stored summary equals the counts in the exported file; download links work |
| Every row has a result and an explanation | ✅ | 0 missing `email_status`; every row has `verification_reason` and `verified_at` |

## 4. Processing duration (1,000 rows, SMTP disabled)

| Run | Unique addresses | Upload | Verify | Download (CSV + XLSX) |
|---|---:|---:|---:|---:|
| CSV, cold DNS cache (177 domains looked up over DoH) | 748 | 0.04 s | **10.3 s** | 0.01 s |
| XLSX, same server (DNS cached) | 748 | 0.14 s | **0.9 s** | 0.01 s |
| Manual column file (30 rows) | 30 | 0.01 s | 0.11 s | <0.01 s |
| Browser UI, cold cache, XLSX | 748 | — | 12.1 s | — |

Duration is dominated by DNS lookups for new domains, and two broken domains time out
on both DoH providers (the "long tail"). With SMTP **enabled**, expect much longer runs:
the conservative defaults are 1 connection per mail server, 2 s between connections, and a
90 s greylist retry pass. Large-list timing with SMTP has **not** been measured.

## 5. Bugs fixed in this round

| ID | Bug | Effect before the fix |
|---|---|---|
| F1 | Zero-width space at the edge of an address made it INVALID | Good addresses copied from web or CRM sources were marked INVALID |
| F2 | `mailto:` or `<>` cells labelled `empty` | Misleading sub_status (status was right) |
| F6 | SMTP-disabled banner said "cannot run on this machine" | Misleading UI message |
| F7 | Forced system DNS on a broken resolver was silent | Wrong `mx_found` / Null-MX results with no warning (default `auto` mode unaffected) |

Two answer-key errors (F3, F4) and one test bug (F5) were corrected in the test code with
written reasons. Nothing was reclassified to make a test pass.

## 6. What works
- The upload → verify → download workflow, for CSV, XLSX and TXT. Column auto-detection
  and manual override work, and progress, history and both exports are correct.
- Exact preservation of your data: every original cell, column order, leading zeros, and
  formula-looking text.
- **Syntax**: 1,000/1,000 against hand-written RFC cases, including invisible-character
  trimming at the edges.
- **DNS**: NXDOMAIN, Null MX, no mail host, malformed MX (`~`), implicit MX through the A
  record (IDN `bücher.de`), and real SERVFAIL domains, which give UNKNOWN, never INVALID.
- **Dataset classification**: disposable (list, parent domains), free provider, role-based
  (with look-alike rejection), and typo suggestions.
- **Safety**: with no SMTP evidence the app never outputs VALID, ROLE_BASED or CATCH_ALL;
  every UNKNOWN has confidence 0.

## 7. What does not work, or is not verified yet
- **Mailbox existence has not been tested against real mail servers.** SMTP was disabled
  by design this round, and port 25 is blocked in this environment anyway.
- Catch-all detection, greylisting, and provider-specific replies (Google, Microsoft 365,
  Yahoo, gateways) are tested **only against local mock servers** using documented reply
  texts (`tests/test_engine.py`, `tests/test_units.py`).
- Vendor benchmark (ZeroBounce / MillionVerifier / Hunter): **not run**; it needs your API
  keys. Adapters pass a self-test against the vendors' public test keys.
- Docker image: not built here (no Docker daemon). `run.sh` is verified.
- Performance with SMTP enabled, and lists much larger than 1,000 rows, are not measured.
- Dataset coverage is limited: disposable detection is only as good as the community list
  plus 20 MX operators. A new disposable service on fresh infrastructure will be missed.
- Expected DNS results can drift as domains change; see `TEST_DATA_GUIDE.md`.

## 8. What needs real SMTP testing (proposed round 2)
Run on a server with outbound port 25, a static IP with matching reverse DNS (PTR), and
`EMAILISH_HELO_HOST` / `EMAILISH_MAIL_FROM` set to a domain you own. Use **only addresses
you control**, and keep the default conservative limits.

| Test | Controlled address needed | Expected |
|---|---|---|
| Existing mailbox, non-catch-all | a real mailbox on your own domain | VALID |
| Non-existent mailbox | `random-string@` your non-catch-all domain | INVALID `mailbox_not_found` |
| Catch-all | any address on a domain you configure as catch-all | CATCH_ALL |
| Role mailbox | `info@` your domain (exists) | ROLE_BASED |
| Gmail / Outlook.com | your own accounts, plus `zz-nonexistent-…@` | VALID / INVALID (5.1.1, 5.5.0) |
| Microsoft 365 tenant | your M365 user, plus a non-existent one | VALID / INVALID (5.4.1 DBEB) |
| Greylisting | a Postfix + postgrey server you run | UNKNOWN → VALID after the retry pass |
| Blocking behaviour | — | observe `antispam_system` rates; these must never be INVALID |

Then run `python -m benchmark.run_benchmark your_list.csv --yes` with vendor keys and
review the disagreement table before any accuracy claim is made.

## 9. How to reproduce this QA

```bash
pip install -r requirements-dev.txt
pytest                                  # 141 tests
python test_data/run_qa.py              # end-to-end, SMTP disabled
python test_data/run_layers.py          # layers separately
EMAILISH_SMTP_ENABLED=false python -m uvicorn app.main:app --port 8766 &
node test_data/qa_ui.js http://127.0.0.1:8766 test_data/qa_results/latest   # needs Playwright
```
