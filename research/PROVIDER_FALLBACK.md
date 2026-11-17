# Provider Fallback: Accurate Mailbox Results on Any Computer

## The problem
To know whether a mailbox exists (whether mail to it would bounce), something has to ask the
recipient's mail server over SMTP on **port 25**. Home and office internet connections, and
most cloud servers by default, block that port. On such a machine emailish can settle
syntax, domain, Null-MX, disposable, typo and role questions itself, but mailbox existence
stays **UNKNOWN**.

## The solution
ZeroBounce, MillionVerifier and Hunter run their own SMTP verification infrastructure.
emailish now sends them **only the addresses it could not settle itself**, and merges their
answers into the same output file.

```
your file ─► emailish (free): syntax · DNS/MX · Null MX · disposable · typo · role · SMTP if port 25 works
                │
                ├─ settled locally (INVALID, DISPOSABLE, VALID by own SMTP, ...) ──► kept, no credits used
                │
                └─ still UNKNOWN / TIMEOUT / catch-all undetermined ──► provider (1 call per unique address)
                                                                           │
                                                                           ▼
                           VALID / INVALID / CATCH_ALL / ...  with  verified_by = provider
```

## Setup (Windows, macOS, Linux)
1. Create an account with **one** provider and copy its API key:
   - MillionVerifier: https://app.millionverifier.com/api
   - ZeroBounce: https://www.zerobounce.net/members/API
   - Hunter: https://hunter.io/api-keys
2. In the emailish folder, copy `.env.example` to `.env` and put the key on one line, e.g.
   `MILLIONVERIFIER_API_KEY=abc123...`. `.env` is git-ignored, so the key is never committed.
3. Check the key and your balance. This is free and uses no credits:

   ```powershell
   .\.venv\Scripts\python.exe -m app.cli provider-check
   ```
4. Start emailish (`.\run.bat --no-smtp` on a home connection). The banner shows
   "Mailbox checks via MillionVerifier … N credits available".
5. Upload your file as usual. The download has a new column, **`verified_by`**: `emailish`
   or the provider's name.

## Cost control
- Only unsettled addresses are sent: bad syntax, dead domains, Null MX and disposable
  domains never cost a credit.
- Duplicates (case, spaces, `<…>`, `mailto:`) are sent **once**.
- `EMAILISH_PROVIDER_MAX_CHECKS` (default 5000) caps paid checks per uploaded file. Anything
  above the cap stays UNKNOWN, and its reason says why.
- The available credit balance is read at startup; emailish never attempts more checks
  than the balance allows.
- On a key or credit error, emailish **stops calling the provider immediately** for that job.
- Catch-all results are not sent unless `EMAILISH_PROVIDER_INCLUDE_CATCH_ALL=true`.
- Providers do not charge for their own "unknown" results (vendor documentation).

## How provider answers are mapped

| Provider says | emailish status | certainty |
|---|---|---|
| ZeroBounce `valid`, MillionVerifier `ok`, Hunter `valid` | VALID (ROLE_BASED for role addresses) | CONFIRMED |
| ZeroBounce `valid / accept_all` | VALID, flagged: based on ZeroBounce's history, not a mailbox check | UNCERTAIN |
| `invalid` (all three) | INVALID | CONFIRMED |
| ZeroBounce `catch-all`, MillionVerifier `catch_all`, Hunter `accept_all` | CATCH_ALL | UNCERTAIN |
| `disposable`, ZeroBounce `do_not_mail/disposable` | DISPOSABLE | CONFIRMED |
| ZeroBounce `spamtrap`, `abuse`, other `do_not_mail` | RISKY | UNCERTAIN |
| Hunter `webmail` | VALID / INVALID / RISKY per Hunter's `result` | |
| `unknown`, errors, Hunter 451 (owner opted out) | UNKNOWN | UNCERTAIN |

The provider's own status is kept in `sub_status` (e.g. `millionverifier:invalid/no_mailbox`)
and in `verification_reason`. Results from vendor **test keys** are labelled
"TEST KEY: simulated result".

## Privacy
Addresses sent to a provider leave your computer and are processed under that provider's
terms; ZeroBounce, for example, states that US endpoints process data in the US. Only the
address itself is sent, none of your other columns. With no key configured, nothing is sent
anywhere.

## Limits and honesty
- Accuracy for these addresses is the provider's accuracy, not emailish's. The
  `verified_by` column makes the source visible.
- Catch-all domains stay CATCH_ALL for most addresses with every provider.
- **Tested:** the simulated provider API (38 tests: selection, caps, fatal errors, network
  errors, polling, mapping, job summary), and the real MillionVerifier and Hunter endpoints
  with their free test keys (5 live tests).
- **Not tested here:** ZeroBounce's real endpoint. It refuses this build environment's IP
  (Cloudflare 1020), so it was tested against its documented response format only. Run
  `provider-check`, then a 5-address file, after adding a ZeroBounce key.
- **No paid key was used**, so no real (non-simulated) provider verdict has been observed yet.

## Bounce-safe sending policy
| Status | Send? |
|---|---|
| VALID | yes |
| ROLE_BASED | yes, if you really want shared inboxes |
| CATCH_ALL | only in a small, separate segment; watch bounces |
| RISKY | no (or review manually) |
| UNKNOWN / TIMEOUT | no; re-verify later |
| INVALID / DISPOSABLE | never |
