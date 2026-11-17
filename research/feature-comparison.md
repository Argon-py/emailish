# Feature Comparison and Feasibility

✅ = documented / implemented · ⚠️ = partial · ❌ = not offered / not implemented
Vendor columns are based on vendor documentation (see `competitor-analysis.md`).

## Verification signals

| Capability | MillionVerifier | ZeroBounce | Hunter | emailish | Feasible independently? |
|---|---|---|---|---|---|
| Syntax (RFC) | ✅ `invalid_syntax` | ✅ `failed_syntax_check` | ✅ `regexp` | ✅ | Yes, fully |
| Typo suggestion | ✅ `didyoumean` | ✅ `did_you_mean` / `possible_typo` | ❌ | ✅ (flags RISKY, does not auto-invalidate) | Yes |
| Domain existence / NXDOMAIN | ✅ `dns_no_domain` | ✅ `no_dns_entries` | ⚠️ via `mx_records` | ✅ | Yes |
| MX lookup + implicit-MX fallback | ✅ (`dns_no_mx`/`dns_no_a`) | ✅ `mx_found`, `mx_record` | ✅ `mx_records` | ✅ | Yes |
| Null MX (RFC 7505) | not documented | ✅ `does_not_accept_mail` | not documented | ✅ | Yes |
| Unroutable MX IPs | not documented | ✅ `unroutable_ip_address` | not documented | ✅ | Yes |
| SMTP RCPT probe | ✅ | ✅ | ✅ `smtp_check` | ✅ (needs outbound port 25) | **Yes, but only from a host with port 25 egress and a clean IP** |
| Stage-level SMTP diagnostics | ✅ 33 subresults | ✅ 8 unknown sub-statuses | ⚠️ `smtp_server`, `block` | ✅ stage + raw code + text | Yes |
| Greylist retry | ✅ background retry | ✅ Greylist API (~30 min) | ⚠️ 202 polling | ✅ end-of-job retry pass | Yes |
| Catch-all detection | ✅ | ✅ `catchall_domain` | ✅ `accept_all` | ✅ random-RCPT probe, cached per domain | Yes |
| Catch-all **resolution** to valid/invalid | ✅ paid, "30–40%" | ✅ Verify+, provider-specific | ❌ | ❌ | **No.** Relies on sending history, IP pools and proprietary techniques |
| Provider identification | not documented | ✅ `smtp_provider` (beta) | ❌ | ✅ `mail_provider` from MX patterns | Yes |
| Provider-specific reply handling | likely (INFERENCE) | ✅ (M365/Google/SEG per catch-all FAQ) | not documented | ✅ (M365 DBEB 5.4.1, Google, Yahoo, SEGs) | Yes, for publicly documented behaviour |
| Disposable (domain list) | ✅ | ✅ | ✅ | ✅ open-source list | Yes. Coverage depends on list freshness |
| Disposable (MX infrastructure) | not documented | ✅ `mx_forward` | not documented | ✅ data-derived MX operator list | Yes, partially |
| Free/webmail flag | ✅ `free` | ✅ `free_email` | ✅ `webmail` | ✅ | Yes |
| Role-based | ✅ `role` | ✅ (valid-only policy) | ❌ | ✅ (ZB-style policy) | Yes |
| Gibberish local part | ❌ | ❌ | ✅ `gibberish` | ❌ (not in scope) | Yes, heuristically. Low value |
| Spam-trap detection | ❌ | ✅ `spamtrap`, keyword `possible_trap` | ❌ | ⚠️ keyword-only flag (`possible_trap` sub-status) | **No** for real traps (needs trap-network data). Keywords only |
| Abuse / complainer lists | ❌ | ✅ `abuse`, `toxic`, `global_suppression` | ❌ | ❌ | **No.** Proprietary data |
| Activity / engagement data | ❌ | ✅ `active_in_days` | ⚠️ `sources` | ❌ | **No** |
| Web-crawl sources | ❌ | ❌ | ✅ `sources` | ❌ | **No** at useful scale |
| AI / quality score | ✅ `quality` (3 levels) | ✅ AI Scoring (separate API) | ✅ `score` | ✅ rule-based `confidence` with documented rules | Rule-based yes. A calibrated model needs labelled outcome data |
| Name / gender / geo enrichment | ❌ | ✅ | ❌ | ❌ (out of scope) | Not relevant |

## Bulk workflow

| Capability | MillionVerifier | ZeroBounce | Hunter | emailish |
|---|---|---|---|---|
| File upload | ✅ CSV | ✅ CSV/TXT (`email_address_column`) | ✅ (UI) | ✅ CSV, TXT, XLSX |
| Column auto-detection | UI | UI / parameter | UI | ✅ header + content scoring, user override |
| De-duplication | ✅ | ✅ `remove_duplicate` | ? | ✅ (results mapped back to every row) |
| Progress | ✅ `percent`, ETA | ✅ `file_status` | ✅ | ✅ live progress bar |
| Filtered download | ✅ by status/free/role | ✅ | ✅ | ⚠️ full file (CSV and XLSX). Filter in Excel |
| Original columns preserved | ✅ | ✅ | ✅ | ✅ |
| Webhooks / callbacks | ❌ | ✅ `return_url` | ❌ | ❌ (not needed locally) |
| History | ✅ | ✅ | ✅ | ✅ SQLite |

## What can realistically be implemented independently

**Fully feasible (implemented):** syntax, normalization, typo hints, DNS
(NXDOMAIN, MX, implicit MX, Null MX, unroutable), disposable (list + MX
infrastructure), free/role flags, SMTP RCPT diagnostics with stage-level
reason codes, catch-all detection, greylist retry, provider-aware reply
handling, rule-based confidence, bulk file workflow.

**Feasible only with the right network position (deployment requirement, not
code):** SMTP checks need a host with **outbound TCP/25**, a static IP with
**forward-confirmed reverse DNS (PTR)** matching the HELO name, and an IP not
on blocklists. Without these, a large share of results will be UNKNOWN
(`smtp_unavailable` or `antispam_system`). That is the honest outcome, and it
is better than a guess. The vendors operate pools of reputable IPs (MV's
`no_local_ip_available` subresult points to such a pool).

**Not feasible independently (proprietary data):** catch-all mailbox
resolution, spam-trap networks, abuse/complainer and litigator lists, activity
data, web sources, and "accept_all" allow-lists built from years of sending
outcomes. emailish does not pretend to provide these. It reports CATCH_ALL
and UNKNOWN honestly instead.

## Expected accuracy relative to the vendors (hypothesis, NOT measured)

The INVALID/VALID classes on non-catch-all domains are decided by the same
RCPT signal the vendors use. Agreement there should be high **when the probe
runs from a reputable IP**. Expected differences:
1. **More CATCH_ALL / RISKY** than ZB or MV, which resolve some catch-alls
   using proprietary data.
2. **More UNKNOWN** from fresh or unreputed IPs (blocked probes).
3. **Different handling of typos:** ZB = INVALID, emailish = RISKY.
4. **Role-based:** like ZB (`do_not_mail`). MV reports `ok` + `role:true`.

These are hypotheses. They can only be confirmed by running
`benchmark/run_benchmark.py` with authorized vendor keys on a labelled set of
controlled addresses. See the README.
