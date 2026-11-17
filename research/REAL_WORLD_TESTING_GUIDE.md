# Real-World Testing Guide

How to test emailish beyond the lab, safely, using **only addresses and domains you
control**. Each stage says what it needs. Do the stages in order.

Ground rules for every stage:
- Never put made-up addresses at Gmail, Outlook or any domain you don't own into a list
  while SMTP is on. That probes someone else's mailbox.
- Keep `EMAILISH_SMTP_ALLOWED_DOMAINS` set to your own domains while testing.
- No email is ever sent: the engine stops at `RCPT TO` and never issues `DATA`.
- Commercial API keys are only used in stage 6, and only with `--yes`.

Windows commands are for PowerShell in the `emailish` folder. On Linux/macOS replace
`.\.venv\Scripts\python.exe` with `python` (or `.venv/bin/python`).

---

## Stage 0: one-time setup on your computer

```powershell
git pull
.\run.bat --no-smtp            # first run creates .venv and installs everything; Ctrl+C to stop
.\.venv\Scripts\python.exe -m pip install pytest
```

## Stage 1: what runs on your computer right now (no SMTP, no domain, no keys)

| Check | Command | Expected |
|---|---|---|
| Automated test suite (mock SMTP servers on localhost, simulated provider APIs) | `.\.venv\Scripts\python.exe -m pytest` | `183 passed, 14 skipped` |
| 1,000-record QA through the real app, SMTP disabled | `.\.venv\Scripts\python.exe test_data\run_qa.py` | `records: {'pass': 1000 …}`, `checks: 47 passed, 0 failed` |
| Syntax / dataset / DNS layers separately | `.\.venv\Scripts\python.exe test_data\run_layers.py` | every layer `fail 0` |
| Live DNS facts (SMTP forced off) | `$env:EMAILISH_LIVE="1"; .\.venv\Scripts\python.exe -m pytest tests\test_live.py -s` | `3 passed` |
| Benchmark adapter self-test (vendors' public test keys, **0 credits**) | `.\.venv\Scripts\python.exe -m benchmark.run_benchmark --selftest` | 7 × `OK` |
| Your own spreadsheet, SMTP off | `.\run.bat --no-smtp`, then upload at http://127.0.0.1:8000 | syntax, DNS, disposable, free, role and typo results; mailbox results UNKNOWN `smtp_disabled` |

The 9 skipped tests are the 3 live-DNS tests (opt-in) and the 6 Postfix-lab tests, which
need a Linux machine where you can install Postfix. The DNS-based checks need internet
access (DNS-over-HTTPS).

## Stage 1b: accurate mailbox results on this computer with a provider key (needs one API key)

If you don't want to run a server, add one provider key to `.env` (see `.env.example`).
emailish then sends the addresses it cannot settle to that provider. Test it with a
**5-address file of addresses you own** before a large list:

```powershell
.\.venv\Scripts\python.exe -m app.cli provider-check     # free: key + credit balance
.\run.bat --no-smtp                                        # then upload the 5-address file
```

Check the `verified_by` column, and that your real addresses come back VALID and a
non-existent address on your own domain comes back INVALID. Details:
[`PROVIDER_FALLBACK.md`](PROVIDER_FALLBACK.md).

## Stage 2: can this computer do SMTP? (no domain, no keys)

```powershell
.\.venv\Scripts\python.exe -m app.cli diagnose
```

This connects to port 25 of `portquiz.net`, a neutral port-test service, and reports your
public IP's reverse DNS. It sends no MAIL, RCPT or DATA. Windows' built-in alternative:

```powershell
Test-NetConnection portquiz.net -Port 25
```

`TcpTestSucceeded : False` means port 25 is blocked.

- **Port blocked** (the usual result on home and office connections): mailbox checks
  cannot run from this computer. Go to stage 3.
- **Port open:** you still need a real HELO hostname with matching reverse DNS. Residential
  IPs can't have that, and most mail servers block probes from them. Use a server
  (stage 3) for meaningful results.

## Stage 3: a server with outbound port 25 (needs a VPS and a domain you own)

Follow [`SMTP_ENABLEMENT_REPORT.md` §5–6](SMTP_ENABLEMENT_REPORT.md):
1. Rent a VPS and confirm (or request) that outbound port 25 is allowed.
2. DNS for your domain: `verify.yourdomain.com A <server IP>`; set the **PTR** of the server
   IP to `verify.yourdomain.com` in the provider's panel; add the server IP to your SPF.
3. Install emailish (systemd or Docker) with `deploy/emailish.env.example` filled in:
   `EMAILISH_HELO_HOST`, `EMAILISH_MAIL_FROM`, and
   `EMAILISH_SMTP_ALLOWED_DOMAINS=yourdomain.com`.
4. On the server:

   ```bash
   python -m app.cli diagnose --domain yourdomain.com
   ```

   Continue only when it prints **READY**. With `--domain`, it opens your own mail server
   (banner + EHLO + QUIT only).

## Stage 4: prepare controlled test addresses (needs a domain you own)

On **your own domain** (any mail host: Google Workspace, Microsoft 365, Zoho, your own
Postfix…):

| Make sure this exists | Purpose | Expected result |
|---|---|---|
| `test.real@yourdomain.com` (a real mailbox) | positive response | VALID (or CATCH_ALL if the domain is catch-all) |
| `info@yourdomain.com` (a real shared/role mailbox) | role handling | ROLE_BASED |
| `zz-no-such-user-8h3k@yourdomain.com` (does **not** exist) | negative response | INVALID `mailbox_not_found`. On a non-catch-all Microsoft 365 domain with DBEB, also INVALID. |
| a second domain (or subdomain) configured as **catch-all** | catch-all behaviour | CATCH_ALL for both real and random addresses |
| optionally, your own Gmail / Outlook.com address | large-provider behaviour | VALID, or UNKNOWN if the provider blocks the probe |

Recommended extras, only if you run your own mail server:
- **Greylisting:** install postgrey on your test mail server. Expect UNKNOWN on the first
  pass and VALID after the retry pass (`EMAILISH_GREYLIST_RETRY_DELAY` must exceed the
  postgrey delay).
- **Temporary failure:** a Postfix access map entry `tempfail@yourdomain.com 451 4.3.0
  Temporary lookup failure` → UNKNOWN.
- **Restriction:** a Postfix access map `554 5.7.1 …` entry, or `smtpd_client_recipient_rate_limit`
  → UNKNOWN, never INVALID.
  (`lab/setup_lab_mta.sh` shows exactly these settings.)

Write them into a CSV using the benchmark format
(`benchmark/controlled_addresses.example.csv`):

```
email,expected,note
test.real@yourdomain.com,VALID,real mailbox
info@yourdomain.com,ROLE_BASED,real role mailbox
zz-no-such-user-8h3k@yourdomain.com,INVALID,does not exist
anything-4f2a@your-catchall-domain.com,CATCH_ALL,catch-all domain
```

## Stage 5: run the controlled test (server, your domain, no keys)

```bash
# on the server, with the env file loaded and EMAILISH_SMTP_ALLOWED_DOMAINS set to your domains
python -m benchmark.run_benchmark controlled_addresses.csv   # no keys set = no vendor calls
```

The report (`benchmark/results/<time>/report.md`) shows emailish against your ground
truth. You can also run the normal workflow: upload the same file in the UI (through the
SSH tunnel) and download the Excel file.

How to judge it:
- **Every** `INVALID` must be an address that really does not exist. A real address marked
  INVALID is a serious bug; please report it with its `verification_reason` and `smtp_code`.
- `UNKNOWN` with `antispam_system` means the receiving server blocked the probe. Check
  reverse DNS and IP reputation (`diagnose`) before treating it as a tool problem.
- `VALID` must only appear for addresses that exist on a non-catch-all domain.

## Stage 6: commercial comparison (needs API keys, spends credits)

Optional free check first: ZeroBounce sandbox addresses don't use credits, but still need
your key.

```bash
export ZEROBOUNCE_API_KEY=...            # PowerShell: $env:ZEROBOUNCE_API_KEY="..."
python -m benchmark.run_benchmark --zerobounce-sandbox
```

Real comparison (≈1 credit per address per vendor; Hunter 0.5):

```bash
export ZEROBOUNCE_API_KEY=... MILLIONVERIFIER_API_KEY=... HUNTER_API_KEY=...
python -m benchmark.run_benchmark controlled_addresses.csv          # dry run: shows the cost
python -m benchmark.run_benchmark controlled_addresses.csv --yes    # spends credits
```

## Stage 7: before general use
Only after stage 5 matches your ground truth: clear `EMAILISH_SMTP_ALLOWED_DOMAINS` (or
widen it), keep the conservative limits, and watch the share of `antispam_system`
results. A rising share means your IP is being blocked; slow down rather than adding IPs.

---

### Summary: what needs what

| Needs | Checks |
|---|---|
| **Just your computer** | pytest (145), 1,000-record QA, layer tests, live DNS tests, benchmark self-test, `diagnose`, and verifying your own spreadsheets for syntax, DNS, Null MX, disposable, free, role and typo |
| **A server with outbound port 25** (+ PTR/HELO) | any mailbox-level result: VALID, INVALID-by-SMTP, CATCH_ALL, greylisting, provider restrictions |
| **A domain you control** | the controlled test addresses (stage 4), PTR/HELO/SPF identity, catch-all test domain |
| **Commercial API keys** | ZeroBounce sandbox check; the vendor comparison in `benchmark/run_benchmark.py --yes` |
| **A Linux machine you can reconfigure** (optional) | the Postfix lab: `sudo bash lab/setup_lab_mta.sh`, then `EMAILISH_LAB_MTA=1 pytest tests/test_lab_mta.py` and `python lab/run_lab_e2e.py` |
