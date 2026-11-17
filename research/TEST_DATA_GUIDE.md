# Test Data Guide

The QA dataset lives in [`test_data/`](../test_data/). It is **synthetic**: no record is
a real inbox, and nothing in it is ever sent an email. It was generated on 2026-11-17
(seed `20261117`) and is reproducible.

## Files

| file | what it is |
|---|---|
| `synthetic_1000.csv` | the "customer file" you would upload: 1,000 rows with columns `Record ID, First Name, Last Name, Company, Email Address, Zip, Customer No, Phone, Notes` |
| `synthetic_1000.xlsx` | the same rows as an Excel file. Every cell is stored as text, so leading zeros and formula-looking notes stay intact. |
| `synthetic_1000_expected.csv` | **answer key**, one row per `Record ID` (see the column list below) |
| `two_email_columns.csv` | 30 rows with two email columns (`Email`, `Backup Email`), used to test **manual** column selection |
| `dns_evidence.json` | frozen DNS facts for every domain used, with the capture timestamp and source |
| `generate_test_data.py` | regenerates everything above |
| `run_qa.py` | end-to-end QA: starts the real app with SMTP disabled, drives it over HTTP, and compares every row with the answer key |
| `run_layers.py` | tests the syntax, dataset and DNS layers **separately** |
| `qa_ui.js` | browser-level QA of the UI (headless Chromium) |
| `qa_results/round1/` | results of the first run (22 failures) plus the answer key used then |
| `qa_results/latest/` | results of the final run (`summary.json`, `comparison.csv`, `failures.csv`, `layers.json`, `ui_checks.json`, a UI screenshot and the downloaded exports) |

## How expectations were set (and why they are not circular)

Expected results were **not** produced by the emailish engine. They come from three
independent sources:

1. **Hand-written rules** in `generate_test_data.py`: the invalid-syntax patterns and why
   each is invalid, role words, the typo → intended-domain map, and which rows are
   duplicates of which.
2. **An independent DNS oracle**: a ~60-line DNS-over-HTTPS client inside the generator
   that shares no code with `app/engine`. It looks up MX, then A/AAAA for up to 3 mail
   hosts, and records a fact per domain: `ok`, `nxdomain`, `null_mx`, `no_mail_host`,
   `unroutable` or `dns_error`. The facts are frozen in `dns_evidence.json`.
3. **The vendored datasets** (`app/data/*.txt`), parsed independently by the generator.

The expected *status* for each record is derived from those facts using the classification
spec in `technical-architecture.md` §5, applied with **SMTP disabled**.

## Expectation types (column `expectation_type`)

| type | records | meaning |
|---|---:|---|
| `DETERMINISTIC_OFFLINE` | 220 | Needs no network: syntax errors, empty cells, special-use TLDs rejected by syntax (`.test`, `.invalid`, `.localhost`). Must always produce the same result. |
| `DETERMINISTIC_DNS` | 423 | Fixed by public DNS facts captured in `dns_evidence.json`: Null MX, NXDOMAIN, dead disposable domains, typo domains. Stable unless the domain's DNS changes (see "Evidence drift"). |
| `REQUIRES_SMTP_EVIDENCE` | 357 | Whether the mailbox exists **cannot be known** without SMTP. With SMTP disabled the only correct answer is `UNKNOWN / smtp_disabled`. **VALID would be a failure.** Their true deliverability is unknown and is not claimed. |

The **flags** (`syntax_valid`, `disposable`, `free_email`, `role_based`, `did_you_mean`)
are deterministic for every record, whatever its type.

## Categories

| category | records | what it covers | expected (SMTP disabled) |
|---|---:|---|---|
| `valid_syntax_free_provider` | 200 | `emailish.qa.NNNN.xxxx@` at 12 free providers (Gmail, Yahoo, Outlook, Hotmail, iCloud, AOL, Proton, GMX, Zoho, Yandex, mail.com, Live), some with `+tag` or `_` | UNKNOWN `smtp_disabled`, free_email Yes |
| `valid_syntax_other` | 37 | upper-case domains, plus-tags, apostrophe, IDN (`bücher.de`, implicit MX via A record) | UNKNOWN `smtp_disabled` |
| `nonexistent_domain` | 3 | valid syntax, non-existent domain or TLD (`gmail.c`, `*.zzqq`, `*.notarealtld`) | INVALID `no_dns_entries` |
| `reserved_domain_null_mx` | 60 | `example.com/.org/.net` (RFC 2606), which publish a Null MX (RFC 7505) | INVALID `does_not_accept_mail` |
| `reserved_tld` | 80 | 60 × `.test/.invalid/.localhost` (RFC 6761) + 20 × `.example` | INVALID: syntax for the first group, NXDOMAIN for `.example` |
| `invalid_syntax` | 120 | 40 hand-written patterns × 3: missing or double `@`, dots, spaces, commas, brackets, quoted local part, IP literal, local part > 64, label > 63, underscore, two addresses in one cell, `N/A`, `(at)`, `mailto:` alone, `<>`, embedded newline… | INVALID `failed_syntax_check` |
| `role_based` | 90 | 54 role words at Null-MX domains, 24 role words with `+emailish-qa` tag at free providers, 12 non-role look-alikes (`infomaniac`, `john.sales`) | role flag as written; status INVALID (Null MX) or UNKNOWN. **Never ROLE_BASED**: that status needs a confirmed mailbox. |
| `typo_domain` | 90 | 18 typo domains × 5 (`gmial.com`, `gmail.con`, `hotmial.com`, `yaho.com`, `icloud.co`…) | `did_you_mean` always set. Status depends on DNS: RISKY `possible_typo` (45), INVALID (30), DISPOSABLE (10, typo domains on the disposable list), UNKNOWN `dns_error` (5, `icloud.co` has a broken delegation) |
| `disposable` | 120 | 10 well-known + 110 random domains from `app/data/disposable_domains.txt` | disposable flag Yes. DISPOSABLE (90, including 1 with a DNS failure) or INVALID when the domain is dead (30: 25 NXDOMAIN, 3 no mail host, 2 Null MX) |
| `whitespace` | 60 | leading/trailing space, tab, NBSP (U+00A0), zero-width space (U+200B) | identical to the trimmed address |
| `empty` | 40 | `""`, `" "`, `"   "`, `"\t"` | INVALID `empty` |
| `duplicate` | 100 | copies of earlier addresses: exact, upper-case, surrounding spaces, `<…>`, `mailto:`, upper-case domain | exactly the original's result, and verified only once |

The free-provider records exercise the "valid email syntax" requirement. Their local
parts are deliberately implausible as real inboxes.

Other columns test data preservation: `Zip` (539 values with leading zeros), `Customer No`
(all 8 digits with leading zeros), and `Notes`, which contains `=SUM(1,2)`,
`=HYPERLINK(...)`, `+44…`, `-…`, `@…`, commas, quotes, semicolons and Unicode.

### Real-world DNS cases the data happened to include
- `icloud.co` and `maildy.site` have broken delegations: Cloudflare **and** Google both
  return SERVFAIL. These are real DNS-failure test cases.
- `mailnator.com` publishes a malformed MX (`300 ~.`), which tests a mail host that does
  not resolve.
- `gmaill.com` and `hotmail.co` exist but have no MX and no A record.
- `yahooo.com`, `iroid.com` and `jpco.org` publish Null MX.

## Answer key columns (`synthetic_1000_expected.csv`)
`record_id, category, scenario, expectation_type, email, expected_status,
expected_sub_status, expected_syntax_valid, expected_domain_valid, expected_mx_found,
expected_disposable, expected_free_email, expected_role_based, expected_did_you_mean,
duplicate_of, dns_fact, rationale`

A record **passes** only if all nine compared fields match: status, sub_status,
syntax_valid, domain_valid, mx_found, disposable, free_email, role_based and
did_you_mean.

## Regenerate / run

```bash
python test_data/generate_test_data.py              # reuses dns_evidence.json
python test_data/generate_test_data.py --refresh-dns   # re-captures DNS facts first
python test_data/run_qa.py                          # end-to-end, SMTP disabled
python test_data/run_layers.py                      # syntax / dataset / DNS separately
python -m uvicorn app.main:app --port 8766 &        # with EMAILISH_SMTP_ENABLED=false
node test_data/qa_ui.js http://127.0.0.1:8766 test_data/qa_results/latest
```

## Evidence drift
DNS facts can change (domains expire, MX records move). If a `DETERMINISTIC_DNS` record
starts failing, first run `--refresh-dns` and compare `dns_evidence.json` with the version
in git. If the fact changed, the expectation changes with it. That is drift, not an
engine bug.

## What this dataset cannot test
Mailbox existence, catch-all detection, greylisting, and provider-specific SMTP replies
all need SMTP. They are covered offline by `tests/` (local mock SMTP servers). Against
real servers they need **controlled addresses you own**; see the SMTP test plan in
`QA_REPORT.md`.
