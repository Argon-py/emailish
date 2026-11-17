"""Shared lab configuration: DNS for the lab domains (all served by the local Postfix lab
on 127.0.0.1:25) and engine settings for lab runs."""
from __future__ import annotations

from app.config import Settings
from app.engine.dnslayer import NODATA, OK, DnsAnswer, FakeBackend

LAB_DOMAINS = ["lab-valid.mock", "lab-catchall.mock", "lab-grey.mock", "lab-temp.mock", "lab-blocked.mock"]


def lab_dns() -> FakeBackend:
    t = {}
    for d in LAB_DOMAINS:
        t[(d, "MX")] = DnsAnswer(OK, [(10, "mx.lab.mock")])
    t[("mx.lab.mock", "A")] = DnsAnswer(OK, ["127.0.0.1"])
    t[("mx.lab.mock", "AAAA")] = DnsAnswer(NODATA)
    # a domain whose mail server is down: nothing listens on 127.0.0.2:25
    t[("lab-down.mock", "MX")] = DnsAnswer(OK, [(10, "mx.down.mock")])
    t[("mx.down.mock", "A")] = DnsAnswer(OK, ["127.0.0.2"])
    t[("mx.down.mock", "AAAA")] = DnsAnswer(NODATA)
    t[("egress.lab.mock", "A")] = DnsAnswer(OK, ["127.0.0.1"])
    return FakeBackend(t)


def lab_settings(**kw) -> Settings:
    base = dict(helo_host="verifier.lab.mock", mail_from="probe@verifier.lab.mock", smtp_port=25,
                egress_check_host="egress.lab.mock", allow_private_mx=True, mx_min_interval=0.5,
                greylist_retry_delay=8.0, connect_timeout=5, command_timeout=10)
    base.update(kw)
    return Settings(**base)
