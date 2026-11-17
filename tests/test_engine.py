"""End-to-end engine tests against the local mock SMTP cluster and fake DNS."""
import asyncio

from conftest import fake_dns, make_settings, run

from app.engine.dnslayer import DnsAnswer, FakeBackend
from app.engine.models import Certainty, SmtpStatus, Status
from app.engine.verifier import Verifier


def verify(v, *emails):
    return run(v.verify_many(list(emails)))


# --- confirmed mailbox verdicts -------------------------------------------------
def test_valid_mailbox_confirmed(verifier):
    r = verify(verifier, "alice@valid.mock")["alice@valid.mock"]
    assert r.email_status == Status.VALID
    assert r.certainty == Certainty.CONFIRMED
    assert r.smtp_status == SmtpStatus.ACCEPTED
    assert r.catch_all is False
    assert r.confidence == 90
    assert "250" in r.verification_reason and "random address rejected" in r.verification_reason


def test_unknown_mailbox_invalid(verifier):
    r = verify(verifier, "nobody@valid.mock")["nobody@valid.mock"]
    assert r.email_status == Status.INVALID
    assert r.sub_status == "mailbox_not_found"
    assert r.certainty == Certainty.CONFIRMED
    assert r.smtp_code == "550 5.1.1"


def test_role_based_only_after_mailbox_confirmed(verifier):
    res = verify(verifier, "info@valid.mock", "support@valid.mock")
    assert res["info@valid.mock"].email_status == Status.ROLE_BASED
    assert res["info@valid.mock"].certainty == Certainty.CONFIRMED
    # role address that does not exist stays INVALID (role flag still reported)
    assert res["support@valid.mock"].email_status == Status.INVALID
    assert res["support@valid.mock"].role_based is True


def test_catch_all_domain(verifier):
    res = verify(verifier, "anyone@catchall.mock", "sales@catchall.mock")
    a = res["anyone@catchall.mock"]
    assert a.email_status == Status.CATCH_ALL
    assert a.catch_all is True
    assert a.certainty == Certainty.UNCERTAIN
    assert res["sales@catchall.mock"].sub_status == "role_based_catch_all"


def test_catch_all_probe_runs_once_per_domain(cluster, verifier):
    b = cluster.servers["127.0.0.3"]
    before = sum(1 for c in b.log if c.upper().startswith("RCPT TO:<ZZ"))
    verify(verifier, *[f"user{i}@catchall.mock" for i in range(25)])
    after = sum(1 for c in b.log if c.upper().startswith("RCPT TO:<ZZ"))
    assert after - before == 1


def test_m365_dbeb_541_is_invalid_not_blocked(verifier):
    res = verify(verifier, "dave@m365.mock", "ghost@m365.mock")
    assert res["dave@m365.mock"].email_status == Status.VALID
    assert res["dave@m365.mock"].mail_provider == "Microsoft 365"
    g = res["ghost@m365.mock"]
    assert g.email_status == Status.INVALID and g.sub_status == "mailbox_not_found"
    assert g.confidence == 88


def test_mailbox_disabled(verifier):
    r = verify(verifier, "x@disabled.mock")["x@disabled.mock"]
    assert r.email_status == Status.INVALID and r.sub_status == "mailbox_disabled"


def test_mailbox_full_is_risky(verifier):
    r = verify(verifier, "x@full.mock")["x@full.mock"]
    assert r.email_status == Status.RISKY and r.sub_status == "mailbox_quota_exceeded"


# --- inconclusive outcomes must never become VALID or INVALID ---------------------
def test_greylisted_then_resolved_by_retry_pass(verifier):
    r = verify(verifier, "carol@grey.mock")["carol@grey.mock"]
    assert r.email_status == Status.VALID
    assert "greylist retry" in r.verification_reason


def test_greylisted_persistently_is_unknown(verifier):
    r = verify(verifier, "carol@greyforever.mock")["carol@greyforever.mock"]
    assert r.email_status == Status.UNKNOWN and r.sub_status == "greylisted"
    assert r.confidence == 0


def test_spamhaus_block_is_unknown_not_invalid(verifier):
    r = verify(verifier, "x@blocked.mock")["x@blocked.mock"]
    assert r.email_status == Status.UNKNOWN
    assert r.sub_status == "antispam_system"
    assert r.smtp_status == SmtpStatus.BLOCKED


def test_ambiguous_5xx_is_unknown(verifier):
    r = verify(verifier, "x@weird.mock")["x@weird.mock"]
    assert r.email_status == Status.UNKNOWN and r.sub_status == "ambiguous_rejection"


def test_banner_timeout_is_timeout(verifier):
    r = verify(verifier, "x@slow.mock")["x@slow.mock"]
    assert r.email_status == Status.TIMEOUT and r.sub_status == "timeout_exceeded"
    assert "banner" in r.verification_reason


def test_rcpt_timeout_is_timeout(verifier):
    r = verify(verifier, "x@slowrcpt.mock")["x@slowrcpt.mock"]
    assert r.email_status == Status.TIMEOUT


def test_garbage_banner_is_unknown(verifier):
    r = verify(verifier, "x@garbage.mock")["x@garbage.mock"]
    assert r.email_status == Status.UNKNOWN and r.sub_status == "unexpected_response"


def test_connection_refused_is_unknown(verifier):
    r = verify(verifier, "x@refused.mock")["x@refused.mock"]
    assert r.email_status == Status.UNKNOWN and r.sub_status == "failed_smtp_connection"


def test_disconnect_mid_session_is_unknown(verifier):
    r = verify(verifier, "x@dropper.mock")["x@dropper.mock"]
    assert r.email_status == Status.UNKNOWN and r.sub_status == "forcible_disconnect"


def test_banner_554_blocklist_is_unknown(verifier):
    r = verify(verifier, "x@banner554.mock")["x@banner554.mock"]
    assert r.email_status == Status.UNKNOWN and r.sub_status == "antispam_system"


def test_mail_from_rejected_is_unknown(verifier):
    r = verify(verifier, "x@mailfromreject.mock")["x@mailfromreject.mock"]
    assert r.email_status == Status.UNKNOWN and r.sub_status == "antispam_system"
    assert "mail_from" in r.verification_reason


def test_accepted_but_catch_all_probe_inconclusive_is_risky(verifier):
    r = verify(verifier, "erin@probeblock.mock")["erin@probeblock.mock"]
    assert r.email_status == Status.RISKY and r.sub_status == "catch_all_undetermined"
    assert r.catch_all is None


# --- DNS-level verdicts ---------------------------------------------------------
def test_nonexistent_domain(verifier):
    r = verify(verifier, "a@doesnotexist.mock")["a@doesnotexist.mock"]
    assert r.email_status == Status.INVALID and r.sub_status == "no_dns_entries"
    assert r.mx_found is False and r.domain_valid is False


def test_null_mx(verifier):
    r = verify(verifier, "a@nullmx.mock")["a@nullmx.mock"]
    assert r.email_status == Status.INVALID and r.sub_status == "does_not_accept_mail"


def test_missing_mx_with_a_record_uses_implicit_mx(verifier):
    r = verify(verifier, "alice@implicit.mock")["alice@implicit.mock"]
    assert r.mx_found is False
    assert r.email_status == Status.VALID          # verified via the implicit MX, not assumed


def test_missing_mx_and_no_a(verifier):
    r = verify(verifier, "a@nohost.mock")["a@nohost.mock"]
    assert r.email_status == Status.INVALID and r.sub_status == "no_mail_host"


def test_dangling_mx(verifier):
    r = verify(verifier, "a@dangling.mock")["a@dangling.mock"]
    assert r.email_status == Status.INVALID and r.sub_status == "no_mail_host"


def test_dns_failure_is_unknown_not_invalid(verifier):
    r = verify(verifier, "a@dnsfail.mock")["a@dnsfail.mock"]
    assert r.email_status == Status.UNKNOWN and r.sub_status == "dns_error"
    assert r.domain_valid is None


def test_unroutable_mx(cluster):
    v = Verifier(make_settings(cluster, allow_private_mx=False), dns_backend=fake_dns())
    v.status.smtp_available = True
    v._ready = True
    r = run(v.verify_one("a@private.mock"))
    assert r.email_status == Status.INVALID and r.sub_status == "unroutable_ip_address"


# --- list intelligence ------------------------------------------------------------
def test_disposable_list_skips_smtp(cluster, verifier):
    r = verify(verifier, "x@mailinator.com")["x@mailinator.com"]
    assert r.email_status == Status.DISPOSABLE and r.disposable is True
    assert r.smtp_status == SmtpStatus.NOT_CHECKED


def test_disposable_by_mx_infrastructure(verifier):
    r = verify(verifier, "x@brand-new-temp.mock")["x@brand-new-temp.mock"]
    assert r.email_status == Status.DISPOSABLE and r.sub_status == "disposable_mx"


def test_typo_domain_that_accepts_mail_is_risky(verifier):
    r = verify(verifier, "john@gmaill.com")["john@gmaill.com"]
    assert r.email_status == Status.RISKY and r.sub_status == "possible_typo"
    assert r.did_you_mean == "john@gmail.com"


def test_syntax_invalid_and_empty(verifier):
    res = verify(verifier, "not-an-email", "a..b@valid.mock", "")
    assert res["not-an-email"].email_status == Status.INVALID
    assert res["a..b@valid.mock"].sub_status == "failed_syntax_check"
    assert res[""].sub_status == "empty"


# --- environment: SMTP unavailable ------------------------------------------------
def test_smtp_unavailable_never_valid(cluster):
    # egress check host resolves to an address where nothing listens
    dns = fake_dns()
    dns.table[("egress.mock", "A")] = DnsAnswer("ok", ["127.0.0.40"])
    v = Verifier(make_settings(cluster), dns_backend=dns)
    run(v.init())
    assert v.status.smtp_available is False
    res = run(v.verify_many(["alice@valid.mock", "x@catchall.mock", "info@valid.mock", "a@doesnotexist.mock"]))
    for e in ("alice@valid.mock", "x@catchall.mock", "info@valid.mock"):
        assert res[e].email_status == Status.UNKNOWN, e
        assert res[e].sub_status == "smtp_unavailable"
        assert res[e].smtp_status == SmtpStatus.UNAVAILABLE
        assert res[e].mx_found is True
    # DNS-level facts are still reported
    assert res["a@doesnotexist.mock"].email_status == Status.INVALID


def test_smtp_disabled(cluster):
    v = Verifier(make_settings(cluster, smtp_enabled=False), dns_backend=fake_dns())
    run(v.init())
    r = run(v.verify_one("alice@valid.mock"))
    assert r.email_status == Status.UNKNOWN and r.sub_status == "smtp_disabled"


# --- safety / invariants ------------------------------------------------------------
def test_data_command_never_sent(cluster, verifier):
    verify(verifier, "alice@valid.mock", "x@catchall.mock", "carol@grey.mock")
    assert not any(c.upper().startswith("DATA") for c in cluster.all_commands())


def test_invariants_over_all_scenarios(cluster):
    v = Verifier(make_settings(cluster, greylist_retry_delay=0), dns_backend=fake_dns())
    run(v.init())
    emails = [f"{u}@{d}" for d in list(__import__("conftest").SCENARIOS) +
              ["refused.mock", "implicit.mock", "nohost.mock", "nullmx.mock", "dnsfail.mock", "dangling.mock"]
              for u in ("alice", "info", "zed")]
    res = run(v.verify_many(emails))
    for e, r in res.items():
        assert r.verification_reason, e
        if r.email_status == Status.VALID:
            assert r.smtp_status == SmtpStatus.ACCEPTED and r.catch_all is False, e
        if r.email_status == Status.INVALID and r.smtp_status != SmtpStatus.NOT_CHECKED:
            assert r.smtp_status == SmtpStatus.REJECTED, e
        if r.certainty == Certainty.UNCERTAIN and r.email_status in (Status.UNKNOWN, Status.TIMEOUT):
            assert r.confidence == 0, e


def test_duplicates_verified_once(cluster, verifier):
    b = cluster.servers["127.0.0.2"]
    before = sum(1 for c in b.log if "bob.smith@valid.mock" in c.lower())
    res = verify(verifier, "bob.smith@valid.mock", " Bob.Smith@VALID.mock ", "<bob.smith@valid.mock>")
    after = sum(1 for c in b.log if "bob.smith@valid.mock" in c.lower())
    assert after - before == 1
    assert {r.email_status for r in res.values()} == {Status.VALID}


def test_per_mx_rate_limit_spacing(cluster):
    """With rcpt_per_session=1 and a 0.4s interval, 3 addresses need >= 0.8s on one MX."""
    v = Verifier(make_settings(cluster, rcpt_per_session=1, mx_min_interval=0.4), dns_backend=fake_dns())
    run(v.init())
    import time
    t0 = time.monotonic()
    run(v.verify_many(["alice@valid.mock", "info@valid.mock", "bob.smith@valid.mock"]))
    assert time.monotonic() - t0 >= 0.8


def test_reserved_tlds_are_invalid(verifier):
    """RFC 2606/6761 special-use names can never receive mail."""
    res = verify(verifier, "test@invalid-domain.example", "a@foo.test", "b@bar.invalid", "c@host.localhost")
    for e, r in res.items():
        assert r.email_status == Status.INVALID, e
        assert r.certainty == Certainty.CONFIRMED
