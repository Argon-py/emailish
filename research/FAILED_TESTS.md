# Failed Tests: Investigation Log (QA round, 2026-11-17)

Every failure was investigated to its root cause before anything was changed.
**Engine bugs** were fixed in the code, each with a regression test. **Answer-key or
test errors** were corrected in the test data with the reasoning recorded. No
classification was changed just to make a test pass, and no uncertain address became
VALID.

Round 1 raw results: `test_data/qa_results/round1/` (`failures.csv`, `comparison.csv`,
`answer_key_round1.csv`).

## Summary

| # | Found by | Records / checks | Root cause | Verdict | Status |
|---|---|---|---|---|---|
| F1 | `run_qa.py` round 1 | 12 records (6 wrong status) | Zero-width space (U+200B) at the edge of an address made it INVALID | **Engine bug**: false INVALID | ✅ fixed |
| F2 | `run_qa.py` round 1 | 6 records | Cells containing only `mailto:` or `<>` were labelled `empty` | **Engine bug** (labelling) | ✅ fixed |
| F3 | `run_qa.py` round 1 | 3 records | `x@gmail.c` was expected as a syntax error | **Answer-key error** | ✅ key corrected |
| F4 | `run_qa.py` round 1 | 1 record | `amail.com` was expected to have no typo hint | **Answer-key error** | ✅ key corrected |
| F5 | `run_layers.py` | 7 domains | MX list compared with *all* MX hosts; the engine keeps the top 3 | **Test bug** | ✅ test corrected |
| F6 | `qa_ui.js` screenshot review | UI banner | With SMTP *disabled*, the banner said checks "cannot run on this machine", plus an irrelevant HELO warning | **UI bug** (misleading text) | ✅ fixed |
| F7 | targeted probe | configuration | `EMAILISH_DNS_MODE=system` on a resolver that drops MX answers gave silently wrong MX data | **Engine bug** (config-dependent) | ✅ now warns |

Round 1: **978 / 1,000 records passed, 22 failed (6 wrong status), 0 missing.**
Final run: **1,000 / 1,000 passed.**

---

## F1: Zero-width space made good addresses INVALID (engine bug)

- **Records:** 12 in the `whitespace` category, e.g. `emailish.qa.6048.b601@gmail.com​`
  and `​emailish.qa.6059.f97d@example.net`.
- **Expected:** classified exactly like the trimmed address (UNKNOWN for free providers,
  INVALID Null MX for example.*).
- **Got:** `INVALID / failed_syntax_check`, reason "The email address contains unsafe
  characters: ZERO WIDTH SPACE".
- **Root cause:** `syntax.clean()` used `str.strip()`, which removes Unicode *whitespace*
  only. U+200B, U+200C, U+200D, U+2060 and U+FEFF are *format* characters, so they
  survived. They are invisible and appear when addresses are copied from web pages, CRMs
  or Excel. The user sees a correct address and the tool told them to delete it.
- **Impact:** 6 of the 12 changed status (UNKNOWN → INVALID); the other 6 were INVALID
  either way (Null-MX domains) but had wrong `syntax_valid`, `domain_valid` and `mx_found`.
  With SMTP enabled, a real, deliverable customer address would have been reported
  INVALID.
- **Fix (`app/engine/syntax.py`):** invisible format characters are trimmed **only at the
  edges**, together with whitespace and wrappers. Inside an address they are still an
  error (`x​y@gmail.com` stays INVALID).
- **Regression tests:** `test_invisible_edge_characters_are_trimmed` (6 cases),
  `test_invisible_character_inside_address_is_still_invalid`,
  `test_zero_width_space_does_not_make_a_good_address_invalid` (full engine with mock SMTP).

## F2: Wrapper-only cells labelled `empty` (engine bug, labelling)

- **Records:** 6 (`mailto:` × 3, `<>` × 3) in `invalid_syntax`.
- **Expected:** `INVALID / failed_syntax_check`. **Got:** `INVALID / empty`.
- **Root cause:** "empty" was decided *after* stripping `mailto:` and brackets. A cell that
  contains text is not empty, and a user filtering on `sub_status = empty` to find blank
  rows would have been misled. The status (INVALID) was already correct.
- **Fix:** new `syntax.is_blank()`. `empty` now means the cell holds only whitespace or
  invisible characters. Wrapper-only cells get `failed_syntax_check` with the reason "No
  address found: the cell only contains wrapper characters ('mailto:')".
- **Regression tests:** `test_empty_means_blank_cell` (7 cases),
  `test_wrapper_only_cells_are_syntax_invalid_not_empty`.

## F3: `x@gmail.c` (answer-key error)

- **Expected (key):** `failed_syntax_check`. **Got:** `INVALID / no_dns_entries`,
  syntax_valid Yes.
- **Investigation:** RFC 1035 labels may be a single character, so `gmail.c` is legal
  *syntax*. It fails because no such TLD exists, which the engine established with DNS
  evidence (NXDOMAIN). The engine's answer is correct and better evidenced; the key was
  wrong.
- **Correction:** the pattern was replaced in the syntax list by `emailish.qa@@`. `gmail.c`
  moved to a new `nonexistent_domain` category (3 records: `gmail.c`, `*.zzqq`,
  `*.notarealtld`) with its expectation taken from the DNS oracle.

## F4: `amail.com` got "did you mean gmail.com" (answer-key error)

- **Expected (key):** no suggestion. **Got:** `…@gmail.com`. Status DISPOSABLE was correct.
- **Investigation:** `amail.com` is one edit from `gmail.com`. The documented typo rule
  (edit distance 1 from a popular provider) applies to every domain. The key had assumed
  that domains taken from the disposable list never get a hint, which is not a rule of
  the spec. The same rule correctly flags `gmial.com`, which is also on the disposable
  list.
- **Correction:** `KNOWN_LOOKALIKES = {"amail.com": "gmail.com"}` in the generator, with a
  comment. The suggestion is informational only and never changes the status.

## F5: MX host list mismatch on 7 domains (test bug)

- **Domains:** `gmail.com` (5 MX), `anappthat.com` (5), `cocoro.uk` (7), `digi-value.fr` (4),
  `sendnow.win` (7), `shortmail.net` (5), `uma3.be` (7).
- **Investigation:** the engine keeps the **3 most-preferred** MX hosts (`max_hosts=3` in
  `analyze_domain`; SMTP failover uses at most 2). Each engine list equals the oracle's top 3
  in preference order. The test compared against all hosts, and sorted alphabetically.
- **Correction:** `run_layers.py` now compares with the oracle's top 3 in preference order.
  Result: 113 / 113.

## F6: Misleading banner when SMTP is disabled (UI bug)

- **Found in:** the screenshot from `qa_ui.js` (the text check itself passed).
- **Problem:** with `EMAILISH_SMTP_ENABLED=false` the banner said "Mailbox (SMTP) checks
  cannot run on this machine", which suggests a network problem. It also showed the HELO
  hostname warning, which is irrelevant when SMTP is off.
- **Fix:** `/api/health` now returns `smtp_enabled`. The UI says "Mailbox (SMTP) checks
  are turned off … reported as UNKNOWN (smtp_disabled), never VALID". The HELO warning is
  only raised when SMTP is enabled. The UI QA was re-run: 16 / 16.

## F7: Forced system DNS on a broken resolver was silent (engine bug, config-dependent)

- **Found by:** a targeted probe after the main run. This container's system resolver
  answers A queries but returns nothing for MX.
- **Problem:** with `EMAILISH_DNS_MODE=system`, `gmail.com` was reported with
  `mx_found=No` and itself as the "implicit MX". By the same path a Null-MX domain would be
  UNKNOWN instead of INVALID. No warning was shown. The default `auto` mode was never
  affected: it self-tests and switches to DNS-over-HTTPS.
- **Fix (`dnslayer.pick_backend`, `Verifier.init`):** forced `system` mode now runs the same
  MX self-test. On failure a **WARNING** is attached to the engine status and shown in the
  UI banner, `/api/health`, each job's engine info and the CLI. The explicit setting is
  respected rather than overridden, because people usually force `system` when DoH is not
  reachable.
- **Regression test:** `test_forced_system_resolver_that_drops_mx_is_flagged`.

---

## Not failures, but recorded observations
- **Progress has a long tail:** in the cold-cache run, 238 of 748 addresses were done at
  1.4 s and the last 21 took about 2 s more. These are domains with slow or failing DNS
  (`icloud.co` and `maildy.site` time out on both DoH endpoints). Expected behaviour.
- **Unique count:** 1,000 rows → 748 unique verifications. The 252 skipped rows are the 100
  intended duplicates plus natural repeats: syntax patterns × 3, empty cells, role words
  repeating at the same domain. All duplicate variants received the original's result.
