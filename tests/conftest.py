import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import Settings  # noqa: E402
from app.engine.dnslayer import ERROR, NODATA, OK, DnsAnswer, FakeBackend  # noqa: E402
from app.engine.verifier import Verifier  # noqa: E402
from mocksmtp import (Behaviour, MockCluster, accept_all, fixed, greylist_then,  # noqa: E402
                      mailbox_set)

# domain -> (mx hostname, loopback ip, behaviour factory)
SCENARIOS = {
    "valid.mock": ("mx.valid.mock", "127.0.0.2", lambda: Behaviour(mailbox_set({"alice", "info", "bob.smith"}))),
    "catchall.mock": ("mx.catchall.mock", "127.0.0.3", lambda: Behaviour(accept_all)),
    "grey.mock": ("mx.grey.mock", "127.0.0.4", lambda: Behaviour(greylist_then(mailbox_set({"carol"})))),
    "greyforever.mock": ("mx.greyforever.mock", "127.0.0.5",
                         lambda: Behaviour(greylist_then(mailbox_set({"carol"}), times=99))),
    "slow.mock": ("mx.slow.mock", "127.0.0.6", lambda: Behaviour(accept_all, banner_delay=5)),
    "slowrcpt.mock": ("mx.slowrcpt.mock", "127.0.0.7", lambda: Behaviour(accept_all, rcpt_delay=5)),
    "blocked.mock": ("mx.blocked.mock", "127.0.0.8", lambda: Behaviour(fixed(
        554, "5.7.1 Service unavailable; Client host [203.0.113.9] blocked using zen.spamhaus.org"))),
    "m365.mock": ("m365-test.mail.protection.outlook.com", "127.0.0.9", lambda: Behaviour(mailbox_set(
        {"dave"}, unknown=(550, "5.4.1 Recipient address rejected: Access denied. "
                                "For more information see https://aka.ms/EXOSmtpErrors [BN8NAM12FT0.eop-nam12]")))),
    "full.mock": ("mx.full.mock", "127.0.0.10", lambda: Behaviour(mailbox_set(
        set(), unknown=(552, "5.2.2 <{a}>: Mailbox full")))),
    "weird.mock": ("mx.weird.mock", "127.0.0.11", lambda: Behaviour(fixed(550, "Administrative prohibition"))),
    "garbage.mock": ("mx.garbage.mock", "127.0.0.12", lambda: Behaviour(accept_all, garbage_banner=True)),
    "dropper.mock": ("mx.dropper.mock", "127.0.0.13", lambda: Behaviour(accept_all, drop_after_mail=True)),
    "banner554.mock": ("mx.banner554.mock", "127.0.0.14", lambda: Behaviour(
        accept_all, banner=(554, "5.7.1 No SMTP service for 203.0.113.9: listed at bl.spamcop.net"))),
    "gmaill.com": ("mx.gmaill.com", "127.0.0.15", lambda: Behaviour(accept_all)),
    "disabled.mock": ("mx.disabled.mock", "127.0.0.16", lambda: Behaviour(mailbox_set(
        set(), unknown=(550, "5.2.1 <{a}>: mailbox disabled")))),
    "probeblock.mock": ("mx.probeblock.mock", "127.0.0.17", lambda: Behaviour(
        lambda a, n: (250, "2.1.5 Ok") if a.startswith("erin@") else
        (450, "4.7.1 Too many recipients, slow down"))),
    "mailfromreject.mock": ("mx.mailfromreject.mock", "127.0.0.18", lambda: Behaviour(
        accept_all, mail_reply=(550, "5.7.1 Sender address rejected: Access denied (no PTR)"))),
}
REFUSED_IP = "127.0.0.40"   # nothing listens here


def fake_dns(scenarios=SCENARIOS) -> FakeBackend:
    t = {}
    for dom, (mx, ip, _) in scenarios.items():
        t[(dom, "MX")] = DnsAnswer(OK, [(10, mx)])
        t[(mx, "A")] = DnsAnswer(OK, [ip])
        t[(mx, "AAAA")] = DnsAnswer(NODATA)
    t[("refused.mock", "MX")] = DnsAnswer(OK, [(10, "mx.refused.mock")])
    t[("mx.refused.mock", "A")] = DnsAnswer(OK, [REFUSED_IP])
    t[("mx.refused.mock", "AAAA")] = DnsAnswer(NODATA)
    # implicit MX: no MX record, A points at the 'valid' server
    t[("implicit.mock", "MX")] = DnsAnswer(NODATA)
    t[("implicit.mock", "A")] = DnsAnswer(OK, ["127.0.0.2"])
    t[("implicit.mock", "AAAA")] = DnsAnswer(NODATA)
    # no MX and no address records
    t[("nohost.mock", "MX")] = DnsAnswer(NODATA)
    t[("nohost.mock", "A")] = DnsAnswer(NODATA)
    t[("nohost.mock", "AAAA")] = DnsAnswer(NODATA)
    # null MX
    t[("nullmx.mock", "MX")] = DnsAnswer(OK, [(0, "")])
    # DNS failure
    t[("dnsfail.mock", "MX")] = DnsAnswer(ERROR, error="SERVFAIL")
    # MX pointing at private address space
    t[("private.mock", "MX")] = DnsAnswer(OK, [(10, "mx.private.mock")])
    t[("mx.private.mock", "A")] = DnsAnswer(OK, ["10.1.2.3"])
    t[("mx.private.mock", "AAAA")] = DnsAnswer(NODATA)
    # dangling MX (host has no A/AAAA)
    t[("dangling.mock", "MX")] = DnsAnswer(OK, [(10, "mx.gone.mock")])
    t[("mx.gone.mock", "A")] = DnsAnswer(NODATA)
    t[("mx.gone.mock", "AAAA")] = DnsAnswer(NODATA)
    # disposable via MX infrastructure (domain not on any list)
    t[("brand-new-temp.mock", "MX")] = DnsAnswer(OK, [(10, "mx1.generator.email")])
    t[("mx1.generator.email", "A")] = DnsAnswer(OK, ["127.0.0.3"])
    t[("mx1.generator.email", "AAAA")] = DnsAnswer(NODATA)
    # a listed disposable domain
    t[("mailinator.com", "MX")] = DnsAnswer(OK, [(10, "mail.mailinator.com")])
    t[("mail.mailinator.com", "A")] = DnsAnswer(OK, ["127.0.0.3"])
    t[("mail.mailinator.com", "AAAA")] = DnsAnswer(NODATA)
    # egress check host
    t[("egress.mock", "A")] = DnsAnswer(OK, ["127.0.0.2"])
    return FakeBackend(t)


@pytest.fixture(scope="session")
def cluster():
    c = MockCluster()
    for dom, (mx, ip, make) in SCENARIOS.items():
        c.add(ip, make())
    yield c
    c.close()


def make_settings(cluster, **kw) -> Settings:
    base = dict(helo_host="verifier.example.org", mail_from="probe@example.org", smtp_port=cluster.port,
                connect_timeout=1.5, command_timeout=1.5, mx_min_interval=0.0, greylist_retry_delay=0.3,
                egress_check_host="egress.mock", allow_private_mx=True, global_concurrency=8,
                rcpt_per_session=10, data_dir=Path("/tmp/emailish-test-data"))
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def verifier(cluster):
    v = Verifier(make_settings(cluster), dns_backend=fake_dns())
    asyncio.run(v.init())
    assert v.status.smtp_available, v.status.smtp_note
    return v


def run(coro):
    return asyncio.run(coro)
