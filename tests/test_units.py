"""Unit tests: reply interpretation, syntax, list intelligence, DNS analysis."""
import pytest
from conftest import run

from app.engine import lists, replies
from app.engine.dnslayer import ERROR, NODATA, OK, DnsAnswer, FakeBackend, analyze_domain
from app.engine.syntax import check_syntax

G = replies.identify_provider(["gmail-smtp-in.l.google.com"])
M365 = replies.identify_provider(["contoso-com.mail.protection.outlook.com"])
OTHER = replies.GENERIC


# --- SMTP reply interpretation: real-world reply texts -------------------------
@pytest.mark.parametrize("code,text,prov,kind", [
    (250, "2.1.5 OK k12si123 - gsmtp", G, replies.ACCEPTED),
    (550, "5.1.1 The email account that you tried to reach does not exist. Please try\n5.1.1 double-checking "
          "the recipient's email address for typos or\n5.1.1 unnecessary spaces. "
          "https://support.google.com/mail/?p=NoSuchUser", G, replies.NOT_FOUND),
    (452, "4.2.2 The recipient's inbox is out of storage space.", G, replies.FULL),
    (421, "4.7.0 [203.0.113.9] Our system has detected an unusual rate of unsolicited mail", G, replies.BLOCKED),
    (550, "5.7.1 [203.0.113.9] The IP you're using to send mail is not authorized", G, replies.BLOCKED),
    (550, "5.4.1 Recipient address rejected: Access denied. AS(201806281)", M365, replies.NOT_FOUND),
    (550, "5.4.1 Recipient address rejected: Access denied.", OTHER, replies.BLOCKED),   # not M365: policy, no mailbox verdict
    (550, "5.5.0 Requested action not taken: mailbox unavailable (S2017062302)",
     replies.identify_provider(["eur.olc.protection.outlook.com"]), replies.NOT_FOUND),
    (554, "5.7.1 Service unavailable; Client host [203.0.113.9] blocked using zen.spamhaus.org", OTHER, replies.BLOCKED),
    (550, "<bob@x.com>: Recipient address rejected: User unknown in virtual mailbox table", OTHER, replies.NOT_FOUND),
    (550, "Unrouteable address", OTHER, replies.NOT_FOUND),                                  # Exim
    (553, "sorry, that domain isn't in my list of allowed rcpthosts (#5.7.1)", OTHER, replies.BLOCKED),  # qmail
    (550, "Administrative prohibition", OTHER, replies.AMBIGUOUS),
    (450, "4.7.1 <x@y.com>: Recipient address rejected: Greylisted for 300 seconds", OTHER, replies.GREYLISTED),
    (451, "4.7.1 Please try again later", OTHER, replies.GREYLISTED),
    (450, "4.1.1 <x@y.com>: Recipient address rejected: unverified address: host not found", OTHER, replies.TEMP),
    (451, "Temporary local problem - please try later", OTHER, replies.TEMP),          # Exim server-side fault
    (450, "Requested mail action not taken: mailbox busy", OTHER, replies.TEMP),
    (550, "5.2.1 The email account that you tried to reach is disabled.", G, replies.DISABLED),
    (552, "5.2.2 Mailbox full", OTHER, replies.FULL),
    (550, "5.1.8 Sender address rejected: Domain not found", OTHER, replies.BLOCKED),
    (252, "2.1.5 Cannot VRFY user, but will accept message", OTHER, replies.CANNOT_VERIFY),
    (550, "Requested action not taken: mailbox unavailable, rejected by policy", OTHER, replies.AMBIGUOUS),  # mixed signals
])
def test_classify_rcpt(code, text, prov, kind):
    assert replies.classify_rcpt(code, text, prov).kind == kind


@pytest.mark.parametrize("code,text", [
    (550, "5.4.1 Recipient address rejected: Access denied."),
    (550, "Requested action not taken: mailbox unavailable, rejected by policy"),
    (550, "Administrative prohibition"),
    (554, "5.7.1 Service unavailable; Client host blocked using zen.spamhaus.org"),
    (550, "5.7.1 Unauthenticated email is not accepted from this domain"),
])
def test_policy_rejections_never_mean_mailbox_missing(code, text):
    assert replies.classify_rcpt(code, text, OTHER).kind not in (replies.NOT_FOUND, replies.DISABLED)


def test_provider_identification():
    assert replies.identify_provider(["aspmx.l.google.com"]).key == "google"
    assert replies.identify_provider(["acme-com.mail.protection.outlook.com"]).key == "microsoft365"
    assert replies.identify_provider(["mta5.am0.yahoodns.net"]).accept_all_reported
    assert replies.identify_provider(["us-smtp-inbound-1.mimecast.com"]).seg
    assert replies.identify_provider(["mx0a-00123.pphosted.com"]).key == "proofpoint"
    assert replies.identify_provider(["mail.example.org"]).key == "other"


# --- syntax --------------------------------------------------------------------
@pytest.mark.parametrize("raw,ok", [
    ("john@gmail.com", True), ("  John.Doe+tag@Example.ORG ", True), ("mailto:a@b.co", True),
    ("<a@b.co>", True), ("plainaddress", False), ("@no-local.com", False), ("a@b", False),
    ("a..b@c.com", False), ("a@b..com", False), ("a b@c.com", False), ('"quoted"@c.com', False),
    ("a@[192.168.0.1]", False), ("x" * 65 + "@c.com", False), ("two@@c.com", False), ("a@-c.com", False),
    ("ünïcode@bücher.de", True),
])
def test_syntax(raw, ok):
    assert check_syntax(raw).ok is ok


def test_syntax_normalizes_domain_case_only():
    r = check_syntax("  John.Doe@EXAMPLE.Com ")
    assert r.normalized == "John.Doe@example.com"
    assert check_syntax("x@bücher.de").domain == "xn--bcher-kva.de"


# --- lists -----------------------------------------------------------------------
def test_disposable_and_allowlist():
    assert lists.is_disposable_domain("mailinator.com")
    assert lists.is_disposable_domain("sub.mailinator.com")
    assert not lists.is_disposable_domain("gmail.com")
    assert not lists.is_disposable_domain("protonmail.com")


def test_disposable_mx_operator():
    assert lists.disposable_mx_operator(["mx2.generator.email"]) == "generator.email"
    assert lists.disposable_mx_operator(["route1.mx.cloudflare.net"]) == ""
    assert lists.disposable_mx_operator(["aspmx.l.google.com"]) == ""


def test_free():
    for d in ("gmail.com", "yahoo.com", "outlook.com", "protonmail.com"):
        assert lists.is_free_domain(d), d
    assert not lists.is_free_domain("stripe.com")
    assert not lists.is_free_domain("mailinator.com")   # disposable, not "free"


@pytest.mark.parametrize("local,role", [
    ("info", True), ("Sales", True), ("support+tickets", True), ("sales.uk", True), ("hr", True),
    ("john", False), ("john.sales", False), ("infomaniac", False), ("noreply", True),
])
def test_role(local, role):
    assert lists.is_role(local) is role


@pytest.mark.parametrize("domain,sug", [
    ("gmial.com", "gmail.com"), ("gmail.con", "gmail.com"), ("gmai.com", "gmail.com"),
    ("hotmial.com", "hotmail.com"), ("yahooo.com", "yahoo.com"), ("outlok.com", "outlook.com"),
    ("googlemial.com", "googlemail.com"), ("gmail.com", ""), ("yopmail.com", ""), ("mail.com", ""),
    ("email.com", ""), ("aon.com", ""), ("stripe.com", ""), ("gmx.net", ""),
])
def test_typo(domain, sug):
    assert lists.suggest_domain(domain) == sug


# --- DNS analysis --------------------------------------------------------------
def _dns(**t):
    return FakeBackend(t.get("table", {}))


def test_dns_mx_sorted_and_parsed():
    b = FakeBackend({("a.com", "MX"): DnsAnswer(OK, [(20, "mx2.a.com"), (10, "mx1.a.com")]),
                     ("mx1.a.com", "A"): DnsAnswer(OK, ["93.184.216.34"]), ("mx1.a.com", "AAAA"): DnsAnswer(NODATA),
                     ("mx2.a.com", "A"): DnsAnswer(OK, ["93.184.216.35"]), ("mx2.a.com", "AAAA"): DnsAnswer(NODATA)})
    info = run(analyze_domain(b, "a.com"))
    assert info.status == "ok" and info.mx_names == ["mx1.a.com", "mx2.a.com"] and info.mx_found


def test_dns_mx_host_lookup_error_is_dns_error_not_invalid():
    b = FakeBackend({("a.com", "MX"): DnsAnswer(OK, [(10, "mx.a.com")]),
                     ("mx.a.com", "A"): DnsAnswer(ERROR, error="timeout"),
                     ("mx.a.com", "AAAA"): DnsAnswer(ERROR, error="timeout")})
    assert run(analyze_domain(b, "a.com")).status == "dns_error"


def test_dns_partial_unroutable_is_ok():
    b = FakeBackend({("a.com", "MX"): DnsAnswer(OK, [(10, "mx1.a.com"), (20, "mx2.a.com")]),
                     ("mx1.a.com", "A"): DnsAnswer(OK, ["10.0.0.5"]), ("mx1.a.com", "AAAA"): DnsAnswer(NODATA),
                     ("mx2.a.com", "A"): DnsAnswer(OK, ["93.184.216.34"]), ("mx2.a.com", "AAAA"): DnsAnswer(NODATA)})
    assert run(analyze_domain(b, "a.com")).status == "ok"


# --- regressions found by the QA round (research/FAILED_TESTS.md) ----------------
@pytest.mark.parametrize("raw", ["x@gmail.com​", "​x@gmail.com", " ​ x@gmail.com ﻿ ",
                                 "⁠x@gmail.com", "mailto:​x@gmail.com", " x@gmail.com "])
def test_invisible_edge_characters_are_trimmed(raw):
    r = check_syntax(raw)
    assert r.ok and r.normalized == "x@gmail.com"


def test_invisible_character_inside_address_is_still_invalid():
    assert not check_syntax("x​y@gmail.com").ok


@pytest.mark.parametrize("raw,empty", [("", True), ("   ", True), ("\t", True), ("​", True),
                                       ("mailto:", False), ("<>", False), ('""', False)])
def test_empty_means_blank_cell(raw, empty):
    from app.engine.syntax import is_blank
    r = check_syntax(raw)
    assert not r.ok and is_blank(raw) is empty


def test_forced_system_resolver_that_drops_mx_is_flagged(monkeypatch):
    """QA finding: EMAILISH_DNS_MODE=system on a resolver that returns no MX data used to be silent."""
    from app.engine import dnslayer

    class BrokenResolver:
        name = "system"

        async def query(self, name, rtype):
            return DnsAnswer(NODATA) if rtype == "MX" else DnsAnswer(OK, ["192.0.2.1"])

    monkeypatch.setattr(dnslayer, "SystemBackend", BrokenResolver)
    backend, note = run(dnslayer.pick_backend("system"))
    assert note.startswith("WARNING") and "self-test" in note

    from app.config import Settings
    from app.engine.verifier import Verifier
    monkeypatch.setattr("app.engine.verifier.pick_backend", dnslayer.pick_backend)
    v = Verifier(Settings(dns_mode="system", smtp_enabled=False))
    st = run(v.init())
    assert any("self-test" in w for w in st.warnings)
