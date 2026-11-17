"""Live checks against real DNS (and real SMTP egress detection).

Skipped unless EMAILISH_LIVE=1, because results depend on the network and on
third-party DNS data that can change. Run:  EMAILISH_LIVE=1 pytest tests/test_live.py -v
"""
import asyncio
import os

import pytest

from app.config import Settings
from app.engine.models import Status
from app.engine.verifier import Verifier

pytestmark = pytest.mark.skipif(os.environ.get("EMAILISH_LIVE") != "1", reason="set EMAILISH_LIVE=1")


@pytest.fixture(scope="module")
def live():
    # SMTP is always OFF here: these live tests check DNS facts only and must never send SMTP
    # commands to third-party mail servers, even on a machine where port 25 is open.
    v = Verifier(Settings(smtp_enabled=False, greylist_retry_delay=0,
                          data_dir=__import__("pathlib").Path("/tmp/emailish-live")))
    asyncio.run(v.init())
    print("\nDNS backend:", v.status.dns_backend, "\nSMTP:", v.status.smtp_note)
    return v


def test_live_matrix(live):
    cases = {
        "someone@gmail.com": None,                                 # depends on SMTP egress
        "nobody@zz-nonexistent-9f8e7d6c-domain.com": (Status.INVALID, "no_dns_entries"),
        "user@example.com": (Status.INVALID, "does_not_accept_mail"),     # RFC 7505 Null MX
        "user@example.org": (Status.INVALID, "does_not_accept_mail"),
        "x@mailinator.com": (Status.DISPOSABLE, "disposable"),
        "x@yopmail.com": (Status.DISPOSABLE, "disposable"),
        "a@gmail.con": (Status.INVALID, "no_dns_entries"),
        "test@invalid-domain.example": (Status.INVALID, None),          # reserved TLD: NXDOMAIN
    }
    res = asyncio.run(live.verify_many(list(cases)))
    for e, r in res.items():
        print(f"{e:45} {r.email_status.value:10} {r.sub_status:24} mx={r.mx_found} {r.verification_reason[:90]}")
    for e, want in cases.items():
        if want:
            assert res[e].email_status == want[0], e
            if want[1]:
                assert res[e].sub_status == want[1], e
    g = res["someone@gmail.com"]
    assert g.mx_found is True and g.mail_provider == "Google"
    assert g.email_status == Status.UNKNOWN and g.sub_status == "smtp_disabled"


def test_live_implicit_mx(live):
    r = asyncio.run(live.verify_one("someone@httpbin.org"))
    assert r.mx_found is False and r.domain_valid is True     # A record used as implicit MX
    assert r.email_status == Status.UNKNOWN and r.sub_status == "smtp_disabled"


def test_live_a_suggestion_on_real_typo(live):
    r = asyncio.run(live.verify_one("jane@gmail.con"))
    assert r.did_you_mean == "jane@gmail.com"


# --- real provider endpoints with their documented FREE test keys -----------------
# MillionVerifier demo keys (API_KEY_FOR_*) and Hunter's "test-api-key" hit the real APIs,
# return simulated answers, and use no credits. They prove the HTTP integration end to end.
def _live_provider_verifier(provider):
    async def no_balance(_client):
        return None                      # test keys have no account behind them
    provider.credits = no_balance
    v = Verifier(Settings(smtp_enabled=False, greylist_retry_delay=0,
                          data_dir=__import__("pathlib").Path("/tmp/emailish-live")), provider=provider)
    asyncio.run(v.init())
    return v


@pytest.mark.parametrize("key,status", [("API_KEY_FOR_OK", Status.VALID), ("API_KEY_FOR_INVALID", Status.INVALID),
                                        ("API_KEY_FOR_CATCH_ALL", Status.CATCH_ALL),
                                        ("API_KEY_FOR_DISPOSABLE", Status.DISPOSABLE)])
def test_live_millionverifier_demo_keys(key, status):
    from app.providers import MillionVerifier
    v = _live_provider_verifier(MillionVerifier(key))
    r = asyncio.run(v.verify_one("emailish.qa.live@gmail.com"))   # gmail.com resolves; no SMTP is used
    print(key, r.email_status.value, r.sub_status, r.verified_by, r.verification_reason[:100])
    assert r.email_status == status and r.verified_by == "millionverifier"
    assert "TEST KEY" in r.verification_reason          # simulated answers are labelled as such


def test_live_hunter_test_key():
    from app.providers import Hunter
    v = _live_provider_verifier(Hunter("test-api-key"))
    r = asyncio.run(v.verify_one("emailish.qa.live@gmail.com"))
    print("hunter", r.email_status.value, r.sub_status, r.verification_reason[:100])
    assert r.verified_by == "hunter" and r.email_status == Status.VALID
