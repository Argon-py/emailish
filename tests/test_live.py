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
    v = Verifier(Settings(greylist_retry_delay=0, data_dir=__import__("pathlib").Path("/tmp/emailish-live")))
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
    if not live.status.smtp_available:
        assert g.email_status == Status.UNKNOWN and g.sub_status == "smtp_unavailable"
    assert g.email_status != Status.VALID or g.smtp_status.value == "accepted"


def test_live_implicit_mx(live):
    r = asyncio.run(live.verify_one("someone@httpbin.org"))
    assert r.mx_found is False and r.domain_valid is True     # A record used as implicit MX
    assert r.email_status != Status.VALID or r.smtp_status.value == "accepted"


def test_live_a_suggestion_on_real_typo(live):
    r = asyncio.run(live.verify_one("jane@gmail.con"))
    assert r.did_you_mean == "jane@gmail.com"
