# SMTP Mailbox Verification: Investigation, Implementation and Enablement

Date: 2026-11-17. Scope: why mailbox (SMTP) checks were off, what was implemented and
tested, what infrastructure is required, and exactly how to enable them safely.

**Bottom line.** The SMTP verification engine is implemented and has now been proven
against a **real mail server**: Postfix 3.8 with postgrey, hosting test domains we control.
**17/17** end-to-end rows and **6/6** integration tests matched. The full UPLOAD → VERIFY
→ DOWNLOAD workflow works with SMTP on. It has **not** been run against real internet mail
providers yet, because that needs a machine with outbound port 25, which neither this
build environment nor (most likely) your laptop has. Until you run it from such a server,
mailbox results are reported as UNKNOWN, never guessed.

---

## 1. Why you saw "Mailbox (SMTP) checks are turned off"

SMTP can be off for four independent reasons, at different layers. The app reports which
one applies.

| Layer | Where | What happens | Result for SMTP-dependent addresses |
|---|---|---|---|
| **1. Configuration switch** | `EMAILISH_SMTP_ENABLED` → `app/config.py` (`smtp_enabled`, default **true**). `run.bat --no-smtp` sets it to `false`. Checked in `Verifier.init()` and `Verifier._domain_outcomes()`. | No connection at all | UNKNOWN / `smtp_disabled` |
| **2. Domain allowlist** (new) | `EMAILISH_SMTP_ALLOWED_DOMAINS` → `Settings.smtp_permitted()` | Only listed domains are contacted | others: UNKNOWN / `smtp_not_permitted` |
| **3. Infrastructure** | `Verifier.check_egress()` runs at startup and before every job: a TCP connect to port 25, no SMTP commands | If port 25 is blocked, SMTP is marked unavailable | UNKNOWN / `smtp_unavailable` |
| **4. Receiving-server policy** | `replies.classify_rcpt()` | The server blocks, greylists or rate-limits the probe | UNKNOWN / `antispam_system`, `greylisted`, … |

**Your case:** the banner text "are turned off" is layer 1. You started with
`run.bat --no-smtp` (or `EMAILISH_SMTP_ENABLED=false`). Starting with plain `run.bat`
turns it on, **but** layer 3 will then almost certainly report port 25 blocked, because
home and office ISPs block outbound port 25. Confirm this with `diagnose` (§4).

Note that `python -m app.cli health` cannot answer the port-25 question while SMTP is
switched off: it skips the egress test in that case. Use `diagnose` instead.

## 2. What the SMTP engine does

Module by module (unchanged unless marked new):

| Capability | Implementation | Verified against real Postfix? |
|---|---|---|
| MX discovery | `dnslayer.analyze_domain`: MX sorted by preference; RFC 5321 implicit MX via A/AAAA; RFC 7505 Null MX; unroutable and dangling MX detected; top 3 hosts kept | DNS layer: 177/177 domains vs an independent oracle (QA round) |
| Connectivity | `smtp.probe_session`: connect with a 10 s timeout; failover to the next MX only when no SMTP conversation started; per-command 15 s timeout | ✅ server down → UNKNOWN `failed_smtp_connection` |
| Protocol | banner 220 → EHLO (HELO fallback) → MAIL FROM → RCPT ×N → catch-all RCPT → RSET → QUIT. **Never DATA.** | ✅ Postfix log: 0 sessions with `data=` |
| Response interpretation | `replies.classify_rcpt`: RFC 5321 codes + RFC 3463 enhanced codes + reply text; a 5xx counts as "mailbox missing" only when it is recipient-specific | ✅ native `550 5.1.1 User unknown`, `550 5.2.1`, `552 5.2.2`, `451 4.3.0`, `554 5.7.1`, `504 5.5.2 Helo command rejected`, `450 4.7.1 too many recipients` |
| Mailbox acceptance / rejection | 250/251 with a rejected random probe → VALID; recipient-specific 5xx → INVALID | ✅ |
| Temporary failures | 4xx → UNKNOWN; queued for the retry pass | ✅ |
| Greylisting | stable catch-all probe address per domain; end-of-job retry pass (`EMAILISH_GREYLIST_RETRY_DELAY`, default 90 s) | ✅ real postgrey: greylisted, then VALID after retry |
| Timeouts | connect/banner/command timeouts → TIMEOUT | mock servers only (real timeouts need an unresponsive host; see §6) |
| Catch-all | random `zz…` recipient in the same session, cached per domain for 24 h | ✅ Postfix virtual-alias catch-all → CATCH_ALL |
| Provider restrictions | policy/blocklist/HELO/rate-limit rejections → UNKNOWN, never INVALID; Microsoft 365 `5.4.1` → INVALID (DBEB); Yahoo and gateway profiles | ✅ real HELO restriction and real anvil rate limit → UNKNOWN |
| Conservative limits | 1 connection per MX host, 2 s between connections, 8 sessions globally, 10 recipients per session | ✅ 23 addresses → 3 Postfix sessions, 24 RCPTs (23 + 1 probe) |
| **Domain allowlist** (new) | `EMAILISH_SMTP_ALLOWED_DOMAINS` | ✅ no connection to non-allowed domains (Postfix log) |
| **Readiness diagnosis** (new) | `python -m app.cli diagnose` | ✅ sends only EHLO/QUIT (Postfix log: `ehlo=1 quit=1 commands=2`) |

### Classification definitions (no rules were changed)

| Status | Rule in the engine |
|---|---|
| VALID | the server accepted the recipient (250/251) **and** rejected a random address on the same domain. Syntax or MX alone never produces VALID. |
| INVALID | syntax failure, non-existent domain, Null MX, no mail host, or a recipient-specific SMTP rejection (`5.1.1`, unknown user, `5.2.1` disabled, M365 `5.4.1`) |
| CATCH_ALL | the random address was accepted too, so the individual mailbox cannot be established |
| DISPOSABLE | the domain is on the disposable list, or its MX belongs to disposable-mail infrastructure |
| RISKY | accepted but the catch-all status is undetermined; mailbox full; typo domain; spam-trap keyword |
| UNKNOWN | blocked, greylisted after retry, temporary error, ambiguous reply, DNS failure, or SMTP disabled/unavailable/not permitted |
| ROLE_BASED, TIMEOUT | kept from your original specification: role address with a confirmed (or catch-all) mailbox; server did not answer in time |

## 3. Review: DNS, timeouts, provider restrictions

**Handled correctly (tested):**
- DNS failures (SERVFAIL, timeout) → UNKNOWN, never INVALID. A broken system resolver is
  detected by the MX self-test, and `auto` mode switches to DNS-over-HTTPS. A forced
  `system` resolver that fails the self-test raises a visible warning.
- A blocked port 25 is detected once and reported as `smtp_unavailable`, so you don't get
  1,000 individual timeouts.
- Blocklist, policy, HELO and rate-limit rejections → UNKNOWN `antispam_system`.
- Session-level failures (banner, EHLO or MAIL FROM refused) are applied to the whole
  domain without reconnecting, so a server that rejects us is not hammered.

**Known limitations (documented, not hidden):**
1. **No STARTTLS.** Probes run in plaintext. RFC 3207 says public MX servers must not
   require TLS, so this rarely matters. If a server answers `530 Must issue STARTTLS`, the
   result is UNKNOWN (`antispam_system`): safe, but the label is imprecise.
2. **IPv4 first.** The engine uses the first resolved address of each MX host (A before
   AAAA). A host reachable only over IPv6 from an IPv4-only machine gives UNKNOWN.
3. **No relays or proxies.** Verification must speak to the recipient's MX on port 25.
   **Port 587 or 465 cannot be used**: those are submission ports for sending through
   your own provider, and they don't reveal anything about other domains' mailboxes.
   Using a relay or rotating IPs to get around blocks would be evading provider
   restrictions, and is deliberately not done.
4. Providers that accept everything and bounce later can only be detected as CATCH_ALL.
   That covers Yahoo/AOL (reported), many secure gateways, and Microsoft 365 tenants
   without DBEB. Resolving those needs sending history, which is not attempted.

## 4. Does YOUR machine allow SMTP? Run this

This build environment could not reach your laptop, so the test has to run on your side.
It **sends no MAIL, RCPT or DATA**. By default it tests against `portquiz.net`, a neutral
port-test service, not a mail server.

Windows PowerShell, in the `emailish` folder (after `.\run.bat` has created `.venv`):

```powershell
.\.venv\Scripts\python.exe -m app.cli diagnose
```

Or with Windows' own tool:

```powershell
Test-NetConnection portquiz.net -Port 25
```

Linux/macOS: `python -m app.cli diagnose`. If you own a domain with a mail server, test
that server instead (banner and EHLO only):
`python -m app.cli diagnose --domain yourdomain.com`.

How to read the result:

| Output | Meaning |
|---|---|
| `outbound TCP/25 … connected` and `SMTP greeting: banner '220 …'` | port 25 works |
| `ConnectionRefusedError` | your ISP or firewall rejects port 25. **Expected on home connections.** |
| `TimeoutError` / no answer | port 25 is silently dropped (ISP, corporate firewall) |
| connected but `no valid SMTP banner` | something intercepts port 25 (antivirus "mail shield", corporate proxy) |
| `PTR does not match HELO host` | fine for a laptop test, but on a server it must be fixed |

**This build environment's result** (for reference): port 25 is refused, there is no PTR,
and HELO is not an FQDN, so it is **NOT READY**. That is why internet mailbox
verification could not be demonstrated here.

## 5. Infrastructure required for real-world SMTP diagnostics

| Requirement | Why | How |
|---|---|---|
| **Outbound TCP/25** | recipient mail servers only answer on 25 | A VPS or dedicated server whose provider allows port 25. **Most cloud providers block it by default** and lift it on request. AWS documents a removal request; Vultr unblocks via a support ticket; policies at DigitalOcean, Linode and Hetzner vary by account and change over time, so check the provider's current documentation. Residential ISPs almost always block it. |
| **Static IPv4 with reverse DNS (PTR)** | many servers reject or tarpit clients without forward-confirmed reverse DNS | Set the PTR in the provider's panel to e.g. `verify.yourdomain.com`; create an A record `verify.yourdomain.com → server IP`. |
| **HELO name** | must be that FQDN | `EMAILISH_HELO_HOST=verify.yourdomain.com` |
| **MAIL FROM on your domain** | some servers verify the sender domain (SPF/callback) | `EMAILISH_MAIL_FROM=verify-bounces@yourdomain.com`, a real mailbox; publish SPF for yourdomain.com that includes the server IP (e.g. `v=spf1 ip4:SERVER_IP -all`, merged with your existing SPF) |
| **Clean IP reputation** | blocklisted IPs get `554 5.7.1` → UNKNOWN for everything | check the IP at check.spamhaus.org before you start |
| **Conservative volume** | high RCPT volume from one IP gets it blocklisted | keep the defaults; don't raise concurrency |
| **No public UI** | the app has no login | bind to 127.0.0.1 and use an SSH tunnel (already set in `deploy/`) |

## 6. Deployment configuration (a Linux server with port 25 allowed)

**One command** (Ubuntu/Debian, as root). It installs, writes the settings, runs the readiness
check, and starts the service on 127.0.0.1 only:

```bash
curl -fsSLO https://raw.githubusercontent.com/Argon-py/emailish/main/deploy/install.sh
sudo bash install.sh --helo verify.yourdomain.com --mail-from verify-bounces@yourdomain.com --allow yourdomain.com
```

Use `--dry-run` to see every step first. The steps it automates are listed below.

Files in [`deploy/`](../deploy/): `emailish.env.example` and `emailish.service`. The
`docker-compose.yml` is now bound to localhost.

```bash
# on the server (Ubuntu/Debian), as root
apt-get update && apt-get install -y python3-venv git
useradd --system --home /opt/emailish emailish
git clone https://github.com/Argon-py/emailish.git /opt/emailish
python3 -m venv /opt/emailish/.venv && /opt/emailish/.venv/bin/pip install -r /opt/emailish/requirements.txt
mkdir -p /etc/emailish /var/lib/emailish && chown emailish: /var/lib/emailish
cp /opt/emailish/deploy/emailish.env.example /etc/emailish/emailish.env   # then edit it
cp /opt/emailish/deploy/emailish.service /etc/systemd/system/
# 1) readiness check BEFORE enabling the service
cd /opt/emailish && set -a && . /etc/emailish/emailish.env && set +a && .venv/bin/python -m app.cli diagnose --domain yourdomain.com
# 2) only when it prints READY:
systemctl enable --now emailish
```

From your laptop:

```bash
ssh -L 8000:127.0.0.1:8000 you@your-server
```

Then open http://127.0.0.1:8000.

Docker alternative: copy `deploy/emailish.env.example` to `.env`, edit it, then run:

```bash
docker compose up -d --build
```

The image itself has not been built in this environment.

## 7. How to enable SMTP safely, step by step
1. Run `diagnose` on the machine you intend to use (§4). Continue only if it says READY.
2. Set `EMAILISH_SMTP_ALLOWED_DOMAINS` to **domains you own** (comma-separated).
3. Start emailish **without** `--no-smtp`. The banner shows "SMTP is limited to: …".
4. Upload a list containing only your own test addresses; follow
   [`REAL_WORLD_TESTING_GUIDE.md`](REAL_WORLD_TESTING_GUIDE.md).
5. Compare results with what you know is true. Only after that passes, decide whether to
   clear the allowlist for general use.

## 8. Evidence
- `tests/test_lab_mta.py`: 6 tests against real Postfix + postgrey (`EMAILISH_LAB_MTA=1`), all pass.
- `lab/run_lab_e2e.py`: full app workflow with SMTP on; results in `lab/results/` (17/17).
- `lab/setup_lab_mta.sh`: reproducible lab (disposable container or VM only).
- `tests/test_engine.py`: 38 mock-server scenarios, plus allowlist and diagnose tests; 145 tests pass in total.
- Postfix log checks: zero DATA commands; diagnose sessions `ehlo=1 quit=1`; no session for non-allowed domains.
